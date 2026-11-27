"""開発用: サイトを組み立てて、この PC の中だけで配信する。  uv run python -m hazaism

利用者は公開されたページ（GitHub Pages）を開くだけなので、これは使わない。
"""
from __future__ import annotations

import argparse
import functools
import http.server
import socket
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".js": "text/javascript", ".mjs": "text/javascript", ".wasm": "application/wasm",
                      ".json": "application/json", ".whl": "application/zip", ".zip": "application/zip",
                      ".webmanifest": "application/manifest+json", ".svg": "image/svg+xml", ".md": "text/markdown; charset=utf-8"}

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")      # 開発中は常に最新のファイルを使う
        super().end_headers()

    def log_message(self, fmt, *args):
        pass


def _pick_port(preferred: int) -> int:
    for port in [preferred, *range(preferred + 1, preferred + 20)]:
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit("空いているポートがありません")


def main() -> None:
    ap = argparse.ArgumentParser(description="Hazaism（開発用のローカル配信）")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="ブラウザを自動で開かない")
    ap.add_argument("--site", type=Path, default=ROOT / "site", help="組み立て先")
    args = ap.parse_args()

    sys.path.insert(0, str(ROOT / "tools"))
    import build_site
    info = build_site.build(args.site)
    port = _pick_port(args.port)
    url = f"http://127.0.0.1:{port}/"
    print(f"Hazaism v{info['version']} ({info['build']}): {url}  （終了は Ctrl+C）", flush=True)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), functools.partial(Handler, directory=str(args.site)))
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
