"""写真 → 寸法つきの輪郭、の検査。正解がわかっている合成写真（make_synthetic.py）と、実際のテスト写真を使う。"""
from pathlib import Path

import cv2
import numpy as np
import pytest
from shapely import affinity
from shapely.geometry import Polygon

import make_synthetic as syn
from hazaism.geom import outline as go
from hazaism.vision import grid as vgrid
from hazaism.vision import outline as voutline
from hazaism.vision import segment as vsegment

PHOTO = Path(__file__).resolve().parent / "data" / "scrap_sample_01.png"


class Scene:
    """合成写真を 1 枚解析したもの。"""

    def __init__(self, **kw):
        self.photo, self.truth_mat, self.cam = syn.make(**kw)
        self.grid = vgrid.detect_grid(self.photo)
        self.rect = vsegment.rectify(self.photo, self.grid)
        self.seg = vsegment.segment(self.rect, self.grid)
        self.pose = vgrid.camera_pose(self.grid)

    def to_app(self, pts_mat) -> np.ndarray:
        """マット上の点（mm、y 下向き）→ アプリ内の座標（mm、y 上向き）。推定した格子を通す。"""
        return self.rect.grid_to_mm(self.grid.to_grid(self.cam.project(np.asarray(pts_mat, float))))

    def offset(self) -> np.ndarray:
        """マット座標（y を反転したもの）からアプリ座標への平行移動。"""
        pts = np.array([[x, y] for x in np.arange(40, 420, 40.0) for y in np.arange(60, 560, 50.0)])
        q = self.to_app(pts)
        return (q - pts * [1, -1]).mean(0)

    def truth(self) -> Polygon:
        """正解の外形を、アプリ内の座標にしたもの（格子の推定誤差を含まない: 平行移動だけ合わせる）。"""
        t = self.offset()
        return affinity.translate(affinity.scale(self.truth_mat, xfact=1, yfact=-1, origin=(0, 0)), t[0], t[1])

    def outlines(self, thickness: float):
        nadir = self.rect.grid_to_mm([[self.pose["x_mm"] / 10, self.pose["y_mm"] / 10]])[0]
        return voutline.extract_outlines(self.seg, self.rect, nadir_mm=nadir, cam_height_mm=self.pose["height_mm"],
                                         thickness_mm=thickness)


def hausdorff(a, b) -> float:
    return a.boundary.segmentize(0.2).hausdorff_distance(b.boundary.segmentize(0.2))


def outside(est, truth) -> float:
    """est が truth からはみ出している最大の距離（板を大きく見積もった量）。"""
    pts = np.asarray(est.exterior.segmentize(0.3).coords)
    from shapely import contains_xy, distance, points
    out = ~contains_xy(truth, pts[:, 0], pts[:, 1])
    return float(distance(points(pts[out]), truth.boundary).max()) if out.any() else 0.0


@pytest.fixture(scope="module")
def flat():
    """厚みのない板を、少し斜めから。"""
    return Scene(thickness=0.0, tilt_deg=18.0, yaw_deg=7.0)


@pytest.fixture(scope="module")
def thick():
    """厚さ 3 mm の板を、25° 斜めから。"""
    return Scene(thickness=3.0, tilt_deg=25.0, yaw_deg=-5.0)


def test_grid_scale_and_orientation(flat):
    g = flat.grid
    assert g.rms_mm < 0.12
    assert not vgrid.is_mirrored(g.H, *g.image_size)
    # マット上の点どうしの位置関係が、平行移動だけで再現される（＝縮尺が合い、回転も鏡像もしていない）
    pts = np.array([[x, y] for x in np.arange(30, 430, 25.0) for y in np.arange(30, 580, 25.0)])
    q = flat.to_app(pts)
    resid = q - pts * [1, -1] - flat.offset()
    assert np.abs(resid).max() < 0.2, f"格子の対応がずれている: 最大 {np.abs(resid).max():.3f} mm"
    # 太線（50 mm ごと）が 5 の倍数の番号になっている
    u = g.to_grid(flat.cam.project(np.array([[20.0, 300.0], [70.0, 300.0]])))[:, 0]
    assert np.allclose(np.mod(u + 0.02, 5), 0.02, atol=0.03)


def test_camera_pose(flat, thick):
    for sc in (flat, thick):
        assert sc.pose["tilt_deg"] == pytest.approx(sc.cam.tilt_deg, abs=1.5)
        assert sc.pose["height_mm"] == pytest.approx(sc.cam.C[2], rel=0.04)


