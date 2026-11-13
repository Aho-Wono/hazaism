"""部材 DXF の読込。

- 切断線になる要素（LINE / ARC / CIRCLE / LWPOLYLINE / POLYLINE / ELLIPSE / SPLINE、INSERT は展開）だけを取り出す
- 敷き詰め計算用に、閉じた輪を集めて「材料がある領域」（外形と穴）を多角形にする
- 元の要素は出力用にそのまま持っておく（円弧は円弧のまま書き出すため）
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import ezdxf
import numpy as np
from ezdxf import path as ezpath
from ezdxf.upright import upright
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.ops import polygonize_full, unary_union

CUT_TYPES = {"LINE", "ARC", "CIRCLE", "LWPOLYLINE", "POLYLINE", "ELLIPSE", "SPLINE"}
FLATTEN_MM = 0.02        # 敷き詰め計算用に曲線を折れ線にするときの許容差
JOIN_TOL_MM = 0.05       # 端点どうしがこれ以内なら、つながっているとみなす

# $INSUNITS → mm への倍率
UNIT_SCALE = {0: 1.0, 1: 25.4, 2: 304.8, 4: 1.0, 5: 10.0, 6: 1000.0}
UNIT_NAME = {0: "mm", 1: "inch", 2: "ft", 4: "mm", 5: "cm", 6: "m"}
SCALE_BY_NAME = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "inch": 25.4}


class DxfError(Exception):
    pass


@dataclass
class Part:
    name: str
    entities: list                      # ezdxf の要素（元の単位のまま。+Z 向きにそろえてある）
    layers: dict                        # レイヤー名 → {"color": ACI}
    unit: str                           # "mm" / "cm" / "m" / "inch"
    polygon: MultiPolygon               # 材料がある領域（mm）
    warnings: list[str] = field(default_factory=list)

    @property
    def scale(self) -> float:
        return SCALE_BY_NAME[self.unit]

    @property
    def area(self) -> float:
        return float(self.polygon.area)

    @property
    def bounds(self):
        return self.polygon.bounds


def _collect(layout, doc, out, skipped, depth=0):
    for e in layout:
        t = e.dxftype()
        if t == "INSERT":
            if depth > 8:
                continue
            try:
                _collect(list(e.virtual_entities()), doc, out, skipped, depth + 1)
            except Exception:
                skipped["INSERT"] = skipped.get("INSERT", 0) + 1
        elif t in CUT_TYPES:
            if t == "POLYLINE" and not (e.is_2d_polyline or e.is_3d_polyline):
                skipped[t] = skipped.get(t, 0) + 1
                continue
            out.append(e)
        else:
            skipped[t] = skipped.get(t, 0) + 1


def _flatten(e, scale: float) -> tuple[np.ndarray, bool] | None:
    """要素 → 折れ線（mm）と「それ自体で閉じているか」。"""
    try:
        p = ezpath.make_path(e)
    except Exception:
        return None
    pts = np.array([(v.x, v.y) for v in p.flattening(FLATTEN_MM / scale)], float) * scale
    if len(pts) < 2:
        return None
    closed = len(pts) >= 4 and float(np.linalg.norm(pts[0] - pts[-1])) < 1e-9
    return pts, closed


def _snap_endpoints(lines: list[np.ndarray], tol: float) -> list[np.ndarray]:
    """近い端点どうしを同じ点にまとめる（CAD によっては端点が微妙にずれている）。"""
    ends = np.array([p for ln in lines for p in (ln[0], ln[-1])])
    n = len(ends)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    order = np.argsort(ends[:, 0])
    xs = ends[order, 0]
    for a in range(n):
        i = order[a]
        b = a + 1
        while b < n and xs[b] - xs[a] <= tol:
            j = order[b]
            if abs(ends[i, 1] - ends[j, 1]) <= tol:
                parent[find(i)] = find(j)
            b += 1
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    snapped = ends.copy()
    for members in groups.values():
        snapped[members] = ends[members].mean(0)
    out = []
    for k, ln in enumerate(lines):
        ln = ln.copy()
        ln[0], ln[-1] = snapped[2 * k], snapped[2 * k + 1]
        out.append(ln)
    return out


def _material(rings: list[Polygon]) -> MultiPolygon:
    """閉じた輪の集まりから、偶奇則で「材料がある領域」を作る（外形の中の輪は穴、穴の中の輪は島）。"""
    rings = sorted((r for r in rings if r.area > 1e-9), key=lambda r: r.area, reverse=True)
    region = None
    for r in rings:
        r = r if r.is_valid else r.buffer(0)
        region = r if region is None else region.symmetric_difference(r)
    if region is None or region.is_empty:
        return MultiPolygon()
    polys = [g for g in getattr(region, "geoms", [region]) if isinstance(g, Polygon) and g.area > 1e-6]
    return MultiPolygon(polys)


def build_polygon(entities, scale: float) -> tuple[MultiPolygon, list[str]]:
    warnings = []
    rings, open_lines, bad = [], [], 0
    for e in entities:
        fl = _flatten(e, scale)
        if fl is None:
            bad += 1
            continue
        pts, closed = fl
        if closed:
            rings.append(Polygon(pts[:-1]))
        else:
            open_lines.append(pts)
    if bad:
        warnings.append(f"形状を読めない要素が {bad} 個あります")
    if open_lines:
        open_lines = _snap_endpoints(open_lines, JOIN_TOL_MM)
        merged = unary_union([LineString(ln) for ln in open_lines if float(np.linalg.norm(ln[0] - ln[-1])) > 0 or len(ln) > 2])
        polys, cuts, dangles, invalid = polygonize_full(merged)
        rings += [Polygon(p.exterior) for p in polys.geoms]
        n_open = len(dangles.geoms) + len(cuts.geoms) + len(invalid.geoms)
        if n_open:
            warnings.append(f"閉じていない線が {n_open} 本あります（敷き詰めの形には含めず、そのまま出力します）")
    return _material(rings), warnings


def load_part(path_or_stream, name: str, unit: str | None = None) -> Part:
    try:
        doc = ezdxf.readfile(path_or_stream) if isinstance(path_or_stream, str) else ezdxf.read(path_or_stream)
    except Exception as ex:
        raise DxfError(f"DXF を読めません: {ex}") from ex
    return part_from_doc(doc, name, unit)


def load_part_bytes(data: bytes, name: str, unit: str | None = None) -> Part:
    """アップロードされた DXF（バイト列）を読む。文字コードの違いや多少の壊れは ezdxf の recover に任せる。"""
    import io

    from ezdxf import recover
    try:
        doc, _ = recover.read(io.BytesIO(data))
    except Exception as ex:
        raise DxfError(f"DXF を読めません: {ex}") from ex
    return part_from_doc(doc, name, unit)


def part_from_doc(doc, name: str, unit: str | None = None) -> Part:
    msp = doc.modelspace()
    entities, skipped = [], {}
    _collect(msp, doc, entities, skipped)
    for e in entities:
        try:
            upright(e)       # 押し出し方向が -Z の要素（鏡像で作られた円弧など）を +Z にそろえる
        except Exception:
            pass
    if not entities:
        raise DxfError("切断できる図形（線・円弧・円・ポリライン・楕円・スプライン）がありません")
    warnings = []
    if skipped:
        warnings.append("無視した要素: " + ", ".join(f"{k}×{v}" for k, v in sorted(skipped.items())))
    insunits = int(doc.header.get("$INSUNITS", 0) or 0)
    if unit is None:
        if insunits not in UNIT_SCALE:
            warnings.append(f"単位（$INSUNITS={insunits}）が不明なので mm として読みました")
        unit = UNIT_NAME.get(insunits, "mm")
        if unit == "ft":
            unit = "inch"
    if unit not in SCALE_BY_NAME:
        raise DxfError(f"単位が不正です: {unit}")
    poly, w = build_polygon(entities, SCALE_BY_NAME[unit])
    warnings += w
    if poly.is_empty:
        raise DxfError("閉じた外形がありません（線の端点がつながっているか確認してください）")
    if unit != "mm":
        warnings.append(f"図面の単位が {unit} なので mm に換算しました。外形寸法を確認してください")
    layers = {}
    for e in entities:
        ln = e.dxf.get("layer", "0")
        if ln not in layers:
            color = 7
            if ln in doc.layers:
                try:
                    color = abs(int(doc.layers.get(ln).color))
                except Exception:
                    color = 7
            layers[ln] = {"color": color}
    return Part(name=name, entities=entities, layers=layers, unit=unit, polygon=poly, warnings=warnings)


def set_unit(part: Part, unit: str) -> None:
    """単位を変えて、敷き詰め用の形を作り直す。"""
    if unit not in SCALE_BY_NAME:
        raise DxfError(f"単位が不正です: {unit}")
    poly, _ = build_polygon(part.entities, SCALE_BY_NAME[unit])
    part.unit, part.polygon = unit, poly


def outline_paths(part: Part) -> list[list[tuple[float, float]]]:
    """画面表示用: 各要素を折れ線（mm）にしたもの。"""
    out = []
    for e in part.entities:
        fl = _flatten(e, part.scale)
        if fl is not None:
            out.append([(round(float(x), 3), round(float(y), 3)) for x, y in fl[0]])
    return out


def placement_matrix(scale: float, flip: bool, angle_deg: float, tx: float, ty: float):
    """部材のローカル座標（元の単位）→ 配置後の座標（mm）。順に: 単位換算 → 反転（x → −x）→ 回転 → 移動。"""
    from ezdxf.math import Matrix44
    return Matrix44.chain(
        Matrix44.scale(-scale if flip else scale, scale, 1.0),
        Matrix44.z_rotate(math.radians(angle_deg)),
        Matrix44.translate(tx, ty, 0.0),
    )
