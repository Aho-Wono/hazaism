"""配置結果を、元の図形（ラスタではなく多角形）で確かめる。"""
from __future__ import annotations

from shapely.strtree import STRtree

from .engine import Placed, transform_polygon

TOL = 1e-3   # mm


def placed_polygons(parts_by_index: dict, placements: list[Placed]) -> list:
    return [transform_polygon(parts_by_index[p.part], p.flip, p.angle, p.tx, p.ty) for p in placements]


def verify(container, parts_by_index: dict, placements: list[Placed], spacing: float, margin: float) -> list[dict]:
    """違反の一覧を返す（空なら合格）。

    parts_by_index: 部材番号 → 多角形（mm、ローカル座標）
    違反: {"kind": "outside" | "margin" | "spacing", "a": 配置番号, "b": 相手の配置番号 or None, "value": 実際の距離}
    """
    polys = placed_polygons(parts_by_index, placements)
    problems = []
    boundary = container.boundary
    for i, g in enumerate(polys):
        if not container.covers(g):
            problems.append({"kind": "outside", "a": i, "b": None, "value": 0.0})
            continue
        d = boundary.distance(g)
        if d < margin - TOL:
            problems.append({"kind": "margin", "a": i, "b": None, "value": float(d)})
    if len(polys) > 1:
        tree = STRtree(polys)
        for i, g in enumerate(polys):
            for j in tree.query(g.buffer(spacing)):
                j = int(j)
                if j <= i:
                    continue
                d = g.distance(polys[j])
                if d < spacing - TOL:
                    problems.append({"kind": "spacing", "a": i, "b": j, "value": float(d)})
    return problems


def drop_invalid(container, parts_by_index: dict, placements: list[Placed], spacing: float, margin: float):
    """違反がなくなるまで、違反している配置を後ろから外す。戻り値: (残した配置, 外した配置)。"""
    keep = list(placements)
    dropped = []
    while True:
        problems = verify(container, parts_by_index, keep, spacing, margin)
        if not problems:
            return keep, dropped
        worst = max(max(p["a"], p["b"] if p["b"] is not None else -1) for p in problems)
        dropped.append(keep.pop(worst))


def measure(container, parts_by_index: dict, placements: list[Placed]) -> dict:
    """実際の最小間隔（部材どうし）と最小余白（端材の縁まで）[mm]。部材が 1 個なら間隔は None。"""
    polys = placed_polygons(parts_by_index, placements)
    if not polys:
        return {"min_spacing": None, "min_margin": None}
    boundary = container.boundary
    min_margin = min(float(boundary.distance(g)) for g in polys)
    min_spacing = None
    if len(polys) > 1:
        tree = STRtree(polys)
        for i, g in enumerate(polys):
            # 近いものから順に見る。最小値が見つかっていれば、それより遠いものは調べない
            reach = 50.0 if min_spacing is None else min_spacing
            for j in tree.query(g.buffer(reach)):
                j = int(j)
                if j <= i:
                    continue
                d = float(g.distance(polys[j]))
                if min_spacing is None or d < min_spacing:
                    min_spacing = d
        if min_spacing is None:         # 50 mm 以内に相手がいない
            min_spacing = min(float(polys[i].distance(polys[j])) for i in range(len(polys)) for j in range(i + 1, len(polys)))
    return {"min_spacing": min_spacing, "min_margin": min_margin}
