"""DXF の読込と出力の検査。"""
import math
from pathlib import Path

import ezdxf
import pytest
from shapely import affinity

from scrapnest.dxfio import reader, writer

PARTS = Path(__file__).resolve().parent / "data" / "parts"


@pytest.fixture(scope="module", autouse=True)
def _make_parts():
    import make_parts
    make_parts.ensure()


def load(name, unit=None):
    return reader.load_part(str(PARTS / name), name, unit)


def test_bracket_area_and_holes():
    p = load("bracket_L.dxf")
    r = 4.0
    # L 字の面積から、外側の角丸 5 か所ぶんを引き、内側の角丸 1 か所ぶんを足し、穴 3 つを引く
    corner = r * r - math.pi * r * r / 4
    expect = 60 * 20 + 20 * 25 - 5 * corner + corner - 3 * math.pi * 2.6 ** 2
    assert p.area == pytest.approx(expect, abs=0.5)      # 曲線は 0.02 mm の折れ線にしている
    assert len(p.polygon.geoms) == 1 and len(p.polygon.geoms[0].interiors) == 3
    assert not p.warnings


def test_flange():
    p = load("flange.dxf")
    assert p.area == pytest.approx(math.pi * (30 ** 2 - 11 ** 2 - 6 * 2.2 ** 2), abs=1.5)
    assert len(p.polygon.geoms[0].interiors) == 7


def test_gusset_joins_near_endpoints():
    p = load("gusset.dxf")
    assert len(p.polygon.geoms) == 1
    assert p.area == pytest.approx(45 * 40 - 0.5 * 37 * 32 - math.pi * 2.1 ** 2, abs=0.3)


def test_units_cm():
    p = load("cover_cm.dxf")
    assert p.unit == "cm"
    minx, miny, maxx, maxy = p.bounds
    assert maxx - minx == pytest.approx(70.0, abs=0.05)
    assert maxy - miny == pytest.approx(42.0, abs=0.05)
    reader.set_unit(p, "mm")
    assert p.bounds[2] - p.bounds[0] == pytest.approx(7.0, abs=0.01)


def test_mirrored_arc_is_read_upright():
    p = load("mirrored_arc.dxf")
    assert p.area == pytest.approx(30 * 12 + math.pi * 15 ** 2 / 2, abs=1.0)
    assert p.bounds == pytest.approx((0, 0, 30, 27), abs=0.02)


def test_open_path_is_reported(tmp_path):
    doc = ezdxf.new("R2010")
    doc.units = ezdxf.units.MM          # ezdxf の新規図面は既定で m なので明示する
    msp = doc.modelspace()
    msp.add_circle((0, 0), 10)
    msp.add_line((20, 0), (30, 0))       # どこにもつながらない線
    msp.add_text("memo")
    f = tmp_path / "open.dxf"
    doc.saveas(f)
    p = reader.load_part(str(f), "open")
    assert any("閉じていない" in w for w in p.warnings)
    assert any("TEXT" in w for w in p.warnings)
    assert p.area == pytest.approx(math.pi * 100, abs=0.5)


def test_no_closed_shape(tmp_path):
    doc = ezdxf.new("R2010")
    doc.modelspace().add_line((0, 0), (10, 0))
    f = tmp_path / "line.dxf"
    doc.saveas(f)
    with pytest.raises(reader.DxfError):
        reader.load_part(str(f), "line")


def expected_polygon(part, pl, origin=(0, 0), angle=0.0):
    g = part.polygon
    if pl.flip:
        g = affinity.scale(g, xfact=-1, yfact=1, origin=(0, 0))
    g = affinity.rotate(g, pl.angle, origin=(0, 0))
    g = affinity.translate(g, pl.tx, pl.ty)
    g = affinity.translate(g, -origin[0], -origin[1])
    return affinity.rotate(g, -angle, origin=(0, 0))


