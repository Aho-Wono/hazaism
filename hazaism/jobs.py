"""作業の状態（写真・輪郭・部材・敷き詰め結果）と、設定の記憶。

このアプリはブラウザの中（Pyodide）で 1 人が使うものなので、作業は常に 1 つだけ持つ。
ページを開き直しても続きから再開できるように、HOME/jobs/last に保存しておく
（ブラウザでは HOME=/hazaism で、ブラウザ内の保存領域 IndexedDB につながっている）。
"""
from __future__ import annotations

import io
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

from .dxfio import reader, writer
from .geom import outline as go
from .nest import engine, verify
from .vision import grid as vgrid
from .vision import outline as voutline
from .vision import segment as vsegment

ROOT = Path(__file__).resolve().parent.parent
# 保存場所。テストでは HAZAISM_HOME で別の場所に向ける
HOME = Path(os.environ.get("HAZAISM_HOME") or ROOT)
JOB_DIR = HOME / "jobs" / "last"
CONFIG_PATH = HOME / "config.json"

DEFAULT_SETTINGS = {
    "pitch_mm": 10.0,
    "thickness_mm": 2.0,
    "sensitivity": 0.5,
    "spacing_mm": 0.5,
    "margin_mm": 1.5,
    "angle_step": 2.0,
    "time_limit": 10.0,
    "use_holes": True,
    "include_scrap": True,
    "scrap_color": 5,           # 端材の輪郭の線の色（AutoCAD の色番号。5 = 青）
    "out_name": "nest.dxf",
    "dxf_version": "R2010",
}

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:  # HEIC が読めなくても他は動く
    pass


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    try:
        s.update({k: v for k, v in json.loads(CONFIG_PATH.read_text(encoding="utf-8")).items() if k in DEFAULT_SETTINGS})
    except Exception:
        pass
    return s


def save_settings(s: dict) -> None:
    CONFIG_PATH.write_text(json.dumps({k: s[k] for k in DEFAULT_SETTINGS if k in s}, ensure_ascii=False, indent=2), encoding="utf-8")


class UnsupportedImage(ValueError):
    """Pillow で読めない画像（HEIC など）。ブラウザ側で JPEG に直して送り直してもらう。"""


def decode_photo(data: bytes) -> tuple[np.ndarray, float | None]:
    """写真のバイト列 → (BGR 画像, 焦点距離 [px] or None)。EXIF の向きを反映する。"""
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception as ex:
        raise UnsupportedImage(f"この形式の画像は読めません: {ex}") from ex
    f35 = None
    try:
        exif = im.getexif()
        f35 = exif.get_ifd(0x8769).get(0xA405)      # FocalLengthIn35mmFilm
    except Exception:
        pass
    im = ImageOps.exif_transpose(im).convert("RGB")
    bgr = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)
    f_px = None
    if f35:
        h, w = bgr.shape[:2]
        f_px = float(f35) / 43.27 * float(np.hypot(w, h))
    return bgr, f_px


def _round_shape(shape: dict) -> dict:
    r = lambda loop: [[round(x, 3), round(y, 3), round(b, 6)] for x, y, b in loop]
    return {"outer": r(shape["outer"]), "holes": [r(h) for h in shape.get("holes", [])]}


@dataclass
class PartEntry:
    id: int
    part: reader.Part
    data: bytes                 # 元の DXF（保存・再読込用）
    qty: int = 1
    no_flip: bool = False
    rotation: str = "free"

    def to_json(self) -> dict:
        minx, miny, maxx, maxy = self.part.bounds
        # 画面で塗りつぶすための輪（外形と穴）。表示用なので少し粗くする
        rings = []
        simple = self.part.polygon.simplify(0.03)
        for poly in getattr(simple, "geoms", [simple]):
            for ring in (poly.exterior, *poly.interiors):
                rings.append([[round(x, 3), round(y, 3)] for x, y in ring.coords])
        return {
            "rings": rings,
            "id": self.id, "name": self.part.name, "qty": self.qty, "no_flip": self.no_flip, "rotation": self.rotation,
            "unit": self.part.unit, "width": round(maxx - minx, 2), "height": round(maxy - miny, 2),
            "area": round(self.part.area, 1), "bounds": [round(v, 3) for v in (minx, miny, maxx, maxy)],
            "paths": reader.outline_paths(self.part), "warnings": self.part.warnings,
            "entities": len(self.part.entities),
        }


