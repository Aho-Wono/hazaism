"""閉じた点列を直線と円弧だけに分解する。

ロゴのベクター化用に作った直線＋円弧の当てはめの考え方を、写真から測った雑音のある輪郭向けに作り直したもの。

- 「要素の数が最小になる分け方」を動的計画法で求める（各要素は RMS 誤差が許容差以内の直線か円弧）
- 最大のずれが大きい要素は、許容差を厳しくして分け直す（小さな切り欠きを見落とさないため）
- 極小の要素は捨てて、両隣の交点を角にする

結果は DXF の LWPOLYLINE と同じ「頂点 (x, y, bulge) の列」。bulge はその頂点から次の頂点までの円弧（0 は直線）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass
class FitParams:
    tol: float = 0.2        # 直線・円弧の当てはめ許容差（RMS）[mm]
    tol_max: float = 0.6    # 1 点でもこれ以上ずれる要素は分け直す [mm]
    max_r: float = 3000.0   # これより大きい半径の円弧は使わない（直線にする）[mm]
    min_r: float = 1.5      # これより小さい半径の円弧は使わない（ばりなどの小さな凹凸を円弧にしない）[mm]
    min_len: float = 2.0    # これより短い要素は捨てて隣同士の交点で角にする [mm]
    snap: float = 1.0       # 交点を探す範囲の余裕 [mm]


# ---- 当てはめ -------------------------------------------------------------------

def fit_line(Q):
    m = Q.mean(0)
    d = np.linalg.svd(Q - m, full_matrices=False)[2][0]
    return (m, d), np.abs((Q - m) @ np.array([-d[1], d[0]]))


def fit_circle(Q):
    m = Q.mean(0); X = Q - m
    A = np.c_[2 * X, np.ones(len(X))]
    (cx, cy, k), *_ = np.linalg.lstsq(A, (X ** 2).sum(1), rcond=None)
    c = np.array([cx, cy]) + m
    r = math.sqrt(max(k + cx * cx + cy * cy, 0.0))
    return (c, r), np.abs(np.linalg.norm(Q - c, axis=1) - r)


def _fit_errors(P: np.ndarray, j: int, lo: int, max_r: float, min_r: float):
    """区間 [i..j]（lo <= i < j）を直線／円弧に当てはめたときの RMS 誤差を、すべての i についてまとめて求める。

    戻り値は i = j-1, j-2, ..., lo の順の配列（直線の誤差, 円弧の誤差）。円弧にできない区間は inf。
    """
    X = P[j::-1][: j - lo + 1] - P[j]          # P[j], P[j-1], ..., P[lo] を P[j] 基準で
    x, y = X[:, 0], X[:, 1]
    z = x * x + y * y
    n = np.arange(1, len(x) + 1, dtype=float)
    Sx, Sy, Sz = np.cumsum(x), np.cumsum(y), np.cumsum(z)
    Sxx, Sxy, Syy = np.cumsum(x * x), np.cumsum(x * y), np.cumsum(y * y)
    Sxz, Syz, Szz = np.cumsum(x * z), np.cumsum(y * z), np.cumsum(z * z)

    # 直線: 共分散行列の小さい方の固有値 = 垂直距離の二乗平均
    mx, my = Sx / n, Sy / n
    cxx, cxy, cyy = Sxx / n - mx * mx, Sxy / n - mx * my, Syy / n - my * my
    lam = (cxx + cyy) / 2 - np.sqrt(((cxx - cyy) / 2) ** 2 + cxy ** 2)
    e_line = np.sqrt(np.maximum(lam, 0.0))

    # 円: x^2 + y^2 = a x + b y + c の最小二乗（3x3 をクラメルの公式で解く）
    det = (Sxx * (Syy * n - Sy * Sy) - Sxy * (Sxy * n - Sy * Sx) + Sx * (Sxy * Sy - Syy * Sx))
    with np.errstate(divide="ignore", invalid="ignore"):
        a = (Sxz * (Syy * n - Sy * Sy) - Sxy * (Syz * n - Sy * Sz) + Sx * (Syz * Sy - Syy * Sz)) / det
        b = (Sxx * (Syz * n - Sz * Sy) - Sxz * (Sxy * n - Sy * Sx) + Sx * (Sxy * Sz - Syz * Sx)) / det
        c = (Sxx * (Syy * Sz - Sy * Syz) - Sxy * (Sxy * Sz - Syz * Sx) + Sxz * (Sxy * Sy - Syy * Sx)) / det
        r2 = c + (a * a + b * b) / 4
        ss = Szz - a * Sxz - b * Syz - c * Sz
        e_arc = np.sqrt(np.maximum(ss, 0.0) / n) / (2 * np.sqrt(np.maximum(r2, 1e-12)))
    # det / (n^3 (cxx+cyy)^2) は点の広がりの「細さ」の二乗。ほぼ一直線なら円は決まらない
    flat = np.abs(det) < 1e-10 * n ** 3 * (cxx + cyy) ** 2 + 1e-30
    bad = ~np.isfinite(e_arc) | flat | (r2 < min_r * min_r) | (r2 > max_r * max_r) | (n < 4)
    e_arc = np.where(bad, np.inf, e_arc)
    return e_line[1:], e_arc[1:]


def _segment(P: np.ndarray, tol: float, max_r: float, min_r: float) -> list[int]:
    """開いた点列 P[0..n] を、要素数が最小になるように分ける。戻り値は区切りの番号（0 と n を含む）。"""
    n = len(P) - 1
    best = np.full(n + 1, np.inf)
    prev = np.zeros(n + 1, int)
    best[0] = 0.0
    reach = 0                      # 直前の終点で、当てはめが成り立ったいちばん遠い始点
    for j in range(1, n + 1):
        lo = max(0, min(reach, j - 1) - 40)
        e_line, e_arc = _fit_errors(P, j, lo, max_r, min_r)
        err = np.minimum(e_line, e_arc * 1.0001)          # i = j-1, ..., lo
        ok = err <= tol
        ok[0] = True                                       # 2 点の直線は必ず成り立つ
        i_all = j - 1 - np.arange(len(err))
        cost = np.where(ok, best[i_all] + 1.0 + 1e-3 * np.minimum(err / tol, 1.0) ** 2, np.inf)
        k = int(np.argmin(cost))
        best[j], prev[j] = cost[k], i_all[k]
        reach = int(i_all[ok].min())
    cuts = [n]
    while cuts[-1] > 0:
        cuts.append(int(prev[cuts[-1]]))
    return cuts[::-1]


class Prim:
    def __init__(self, pts: np.ndarray, tol: float, max_r: float, min_r: float):
        self.pts = pts
        (m, d), e_l = fit_line(pts)
        self.kind, self.geom, self.dev = "L", (m, d), e_l
        if len(pts) >= 4 and math.sqrt(float(np.mean(e_l ** 2))) > tol:
            (c, r), e_a = fit_circle(pts)
            if min_r <= r < max_r and float(np.mean(e_a ** 2)) < float(np.mean(e_l ** 2)):
                self.kind, self.geom, self.dev = "A", (c, r), e_a

    @property
    def length(self):
        return float(np.linalg.norm(np.diff(self.pts, axis=0), axis=1).sum())


def _prims(P: np.ndarray, prm: FitParams, tol: float, depth: int = 0) -> list[Prim]:
    cuts = _segment(P, tol, prm.max_r, prm.min_r)
    out = []
    for a, b in zip(cuts[:-1], cuts[1:]):
        p = Prim(P[a:b + 1], tol, prm.max_r, prm.min_r)
        if b - a >= 6 and depth < 3 and float(p.dev.max()) > prm.tol_max:
            out += _prims(P[a:b + 1], prm, tol / 2, depth + 1)   # 小さな出っ張り・切り欠きを含むので分け直す
        else:
            out.append(p)
    return out


# ---- 幾何 -----------------------------------------------------------------------

def project(prim, x):
    if prim.kind == "L":
        m, d = prim.geom
        return m + d * ((x - m) @ d)
    c, r = prim.geom
    v = x - c
    return c + v * (r / np.linalg.norm(v))


def intersections(a, b):
    if a.kind == "A" and b.kind == "L":
        a, b = b, a
    if a.kind == "L" and b.kind == "L":
        (m1, d1), (m2, d2) = a.geom, b.geom
        det = d1[0] * -d2[1] + d2[0] * d1[1]
        if abs(det) < 0.05:       # ほぼ平行な直線どうしの交点は遠くへ飛ぶので使わない
            return []
        t = ((m2 - m1)[0] * -d2[1] + d2[0] * (m2 - m1)[1]) / det
        return [m1 + t * d1]
    if a.kind == "L":
        (m, d), (c, r) = a.geom, b.geom
        f = m - c; bb = f @ d; cc = f @ f - r * r
        disc = bb * bb - cc
        if disc < 0:
            return []
        sq = math.sqrt(disc)
        return [m + (-bb - sq) * d, m + (-bb + sq) * d]
    (c1, r1), (c2, r2) = a.geom, b.geom
    dv = c2 - c1; d = np.linalg.norm(dv)
    if d < 1e-9 or d > r1 + r2 or d < abs(r1 - r2):
        return []
    t = (r1 * r1 - r2 * r2 + d * d) / (2 * d)
    hh = math.sqrt(max(r1 * r1 - t * t, 0.0))
    base = c1 + dv * (t / d); perp = np.array([-dv[1], dv[0]]) / d
    return [base + perp * hh, base - perp * hh]


def joint(a, b, prm: FitParams):
    """要素 a の終わりと要素 b の始まりをつなぐ頂点。近くで交われば交点、交わらなければ両方への射影の中点。"""
    x0 = (a.pts[-1] + b.pts[0]) / 2
    gap = np.linalg.norm(a.pts[-1] - b.pts[0])
    cands = [p for p in intersections(a, b) if np.linalg.norm(p - x0) < gap + 3 * prm.tol + prm.snap]
    if cands:
        return min(cands, key=lambda p: np.linalg.norm(p - x0))
    return (project(a, x0) + project(b, x0)) / 2


def bulge(prim, A, B):
    c, _ = prim.geom
    ang = lambda p: math.atan2(p[1] - c[1], p[0] - c[0])
    a0, a1, am = ang(A), ang(B), ang(prim.pts[len(prim.pts) // 2])
    ccw = (a1 - a0) % (2 * math.pi)
    if (am - a0) % (2 * math.pi) < ccw:
        return math.tan(ccw / 4)
    return -math.tan((2 * math.pi - ccw) / 4)


# ---- 公開関数 -------------------------------------------------------------------

def resample(P: np.ndarray, step: float) -> np.ndarray:
    """閉じた折れ線（最後の点は最初の点と重ならない）を、ほぼ step 間隔の点列にする。"""
    Q = np.vstack([P, P[:1]])
    d = np.linalg.norm(np.diff(Q, axis=0), axis=1)
    s = np.r_[0, np.cumsum(d)]
    n = max(8, int(round(s[-1] / step)))
    t = np.arange(n) * (s[-1] / n)
    return np.c_[np.interp(t, s, Q[:, 0]), np.interp(t, s, Q[:, 1])]


def _sharpest(P: np.ndarray, k: int) -> int:
    """いちばん鋭い角の番号（輪の切れ目にする）。"""
    a = P - np.roll(P, k, axis=0)
    b = np.roll(P, -k, axis=0) - P
    cross = a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]
    dot = (a * b).sum(1)
    return int(np.argmax(np.abs(np.arctan2(cross, dot))))


def fit_closed(P: np.ndarray, prm: FitParams | None = None) -> list[tuple[float, float, float]]:
    """閉じた点列（ほぼ等間隔）を [(x, y, bulge), ...] にする。全体が 1 つの円なら 2 つの半円で返す。"""
    prm = prm or FitParams()
    P = np.asarray(P, float)
    N = len(P)
    if N < 3:
        return [(float(x), float(y), 0.0) for x, y in P]
    if N >= 8:
        (c, r), dev = fit_circle(P)
        if math.sqrt(float(np.mean(dev ** 2))) <= prm.tol and dev.max() <= prm.tol_max and r < prm.max_r:
            b = 1.0 if _signed_area(P) > 0 else -1.0
            return [(float(c[0] - r), float(c[1]), b), (float(c[0] + r), float(c[1]), b)]
    step = float(np.linalg.norm(P[1] - P[0])) or 1.0
    P = np.roll(P, -_sharpest(P, max(1, int(round(1.5 / step)))), axis=0)
    P = np.vstack([P, P[:1]])  # 閉じる
    prims = _prims(P, prm, prm.tol)
    if len(prims) > 3:
        kept = [p for p in prims if p.length >= prm.min_len]
        if len(kept) >= 3:
            prims = kept
    if len(prims) < 2:
        return [(float(x), float(y), 0.0) for x, y in P[:-1]]

    verts = [joint(prims[i - 1], prims[i], prm) for i in range(len(prims))]
    out = []
    for i, prim in enumerate(prims):
        A, B = verts[i], verts[(i + 1) % len(verts)]
        if np.linalg.norm(B - A) < 1e-6:
            continue
        b = bulge(prim, A, B) if prim.kind == "A" else 0.0
        if abs(b) < 1e-5:
            b = 0.0
        out.append((float(A[0]), float(A[1]), float(b)))
    return out


def _signed_area(P):
    x, y = P[:, 0], P[:, 1]
    return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))
