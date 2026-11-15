"""画面を通しで操作する検査（ブラウザ版。Playwright の Chromium で、ブラウザ内の Python を実際に動かす）。

前提:  uv run playwright install chromium。Pyodide を CDN から読むのでネット接続が要る
実行:  uv run pytest tests/test_e2e.py -q        （スクリーンショットと各段の時間は tests/out に出る）
"""
import json
import math
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import ezdxf
import pytest

pw_api = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tests" / "out"
PHOTO = ROOT / "tests" / "data" / "scrap_sample_01.png"
PARTS = ROOT / "tests" / "data" / "parts"
PORT = 8791
BASE = f"http://127.0.0.1:{PORT}/"
TIMES = {}          # 各段の所要時間 [秒]（遅さの目安。tests/out/timings.json に書く）

# テスト用の写真は公開リポジトリに入れていない。無ければこのファイルの検査は飛ばす
pytestmark = pytest.mark.skipif(not PHOTO.exists(), reason="tests/data/scrap_sample_01.png がありません")


def _start_server(site: Path):
    """サイトを組み立てて、手元で配信する（python -m scrapnest と同じ）。"""
    log = open(site.parent / "server.log", "a", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "scrapnest", "--port", str(PORT), "--no-browser", "--site", str(site)],
                            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(120):
        try:
            urllib.request.urlopen(BASE + "version.json", timeout=1)
            return proc
        except Exception:
            if proc.poll() is not None:
                raise RuntimeError("配信を始められません: " + (site.parent / "server.log").read_text(encoding="utf-8", errors="replace")[-2000:])
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError("配信を始められません")


@pytest.fixture(scope="module")
def home(tmp_path_factory):
    import make_parts
    make_parts.ensure()
    OUT.mkdir(exist_ok=True)
    return tmp_path_factory.mktemp("home")


@pytest.fixture(scope="module")
def server(home):
    proc = _start_server(home / "site")
    yield proc
    proc.terminate()
    try:
        proc.wait(8)
    except Exception:
        proc.kill()
    (OUT / "timings.json").write_text(json.dumps({k: round(v, 1) for k, v in TIMES.items()}, ensure_ascii=False, indent=2),
                                      encoding="utf-8")


def wait_ready(pg, timeout=240_000):
    """ブラウザ内の Python の読み込みが終わるまで待つ。"""
    pg.wait_for_function("document.querySelector('#version').textContent.startsWith('v')", timeout=timeout)
    pg.wait_for_selector("#busy", state="hidden", timeout=timeout)


@pytest.fixture(scope="module")
def page(server):
    with pw_api.sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1500, "height": 950}, accept_downloads=True)
        # 保存の画面（showSaveFilePicker）は自動で操作できないので、ダウンロードで受け取る
        ctx.add_init_script("delete window.showSaveFilePicker")
        pg = ctx.new_page()
        pg.errors = []
        pg.on("console", lambda m: pg.errors.append(f"[{m.type}] {m.text}") if m.type == "error" else None)
        pg.on("pageerror", lambda e: pg.errors.append(f"[pageerror] {e}"))
        pg.on("requestfailed", lambda r: pg.errors.append(f"[requestfailed] {r.method} {r.url}"))
        pg.on("dialog", lambda d: d.accept())
        t = time.time()
        pg.goto(BASE)
        wait_ready(pg)
        TIMES["起動（Python の読み込み）"] = time.time() - t
        yield pg
        browser.close()


# ---- 小道具 ----
JS_STATE = "async () => { const m = await import('./js/ctx.js'); return JSON.parse(JSON.stringify({outline: m.S.outline, step: m.S.step, result: m.S.result, parts: m.S.parts.map(p => ({id: p.id, name: p.name, qty: p.qty, no_flip: p.no_flip})), photo: m.S.photo && {ok: m.S.photo.ok, grid: m.S.photo.grid && {rms: m.S.photo.grid.rms_mm, tilt: m.S.photo.grid.tilt_deg}, n: m.S.photo.candidates.length}})); }"
JS_CLIENT = """async ([x, y]) => { const m = await import('./js/ctx.js'); const vp = m.vp; const r = vp.svg.getBoundingClientRect();
  return [r.left + vp.tx + vp.s * x, r.top + vp.ty + (vp.flipY ? -vp.s : vp.s) * y]; }"""


def state(pg):
    return pg.evaluate(JS_STATE)


def client(pg, x, y):
    return pg.evaluate(JS_CLIENT, [x, y])