def test_outline_flat(flat):
    cands = flat.outlines(0.0)
    assert len(cands) == 1 and not cands[0].touches_edge
    shape = cands[0].shape
    assert not go.validate(shape)
    est, truth = go.shape_to_polygon(shape, 0.02), flat.truth()
    assert len(est.interiors) == 2, "穴が 2 つ見つかるはず"
    d = hausdorff(est, truth)
    assert d < 0.5, f"輪郭のずれ {d:.2f} mm"
    assert est.area == pytest.approx(truth.area, rel=0.01)
    # 直線と円弧にまとまっている（細かい折れ線になっていない）
    assert len(shape["outer"]) <= 16
    assert any(abs(p[2]) > 0.05 for p in shape["outer"]), "えぐった角が円弧になっていない"


def test_outline_is_not_mirrored(flat):
    est = go.shape_to_polygon(flat.outlines(0.0)[0].shape, 0.05)
    truth = flat.truth()
    mirrored = affinity.scale(truth, xfact=-1, yfact=1, origin=truth.centroid)
    assert est.symmetric_difference(truth).area < 0.02 * truth.area
    assert est.symmetric_difference(mirrored).area > 0.15 * truth.area     # 鏡像とは重ならない形で検査している


def test_thickness_correction(thick):
    truth = thick.truth()
    raw = go.shape_to_polygon(thick.outlines(0.0)[0].shape, 0.02)
    fixed = go.shape_to_polygon(thick.outlines(3.0)[0].shape, 0.02)
    # 補正なしでは、奥側の縁が外へずれて写るぶん板を大きく見積もる
    assert outside(raw, truth) > 0.8
    # 補正すると、はみ出しがほぼなくなる（板を大きく見積もらない）
    assert outside(fixed, truth) < 0.45, f"補正後のはみ出し {outside(fixed, truth):.2f} mm"
    assert hausdorff(fixed, truth) < 1.5
    assert fixed.area <= truth.area * 1.003


def test_bright_plate():
    """明るい（銀色の）板でも同じように取れる。色ではなく格子の隠れ方で見つけているため。"""
    sc = Scene(thickness=0.0, tilt_deg=15.0, yaw_deg=3.0, top_gray=228, seed=3)
    cands = sc.outlines(0.0)
    assert len(cands) == 1
    d = hausdorff(go.shape_to_polygon(cands[0].shape, 0.02), sc.truth())
    assert d < 0.6, f"輪郭のずれ {d:.2f} mm"


@pytest.mark.skipif(not PHOTO.exists(), reason="tests/data/scrap_sample_01.png がありません（公開リポジトリには入れていない）")
def test_real_photo():
    img = cv2.imdecode(np.fromfile(str(PHOTO), np.uint8), cv2.IMREAD_COLOR)
    g = vgrid.detect_grid(img)
    assert g.rms_mm < 0.3
    assert g.u_range[1] - g.u_range[0] >= 30 and g.v_range[1] - g.v_range[0] >= 40
    rect = vsegment.rectify(img, g)
    cands = voutline.extract_outlines(vsegment.segment(rect, g), rect)
    assert cands, "板が見つからない"
    main = cands[0]
    assert 190e2 < main.area_mm2 < 215e2            # およそ 200 cm2 の板
    assert main.touches_edge                          # この写真は板の下端が写真からはみ出している
    assert not go.validate(main.shape)
    assert len(main.shape["outer"]) < 120
    minx, miny, maxx, maxy = go.shape_bounds(main.shape)
    assert 255 < maxx - minx < 290 and 300 < maxy - miny < 330


def test_manual_calibration(flat):
    """格子の交点 4 つとマス数からでも、同じ縮尺になる。"""
    mat = np.array([[60.0, 100.0], [360.0, 100.0], [360.0, 500.0], [60.0, 500.0]])      # 30 × 40 マスの四隅
    px = flat.cam.project(mat)
    h, w = flat.photo.shape[:2]
    g = vgrid.grid_from_points(px, 30, 40, (w, h))
    q = g.to_grid(flat.cam.project(np.array([[100.0, 200.0], [300.0, 450.0]]))) * 10
    assert np.linalg.norm(q[1] - q[0]) == pytest.approx(np.hypot(200, 250), abs=0.5)
    g2 = vgrid.refine_manual(flat.photo, g)
    assert g2.samples is not None and g2.rms_mm < 0.15
    # 点の順番が逆（鏡像）なら断る
    with pytest.raises(vgrid.GridNotFound):
        vgrid.grid_from_points(px[::-1], 30, 40, (w, h))
