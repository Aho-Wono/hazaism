"""敷き詰めの探索（JavaScript: hazaism/static/js/nest/engine.js）の検査。

Python で準備したデータ（engine.prepare）を、Chromium の中の engine.js で探索し、結果を Python の verify で確かめる。
ファイルは hazaism/static からそのまま読む（サイトの組み立ても配信も要らない）。写真は使わないので GitHub の自動検査でも走る。
前提: uv run playwright install chromium
"""
import base64
import json
import mimetypes
import time
from pathlib import Path

import numpy as np
import pytest
from numpy.lib.stride_tricks import sliding_window_view
from shapely.geometry import Point, Polygon, box

from hazaism.dxfio import reader
from hazaism.geom import outline as go
from hazaism.nest import engine, verify

pw_api = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "hazaism" / "static"
DATA = Path(__file__).resolve().parent / "data"
ORIGIN = "http://hazaism.test/"


@pytest.fixture(scope="module")
def page():
    with pw_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as ex:            # Chromium が入っていない
            pytest.skip(f"Chromium を起動できません: {str(ex).splitlines()[0]}")

        def serve(route):
            path = route.request.url[len(ORIGIN):].split("?")[0] or "index.html"
            if path == "__test__.html":
                return route.fulfill(body="<!doctype html><meta charset=utf-8><title>nest</title>", content_type="text/html")
            f = (STATIC / path).resolve()
            if STATIC not in f.parents or not f.is_file():
                return route.fulfill(status=404, body="")
            ctype = "text/javascript" if f.suffix in (".js", ".mjs") else (mimetypes.guess_type(f.name)[0] or "application/octet-stream")
            return route.fulfill(body=f.read_bytes(), content_type=ctype)

        ctx = browser.new_context()
        ctx.route(ORIGIN + "**", serve)
        pg = ctx.new_page()
        pg.errors = []
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.goto(ORIGIN + "__test__.html")
        pg.evaluate("async () => { window.E = await import('./js/nest/engine.js'); }")
        yield pg
        browser.close()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


JS_DECODE = "const u8 = Uint8Array.from(atob(b), (c) => c.charCodeAt(0)); const P = E.decodeProblem(u8);"


def js_search(page, payload: bytes, seconds: float, workers: int = 1, opts=None):
    """作業スレッド workers 本ぶんを（順に）seconds 秒ずつ回して、いちばん良い結果と回数を返す。"""
    return page.evaluate(f"""([b, secs, nw, opts]) => {{ {JS_DECODE}
      let best = null, runs = [], rnd = [];
      for (let w = 0; w < nw; w++) {{
        const s = new E.Search(P, w, opts || {{}});
        const out = s.runFor(secs);
        runs.push(out.runs);
        rnd.push(s.rng.random());
        if (out.best && (!best || keyGreater(out.best.key, best.key))) best = out.best;
      }}
      function keyGreater(a, b) {{ for (let i = 0; i < 3; i++) if (a[i] !== b[i]) return a[i] > b[i]; return false; }}
      return {{ best, runs, rnd }};
    }}""", [b64(payload), seconds, workers, opts])


def check(container, parts, best, prm):
    polys = {p.index: p.polygon for p in parts}
    placements = engine.placements_from_dicts(best["placements"]) if best else []
    problems = verify.verify(container, polys, placements, prm.spacing, prm.margin)
    assert not problems, problems
    for p in parts:
        n = sum(1 for pl in placements if pl.part == p.index)
        assert n <= p.qty
        if not p.allow_flip:
            assert not any(pl.flip for pl in placements if pl.part == p.index)
    return polys, placements


def params(**kw):
    base = dict(spacing=2.0, margin=2.0, angle_step=15.0, time_limit=4.0, seed=1)
    base.update(kw)
    return engine.NestParams(**base)


# ---- 置けない位置の計算が、1 画素ずつ調べた答えと一致する ----

