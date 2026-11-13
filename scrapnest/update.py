"""いまの版と、新しい版が公開されているかの確認。

起動時に 1 回だけ、公開リポジトリの pyproject.toml を見に行く（失敗しても何も起きない）。
確認をやめたいときは環境変数 SCRAPNEST_NO_UPDATE_CHECK=1 を設定する。更新そのものは update.bat が行う。
"""
from __future__ import annotations

import os
import threading
import tomllib
import urllib.request
from pathlib import Path

REPO = "Aho-Wono/scrapnest"
BRANCH = "main"
ROOT = Path(__file__).resolve().parent.parent


def current_version() -> str:
    try:
        return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    except Exception:
        return "0"


def _key(v: str) -> tuple:
    out = []
    for part in v.split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def is_newer(latest: str | None, current: str) -> bool:
    return bool(latest) and _key(latest) > _key(current)


class UpdateCheck:
    def __init__(self):
        self.version = current_version()
        self.latest: str | None = None
        if not os.environ.get("SCRAPNEST_NO_UPDATE_CHECK"):
            threading.Thread(target=self._fetch, daemon=True).start()

    def _fetch(self) -> None:
        url = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/pyproject.toml"
        try:
            with urllib.request.urlopen(url, timeout=4) as r:
                self.latest = tomllib.loads(r.read().decode("utf-8"))["project"]["version"]
        except Exception:
            self.latest = None       # オフラインでも普通に使える

    def info(self) -> dict:
        return {"version": self.version, "latest": self.latest, "update": is_newer(self.latest, self.version)}
