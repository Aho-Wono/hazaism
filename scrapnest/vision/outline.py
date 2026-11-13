"""切り出したマスクから輪郭（直線＋円弧）を作る。

1. マスクの輪郭を取り出す（外周と穴）
2. 輪郭の各点を、法線方向でいちばん強いエッジへ寄せる（影は勾配がゆるいので拾わない）
3. 板厚による視差を補正する
4. 直線と円弧に当てはめる
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np
from scipy.ndimage import gaussian_filter1d, median_filter
from shapely import affinity
from shapely.geometry import Polygon

from ..geom import outline as go
from . import arcfit
from .segment import Rectified, Segmentation

SNAP_STEP_MM = 0.4       # 輪郭に沿って縁を探す間隔
SNAP_RANGE_MM = (2.4, 0.8)   # 法線方向に探す幅（1 回目、2 回目）


@dataclass
class Candidate:
    shape: dict                 # {"outer": Loop, "holes": [Loop]}（mm・y 上）
    area_mm2: float
    touches_edge: bool          # 写真や格子の外にはみ出している疑いがある
    raw: Polygon | None = field(default=None, repr=False)   # 当てはめ前の多角形


def _smooth_closed(P: np.ndarray, sigma: float) -> np.ndarray:
    return np.c_[gaussian_filter1d(P[:, 0], sigma, mode="wrap"), gaussian_filter1d(P[:, 1], sigma, mode="wrap")]


def _best_path(U: np.ndarray, lam: float, max_jump: int = 2) -> np.ndarray:
    """輪に沿った各点で 1 つずつ位置 k を選ぶ。sum(U[i, k_i]) + lam * sum|k_i - k_{i+1}| が最小の道（動的計画法）。"""
    N, K = U.shape
    ov = min(N, 80)                       # 輪なので、前後を重ねて解いて真ん中だけ使う
    Ue = np.vstack([U[-ov:], U, U[:ov]])
    ks = np.arange(K)
    cost = Ue[0].copy()
    back = np.zeros((len(Ue), K), np.int32)
    for i in range(1, len(Ue)):
        best, arg = cost.copy(), ks.copy()
        for d in range(1, max_jump + 1):
            c = cost[:-d] + lam * d       # k-d から
            upd = c < best[d:]
            best[d:][upd] = c[upd]; arg[d:][upd] = ks[:-d][upd]
            c = cost[d:] + lam * d        # k+d から
            upd = c < best[:-d]
            best[:-d][upd] = c[upd]; arg[:-d][upd] = ks[d:][upd]
        back[i] = arg
        cost = best + Ue[i]
    k = int(np.argmin(cost))
    path = np.empty(len(Ue), int)
    for i in range(len(Ue) - 1, -1, -1):
        path[i] = k
        k = back[i, k]
    return path[ov:ov + N]


def _bridge_crossings(P: np.ndarray, lines, axis: int, ppm: float, max_len_mm: float = 15.0) -> np.ndarray:
    """輪郭が格子線の下を通っている区間を、線の手前と向こうの見えている縁から作り直す。

    格子線の下では板の縁が見えない（線を消した画像では、帯の両側を補間した値になっている）ので、
    線に対して浅い角度で横切る縁は、帯の中で最大で帯の半分ほどずれる。そこで区間の前後の向きを調べて、
      - 前後がほぼ同じ向きで、線の反対側へ抜けている → まっすぐな縁が線を横切っている。前後の点を線分で結ぶ
      - 前後の向きが違う → 角が線の下に隠れている。前後の縁を延長した交点を角にする
      - 前後が同じ向きで、線の同じ側へ戻る → 縁が線に沿っているだけ。作り直すと形が変わるので触らない

    lines: [(線の中心, 半分の幅), ...]（画素）。axis: 0 = 縦線（x を見る）、1 = 横線（y を見る）。
    """
    n = len(P)
    if not lines or n < 16:
        return P
    centers = np.array([c for c, _ in lines], float)
    halves = np.array([h for _, h in lines], float) + 1.5      # ぼけたぶんの余裕
    q = P[:, axis]
    k = np.abs(q[:, None] - centers[None, :]).argmin(axis=1)
    inside = np.abs(q - centers[k]) <= halves[k]
    if not inside.any() or inside.all():
        return P
    out = P.copy()
    start = int(np.argmax(~inside))
    idx = (np.arange(n) + start) % n
    ins = inside[idx]
    step_mm = float(np.linalg.norm(P[1] - P[0])) / ppm
    reach = max(3, int(round(2.4 / max(step_mm, 1e-6))))       # 向きを見るのに使う点の数（2.4 mm ぶん）

    def direction(pos: int, sign: int):
        """idx[pos] から sign の向きへ続く、線の外の点に当てはめた直線（通る点, 進行方向の単位ベクトル）。"""
        pts = []
        for t in range(reach + 1):
            m = (pos + sign * t) % n
            if ins[m]:
                break
            pts.append(P[idx[m]])
        if len(pts) < 3:
            return None
        pts = np.array(pts)
        c = pts.mean(0)
        d = np.linalg.svd(pts - c, full_matrices=False)[2][0]
        if d @ (pts[-1] - pts[0]) * sign < 0:                    # 輪郭の進む向きにそろえる
            d = -d
        return c, d

    i = 0
    while i < n:
        if not ins[i]:
            i += 1
            continue
        j = i
        while j < n and ins[j]:
            j += 1
        m = j - i
        ia, ib = i - 1, j % n                                    # 区間の直前と直後（線の外）
        A, B = P[idx[ia]], P[idx[ib]]
        if (m + 1) * step_mm <= max_len_mm:
            la, lb = direction(ia, -1), direction(ib, +1)
            turn = None
            if la is not None and lb is not None:
                turn = float(np.degrees(np.arccos(np.clip(la[1] @ lb[1], -1, 1))))
            c = centers[k[idx[i]]]
            crosses = (q[idx[ia]] - c) * (q[idx[ib]] - c) < 0
            if turn is not None and turn > 15.0:
                # 角: 前後の直線の交点
                (c1, d1), (c2, d2) = la, lb
                det = d1[0] * -d2[1] + d2[0] * d1[1]
                if abs(det) > 1e-6:
                    t1 = ((c2 - c1)[0] * -d2[1] + d2[0] * (c2 - c1)[1]) / det
                    X = c1 + t1 * d1
                    lim = (m + 1) * step_mm * ppm + 3.0 * ppm
                    # 角は線の下に隠れているはずなので、交点が線の帯から外れるなら採らない
                    under = abs(X[axis] - c) <= halves[k[idx[i]]] + 3.0
                    # いまの輪郭から大きく飛び出す交点も採らない（ばりなどで縁の向きが乱れていると、とがった角ができてしまう）
                    run = P[idx[np.arange(i, j) % n]]
                    near = np.linalg.norm(run - X, axis=1).min() <= 1.2 * ppm
                    if under and near and np.linalg.norm(X - A) <= lim and np.linalg.norm(X - B) <= lim:
                        la_len, lb_len = np.linalg.norm(X - A), np.linalg.norm(B - X)
                        for t in range(m):
                            sdist = (la_len + lb_len) * (t + 1) / (m + 1)
                            out[idx[i + t]] = A + (X - A) * (sdist / la_len) if sdist <= la_len and la_len > 1e-9 \
                                else X + (B - X) * ((sdist - la_len) / max(lb_len, 1e-9))
            elif crosses:
                for t in range(m):
                    out[idx[i + t]] = A + (B - A) * ((t + 1) / (m + 1))
        i = j
    return out


def _snap_ring(P: np.ndarray, nolines: np.ndarray, mask: np.ndarray, ppm: float, lines_x=None, lines_y=None) -> np.ndarray:
    """閉じた点列（画素の番号＝中心の座標）を実際のエッジへ寄せる。

    各点で法線方向の勾配を調べ、「エッジが強く、隣の点となめらかにつながる」位置の列を選ぶ。
    斜めから撮ると、カメラ側の縁には板の側面が写ってエッジが 2 本並ぶ（上面／側面、側面／マット）。
    同じくらい強ければ内側を採る: 板を小さめに見積もる側なので安全で、マットの汚れや影も拾いにくい。
    """
    step = SNAP_STEP_MM * ppm
    P = arcfit.resample(P, step)
    if len(P) < 12:
        return P
    P = _smooth_closed(P, 1.2)
    H, W = nolines.shape[:2]
    pad = int(np.ceil(SNAP_RANGE_MM[0] * ppm)) + 6
    x0, y0 = max(0, int(P[:, 0].min()) - pad), max(0, int(P[:, 1].min()) - pad)
    x1, y1 = min(W, int(P[:, 0].max()) + pad + 1), min(H, int(P[:, 1].max()) + pad + 1)
    crop = cv2.GaussianBlur(nolines[y0:y1, x0:x1].astype(np.float32), (0, 0), 1.0)
    gx = cv2.Sobel(crop, cv2.CV_32F, 1, 0, ksize=3) / 8
    gy = cv2.Sobel(crop, cv2.CV_32F, 0, 1, ksize=3) / 8

    for rng_mm in SNAP_RANGE_MM:
        D = rng_mm * ppm
        Q = _smooth_closed(P, 2.5)
        t = np.roll(Q, -1, axis=0) - np.roll(Q, 1, axis=0)
        t /= np.maximum(np.linalg.norm(t, axis=1, keepdims=True), 1e-9)
        n = np.c_[t[:, 1], -t[:, 0]]
        # 法線を外向き（マスクの外）にそろえる
        probe = np.round(P + n * (0.6 * ppm)).astype(int)
        probe[:, 0] = np.clip(probe[:, 0], 0, W - 1)
        probe[:, 1] = np.clip(probe[:, 1], 0, H - 1)
        if (mask[probe[:, 1], probe[:, 0]] > 0).mean() > 0.5:
            n = -n
        offs = np.arange(-D, D + 0.25, 0.5)
        m = np.zeros((len(P), len(offs)), np.float32)
        for a in range(0, len(P), 20000):          # cv2.remap は一辺 32767 画素まで
            b = min(len(P), a + 20000)
            X = (P[a:b, 0:1] + n[a:b, 0:1] * offs[None, :] - x0).astype(np.float32)
            Y = (P[a:b, 1:2] + n[a:b, 1:2] * offs[None, :] - y0).astype(np.float32)
            GX = cv2.remap(gx, X, Y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
            GY = cv2.remap(gy, X, Y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
            dn = GX * n[a:b, 0][:, None, None] + GY * n[a:b, 1][:, None, None]
            m[a:b] = np.sqrt((dn ** 2).sum(-1))
        m0 = 0.5 * float(np.median(m.max(axis=1))) + 1e-6
        rel = offs / D
        U = -(m / (m + m0)) + 0.06 * rel[None, :] + 0.06 * rel[None, :] ** 2
        j = np.clip(_best_path(U.astype(np.float64), lam=0.12), 1, len(offs) - 2)
        r = np.arange(len(P))
        s0, s1, s2 = m[r, j - 1], m[r, j], m[r, j + 1]
        den = s0 - 2 * s1 + s2
        sub = np.where(den < -1e-9, 0.5 * (s0 - s2) / np.where(den < -1e-9, den, 1.0), 0.0)
        off = offs[j] + np.clip(sub, -1, 1) * 0.5
        off = gaussian_filter1d(median_filter(off, size=5, mode="wrap"), 1.0, mode="wrap")
        P = P + n * off[:, None]
    P = _bridge_crossings(P, lines_x, 0, ppm)
    P = _bridge_crossings(P, lines_y, 1, ppm)
    return P


def _ring_polygon(pts_mm: np.ndarray) -> Polygon | None:
    if len(pts_mm) < 4:
        return None
    poly = Polygon(pts_mm)
    if not poly.is_valid:
        poly, _ = go.largest_polygon(poly.buffer(0))
    return poly if poly is not None and poly.area > 0 else None


def thickness_correct(poly: Polygon, nadir_mm, cam_height_mm: float, thickness_mm: float) -> Polygon:
    """板厚による視差の補正。

    板の上面（高さ t）の縁は、カメラ直下点から放射状に H/(H−t) 倍だけ外へずれて写る。写った領域 S は
    「底面の外形 ∪ 上面の外形の投影」なので、S と、S を直下点中心に (H−t)/H 倍に縮めたものの共通部分が
    底面の外形になる（真の外形は必ずこの中に入る）。
    """
    if thickness_mm <= 0 or not cam_height_mm or cam_height_mm <= thickness_mm * 2:
        return poly
    k = (cam_height_mm - thickness_mm) / cam_height_mm
    shrunk = affinity.scale(poly, xfact=k, yfact=k, origin=(float(nadir_mm[0]), float(nadir_mm[1])))
    res, _ = go.largest_polygon(poly.intersection(shrunk))
    return res if res is not None else poly


def extract_outlines(seg: Segmentation, rect: Rectified, *, min_area_mm2: float = 150.0, min_hole_mm2: float = 15.0,
                     nadir_mm=None, cam_height_mm: float | None = None, thickness_mm: float = 0.0,
                     fit: arcfit.FitParams | None = None, snap: bool = True) -> list[Candidate]:
    """マスクの連結成分ごとに輪郭を作り、面積の大きい順に返す。"""
    ppm = rect.px_per_mm
    H, W = seg.mask.shape
    contours, hier = cv2.findContours(seg.mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    if hier is None:
        return []
    hier = hier[0]
    # 写真の外・格子の外に近い画素
    edge = cv2.dilate((rect.valid == 0).astype(np.uint8), np.ones((3, 3), np.uint8), iterations=int(round(1.5 * ppm)))
    m = int(round(2.0 * ppm))
    edge[:m, :] = 1; edge[-m:, :] = 1; edge[:, :m] = 1; edge[:, -m:] = 1

    def ring_mm(cnt):
        P = cnt.reshape(-1, 2).astype(float)
        if snap:
            P = _snap_ring(P, seg.nolines, seg.mask, ppm, seg.lines_x, seg.lines_y)
        return rect.px_to_mm(P + 0.5)      # 画素の番号 → 連続座標 → mm

    out = []
    for i, cnt in enumerate(contours):
        if hier[i][3] != -1:      # 穴は親のところで扱う
            continue
        if cv2.contourArea(cnt) / ppm ** 2 < min_area_mm2:
            continue
        outer = _ring_polygon(ring_mm(cnt))
        if outer is None:
            continue
        holes = []
        j = hier[i][2]
        while j != -1:
            if cv2.contourArea(contours[j]) / ppm ** 2 >= min_hole_mm2:
                hp = _ring_polygon(ring_mm(contours[j]))
                if hp is not None:
                    holes.append(hp)
            j = hier[j][0]
        poly = outer
        for hp in holes:
            poly = poly.difference(hp)
        poly, _ = go.largest_polygon(poly)
        if poly is None:
            continue
        if nadir_mm is not None:
            poly = thickness_correct(poly, nadir_mm, cam_height_mm, thickness_mm)
        pts = cnt.reshape(-1, 2)
        touches = bool(edge[pts[:, 1], pts[:, 0]].any())
        shape = go.polygon_to_shape_fit(poly, fit)
        out.append(Candidate(shape=shape, area_mm2=float(poly.area), touches_edge=touches, raw=poly))
    out.sort(key=lambda c: c.area_mm2, reverse=True)
    return out
