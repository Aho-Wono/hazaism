"""写真の読み込みと、画面からの頼みごとの入口（webapi）の検査。"""
import io
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

DATA = Path(__file__).resolve().parent / "data"


@pytest.fixture()
def jobs(tmp_path, monkeypatch):
    """保存先を一時フォルダーに向けた jobs モジュール。"""
    from hazaism import jobs as j
    monkeypatch.setattr(j, "JOB_DIR", tmp_path / "jobs" / "last")
    monkeypatch.setattr(j, "CONFIG_PATH", tmp_path / "config.json")
    return j


def _jpeg(img: Image.Image, exif=None) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90, **({"exif": exif} if exif is not None else {}))
    return buf.getvalue()


def test_decode_respects_exif_orientation(jobs):
    im = Image.new("RGB", (80, 40), (0, 0, 0))
    im.paste((255, 0, 0), (0, 0, 20, 40))             # 左端が赤
    exif = Image.Exif()
    exif[0x0112] = 6                                   # 「右に 90° 回して表示」
    bgr, f = jobs.decode_photo(_jpeg(im, exif))
    assert bgr.shape[:2] == (80, 40)                   # 縦長になる
    assert bgr[5, 20, 2] > 200 and bgr[70, 20, 2] < 60  # 赤は上に来る
    assert f is None


def test_decode_reads_focal_length(jobs):
    im = Image.new("RGB", (400, 300), (10, 20, 30))
    exif = Image.Exif()
    exif.get_ifd(0x8769)[0xA405] = 26                  # 35 mm 換算の焦点距離
    _, f = jobs.decode_photo(_jpeg(im, exif))
    assert f == pytest.approx(26 / 43.27 * 500, rel=1e-3)


def test_decode_unsupported_image(jobs):
    """Pillow で読めない画像は UnsupportedImage（画面はブラウザで JPEG に直して送り直す）。"""
    with pytest.raises(jobs.UnsupportedImage):
        jobs.decode_photo(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64)


