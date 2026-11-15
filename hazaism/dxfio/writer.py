"""敷き詰め結果の DXF 出力。

部材の元の要素を複製して、単位換算・反転・回転・移動（＋出力時の原点と向き）をかける。円弧は円弧のまま出す。
鏡像にした要素は押し出し方向が -Z になるので、+Z にそろえてから書く（-Z のままだと加工ソフトで円弧が裏返ることがある）。
要素は参照（ハンドルや線種）を引きずらないように、座標だけ取り出して新しく作り直す。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import ezdxf
from ezdxf import bbox
from ezdxf.math import Matrix44
from ezdxf.upright import upright

from .reader import Part, placement_matrix

SCRAP_LAYER = "SCRAP_OUTLINE"
# 端材の輪郭に使える線の色（AutoCAD の色番号）: 赤・黄・緑・水色・青・紫
SCRAP_COLORS = (1, 2, 3, 4, 5, 6)


@dataclass
class Placement:
    part: int          # parts の番号
    flip: bool
    angle: float       # 度（反時計回り）
    tx: float
    ty: float


def entity_colors(part: Part) -> set[int]:
    """部材の線の色（AutoCAD の色番号）。色がレイヤー任せ（BYLAYER）の線は、そのレイヤーの色を数える。"""
    out = set()
    for e in part.entities:
        c = e.dxf.get("color", 256)
        if c == 256:
            c = part.layers.get(e.dxf.get("layer", "0"), {}).get("color", 7)
        out.add(abs(int(c)))
    return out


def output_matrix(origin=(0.0, 0.0), angle_deg: float = 0.0) -> Matrix44:
    """アプリ内の座標 → 出力座標。origin が (0, 0) に、angle_deg の方向が +X になる。"""
    return Matrix44.chain(
        Matrix44.translate(-origin[0], -origin[1], 0.0),
        Matrix44.z_rotate(-math.radians(angle_deg)),
    )


def _attribs(e, layer_prefix: str = "") -> dict:
    a = {"layer": layer_prefix + e.dxf.get("layer", "0")}
    color = e.dxf.get("color", 256)
    if color != 256:
        a["color"] = color
    if e.dxf.hasattr("true_color"):
        a["true_color"] = e.dxf.true_color
    return a


def _emit(msp, e, m: Matrix44) -> None:
    """要素 e に変換 m をかけたものを msp に追加する。"""
    c = e.copy()
    c.transform(m)
    upright(c)
    t = c.dxftype()
    a = _attribs(e)
    if t == "LINE":
        msp.add_line(c.dxf.start.vec2, c.dxf.end.vec2, dxfattribs=a)
    elif t == "CIRCLE":
        msp.add_circle(c.dxf.center.vec2, c.dxf.radius, dxfattribs=a)
    elif t == "ARC":
        msp.add_arc(c.dxf.center.vec2, c.dxf.radius, c.dxf.start_angle, c.dxf.end_angle, dxfattribs=a)
    elif t == "LWPOLYLINE":
        msp.add_lwpolyline(list(c.get_points("xyb")), format="xyb", close=bool(c.closed), dxfattribs=a)
    elif t == "POLYLINE":
        pts = [(v.dxf.location.x, v.dxf.location.y, v.dxf.get("bulge", 0.0)) for v in c.vertices]
        msp.add_lwpolyline(pts, format="xyb", close=bool(c.is_closed), dxfattribs=a)
    elif t == "ELLIPSE":
        msp.add_ellipse(c.dxf.center.vec2, major_axis=c.dxf.major_axis, ratio=c.dxf.ratio,
                        start_param=c.dxf.start_param, end_param=c.dxf.end_param, dxfattribs=a)
    elif t == "SPLINE":
        sp = msp.add_spline(dxfattribs=a)
        sp.dxf.degree = c.dxf.degree
        sp.dxf.flags = c.dxf.flags
        if len(c.control_points):
            sp.control_points = [(p[0], p[1], 0.0) for p in c.control_points]
            sp.knots = list(c.knots)
            if len(c.weights):
                sp.weights = list(c.weights)
        if len(c.fit_points):
            sp.fit_points = [(p[0], p[1], 0.0) for p in c.fit_points]
            for name in ("start_tangent", "end_tangent"):
                if c.dxf.hasattr(name):
                    sp.dxf.set(name, c.dxf.get(name))
    else:
        raise ValueError(f"unsupported entity: {t}")


def write_layout(out_path: str | None, parts: list[Part], placements: list[Placement], *, scrap_shape: dict | None = None,
                 scrap_color: int = 5, origin=(0.0, 0.0), angle_deg: float = 0.0, version: str = "R2010") -> dict:
    """配置結果を DXF に書き出す。戻り値: {"entities": 要素数, "extents": (minx, miny, maxx, maxy), "data": DXF の中身}。

    out_path が None なら、ファイルには書かず中身（bytes）だけを返す（ブラウザから保存するとき）。

    端材の輪郭は、レイヤーを分けるだけでなく、線そのものに色（scrap_color）を付ける。
    加工ソフトには DXF のレイヤーを見ず、線の色で加工条件を分けるものがある（SmartDIYs Cutter など）。
    レイヤーの色に任せる（BYLAYER）と部材と同じ扱いになってしまうので、図形に直接色を書く。
    """
    doc = ezdxf.new(version, setup=False)
    doc.units = ezdxf.units.MM
    doc.header["$MEASUREMENT"] = 1
    msp = doc.modelspace()
    out_m = output_matrix(origin, angle_deg)

    for pl in placements:
        part = parts[pl.part]
        for name, info in part.layers.items():
            if name not in doc.layers and name != "0":
                doc.layers.add(name, color=info.get("color", 7))
        m = Matrix44.chain(placement_matrix(part.scale, pl.flip, pl.angle, pl.tx, pl.ty), out_m)
        for e in part.entities:
            _emit(msp, e, m)

    if scrap_shape is not None:
        if SCRAP_LAYER not in doc.layers:
            doc.layers.add(SCRAP_LAYER, color=scrap_color)
        for loop in [scrap_shape["outer"], *scrap_shape.get("holes", [])]:
            pts = []
            for x, y, b in loop:
                v = out_m.transform((x, y, 0.0))
                pts.append((v.x, v.y, b))
            msp.add_lwpolyline(pts, format="xyb", close=True, dxfattribs={"layer": SCRAP_LAYER, "color": scrap_color})

    ext = bbox.extents(msp)
    extents = None
    if ext.has_data:
        # 図面の範囲（$EXTMIN / $EXTMAX）。入れておくと、開いたときに全体が表示される。
        # ezdxf では、レイアウト側とヘッダー側の両方に入れないと保存後に残らない
        lo, hi = (ext.extmin.x, ext.extmin.y, 0.0), (ext.extmax.x, ext.extmax.y, 0.0)
        msp.reset_extents(lo, hi)
        doc.header["$EXTMIN"], doc.header["$EXTMAX"] = lo, hi
        extents = (ext.extmin.x, ext.extmin.y, ext.extmax.x, ext.extmax.y)
    if out_path is not None:
        doc.saveas(out_path)
        data = None
    else:
        import os
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix=".dxf")
        os.close(fd)
        try:
            doc.saveas(tmp)               # 文字コードなどの扱いを saveas と同じにするため、いったんファイルに書く
            with open(tmp, "rb") as f:
                data = f.read()
        finally:
            os.remove(tmp)
    return {"entities": len(msp), "extents": extents, "data": data}
