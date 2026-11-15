"""画面からの頼みごとを受ける入口（ブラウザ内の Python から直接呼ばれる）。

画面（JavaScript）は今まで通り api("POST", "/api/photo", ...) のように URL で頼み、作業スレッドがここの handle() を呼ぶ。
サーバーは無く、HTTP も使わない。URL は頼みごとの名前として使っているだけ。

戻り値は Response（status, JSON の文字列, バイナリ）。エラーは {"detail": 文言, "code": 種類} の JSON で返す。
"""
from __future__ import annotations

import json
import re
import traceback
from dataclasses import dataclass

from . import jobs
from .dxfio import reader
from .geom import outline as go
from .vision import grid as vgrid


@dataclass
class Response:
    status: int = 200
    json: str | None = None                # JSON の文字列
    data: bytes | None = None              # 画像や DXF の中身
    content_type: str | None = None


class ApiError(Exception):
    def __init__(self, msg: str, status: int = 400, code: str | None = None):
        super().__init__(msg)
        self.status, self.code = status, code


_job: jobs.Job | None = None


def init(restore: bool = True) -> None:
    """作業を読み込む（ブラウザでは IndexedDB から戻したファイルを読む）。"""
    global _job
    _job = jobs.Job.restore() if restore else jobs.Job()


def job() -> jobs.Job:
    if _job is None:
        init()
    return _job


def _ok(obj) -> Response:
    return Response(200, json.dumps(obj, ensure_ascii=False))


def _need_photo():
    if job().image is None:
        raise ApiError("写真がありません", 404)


# ---- 各頼みごと ----------------------------------------------------------------------

def state(body, files):
    return job().state()


def reset(body, files):
    job().reset(keep_parts=bool((body or {}).get("keep_parts", False)))
    return job().state()


def settings(body, files):
    j = job()
    for k, v in (body or {}).items():
        if k in jobs.DEFAULT_SETTINGS:
            j.settings[k] = v
    jobs.save_settings(j.settings)
    return j.settings


def photo(body, files):
    if not files or not files[0][1]:
        raise ApiError("ファイルが空です")
    try:
        return job().set_photo(files[0][1])
    except jobs.UnsupportedImage as ex:
        raise ApiError(str(ex), 415, "unsupported_image") from ex
    except Exception as ex:
        raise ApiError(f"写真を読めません: {ex}") from ex


def photo_preview(body, files):
    _need_photo()
    return Response(200, data=job().photo_preview(), content_type="image/jpeg")


def photo_rect(body, files):
    if job().rect_jpeg is None:
        raise ApiError("補正画像がありません", 404)
    return Response(200, data=job().rect_jpeg, content_type="image/jpeg")


def calibrate(body, files):
    try:
        return job().calibrate(body["points"], float(body["cells_u"]), float(body["cells_v"]))
    except (vgrid.GridNotFound, ValueError, KeyError) as ex:
        raise ApiError(str(ex)) from ex


def detect(body, files):
    body = body or {}
    try:
        cands = job().detect(body.get("thickness_mm"), body.get("sensitivity"))
    except (vgrid.GridNotFound, ValueError) as ex:
        raise ApiError(str(ex)) from ex
    return {"candidates": cands, "rect": job()._rect_info()}


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


def outline(body, files):
    shape = (body or {}).get("shape")
    if shape is None:
        job().set_outline(None)
        return {"ok": True}
    return _describe(shape, save=True)


def outline_boolean(body, files):
    try:
        if body.get("shape") is None:        # 輪郭がまだ無いときは、足した図形がそのまま輪郭になる
            if body["op"] != "add":
                raise ApiError("輪郭がありません")
            loop = go.primitive_loop(body["prim"])
            shape, dropped = go.normalize_shape({"outer": loop, "holes": []}), 0
        else:
            shape, dropped = go.boolean(body["shape"], body["prim"], body["op"])
    except (ValueError, KeyError) as ex:
        raise ApiError(str(ex)) from ex
    out = _describe(shape, save=False)
    out["dropped"] = dropped
    return out