def test_settings_roundtrip(jobs):
    s = jobs.load_settings()
    assert s["pitch_mm"] == 10.0
    assert (s["spacing_mm"], s["margin_mm"], s["angle_step"], s["time_limit"]) == (0.5, 1.5, 2.0, 10.0)   # 敷き詰めの初期値
    s["spacing_mm"] = 1.5
    s["unknown_key"] = 1
    jobs.save_settings(s)
    saved = json.loads(jobs.CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["spacing_mm"] == 1.5 and "unknown_key" not in saved
    assert jobs.load_settings()["spacing_mm"] == 1.5


class Client:
    """webapi.handle を HTTP の頼みごとのように呼ぶための小道具。"""

    def __init__(self, webapi):
        self.w = webapi

    def _do(self, method, path, json_body=None, files=None):
        r = self.w.handle(method, path, json_body, files)
        r.body = json.loads(r.json) if r.json else None
        return r

    def get(self, path):
        return self._do("GET", path)

    def post(self, path, json=None, files=None):
        return self._do("POST", path, json, files)

    def patch(self, path, json=None):
        return self._do("PATCH", path, json)

    def delete(self, path):
        return self._do("DELETE", path)


@pytest.fixture()
def client(jobs):
    from hazaism import webapi
    webapi.init(restore=False)
    return Client(webapi)


def test_api_outline_boolean_and_validation(client):
    rect = {"type": "rect", "x": 0, "y": 0, "w": 100, "h": 60}
    r = client.post("/api/outline/boolean", json={"shape": None, "prim": rect, "op": "add"}).body
    assert r["info"]["area"] == pytest.approx(6000, abs=0.5) and not r["problems"]
    shape = r["shape"]
    # 円を引く → 穴になる。円弧のまま残る
    r = client.post("/api/outline/boolean", json={"shape": shape, "prim": {"type": "circle", "cx": 50, "cy": 30, "r": 10}, "op": "sub"}).body
    assert len(r["shape"]["holes"]) == 1
    assert all(abs(abs(b) - 1.0) < 1e-6 for _, _, b in r["shape"]["holes"][0])      # 半円 2 つ
    assert r["info"]["area"] == pytest.approx(6000 - np.pi * 100, abs=0.5)
    # 角に円を足す → 外周に円弧が 1 本入る
    r2 = client.post("/api/outline/boolean", json={"shape": shape, "prim": {"type": "circle", "cx": 100, "cy": 60, "r": 20}, "op": "add"}).body
    arcs = [p for p in r2["shape"]["outer"] if abs(p[2]) > 1e-6]
    assert 1 <= len(arcs) <= 2 and len(r2["shape"]["outer"]) <= 7
    assert r2["info"]["area"] == pytest.approx(6000 + 0.75 * np.pi * 400, abs=0.5)
    # 真っ二つにすると、大きい方だけ残る
    r3 = client.post("/api/outline/boolean", json={"shape": shape, "prim": {"type": "rect", "x": 60, "y": -5, "w": 5, "h": 70}, "op": "sub"}).body
    assert r3["dropped"] == 1 and r3["info"]["area"] == pytest.approx(3600, abs=0.5)
    # 自分自身と交差する輪郭は不具合として返る
    bow = {"outer": [[0, 0, 0], [10, 10, 0], [10, 0, 0], [0, 10, 0]], "holes": []}
    assert client.post("/api/outline", json={"shape": bow}).body["problems"]
    # 楕円は円弧の集まりになる（誤差 0.05 mm 以内）
    e = client.post("/api/outline/boolean", json={"shape": None, "prim": {"type": "ellipse", "cx": 0, "cy": 0, "rx": 40, "ry": 25}, "op": "add"}).body
    assert e["info"]["area"] == pytest.approx(np.pi * 40 * 25, rel=2e-3)
    assert len(e["shape"]["outer"]) <= 40


def test_api_parts_and_export_errors(client, tmp_path):
    import make_parts
    make_parts.ensure()
    data = (make_parts.OUT / "flange.dxf").read_bytes()
    r = client.post("/api/parts", files=[("C:\\x\\flange.dxf", data), ("bad.dxf", b"not a dxf")]).body
    assert [p["name"] for p in r["added"]] == ["flange.dxf"] and [e["name"] for e in r["errors"]] == ["bad.dxf"]
    pid = r["added"][0]["id"]
    assert r["added"][0]["width"] == pytest.approx(60, abs=0.1)
    up = client.patch(f"/api/parts/{pid}", json={"qty": 5, "no_flip": True, "rotation": "90", "unit": "cm"}).body
    assert (up["qty"], up["no_flip"], up["rotation"], up["unit"]) == (5, True, "90", "cm") and up["width"] == pytest.approx(600, abs=1)
    assert client.patch("/api/parts/999", json={"qty": 1}).status == 404
    # 敷き詰め結果がないうちは出力できない
    assert client.post("/api/export", json={"name": "x.dxf"}).status == 400
    assert client.post("/api/nest/finish", json={"best": None}).status == 400      # 準備の前に終わりは来ない
    assert client.get("/api/nope").status == 404
    assert client.delete(f"/api/parts/{pid}").body == {"ok": True}
    assert client.get("/api/parts").body == []


def test_api_photo_and_manual_calibration(client):
    """合成写真を読み込み、格子の交点 4 つを指定する手動校正でも同じ結果になること。"""
    import cv2

    import make_synthetic as syn
    photo, truth, cam = syn.make(thickness=0.0, tilt_deg=12.0, yaw_deg=4.0)
    data = cv2.imencode(".jpg", photo, [cv2.IMWRITE_JPEG_QUALITY, 92])[1].tobytes()
    info = client.post("/api/photo", files=[("p.jpg", data)]).body
    assert info["ok"] and not info["grid"]["manual"] and info["grid"]["rms_mm"] < 0.15
    assert len(info["candidates"]) == 1
    auto_area = info["candidates"][0]["area"]
    assert auto_area == pytest.approx(truth.area, rel=0.02)
    rect = client.get("/api/photo/rect.jpg")
    assert rect.status == 200 and rect.data[:2] == b"\xff\xd8" and rect.content_type == "image/jpeg"
    assert client.get("/api/photo/preview.jpg").data[:2] == b"\xff\xd8"
    bad = client.post("/api/photo", files=[("x.heic", b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64)])
    assert bad.status == 415 and bad.body["code"] == "unsupported_image"

    # 30 × 40 マスの四隅（左上 → 右上 → 右下 → 左下）
    pts = cam.project(np.array([[60.0, 100.0], [360.0, 100.0], [360.0, 500.0], [60.0, 500.0]])).tolist()
    info2 = client.post("/api/calibrate", json={"points": pts, "cells_u": 30, "cells_v": 40}).body
    assert info2["ok"] and info2["grid"]["manual"] and info2["grid"]["rms_mm"] < 0.15
    assert info2["candidates"][0]["area"] == pytest.approx(auto_area, rel=0.01)
    # 順番が逆（鏡像）なら断られる
    assert client.post("/api/calibrate", json={"points": pts[::-1], "cells_u": 30, "cells_v": 40}).status == 400

    # 板厚を変えて検出し直せる
    r = client.post("/api/detect", json={"thickness_mm": 0.0, "sensitivity": 0.5}).body
    assert len(r["candidates"]) == 1
    st = client.get("/api/state").body
    assert st["photo"]["ok"] and st["settings"]["thickness_mm"] == 0.0


def test_reset(client):
    import make_parts
    make_parts.ensure()
    data = (make_parts.OUT / "flange.dxf").read_bytes()
    client.post("/api/parts", files=[("flange.dxf", data)])
    assert len(client.post("/api/reset", json={"keep_parts": True}).body["parts"]) == 1
    assert client.post("/api/reset", json={"keep_parts": False}).body["parts"] == []
    assert client.get("/api/state").body["parts"] == []


def test_nest_prepare_finish_and_export(client):
    """敷き詰めの準備 → 作業スレッドで計算（ここでは手で置いた配置を使う）→ 検査して結果 → DXF の中身。"""
    import ezdxf

    import make_parts
    from hazaism.nest import engine
    make_parts.ensure()
    added = client.post("/api/parts", files=[("gusset.dxf", (make_parts.OUT / "gusset.dxf").read_bytes())]).body["added"]
    pid = added[0]["id"]
    client.patch(f"/api/parts/{pid}", json={"qty": 3})
    shape = {"outer": [[0, 0, 0], [160, 0, 0], [160, 100, 0], [0, 100, 0]], "holes": []}
    r = client.post("/api/nest/prepare", json={"shape": shape, "params": {"spacing_mm": 2, "margin_mm": 2, "angle_step": 90, "time_limit": 5}})
    assert r.status == 200 and r.content_type == "application/octet-stream" and len(r.data) > 100
    meta = r.body
    assert meta["qty"] == {str(pid): 3} and meta["scrap_area"] == pytest.approx(16000) and meta["time_limit"] == 5
    head = engine.decode_payload(r.data)                      # 作業スレッド（JavaScript）に渡すデータ
    assert head["parts"][0]["index"] == pid and head["parts"][0]["qty"] == 3 and head["orients"]
    assert all(o["angle"] % 90 == 0 for o in head["orients"])
    # 計算の結果の代わりに、間隔 5 mm・余白 5 mm で 3 個並べた配置（gusset は 45 × 40 mm）
    best = {"placements": [{"part": pid, "flip": False, "angle": 0, "tx": x, "ty": 5} for x in (5, 55, 105)],
            "placed_area": 0, "leftover": 1234.5, "key": [0, 3, 1235]}
    res = client.post("/api/nest/finish", json={"best": best, "stats": {"runs": 7, "elapsed": 2.0, "workers": 1, "prepare_s": 0.4,
                                                                       "history": [{"t": 0.5, "utilization": 10.0, "placed": 3}]}}).body
    assert res["placed"] == 3 and res["counts"] == {str(pid): [3, 3]} and res["dropped"] == 0
    m = res["metrics"]
    assert m["runs"] == 7 and m["runs_per_sec"] == pytest.approx(3.5) and m["workers"] == 1 and len(m["history"]) == 1
    assert m["prepare_s"] == 0.4 and m["verify_s"] >= 0 and m["leftover"] == 1234.5
    assert m["min_spacing"] >= 2.0 - 1e-3 and m["min_margin"] >= 2.0 - 1e-3
    assert client.get("/api/state").body["result"]["placed"] == 3        # 保存されている
    # DXF はファイルに書かず、中身を返す。ファイル名の使えない文字は置き換える
    ex = client.post("/api/export", json={"name": "a/b:c", "origin": [0, 0], "angle": 0, "include_scrap": True, "scrap_color": 1})
    assert ex.status == 200 and ex.body["name"] == "a_b_c.dxf" and ex.content_type == "application/dxf"
    doc = ezdxf.read(io.StringIO(ex.data.decode("utf-8"), newline=None))     # 改行は CRLF
    assert doc.units == ezdxf.units.MM
    assert [e.dxf.color for e in doc.modelspace() if e.dxf.layer == "SCRAP_OUTLINE"] == [1]
    assert client.get("/api/state").body["settings"]["out_name"] == "a_b_c.dxf"


def test_restore_keeps_parts_when_photo_is_broken(jobs):
    """前回の写真が壊れていても、登録した部材は残る。古い番号のファイルが残っていても正しい中身で読む。"""
    import make_parts
    make_parts.ensure()
    job = jobs.Job()
    pdir = jobs.JOB_DIR / "parts"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "1.dxf").write_bytes((make_parts.OUT / "gusset.dxf").read_bytes())      # 前の作業の残り
    e = job.add_part("flange.dxf", (make_parts.OUT / "flange.dxf").read_bytes())
    assert e.id == 1
    (jobs.JOB_DIR / "photo.bin").write_bytes(b"broken")
    meta = json.loads((jobs.JOB_DIR / "state.json").read_text(encoding="utf-8"))
    meta["photo_info"] = {"ok": True, "candidates": []}
    (jobs.JOB_DIR / "state.json").write_text(json.dumps(meta), encoding="utf-8")
    back = jobs.Job.restore()
    assert [p.part.name for p in back.parts] == ["flange.dxf"]
    assert back.parts[0].part.area == pytest.approx(e.part.area)
    assert back.photo is None and back.next_part_id == 2
