"""敷き詰め計算のラスタ部品。

端材の空き領域と部材（間隔の半分ぶん太らせたもの）を同じ格子の画像にして、
「この向きの部材を置けない位置」を相関計算でまとめて求める。

格子の約束: 画素 (行 j, 列 i) は x∈[x0 + i·r, x0 + (i+1)·r), y∈[y0 + j·r, y0 + (j+1)·r) を表す
（y は上向きのまま。画像としては上下が逆だが計算には関係ない）。
図形の内側の画素に加えて、輪郭がかかる画素も 1 にする（＝図形より少し大きめになる）。
"""
from __future__ import annotations

import math

import cv2
import numpy as np
from scipy import fft as sfft
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


def blocked_positions(blocked: np.ndarray, mask: np.ndarray) -> np.ndarray | None:
    """blocked（1 = 置けない画素）の上で、mask（1 = 部材）の左上を置けない位置の bool 画像。

    戻り値の形は (H − h + 1, W − w + 1)。部材が格子より大きければ None。
    """
    H, W = blocked.shape
    h, w = mask.shape
    if h > H or w > W:
        return None
    return cv2.matchTemplate(blocked, mask, cv2.TM_CCORR) > 0.5


_FFT_SIZES = sorted({m * 2 ** k for m in (1, 3, 5) for k in range(3, 13)})


def fft_size(n: int) -> int:
    """n 以上の、FFT が速くて種類の少ない長さ（同じ大きさで計算することが増えて、変換結果を使い回せる）。"""
    for v in _FFT_SIZES:
        if v >= n:
            return v
    return n


def mask_fft(mask: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    return sfft.rfft2(mask.astype(np.float32), s=size)


def no_fit_region(fp: np.ndarray, fm: np.ndarray, size: tuple[int, int], placed_shape, mask_shape) -> np.ndarray:
    """置いた部材に対して、mask の左上を置くと重なる相対位置の bool 画像（相互相関を FFT で求める）。

    fp, fm は placed, mask を mask_fft(…, size) で変換したもの。size は各辺 hp + h − 1, wp + w − 1 以上。
    形は (hp + h − 1, wp + w − 1)。要素 [j, i] は、mask の左上が placed の左上から
    (dy, dx) = (j − (h − 1), i − (w − 1)) だけずれた位置にあたる。
    """
    (hp, wp), (h, w) = placed_shape, mask_shape
    c = sfft.irfft2(fp * np.conj(fm), s=size)
    c = np.roll(c, (h - 1, w - 1), axis=(0, 1))
    return c[:hp + h - 1, :wp + w - 1] > 0.5


def choose_resolution(width_mm: float, height_mm: float, target_px: float = 250_000, lo: float = 0.3, hi: float = 1.5) -> float:
    r = math.sqrt(max(width_mm * height_mm, 1.0) / target_px)
    return float(min(hi, max(lo, round(r * 20) / 20)))
