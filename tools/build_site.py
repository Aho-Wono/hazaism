"""公開するサイト（静的ファイルだけ）を site/ に組み立てる。

  site/
    index.html, css/, js/, sw.js    ← hazaism/static をそのまま
    py/hazaism.zip                 ← ブラウザ内の Python が読み込むプログラム（hazaism パッケージ）
    py/ezdxf-<版>-py3-none-any.whl  ← Pyodide に同梱されていないので一緒に配る（uv.lock と同じ版・同じハッシュ）
    version.json                     ← 版と、中身から作った build 番号（ブラウザに古いものを使わせないため）

usage: uv run python tools/build_site.py [出力先]
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import sys
import tomllib
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "hazaism"
STATIC = PKG / "static"
CACHE = ROOT / ".cache" / "wheels"


def project_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def locked_pure_wheel(name: str) -> tuple[str, str, str]:
    """uv.lock に書かれた、純 Python 版の wheel の (ファイル名, URL, sha256)。"""
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    for pkg in lock["package"]:
        if pkg["name"] != name:
            continue
        for w in pkg.get("wheels", []):
            url = w["url"]
            fname = url.rsplit("/", 1)[-1]
            if fname.endswith("-py3-none-any.whl"):
                return fname, url, w["hash"].split(":", 1)[1]
    raise SystemExit(f"uv.lock に {name} の純 Python 版の wheel がありません")


def fetch_wheel(name: str) -> Path:
    fname, url, sha = locked_pure_wheel(name)
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / fname
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
        print(f"download {fname}")
        with urllib.request.urlopen(url, timeout=60) as r:
            data = r.read()
        if hashlib.sha256(data).hexdigest() != sha:
            raise SystemExit(f"{fname} のハッシュが uv.lock と一致しません")
        path.write_bytes(data)
    return path


def package_zip() -> bytes:
    """hazaism パッケージの .py だけを zip にする（画面のファイルと __pycache__ は入れない）。日時は固定して、中身が同じなら同じ zip にする。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(PKG.rglob("*.py")):
            rel = p.relative_to(ROOT).as_posix()
            if "/static/" in rel or "__pycache__" in rel:
                continue
            info = zipfile.ZipInfo(rel, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, p.read_bytes().replace(b"\r\n", b"\n"))
    return buf.getvalue()


def build(out: Path) -> dict:
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(STATIC, out, ignore=shutil.ignore_patterns("__pycache__"))
    (out / "py").mkdir()
    zdata = package_zip()
    (out / "py" / "hazaism.zip").write_bytes(zdata)
    wheel = fetch_wheel("ezdxf")
    shutil.copy2(wheel, out / "py" / wheel.name)

    pyodide = re.search(r'PYODIDE_VERSION\s*=\s*"([^"]+)"', (STATIC / "js" / "pyconfig.js").read_text(encoding="utf-8")).group(1)
    sw = out / "sw.js"
    sw.write_text(sw.read_text(encoding="utf-8").replace("__PYODIDE__", pyodide), encoding="utf-8")
    # build 番号: 配るファイルすべての中身から作る（どれかが変われば変わる）
    h = hashlib.sha256()
    for p in sorted(out.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(out).as_posix().encode())
            h.update(p.read_bytes())
    info = {"version": project_version(), "build": h.hexdigest()[:12], "ezdxf_wheel": wheel.name, "pyodide": pyodide}
    (out / "version.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")     # GitHub Pages に、ファイルをそのまま配らせる
    return info


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "site"
    info = build(out)
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"built {out}  v{info['version']}  build {info['build']}  {size / 1e6:.1f} MB")
