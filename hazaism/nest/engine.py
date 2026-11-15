"""敷き詰め: 貪欲配置を、順番と向きを変えながら時間いっぱい繰り返す。

1 回の貪欲配置:
  - 部材を 1 個ずつ、候補の向きそれぞれについて「置ける位置」の画像を作る（raster.py）
  - 「寄せる方向」に対して部材の先端がいちばん手前に収まる位置と向きを選ぶ
    （奥から順に詰まっていくので、残りの端材がひとかたまりで残りやすい）
配置どうしの比較は、①置けた面積 ②置けた個数 ③残った空き領域のうち最大のひとかたまりの面積。

部材は「間隔の半分」太らせた形で重なりを調べるので、結果は間隔と余白を満たす
（最後に verify.py で元の図形でも確かめる）。
"""
from __future__ import annotations

import math
import pickle
import sys
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field

import cv2
import numpy as np
from shapely import affinity
from shapely.geometry import MultiPolygon, Polygon, box

from . import raster

N_GRAVITY = 8
IN_BROWSER = sys.platform == "emscripten"     # Pyodide（ブラウザ内の Python）。スレッドが使えず、メモリも控えめにする


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
    workers: int | None = None
    seed: int = 0


@dataclass
class Placed:
    part: int
    flip: bool
    angle: float
    tx: float
    ty: float


@dataclass
class Layout:
    placements: list[Placed] = field(default_factory=list)
    placed_area: float = 0.0
    leftover: float = 0.0            # 残った空き領域のうち最大のひとかたまり [mm2]
    raw: list = field(default_factory=list, repr=False)   # 格子上の配置 [(向きのキー, x, y)]。置き直しに使う

    def key(self):
        return (round(self.placed_area, 2), len(self.placements), round(self.leftover, 0))

    def to_dict(self) -> dict:
        """作業スレッドから画面へ送る形（JSON にできるものだけ）。"""
        return {"placements": [asdict(p) for p in self.placements], "placed_area": self.placed_area,
                "leftover": self.leftover, "key": list(self.key())}


def transform_polygon(poly, flip: bool, angle: float, tx: float = 0.0, ty: float = 0.0):
    """部材のローカル座標 → 配置後。順に: 反転（x → −x）→ 回転 → 移動。"""
    g = affinity.scale(poly, xfact=-1.0, yfact=1.0, origin=(0, 0)) if flip else poly
    if angle:
        g = affinity.rotate(g, angle, origin=(0, 0))
    if tx or ty:
        g = affinity.translate(g, tx, ty)
    return g


def _filled(poly):
    polys = poly.geoms if isinstance(poly, MultiPolygon) else [poly]
    return MultiPolygon([Polygon(p.exterior) for p in polys])


# ---- 向きの候補 --------------------------------------------------------------------

def _same_shape(a, b) -> bool:
    """重心を合わせたとき同じ形か。"""
    ca, cb = a.centroid, b.centroid
    b2 = affinity.translate(b, ca.x - cb.x, ca.y - cb.y)
    return a.symmetric_difference(b2).area < 0.004 * a.area


