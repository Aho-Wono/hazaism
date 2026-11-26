"""敷き詰めの準備（Python 側）。探索そのものはブラウザの作業スレッドで JavaScript（static/js/nest/engine.js）が回す。

ここでするのは、図形（shapely）が要る次のことだけ:
  - 部材ごとに試す向き（角度, 反転）の一覧を作る。回転対称・鏡映対称で同じ形になるものは省く
  - 端材と、各向きの部材（間隔の半分ぶん太らせたもの）を同じ格子の画像にする（raster.rasterize）
  - それをまとめて、JavaScript が読めるデータ（prepare() → bytes）にする
探索の結果は jobs.nest_finish → verify.py で、元の図形のまま確かめる。

探索のしかた（engine.js）:
  1 回の貪欲配置で、部材を 1 個ずつ、候補の向きそれぞれについて「置ける位置」を求め、「寄せる方向」に対して
  部材の先端がいちばん手前に収まる位置と向きを選ぶ。配置どうしの比較は ①置けた面積 ②置けた個数 ③残った空き領域のうち
  最大のひとかたまりの面積。これを、置く順番と向きを乱数で変えたり、良い配置の一部をこわして置き直したりしながら繰り返す。
"""
from __future__ import annotations

import hashlib
import json
import math
import struct
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np
from shapely import affinity
from shapely.geometry import MultiPolygon, Polygon, box
from shapely.geometry.polygon import orient as orient_ring

from . import raster

PAYLOAD_MAGIC = b"HZN1"
PAYLOAD_VERSION = 1


@dataclass
class NestPart:
    index: int                       # 呼び出し側の部材番号
    polygon: object                  # shapely の多角形（mm、部材のローカル座標）
    qty: int
    allow_flip: bool = True
    rotation: str = "free"           # "free" / "90" / "fixed"


@dataclass
class NestParams:
    spacing: float = 3.0             # 部材どうしの間隔 [mm]
    margin: float = 3.0              # 端材の縁からの余白 [mm]
    angle_step: float = 5.0          # 回転の刻み [度]
    use_holes: bool = True           # 他の部材の穴の中にも置く
    time_limit: float = 30.0         # 計算時間 [秒]
    resolution: float | None = None  # 格子の細かさ [mm]（None なら自動）
    seed: int = 0


@dataclass
class Placed:
    part: int
    flip: bool
    angle: float
    tx: float
    ty: float


def transform_polygon(poly, flip: bool, angle: float, tx: float = 0.0, ty: float = 0.0):
    """部材のローカル座標 → 配置後。順に: 反転（x → −x）→ 回転 → 移動。"""
    g = affinity.scale(poly, xfact=-1.0, yfact=1.0, origin=(0, 0)) if flip else poly
    if angle:
        g = affinity.rotate(g, angle, origin=(0, 0))
    if tx or ty:
        g = affinity.translate(g, tx, ty)
    return g


def placements_from_dicts(items) -> list[Placed]:
    return [Placed(part=int(d["part"]), flip=bool(d["flip"]), angle=float(d["angle"]), tx=float(d["tx"]), ty=float(d["ty"]))
            for d in items]


def _filled(poly):
    polys = poly.geoms if isinstance(poly, MultiPolygon) else [poly]
    return MultiPolygon([Polygon(p.exterior) for p in polys])


def _poly_key(poly) -> str:
    return hashlib.sha1(poly.wkb).hexdigest()


# ---- 向きの候補 --------------------------------------------------------------------

def _same_shape(a, b) -> bool:
    """重心を合わせたとき同じ形か。"""
    ca, cb = a.centroid, b.centroid
    b2 = affinity.translate(b, ca.x - cb.x, ca.y - cb.y)
    return a.symmetric_difference(b2).area < 0.004 * a.area


