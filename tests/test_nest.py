"""敷き詰めの検査。"""
import json
import math
from pathlib import Path

import numpy as np
import pytest
from shapely import affinity
from shapely.geometry import Point, Polygon, box

from hazaism.dxfio import reader
from hazaism.geom import outline as go
from hazaism.nest import engine, raster, verify

DATA = Path(__file__).resolve().parent / "data"


def params(**kw):
    base = dict(spacing=2.0, margin=2.0, angle_step=15.0, time_limit=4.0, seed=1)
    base.update(kw)
    return engine.NestParams(**base)


def check(container, parts, layout, prm):
    polys = {p.index: p.polygon for p in parts}
    problems = verify.verify(container, polys, layout.placements, prm.spacing, prm.margin)
    assert not problems, problems
    for p in parts:
        n = sum(1 for pl in layout.placements if pl.part == p.index)
        assert n <= p.qty
        if not p.allow_flip:
            assert not any(pl.flip for pl in layout.placements if pl.part == p.index)
    return polys


# ---- 部品 ----
def test_no_fit_region_matches_brute_force():
    rng = np.random.default_rng(0)
    for _ in range(5):
        a = (rng.random((rng.integers(5, 30), rng.integers(5, 30))) > 0.6).astype(np.uint8)
        b = (rng.random((rng.integers(4, 20), rng.integers(4, 20))) > 0.6).astype(np.uint8)
        size = (raster.fft_size(a.shape[0] + b.shape[0] - 1), raster.fft_size(a.shape[1] + b.shape[1] - 1))
        got = raster.no_fit_region(raster.mask_fft(a, size), raster.mask_fft(b, size), size, a.shape, b.shape)
        h, w = b.shape
        pad = np.zeros((a.shape[0] + 2 * (h - 1), a.shape[1] + 2 * (w - 1)), np.uint8)
        pad[h - 1:h - 1 + a.shape[0], w - 1:w - 1 + a.shape[1]] = a
        want = np.zeros_like(got)
        for j in range(got.shape[0]):
            for i in range(got.shape[1]):
                want[j, i] = (pad[j:j + h, i:i + w] & b).any()
        assert (got == want).all()


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


# ---- 敷き詰め ----
def test_rectangles_fill_a_rectangle():
    container = box(0, 0, 200, 100)
    parts = [engine.NestPart(0, box(0, 0, 46, 21.5), qty=16)]
    prm = params(time_limit=5.0)
    layout = engine.nest_once(container, parts, prm)
    check(container, parts, layout, prm)
    assert len(layout.placements) == 16, "4 × 4 に並ぶはず"


def test_stops_when_nothing_fits():
    container = box(0, 0, 30, 30)
    parts = [engine.NestPart(0, box(0, 0, 60, 60), qty=2), engine.NestPart(1, box(0, 0, 10, 10), qty=1)]
    prm = params(time_limit=1.0)
    layout = engine.nest_once(container, parts, prm)
    check(container, parts, layout, prm)
    assert [pl.part for pl in layout.placements] == [1]


def test_parts_inside_holes():
    ring = Point(0, 0).buffer(40, quad_segs=32).difference(Point(0, 0).buffer(26, quad_segs=32))
    disc = Point(0, 0).buffer(10, quad_segs=32)
    container = box(0, 0, 88, 88)
    parts = [engine.NestPart(0, ring, qty=1), engine.NestPart(1, disc, qty=1)]
    prm = params(time_limit=2.0)
    layout = engine.nest_once(container, parts, prm)
    polys = check(container, parts, layout, prm)
    assert sorted(pl.part for pl in layout.placements) == [0, 1]
    placed = dict(zip([pl.part for pl in layout.placements], verify.placed_polygons(polys, layout.placements)))
    assert Polygon(placed[0].exterior).contains(placed[1]), "小さい円板は輪の穴の中に入るはず"
    # 穴の中を使わない設定では入らない
    layout2 = engine.nest_once(container, parts, params(time_limit=2.0, use_holes=False))
    assert [pl.part for pl in layout2.placements] == [0]


def test_scrap_outline_with_dxf_parts():
    shape = json.loads((DATA / "scrap_outline_01.json").read_text(encoding="utf-8"))
    container = go.shape_to_polygon(shape, 0.02)
    import make_parts
    make_parts.ensure()
    specs = [("bracket_L.dxf", 3, False), ("gusset.dxf", 4, True), ("slot_plate.dxf", 2, True)]
    parts = [engine.NestPart(i, reader.load_part(str(make_parts.OUT / n), n).polygon, qty=q, allow_flip=f)
             for i, (n, q, f) in enumerate(specs)]
    prm = params(spacing=3.0, margin=3.0, angle_step=10.0, time_limit=6.0)
    layout = engine.nest_once(container, parts, prm)
    check(container, parts, layout, prm)
    assert len(layout.placements) >= 4
    assert layout.placed_area == pytest.approx(sum(parts[pl.part].polygon.area for pl in layout.placements))


def test_workers_from_prepared_payload():
    """ブラウザと同じ流れ: prepare() のデータで作業スレッドを 2 本作り、少しずつ回して、良いほうを採る。"""
    import json as _json
    container = box(0, 0, 150, 90)
    parts = [engine.NestPart(7, box(0, 0, 40, 25), qty=6), engine.NestPart(9, Point(0, 0).buffer(12), qty=4)]
    prm = params(time_limit=30.0)
    payload = engine.prepare(container, parts, prm)
    workers = [engine.NestWorker(payload, wid) for wid in range(2)]
    best = None
    for _ in range(4):
        for w in workers:
            out = w.run_for(0.2)
            _json.dumps(out)                     # 画面へ送れる形（JSON にできる）
            if out["best"] and (best is None or out["best"]["key"] > best["key"]):
                best = out["best"]
    assert best is not None and all(w.runs >= 4 for w in workers)
    layout = engine.Layout(placements=engine.placements_from_dicts(best["placements"]), placed_area=best["placed_area"])
    check(container, parts, layout, prm)
    assert len(layout.placements) >= 8
    assert {pl.part for pl in layout.placements} <= {7, 9}
    # 2 本は違う乱数で探している（同じ結果ばかりにならない）
    assert workers[0].rng.random() != workers[1].rng.random()


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
