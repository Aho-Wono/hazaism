"""真上視点への補正と、板の領域の切り出し。"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .grid import Grid

MAX_RECT_PIXELS = 16_000_000


@dataclass
class Rectified:
    """真上から見た図に直した写真。

    アプリ内の座標は CAD と同じ「mm・x 右・y 上」で、この画像は x∈[0, width_mm], y∈[0, height_mm] に置かれる
    （画像の 1 行目が y = height_mm）。
    """
    image: np.ndarray          # BGR
    valid: np.ndarray          # 写真に写っていた範囲（uint8 0/255）
    px_per_mm: float
    u0: float                  # 画像左上の格子座標（マス単位）
    v0: float
    pitch_mm: float

    @property
    def width_mm(self) -> float:
        return self.image.shape[1] / self.px_per_mm

    @property
    def height_mm(self) -> float:
        return self.image.shape[0] / self.px_per_mm

    def px_to_mm(self, pts) -> np.ndarray:
        pts = np.asarray(pts, float).reshape(-1, 2)
        return np.c_[pts[:, 0] / self.px_per_mm, self.height_mm - pts[:, 1] / self.px_per_mm]

    def mm_to_px(self, pts) -> np.ndarray:
        pts = np.asarray(pts, float).reshape(-1, 2)
        return np.c_[pts[:, 0] * self.px_per_mm, (self.height_mm - pts[:, 1]) * self.px_per_mm]

    def grid_to_px(self, pts) -> np.ndarray:
        pts = np.asarray(pts, float).reshape(-1, 2)
        k = self.pitch_mm * self.px_per_mm
        return np.c_[(pts[:, 0] - self.u0) * k, (pts[:, 1] - self.v0) * k]

    def px_to_grid(self, pts) -> np.ndarray:
        pts = np.asarray(pts, float).reshape(-1, 2)
        k = self.pitch_mm * self.px_per_mm
        return np.c_[pts[:, 0] / k + self.u0, pts[:, 1] / k + self.v0]

    def mm_to_grid(self, pts) -> np.ndarray:
        return self.px_to_grid(self.mm_to_px(pts))

    def grid_to_mm(self, pts) -> np.ndarray:
        return self.px_to_mm(self.grid_to_px(pts))


def _upsample(coarse: np.ndarray, step: int, W: int, H: int) -> np.ndarray:
    """step 画素おきの値（coarse[j, i] は画素 (i*step, j*step) の値）を双一次補間で全画素に広げる。"""
    x = np.arange(W) / step
    i0 = np.floor(x).astype(int)
    fx = x - i0
    rows = coarse[:, i0] * (1 - fx) + coarse[:, i0 + 1] * fx
    y = np.arange(H) / step
    j0 = np.floor(y).astype(int)
    fy = (y - j0)[:, None]
    return (rows[j0] * (1 - fy) + rows[j0 + 1] * fy).astype(np.float32)


def rectify(img_bgr: np.ndarray, grid: Grid, px_per_mm: float = 5.0, margin_cells: float = 0.5) -> Rectified:
    u0, u1 = grid.u_range[0] - margin_cells, grid.u_range[1] + margin_cells
    v0, v1 = grid.v_range[0] - margin_cells, grid.v_range[1] + margin_cells
    w_mm, h_mm = (u1 - u0) * grid.pitch_mm, (v1 - v0) * grid.pitch_mm
    px_per_mm = min(px_per_mm, (MAX_RECT_PIXELS / (w_mm * h_mm)) ** 0.5)
    W, H = int(round(w_mm * px_per_mm)), int(round(h_mm * px_per_mm))
    k = grid.pitch_mm * px_per_mm

    # 写像はなめらかなので、粗い網目で計算して補間する。
    # 補正後の画素 (c, r) の中心は連続座標で (c + 0.5, r + 0.5)。写真側は「画素の番号＝中心」の座標。
    step = 8
    gx = np.arange(0, W + step, step, dtype=float)
    gy = np.arange(0, H + step, step, dtype=float)
    GX, GY = np.meshgrid(gx, gy)
    g = np.c_[(GX.reshape(-1) + 0.5) / k + u0, (GY.reshape(-1) + 0.5) / k + v0]
    xy = grid.to_image(g).reshape(len(gy), len(gx), 2)
    mapx = _upsample(xy[..., 0], step, W, H)
    mapy = _upsample(xy[..., 1], step, W, H)
    image = cv2.remap(img_bgr, mapx, mapy, cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))
    ih, iw = img_bgr.shape[:2]
    valid = ((mapx >= 0) & (mapx <= iw - 1) & (mapy >= 0) & (mapy <= ih - 1)).astype(np.uint8) * 255
    return Rectified(image=image, valid=valid, px_per_mm=px_per_mm, u0=u0, v0=v0, pitch_mm=grid.pitch_mm)


# ---- 板の切り出し ------------------------------------------------------------------
#
# 板は鏡面反射で色が定まらないので、「あるはずの格子線が見えない」ことを主な手がかりにする。
#   1. 格子線の位置を調べて「線が見える（マット）」「線がない（板）」を決め、前景・背景の種にする
#   2. 格子線を画像から消す（マットを一様な色にする）
#   3. 種と粗い被覆領域を初期値にして GrabCut で境界を決める

@dataclass
class Segmentation:
    mask: np.ndarray       # uint8 0/255。板らしい領域（写り込んだ他の物も含む）
    nolines: np.ndarray    # 格子線を消した補正画像（縁の位置合わせに使う）
    lines_x: list | None = None       # 縦の格子線: (中心の列, 消した半分の幅)。線の下では縁の位置がわからない
    lines_y: list | None = None       # 横の格子線: (中心の行, 消した半分の幅)


def _erode1d(x: np.ndarray, n: int) -> np.ndarray:
    """True の連続区間を、両端から n 個ずつ削る。"""
    return np.convolve(x.astype(np.float32), np.ones(2 * n + 1, np.float32), "same") > 2 * n + 0.5


def _line_evidence(rect: Rectified, grid: Grid):
    """補正画像の格子線の位置を 1 画素ずつ調べて、「線が見えている（＝マット）」「線がない（＝何かが載っている）」を決める。

    線の中心 C と、線の両脇（1 mm 離れた所）A, B の明るさを比べる:
      - C が両脇より暗い             → 線が見えている
      - A, B, C がどれも同じくらい   → 線がない（板に隠れている）
      - 両脇の明るさが違う           → 板の縁の近く。どちらとも決めない
    線が擦れて消えたマットを板と間違えないように、「線がない」うえに色がマットと違う所だけを確実な前景にする。

    戻り値: bg_seed（確実にマット）, fg_seed（確実に何かが載っている）, hidden（線がない画素）, visible（線が見える画素）
    """
    ppm = rect.px_per_mm
    cell = rect.pitch_mm * ppm
    H, W = rect.image.shape[:2]
    gray0 = cv2.GaussianBlur(cv2.cvtColor(rect.image, cv2.COLOR_BGR2GRAY), (0, 0), 1.0).astype(np.float32)
    valid0 = cv2.erode(rect.valid, np.ones((7, 7), np.uint8))
    out = {name: np.zeros((H, W), np.uint8) for name in ("bg", "fg", "hidden", "visible")}
    off = max(4, int(round(1.0 * ppm)))        # 脇を見る位置（線の中心から 1 mm）
    trim = max(2, int(round(0.6 * ppm)))       # 種にするときに区間の両端から削る長さ（0.6 mm）
    ranges = {1: (grid.u_range, rect.u0), 0: (grid.v_range, rect.v0)}

    for axis in (1, 0):
        # axis=1: 縦線、axis=0: 横線。横線は転置して、どちらも「列」として扱う
        tr = (lambda a: a) if axis == 1 else (lambda a: a.T)
        gray, valid = tr(gray0), tr(valid0)
        img = rect.image if axis == 1 else rect.image.transpose(1, 0, 2)
        n = gray.shape[1]
        (k0, k1), origin = ranges[axis]
        (j0, j1), jorigin = ranges[1 - axis]
        # 交差する線の近くは、脇も線になるので判定しない
        cross = np.zeros(gray.shape[0], bool)
        for j in range(j0, j1 + 1):
            cj = int(round((j - jorigin) * cell - 0.5))
            cross[max(0, cj - off - 3):cj + off + 4] = True

        rows = []
        for k in range(k0, k1 + 1):
            ci = int(round((k - origin) * cell - 0.5))
            if ci - off - 1 < 0 or ci + off + 2 > n:
                continue
            C = gray[:, ci - 1:ci + 2].min(axis=1)
            A = gray[:, ci - off - 1:ci - off + 2].mean(axis=1)
            B = gray[:, ci + off - 1:ci + off + 2].mean(axis=1)
            ok = (valid[:, ci] > 0) & ~cross
            rows.append((ci, A, B, np.minimum(A, B) - C, ok))
        if not rows:
            continue
        typical = [float(np.percentile(c[ok], 75)) for _, _, _, c, ok in rows if ok.sum() > 20]
        fam = float(np.median(typical)) if typical else 30.0
        mats = []
        for ci, A, B, contrast, ok in rows:
            cl = float(np.percentile(contrast[ok], 75)) if ok.sum() > 20 else fam
            T = max(8.0, 0.4 * (cl if cl > 0.5 * fam else fam))
            vis = ok & (contrast >= T)
            flat = ok & (np.abs(A - B) < 0.5 * T) & (np.abs(contrast) < 0.33 * T)
            mats.append((ci, vis, flat, T))
        # マットの色: 見えている線の両脇の色
        samples = [img[vis, ci - off] for ci, vis, _, _ in mats if vis.sum() > 20] + \
                  [img[vis, ci + off] for ci, vis, _, _ in mats if vis.sum() > 20]
        mat_color = np.median(np.vstack(samples), axis=0) if samples else None
        o = {name: tr(v) for name, v in out.items()}
        for ci, vis, flat, T in mats:
            strong = flat
            if mat_color is not None:
                diff = np.abs(img[:, ci].astype(np.float32) - mat_color).max(axis=1)
                strong = flat & (diff > 22)
            sl = slice(ci - 1, ci + 2)
            for name, m in (("visible", vis), ("hidden", flat), ("bg", _erode1d(vis, trim)), ("fg", _erode1d(strong, trim))):
                o[name][m, sl] = 255
    return out["bg"], out["fg"], out["hidden"], out["visible"]


def erase_grid_lines(rect: Rectified, grid: Grid) -> tuple[np.ndarray, list, list]:
    """格子線を画像から消す。戻り値: (線を消した画像, 縦線の一覧, 横線の一覧)。線は (中心の画素, 置き換えた半分の幅)。

    線の位置は格子からわかっているので、線の幅ぶんの帯を、帯の両外側の画素の間を線に垂直な向きに補間した値で
    置き換える。両側がマットならマットに、両側が板なら板になる。板の縁がちょうど帯の中を通っている場所は
    縁の位置がわからなくなる（線に隠れて見えない）ので、線の一覧を返して、縁を探すときに考慮できるようにする。
    """
    ppm = rect.px_per_mm
    cell = rect.pitch_mm * ppm
    out = rect.image.astype(np.float32)
    lines = {1: [], 0: []}
    max_hw = max(2, int(round(0.7 * ppm)))        # 線の半分の幅の上限（太線でも 1.4 mm 幅まで）
    R = max_hw + 3

    for axis, (k0, k1), origin in ((1, grid.u_range, rect.u0), (0, grid.v_range, rect.v0)):
        # axis=1: 縦線（列を置き換える）、axis=0: 横線（行を置き換える）。どちらも「列」として扱う（ビューなので元が書き換わる）
        img = out if axis == 1 else out.transpose(1, 0, 2)
        n = img.shape[1]
        gray = img.mean(axis=2)
        centers, widths = {}, {}
        for k in range(k0, k1 + 1):
            ci = int(round((k - origin) * cell - 0.5))
            if ci - R < 0 or ci + R >= n:
                continue
            centers[k] = ci
            prof = np.median(gray[:, ci - R:ci + R + 1], axis=0)     # 線に沿って中央値をとった、線を横切る明るさ
            dark = max(prof[0], prof[-1]) - prof
            if dark.max() > 12:
                idx = np.where(dark > 0.25 * dark.max())[0]
                widths[k] = int(np.abs(idx - R).max()) + 1
        default = int(np.median(list(widths.values()))) if widths else max(1, int(round(0.3 * ppm))) + 1
        for k, ci in centers.items():
            hw = min(widths.get(k, default), max_hw)
            a, b = ci - hw - 1, ci + hw + 1
            left, right = img[:, a].copy(), img[:, b].copy()
            for x in range(a + 1, b):
                t = (x - a) / (b - a)
                img[:, x] = left * (1 - t) + right * t
            lines[axis].append((ci, hw))
    return np.clip(out, 0, 255).astype(np.uint8), lines[1], lines[0]


def _within(mask: np.ndarray, r: float) -> np.ndarray:
    """mask（0 以外が対象）から距離 r 画素以内の画素（＝半径 r の円で太らせたもの）。

    大きな円での cv2.dilate は、かかる時間が円の大きさに比例する。ブラウザ（WebAssembly）では高速化の命令が
    使えず数十秒かかったので、時間が円の大きさによらない距離変換で同じことをする。
    """
    return cv2.distanceTransform((mask == 0).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE) <= r


def _inside(mask: np.ndarray, r: float) -> np.ndarray:
    """mask の縁から r 画素より内側の画素（＝半径 r の円で細らせたもの）。"""
    return cv2.distanceTransform((mask != 0).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE) > r


def segment(rect: Rectified, grid: Grid, sensitivity: float = 0.5) -> Segmentation:
    """補正画像から「マットの上に何かが載っている」領域を切り出す。sensitivity は 0〜1（大きいほど拾いやすい）。"""
    H, W = rect.image.shape[:2]
    if grid.samples is None:
        return Segmentation(mask=np.zeros((H, W), np.uint8), nolines=rect.image)
    cell = rect.pitch_mm * rect.px_per_mm
    bg_seed, fg_seed, hidden, visible = _line_evidence(rect, grid)
    c = int(round(cell))
    nolines, lines_x, lines_y = erase_grid_lines(rect, grid)

    # 粗い被覆領域: 近くの測定点のうち隠れているものの割合
    sc = 8.0 / cell
    ws, hs = max(8, int(round(W * sc))), max(8, int(round(H * sc)))
    hid_s = cv2.resize(hidden, (ws, hs), interpolation=cv2.INTER_AREA).astype(np.float32)
    vis_s = cv2.resize(visible, (ws, hs), interpolation=cv2.INTER_AREA).astype(np.float32)
    num = cv2.blur(hid_s, (13, 13))
    den = cv2.blur(hid_s + vis_s, (13, 13))
    ratio = np.where(den > 1e-3, num / np.maximum(den, 1e-3), 0.0)
    thr = float(np.clip(0.7 - 0.4 * sensitivity, 0.2, 0.8))
    coarse = cv2.resize((ratio > thr).astype(np.uint8) * 255, (W, H), interpolation=cv2.INTER_LINEAR) > 127

    allowed = _within(hidden, 1.5 * c)        # 隠れた線から 1.5 マス以内だけを候補にする
    core = _inside(coarse, c)

    gc = np.full((H, W), cv2.GC_BGD, np.uint8)
    gc[allowed] = cv2.GC_PR_BGD
    gc[coarse & allowed] = cv2.GC_PR_FGD
    gc[core] = cv2.GC_FGD
    gc[fg_seed > 0] = cv2.GC_FGD
    gc[bg_seed > 0] = cv2.GC_BGD
    gc[rect.valid == 0] = cv2.GC_BGD
    if not ((gc == cv2.GC_FGD) | (gc == cv2.GC_PR_FGD)).any():
        return Segmentation(mask=np.zeros((H, W), np.uint8), nolines=nolines, lines_x=lines_x, lines_y=lines_y)

    # GrabCut は重いので、候補のある範囲だけを半分の解像度（0.4 mm/px 程度）で解く。細かい位置は後で縁に合わせる
    ys, xs = np.where(allowed)
    x0, x1 = max(0, xs.min() - c), min(W, xs.max() + c + 1)
    y0, y1 = max(0, ys.min() - c), min(H, ys.max() + c + 1)
    f = min(1.0, 2.5 / rect.px_per_mm)
    roi = nolines[y0:y1, x0:x1]
    small = cv2.resize(roi, None, fx=f, fy=f, interpolation=cv2.INTER_AREA) if f < 1 else roi.copy()
    size = (small.shape[1], small.shape[0])
    gs = cv2.resize(gc[y0:y1, x0:x1], size, interpolation=cv2.INTER_NEAREST)
    for seed, val in ((fg_seed, cv2.GC_FGD), (bg_seed, cv2.GC_BGD)):   # 細い種が縮小で消えないように
        gs[cv2.resize(seed[y0:y1, x0:x1], size, interpolation=cv2.INTER_AREA) > 100] = val
    gs[cv2.resize(rect.valid[y0:y1, x0:x1], size, interpolation=cv2.INTER_NEAREST) == 0] = cv2.GC_BGD
    bgd, fgd = np.zeros((1, 65)), np.zeros((1, 65))
    # GrabCut は OpenCV 共通の乱数で初期値を決めるので、先に何を動かしたかで結果が変わってしまう。種を固定する
    cv2.setRNGSeed(20261111)
    try:
        cv2.grabCut(small, gs, None, bgd, fgd, 3, cv2.GC_INIT_WITH_MASK)
    except cv2.error:
        pass
    res = ((gs == cv2.GC_FGD) | (gs == cv2.GC_PR_FGD)).astype(np.uint8) * 255
    res = cv2.morphologyEx(res, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    mask = np.zeros((H, W), np.uint8)
    mask[y0:y1, x0:x1] = (cv2.resize(res, (x1 - x0, y1 - y0), interpolation=cv2.INTER_LINEAR) > 127) * 255
    mask[rect.valid == 0] = 0
    return Segmentation(mask=mask, nolines=nolines, lines_x=lines_x, lines_y=lines_y)