def _second_moments(poly) -> tuple[float, float, float]:
    """重心まわりの 2 次モーメント（面積で割ったもの）: (xx, yy, xy)。"""
    polys = poly.geoms if isinstance(poly, MultiPolygon) else [poly]
    A = Sx = Sy = Sxx = Syy = Sxy = 0.0
    for p in polys:
        p = orient_ring(p, 1.0)               # 外周は反時計回り、穴は時計回り（穴の分は自然に差し引かれる）
        for ring in [p.exterior, *p.interiors]:
            c = np.asarray(ring.coords)
            x0, y0, x1, y1 = c[:-1, 0], c[:-1, 1], c[1:, 0], c[1:, 1]
            cr = x0 * y1 - x1 * y0
            A += cr.sum() / 2
            Sx += ((x0 + x1) * cr).sum() / 6
            Sy += ((y0 + y1) * cr).sum() / 6
            Sxx += ((x0 * x0 + x0 * x1 + x1 * x1) * cr).sum() / 12
            Syy += ((y0 * y0 + y0 * y1 + y1 * y1) * cr).sum() / 12
            Sxy += ((x0 * y1 + 2 * x0 * y0 + 2 * x1 * y1 + x1 * y0) * cr).sum() / 24
    cx, cy = Sx / A, Sy / A
    return Sxx / A - cx * cx, Syy / A - cy * cy, Sxy / A - cx * cy


_ORIENT_CACHE: OrderedDict = OrderedDict()


def orientations(part: NestPart, angle_step: float) -> list[tuple[float, bool]]:
    """試す向き（角度, 反転）の一覧。回転対称・鏡映対称で同じ形になるものは省く（同じ部材・同じ刻みなら覚えておく）。"""
    key = (_poly_key(part.polygon), float(angle_step), part.allow_flip, part.rotation)
    hit = _ORIENT_CACHE.get(key)
    if hit is not None:
        _ORIENT_CACHE.move_to_end(key)
        return list(hit)
    out = _orientations(part, angle_step)
    _ORIENT_CACHE[key] = out
    while len(_ORIENT_CACHE) > 256:
        _ORIENT_CACHE.popitem(last=False)
    return list(out)


def _orientations(part: NestPart, angle_step: float) -> list[tuple[float, bool]]:
    poly = part.polygon.simplify(0.05)
    if part.rotation == "fixed":
        angles, grid = [0.0], 360.0
    elif part.rotation == "90":
        angles, grid = [0.0, 90.0, 180.0, 270.0], 90.0
    else:
        grid = min(max(float(angle_step), 1.0), 360.0)
        angles = [float(a) for a in np.arange(0.0, 360.0 - 1e-9, grid)]
    # 回転対称の周期（いちばん小さいもの）。周期は 360 の約数なので、それだけ試す
    period = 360.0
    if len(angles) > 1:
        cands = {360.0 / k for k in range(2, 13)}
        cands |= {360.0 / k for k in range(2, 361) if abs((360.0 / k) / grid - round((360.0 / k) / grid)) < 1e-9}
        for cand in sorted(cands):
            if _same_shape(poly, affinity.rotate(poly, cand, origin="centroid")):
                period = cand
                break
    angles = [a for a in angles if a < period - 1e-9] or [0.0]
    out = [(a, False) for a in angles]
    if part.allow_flip and not _has_mirror_on_grid(poly, grid, period):
        out += [(a, True) for a in angles]
    return out


def _has_mirror_on_grid(poly, grid: float, period: float) -> bool:
    """裏返した形が、試す角度のどれかだけ回した元の形と同じか（同じなら裏返す向きは要らない）。

    裏返し（x → −x）が角度 θ の回転と同じになるのは、形に鏡映の軸があるとき。軸は慣性主軸のどちらかなので、
    その θ を試す角度にそろえて確かめる。慣性が等方的な形（正方形・正多角形・円）は、試す角度を全部確かめる。
    """
    mirrored = affinity.scale(poly, xfact=-1.0, yfact=1.0, origin=(0, 0))
    mxx, myy, mxy = _second_moments(poly)
    aniso = math.hypot(mxx - myy, 2 * mxy) > 1e-3 * (mxx + myy)
    if aniso:
        phi = 0.5 * math.degrees(math.atan2(2 * mxy, mxx - myy))       # 主軸の向き
        thetas = {180.0 - 2 * phi, -2 * phi}                            # 軸が φ か φ + 90° のときの回転角
    else:
        thetas = {float(a) for a in np.arange(0.0, period - 1e-9, grid)}
    for th in thetas:
        a = (round((th % period) / grid) * grid) % period              # 試す角度にそろえる
        if _same_shape(mirrored, affinity.rotate(poly, a, origin="centroid")):
            return True
    return False


