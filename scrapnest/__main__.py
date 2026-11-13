"""ScrapNest を起動する:  uv run python -m scrapnest"""
from __future__ import annotations

import argparse
import multiprocessing
import socket
import threading
import webbrowser


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
    ap = argparse.ArgumentParser(description="端材ネスティング ScrapNest")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="ブラウザを自動で開かない")
    ap.add_argument("--fresh", action="store_true", help="前回の作業を読み込まずに始める")
    args = ap.parse_args()

    import uvicorn

    from .server import create_app

    port = _pick_port(args.port)
    url = f"http://127.0.0.1:{port}/"
    print(f"ScrapNest: {url}  （終了は Ctrl+C）")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(restore=not args.fresh), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