def click_w(pg, x, y, **kw):
    cx, cy = client(pg, x, y)
    pg.mouse.click(cx, cy, **kw)


def drag_w(pg, a, b, steps=6):
    ax, ay = client(pg, *a)
    bx, by = client(pg, *b)
    pg.mouse.move(ax, ay)
    pg.mouse.down()
    pg.mouse.move(bx, by, steps=steps)
    pg.mouse.up()


def outline_info(pg):
    """ブラウザ内の Python に計算してもらった面積など（保存が済むまで少し待つ）。"""
    pg.wait_for_timeout(700)
    return pg.evaluate("""async () => { const m = await import('./js/ctx.js'); const a = await import('./js/api.js');
        return await a.api('POST', '/api/outline', {shape: m.S.outline}); }""")


def n_vertices(shape):
    return len(shape["outer"]) + sum(len(h) for h in shape["holes"])


def shot(pg, name):
    pg.screenshot(path=str(OUT / f"{name}.png"))


# ---- 検査 ----
def test_1_photo(page):
    t = time.time()
    page.set_input_files("#file-photo", str(PHOTO))
    page.wait_for_selector("#busy", state="visible")
    page.wait_for_selector("#busy", state="hidden", timeout=180_000)
    TIMES["写真の解析"] = time.time() - t
    st = state(page)
    assert st["photo"]["ok"]
    assert st["photo"]["grid"]["rms"] < 0.3, "格子の当てはめ誤差が大きい"
    assert st["photo"]["n"] >= 1
    assert page.locator(".photo-grid line").count() > 50
    shot(page, "1_photo")
    page.wait_for_function("document.querySelector('#layer-image image') && document.querySelector('#layer-image image').getAttribute('href')")
    assert page.get_attribute("#layer-image image", "href").startswith("blob:")      # 写真は作業スレッドから受け取る
    page.click("#btn-photo-next")
    page.wait_for_selector(".h-vertex")
    assert state(page)["step"] == "outline"
    shot(page, "2_outline")


