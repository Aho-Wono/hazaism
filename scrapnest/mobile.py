"""スマホから写真を送るための受け口。

普段の画面は 127.0.0.1 だけで待ち受けているので、スマホからは見えない。「スマホから送る」を押したときだけ、
写真の受け取りしかできない小さなサーバーを LAN 向けに立てる。URL には使い捨ての合言葉（トークン）を入れる。
"""
from __future__ import annotations

import secrets
import socket
import threading
from pathlib import Path

import segno
import uvicorn
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse

STATIC = Path(__file__).resolve().parent / "static"
MAX_BYTES = 80 * 1024 * 1024


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))     # 実際には何も送らない。外向きに使われるアドレスを知るだけ
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("0.0.0.0", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class MobileUpload:
    def __init__(self):
        self.token: str | None = None
        self.url: str | None = None
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._inbox: bytes | None = None
        self._lock = threading.Lock()

    def start(self) -> dict:
        if self._server is None or not self._thread or not self._thread.is_alive():
            self.token = secrets.token_urlsafe(9)
            port = _free_port()
            app = self._app()
            self._server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning"))
            self._thread = threading.Thread(target=self._server.run, daemon=True)
            self._thread.start()
            self.url = f"http://{lan_ip()}:{port}/m/{self.token}"
        qr = segno.make(self.url, error="m")
        return {"url": self.url, "qr": qr.svg_inline(scale=6, border=2, dark="#111", light="#fff")}

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        self._server = None
        self.token = self.url = None

    def pending(self) -> bool:
        with self._lock:
            return self._inbox is not None

    def take(self) -> bytes | None:
        with self._lock:
            data, self._inbox = self._inbox, None
            return data

    def _app(self) -> FastAPI:
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        def check(token: str):
            if not self.token or not secrets.compare_digest(token, self.token):
                raise HTTPException(status_code=404)

        @app.get("/m/{token}", response_class=HTMLResponse)
        def page(token: str):
            check(token)
            return HTMLResponse((STATIC / "mobile.html").read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})

        @app.post("/m/{token}/upload")
        def upload(token: str, file: UploadFile = File(...)):
            check(token)
            data = file.file.read(MAX_BYTES + 1)
            if not data or len(data) > MAX_BYTES:
                raise HTTPException(status_code=400, detail="ファイルが空か、大きすぎます")
            with self._lock:
                self._inbox = data
            return {"ok": True}

        return app
