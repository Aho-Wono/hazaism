"""写真の読み込みと、サーバーの入口（API）の検査。"""
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
    from scrapnest import jobs as j
    monkeypatch.setattr(j, "JOB_DIR", tmp_path / "jobs" / "last")
    monkeypatch.setattr(j, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setenv("SCRAPNEST_NO_UPDATE_CHECK", "1")      # テストではネットワークへ出ない
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


def test_decode_heic(jobs):
    pillow_heif = pytest.importorskip("pillow_heif")
    im = Image.new("RGB", (64, 48), (0, 128, 255))
    buf = io.BytesIO()
    try:
        im.save(buf, "HEIF")
    except Exception:
        pytest.skip("この環境では HEIC を書き出せない")
    bgr, _ = jobs.decode_photo(buf.getvalue())
    assert bgr.shape[:2] == (48, 64)
    assert abs(int(bgr[10, 10, 0]) - 255) < 25 and abs(int(bgr[10, 10, 2]) - 0) < 25
    del pillow_heif


def test_settings_roundtrip(jobs):
    s = jobs.load_settings()
    assert s["pitch_mm"] == 10.0 and s["spacing_mm"] == 3.0
    s["spacing_mm"] = 1.5
    s["unknown_key"] = 1
    jobs.save_settings(s)
    saved = json.loads(jobs.CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["spacing_mm"] == 1.5 and "unknown_key" not in saved
    assert jobs.load_settings()["spacing_mm"] == 1.5


@pytest.fixture()
def client(jobs):
    from fastapi.testclient import TestClient

    from scrapnest.server import create_app
    return TestClient(create_app(restore=False))


def test_api_outline_boolean_and_validation(client):
    rect = {"type": "rect", "x": 0, "y": 0, "w": 100, "h": 60}
    r = client.post("/api/outline/boolean", json={"shape": None, "prim": rect, "op": "add"}).json()
    assert r["info"]["area"] == pytest.approx(6000, abs=0.5) and not r["problems"]
    shape = r["shape"]
    # 円を引く → 穴になる。円弧のまま残る
    r = client.post("/api/outline/boolean", json={"shape": shape, "prim": {"type": "circle", "cx": 50, "cy": 30, "r": 10}, "op": "sub"}).json()
    assert len(r["shape"]["holes"]) == 1
    assert all(abs(abs(b) - 1.0) < 1e-6 for _, _, b in r["shape"]["holes"][0])      # 半円 2 つ
    assert r["info"]["area"] == pytest.approx(6000 - np.pi * 100, abs=0.5)
    # 角に円を足す → 外周に円弧が 1 本入る
    r2 = client.post("/api/outline/boolean", json={"shape": shape, "prim": {"type": "circle", "cx": 100, "cy": 60, "r": 20}, "op": "add"}).json()
    arcs = [p for p in r2["shape"]["outer"] if abs(p[2]) > 1e-6]
    assert 1 <= len(arcs) <= 2 and len(r2["shape"]["outer"]) <= 7
    assert r2["info"]["area"] == pytest.approx(6000 + 0.75 * np.pi * 400, abs=0.5)
    # 真っ二つにすると、大きい方だけ残る
    r3 = client.post("/api/outline/boolean", json={"shape": shape, "prim": {"type": "rect", "x": 60, "y": -5, "w": 5, "h": 70}, "op": "sub"}).json()
    assert r3["dropped"] == 1 and r3["info"]["area"] == pytest.approx(3600, abs=0.5)
    # 自分自身と交差する輪郭は不具合として返る
    bow = {"outer": [[0, 0, 0], [10, 10, 0], [10, 0, 0], [0, 10, 0]], "holes": []}
    assert client.post("/api/outline", json={"shape": bow}).json()["problems"]
    # 楕円は円弧の集まりになる（誤差 0.05 mm 以内）
    e = client.post("/api/outline/boolean", json={"shape": None, "prim": {"type": "ellipse", "cx": 0, "cy": 0, "rx": 40, "ry": 25}, "op": "add"}).json()
    assert e["info"]["area"] == pytest.approx(np.pi * 40 * 25, rel=2e-3)
    assert len(e["shape"]["outer"]) <= 40


def test_api_parts_and_export_errors(client, tmp_path):
    import make_parts
    make_parts.OUT.mkdir(parents=True, exist_ok=True)
    make_parts.flange()
    data = (make_parts.OUT / "flange.dxf").read_bytes()
    r = client.post("/api/parts", files=[("files", ("flange.dxf", data, "application/dxf")), ("files", ("bad.dxf", b"not a dxf", "application/dxf"))]).json()
    assert [p["name"] for p in r["added"]] == ["flange.dxf"] and [e["name"] for e in r["errors"]] == ["bad.dxf"]
    pid = r["added"][0]["id"]
    assert r["added"][0]["width"] == pytest.approx(60, abs=0.1)
    up = client.patch(f"/api/parts/{pid}", json={"qty": 5, "no_flip": True, "rotation": "90", "unit": "cm"}).json()
    assert (up["qty"], up["no_flip"], up["rotation"], up["unit"]) == (5, True, "90", "cm") and up["width"] == pytest.approx(600, abs=1)
    assert client.patch("/api/parts/999", json={"qty": 1}).status_code == 404
    # 敷き詰め結果がないうちは出力できない
    assert client.post("/api/export", json={"path": str(tmp_path / "x.dxf")}).status_code == 400
    assert client.delete(f"/api/parts/{pid}").json() == {"ok": True}
    assert client.get("/api/parts").json() == []


def test_api_photo_and_manual_calibration(client):
    """合成写真を読み込み、格子の交点 4 つを指定する手動校正でも同じ結果になること。"""
    import cv2

    import make_synthetic as syn
    photo, truth, cam = syn.make(thickness=0.0, tilt_deg=12.0, yaw_deg=4.0)
    data = cv2.imencode(".jpg", photo, [cv2.IMWRITE_JPEG_QUALITY, 92])[1].tobytes()
    info = client.post("/api/photo", files={"file": ("p.jpg", data, "image/jpeg")}).json()
    assert info["ok"] and not info["grid"]["manual"] and info["grid"]["rms_mm"] < 0.15
    assert len(info["candidates"]) == 1
    auto_area = info["candidates"][0]["area"]
    assert auto_area == pytest.approx(truth.area, rel=0.02)
    assert client.get("/api/photo/rect.jpg").status_code == 200
    assert client.get("/api/photo/preview.jpg").status_code == 200

    # 30 × 40 マスの四隅（左上 → 右上 → 右下 → 左下）
    pts = cam.project(np.array([[60.0, 100.0], [360.0, 100.0], [360.0, 500.0], [60.0, 500.0]])).tolist()
    info2 = client.post("/api/calibrate", json={"points": pts, "cells_u": 30, "cells_v": 40}).json()
    assert info2["ok"] and info2["grid"]["manual"] and info2["grid"]["rms_mm"] < 0.15
    assert info2["candidates"][0]["area"] == pytest.approx(auto_area, rel=0.01)
    # 順番が逆（鏡像）なら断られる
    assert client.post("/api/calibrate", json={"points": pts[::-1], "cells_u": 30, "cells_v": 40}).status_code == 400

    # 板厚を変えて検出し直せる
    r = client.post("/api/detect", json={"thickness_mm": 0.0, "sensitivity": 0.5}).json()
    assert len(r["candidates"]) == 1
    st = client.get("/api/state").json()
    assert st["photo"]["ok"] and st["settings"]["thickness_mm"] == 0.0


def test_reset_and_version(client):
    import make_parts
    make_parts.OUT.mkdir(parents=True, exist_ok=True)
    make_parts.flange()
    data = (make_parts.OUT / "flange.dxf").read_bytes()
    client.post("/api/parts", files=[("files", ("flange.dxf", data, "application/dxf"))])
    assert len(client.post("/api/reset", json={"keep_parts": True}).json()["parts"]) == 1
    assert client.post("/api/reset", json={"keep_parts": False}).json()["parts"] == []
    assert client.get("/api/state").json()["parts"] == []
    v = client.get("/api/version").json()
    assert v["version"].count(".") == 2 and v["update"] is False and v["latest"] is None


def test_update_version_compare():
    from scrapnest import update
    assert update.is_newer("0.2.1", "0.2.0") and update.is_newer("0.10.0", "0.9.9") and update.is_newer("1.0", "0.9.9")
    assert not update.is_newer("0.2.0", "0.2.0") and not update.is_newer("0.1.9", "0.2.0") and not update.is_newer(None, "0.2.0")


def test_restore_keeps_parts_when_photo_is_broken(jobs):
    """前回の写真が壊れていても、登録した部材は残る。古い番号のファイルが残っていても正しい中身で読む。"""
    import make_parts
    make_parts.OUT.mkdir(parents=True, exist_ok=True)
    make_parts.flange()
    make_parts.gusset()
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