def test_2_outline_edit(page):
    # 板厚を 0 にして検出し直すと、視差の補正がなくなるぶん輪郭が大きくなる。2 mm に戻すと元に戻る
    a_thick = outline_info(page)["info"]["area"]
    for value, check in (("0", lambda a: a > a_thick + 100), ("2", lambda a: abs(a - a_thick) < 30)):
        page.fill("#in-thickness", value)
        page.click("#btn-redetect")
        page.wait_for_selector("#busy", state="visible")
        page.wait_for_selector("#busy", state="hidden", timeout=120_000)
        assert check(outline_info(page)["info"]["area"]), f"板厚 {value} mm で検出し直した結果がおかしい"

    s0 = state(page)["outline"]
    a0 = outline_info(page)
    assert not a0["problems"]
    assert 15000 < a0["info"]["area"] < 25000          # 写真の板はおよそ 200 cm2

    # 多角形を引く（左下に写り込んだペンチのあたり）
    page.click('#tools [data-tool="polygon"]')
    page.click('#boolmode [data-op="sub"]')
    for p in [(5, -8), (80, -8), (80, 8), (45, 20), (5, 20)]:
        click_w(page, *p)
        page.wait_for_timeout(60)
    page.keyboard.press("Enter")
    page.wait_for_timeout(600)
    a1 = outline_info(page)
    assert not a1["problems"]
    assert a1["info"]["area"] < a0["info"]["area"] - 100, "引いたのに面積が減っていない"
    shot(page, "2_outline_sub")

    # 円を足す
    page.click('#boolmode [data-op="add"]')
    page.click('#tools [data-tool="circle"]')
    drag_w(page, (205, 316), (205, 336))        # 板の上の縁に中心を置くので、上半分がはみ出して足される
    page.wait_for_timeout(600)
    a2 = outline_info(page)
    assert not a2["problems"]
    assert a2["info"]["area"] > a1["info"]["area"] + 200, "足したのに面積が増えていない"
    assert any(abs(p[2]) > 0.05 for p in state(page)["outline"]["outer"]), "円弧が残っていない"

    # 矩形を引いて穴を作る → 取り消し
    page.click('#boolmode [data-op="sub"]')
    page.click('#tools [data-tool="rect"]')
    drag_w(page, (195, 280), (205, 290))
    page.wait_for_timeout(600)
    assert len(state(page)["outline"]["holes"]) == 1, "内側を引いたら穴になるはず"
    page.click("#btn-undo")
    page.wait_for_timeout(200)
    assert len(state(page)["outline"]["holes"]) == 0

    # 元に戻す ×2 で最初の形、やり直す ×2 で戻る
    page.click("#btn-undo"); page.click("#btn-undo")
    page.wait_for_timeout(200)
    assert state(page)["outline"] == s0
    page.click("#btn-redo"); page.click("#btn-redo")
    assert abs(outline_info(page)["info"]["area"] - a2["info"]["area"]) < 1.0

    # 選択ツール: 頂点をドラッグ
    page.click('#tools [data-tool="select"]')
    page.click('#boolmode [data-op="add"]')
    sh = state(page)["outline"]
    # いちばん上の頂点を 5 mm 上へ
    i = max(range(len(sh["outer"])), key=lambda k: sh["outer"][k][1])
    v = sh["outer"][i]
    drag_w(page, (v[0], v[1]), (v[0], v[1] + 5))
    page.wait_for_timeout(200)
    v2 = state(page)["outline"]["outer"][i]
    assert abs(v2[1] - (v[1] + 5)) < 0.6 and abs(v2[0] - v[0]) < 0.6, "頂点が動いていない"
    page.keyboard.press("Control+z")
    page.wait_for_timeout(150)
    assert state(page)["outline"]["outer"][i][:2] == v[:2]

    # 辺をダブルクリックで頂点追加 → Delete で削除
    n = n_vertices(state(page)["outline"])
    sh = state(page)["outline"]
    # いちばん長い直線の辺の 1/3 の位置
    def length(k):
        a, b = sh["outer"][k], sh["outer"][(k + 1) % len(sh["outer"])]
        return math.hypot(b[0] - a[0], b[1] - a[1]) if abs(a[2]) < 1e-9 else 0.0
    k = max(range(len(sh["outer"])), key=length)
    a, b = sh["outer"][k], sh["outer"][(k + 1) % len(sh["outer"])]
    q = (a[0] + (b[0] - a[0]) / 3, a[1] + (b[1] - a[1]) / 3)
    cx, cy = client(page, *q)
    page.mouse.dblclick(cx, cy)
    page.wait_for_timeout(200)
    assert n_vertices(state(page)["outline"]) == n + 1, "ダブルクリックで頂点が増えていない"
    page.keyboard.press("Delete")
    page.wait_for_timeout(200)
    assert n_vertices(state(page)["outline"]) == n

    # 辺の中点をドラッグして円弧にする
    mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    L = math.hypot(b[0] - a[0], b[1] - a[1])
    nx, ny = -(b[1] - a[1]) / L, (b[0] - a[0]) / L
    drag_w(page, mid, (mid[0] + nx * 6, mid[1] + ny * 6))
    page.wait_for_timeout(200)
    bulge = state(page)["outline"]["outer"][k][2]
    assert abs(abs(bulge) - 6 / (L / 2)) < 0.05, f"中点のドラッグで円弧になっていない: {bulge}"
    page.keyboard.press("Control+z")

    # 測る
    page.click('#tools [data-tool="measure"]')
    click_w(page, 100, 100)
    click_w(page, 130, 140)
    page.wait_for_timeout(150)
    lab = page.locator("#layer-labels text").last
    assert "50.00 mm" in lab.text_content()
    # 文字のふちどりは拡大しても太らない（以前は図と一緒に拡大されて黒くつぶれた）
    page.mouse.move(*client(page, 115, 120))
    for _ in range(8):
        page.mouse.wheel(0, -400)
    page.wait_for_timeout(200)
    box = page.locator("#layer-labels text").last.bounding_box()
    assert box and box["height"] < 40 and box["width"] < 160, box
    page.click("#btn-fit")
    page.wait_for_timeout(150)
    page.keyboard.press("Escape")
    page.click('#tools [data-tool="select"]')
    assert not outline_info(page)["problems"]
    shot(page, "2_outline_edited")
    assert not page.errors, page.errors


def test_3_parts(page):
    page.click("#btn-outline-next")
    page.wait_for_selector("#parts-board:not([hidden])")
    page.set_input_files("#file-parts", [str(p) for p in sorted(PARTS.glob("*.dxf"))])
    page.wait_for_selector(".part-card")
    page.wait_for_timeout(400)
    cards = page.locator(".part-card")
    assert cards.count() == 6
    qty = {"bracket_L.dxf": 3, "gusset.dxf": 3, "flange.dxf": 1, "slot_plate.dxf": 1, "cover_cm.dxf": 1, "mirrored_arc.dxf": 2}
    for i in range(cards.count()):
        name = cards.nth(i).locator(".name").inner_text()
        box = cards.nth(i).locator(".qty")
        box.fill(str(qty[name]))
        box.dispatch_event("change")
        if name == "bracket_L.dxf":
            cards.nth(i).locator(".noflip").check()
        page.wait_for_timeout(120)
    page.wait_for_timeout(400)
    st = state(page)
    assert {p["name"]: p["qty"] for p in st["parts"]} == qty
    assert [p["no_flip"] for p in st["parts"] if p["name"] == "bracket_L.dxf"] == [True]
    assert "cm" in cards.filter(has_text="cover_cm.dxf").locator(".warn").inner_text()
    shot(page, "3_parts")


