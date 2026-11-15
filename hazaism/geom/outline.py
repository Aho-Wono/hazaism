"""輪郭のモデルと幾何演算。

Loop  = 頂点 (x, y, bulge) の閉じた列。bulge はその頂点から次の頂点までの円弧（DXF の LWPOLYLINE と同じ。0 は直線、
        正は反時計回り）。座標は mm・x 右・y 上。
Shape = {"outer": Loop, "holes": [Loop, ...]}（JSON にそのまま載せる形）。外周は反時計回り、穴は時計回りにそろえる。
"""
from __future__ import annotations

import math

import numpy as np
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.polygon import orient

from ..vision import arcfit

FLAT_SAG = 0.002      # 演算用に円弧を折れ線にするときの矢高 [mm]


# ---- 円弧 ---------------------------------------------------------------------------

def arc_params(p0, p1, b):
    """bulge 付きの辺 → (中心, 半径, 開始角, 掃引角[符号つき])。"""
    p0, p1 = np.asarray(p0, float), np.asarray(p1, float)
    chord = p1 - p0
    L = float(np.linalg.norm(chord))
    sweep = 4 * math.atan(b)
    r = L / (2 * math.sin(abs(sweep) / 2))
    mid = (p0 + p1) / 2
    # 中心は弦の中点から左法線方向へ d（b > 0 のとき左側、劣弧なら d > 0）
    d = (L / 2) / math.tan(sweep / 2)
    n = np.array([-chord[1], chord[0]]) / L
    c = mid + n * d
    a0 = math.atan2(p0[1] - c[1], p0[0] - c[0])
    return c, r, a0, sweep


def bulge_from_sweep(sweep: float) -> float:
    return math.tan(sweep / 4)


def flatten_loop(loop, sag: float = FLAT_SAG) -> np.ndarray:
    """Loop → 折れ線の頂点列（閉じる点は重ねない）。"""
    pts = []
    n = len(loop)
    for i in range(n):
        x0, y0, b = loop[i]
        x1, y1, _ = loop[(i + 1) % n]
        pts.append((x0, y0))
        if abs(b) < 1e-9 or (x0 == x1 and y0 == y1):
            continue
        c, r, a0, sweep = arc_params((x0, y0), (x1, y1), b)
        step = 2 * math.acos(max(-1.0, 1 - min(sag, r) / r)) if r > sag else math.pi / 4
        k = max(1, int(math.ceil(abs(sweep) / max(step, 1e-6))))
        for j in range(1, k):
            a = a0 + sweep * j / k
            pts.append((c[0] + r * math.cos(a), c[1] + r * math.sin(a)))
    return np.array(pts, float)


def loop_area(loop) -> float:
    """符号つき面積（円弧ぶんも含む）。反時計回りが正。"""
    n = len(loop)
    area = 0.0
    for i in range(n):
        x0, y0, b = loop[i]
        x1, y1, _ = loop[(i + 1) % n]
        area += 0.5 * (x0 * y1 - x1 * y0)
        if abs(b) > 1e-9:
            _, r, _, sweep = arc_params((x0, y0), (x1, y1), b)
            area += 0.5 * r * r * (sweep - math.sin(sweep))
    return area


def reverse_loop(loop):
    n = len(loop)
    out = []
    for i in range(n - 1, -1, -1):
        x, y, _ = loop[i]
        out.append((x, y, -loop[(i - 1) % n][2]))
    return out


def normalize_shape(shape: dict) -> dict:
    outer = [tuple(map(float, p)) for p in shape["outer"]]
    if loop_area(outer) < 0:
        outer = reverse_loop(outer)
    holes = []
    for h in shape.get("holes", []):
        h = [tuple(map(float, p)) for p in h]
        if loop_area(h) > 0:
            h = reverse_loop(h)
        holes.append(h)
    return {"outer": outer, "holes": holes}


def shape_to_polygon(shape: dict, sag: float = FLAT_SAG) -> Polygon:
    outer = flatten_loop(shape["outer"], sag)
    holes = [flatten_loop(h, sag) for h in shape.get("holes", [])]
    return Polygon(outer, [h for h in holes if len(h) >= 3])


def shape_bounds(shape: dict):
    return shape_to_polygon(shape, 0.05).bounds


# ---- 図形（矩形・円・楕円・多角形）→ Loop -------------------------------------------

