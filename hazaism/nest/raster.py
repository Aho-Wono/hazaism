"""敷き詰め計算のラスタ部品（Python 側は画像にするところまで。置ける位置の計算は static/js/nest/engine.js）。

端材の空き領域と部材（間隔の半分ぶん太らせたもの）を同じ格子の画像にする。

格子の約束: 画素 (行 j, 列 i) は x∈[x0 + i·r, x0 + (i+1)·r), y∈[y0 + j·r, y0 + (j+1)·r) を表す
（y は上向きのまま。画像としては上下が逆だが計算には関係ない）。
図形の内側の画素に加えて、輪郭がかかる画素も 1 にする（＝図形より少し大きめになる）。
"""
from __future__ import annotations

import math

import cv2
import numpy as np
from shapely.geometry import MultiPolygon, Polygon

_SHIFT = 6   # fillPoly の固定小数点（1/64 画素）


def _rings(geom):
    polys = geom.geoms if isinstance(geom, MultiPolygon) else [geom]
    for p in polys:
        if isinstance(p, Polygon) and not p.is_empty:
            yield np.asarray(p.exterior.coords)
            for r in p.interiors:
                yield np.asarray(r.coords)


def rasterize(geom, x0: float, y0: float, res: float, shape: tuple[int, int]) -> np.ndarray:
    """図形を uint8（0/1）の画像にする。穴は偶奇則で抜ける。輪郭がかかる画素も 1 になる。"""
    mask = np.zeros(shape, np.uint8)
    pts = []
    for ring in _rings(geom):
        px = (ring - np.array([x0, y0])) / res - 0.5          # 画素の中心が整数になる座標
        pts.append(np.round(px * (1 << _SHIFT)).astype(np.int32).reshape(-1, 1, 2))
    if pts:
        cv2.fillPoly(mask, pts, 1, lineType=cv2.LINE_8, shift=_SHIFT)
        cv2.polylines(mask, pts, True, 1, 1, lineType=cv2.LINE_8, shift=_SHIFT)     # 輪郭（穴の縁も）を確実に塗る
    return mask


def choose_resolution(width_mm: float, height_mm: float, target_px: float = 250_000, lo: float = 0.3, hi: float = 1.5) -> float:
    r = math.sqrt(max(width_mm * height_mm, 1.0) / target_px)
    return float(min(hi, max(lo, round(r * 20) / 20)))
