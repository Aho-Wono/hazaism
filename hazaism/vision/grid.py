"""格子マットの検出: 写真 px → 格子座標（マス単位）のホモグラフィを求める。

1. 細く暗い線を取り出して線分を検出し、2 つの消失点にまとめる（粗検出・縮小画像）
2. 消失点でアフィン補正 → 等間隔性から各線に整数番号を振る → 交点からホモグラフィの初期値
3. 予測した線の近くで実際の線の位置をサブピクセルで測り直し、全点でホモグラフィを最小二乗
   （ここで「線が見えたかどうか」も記録する。板の切り出しで使う）

格子座標 (u, v) は、u が写真の右、v が写真の下へ増える向きにそろえる。つまり真上から見た図で、鏡像にはならない。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np
from scipy.ndimage import map_coordinates
from scipy.optimize import least_squares

WORK_LONG = 1600        # 粗検出の作業解像度（長辺 px）
FINE_LONG = 3200        # 測り直しに使う解像度の上限（長辺 px）
VP_ANGLE_TOL = math.radians(1.5)
SAMPLE_STEP_CELLS = 0.125   # 線に沿って位置を測る間隔（マス単位）
SEARCH_HALF_CELLS = 0.30    # 線の左右を探す幅（マス単位）


class GridNotFound(Exception):
    pass


@dataclass
class LineSamples:
    """格子線に沿った測定点。板で隠れた場所は visible=False になる。"""
    family: np.ndarray    # 0: u = 一定の線（写真で縦方向）、1: v = 一定の線
    index: np.ndarray     # 線の番号（整数）
    t: np.ndarray         # 線に沿った位置（マス単位、family 0 なら v、1 なら u）
    visible: np.ndarray   # 線が見えたか
    contrast: np.ndarray  # 線の暗さ（周囲との差、0〜255）


@dataclass
class Grid:
    H: np.ndarray                     # 写真 px → 格子座標（マス単位）
    image_size: tuple[int, int]       # (w, h)
    pitch_mm: float
    u_range: tuple[int, int]          # 線を観測できた番号の範囲（両端を含む）
    v_range: tuple[int, int]
    rms_mm: float
    n_points: int
    samples: LineSamples | None = None
    manual: bool = False
    corr: np.ndarray | None = None    # 2x10: (u, v) の補正多項式の係数（正規化した写真座標の 3 次式）
    extra: dict = field(default_factory=dict)

    def to_grid(self, pts) -> np.ndarray:
        """写真 px → 格子座標。H に、レンズ歪みなどを吸収する 3 次多項式の補正を足したもの。"""
        pts = np.asarray(pts, float).reshape(-1, 2)
        g = apply_h(self.H, pts)
        if self.corr is not None:
            g = g - _poly_terms(self._norm(pts)) @ self.corr.T
        return g

    def to_image(self, pts) -> np.ndarray:
        g = np.asarray(pts, float).reshape(-1, 2)
        Hinv = np.linalg.inv(self.H)
        x = apply_h(Hinv, g)
        if self.corr is not None:
            for _ in range(3):   # 補正は小さいので数回の反復で収束する
                x = apply_h(Hinv, g + _poly_terms(self._norm(x)) @ self.corr.T)
        return x

    def _norm(self, pts) -> np.ndarray:
        w, h = self.image_size
        return (pts - np.array([w / 2, h / 2])) / (max(w, h) / 2)

    def mm_per_px(self) -> float:
        """写真中央付近での 1 px あたりの mm。"""
        w, h = self.image_size
        c = np.array([[w / 2, h / 2], [w / 2 + 1, h / 2], [w / 2, h / 2 + 1]], float)
        g = self.to_grid(c)
        return float(self.pitch_mm * (np.linalg.norm(g[1] - g[0]) + np.linalg.norm(g[2] - g[0])) / 2)


def _poly_terms(n: np.ndarray) -> np.ndarray:
    x, y = n[:, 0], n[:, 1]
    return np.stack([np.ones_like(x), x, y, x * x, x * y, y * y, x ** 3, x * x * y, x * y * y, y ** 3], 1)


def apply_h(H, pts) -> np.ndarray:
    pts = np.asarray(pts, float).reshape(-1, 2)
    q = np.c_[pts, np.ones(len(pts))] @ np.asarray(H, float).T
    return q[:, :2] / q[:, 2:3]


# ---- 1. 線分と消失点 --------------------------------------------------------------

def _line_mask(gray: np.ndarray) -> np.ndarray:
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
    bh = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, k)
    thr, _ = cv2.threshold(bh, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    return (bh > max(10.0, thr * 0.8)).astype(np.uint8) * 255


def _segments(gray: np.ndarray) -> np.ndarray:
    mask = _line_mask(gray)
    long = max(gray.shape)
    segs = cv2.HoughLinesP(mask, 1, math.pi / 720, threshold=60,
                           minLineLength=long * 0.06, maxLineGap=long * 0.006)
    if segs is None or len(segs) < 20:
        raise GridNotFound("格子の線が見つかりません")
    return segs.reshape(-1, 4).astype(float)


def _vanishing_point(segs, weights, rng, iters=1500):
    """線分の束がいちばんよく通る点（同次座標）と、その束に属する線分のマスク。"""
    p1 = np.c_[segs[:, :2], np.ones(len(segs))]
    p2 = np.c_[segs[:, 2:], np.ones(len(segs))]
    lines = np.cross(p1, p2)
    mid = (segs[:, :2] + segs[:, 2:]) / 2
    d = segs[:, 2:] - segs[:, :2]
    d /= np.linalg.norm(d, axis=1, keepdims=True)

    def inliers(v):
        # 線分の中点から消失点へ向かう方向と、線分の方向のなす角
        to = v[:2][None, :] - mid * v[2]
        n = np.linalg.norm(to, axis=1)
        s = np.abs(to[:, 0] * d[:, 1] - to[:, 1] * d[:, 0]) / np.maximum(n, 1e-12)
        return s < math.sin(VP_ANGLE_TOL)

    best, best_score = None, -1.0
    prob = weights / weights.sum()
    for _ in range(iters):
        i, j = rng.choice(len(segs), 2, replace=False, p=prob)
        v = np.cross(lines[i], lines[j])
        nv = np.linalg.norm(v)
        if nv < 1e-9:
            continue
        v = v / nv
        m = inliers(v)
        score = weights[m].sum()
        if score > best_score:
            best, best_score = m, score
    if best is None:
        raise GridNotFound("格子の向きが決まりません")
    for _ in range(3):  # 束に属する線分で最小二乗
        L = lines[best] / np.linalg.norm(lines[best][:, :2], axis=1, keepdims=True)
        L = L * np.sqrt(weights[best])[:, None]
        v = np.linalg.svd(L, full_matrices=False)[2][-1]
        best = inliers(v)
    return v, best


# ---- 2. 番号付けと初期ホモグラフィ ------------------------------------------------

def _pitch_and_phase(c, w):
    """ほぼ等間隔に並ぶ値 c（重み w）の間隔と位相。"""
    order = np.argsort(c)
    c, w = c[order], w[order]
    # 近い値をまとめて 1 本の線にする
    span = c[-1] - c[0]
    gap = np.diff(c)
    # まず細かいヒストグラムの自己相関で間隔の見当をつける
    nb = 4096
    hist, edges = np.histogram(c, bins=nb, weights=w)
    binw = edges[1] - edges[0]
    f = np.fft.rfft(hist - hist.mean(), n=2 * nb)
    ac = np.fft.irfft(f * np.conj(f))[:nb]
    ac[: max(3, int(0.004 * nb))] = 0  # 0 付近（線の太さ）を除く
    lo = int(0.008 * nb)
    # 自己相関の山のうち、十分に高くて、いちばん短い周期のものを採る。
    # （太線が 5 本ごとにあるので 5 倍の周期の山がいちばん高くなることがある。最大の山を採ると間隔を取り違える）
    seg = ac[lo: nb // 2]
    top = float(seg.max())
    is_peak = (seg[1:-1] >= seg[:-2]) & (seg[1:-1] >= seg[2:]) & (seg[1:-1] > 0.35 * top)
    cand = np.where(is_peak)[0]
    k = float(lo + 1 + int(cand[0]) if len(cand) else lo + int(np.argmax(seg)))
    # 2 倍、3 倍…の位置の山を順にたどって、周期を精密にする（最初の山だけだと数 % ずれて、端の線の番号が狂う）
    m = 2
    while m * k < nb // 2:
        a, b = int(m * k - 0.3 * k), int(m * k + 0.3 * k) + 1
        j = a + int(np.argmax(ac[a:b]))
        if ac[j] > 0.2 * top:
            k = j / m
        m += 1
    s = k * binw
    for _ in range(3):
        ang = 2 * math.pi * c / s
        a = math.atan2((w * np.sin(ang)).sum(), (w * np.cos(ang)).sum()) * s / (2 * math.pi)
        idx = np.round((c - a) / s)
        ok = np.abs(c - (a + idx * s)) < 0.2 * s
        if ok.sum() < 4:
            break
        A = np.c_[idx[ok], np.ones(ok.sum())] * np.sqrt(w[ok])[:, None]
        (s, a), *_ = np.linalg.lstsq(A, c[ok] * np.sqrt(w[ok]), rcond=None)
    del gap, span
    return float(s), float(a)


def _fit_family(segs_a, weights, direction):
    """アフィン補正後の線分（ほぼ平行）に番号を振る。戻り値: 番号（外れは -inf にせず mask で返す）。"""
    n = np.array([-direction[1], direction[0]])
    mid = (segs_a[:, :2] + segs_a[:, 2:]) / 2
    c = mid @ n
    s, a = _pitch_and_phase(c, weights)
    idx = np.round((c - a) / s).astype(int)
    ok = np.abs(c - (a + idx * s)) < 0.2 * s
    return idx, ok, s


def _initial_homography(gray: np.ndarray, rng) -> np.ndarray:
    h, w = gray.shape
    segs = _segments(gray)
    length = np.linalg.norm(segs[:, 2:] - segs[:, :2], axis=1)

    v1, m1 = _vanishing_point(segs, length, rng)
    rest = ~m1
    if rest.sum() < 10:
        raise GridNotFound("格子の線が 1 方向しか見つかりません")
    v2, m2r = _vanishing_point(segs[rest], length[rest], rng)
    m2 = np.zeros(len(segs), bool)
    m2[np.where(rest)[0][m2r]] = True

    # 画像中心を原点にしてからアフィン補正（消失線を無限遠へ）
    T = np.array([[1, 0, -w / 2], [0, 1, -h / 2], [0, 0, 1.0]])
    Tinv_T = np.linalg.inv(T).T
    linf = np.cross(v1, v2)
    linf = Tinv_T @ linf
    if abs(linf[2]) < 1e-12:
        raise GridNotFound("視点の推定に失敗しました")
    linf = linf / linf[2]
    Ha = np.array([[1, 0, 0], [0, 1, 0], [linf[0], linf[1], 1.0]]) @ T

    fams = []
    for m in (m1, m2):
        s = segs[m]
        a = np.c_[apply_h(Ha, s[:, :2]), apply_h(Ha, s[:, 2:])]
        d = a[:, 2:] - a[:, :2]
        d[d @ d[np.argmax(length[m])] < 0] *= -1
        direction = (d * length[m][:, None]).sum(0)
        direction /= np.linalg.norm(direction)
        idx, ok, pitch = _fit_family(a, length[m], direction)
        fams.append((s[ok], idx[ok], length[m][ok], direction, pitch))

    # 写真で縦に近い方を family 0（u = 一定）にする
    if abs(fams[0][3][0]) > abs(fams[1][3][0]):
        fams.reverse()

    # 番号ごとに写真上の直線を当てはめる
    lines = []
    for s, idx, wt, _, _ in fams:
        fam_lines = {}
        for k in np.unique(idx):
            sel = idx == k
            pts = np.r_[s[sel, :2], s[sel, 2:]]
            ww = np.r_[wt[sel], wt[sel]]
            if ww.sum() < 0.08 * max(h, w):
                continue
            m = (pts * ww[:, None]).sum(0) / ww.sum()
            _, _, vt = np.linalg.svd((pts - m) * np.sqrt(ww)[:, None], full_matrices=False)
            nrm = vt[-1]
            fam_lines[int(k)] = np.array([nrm[0], nrm[1], -nrm @ m])
        if len(fam_lines) < 4:
            raise GridNotFound("格子の線が足りません")
        lines.append(fam_lines)

    src, dst = [], []
    for ku, lu in lines[0].items():
        for kv, lv in lines[1].items():
            p = np.cross(lu, lv)
            if abs(p[2]) < 1e-9:
                continue
            p = p[:2] / p[2]
            if -0.2 * w < p[0] < 1.2 * w and -0.2 * h < p[1] < 1.2 * h:
                src.append(p); dst.append((ku, kv))
    src, dst = np.array(src), np.array(dst, float)
    H, inl = cv2.findHomography(src, dst, cv2.RANSAC, 0.15)
    if H is None or inl.sum() < 12:
        raise GridNotFound("格子の当てはめに失敗しました")
    return _orient(H, w, h)


def _orient(H, w, h):
    """u が写真の右、v が写真の下へ増えるように、番号の向きをそろえる（鏡像にしない）。"""
    c = np.array([[w / 2, h / 2], [w / 2 + 10, h / 2], [w / 2, h / 2 + 10]], float)
    g = apply_h(H, c)
    J = np.c_[g[1] - g[0], g[2] - g[0]]   # 列: 写真の右／下へ動いたときの (u, v) の変化
    # 写真の右に対応する格子の軸を u にする
    if abs(J[0, 0]) < abs(J[1, 0]):
        H = np.array([[0, 1, 0], [1, 0, 0], [0, 0, 1.0]]) @ H
        J = J[::-1]
    su = 1.0 if J[0, 0] > 0 else -1.0
    # v の向きは「鏡像にならない」方に決める（格子が 45° 近く回っていても必ず正しい向きになる）
    sv = 1.0 if su * np.linalg.det(J) > 0 else -1.0
    H = np.diag([su, sv, 1.0]) @ H
    return H / H[2, 2]


def is_mirrored(H, w, h) -> bool:
    c = np.array([[w / 2, h / 2], [w / 2 + 10, h / 2], [w / 2, h / 2 + 10]], float)
    g = apply_h(H, c)
    J = np.c_[g[1] - g[0], g[2] - g[0]]
    return float(np.linalg.det(J)) < 0


# ---- 3. 測り直し -------------------------------------------------------------------

def _measure_at(gray: np.ndarray, H: np.ndarray, fam, idx, tt, search_half: float = SEARCH_HALF_CELLS):
    """格子座標で指定した点（family, 線の番号 idx, 線に沿った位置 tt）の近くで、線の実際の位置を測る。

    線に垂直な向きに ±search_half マスの範囲を調べる。
    戻り値: inside（写真の中にあって測れた点のマスク）, 写真上の位置 (x, y), visible, contrast（inside の点だけ）
    """
    h, w = gray.shape
    Hinv = np.linalg.inv(H)
    fam = np.asarray(fam)
    g = np.where((fam == 0)[:, None], np.c_[idx, tt], np.c_[tt, idx]).astype(float)
    off = np.zeros_like(g)
    off[fam == 0, 0] = search_half
    off[fam == 1, 1] = search_half
    p = apply_h(Hinv, g)
    pa = apply_h(Hinv, g - off)
    pb = apply_h(Hinv, g + off)
    inside = (p[:, 0] > 2) & (p[:, 0] < w - 3) & (p[:, 1] > 2) & (p[:, 1] < h - 3)
    for q in (pa, pb):
        inside &= (q[:, 0] > 1) & (q[:, 0] < w - 2) & (q[:, 1] > 1) & (q[:, 1] < h - 2)
    if not inside.any():
        return inside, np.zeros((0, 2)), np.zeros(0, bool), np.zeros(0, np.float32)
    base, nrm = pa[inside], (pb - pa)[inside]

    span = np.linalg.norm(nrm, axis=1)                    # 探す幅 [px]
    n_s = int(np.clip(np.ceil(np.percentile(span, 95) * 2), 9, 121))
    s = np.linspace(0, 1, n_s)
    xs = base[:, 0:1] + nrm[:, 0:1] * s[None, :]
    ys = base[:, 1:2] + nrm[:, 1:2] * s[None, :]
    prof = map_coordinates(gray, [ys, xs], order=1, mode="nearest").astype(np.float32)

    bg = np.percentile(prof, 70, axis=1, keepdims=True)
    dark = np.clip(bg - prof, 0, None)
    j = np.argmax(dark, axis=1)
    peak = dark[np.arange(len(dark)), j]
    # 山のまわりの重心でサブピクセル化
    step_px = span / (n_s - 1)
    half = np.clip(np.round(3.0 / np.maximum(step_px, 1e-6)).astype(int), 1, n_s // 4)
    cols = np.arange(n_s)[None, :]
    win = (np.abs(cols - j[:, None]) <= half[:, None]) & (dark > 0.4 * peak[:, None])
    wsum = (dark * win).sum(1)
    pos = np.where(wsum > 0, (dark * win * s[None, :]).sum(1) / np.maximum(wsum, 1e-9), 0.5)
    # 線らしさ: 山が細いこと（板の縁や影のような「段差」は幅が広い）
    width_px = (dark > 0.5 * peak[:, None]).sum(1) * step_px
    cell_px = span / (2 * search_half)
    visible = (peak > 14) & (width_px < np.maximum(0.3 * cell_px, 4.0)) & (j > 0) & (j < n_s - 1)
    xy = base + nrm * pos[:, None]
    return inside, xy, visible, peak


def _grid_extent(H: np.ndarray, shape, margin_cells: int = 2):
    h, w = shape
    corners = apply_h(H, [[0, 0], [w, 0], [w, h], [0, h]])
    u0, v0 = np.floor(corners.min(0)).astype(int) - margin_cells
    u1, v1 = np.ceil(corners.max(0)).astype(int) + margin_cells
    # 斜めすぎる写真で範囲が発散しないように
    return max(u0, -400), min(u1, 400), max(v0, -400), min(v1, 400)


def _measure_lines(gray: np.ndarray, H: np.ndarray, margin_cells: int = 2):
    """いまの H で予測した線（整数番号の線すべて）の近くを調べ、線の実際の位置を測る。

    戻り値: family, index, t, 写真上の位置 (x, y), visible, contrast
    """
    u0, u1, v0, v1 = _grid_extent(H, gray.shape, margin_cells)
    fam, idx, tt = [], [], []
    for f, (k0, k1, t0, t1) in enumerate(((u0, u1, v0, v1), (v0, v1, u0, u1))):
        ks = np.arange(k0, k1 + 1)
        ts = np.arange(t0, t1, SAMPLE_STEP_CELLS) + SAMPLE_STEP_CELLS / 2
        K, Tt = np.meshgrid(ks, ts, indexing="ij")
        fam.append(np.full(K.size, f)); idx.append(K.reshape(-1)); tt.append(Tt.reshape(-1))
    fam, idx, tt = np.concatenate(fam), np.concatenate(idx), np.concatenate(tt)
    inside, xy, visible, peak = _measure_at(gray, H, fam, idx, tt)
    return fam[inside], idx[inside], tt[inside], xy, visible, peak


def _fix_pitch(gray: np.ndarray, H: np.ndarray, samples: LineSamples) -> np.ndarray:
    """線の間隔を取り違えていないか確かめて、取り違えていれば直した H を返す（正しければ同じ H）。

    - 番号の付いた線の間（1/2, 1/3, 1/5 の位置）にも線がある → 間隔を大きく取りすぎている（太線だけ拾った等）
    - 番号の付いた線のうち n 本に 1 本しか見えない           → 間隔を小さく取りすぎている
    """
    H = H.copy()
    for f in (0, 1):
        sel = samples.family == f
        ks = np.unique(samples.index[sel])
        frac = {int(k): float(samples.visible[sel & (samples.index == k)].mean()) for k in ks
                if (sel & (samples.index == k)).sum() >= 16}
        if len(frac) < 6:
            continue
        # 小さく取りすぎ: n 本ごとにしか線がない
        done = False
        for n in (5, 3, 2):
            means = [np.mean([v for k, v in frac.items() if k % n == r] or [0.0]) for r in range(n)]
            r = int(np.argmax(means))
            others = [m for i, m in enumerate(means) if i != r]
            if means[r] > 0.3 and max(others) < 0.15 * means[r]:
                D = np.eye(3)
                D[f, f], D[f, 2] = 1.0 / n, -r / n
                H = D @ H
                done = True
                break
        if done:
            continue
        # 大きく取りすぎ: よく見えている線の間を、狭い範囲（±0.08 マス）で調べる
        good = sorted(k for k, v in frac.items() if v > 0.5)
        if len(good) < 4:
            continue
        lines = np.array(good[:: max(1, len(good) // 24)], float)
        t_lo, t_hi = samples.t[sel].min(), samples.t[sel].max()
        ts = np.arange(t_lo, t_hi, 0.5)

        def presence(offset: float) -> float:
            K, Tt = np.meshgrid(lines + offset, ts, indexing="ij")
            inside, _, vis, _ = _measure_at(gray, H, np.full(K.size, f), K.reshape(-1), Tt.reshape(-1), search_half=0.08)
            return float(vis.mean()) if inside.sum() >= 50 else 0.0

        base = presence(0.0)
        if base < 0.2:
            continue
        for n in (5, 3, 2):
            if min(presence(j / n) for j in range(1, n)) > 0.5 * base:
                D = np.eye(3)
                D[f, f] = float(n)
                H = D @ H
                break
    return H


def _interior(fam, idx, tt, vis):
    """両隣 2 点ずつも見えている測定点。板の縁に近い点は位置が怪しいので、当てはめにはこれだけを使う。"""
    n = len(vis)
    ok = vis.copy()
    for d in (1, 2):
        same = (fam[d:] == fam[:-d]) & (idx[d:] == idx[:-d]) & \
               (np.abs(tt[d:] - tt[:-d] - d * SAMPLE_STEP_CELLS) < 1e-6)
        both = same & vis[d:] & vis[:-d]
        fwd = np.zeros(n, bool); fwd[:-d] = both
        bwd = np.zeros(n, bool); bwd[d:] = both
        ok &= fwd & bwd
    return ok


def _refine(gray: np.ndarray, H0: np.ndarray, iters: int = 3):
    """線の位置を測り直して H を最小二乗し、残った系統誤差を 3 次多項式で吸収する。

    戻り値: H, corr(2x10), LineSamples, RMS（マス単位）, 当てはめに使った点数
    """
    gh, gw = gray.shape
    cx, cy, sc = gw / 2, gh / 2, max(gw, gh) / 2
    N = np.array([[1 / sc, 0, -cx / sc], [0, 1 / sc, -cy / sc], [0, 0, 1]])
    H = H0 / H0[2, 2]
    for _ in range(iters):
        fam, idx, tt, xy, vis, peak = _measure_lines(gray, H)
        use = _interior(fam, idx, tt, vis)
        if use.sum() < 200:
            raise GridNotFound("格子線を十分に測れません")
        f, k = fam[use], idx[use].astype(float)
        xn, yn = (xy[use, 0] - cx) / sc, (xy[use, 1] - cy) / sc   # 条件数をよくするため正規化
        Hn0 = H @ np.linalg.inv(N)
        Hn0 = Hn0 / Hn0[2, 2]

        def resid(p):
            Hn = np.append(p, 1.0).reshape(3, 3)
            den = Hn[2, 0] * xn + Hn[2, 1] * yn + 1.0
            u = (Hn[0, 0] * xn + Hn[0, 1] * yn + Hn[0, 2]) / den
            v = (Hn[1, 0] * xn + Hn[1, 1] * yn + Hn[1, 2]) / den
            return np.where(f == 0, u, v) - k

        sol = least_squares(resid, Hn0.reshape(-1)[:8], loss="soft_l1", f_scale=0.03, x_scale="jac")
        H = np.append(sol.x, 1.0).reshape(3, 3) @ N
        H = H / H[2, 2]

    # 最終の H で測り直し、残差のなめらかな成分（レンズ歪み・マットの反りなど）を多項式にする
    fam, idx, tt, xy, vis, peak = _measure_lines(gray, H)
    use = _interior(fam, idx, tt, vis)
    g = apply_h(H, xy)
    r = np.where(fam == 0, g[:, 0], g[:, 1]) - idx
    T = _poly_terms((xy - np.array([cx, cy])) / sc)
    corr = np.zeros((2, T.shape[1]))
    for fi in (0, 1):
        m = use & (fam == fi) & (np.abs(r) < 0.08)
        if m.sum() < 100:
            continue
        c = np.linalg.lstsq(T[m], r[m], rcond=None)[0]
        m2 = m & (np.abs(r - T @ c) < 0.04)
        if m2.sum() >= 100:
            c = np.linalg.lstsq(T[m2], r[m2], rcond=None)[0]
        corr[fi] = c
    dev = r - np.where(fam == 0, T @ corr[0], T @ corr[1])
    vis = vis & (np.abs(dev) < 0.08)
    inl = use & (np.abs(dev) < 0.08)
    rms_cells = float(np.sqrt(np.mean(dev[inl] ** 2))) if inl.sum() else float("nan")
    return H, corr, LineSamples(fam, idx, tt, vis, peak), rms_cells, int(inl.sum())


def _observed_range(samples: LineSamples, family: int) -> tuple[int, int]:
    """線の大半が見えている番号の範囲（マットの格子がある範囲）。"""
    sel = samples.family == family
    ks = np.unique(samples.index[sel])
    good = []
    for k in ks:
        m = sel & (samples.index == k)
        n_vis = int(samples.visible[m].sum())
        if n_vis >= 12 and n_vis >= 0.2 * m.sum():
            good.append(int(k))
    if len(good) < 4:
        raise GridNotFound("格子の範囲が決まりません")
    return min(good), max(good)


def _bold_offset(samples: LineSamples, family: int, every: int = 5) -> int:
    """太線（every 本ごと）が番号の倍数に来るようにするためのずらし量。太線が判別できなければ 0。"""
    sel = (samples.family == family) & samples.visible
    if sel.sum() < 100:
        return 0
    means = []
    for r in range(every):
        m = sel & (np.mod(samples.index, every) == r)
        means.append(float(samples.contrast[m].mean()) if m.sum() > 20 else 0.0)
    best = int(np.argmax(means))
    others = [v for i, v in enumerate(means) if i != best]
    return best if means[best] > 1.12 * max(others) else 0


def _finish(H, corr, samples, rms_cells, n, image_size, pitch_mm, manual) -> Grid:
    w, h = image_size
    if is_mirrored(H, w, h):
        raise GridNotFound("格子の向きが不正です（鏡像）")
    du, dv = _bold_offset(samples, 0), _bold_offset(samples, 1)
    if du or dv:
        H = np.array([[1, 0, -du], [0, 1, -dv], [0, 0, 1.0]]) @ H
        f0 = samples.family == 0
        samples.index = samples.index - np.where(f0, du, dv)
        samples.t = samples.t - np.where(f0, dv, du)
    return Grid(H=H, image_size=(w, h), pitch_mm=pitch_mm,
                u_range=_observed_range(samples, 0), v_range=_observed_range(samples, 1),
                rms_mm=rms_cells * pitch_mm, n_points=n, samples=samples, manual=manual, corr=corr)


# ---- 公開関数 ----------------------------------------------------------------------

def _scale_mat(s: float) -> np.ndarray:
    """元の写真の画素座標 → s 倍に縮小した画像の画素座標（どちらも「画素の番号＝中心」）。"""
    o = 0.5 * s - 0.5
    return np.array([[s, 0, o], [0, s, o], [0, 0, 1.0]])


def _fine_gray(img_bgr: np.ndarray):
    h, w = img_bgr.shape[:2]
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    fs = min(1.0, FINE_LONG / max(h, w))
    if fs < 1:
        gray = cv2.resize(gray, None, fx=fs, fy=fs, interpolation=cv2.INTER_AREA)
    return cv2.GaussianBlur(gray, (0, 0), 0.8), fs


def detect_grid(img_bgr: np.ndarray, pitch_mm: float = 10.0, seed: int = 0) -> Grid:
    h, w = img_bgr.shape[:2]
    gray_full = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    rng = np.random.default_rng(seed)

    sc = min(1.0, WORK_LONG / max(h, w))
    small = cv2.resize(gray_full, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA) if sc < 1 else gray_full
    H0 = _initial_homography(small, rng) @ _scale_mat(sc)

    fine, fs = _fine_gray(img_bgr)
    Hf, corr, samples, rms_cells, n = _refine(fine, H0 @ np.linalg.inv(_scale_mat(fs)))
    for _ in range(3):          # 線の間隔の取り違えを直す（直したら測り直す）
        H2 = _fix_pitch(fine, Hf, samples)
        if np.allclose(H2, Hf):
            break
        Hf, corr, samples, rms_cells, n = _refine(fine, H2)
    return _finish(Hf @ _scale_mat(fs), corr, samples, rms_cells, n, (w, h), pitch_mm, manual=False)


def grid_from_points(img_pts, cells_u: float, cells_v: float, image_size, pitch_mm: float = 10.0) -> Grid:
    """手動校正: 格子の交点 4 つ（左上→右上→右下→左下の順）と、その間のマス数から作る。"""
    src = np.asarray(img_pts, float).reshape(4, 2)
    dst = np.array([[0, 0], [cells_u, 0], [cells_u, cells_v], [0, cells_v]], float)
    H = cv2.getPerspectiveTransform(src.astype(np.float32), dst.astype(np.float32)).astype(float)
    w, h = image_size
    if is_mirrored(H, w, h):
        raise GridNotFound("点の順番が逆です（左上→右上→右下→左下の順にクリックしてください）")
    H = _orient(H, w, h)
    corners = apply_h(H, [[0, 0], [w, 0], [w, h], [0, h]])
    lo = np.floor(np.maximum(corners.min(0), -400)).astype(int)
    hi = np.ceil(np.minimum(corners.max(0), 400)).astype(int)
    return Grid(H=H, image_size=(w, h), pitch_mm=pitch_mm, u_range=(int(lo[0]), int(hi[0])),
                v_range=(int(lo[1]), int(hi[1])), rms_mm=float("nan"), n_points=4, manual=True)


def refine_manual(img_bgr: np.ndarray, grid: Grid) -> Grid:
    """手動校正の結果を初期値にして、格子線で測り直す（失敗したら元のまま返す）。"""
    h, w = img_bgr.shape[:2]
    fine, fs = _fine_gray(img_bgr)
    try:
        Hf, corr, samples, rms_cells, n = _refine(fine, grid.H @ np.linalg.inv(_scale_mat(fs)))
        return _finish(Hf @ _scale_mat(fs), corr, samples, rms_cells, n, (w, h), grid.pitch_mm, manual=True)
    except GridNotFound:
        return grid


# ---- カメラの位置 ------------------------------------------------------------------

def camera_pose(grid: Grid, f_px: float | None = None) -> dict:
    """ホモグラフィからカメラの位置（マット上の mm 座標と高さ）と傾きを求める。

    f_px（焦点距離 [px]）が無ければホモグラフィから推定する。真上に近くて推定できないときは
    一般的なスマホの広角（35 mm 換算 26 mm）を仮定する。
    """
    w, h = grid.image_size
    P = grid.pitch_mm
    # 格子 mm → 写真 px
    G = np.linalg.inv(grid.H) @ np.diag([1 / P, 1 / P, 1.0])
    C = np.array([[1, 0, -w / 2], [0, 1, -h / 2], [0, 0, 1.0]])
    G = C @ G
    h1, h2 = G[:, 0], G[:, 1]
    f_est = None
    # 直交条件と等長条件から f^2 の候補を 2 つ得る
    cands = []
    d = h1[2] * h2[2]
    if abs(d) > 1e-12:
        cands.append(-(h1[0] * h2[0] + h1[1] * h2[1]) / d)
    d = h1[2] ** 2 - h2[2] ** 2
    if abs(d) > 1e-12:
        cands.append(-((h1[0] ** 2 + h1[1] ** 2) - (h2[0] ** 2 + h2[1] ** 2)) / d)
    cands = [math.sqrt(c) for c in cands if c > 0]
    diag = math.hypot(w, h)
    cands = [c for c in cands if 0.4 * diag < c < 3.0 * diag]
    if cands:
        f_est = float(np.median(cands))
    source = "exif" if f_px else ("estimated" if f_est else "assumed")
    f = f_px or f_est or (26.0 / 43.27 * diag)

    Kinv = np.diag([1 / f, 1 / f, 1.0])
    A = Kinv @ G
    lam = 2.0 / (np.linalg.norm(A[:, 0]) + np.linalg.norm(A[:, 1]))
    r1, r2, t = A[:, 0] * lam, A[:, 1] * lam, A[:, 2] * lam
    if t[2] < 0:
        r1, r2, t = -r1, -r2, -t
    R = np.c_[r1, r2, np.cross(r1, r2)]
    U, _, Vt = np.linalg.svd(R)
    R = U @ Vt
    center = -R.T @ t                      # マット座標（mm）。z の符号は軸の取り方による
    height = abs(float(center[2]))
    tilt = math.degrees(math.acos(min(1.0, abs(float(R[2, 2])))))
    return {"x_mm": float(center[0]), "y_mm": float(center[1]), "height_mm": height,
            "tilt_deg": tilt, "f_px": float(f), "f_source": source}