# ---- 格子とマスク ------------------------------------------------------------------

@dataclass
class Grid:
    x0: float
    y0: float
    res: float
    W: int
    H: int
    grow: float                      # 部材を太らせる量 [mm]（間隔の半分 ＋ わずかな上乗せ）


def target_pixels(time_limit: float) -> float:
    """格子の画素数の目安。計算時間が短いときは格子を粗くして、試せる回数を増やす（10 秒以上は 0.5 までと同じ細かさ）。"""
    if time_limit <= 1:
        return 80_000
    if time_limit <= 3:
        return 120_000
    if time_limit <= 5:
        return 160_000
    return 250_000


def make_grid(container, params: NestParams) -> tuple[Grid, np.ndarray]:
    """格子と、端材の「置けない画素」（1 = 端材の外か、縁からの余白の内側）。"""
    minx, miny, maxx, maxy = container.bounds
    r = params.resolution or raster.choose_resolution(maxx - minx, maxy - miny, target_pixels(params.time_limit))
    # ラスタ化は「図形が少しでもかかる画素」を 1 にする（raster.rasterize）。部材も「置けない領域」も
    # その流儀で作るので、画像どうしが重ならなければ図形どうしも重ならない。念のため少しだけ上乗せする
    pad = 0.25 * r
    grid = Grid(x0=minx - 2 * r, y0=miny - 2 * r, res=r,
                W=int(math.ceil((maxx - minx) / r)) + 4, H=int(math.ceil((maxy - miny) / r)) + 4,
                grow=params.spacing / 2 + pad)
    free = container.buffer(-(max(params.margin - params.spacing / 2, 0.0) + pad))
    frame = box(grid.x0 - r, grid.y0 - r, grid.x0 + (grid.W + 1) * r, grid.y0 + (grid.H + 1) * r)
    blocked = raster.rasterize(frame.difference(free), grid.x0, grid.y0, r, (grid.H, grid.W))
    return grid, blocked


_MASK_CACHE: OrderedDict = OrderedDict()
_MASK_CACHE_LIMIT = 48_000_000        # 覚えておくマスクの合計 [バイト]
_mask_bytes = 0


def part_masks(part: NestPart, orients: list[tuple[float, bool]], grid: Grid, use_holes: bool) -> list:
    """向きごとのマスク [(mask(uint8), mx0, my0) or None]。格子に入りきらない向きは None。

    太らせる（buffer）のは反転ごとに 1 回だけで、回転はその後にする（向きごとに buffer するより速い）。
    同じ部材・同じ設定なら、作ったマスクを覚えておいて使い回す。
    """
    global _mask_bytes
    pkey = _poly_key(part.polygon)
    r = grid.res
    grown = {}
    out = []
    for angle, flip in orients:
        key = (pkey, use_holes, flip, angle, round(grid.grow, 9), round(r, 9))
        hit = _MASK_CACHE.get(key)
        if hit is None:
            if flip not in grown:
                poly = part.polygon if use_holes else _filled(part.polygon)
                g0 = affinity.scale(poly, xfact=-1.0, yfact=1.0, origin=(0, 0)) if flip else poly
                grown[flip] = g0.buffer(grid.grow, quad_segs=6)
            g = affinity.rotate(grown[flip], angle, origin=(0, 0)) if angle else grown[flip]
            minx, miny, maxx, maxy = g.bounds
            mx0, my0 = math.floor(minx / r) * r, math.floor(miny / r) * r
            w = int(math.ceil((maxx - mx0) / r)) + 1
            h = int(math.ceil((maxy - my0) / r)) + 1
            hit = (raster.rasterize(g, mx0, my0, r, (h, w)), mx0, my0)
            _MASK_CACHE[key] = hit
            _mask_bytes += hit[0].nbytes
            while len(_MASK_CACHE) > 2 and _mask_bytes > _MASK_CACHE_LIMIT:
                _, old = _MASK_CACHE.popitem(last=False)
                _mask_bytes -= old[0].nbytes
        else:
            _MASK_CACHE.move_to_end(key)
        mask = hit[0]
        out.append(hit if mask.shape[0] <= grid.H and mask.shape[1] <= grid.W else None)
    return out