def parts_list(body, files):
    return [e.to_json() for e in job().parts]


def parts_add(body, files):
    added, errors = [], []
    for name, data in files or []:
        name = re.split(r"[\\/]", name or "part.dxf")[-1] or "part.dxf"
        try:
            added.append(job().add_part(name, data).to_json())
        except reader.DxfError as ex:
            errors.append({"name": name, "error": str(ex)})
        except Exception as ex:
            errors.append({"name": name, "error": f"読み込みに失敗しました: {ex}"})
    return {"added": added, "errors": errors}


def parts_update(pid, body, files):
    try:
        return job().update_part(pid, **{k: (body or {}).get(k) for k in ("qty", "no_flip", "rotation", "unit")}).to_json()
    except KeyError as ex:
        raise ApiError("その部材はありません", 404) from ex
    except reader.DxfError as ex:
        raise ApiError(str(ex)) from ex


def parts_delete(pid, body, files):
    job().remove_part(pid)
    return {"ok": True}


def nest_prepare(body, files):
    try:
        meta, payload = job().nest_prepare(body["shape"], body.get("params", {}))
    except (ValueError, KeyError) as ex:
        raise ApiError(str(ex)) from ex
    return Response(200, json.dumps(meta, ensure_ascii=False), payload, "application/octet-stream")


def nest_finish(body, files):
    try:
        return job().nest_finish((body or {}).get("best"), (body or {}).get("stats"))
    except ValueError as ex:
        raise ApiError(str(ex)) from ex


def export(body, files):
    try:
        meta, data = job().export(body or {})
    except ValueError as ex:
        raise ApiError(str(ex)) from ex
    return Response(200, json.dumps(meta, ensure_ascii=False), data, "application/dxf")


ROUTES = {
    ("GET", "/api/state"): state,
    ("POST", "/api/reset"): reset,
    ("POST", "/api/settings"): settings,
    ("POST", "/api/photo"): photo,
    ("GET", "/api/photo/preview.jpg"): photo_preview,
    ("GET", "/api/photo/rect.jpg"): photo_rect,
    ("POST", "/api/calibrate"): calibrate,
    ("POST", "/api/detect"): detect,
    ("POST", "/api/outline"): outline,
    ("POST", "/api/outline/boolean"): outline_boolean,
    ("GET", "/api/parts"): parts_list,
    ("POST", "/api/parts"): parts_add,
    ("POST", "/api/nest/prepare"): nest_prepare,
    ("POST", "/api/nest/finish"): nest_finish,
    ("POST", "/api/export"): export,
}
ROUTES_WITH_ID = {
    ("PATCH", "/api/parts/"): parts_update,
    ("DELETE", "/api/parts/"): parts_delete,
}


def handle(method: str, path: str, body=None, files=None) -> Response:
    """頼みごと 1 つを処理する。path の ? 以降（画像の版など）は無視する。

    body: JSON にできる値（または JSON の文字列）。files: [(ファイル名, bytes), ...]
    """
    method = method.upper()
    path = path.split("?", 1)[0]
    if isinstance(body, str):
        body = json.loads(body) if body else None
    try:
        fn = ROUTES.get((method, path))
        if fn is not None:
            out = fn(body, files)
        else:
            m = re.fullmatch(r"(/api/parts/)(\d+)", path)
            fn = m and ROUTES_WITH_ID.get((method, m.group(1)))
            if not fn:
                raise ApiError(f"そのような操作はありません: {method} {path}", 404)
            out = fn(int(m.group(2)), body, files)
        return out if isinstance(out, Response) else _ok(out)
    except ApiError as ex:
        return Response(ex.status, json.dumps({"detail": str(ex), "code": ex.code}, ensure_ascii=False))
    except Exception as ex:
        traceback.print_exc()
        return Response(500, json.dumps({"detail": f"内部エラー: {ex}", "code": "internal"}, ensure_ascii=False))


MUTATING = {"POST", "PATCH", "DELETE"}
