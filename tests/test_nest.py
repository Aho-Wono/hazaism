"""敷き詰めの Python 側（向きの一覧・格子の画像・JavaScript に渡すデータ・検査）の検査。

探索そのもの（JavaScript）の検査は test_nest_js.py。
"""
import time

import numpy as np
import pytest
from shapely.geometry import Point, Polygon, box

from hazaism.dxfio import reader
from hazaism.nest import engine, raster, verify


# ---- 部品 ----
def test_rasterize_respects_holes():
    ring = Point(10, 10).buffer(8).difference(Point(10, 10).buffer(4))
    m = raster.rasterize(ring, 0, 0, 0.5, (40, 40))
    assert m[20, 20] == 0 and m[20, 8] == 1 and m[2, 2] == 0
    # 輪郭がかかる画素も塗るので、図形より少し大きめ（1 画素ぶんまで）になる
    assert ring.area <= m.sum() * 0.25 <= ring.buffer(0.75).area


def test_orientations_use_symmetry():
    circle = engine.NestPart(0, Point(0, 0).buffer(20), 1)
    assert engine.orientations(circle, 5.0) == [(0.0, False)]
    rect = engine.NestPart(1, box(0, 0, 40, 10), 1)
    assert engine.orientations(rect, 45.0) == [(0.0, False), (45.0, False), (90.0, False), (135.0, False)]
    ell = Polygon([(0, 0), (30, 0), (30, 8), (8, 8), (8, 20), (0, 20)])      # 鏡像と重ならない L 字
    o = engine.orientations(engine.NestPart(2, ell, 1), 90.0)
    assert len(o) == 8 and sum(f for _, f in o) == 4
    assert all(not f for _, f in engine.orientations(engine.NestPart(3, ell, 1, allow_flip=False), 90.0))
    assert engine.orientations(engine.NestPart(4, ell, 1, rotation="fixed"), 5.0) == [(0.0, False), (0.0, True)]
    assert len(engine.orientations(engine.NestPart(5, ell, 1, rotation="90", allow_flip=False), 5.0)) == 4
    # 二等辺三角形は鏡映の軸があるので、裏返しは要らない。軸の向きが試す角度にそろわない形は、裏返しを残す
    iso = Polygon([(0, 0), (40, 0), (20, 30)])
    assert all(not f for _, f in engine.orientations(engine.NestPart(6, iso, 1), 15.0))
    assert any(f for _, f in engine.orientations(engine.NestPart(7, Polygon([(0, 0), (40, 0), (25, 30)]), 1), 15.0))


def test_orientation_counts_of_sample_parts():
    """試験の部材の向きの数（0.5.0 までの Python 版と同じ。対称の調べ方を速くしても変わらないこと）。"""
    import make_parts
    make_parts.ensure()
    want = {"bracket_L.dxf": (360, 180), "gusset.dxf": (360, 180), "flange.dxf": (30, 30), "slot_plate.dxf": (90, 90),
            "cover_cm.dxf": (180, 180), "mirrored_arc.dxf": (180, 180)}
    for name, (n_flip, n_noflip) in want.items():
        poly = reader.load_part(str(make_parts.OUT / name), name).polygon
        assert len(engine.orientations(engine.NestPart(0, poly, 1), 2.0)) == n_flip, name
        assert len(engine.orientations(engine.NestPart(0, poly, 1, allow_flip=False), 2.0)) == n_noflip, name


def test_transform_matches_dxf_writer(tmp_path):
    """敷き詰めで使う変換（反転 → 回転 → 移動）と、DXF に書き出す変換が同じであること。"""
    import make_parts
    from hazaism.dxfio import writer
    make_parts.ensure()
    part = reader.load_part(str(make_parts.OUT / "bracket_L.dxf"), "b")
    pl = engine.Placed(part=0, flip=True, angle=33.0, tx=12.5, ty=-7.25)
    want = engine.transform_polygon(part.polygon, pl.flip, pl.angle, pl.tx, pl.ty)
    out = tmp_path / "o.dxf"
    writer.write_layout(str(out), [part], [writer.Placement(0, pl.flip, pl.angle, pl.tx, pl.ty)])
    got = reader.load_part(str(out), "o").polygon
    assert got.boundary.hausdorff_distance(want.boundary) < 0.05


