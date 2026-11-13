"""正解の形がわかっている合成写真を作る。

格子マット（1 マス 10 mm、5 本ごとに太線）の上に、厚みのある板を置いて斜めから撮った絵を描く。
板の上面と側面は、高さごとの射影（平面ごとのホモグラフィ）で描くので、斜めから撮ったときの
「上面の縁が外へずれて写る」様子も再現される。

usage: uv run python tests/make_synthetic.py [out.png]
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import cv2
import numpy as np
from shapely import affinity
from shapely.geometry import Point, Polygon, box

MAT_W, MAT_H = 450.0, 600.0      # マットの大きさ [mm]
GRID = (20.0, 20.0, 430.0, 580.0)   # 格子が印刷されている範囲


def plate_shape() -> Polygon:
    """左右非対称（鏡像と重ならない）で、直線・円弧・穴を含む板。マット上の mm 座標（x 右、y 下）。"""
    p = box(0, 0, 160, 100)
    p = p.difference(Point(160, 100).buffer(55, quad_segs=128))      # 角を丸くえぐる
    p = p.union(box(-25, 0, 0, 30))                                   # 片側だけ耳を付ける
    p = p.difference(Point(45, 60).buffer(11, quad_segs=64))         # 丸穴
    p = p.difference(box(95, 15, 140, 27))                            # 角穴
    p = affinity.rotate(p, 17, origin=(0, 0))
    return affinity.translate(p, 150, 235)


def mat_texture(s: float, rng) -> np.ndarray:
    """真上から見たマット（s 画素 / mm）。"""
    W, H = int(MAT_W * s), int(MAT_H * s)
    img = np.empty((H, W, 3), np.float32)
    img[:] = (176, 190, 188)                                           # BGR: 灰色がかった薄緑
    # ゆるい明るさのむら
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    img *= (1.0 + 0.05 * np.sin(xx / W * 2.3 + 0.4) * np.cos(yy / H * 1.7))[..., None]
    x0, y0, x1, y1 = GRID
    k = 0
    x = x0
    while x <= x1 + 1e-6:
        w = 0.75 if k % 5 == 0 else 0.4
        cv2.rectangle(img, (int(round((x - w / 2) * s)), int(y0 * s)), (int(round((x + w / 2) * s)), int(y1 * s)), (38, 40, 40), -1)
        x += 10.0
        k += 1
    k = 0
    y = y0
    while y <= y1 + 1e-6:
        w = 0.75 if k % 5 == 0 else 0.4
        cv2.rectangle(img, (int(x0 * s), int(round((y - w / 2) * s))), (int(x1 * s), int(round((y + w / 2) * s))), (38, 40, 40), -1)
        y += 10.0
        k += 1
    img += rng.normal(0, 2.0, img.shape).astype(np.float32)
    return img


class Camera:
    def __init__(self, size, f_px, center, target, yaw_deg=0.0):
        self.w, self.h = size
        self.f = f_px
        # 右手系 (x, y 上, z 上) で考える。マットの y は下向きなので符号を反転する
        self.C = np.array([center[0], -center[1], center[2]], float)
        T = np.array([target[0], -target[1], 0.0])
        fwd = (T - self.C) / np.linalg.norm(T - self.C)
        a = math.radians(yaw_deg)
        up_hint = np.array([math.sin(a), math.cos(a), 0.0])
        right = np.cross(fwd, up_hint)
        right /= np.linalg.norm(right)
        up = np.cross(right, fwd)
        self.R = np.stack([right, up, fwd])

    def project(self, pts_mat: np.ndarray, z: float = 0.0) -> np.ndarray:
        """マット上の点 (x, y 下向き) の、高さ z の位置が写る画素。"""
        P = np.c_[pts_mat[:, 0], -pts_mat[:, 1], np.full(len(pts_mat), z)] - self.C
        c = P @ self.R.T
        return np.c_[self.w / 2 + self.f * c[:, 0] / c[:, 2], self.h / 2 - self.f * c[:, 1] / c[:, 2]]

    def homography(self, z: float = 0.0) -> np.ndarray:
        src = np.array([[0, 0], [MAT_W, 0], [MAT_W, MAT_H], [0, MAT_H]], np.float32)
        return cv2.getPerspectiveTransform(src, self.project(src.astype(float), z).astype(np.float32))

    @property
    def tilt_deg(self) -> float:
        return math.degrees(math.acos(abs(self.R[2, 2])))


def make(size=(2160, 2880), tilt_deg=22.0, yaw_deg=6.0, thickness=2.0, top_gray=70, wall_gray=165, height=480.0,
         f_px=2150.0, seed=1, plate: Polygon | None = None):
    """戻り値: (写真 BGR, 板の底面の外形（マット上の mm、y 下向き）, カメラ)。"""
    rng = np.random.default_rng(seed)
    plate = plate or plate_shape()
    target = (225.0, 300.0)
    d = height * math.tan(math.radians(tilt_deg))
    cam = Camera(size, f_px, center=(target[0] + 0.35 * d, target[1] + 0.94 * d, height), target=target, yaw_deg=yaw_deg)

    ss = 2                       # 2 倍で描いて縮小（縁をなめらかにする）
    W, H = size[0] * ss, size[1] * ss
    o = (ss - 1) / 2             # 拡大した絵の画素番号 = ss × 元の画素番号 + o（どちらも画素の中心が整数）
    S = np.array([[ss, 0, o], [0, ss, o], [0, 0, 1.0]])
    s_tex = 8.0
    tex = mat_texture(s_tex, rng)
    Htex = S @ cam.homography(0.0) @ np.diag([1 / s_tex, 1 / s_tex, 1.0])
    img = cv2.warpPerspective(tex, Htex, (W, H), flags=cv2.INTER_AREA, borderMode=cv2.BORDER_CONSTANT, borderValue=(60, 62, 66))

    def ring_px(ring, z):
        return cam.project(np.asarray(ring.coords, float), z) * ss + o

    rings = [plate.exterior, *plate.interiors]
    if thickness > 0:            # 側面（底の縁と上の縁を結ぶ帯）
        for ring in rings:
            b, t = ring_px(ring, 0.0), ring_px(ring, thickness)
            for i in range(len(b) - 1):
                quad = np.array([b[i], b[i + 1], t[i + 1], t[i]])
                cv2.fillConvexPoly(img, np.round(quad * 16).astype(np.int32), (wall_gray,) * 3, lineType=cv2.LINE_AA, shift=4)
    top = [np.round(ring_px(r, thickness) * 16).astype(np.int32).reshape(-1, 1, 2) for r in rings]
    face = np.zeros((H, W), np.uint8)
    cv2.fillPoly(face, top, 255, lineType=cv2.LINE_AA, shift=4)
    # 上面: 一様な色に、ゆるい明暗（金属の映り込みのつもり）
    yy = np.linspace(0, 1, H, dtype=np.float32)[:, None]
    shade = top_gray * (1.0 + 0.25 * (yy - 0.5)) * np.ones((1, W), np.float32)
    a = (face.astype(np.float32) / 255.0)[..., None]
    img = img * (1 - a) + shade[..., None] * a

    img = cv2.resize(img, size, interpolation=cv2.INTER_AREA)
    img = cv2.GaussianBlur(img, (0, 0), 0.7)
    img += rng.normal(0, 1.5, img.shape).astype(np.float32)
    return np.clip(img, 0, 255).astype(np.uint8), plate, cam


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "out" / "synthetic.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    photo, truth, cam = make()
    cv2.imwrite(str(out), photo)
    print("wrote", out, "tilt", round(cam.tilt_deg, 1), "deg  plate area", round(truth.area, 1), "mm2")