@pytest.mark.parametrize("name", ["bracket_L.dxf", "flange.dxf", "slot_plate.dxf", "gusset.dxf", "cover_cm.dxf", "mirrored_arc.dxf"])
@pytest.mark.parametrize("flip", [False, True])
def test_roundtrip_keeps_shape(tmp_path, name, flip):
    part = load(name)
    pl = writer.Placement(part=0, flip=flip, angle=37.0, tx=123.4, ty=-56.7)
    out = tmp_path / "out.dxf"
    writer.write_layout(str(out), [part], [pl], origin=(10.0, 20.0), angle_deg=15.0)
    back = reader.load_part(str(out), "out")
    assert back.unit == "mm"
    exp = expected_polygon(part, pl, origin=(10.0, 20.0), angle=15.0)
    # 曲線は読むたびに 0.02 mm の折れ線になるので、面積ではなく輪郭どうしの距離で比べる
    dist = back.polygon.boundary.hausdorff_distance(exp.boundary)
    assert dist < 0.06, f"形が変わった: 輪郭のずれ {dist:.3f} mm"
    assert back.area == pytest.approx(exp.area, rel=2e-3)

    doc = ezdxf.readfile(str(out))
    src = ezdxf.readfile(str(PARTS / name))
    kinds = lambda d: sorted(e.dxftype() for e in d.modelspace())
    assert kinds(doc) == kinds(src), "要素の種類が保たれていない（円弧が折れ線になった等）"
    for e in doc.modelspace():
        if e.dxf.hasattr("extrusion"):
            assert tuple(round(v, 9) for v in e.dxf.extrusion) == (0, 0, 1), f"{e.dxftype()} の押し出し方向が +Z でない"
    assert doc.units == ezdxf.units.MM
    assert len(doc.audit().errors) == 0


def test_flip_really_mirrors(tmp_path):
    """反転した L 字は、回転だけでは元に重ならない（＝本当に鏡像になっている）。"""
    part = load("bracket_L.dxf")
    out = tmp_path / "f.dxf"
    writer.write_layout(str(out), [part], [writer.Placement(0, True, 0.0, 0.0, 0.0)])
    back = reader.load_part(str(out), "f").polygon
    c = back.centroid
    best = min(
        back.symmetric_difference(affinity.translate(
            affinity.rotate(part.polygon, a, origin="centroid"),
            c.x - part.polygon.centroid.x, c.y - part.polygon.centroid.y)).area
        for a in range(0, 360, 5))
    assert best > 50.0


def test_scrap_outline_layer(tmp_path):
    part = load("flange.dxf")
    shape = {"outer": [(0, 0, 0), (100, 0, 0), (100, 80, 0.4142135623730951), (0, 80, 0)], "holes": []}
    out = tmp_path / "s.dxf"
    writer.write_layout(str(out), [part], [writer.Placement(0, False, 0, 50, 40)], scrap_shape=shape)
    doc = ezdxf.readfile(str(out))
    scrap = [e for e in doc.modelspace() if e.dxf.layer == writer.SCRAP_LAYER]
    assert len(scrap) == 1 and scrap[0].dxftype() == "LWPOLYLINE" and scrap[0].closed
    assert scrap[0].dxf.color == 5, "輪郭の線には、レイヤー任せでなく直接色を付ける"
    writer.write_layout(str(out), [part], [writer.Placement(0, False, 0, 50, 40)], scrap_shape=shape, scrap_color=1)
    assert [e.dxf.color for e in ezdxf.readfile(str(out)).modelspace() if e.dxf.layer == writer.SCRAP_LAYER] == [1]
    assert any(abs(b) > 0.1 for *_, b in scrap[0].get_points("xyb"))


def test_entity_colors():
    p = load("bracket_L.dxf")          # レイヤー CUT（色 1）に BYLAYER の線
    assert writer.entity_colors(p) == {1}
    assert writer.entity_colors(load("flange.dxf")) == {7}