def primitive_loop(prim: dict):
    t = prim["type"]
    ang = math.radians(prim.get("angle", 0.0))
    ca, sa = math.cos(ang), math.sin(ang)

    def rot(x, y, cx, cy):
        return (cx + x * ca - y * sa, cy + x * sa + y * ca)

    if t == "rect":
        cx, cy = prim["x"] + prim["w"] / 2, prim["y"] + prim["h"] / 2
        hw, hh = abs(prim["w"]) / 2, abs(prim["h"]) / 2
        return [(*rot(dx, dy, cx, cy), 0.0) for dx, dy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))]
    if t == "circle":
        cx, cy, r = prim["cx"], prim["cy"], abs(prim["r"])
        return [(cx - r, cy, 1.0), (cx + r, cy, 1.0)]
    if t == "ellipse":
        cx, cy, rx, ry = prim["cx"], prim["cy"], abs(prim["rx"]), abs(prim["ry"])
        if abs(rx - ry) < 1e-6:
            return [(cx - rx, cy, 1.0), (cx + rx, cy, 1.0)]
        # 楕円は bulge では表せないので、細かい点列を円弧に当てはめ直す（誤差 0.02 mm 以内）
        peri = 2 * math.pi * max(rx, ry)
        n = int(min(4000, max(256, peri / 0.1)))
        a = np.arange(n) * (2 * math.pi / n)
        P = np.array([rot(rx * math.cos(t_), ry * math.sin(t_), cx, cy) for t_ in a])
        return arcfit.fit_closed(P, arcfit.FitParams(tol=0.02, tol_max=0.06, min_len=0.0, min_r=0.2, snap=0.2))
    if t == "polygon":
        return [(float(x), float(y), 0.0) for x, y in prim["pts"]]
    raise ValueError(f"unknown primitive: {t}")


# ---- 折れ線の結果を、元の直線・円弧に戻す ------------------------------------------

def _sources(loops):
    src = []
    for loop in loops:
        n = len(loop)
        for i in range(n):
            x0, y0, b = loop[i]
            x1, y1, _ = loop[(i + 1) % n]
            if x0 == x1 and y0 == y1:
                continue
            if abs(b) < 1e-9:
                src.append(("L", np.array([x0, y0]), np.array([x1, y1])))
            else:
                c, r, a0, sweep = arc_params((x0, y0), (x1, y1), b)
                src.append(("A", c, r, a0, sweep))
    return src


def _on_source(src, Q, tol):
    if src[0] == "L":
        _, p0, p1 = src
        d = p1 - p0
        L = np.linalg.norm(d)
        d = d / L
        t = (Q - p0) @ d
        dist = np.abs((Q - p0) @ np.array([-d[1], d[0]]))
        return (dist < tol) & (t > -tol) & (t < L + tol)
    _, c, r, a0, sweep = src
    v = Q - c
    rad = np.linalg.norm(v, axis=1)
    ang = np.arctan2(v[:, 1], v[:, 0])
    rel = (ang - a0) * (1 if sweep > 0 else -1)
    rel = np.mod(rel + tol / r, 2 * math.pi) - tol / r
    return (np.abs(rad - r) < tol) & (rel < abs(sweep) + tol / r)


def _ring_to_loop(ring: np.ndarray, sources, tol):
    """折れ線の輪（閉じる点は重ねない）を、元になった直線・円弧ごとにまとめて Loop にする。"""
    n = len(ring)
    if not sources:
        return [(float(x), float(y), 0.0) for x, y in ring]
    M = np.array([_on_source(s, ring, tol) for s in sources])       # [source, point]
    mid = (ring + np.roll(ring, -1, axis=0)) / 2
    # 辺 i = 点 i → i+1。両端と中点が同じ直線・円弧に乗っていれば、その辺はそこから来たもの
    # （中点も見るのは、円を横切る直線の両端が円周上にあっても円弧と間違えないため）
    E = M & np.roll(M, -1, axis=1) & np.array([_on_source(s, mid, tol) for s in sources])
    owner = np.full(n, -1)
    prev = -1
    for i in range(n):
        cand = np.where(E[:, i])[0]
        if len(cand) == 0:
            prev = -1
            continue
        prev = prev if prev in cand else int(cand[0])
        owner[i] = prev
    # 輪の切れ目を、持ち主が変わる場所へ回す
    start = 0
    for i in range(n):
        if owner[i] != owner[i - 1] or owner[i] == -1:
            start = i
            break
    order = [(start + i) % n for i in range(n)]
    loop = []
    i = 0
    while i < n:
        k = owner[order[i]]
        j = i
        if k >= 0:
            while j + 1 < n and owner[order[j + 1]] == k:
                j += 1
        a = ring[order[i]]
        if k >= 0 and sources[k][0] == "A":
            _, c, r, _, s_sweep = sources[k]
            pts = np.array([ring[order[m % n]] for m in range(i, j + 2)])
            v = pts - c
            ang = np.unwrap(np.arctan2(v[:, 1], v[:, 0]))
            sweep = float(ang[-1] - ang[0])
            if abs(sweep) < 1e-9:
                loop.append((float(a[0]), float(a[1]), 0.0))
            elif abs(sweep) > math.pi * 1.0001:
                # 半周を超える円弧は 2 つに分ける（bulge が大きくなりすぎないように）
                am = ang[0] + sweep / 2
                mid = c + r * np.array([math.cos(am), math.sin(am)])
                bb = bulge_from_sweep(sweep / 2)
                loop.append((float(a[0]), float(a[1]), bb))
                loop.append((float(mid[0]), float(mid[1]), bb))
            else:
                loop.append((float(a[0]), float(a[1]), bulge_from_sweep(sweep)))
        else:
            loop.append((float(a[0]), float(a[1]), 0.0))
        i = j + 1
    return simplify_loop(loop)