def test_4_nest(page):
    page.click("#btn-parts-next")
    page.wait_for_selector("#btn-nest-start")
    page.select_option("#in-time", "10")
    t = time.time()
    page.click("#btn-nest-start")
    page.wait_for_function("document.querySelector('#btn-nest-start').disabled === true")
    # 計算中も数値とグラフが出る
    page.wait_for_selector("#nest-chart svg", timeout=120_000)
    running = page.inner_text("#nest-metrics")
    assert "試した置き方" in running and "作業スレッド" in running, running
    shot(page, "4_nest_running")
    page.wait_for_function("document.querySelector('#btn-nest-start').disabled === false", timeout=240_000)
    TIMES["敷き詰め（計算 10 秒の設定）"] = time.time() - t
    page.wait_for_timeout(500)
    st = state(page)
    res = st["result"]
    assert res and res["placed"] >= 3, "ほとんど置けていない"
    assert page.locator("#layer-shape g.placed").count() == res["placed"]
    bracket = [p["id"] for p in st["parts"] if p["name"] == "bracket_L.dxf"][0]
    assert not any(pl["flip"] for pl in res["placements"] if pl["part"] == bracket), "反転禁止の部材が反転している"
    for pid, (got, want) in res["counts"].items():
        assert got <= want
    # 最適化の数値: 試した数・速さ・スレッド数・推移・実測の間隔と余白（設定以上）
    m = res["metrics"]
    assert m["runs"] >= m["workers"] >= 1 and m["runs_per_sec"] > 0 and 9.0 <= m["elapsed"] <= 25.0
    assert m["history"] and m["history"][-1]["placed"] <= res["placed"] + res["dropped"]
    assert all(b["utilization"] >= a["utilization"] for a, b in zip(m["history"], m["history"][1:])), "推移は良くなる一方のはず"
    assert m["min_spacing"] is None or m["min_spacing"] >= res["spacing"] - 1e-3
    assert m["min_margin"] >= res["margin"] - 1e-3
    text = page.inner_text("#nest-metrics")
    for label in ("経過時間", "試した置き方", "作業スレッド", "利用率", "置けた数", "残りの最大のかたまり", "最小の間隔（実測）", "最小の余白（実測）"):
        assert label in text, label
    assert page.locator("#nest-chart svg circle").count() == len(m["history"])
    page.click("#nest-history summary")
    assert page.locator("#nest-history-table tbody tr").count() == len(m["history"])
    TIMES["1 秒あたりの試行数"] = m["runs_per_sec"]
    TIMES["作業スレッドの数"] = m["workers"]
    shot(page, "4_nest")
    assert not page.errors, page.errors


def save_dxf(page, home, name):
    page.fill("#in-filename", name)
    with page.expect_download(timeout=60_000) as dl:
        page.click("#btn-export")
    d = dl.value
    out = home / "out" / d.suggested_filename
    out.parent.mkdir(exist_ok=True)
    d.save_as(out)
    page.wait_for_selector("#export-result .msg.ok", timeout=30_000)
    return out