def _random_mask(rng, h, w, holes=True):
    m = np.zeros((h, w), np.uint8)
    yy, xx = np.mgrid[0:h, 0:w]
    for _ in range(rng.integers(1, 4)):                       # 楕円をいくつか重ねて、凹みのある形にする
        cy, cx = rng.uniform(0, h), rng.uniform(0, w)
        ry, rx = rng.uniform(2, h / 1.5), rng.uniform(2, w / 1.5)
        m |= (((yy - cy) / ry) ** 2 + ((xx - cx) / rx) ** 2 <= 1).astype(np.uint8)
    if holes and rng.random() < 0.5:
        cy, cx = h / 2, w / 2
        m &= ~((((yy - cy) / (h / 5)) ** 2 + ((xx - cx) / (w / 5)) ** 2 <= 1)).astype(np.uint8)
    m[rng.integers(h), :] |= (rng.random(w) < 0.3).astype(np.uint8)   # ばらばらの画素
    if not m.any():
        m[h // 2, w // 2] = 1
    return m


def _brute(img, mask):
    win = sliding_window_view(img, mask.shape)
    return (win & mask[None, None]).any(axis=(2, 3)).astype(np.uint8)


def test_blocked_positions_match_brute_force(page):
    rng = np.random.default_rng(3)
    for trial in range(12):
        H, W = int(rng.integers(30, 70)), int(rng.integers(40, 100))      # 32 の倍数をまたぐ幅も試す
        blocked = np.zeros((H, W), np.uint8)
        blocked[:2, :] = blocked[-2:, :] = 1
        blocked[:, :2] = blocked[:, -2:] = 1
        blocked |= _random_mask(rng, H, W, holes=False) * (rng.random() < 0.6)
        grid = engine.Grid(x0=0.0, y0=0.0, res=1.0, W=W, H=H, grow=0.0)
        masks = [_random_mask(rng, int(rng.integers(3, 20)), int(rng.integers(3, 40))) for _ in range(3)]
        orients = [{"part": 0, "angle": 0.0, "flip": False, "mask": m, "mx0": 0.0, "my0": 0.0} for m in masks]
        parts = [{"index": 0, "area": 1.0, "qty": 1, "orients": list(range(len(masks)))}]
        payload = engine.pack_problem(grid, blocked, parts, orients, {"time_limit": 1, "seed": 0})
        # 置いた部材（はみ出すもの・端に掛かるものも）
        placed = [[int(rng.integers(len(masks))), int(rng.integers(-5, W)), int(rng.integers(-5, H))] for _ in range(3)]
        placed = [p for p in placed if 0 <= p[1] <= W - masks[p[0]].shape[1] and 0 <= p[2] <= H - masks[p[0]].shape[0]]
        occ = np.zeros((H, W), np.uint8)
        for pk, px, py in placed:
            h, w = masks[pk].shape
            occ[py:py + h, px:px + w] |= masks[pk]
        for oi, m in enumerate(masks):
            if m.shape[0] > H or m.shape[1] > W:
                continue
            got = page.evaluate(f"""([b, oi, placed]) => {{ {JS_DECODE}
                const a = E.blockedImage(P, oi, [], true), c = E.blockedImage(P, oi, placed, false);
                return {{ Hk: a.Hk, Wk: a.Wk, base: a.bits ? Array.from(a.bits) : null, nfr: Array.from(c.bits) }}; }}""",
                                [b64(payload), oi, placed])
            want_base = _brute(blocked, m)
            assert (got["Hk"], got["Wk"]) == want_base.shape
            base = np.array(got["base"], np.uint8).reshape(want_base.shape) if got["base"] else np.ones_like(want_base)
            assert (base == want_base).all(), f"端材に対する置けない位置が違う（試行 {trial}, 向き {oi}）"
            nfr = np.array(got["nfr"], np.uint8).reshape(want_base.shape)
            assert (nfr == _brute(occ, m)).all(), f"置いた部材との重なりが違う（試行 {trial}, 向き {oi}）"
    assert not page.errors, page.errors


# ---- 探索（今までの Python 版の検査を移したもの） ----

def test_rectangles_fill_a_rectangle(page):
    container = box(0, 0, 200, 100)
    parts = [engine.NestPart(0, box(0, 0, 46, 21.5), qty=16)]
    prm = params(time_limit=3.0)
    r = js_search(page, engine.prepare(container, parts, prm), 3.0)
    _, pls = check(container, parts, r["best"], prm)
    assert len(pls) == 16, "4 × 4 に並ぶはず"


def test_stops_when_nothing_fits(page):
    container = box(0, 0, 30, 30)
    parts = [engine.NestPart(0, box(0, 0, 60, 60), qty=2), engine.NestPart(1, box(0, 0, 10, 10), qty=1)]
    prm = params(time_limit=1.0)
    r = js_search(page, engine.prepare(container, parts, prm), 0.5)
    _, pls = check(container, parts, r["best"], prm)
    assert [pl.part for pl in pls] == [1]


def test_parts_inside_holes(page):
    ring = Point(0, 0).buffer(40, quad_segs=32).difference(Point(0, 0).buffer(26, quad_segs=32))
    disc = Point(0, 0).buffer(10, quad_segs=32)
    container = box(0, 0, 88, 88)
    parts = [engine.NestPart(0, ring, qty=1), engine.NestPart(1, disc, qty=1)]
    prm = params(time_limit=2.0)
    r = js_search(page, engine.prepare(container, parts, prm), 1.0)
    polys, pls = check(container, parts, r["best"], prm)
    assert sorted(pl.part for pl in pls) == [0, 1]
    placed = dict(zip([pl.part for pl in pls], verify.placed_polygons(polys, pls)))
    assert Polygon(placed[0].exterior).contains(placed[1]), "小さい円板は輪の穴の中に入るはず"
    # 穴の中を使わない設定では入らない
    prm2 = params(time_limit=2.0, use_holes=False)
    r2 = js_search(page, engine.prepare(container, parts, prm2), 1.0)
    assert [pl["part"] for pl in r2["best"]["placements"]] == [0]


def test_rotation_modes(page):
    ell = Polygon([(0, 0), (30, 0), (30, 8), (8, 8), (8, 20), (0, 20)])
    container = box(0, 0, 120, 120)
    parts = [engine.NestPart(0, ell, qty=4, allow_flip=False, rotation="fixed"),
             engine.NestPart(1, ell, qty=4, allow_flip=True, rotation="90")]
    prm = params(angle_step=5.0, time_limit=2.0)
    r = js_search(page, engine.prepare(container, parts, prm), 1.0)
    _, pls = check(container, parts, r["best"], prm)
    assert all(pl.angle == 0 and not pl.flip for pl in pls if pl.part == 0)
    assert all(pl.angle % 90 == 0 for pl in pls if pl.part == 1)
    assert len(pls) == 8


def test_scrap_outline_with_dxf_parts(page):
    shape = json.loads((DATA / "scrap_outline_01.json").read_text(encoding="utf-8"))
    container = go.shape_to_polygon(shape, 0.02)
    import make_parts
    make_parts.ensure()
    specs = [("bracket_L.dxf", 3, False), ("gusset.dxf", 4, True), ("slot_plate.dxf", 2, True)]
    parts = [engine.NestPart(i, reader.load_part(str(make_parts.OUT / n), n).polygon, qty=q, allow_flip=f)
             for i, (n, q, f) in enumerate(specs)]
    prm = params(spacing=3.0, margin=3.0, angle_step=10.0, time_limit=3.0)
    r = js_search(page, engine.prepare(container, parts, prm), 2.0)
    _, pls = check(container, parts, r["best"], prm)
    assert len(pls) >= 4
    assert r["best"]["placed_area"] == pytest.approx(sum(parts[pl.part].polygon.area for pl in pls))


def test_workers_differ_and_results_are_json(page):
    """ブラウザと同じ流れ: 同じデータで作業スレッドを 2 本作り、良いほうを採る。"""
    container = box(0, 0, 150, 90)
    parts = [engine.NestPart(7, box(0, 0, 40, 25), qty=6), engine.NestPart(9, Point(0, 0).buffer(12), qty=4)]
    prm = params(time_limit=30.0)
    r = js_search(page, engine.prepare(container, parts, prm), 0.5, workers=2)
    assert all(n >= 4 for n in r["runs"])
    _, pls = check(container, parts, r["best"], prm)
    assert len(pls) >= 8 and {pl.part for pl in pls} <= {7, 9}
    assert r["rnd"][0] != r["rnd"][1], "2 本は違う乱数で探す"


def test_speed_and_quality(page):
    """試験の部材 11 個（計 160 cm2）を、約 200 cm2 の不規則な端材に（全部は入らない）。速さと利用率を記録する。

    比べる値（Python 版、2026-11-26、この PC で作業スレッド 4 本・10 秒）: 1 回の貪欲配置に数秒、8 個・利用率 60.3 %。
    """
    from shapely import affinity
    import make_parts
    make_parts.ensure()
    p = Polygon([(0, 0), (262, 40), (250, 200), (180, 323), (60, 300), (10, 160)])
    k = (200 * 100 / p.area) ** 0.5
    container = affinity.scale(p, k, k, origin=(0, 0))
    qty = {"bracket_L.dxf": 3, "gusset.dxf": 3, "flange.dxf": 1, "slot_plate.dxf": 1, "cover_cm.dxf": 1, "mirrored_arc.dxf": 2}
    parts = [engine.NestPart(i, reader.load_part(str(make_parts.OUT / n), n).polygon, qty=q, allow_flip=(n != "bracket_L.dxf"))
             for i, (n, q) in enumerate(qty.items())]
    report = {}
    for secs in (1.0, 5.0):
        prm = engine.NestParams(spacing=0.5, margin=1.5, angle_step=2.0, time_limit=secs)
        t = time.time()
        payload = engine.prepare(container, parts, prm)
        tp = time.time() - t
        r = js_search(page, payload, secs, workers=1)
        _, pls = check(container, parts, r["best"], prm)
        util = 100 * r["best"]["placed_area"] / container.area
        report[secs] = {"prepare_s": round(tp, 2), "runs_per_s": round(r["runs"][0] / secs, 1), "placed": len(pls), "util": round(util, 1)}
    print("JS 版（作業スレッド 1 本）:", report)
    out = ROOT / "tests" / "out"
    out.mkdir(exist_ok=True)
    (out / "nest_js_speed.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    assert report[1.0]["runs_per_s"] >= 4, "1 秒あたり 4 回は回るはず"
    assert report[5.0]["placed"] >= 8, "Python 版（4 本・10 秒）と同じ 8 個は入るはず"
    assert not page.errors, page.errors
