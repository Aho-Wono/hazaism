"""テスト用の部材 DXF を tests/data/parts に作る。

usage: uv run python tests/make_parts.py
"""
import math
from pathlib import Path

import ezdxf

OUT = Path(__file__).resolve().parent / "data" / "parts"


def new(units=ezdxf.units.MM):
    doc = ezdxf.new("R2010", setup=False)
    doc.units = units
    return doc, doc.modelspace()


def bracket_l():
    """L 字ブラケット。ばらばらの LINE と ARC（角丸）＋ CIRCLE の穴。左右非対称なので反転の検査に使う。"""
    doc, msp = new()
    doc.layers.add("CUT", color=1)
    a = {"layer": "CUT"}
    r = 4.0
    # 外形（反時計回り）: (0,0) → (60,0) → (60,20) → (20,20) → (20,45) → (0,45)
    msp.add_line((r, 0), (60 - r, 0), dxfattribs=a)
    msp.add_arc((60 - r, r), r, 270, 0, dxfattribs=a)
    msp.add_line((60, r), (60, 20 - r), dxfattribs=a)
    msp.add_arc((60 - r, 20 - r), r, 0, 90, dxfattribs=a)
    msp.add_line((60 - r, 20), (20 + r, 20), dxfattribs=a)
    msp.add_arc((20 + r, 20 + r), r, 180, 270, dxfattribs=a)      # 内側の角
    msp.add_line((20, 20 + r), (20, 45 - r), dxfattribs=a)
    msp.add_arc((20 - r, 45 - r), r, 0, 90, dxfattribs=a)
    msp.add_line((20 - r, 45), (r, 45), dxfattribs=a)
    msp.add_arc((r, 45 - r), r, 90, 180, dxfattribs=a)
    msp.add_line((0, 45 - r), (0, r), dxfattribs=a)
    msp.add_arc((r, r), r, 180, 270, dxfattribs=a)
    for c in ((10, 10), (50, 10), (10, 35)):
        msp.add_circle(c, 2.6, dxfattribs=a)
    doc.saveas(OUT / "bracket_L.dxf")


def flange():
    """丸フランジ。CIRCLE だけ。"""
    doc, msp = new()
    msp.add_circle((0, 0), 30)
    msp.add_circle((0, 0), 11)
    for k in range(6):
        t = math.radians(60 * k)
        msp.add_circle((22 * math.cos(t), 22 * math.sin(t)), 2.2)
    doc.saveas(OUT / "flange.dxf")


def slot_plate():
    """角丸の板＋長穴。バルジつきの閉じた LWPOLYLINE。"""
    doc, msp = new()
    w, h, r = 80.0, 25.0, 5.0
    b = math.tan(math.pi / 8)       # 90° の円弧
    pts = [(r, 0, 0), (w - r, 0, b), (w, r, 0), (w, h - r, b), (w - r, h, 0), (r, h, b), (0, h - r, 0), (0, r, b)]
    msp.add_lwpolyline(pts, format="xyb", close=True)
    # 長穴（両端が半円）
    msp.add_lwpolyline([(20, 9.5, 0), (60, 9.5, 1), (60, 15.5, 0), (20, 15.5, 1)], format="xyb", close=True)
    doc.saveas(OUT / "slot_plate.dxf")


def gusset():
    """三角のガセット。LINE だけ（端点をわずかにずらして、つなぎ合わせの検査も兼ねる）。"""
    doc, msp = new()
    p = [(0, 0), (45, 0), (45, 8), (8, 40), (0, 40)]
    for i in range(len(p)):
        a, b = p[i], p[(i + 1) % len(p)]
        msp.add_line((a[0] + 0.004 * (i % 2), a[1]), b)
    msp.add_circle((8, 8), 2.1)
    doc.saveas(OUT / "gusset.dxf")


def cover_cm():
    """楕円とスプラインを含むカバー。単位は cm（Inventor の書き出しを想定）。"""
    doc, msp = new(ezdxf.units.CM)
    msp.add_ellipse((0, 0), major_axis=(3.5, 0), ratio=0.6)
    msp.add_ellipse((0, 0), major_axis=(0, 1.0), ratio=0.5)
    pts = [(1.8, 0.0), (2.4, 0.5), (2.9, 0.0), (2.4, -0.5), (1.8, 0.0)]
    msp.add_spline(pts)        # フィット点のスプライン（始点と終点が同じ＝閉じている）
    doc.saveas(OUT / "cover_cm.dxf")


def mirrored_arc():
    """押し出し方向が -Z の円弧を含む部材（CAD で鏡像コピーするとこうなる）。"""
    doc, msp = new()
    msp.add_line((0, 0), (30, 0))
    msp.add_line((30, 0), (30, 12))
    msp.add_line((0, 12), (0, 0))
    # (30,12) → (0,12) を上にふくらむ半円でつなぐ。OCS が反転しているので中心の x は符号が逆
    arc = msp.add_arc((-15, 12), 15, 0, 180)
    arc.dxf.extrusion = (0, 0, -1)
    doc.saveas(OUT / "mirrored_arc.dxf")


FILES = {"bracket_L.dxf": bracket_l, "flange.dxf": flange, "slot_plate.dxf": slot_plate, "gusset.dxf": gusset,
         "cover_cm.dxf": cover_cm, "mirrored_arc.dxf": mirrored_arc}


def ensure():
    """無いファイルだけ作る（作るたびに日時などが変わって、リポジトリに無駄な差分が出るのを避ける）。"""
    OUT.mkdir(parents=True, exist_ok=True)
    for name, f in FILES.items():
        if not (OUT / name).exists():
            f()


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for f in FILES.values():
        f()
    print("wrote", sorted(p.name for p in OUT.glob("*.dxf")))
