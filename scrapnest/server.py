"""ブラウザ画面のためのサーバー（このPCの中だけで待ち受ける）。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import jobs, mobile, update
from .dxfio import reader, writer
from .geom import outline as go
from .vision import grid as vgrid

STATIC = Path(__file__).resolve().parent / "static"


def create_app(restore: bool = True) -> FastAPI:
    app = FastAPI(title="ScrapNest", docs_url=None, redoc_url=None)
    app.state.job = jobs.Job.restore() if restore else jobs.Job()
    app.state.mobile = mobile.MobileUpload()
    app.state.updates = update.UpdateCheck()

    def job() -> jobs.Job:
        return app.state.job

    def bad(msg: str, status: int = 400):
        raise HTTPException(status_code=status, detail=msg)

    # ---- 画面 ----
    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    @app.middleware("http")
    async def no_cache(request, call_next):
        resp = await call_next(request)
        if request.url.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-store"     # 手元で動かすアプリなので、常に最新のファイルを使う
        return resp

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/api/state")
    def state():
        return job().state()

    @app.post("/api/reset")
    def reset(req: dict = Body(default={})):
        job().reset(keep_parts=bool(req.get("keep_parts", False)))
        return job().state()

    @app.get("/api/version")
    def version():
        return app.state.updates.info()

    @app.post("/api/settings")
    def settings(values: dict = Body(...)):
        j = job()
        for k, v in values.items():
            if k in jobs.DEFAULT_SETTINGS:
                j.settings[k] = v
        jobs.save_settings(j.settings)
        return j.settings

    # ---- 写真 ----
    @app.post("/api/photo")
    def photo(file: UploadFile = File(...)):
        data = file.file.read()
        if not data:
            bad("ファイルが空です")
        try:
            return job().set_photo(data)
        except Exception as ex:
            bad(f"写真を読めません: {ex}")

    @app.post("/api/photo/inbox")
    def photo_from_inbox():
        data = app.state.mobile.take()
        if data is None:
            bad("スマホからの写真がまだ届いていません", 404)
        try:
            return job().set_photo(data)
        except Exception as ex:
            bad(f"写真を読めません: {ex}")

    @app.get("/api/photo/preview.jpg")
    def photo_preview():
        if job().image is None:
            bad("写真がありません", 404)
        return Response(job().photo_preview(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/photo/rect.jpg")
    def photo_rect():
        if job().rect_jpeg is None:
            bad("補正画像がありません", 404)
        return Response(job().rect_jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/api/calibrate")
    def calibrate(req: dict = Body(...)):
        try:
            return job().calibrate(req["points"], float(req["cells_u"]), float(req["cells_v"]))
        except (vgrid.GridNotFound, ValueError, KeyError) as ex:
            bad(str(ex))

    @app.post("/api/detect")
    def detect(req: dict = Body(...)):
        try:
            cands = job().detect(req.get("thickness_mm"), req.get("sensitivity"))
        except (vgrid.GridNotFound, ValueError) as ex:
            bad(str(ex))
        return {"candidates": cands, "rect": job()._rect_info()}

    # ---- 輪郭 ----
    @app.post("/api/outline")
    def set_outline(req: dict = Body(...)):
        shape = req.get("shape")
        if shape is None:
            job().set_outline(None)
            return {"ok": True}
        return _describe(shape, save=True)

    @app.post("/api/outline/boolean")
    def outline_boolean(req: dict = Body(...)):
        try:
            if req.get("shape") is None:        # 輪郭がまだ無いときは、足した図形がそのまま輪郭になる
                if req["op"] != "add":
                    bad("輪郭がありません")
                loop = go.primitive_loop(req["prim"])
                shape, dropped = go.normalize_shape({"outer": loop, "holes": []}), 0
            else:
                shape, dropped = go.boolean(req["shape"], req["prim"], req["op"])
        except (ValueError, KeyError) as ex:
            bad(str(ex))
        out = _describe(shape, save=False)
        out["dropped"] = dropped
        return out

    def _describe(shape: dict, save: bool) -> dict:
        try:
            shape = go.normalize_shape(shape)
            msgs = go.validate(shape)
            minx, miny, maxx, maxy = go.shape_to_polygon(shape, 0.02).bounds
            area = go.loop_area(shape["outer"]) + sum(go.loop_area(h) for h in shape["holes"])   # 穴は時計回りなので負
            info = {"area": round(area, 1), "width": round(maxx - minx, 2), "height": round(maxy - miny, 2),
                    "bounds": [round(v, 3) for v in (minx, miny, maxx, maxy)]}
        except Exception as ex:
            msgs, info = [f"輪郭を作れません: {ex}"], None
        shape = jobs._round_shape(shape)
        if save and not msgs:
            job().set_outline(shape)
        return {"shape": shape, "problems": msgs, "info": info}

    # ---- 部材 ----
    @app.get("/api/parts")
    def parts_list():
        return [e.to_json() for e in job().parts]

    @app.post("/api/parts")
    def parts_add(files: list[UploadFile] = File(...)):
        added, errors = [], []
        for f in files:
            name = Path(f.filename or "part.dxf").name
            try:
                added.append(job().add_part(name, f.file.read()).to_json())
            except reader.DxfError as ex:
                errors.append({"name": name, "error": str(ex)})
            except Exception as ex:
                errors.append({"name": name, "error": f"読み込みに失敗しました: {ex}"})
        return {"added": added, "errors": errors}

    @app.patch("/api/parts/{pid}")
    def parts_update(pid: int, req: dict = Body(...)):
        try:
            return job().update_part(pid, **{k: req.get(k) for k in ("qty", "no_flip", "rotation", "unit")}).to_json()
        except KeyError:
            bad("その部材はありません", 404)
        except reader.DxfError as ex:
            bad(str(ex))

    @app.delete("/api/parts/{pid}")
    def parts_delete(pid: int):
        job().remove_part(pid)
        return {"ok": True}

    # ---- 敷き詰め ----
    @app.post("/api/nest/start")
    def nest_start(req: dict = Body(...)):
        try:
            job().start_nest(req["shape"], req.get("params", {}))
        except (ValueError, KeyError) as ex:
            bad(str(ex))
        return {"ok": True}

    @app.get("/api/nest/status")
    def nest_status():
        return job().nest_status()

    @app.post("/api/nest/stop")
    def nest_stop():
        job().stop_nest()
        return {"ok": True}

    # ---- 出力 ----
    @app.post("/api/export")
    def export(req: dict = Body(...)):
        j = job()
        res = j.result
        if not res or not res.get("placements"):
            bad("敷き詰め結果がありません")
        path = str(req.get("path", "")).strip().strip('"')
        if not path:
            bad("出力先を指定してください")
        p = Path(os.path.expandvars(os.path.expanduser(path)))
        if p.is_dir():
            p = p / (j.settings.get("out_name") or "nest.dxf")
        if p.suffix.lower() != ".dxf":
            p = p.with_name(p.name + ".dxf")
        if not p.parent.is_dir():
            bad(f"フォルダーがありません: {p.parent}")
        if p.exists() and not req.get("overwrite"):
            return {"ok": False, "exists": True, "path": str(p)}
        index = {e.id: i for i, e in enumerate(j.parts)}
        missing = [pl["part"] for pl in res["placements"] if pl["part"] not in index]
        if missing:
            bad("敷き詰めのあとに部材が削除されています。敷き詰めをやり直してください")
        placements = [writer.Placement(part=index[pl["part"]], flip=pl["flip"], angle=pl["angle"], tx=pl["tx"], ty=pl["ty"])
                      for pl in res["placements"]]
        origin = req.get("origin") or [0.0, 0.0]
        include = bool(req.get("include_scrap", True))
        color = int(req.get("scrap_color") or j.settings.get("scrap_color", 5))
        if color not in writer.SCRAP_COLORS:
            color = 5
        try:
            info = writer.write_layout(str(p), [e.part for e in j.parts], placements,
                                       scrap_shape=j.outline if include else None, scrap_color=color,
                                       origin=(float(origin[0]), float(origin[1])), angle_deg=float(req.get("angle", 0.0)),
                                       version=j.settings.get("dxf_version", "R2010"))
        except OSError as ex:
            bad(f"書き込めません: {ex}")
        j.settings.update({"out_dir": str(p.parent), "out_name": p.name, "include_scrap": include, "scrap_color": color})
        warning = None
        if include:
            used = set()
            for i in {pl.part for pl in placements}:
                used |= writer.entity_colors(j.parts[i].part)
            if color in used:
                warning = ("部材の線にも端材の輪郭と同じ色が使われています。加工ソフトで色ごとに分けると、"
                           "部材の一部まで「加工なし」になってしまうので、輪郭の色を変えて出力し直してください。")
        jobs.save_settings(j.settings)
        return {"ok": True, "path": str(p), "entities": info["entities"], "extents": info["extents"], "placed": len(placements),
                "warning": warning}

    @app.post("/api/browse")
    def browse(req: dict = Body(default={})):
        """保存先を選ぶダイアログを、この PC の画面に出す。"""
        initial_dir = req.get("dir") or job().settings.get("out_dir") or str(Path.home())
        initial_file = req.get("name") or job().settings.get("out_name") or "nest.dxf"
        script = (
            "import sys, tkinter as tk\n"
            "from tkinter import filedialog\n"
            "root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)\n"
            "p = filedialog.asksaveasfilename(parent=root, title='出力先', defaultextension='.dxf', confirmoverwrite=False,\n"
            "    filetypes=[('DXF', '*.dxf')], initialdir=sys.argv[1], initialfile=sys.argv[2])\n"
            "sys.stdout.buffer.write(p.encode('utf-8'))\n"
        )
        if not Path(initial_dir).is_dir():
            initial_dir = str(Path.home())
        try:
            out = subprocess.run([sys.executable, "-c", script, initial_dir, initial_file], capture_output=True, timeout=600)
        except subprocess.TimeoutExpired:
            return {"path": None}
        path = out.stdout.decode("utf-8", errors="replace").strip()
        return {"path": path.replace("/", "\\") if path and os.name == "nt" else (path or None)}

    @app.post("/api/open-folder")
    def open_folder(req: dict = Body(...)):
        p = Path(str(req.get("path", "")))
        if not p.exists():
            bad("ファイルがありません", 404)
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", str(p)])
        return {"ok": True}

    # ---- スマホから送る ----
    @app.post("/api/mobile/start")
    def mobile_start():
        try:
            return app.state.mobile.start()
        except OSError as ex:
            bad(f"スマホ用の受け口を開けません: {ex}")

    @app.get("/api/mobile/poll")
    def mobile_poll():
        return {"waiting": app.state.mobile.pending()}

    @app.post("/api/mobile/stop")
    def mobile_stop():
        app.state.mobile.stop()
        return {"ok": True}

    @app.exception_handler(Exception)
    async def on_error(request, exc):
        import traceback
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"detail": f"内部エラー: {exc}"})

    return app