def test_5_export(page, home):
    page.click("#btn-nest-next")
    page.wait_for_selector("#btn-export")
    # 最初は左上の角が原点: 全体が X のプラス側・Y のマイナス側に入る
    info0 = page.inner_text("#export-frame")
    assert "X 0.0 〜" in info0 and "〜 0.0 mm" in info0, info0
    assert page.locator('[data-corner="tl"].active').count() == 1
    # 向きを 90° 回して、原点を左下の角へ
    page.click("#btn-rot-p90")
    page.click('[data-corner="bl"]')
    out = save_dxf(page, home, "result")          # 拡張子は付けてくれる
    assert out.name == "result.dxf"
    shot(page, "5_export")
    assert "ダウンロードしました" in page.inner_text("#export-result")

    st = state(page)
    doc = ezdxf.readfile(str(out))
    msp = doc.modelspace()
    assert doc.units == ezdxf.units.MM and not doc.audit().errors
    scrap = [e for e in msp if e.dxf.layer == "SCRAP_OUTLINE"]
    assert len(scrap) == 1 + len(st["outline"]["holes"])
    # 輪郭は、線そのものに色が付いている（レイヤーを見ない加工ソフトでも部材と区別できる）
    assert all(e.dxf.color == 5 for e in scrap)
    assert all(e.dxf.get("color", 256) != 5 for e in msp if e.dxf.layer != "SCRAP_OUTLINE")
    for e in msp:
        if e.dxf.hasattr("extrusion"):
            assert tuple(round(v, 9) for v in e.dxf.extrusion) == (0, 0, 1)
    # 「原点を左下の角へ」なので、すべて X・Y のプラス側に入っている
    from ezdxf import bbox
    ext = bbox.extents(msp)
    assert ext.extmin.x > -0.01 and ext.extmin.y > -0.01
    assert tuple(doc.header["$EXTMIN"])[:2] == pytest.approx((ext.extmin.x, ext.extmin.y), abs=1e-6)
    # 90° 回したので、縦横が入れ替わっている（元の輪郭は縦長）
    info = outline_info(page)["info"]
    assert ext.size.x == pytest.approx(info["height"], abs=0.2) and ext.size.y == pytest.approx(info["width"], abs=0.2)
    # 部材の要素数: 置いた部材の要素の合計と一致
    from scrapnest.dxfio import reader
    n_ent = {p.name: len(reader.load_part(str(p), p.name).entities) for p in PARTS.glob("*.dxf")}
    id2name = {p["id"]: p["name"] for p in st["parts"]}
    expect = sum(n_ent[id2name[pl["part"]]] for pl in st["result"]["placements"])
    assert len(msp) - len(scrap) == expect

    # 輪郭を入れずに、別の名前でもう一度
    page.uncheck("#in-include-scrap")
    out2 = save_dxf(page, home, "parts_only.dxf")
    assert not [e for e in ezdxf.readfile(str(out2)).modelspace() if e.dxf.layer == "SCRAP_OUTLINE"]
    page.check("#in-include-scrap")
    assert not page.errors, page.errors


def test_6_reload_restores(page):
    """ページを開き直しても、続きから再開できる（作業はブラウザ内の保存領域に残っている）。"""
    before = state(page)
    t = time.time()
    page.reload()
    wait_ready(page)
    page.wait_for_selector(".h-vertex", timeout=60_000)
    TIMES["開き直し（2 回目の読み込み）"] = time.time() - t
    after = state(page)
    assert after["step"] == "outline"
    assert after["outline"] == before["outline"]
    assert after["parts"] == before["parts"]
    assert after["result"] and after["result"]["placements"] == before["result"]["placements"]
    assert after["result"]["metrics"]["history"] == before["result"]["metrics"]["history"]
    page.wait_for_function("document.querySelector('#layer-image image').getAttribute('href')")
    assert page.locator("#layer-image image").count() == 1
    # 2 回目からは、読み込んだファイルをブラウザに残してある（service worker）
    cached = page.evaluate("async () => (await caches.keys()).sort()")
    assert any(k.startswith("scrapnest-pyodide-") for k in cached) and "scrapnest-site" in cached, cached
    assert not page.errors, page.errors


def test_8_reset_keeps_or_clears_parts(page):
    """「最初から」で部材を残すか消すかを選べて、画面とブラウザ内の Python で食い違わない。"""
    n_before = len(state(page)["parts"])
    assert n_before == 6
    server_parts = lambda: page.evaluate("async () => (await (await import('./js/api.js')).api('GET', '/api/state')).parts.length")
    page.click("#btn-reset")
    page.wait_for_selector("#dlg-reset[open]")
    page.click("#btn-reset-keep")
    page.wait_for_timeout(500)
    st = state(page)
    assert st["step"] == "photo" and st["photo"] is None and st["outline"] is None
    assert len(st["parts"]) == n_before == server_parts(), "部材は残る（画面にも Python 側にも）"
    page.click("#btn-reset")
    page.wait_for_selector("#dlg-reset[open]")
    page.click("#btn-reset-all")
    page.wait_for_timeout(500)
    assert len(state(page)["parts"]) == 0 == server_parts(), "部材も消える"
    page.reload()
    wait_ready(page)
    assert len(state(page)["parts"]) == 0 and state(page)["photo"] is None, "開き直しても消えたまま"
    assert not page.errors, page.errors