def simplify_loop(loop, tol: float = 1e-6):
    """一直線に並んだ直線どうしの間の頂点と、重なった頂点を取り除く。"""
    out = list(loop)
    changed = True
    while changed and len(out) > 3:
        changed = False
        n = len(out)
        for i in range(n):
            p, c, q = out[i - 1], out[i], out[(i + 1) % n]
            if math.hypot(c[0] - q[0], c[1] - q[1]) < tol:
                out.pop(i); changed = True; break
            if abs(p[2]) < 1e-9 and abs(c[2]) < 1e-9:
                cross = (c[0] - p[0]) * (q[1] - c[1]) - (c[1] - p[1]) * (q[0] - c[0])
                dot = (c[0] - p[0]) * (q[0] - c[0]) + (c[1] - p[1]) * (q[1] - c[1])
                L = math.hypot(c[0] - p[0], c[1] - p[1]) * math.hypot(q[0] - c[0], q[1] - c[1])
                if L > 0 and abs(cross) / L < 1e-7 and dot > 0:
                    out.pop(i); changed = True; break
    return out


def polygon_to_shape_exact(poly: Polygon, source_loops, tol: float = 4 * FLAT_SAG) -> dict:
    """折れ線化して演算した結果を、元の Loop の直線・円弧を使って Shape に戻す。"""
    src = _sources(source_loops)
    poly = orient(poly, 1.0)
    outer = _ring_to_loop(np.asarray(poly.exterior.coords)[:-1], src, tol)
    holes = [_ring_to_loop(np.asarray(r.coords)[:-1], src, tol) for r in poly.interiors]
    return normalize_shape({"outer": outer, "holes": holes})


def polygon_to_shape_fit(poly: Polygon, prm: arcfit.FitParams | None = None, step: float = 0.3) -> dict:
    """測定で得た（雑音のある）多角形を、直線と円弧に当てはめて Shape にする。

    当てはめた結果が自分自身と交差したら（小さな凹凸のつなぎ目で起きることがある）、短い要素を多めに捨てて
    やり直し、それでもだめなら折れ線のまま返す。
    """
    from dataclasses import replace
    poly = orient(poly, 1.0)
    prm = prm or arcfit.FitParams()
    rings = [arcfit.resample(np.asarray(r.coords)[:-1], step) for r in (poly.exterior, *poly.interiors)]
    for k in (1.0, 2.0, 4.0):
        q = replace(prm, min_len=max(prm.min_len, 1.0) * k, min_r=prm.min_r * k)
        loops = [arcfit.fit_closed(P, q) for P in rings]
        loops = [lp for lp in loops if len(lp) >= 2]
        if not loops:
            continue
        shape = normalize_shape({"outer": loops[0], "holes": loops[1:]})
        if not validate(shape):
            return shape
    simp = poly.simplify(0.1)
    return normalize_shape({
        "outer": [(x, y, 0.0) for x, y in list(simp.exterior.coords)[:-1]],
        "holes": [[(x, y, 0.0) for x, y in list(r.coords)[:-1]] for r in simp.interiors]})


def largest_polygon(geom) -> tuple[Polygon | None, int]:
    """演算結果からいちばん大きい多角形を取り出す。戻り値: (多角形, 捨てた個数)。"""
    if geom.is_empty:
        return None, 0
    if isinstance(geom, Polygon):
        return geom, 0
    polys = [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon) and g.area > 1e-6]
    if isinstance(geom, MultiPolygon) or polys:
        polys.sort(key=lambda p: p.area, reverse=True)
        return (polys[0], len(polys) - 1) if polys else (None, 0)
    return None, 0


def boolean(shape: dict, prim: dict, op: str) -> tuple[dict, int]:
    """輪郭に図形を足す（op="add"）／引く（op="sub"）。戻り値: (新しい Shape, 捨てた破片の数)。"""
    shape = normalize_shape(shape)
    ploop = primitive_loop(prim)
    if loop_area(ploop) < 0:
        ploop = reverse_loop(ploop)
    a = shape_to_polygon(shape).buffer(0)
    b = Polygon(flatten_loop(ploop)).buffer(0)
    res = a.union(b) if op == "add" else a.difference(b)
    poly, dropped = largest_polygon(res)
    if poly is None:
        raise ValueError("図形がなくなってしまいます")
    return polygon_to_shape_exact(poly, [shape["outer"], *shape["holes"], ploop]), dropped


def validate(shape: dict) -> list[str]:
    """輪郭の不具合（自己交差など）を文章で返す。問題なければ空。"""
    msgs = []
    try:
        poly = shape_to_polygon(shape, 0.01)
    except Exception as e:  # 頂点不足など
        return [f"輪郭を作れません: {e}"]
    if len(shape["outer"]) < 2:
        msgs.append("輪郭の頂点が足りません")
    elif not poly.is_valid:
        msgs.append("輪郭が自分自身と交差しています（または穴が外にはみ出しています）")
    elif poly.area < 1.0:
        msgs.append("輪郭の面積がほとんどありません")
    return msgs
