// 画面全体で共有するもの（状態・図の入れ物・小道具）

import { $, svg } from "./api.js";
import { Viewport } from "./viewport.js";

// 作業の状態。サーバーの /api/state と同じ形
export const S = {
  settings: {},
  photo: null,      // 写真の情報（格子・補正画像・検出した輪郭の候補）
  outline: null,    // いまの輪郭 {outer, holes}
  candIndex: null,  // outline がどの候補から来たか（手で作った場合は null）
  parts: [],
  result: null,     // 敷き詰め結果
  step: "photo",
};

export const layers = {};
export let vp = null;

// app.js が入れる（モジュールどうしが輪になって読み込まないように、ここを経由する）
export const nav = { goto: () => {}, refresh: () => {} };

export function initCtx() {
  const el = $("#canvas");
  vp = new Viewport(el, $("#world"));
  for (const k of ["image", "under", "shape", "over", "labels"]) layers[k] = $("#layer-" + k);
}

// 図の上の文字。world 座標 p の位置から画面で (dx, dy) 画素ずらして置く。
// 文字は図と一緒に拡大縮小しないように、画面の座標の層（layers.labels）に描く
// （図の中に置くと、拡大したときに文字のふちどりまで拡大されて黒くつぶれる）
export function screenLabel(p, text, { dx = 12, dy = -12, anchor = "start", cls = "label" } = {}) {
  const x = vp.s * p[0] + vp.tx + dx;
  const y = (vp.flipY ? -vp.s : vp.s) * p[1] + vp.ty + dy;
  const t = svg("text", { x, y, class: cls, "text-anchor": anchor, "dominant-baseline": "middle" }, layers.labels);
  t.textContent = text;
  return t;
}

export function clearLayers() {
  for (const g of Object.values(layers)) g.replaceChildren();
}

export function setHint(text) {
  $("#stage-hint").textContent = text || "";
}

// 補正画像（真上から見た写真）を、mm の図の背景に置く
export function drawRectImage(opacity = 1) {
  const r = S.photo && S.photo.rect;
  if (!r) return;
  svg("image", {
    href: `/api/photo/rect.jpg?v=${r.version}`,
    x: 0, y: 0, width: r.width_mm, height: r.height_mm,
    transform: `translate(0 ${r.height_mm}) scale(1 -1)`,     // 図は y が上向きなので、画像だけ上下を戻す
    preserveAspectRatio: "none", opacity,
    style: "image-rendering: auto; pointer-events: none",
  }, layers.image);
}

export const PART_COLORS = ["#60a5fa", "#34d399", "#fbbf24", "#f472b6", "#a78bfa", "#fb923c", "#22d3ee", "#a3e635", "#f87171", "#94a3b8"];
export const partColor = (id) => PART_COLORS[(id - 1) % PART_COLORS.length];

export function msgHtml(kind, text) {
  const d = document.createElement("div");
  d.className = "msg " + kind;
  d.textContent = text;
  return d;
}