# ---- JavaScript に渡すデータ ----
def test_payload_round_trip():
    """prepare() のデータを読み戻すと、格子と各向きの画像が、図形をそのまま格子にしたものと一致する。"""
    ring = Point(0, 0).buffer(20, quad_segs=16).difference(Point(0, 0).buffer(8, quad_segs=16))
    parts = [engine.NestPart(3, box(0, 0, 33, 12), qty=2), engine.NestPart(8, ring, qty=1, allow_flip=False)]
    container = Polygon([(0, 0), (150, 10), (140, 90), (5, 80)])
    prm = engine.NestParams(spacing=1.0, margin=2.0, angle_step=30.0, time_limit=3.0, seed=4)
    data = engine.prepare(container, parts, prm)
    head = engine.decode_payload(data)
    grid, blocked = engine.make_grid(container, prm)
    assert head["grid"] == {"x0": grid.x0, "y0": grid.y0, "res": grid.res, "W": grid.W, "H": grid.H}
    assert (head["blocked_mask"] == blocked).all()
    assert [p["index"] for p in head["parts"]] == [3, 8] and [p["qty"] for p in head["parts"]] == [2, 1]
    assert head["params"]["time_limit"] == 3.0 and head["params"]["seed"] == 4
    for o in head["orients"]:
        poly = parts[o["part"]].polygon
        g = engine.transform_polygon(poly, o["flip"], o["angle"]).buffer(grid.grow, quad_segs=6)
        want = raster.rasterize(g, o["mx0"], o["my0"], grid.res, (o["h"], o["w"]))
        assert (o["mask"] == want).all()
        assert o["off"] % 4 == 0
    # 向きの番号は部材ごとにまとまっている
    assert sorted(i for p in head["parts"] for i in p["orients"]) == list(range(len(head["orients"])))
    # 同じ部材・同じ設定なら、2 回目は覚えておいたものを使うので速く、中身は同じ
    t = time.time()
    assert engine.prepare(container, parts, prm) == data
    assert time.time() - t < 0.5


def test_parts_too_big_are_left_out():
    container = box(0, 0, 30, 30)
    parts = [engine.NestPart(0, box(0, 0, 60, 60), qty=2), engine.NestPart(1, box(0, 0, 10, 10), qty=1)]
    head = engine.decode_payload(engine.prepare(container, parts, engine.NestParams(angle_step=90.0)))
    assert head["parts"][0]["orients"] == [] and head["parts"][1]["orients"]


# ---- 検査 ----
def test_measure_reports_actual_gaps():
    container = box(0, 0, 100, 100)
    polys = {0: box(0, 0, 20, 20)}
    P = engine.Placed
    m = verify.measure(container, polys, [P(0, False, 0, 5, 7), P(0, False, 0, 30, 7)])
    assert m["min_spacing"] == pytest.approx(5.0) and m["min_margin"] == pytest.approx(5.0)
    m1 = verify.measure(container, polys, [P(0, False, 0, 40, 40)])
    assert m1["min_spacing"] is None and m1["min_margin"] == pytest.approx(40.0)
    assert verify.measure(container, polys, []) == {"min_spacing": None, "min_margin": None}
    # 遠く離れた 2 個（50 mm 以上）でも測れる
    big = box(0, 0, 400, 100)
    m2 = verify.measure(big, polys, [P(0, False, 0, 5, 5), P(0, False, 0, 300, 5)])
    assert m2["min_spacing"] == pytest.approx(275.0)


def test_verify_detects_violations():
    container = box(0, 0, 100, 100)
    polys = {0: box(0, 0, 20, 20)}
    P = engine.Placed
    ok = [P(0, False, 0, 5, 5), P(0, False, 0, 30, 5)]
    assert verify.verify(container, polys, ok, 3.0, 3.0) == []
    kinds = lambda pls: sorted(p["kind"] for p in verify.verify(container, polys, pls, 3.0, 3.0))
    assert kinds([P(0, False, 0, 5, 5), P(0, False, 0, 26, 5)]) == ["spacing"]       # 間隔 1 mm
    assert kinds([P(0, False, 0, 1, 5)]) == ["margin"]
    assert kinds([P(0, False, 0, 90, 5)]) == ["outside"]
    keep, dropped = verify.drop_invalid(container, polys, [P(0, False, 0, 5, 5), P(0, False, 0, 26, 5), P(0, False, 0, 60, 60)], 3.0, 3.0)
    assert len(keep) == 2 and len(dropped) == 1