# ---- JavaScript に渡すデータ ---------------------------------------------------------
#
#   "HZN1" | 見出しの長さ (uint32, little endian) | 見出し（JSON、4 バイト境界まで空白で埋める） | 画像
# 画像はどれも、行ごとに 32 ビットずつ詰めたもの（画素 i は 語 i >> 5 の ビット i & 31、little endian）。
# 見出しの off は、画像の並びの先頭（= 8 + 見出しの長さ）からのバイト位置（4 の倍数）、words は 1 行あたりの語数。

def _pack_rows(mask: np.ndarray) -> tuple[bytes, int]:
    h, w = mask.shape
    words = (w + 31) // 32
    padded = np.zeros((h, words * 32), np.uint8)
    padded[:, :w] = mask
    return np.packbits(padded, axis=1, bitorder="little").tobytes(), words


def _unpack_rows(data: bytes, off: int, h: int, w: int, words: int) -> np.ndarray:
    raw = np.frombuffer(data, np.uint8, count=h * words * 4, offset=off).reshape(h, words * 4)
    return np.unpackbits(raw, axis=1, bitorder="little")[:, :w]


def prepare(container, parts: list[NestPart], params: NestParams) -> bytes:
    """作業スレッド（JavaScript）に配るデータ。"""
    grid, blocked = make_grid(container, params)
    head_parts, head_orients = [], []
    for pi, part in enumerate(parts):
        lst = orientations(part, params.angle_step)
        idxs = []
        for (angle, flip), m in zip(lst, part_masks(part, lst, grid, params.use_holes)):
            if m is None:
                continue
            idxs.append(len(head_orients))
            head_orients.append({"part": pi, "angle": angle, "flip": flip, "mask": m[0], "mx0": m[1], "my0": m[2]})
        head_parts.append({"index": part.index, "area": float(part.polygon.area), "qty": int(part.qty), "orients": idxs})
    prm = {"spacing": params.spacing, "margin": params.margin, "angle_step": params.angle_step,
           "time_limit": params.time_limit, "use_holes": params.use_holes, "seed": params.seed}
    return pack_problem(grid, blocked, head_parts, head_orients, prm)


def pack_problem(grid: Grid, blocked: np.ndarray, parts: list[dict], orients: list[dict], params: dict) -> bytes:
    """格子・置けない画素・部材（index, area, qty, orients）・向き（part, angle, flip, mask, mx0, my0）をまとめる。"""
    blobs: list[bytes] = []
    pos = 0

    def add(mask: np.ndarray) -> dict:
        nonlocal pos
        data, words = _pack_rows(mask)
        off = pos
        blobs.append(data)
        pos += len(data)
        return {"off": off, "words": words}

    head = {"v": PAYLOAD_VERSION,
            "grid": {"x0": grid.x0, "y0": grid.y0, "res": grid.res, "W": grid.W, "H": grid.H},
            "blocked": add(blocked),
            "parts": parts,
            "orients": [{"part": o["part"], "angle": float(o["angle"]), "flip": bool(o["flip"]),
                         "h": int(o["mask"].shape[0]), "w": int(o["mask"].shape[1]),
                         "mx0": float(o["mx0"]), "my0": float(o["my0"]), **add(o["mask"])} for o in orients],
            "params": params}
    hj = json.dumps(head, separators=(",", ":")).encode("utf-8")
    hj += b" " * (-len(hj) % 4)
    return PAYLOAD_MAGIC + struct.pack("<I", len(hj)) + hj + b"".join(blobs)


def decode_payload(data: bytes) -> dict:
    """prepare() のデータを読み戻す（検査用）。画像は uint8 の numpy 配列にする。"""
    if data[:4] != PAYLOAD_MAGIC:
        raise ValueError("敷き詰めのデータではありません")
    (n,) = struct.unpack("<I", data[4:8])
    head = json.loads(data[8:8 + n].decode("utf-8"))
    base = 8 + n
    g = head["grid"]
    head["blocked_mask"] = _unpack_rows(data, base + head["blocked"]["off"], g["H"], g["W"], head["blocked"]["words"])
    for o in head["orients"]:
        o["mask"] = _unpack_rows(data, base + o["off"], o["h"], o["w"], o["words"])
    return head