@dataclass
class Job:
    settings: dict = field(default_factory=load_settings)
    photo: bytes | None = None
    image: np.ndarray | None = None
    f_px: float | None = None
    grid: vgrid.Grid | None = None
    pose: dict | None = None
    rect: vsegment.Rectified | None = None
    rect_jpeg: bytes | None = None
    rect_version: int = 0
    seg: vsegment.Segmentation | None = None
    candidates: list = field(default_factory=list)     # [{"shape", "area", "touches_edge"}]
    photo_info: dict | None = None
    outline: dict | None = None                        # いま編集中の輪郭（クライアントが送ってきたもの）
    parts: list[PartEntry] = field(default_factory=list)
    next_part_id: int = 1
    nest_request: dict | None = None                   # 敷き詰め中の条件（nest_prepare で作り、nest_finish で使う）
    result: dict | None = None                         # 敷き詰め結果（確定したもの）
    lock: threading.RLock = field(default_factory=threading.RLock)

    # ---- 写真 → 輪郭 ----
    def set_photo(self, data: bytes) -> dict:
        image, f_px = decode_photo(data)
        with self.lock:
            self.photo, self.image, self.f_px = data, image, f_px
            self.grid = self.rect = self.seg = self.pose = None
            self.candidates, self.outline, self.result = [], None, None
            self.photo_info = None
        try:
            g = vgrid.detect_grid(image, pitch_mm=float(self.settings["pitch_mm"]))
        except vgrid.GridNotFound as ex:
            with self.lock:
                self.photo_info = self._photo_info(error=str(ex))
            self._persist()
            return self.photo_info
        return self._after_grid(g)

    def calibrate(self, points, cells_u: float, cells_v: float) -> dict:
        """手動校正: 格子の交点 4 つ（左上→右上→右下→左下）と、その間のマス数。"""
        if self.image is None:
            raise ValueError("写真がありません")
        h, w = self.image.shape[:2]
        g = vgrid.grid_from_points(points, cells_u, cells_v, (w, h), pitch_mm=float(self.settings["pitch_mm"]))
        g = vgrid.refine_manual(self.image, g)
        return self._after_grid(g)

    def _after_grid(self, g: vgrid.Grid) -> dict:
        rect = vsegment.rectify(self.image, g)
        ok, buf = cv2.imencode(".jpg", rect.image, [cv2.IMWRITE_JPEG_QUALITY, 88])
        with self.lock:
            self.grid, self.rect = g, rect
            self.pose = vgrid.camera_pose(g, self.f_px)
            self.rect_jpeg = buf.tobytes()
            self.rect_version += 1
        self.detect()
        with self.lock:
            self.photo_info = self._photo_info()
        self._persist()
        return self.photo_info

    def detect(self, thickness_mm: float | None = None, sensitivity: float | None = None) -> list:
        """板の切り出しと輪郭の当てはめをやり直す。"""
        if thickness_mm is not None:
            self.settings["thickness_mm"] = float(thickness_mm)
        if sensitivity is not None:
            self.settings["sensitivity"] = float(sensitivity)
        if self.rect is None or self.grid is None:
            if self.image is None:
                raise ValueError("写真がありません")
            # 保存から復元した直後は格子を持っていないので、写真から求め直す
            g = vgrid.detect_grid(self.image, pitch_mm=float(self.settings["pitch_mm"]))
            rect = vsegment.rectify(self.image, g)
            with self.lock:
                self.grid, self.rect = g, rect
                self.pose = vgrid.camera_pose(g, self.f_px)
                self.rect_jpeg = cv2.imencode(".jpg", rect.image, [cv2.IMWRITE_JPEG_QUALITY, 88])[1].tobytes()
                self.rect_version += 1
        g, rect = self.grid, self.rect
        seg = vsegment.segment(rect, g, sensitivity=float(self.settings["sensitivity"]))
        nadir = rect.grid_to_mm([[self.pose["x_mm"] / g.pitch_mm, self.pose["y_mm"] / g.pitch_mm]])[0]
        cands = voutline.extract_outlines(seg, rect, nadir_mm=nadir, cam_height_mm=self.pose["height_mm"],
                                          thickness_mm=float(self.settings["thickness_mm"]))
        with self.lock:
            self.seg = seg
            self.candidates = [{"shape": _round_shape(c.shape), "area": round(c.area_mm2, 1),
                                "touches_edge": c.touches_edge} for c in cands]
        save_settings(self.settings)
        self._persist()
        return self.candidates

    def _photo_info(self, error: str | None = None) -> dict:
        h, w = self.image.shape[:2]
        info = {"ok": error is None, "error": error, "width": w, "height": h, "grid": None, "rect": None,
                "candidates": self.candidates, "warnings": []}
        if error is not None or self.grid is None:
            return info
        g, rect = self.grid, self.rect
        lines = []
        for fam, (k0, k1), (t0, t1) in ((0, g.u_range, g.v_range), (1, g.v_range, g.u_range)):
            for k in range(k0, k1 + 1):
                ends = [[k, t0], [k, t1]] if fam == 0 else [[t0, k], [t1, k]]
                p = g.to_image(ends)
                lines.append({"p": [[round(float(v), 1) for v in q] for q in p], "major": k % 5 == 0})
        info["grid"] = {
            "rms_mm": None if np.isnan(g.rms_mm) else round(g.rms_mm, 3), "manual": g.manual,
            "mm_per_px": round(g.mm_per_px(), 4), "tilt_deg": round(self.pose["tilt_deg"], 1),
            "height_mm": round(self.pose["height_mm"], 0), "f_source": self.pose["f_source"],
            "cells": [g.u_range[1] - g.u_range[0], g.v_range[1] - g.v_range[0]], "lines": lines,
        }
        info["rect"] = self._rect_info()
        if self.pose["tilt_deg"] > 35:
            info["warnings"].append(f"かなり斜めから撮られています（傾き {self.pose['tilt_deg']:.0f}°）。真上に近いほど寸法が正確になります")
        if not np.isnan(g.rms_mm) and g.rms_mm > 0.4:
            info["warnings"].append(f"格子の当てはめ誤差が大きめです（{g.rms_mm:.2f} mm）。マットが反っていないか確認してください")
        if g.manual and g.samples is None:
            info["warnings"].append("格子線を自動で測れなかったので、クリックした 4 点だけで補正しています。輪郭は手で描いてください")
        return info

    def _rect_info(self) -> dict:
        r = self.rect
        nadir = None
        if self.pose and self.grid:
            nadir = [round(float(v), 1) for v in r.grid_to_mm([[self.pose["x_mm"] / r.pitch_mm, self.pose["y_mm"] / r.pitch_mm]])[0]]
        return {"width_mm": round(r.width_mm, 3), "height_mm": round(r.height_mm, 3), "px_per_mm": r.px_per_mm,
                "pitch_mm": r.pitch_mm, "version": self.rect_version, "nadir": nadir}

    def photo_preview(self, max_side: int = 1600) -> bytes:
        h, w = self.image.shape[:2]
        f = min(1.0, max_side / max(h, w))
        im = cv2.resize(self.image, None, fx=f, fy=f, interpolation=cv2.INTER_AREA) if f < 1 else self.image
        return cv2.imencode(".jpg", im, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()

    # ---- 部材 ----
    def add_part(self, name: str, data: bytes) -> PartEntry:
        part = reader.load_part_bytes(data, name)
        with self.lock:
            e = PartEntry(id=self.next_part_id, part=part, data=data)
            self.next_part_id += 1
            self.parts.append(e)
            self._invalidate()
        self._persist()
        return e

    def get_part(self, pid: int) -> PartEntry:
        for e in self.parts:
            if e.id == pid:
                return e
        raise KeyError(pid)

    def update_part(self, pid: int, **kw) -> PartEntry:
        e = self.get_part(pid)
        if kw.get("qty") is not None:
            e.qty = max(0, min(999, int(kw["qty"])))
        if kw.get("no_flip") is not None:
            e.no_flip = bool(kw["no_flip"])
        if kw.get("rotation") in ("free", "90", "fixed"):
            e.rotation = kw["rotation"]
        if kw.get("unit") and kw["unit"] != e.part.unit:
            reader.set_unit(e.part, kw["unit"])
        self._invalidate()
        self._persist()
        return e

    def remove_part(self, pid: int) -> None:
        with self.lock:
            self.parts = [e for e in self.parts if e.id != pid]
            self._invalidate()
        self._persist()

    # ---- 敷き詰め ----
    # 計算そのものはブラウザの作業スレッド（engine.NestWorker）で回す。ここでは準備と、終わったあとの検査をする
    def nest_prepare(self, shape: dict, params: dict) -> tuple[dict, bytes]:
        """敷き詰めの準備。戻り値: (画面で途中経過を出すための情報, 作業スレッドに配るデータ)。"""
        shape = go.normalize_shape(shape)
        msgs = go.validate(shape)
        if msgs:
            raise ValueError(msgs[0])
        container = go.shape_to_polygon(shape, 0.02)
        nparts = [engine.NestPart(index=e.id, polygon=e.part.polygon, qty=e.qty, allow_flip=not e.no_flip, rotation=e.rotation)
                  for e in self.parts if e.qty > 0]
        if not nparts:
            raise ValueError("個数が 1 以上の部材がありません")
        for k in ("spacing_mm", "margin_mm", "angle_step", "time_limit", "use_holes"):
            if k in params:
                self.settings[k] = params[k]
        save_settings(self.settings)
        prm = engine.NestParams(spacing=float(self.settings["spacing_mm"]), margin=float(self.settings["margin_mm"]),
                                angle_step=float(self.settings["angle_step"]), time_limit=float(self.settings["time_limit"]),
                                use_holes=bool(self.settings["use_holes"]))
        t = time.time()
        payload = engine.prepare(container, nparts, prm)
        with self.lock:
            self.outline = _round_shape(shape)
            self.result = None
            self.nest_request = {"container": container, "polys": {p.index: p.polygon for p in nparts},
                                 "qty": {p.index: p.qty for p in nparts}, "params": prm}
        self._persist()
        meta = {"qty": {str(p.index): p.qty for p in nparts},
                "part_area": {str(p.index): round(float(p.polygon.area), 3) for p in nparts},
                "scrap_area": round(float(container.area), 1), "time_limit": prm.time_limit,
                "spacing": prm.spacing, "margin": prm.margin, "prepare_s": round(time.time() - t, 2)}
        return meta, payload

    def nest_finish(self, best: dict | None, stats: dict | None = None) -> dict:
        """作業スレッドで見つかった最良の配置を、元の図形で検査して結果にまとめ、保存する。"""
        req = self.nest_request
        if req is None:
            raise ValueError("敷き詰めの準備ができていません。もう一度「敷き詰める」を押してください")
        prm = req["params"]
        placements = engine.placements_from_dicts(best["placements"]) if best else []
        placements, dropped = verify.drop_invalid(req["container"], req["polys"], placements, prm.spacing, prm.margin)
        counts: dict[int, int] = {}
        for p in placements:
            counts[p.part] = counts.get(p.part, 0) + 1
        area = sum(req["polys"][p.part].area for p in placements)
        scrap = req["container"].area
        stats = dict(stats or {})
        measured = verify.measure(req["container"], req["polys"], placements)
        metrics = {
            "runs": int(stats.get("runs", 0)), "elapsed": float(stats.get("elapsed", 0.0)),
            "workers": int(stats.get("workers", 0)), "history": stats.get("history", []),
            "leftover": round(float(best.get("leftover", 0.0)), 1) if best else 0.0,
            "min_spacing": None if measured["min_spacing"] is None else round(measured["min_spacing"], 2),
            "min_margin": None if measured["min_margin"] is None else round(measured["min_margin"], 2),
        }
        metrics["runs_per_sec"] = round(metrics["runs"] / metrics["elapsed"], 2) if metrics["elapsed"] > 0 else 0.0
        res = {
            "placements": [{"part": p.part, "flip": p.flip, "angle": p.angle, "tx": round(p.tx, 4), "ty": round(p.ty, 4)}
                           for p in placements],
            "counts": {str(k): [counts.get(k, 0), q] for k, q in req["qty"].items()},
            "placed": len(placements), "requested": sum(req["qty"].values()),
            "area": round(area, 1), "scrap_area": round(scrap, 1),
            "utilization": round(100 * area / scrap, 1) if scrap else 0.0,
            "dropped": len(dropped), "spacing": prm.spacing, "margin": prm.margin, "metrics": metrics,
        }
        with self.lock:
            self.result = res
        self._persist()
        return res

    # ---- 出力 ----
    def export(self, req: dict) -> tuple[dict, bytes]:
        """敷き詰め結果を DXF にする。戻り値: (画面に出す情報, DXF の中身)。"""
        res = self.result
        if not res or not res.get("placements"):
            raise ValueError("敷き詰め結果がありません")
        index = {e.id: i for i, e in enumerate(self.parts)}
        if any(pl["part"] not in index for pl in res["placements"]):
            raise ValueError("敷き詰めのあとに部材が削除されています。敷き詰めをやり直してください")
        placements = [writer.Placement(part=index[pl["part"]], flip=pl["flip"], angle=pl["angle"], tx=pl["tx"], ty=pl["ty"])
                      for pl in res["placements"]]
        origin = req.get("origin") or [0.0, 0.0]
        include = bool(req.get("include_scrap", True))
        color = int(req.get("scrap_color") or self.settings.get("scrap_color", 5))
        if color not in writer.SCRAP_COLORS:
            color = 5
        name = str(req.get("name") or self.settings.get("out_name") or "nest.dxf").strip() or "nest.dxf"
        name = "".join("_" if ch in '\\/:*?"<>|' else ch for ch in name)
        if not name.lower().endswith(".dxf"):
            name += ".dxf"
        info = writer.write_layout(None, [e.part for e in self.parts], placements,
                                   scrap_shape=self.outline if include else None, scrap_color=color,
                                   origin=(float(origin[0]), float(origin[1])), angle_deg=float(req.get("angle", 0.0)),
                                   version=self.settings.get("dxf_version", "R2010"))
        self.settings.update({"out_name": name, "include_scrap": include, "scrap_color": color})
        save_settings(self.settings)
        warning = None
        if include:
            used = set()
            for i in {pl.part for pl in placements}:
                used |= writer.entity_colors(self.parts[i].part)
            if color in used:
                warning = ("部材の線にも端材の輪郭と同じ色が使われています。加工ソフトで色ごとに分けると、"
                           "部材の一部まで「加工なし」になってしまうので、輪郭の色を変えて保存し直してください。")
        meta = {"ok": True, "name": name, "entities": info["entities"], "extents": info["extents"],
                "placed": len(placements), "warning": warning}
        return meta, info["data"]

    # ---- 保存と復元 ----
    def state(self) -> dict:
        """画面を作り直すのに必要なもの一式。"""
        return {
            "settings": self.settings,
            "photo": self.photo_info,
            "outline": self.outline,
            "parts": [e.to_json() for e in self.parts],
            "result": self.result,
        }

    def _persist(self) -> None:
        try:
            JOB_DIR.mkdir(parents=True, exist_ok=True)
            with self.lock:
                if self.photo is not None:
                    f = JOB_DIR / "photo.bin"
                    if not f.exists() or f.stat().st_size != len(self.photo):
                        f.write_bytes(self.photo)
                if self.rect is not None and self.rect_jpeg is not None:
                    (JOB_DIR / "rect.jpg").write_bytes(self.rect_jpeg)
                pdir = JOB_DIR / "parts"
                pdir.mkdir(exist_ok=True)
                keep = set()
                for e in self.parts:
                    f = pdir / f"{e.id}.dxf"
                    keep.add(f.name)
                    # 同じ番号の古いファイルが残っていることがあるので、中身が違えば書き直す
                    if not f.exists() or f.stat().st_size != len(e.data) or f.read_bytes() != e.data:
                        f.write_bytes(e.data)
                for f in pdir.glob("*.dxf"):
                    if f.name not in keep:
                        f.unlink()
                meta = {
                    "photo_info": self.photo_info, "outline": self.outline, "result": self.result,
                    "f_px": self.f_px, "pose": self.pose, "next_part_id": self.next_part_id,
                    "rect": None if self.rect is None else {"px_per_mm": self.rect.px_per_mm, "u0": self.rect.u0,
                                                              "v0": self.rect.v0, "pitch_mm": self.rect.pitch_mm},
                    "parts": [{"id": e.id, "name": e.part.name, "qty": e.qty, "no_flip": e.no_flip,
                               "rotation": e.rotation, "unit": e.part.unit} for e in self.parts],
                }
            (JOB_DIR / "state.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass      # 保存できなくても作業は続けられる

    def set_outline(self, shape: dict | None) -> None:
        new = None if shape is None else _round_shape(go.normalize_shape(shape))
        with self.lock:
            if new != self.outline:
                self._invalidate()
            self.outline = new
        self._persist()

    def _invalidate(self) -> None:
        """輪郭や部材が変わったので、敷き詰め結果を捨てる。"""
        self.nest_request = self.result = None

    @classmethod
    def restore(cls) -> "Job":
        """前回の作業を読み込む。部材・輪郭・写真はそれぞれ別に読み、どれかが読めなくても他は残す。"""
        job = cls()
        try:
            meta = json.loads((JOB_DIR / "state.json").read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        try:
            job.next_part_id = int(meta.get("next_part_id", 1))
        except (TypeError, ValueError):
            pass
        for pm in meta.get("parts", []):
            try:
                f = JOB_DIR / "parts" / f"{pm['id']}.dxf"
                data = f.read_bytes()
                part = reader.load_part_bytes(data, pm["name"], pm.get("unit"))
                job.parts.append(PartEntry(id=int(pm["id"]), part=part, data=data, qty=int(pm.get("qty", 1)),
                                           no_flip=bool(pm.get("no_flip", False)), rotation=pm.get("rotation", "free")))
            except Exception:
                continue          # この部材だけあきらめる
        if job.parts:
            job.next_part_id = max(job.next_part_id, max(e.id for e in job.parts) + 1)
        job.outline, job.result = meta.get("outline"), meta.get("result")
        try:
            photo = JOB_DIR / "photo.bin"
            if photo.exists() and meta.get("photo_info"):
                job.photo = photo.read_bytes()
                job.image, _ = decode_photo(job.photo)
                job.f_px, job.pose = meta.get("f_px"), meta.get("pose")
                job.photo_info = meta["photo_info"]
                job.candidates = job.photo_info.get("candidates", [])
                rj, rm = JOB_DIR / "rect.jpg", meta.get("rect")
                if rj.exists() and rm:
                    job.rect_jpeg = rj.read_bytes()
                    img = cv2.imdecode(np.frombuffer(job.rect_jpeg, np.uint8), cv2.IMREAD_COLOR)
                    job.rect = vsegment.Rectified(image=img, valid=np.full(img.shape[:2], 255, np.uint8),
                                                  px_per_mm=rm["px_per_mm"], u0=rm["u0"], v0=rm["v0"], pitch_mm=rm["pitch_mm"])
                    job.rect_version = 1
        except Exception:
            # 写真が読めなければ、写真まわりだけ捨てる（部材は残す）
            job.photo = job.image = job.rect = job.rect_jpeg = job.pose = None
            job.photo_info, job.candidates, job.outline, job.result = None, [], None, None
        return job

    def reset(self, keep_parts: bool = False) -> None:
        """写真・輪郭・敷き詰め結果を消す。keep_parts が False なら登録した部材も消す。"""
        with self.lock:
            self.photo = self.image = self.grid = self.rect = self.seg = self.pose = self.rect_jpeg = None
            self.candidates, self.photo_info, self.outline, self.result = [], None, None, None
            self.nest_request = None
            if not keep_parts:
                self.parts = []
        try:
            for name in ("photo.bin", "rect.jpg", "state.json"):
                f = JOB_DIR / name
                if f.exists():
                    f.unlink()
        except Exception:
            pass
        self._persist()
