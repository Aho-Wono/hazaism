// 端材と、配置した部材の描画（ステップ 4・5 で共通）

import { svg } from "./api.js";
import { S, layers, partColor } from "./ctx.js";
import * as G from "./geom.js";

export function drawScrap(cls = "scrap") {
  if (!S.outline) return null;
  return svg("path", { d: G.shapeD(S.outline), class: cls }, layers.under);
}

// 配置 {part, flip, angle, tx, ty} を描く。部材のローカル座標 → 反転 → 回転 → 移動
export function drawPlacements(placements) {
  layers.shape.replaceChildren();
  const byId = new Map(S.parts.map((p) => [p.id, p]));
  const cache = new Map();
  for (const pl of placements || []) {
    const p = byId.get(pl.part);
    if (!p) continue;
    if (!cache.has(p.id)) cache.set(p.id, { fill: G.polylinesD(p.rings), line: G.polylinesD(p.paths) });
    const d = cache.get(p.id), col = partColor(p.id);
    const g = svg("g", {
      class: "placed" + (pl.flip ? " flip" : ""),
      transform: `translate(${pl.tx} ${pl.ty}) rotate(${pl.angle})` + (pl.flip ? " scale(-1 1)" : ""),
    }, layers.shape);
    svg("path", { d: d.fill, fill: col, "fill-opacity": 0.55, stroke: "none" }, g);
    svg("path", { d: d.line, fill: "none", stroke: col, "stroke-linejoin": "round" }, g);
    const t = svg("title", {}, g);
    t.textContent = `${p.name}${pl.flip ? "（反転）" : ""}　${Math.round(pl.angle * 10) / 10}°`;
  }
}

// 輪郭の上の点を細かく並べたもの（範囲の計算用）
export function outlinePoints(shape) {
  const pts = [];
  for (const loop of [shape.outer, ...(shape.holes || [])]) {
    for (let i = 0; i < loop.length; i++) {
      const p0 = loop[i], p1 = loop[(i + 1) % loop.length];
      const n = Math.abs(p0[2]) < 1e-9 ? 1 : 24;
      for (let k = 0; k < n; k++) pts.push(G.edgePoint(p0, p1, p0[2], k / n));
    }
  }
  return pts;
}

export function fitToScrap(vp) {
  if (!S.outline) return;
  const [a, b, c, d] = G.loopBounds([S.outline.outer]);
  const m = Math.max(12, 0.08 * Math.max(c - a, d - b));
  vp.fit(a - m, b - m, c + m, d + m);
}
