"""更新履歴（CHANGELOG.md）の検査。

- 版を上げたら CHANGELOG.md にも書く（いまの版が先頭にあるか、書き方が画面の読み方に合っているか）
- サイトに入る（tools/build_site.py）
- 画面の読み方（hazaism/static/js/changelog.js）を Chromium で動かす（写真は使わないので GitHub の自動検査でも走る）
"""
import re
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "hazaism" / "static"
CHANGELOG = ROOT / "CHANGELOG.md"
ORIGIN = "http://hazaism.test/"
HEAD = re.compile(r"^##\s+v?(\d+(?:\.\d+)*)\s*[（(]\s*([^）)]*?)\s*[）)]\s*(.*)$")


def project_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def headings():
    return [m.groups() for line in CHANGELOG.read_text(encoding="utf-8").splitlines() if (m := HEAD.match(line))]


def vkey(v: str):
    return tuple(int(x) for x in v.split("."))


def test_changelog_lists_current_version_first():
    hs = headings()
    assert hs, "CHANGELOG.md に「## 版（日付）見出し」がありません"
    assert hs[0][0] == project_version(), "版を上げたら CHANGELOG.md の先頭に書いてください"
    versions = [h[0] for h in hs]
    assert versions == sorted(versions, key=vkey, reverse=True), "新しい版ほど上に"
    assert len(set(versions)) == len(versions)
    for v, date, title in hs:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", date), (v, date)
        assert title, v
    # 「## 」で始まる行は、すべて版の見出しの形（形が違うと画面では読まれない）
    for line in CHANGELOG.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            assert HEAD.match(line), line


def test_build_puts_changelog_in_site(tmp_path):
    sys.path.insert(0, str(ROOT / "tools"))
    import build_site
    out = tmp_path / "site"
    build_site.build(out)
    assert (out / "CHANGELOG.md").read_bytes() == CHANGELOG.read_bytes()


# ---- 画面の読み方（changelog.js） ----
pw_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def page():
    with pw_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as ex:            # Chromium が入っていない
            pytest.skip(f"Chromium を起動できません: {str(ex).splitlines()[0]}")

        def serve(route):
            path = route.request.url[len(ORIGIN):].split("?")[0]
            if path == "__test__.html":
                return route.fulfill(body="<!doctype html><meta charset=utf-8><title>changes</title><div id=toasts></div>"
                                          "<div id=box></div>", content_type="text/html")
            if path == "CHANGELOG.md":
                return route.fulfill(body=CHANGELOG.read_bytes(), content_type="text/markdown; charset=utf-8")
            f = (STATIC / path).resolve()
            if STATIC not in f.parents or not f.is_file():
                return route.fulfill(status=404, body="")
            ctype = "text/javascript" if f.suffix == ".js" else "application/octet-stream"
            return route.fulfill(body=f.read_bytes(), content_type=ctype)

        ctx = browser.new_context()
        ctx.route(ORIGIN + "**", serve)
        pg = ctx.new_page()
        pg.errors = []
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.goto(ORIGIN + "__test__.html")
        pg.evaluate("async () => { window.C = await import('./js/changelog.js'); }")
        yield pg
        browser.close()


def test_js_reads_the_real_changelog(page):
    entries = page.evaluate("async () => C.parseChangelog(await (await fetch('CHANGELOG.md')).text())")
    assert [(e["version"], e["date"], e["title"]) for e in entries] == headings()
    for e in entries:
        assert e["items"], e["version"]
        assert all(it["text"] and not it["text"].startswith("-") for it in e["items"]), e["version"]
    assert not page.errors, page.errors


def test_js_parser_details(page):
    md = "\n".join([
        "# 題",
        "前置きは読まない",
        "## 1.10.0 (2027-01-02) 半角のかっこ",
        "版の説明の",
        "続き",
        "",
        "- 一つ目の",
        "  続き",
        "  - 下の項目",
        "- two",
        "  words",
        "- リンク https://example.com/a?b=1 と <b>太字にしない</b>",
        "## 次の版（予定）",
        "- 読まない",
        "## 1.9.0（2026-12-31）",
        "* 星の印",
    ])
    entries = page.evaluate("(md) => C.parseChangelog(md)", md)
    assert [e["version"] for e in entries] == ["1.10.0", "1.9.0"]
    a, b = entries
    assert (a["date"], a["title"], a["notes"]) == ("2027-01-02", "半角のかっこ", ["版の説明の続き"])
    assert [it["text"] for it in a["items"]] == ["一つ目の続き", "two words", "リンク https://example.com/a?b=1 と <b>太字にしない</b>"]
    assert [s["text"] for s in a["items"][0]["sub"]] == ["下の項目"]
    assert b["title"] == "" and [it["text"] for it in b["items"]] == ["星の印"]
    assert page.evaluate("[C.compareVersions('0.10.0', '0.9.9') > 0, C.compareVersions('0.6', '0.6.0') === 0, C.compareVersions('0.5.0', '0.6.1') < 0]") == [True, True, True]

    # 表示: 前に見た版（1.8.0）より新しい版に印、いまの版（1.9.0）にも印。未配布の 1.10.0 には付けない。文は HTML として読まない
    html = page.evaluate("""(md) => { const box = document.getElementById('box');
        C.renderChangelog(box, C.parseChangelog(md), { seen: '1.8.0', current: '1.9.0' });
        return { n: box.querySelectorAll('.change').length, isNew: [...box.querySelectorAll('.change.is-new')].map(e => e.dataset.version),
                 now: [...box.querySelectorAll('.change.is-current')].map(e => e.dataset.version),
                 bold: box.querySelectorAll('b').length, link: box.querySelector('a').href, sub: box.querySelectorAll('li ul li').length }; }""", md)
    assert html == {"n": 2, "isNew": ["1.9.0"], "now": ["1.9.0"], "bold": 0, "link": "https://example.com/a?b=1", "sub": 1}
    assert not page.errors, page.errors