def orientations(part: NestPart, angle_step: float) -> list[tuple[float, bool]]:
    """試す向き（角度, 反転）の一覧。回転対称・鏡映対称で同じ形になるものは省く。"""
    poly = part.polygon.simplify(0.05)
    if part.rotation == "fixed":
        angles = [0.0]
    elif part.rotation == "90":
        angles = [0.0, 90.0, 180.0, 270.0]
    else:
        step = min(max(float(angle_step), 1.0), 360.0)
        angles = [float(a) for a in np.arange(0.0, 360.0 - 1e-9, step)]
    # 回転対称: いちばん小さい周期を探して、その範囲だけにする
    period = 360.0
    for cand in sorted({a for a in angles if a > 0} | {360.0 / k for k in (2, 3, 4, 5, 6, 8)}):
        if cand < 360.0 and _same_shape(poly, affinity.rotate(poly, cand, origin="centroid")):
            period = cand
            break
    angles = [a for a in angles if a < period - 1e-9] or [0.0]
    out = [(a, False) for a in angles]
    if part.allow_flip:
        mirrored = affinity.scale(poly, xfact=-1.0, yfact=1.0, origin=(0, 0))
        tests = angles if len(angles) <= 72 else angles[:: max(1, len(angles) // 72)]
        has_axis = any(_same_shape(mirrored, affinity.rotate(poly, a, origin="centroid")) for a in
                       sorted(set(tests) | {a / 2 for a in tests}))
        if not has_axis:
            out += [(a, True) for a in angles]
    return out


# ---- 1 プロセスぶんの計算 ----------------------------------------------------------

class _Orient:
    __slots__ = ("part", "angle", "flip", "mask", "h", "w", "mx0", "my0", "base", "base_shape", "ext")

    def __init__(self, part, angle, flip, mask, mx0, my0):
        self.part, self.angle, self.flip = part, angle, flip
        self.mask = mask
        self.h, self.w = mask.shape
        self.mx0, self.my0 = mx0, my0
        self.base = None          # 端材だけに対する「置けない位置」（ビット詰め）。False は「どこにも置けない」
        self.base_shape = None
        self.ext = {}


class RasterNester:
    FFT_CACHE_BYTES = 32_000_000 if IN_BROWSER else 96_000_000
    MAX_ORIENTS = 160            # 1 部材あたり、1 回の貪欲配置で試す向きの上限

    def __init__(self, container, parts: list[NestPart], params: NestParams, orient_lists=None, bases=None):
        self.parts = parts
        self.params = params
        self._bases = bases or {}       # 親プロセスで計算済みの「端材だけに対する置けない位置」
        minx, miny, maxx, maxy = container.bounds
        self.res = params.resolution or raster.choose_resolution(maxx - minx, maxy - miny)
        r = self.res
        # ラスタ化は「図形が少しでもかかる画素」を 1 にする（raster.rasterize）。部材も「置けない領域」も
        # その流儀で作るので、画像どうしが重ならなければ図形どうしも重ならない。念のため少しだけ上乗せする
        self.pad = 0.25 * r
        self.grow = params.spacing / 2 + self.pad
        self.x0, self.y0 = minx - 2 * r, miny - 2 * r
        self.W = int(math.ceil((maxx - minx) / r)) + 4
        self.H = int(math.ceil((maxy - miny) / r)) + 4
        free = container.buffer(-(max(params.margin - params.spacing / 2, 0.0) + self.pad))
        frame = box(self.x0 - r, self.y0 - r, self.x0 + (self.W + 1) * r, self.y0 + (self.H + 1) * r)
        self.blocked = raster.rasterize(frame.difference(free), self.x0, self.y0, r, (self.H, self.W))
        self.free = (1 - self.blocked).astype(np.uint8)
        self.orient_lists = orient_lists or [orientations(p, params.angle_step) for p in parts]
        self.area = [float(p.polygon.area) for p in parts]
        self._orients: dict = {}
        self._ffts: OrderedDict = OrderedDict()     # (向きのキー, 大きさ) → マスクの FFT（新しい順に残す）
        self._fft_bytes = 0
        # 寄せる方向ごとの「奥行き」の画像（小さいほど奥）。わずかに横方向の成分を足して同点をなくす
        yy, xx = np.mgrid[0:self.H, 0:self.W].astype(np.float32)
        self.G = []
        for k in range(N_GRAVITY):
            a = 2 * math.pi * k / N_GRAVITY
            gx, gy = math.cos(a), math.sin(a)
            self.G.append((gx, gy, gx * xx + gy * yy + 1e-3 * (-gy * xx + gx * yy)))
        ksz = max(3, int(round(8.0 / r)) | 1)
        self._leftover_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ksz, ksz))

    # -- 向きごとのマスク --
    def orient(self, key) -> _Orient | None:
        o = self._orients.get(key, 0)
        if o != 0:
            return o
        pi, angle, flip = key
        poly = self.parts[pi].polygon
        if not self.params.use_holes:
            poly = _filled(poly)
        g = transform_polygon(poly, flip, angle).buffer(self.grow, quad_segs=6)
        minx, miny, maxx, maxy = g.bounds
        r = self.res
        mx0, my0 = math.floor(minx / r) * r, math.floor(miny / r) * r
        w = int(math.ceil((maxx - mx0) / r)) + 1
        h = int(math.ceil((maxy - my0) / r)) + 1
        if h > self.H or w > self.W:
            self._orients[key] = None
            return None
        mask = raster.rasterize(g, mx0, my0, r, (h, w))
        o = _Orient(pi, angle, flip, mask, mx0, my0)
        self._orients[key] = o
        return o

    def _base(self, o: _Orient):
        if o.base is None:
            pre = self._bases.get((o.part, o.angle, o.flip))
            if pre is not None:
                o.base, o.base_shape = pre
            else:
                B = raster.blocked_positions(self.blocked, o.mask)
                if B is None or B.all():
                    o.base = False
                else:
                    o.base_shape = B.shape
                    o.base = np.packbits(B)
        if o.base is False:
            return None
        n = o.base_shape[0] * o.base_shape[1]
        return np.unpackbits(o.base, count=n).reshape(o.base_shape).astype(bool)

    def _extent(self, o: _Orient, g: int) -> float:
        e = o.ext.get(g)
        if e is None:
            gx, gy, _ = self.G[g]
            jj, ii = np.nonzero(o.mask)
            e = float((gx * ii + gy * jj).max())
            o.ext[g] = e
        return e

    def _fft(self, key, size):
        k = (key, size)
        f = self._ffts.get(k)
        if f is None:
            f = raster.mask_fft(self._orients[key].mask, size)
            self._ffts[k] = f
            self._fft_bytes += f.nbytes
            while self._fft_bytes > self.FFT_CACHE_BYTES and len(self._ffts) > 2:
                _, old = self._ffts.popitem(last=False)
                self._fft_bytes -= old.nbytes
        else:
            self._ffts.move_to_end(k)
        return f

    def _apply_nfr(self, B: np.ndarray, o: _Orient, okey, pkey, px: int, py: int) -> None:
        """置いた部材 pkey（左上 px, py）と重なる位置を、向き o の「置けない位置」B に足す。"""
        p = self._orients[pkey]
        size = (raster.fft_size(p.h + o.h - 1), raster.fft_size(p.w + o.w - 1))
        nfr = raster.no_fit_region(self._fft(pkey, size), self._fft(okey, size), size, (p.h, p.w), (o.h, o.w))
        ys, xs = py - (o.h - 1), px - (o.w - 1)
        Hk, Wk = B.shape
        y0, y1 = max(0, ys), min(Hk, ys + nfr.shape[0])
        x0, x1 = max(0, xs), min(Wk, xs + nfr.shape[1])
        if y0 < y1 and x0 < x1:
            B[y0:y1, x0:x1] |= nfr[y0 - ys:y1 - ys, x0 - xs:x1 - xs]

    def all_bases(self, threads: int = 8) -> dict:
        """すべての向きについて「端材だけに対する置けない位置」を先に計算する（作業スレッドへ渡す用）。"""
        keys = [(i, a, f) for i, lst in enumerate(self.orient_lists) for a, f in lst]

        def one(key):
            o = self.orient(key)
            if o is None:
                return key, (False, None)
            self._base(o)
            return key, (o.base, o.base_shape)

        if IN_BROWSER or threads <= 1:          # ブラウザではスレッドを作れないので順番に
            return dict(one(k) for k in keys)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=threads) as ex:
            return dict(ex.map(one, keys))

    # -- 1 回の貪欲配置 --
    def greedy(self, order: list[int], orient_sets: list[list], g: int, fixed=None) -> Layout:
        """order の順に 1 個ずつ置く。fixed は、先に置いたままにしておく配置 [(向きのキー, x, y), ...]。"""
        placed = list(fixed or [])     # (向きのキー, x, y)
        live = {}             # 向きのキー → [置けない位置, 取り込み済みの配置数]
        failed = set()
        occ = np.zeros((self.H, self.W), np.uint8)
        for key, x, y in placed:
            o = self._orients[key]
            occ[y:y + o.h, x:x + o.w] |= o.mask
        Gmap = self.G[g][2]
        for pi in order:
            if pi in failed:
                continue
            best = None
            for key in orient_sets[pi]:
                o = self.orient(key)
                if o is None:
                    continue
                lv = live.get(key)
                if lv is None:
                    B = self._base(o)
                    if B is None:
                        continue
                    lv = live[key] = [B, 0]
                B = lv[0]
                for pkey, px, py in placed[lv[1]:]:
                    self._apply_nfr(B, o, key, pkey, px, py)
                lv[1] = len(placed)
                val = np.where(B, np.inf, Gmap[:B.shape[0], :B.shape[1]])
                idx = int(np.argmin(val))
                v = float(val.flat[idx])
                if not math.isfinite(v):
                    continue
                score = v + self._extent(o, g)
                if best is None or score < best[0]:
                    best = (score, key, idx % B.shape[1], idx // B.shape[1])
            if best is None:
                failed.add(pi)
                continue
            _, key, x, y = best
            o = self._orients[key]
            placed.append((key, x, y))
            occ[y:y + o.h, x:x + o.w] |= o.mask

        out = Layout(raw=placed)
        r = self.res
        for key, x, y in placed:
            o = self._orients[key]
            out.placements.append(Placed(part=self.parts[o.part].index, flip=o.flip, angle=o.angle,
                                         tx=self.x0 + x * r - o.mx0, ty=self.y0 + y * r - o.my0))
            out.placed_area += self.area[o.part]
        remain = cv2.erode(self.free & (1 - occ), self._leftover_kernel)
        n, _, stats, _ = cv2.connectedComponentsWithStats(remain, connectivity=4)
        out.leftover = float(stats[1:, cv2.CC_STAT_AREA].max()) * r * r if n > 1 else 0.0
        return out

    # -- 順番と向きの決め方 --
    def plan(self, rng: np.random.Generator, run: int, elite: Layout | None):
        """run 回目の（置く順番, 部材ごとの向きの候補）。0 回目は決まった手順、以降は乱数で変える。"""
        inst = [i for i, p in enumerate(self.parts) for _ in range(p.qty)]
        sigma = 0.0 if run == 0 else float(rng.choice([0.0, 0.25, 0.6, 1.2]))
        keys = [math.log(self.area[i] + 1e-9) + (rng.normal(0, sigma) if sigma else 0.0) for i in inst]
        order = [i for _, i in sorted(zip(keys, range(len(inst))), key=lambda t: -t[0])]
        order = [inst[k] for k in order]

        sets = []
        for i, lst in enumerate(self.orient_lists):
            if len(lst) <= self.MAX_ORIENTS:
                chosen = list(lst)
            else:
                # 多すぎるときは、90° 単位の向きと、これまでの最良の配置で使われた向きを残して間引く
                chosen = [(a, f) for a, f in lst if abs((a / 90.0) - round(a / 90.0)) < 1e-9]
                if elite is not None:
                    for pl in elite.placements:
                        if pl.part == self.parts[i].index and (pl.angle, pl.flip) not in chosen:
                            chosen.append((pl.angle, pl.flip))
                rest = [o for o in lst if o not in chosen]
                k = max(0, self.MAX_ORIENTS - len(chosen))
                if run == 0:
                    chosen += rest[:: max(1, len(rest) // max(1, k))][:k]
                else:
                    chosen += [rest[j] for j in rng.choice(len(rest), size=min(k, len(rest)), replace=False)]
            sets.append([(i, a, f) for a, f in chosen])
        return order, sets


    # -- 探索の 1 手 --
    def step(self, rng: np.random.Generator, run: int, best: Layout | None, g_offset: int = 0) -> Layout:
        """配置を 1 つ作る。最初のうちと 4 回に 1 回は一から、それ以外は最良の配置を一部こわして置き直す。

        一から作るだけだと当たりを引くまで時間がかかるので、良い配置の近くを重点的に探す（ruin & recreate）。
        """
        order, sets = self.plan(rng, run, best)
        if best is None or not best.raw or run < 2 or rng.random() < 0.25:
            return self.greedy(order, sets, (run + g_offset) % N_GRAVITY)
        raw = best.raw
        n = len(raw)
        k = int(rng.integers(1, max(2, n // 2 + 1)))
        if rng.random() < 0.6:        # 近くにある部材をまとめて外す（空いた場所がひとかたまりになる）
            ctr = np.array([[x + self._orients[key].w / 2, y + self._orients[key].h / 2] for key, x, y in raw])
            seed = int(rng.integers(n))
            drop = set(np.argsort(np.linalg.norm(ctr - ctr[seed], axis=1))[:k].tolist())
        else:
            drop = set(rng.choice(n, size=k, replace=False).tolist())
        kept = [pl for i, pl in enumerate(raw) if i not in drop]
        have: dict[int, int] = {}
        for key, _, _ in kept:
            have[key[0]] = have.get(key[0], 0) + 1
        rest = []
        for pi in order:              # 残してある個数ぶんを、置く予定から差し引く
            if have.get(pi, 0) > 0:
                have[pi] -= 1
            else:
                rest.append(pi)
        return self.greedy(rest, sets, int(rng.integers(N_GRAVITY)), fixed=kept)


# ---- 並列実行 ----------------------------------------------------------------------
#
# ブラウザでは、画面とは別の作業スレッド（Web Worker）を何本か立てて、それぞれで NestWorker を回す。
#   1. prepare(): 向きの一覧と「端材だけに対する置けない位置」を 1 回だけ計算して、まとめて渡せる形にする
#   2. 各作業スレッドで NestWorker(payload, 番号) を作り、run_for() を少しずつ呼ぶ（合間に「止めて」を受け取れる）
#   3. 画面側が各スレッドの最良の結果を集めて、いちばん良いものを採る

def prepare(container, parts: list[NestPart], params: NestParams) -> bytes:
    """作業スレッドに配るデータ。全スレッドで共通の重い計算はここで済ませる。"""
    orient_lists = [orientations(p, params.angle_step) for p in parts]
    bases = RasterNester(container, parts, params, orient_lists).all_bases()
    return pickle.dumps({"container": container, "parts": parts, "params": params,
                         "orient_lists": orient_lists, "bases": bases}, protocol=pickle.HIGHEST_PROTOCOL)


class NestWorker:
    """1 本の作業スレッドで回す探索。番号ごとに乱数と寄せる方向を変えて、スレッドどうしで違う配置を探す。"""

    def __init__(self, payload: bytes, wid: int):
        d = pickle.loads(payload)
        if not IN_BROWSER:
            cv2.setNumThreads(1)          # 並列はスレッドの数でするので、各スレッドの中は 1 本にする
        self.nester = RasterNester(d["container"], d["parts"], d["params"], d["orient_lists"], d["bases"])
        self.rng = np.random.default_rng(d["params"].seed * 1000 + wid)
        self.wid = wid
        self.best: Layout | None = None
        self.runs = 0

    def run_for(self, seconds: float) -> dict:
        """少なくとも 1 回、seconds 秒たつまで探す。戻り値: {"runs": これまでの回数, "best": 良くなったときだけ結果}。"""
        t_end = time.time() + seconds
        improved = False
        while True:
            run = self.runs if self.wid == 0 else self.runs + 1
            layout = self.nester.step(self.rng, run, self.best, g_offset=self.wid * 3)
            self.runs += 1
            if self.best is None or layout.key() > self.best.key():
                self.best = layout
                improved = True
            if time.time() >= t_end:
                break
        return {"runs": self.runs, "best": self.best.to_dict() if improved else None}


def placements_from_dicts(items) -> list[Placed]:
    return [Placed(part=int(d["part"]), flip=bool(d["flip"]), angle=float(d["angle"]), tx=float(d["tx"]), ty=float(d["ty"]))
            for d in items]


def nest_once(container, parts: list[NestPart], params: NestParams) -> Layout:
    """このプロセスだけで計算する（テストや、部材が少ないとき用）。"""
    nester = RasterNester(container, parts, params)
    rng = np.random.default_rng(params.seed)
    best, run = None, 0
    t_end = time.time() + params.time_limit
    while True:
        layout = nester.step(rng, run, best)
        run += 1
        if best is None or layout.key() > best.key():
            best = layout
        if time.time() >= t_end:
            return best
