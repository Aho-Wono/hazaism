// ステップ 5: 原点と向きを決めて DXF を出力

import { $, $$, apiRaw, fmt, svg, toast, withBusy } from "./api.js";
import { S, clearLayers, drawRectImage, layers, msgHtml, nav, screenLabel, setHint, vp } from "./ctx.js";
import * as G from "./geom.js";
import { drawPlacements, drawScrap, fitToScrap, outlinePoints } from "./layout.js";

// 出力座標の決め方。origin が (0, 0) に、angle の方向（度）が +X になる。
// corner は、原点を「端材を囲む四角の角」に置いているときの角の名前（tl / tr / bl / br）。クリックで置いたら null
const X = { origin: null, angle: 0, corner: "tl", tool: "origin", active: false, pts: [] };

const rad = (d) => d * Math.PI / 180;

// world → 出力座標
function toOut(p) {
  const c = Math.cos(rad(X.angle)), s = Math.sin(rad(X.angle));
  const dx = p[0] - X.origin[0], dy = p[1] - X.origin[1];
  return [dx * c + dy * s, -dx * s + dy * c];
}

// いまの向きで端材を囲む四角（出力座標の軸に平行）の範囲
function frameBox() {
  const c = Math.cos(rad(X.angle)), s = Math.sin(rad(X.angle));
  let u0 = Infinity, v0 = Infinity, u1 = -Infinity, v1 = -Infinity;
  for (const p of X.pts) {
    const u = p[0] * c + p[1] * s, v = -p[0] * s + p[1] * c;
    u0 = Math.min(u0, u); u1 = Math.max(u1, u); v0 = Math.min(v0, v); v1 = Math.max(v1, v);
  }
  return { u0, v0, u1, v1, c, s };
}

// 囲む四角の角（world 座標）。which: tl = 左上、tr = 右上、bl = 左下、br = 右下（出力の向きで見て）
function cornerOrigin(which) {
  const { u0, v0, u1, v1, c, s } = frameBox();
  const u = which[1] === "l" ? u0 : u1, v = which[0] === "t" ? v1 : v0;
  return [u * c - v * s, u * s + v * c];
}

function setCorner(which) {
  X.corner = which;
  X.origin = cornerOrigin(which);
  render();
}

function setAngle(a) {
  X.angle = ((a % 360) + 540) % 360 - 180;
  if (Math.abs(X.angle) < 1e-9) X.angle = 0;
  if (X.corner) X.origin = cornerOrigin(X.corner);      // 角に置いているなら、回した後も同じ角に置き直す
  render();
}

function render() {
  if (!X.active) return;
  layers.over.replaceChildren();
  layers.labels.replaceChildren();
  $$("[data-corner]").forEach((b) => b.classList.toggle("active", b.dataset.corner === X.corner));
  // 辺を選ぶとき用の当たり判定
  if (X.tool === "edge") {
    [S.outline.outer, ...(S.outline.holes || [])].forEach((loop) => {
      loop.forEach((p0, i) => {
        const p1 = loop[(i + 1) % loop.length];
        const e = svg("path", { d: `M${p0[0]} ${p0[1]}` + G.edgeD(p0, p1, p0[2]), class: "edge-pick" }, layers.over);
        e.dataset.ang = Math.atan2(p1[1] - p0[1], p1[0] - p0[0]) * 180 / Math.PI;
      });
    });
  }
  // 座標軸
  const L = vp.px(90), c = Math.cos(rad(X.angle)), s = Math.sin(rad(X.angle));
  const o = X.origin;
  const arrow = (dx, dy, cls) => {
    const tip = [o[0] + dx * L, o[1] + dy * L], h = vp.px(11);
    const bx = tip[0] - dx * h, by = tip[1] - dy * h, nx = -dy * h * 0.45, ny = dx * h * 0.45;
    svg("path", { d: `M${o[0]} ${o[1]}L${tip[0]} ${tip[1]}M${bx + nx} ${by + ny}L${tip[0]} ${tip[1]}L${bx - nx} ${by - ny}`, class: cls }, layers.over);
    // 文字は矢印の先の少し外側（画面では y が下向きなので dy の符号を反転する）
    screenLabel(tip, cls === "axis-x" ? "X" : "Y", { dx: dx * 14, dy: -dy * 14, anchor: "middle" });
  };
  arrow(c, s, "axis-x");
  arrow(-s, c, "axis-y");
  svg("circle", { cx: o[0], cy: o[1], r: vp.px(5), class: "axis-o" }, layers.over);

  // 出力座標での範囲
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const p of X.pts) {
    const q = toOut(p);
    x0 = Math.min(x0, q[0]); x1 = Math.max(x1, q[0]);
    y0 = Math.min(y0, q[1]); y1 = Math.max(y1, q[1]);
  }
  $("#export-frame").innerHTML = `<span class="k">端材の範囲</span>X ${fmt(x0, 1)} 〜 ${fmt(x1, 1)} mm<br>` +
    `<span class="k"></span>Y ${fmt(y0, 1)} 〜 ${fmt(y1, 1)} mm<br><span class="k">大きさ</span>${fmt(x1 - x0, 1)} × ${fmt(y1 - y0, 1)} mm`;
  const ai = $("#in-export-angle");
  if (document.activeElement !== ai) ai.value = (Math.round(X.angle * 100) / 100).toString();
}

// クリックした点を、近くの頂点・外接矩形の角・辺の上へ吸着させる
function snap(w) {
  const tol = vp.px(10);
  let best = null;
  const tryPt = (p) => {
    const d = G.dist(p, w);
    if (d < tol && (!best || d < best.d)) best = { p: [p[0], p[1]], d };
  };
  const ls = [S.outline.outer, ...(S.outline.holes || [])];
  for (const loop of ls) for (const p of loop) tryPt(p);
  // いまの向きで端材を囲む四角の四隅
  for (const k of ["tl", "tr", "bl", "br"]) tryPt(cornerOrigin(k));
  if (best) return best.p;
  for (const loop of ls) {
    loop.forEach((p0, i) => {
      const n = G.nearestOnEdge(p0, loop[(i + 1) % loop.length], p0[2], w);
      if (n.d < tol && (!best || n.d < best.d)) best = { p: n.p, d: n.d };
    });
  }
  return best ? best.p : w;
}

function onClick(e) {
  if (!X.active || e.button !== 0 || vp.panning || vp.spaceDown) return;
  if (X.tool === "edge") {
    const a = e.target.dataset && e.target.dataset.ang;
    if (a === undefined) return;
    // 辺の向きは 2 通り（逆向き）あるので、いまの向きに近い方にする
    let ang = +a;
    const diff = (((ang - X.angle) % 360) + 540) % 360 - 180;
    if (Math.abs(diff) > 90) ang += 180;
    setAngle(ang);
    toast("選んだ辺を X 軸と平行にしました。逆向きにするには「180°」を押してください");
    return;
  }
  X.origin = snap(vp.toWorld(e.clientX, e.clientY));
  X.corner = null;
  render();
}

function setTool(t) {
  X.tool = t;
  $$("[data-etool]").forEach((b) => b.classList.toggle("active", b.dataset.etool === t));
  setHint(t === "origin" ? "原点にしたい場所をクリック（頂点・角・辺に吸着します）" : "X 軸と平行にしたい辺をクリック");
  render();
}

// ---- 保存 ----
// Chrome/Edge は「名前を付けて保存」の画面で場所を選べる。前回の保存先を覚えておき、次はそこから開く
const DB = "hazaism-ui";

function idb(mode, fn) {
  return new Promise((resolve, reject) => {
    const open = indexedDB.open(DB, 1);
    open.onupgradeneeded = () => open.result.createObjectStore("kv");
    open.onerror = () => reject(open.error);
    open.onsuccess = () => {
      const tx = open.result.transaction("kv", mode);
      const req = fn(tx.objectStore("kv"));
      tx.oncomplete = () => { open.result.close(); resolve(req && req.result); };
      tx.onerror = () => { open.result.close(); reject(tx.error); };
    };
  });
}
const getLastHandle = () => idb("readonly", (st) => st.get("lastSave")).catch(() => null);
const setLastHandle = (h) => idb("readwrite", (st) => st.put(h, "lastSave")).catch(() => {});

async function saveFile(data, name) {
  const blob = new Blob([data], { type: "application/dxf" });
  if (window.showSaveFilePicker) {
    const opts = { suggestedName: name, types: [{ description: "DXF", accept: { "application/dxf": [".dxf"] } }] };
    const last = await getLastHandle();
    if (last) opts.startIn = last;
    let handle;
    try {
      handle = await window.showSaveFilePicker(opts);
    } catch (e) {
      if (e.name === "AbortError") return null;               // 保存をやめた
      handle = await window.showSaveFilePicker({ ...opts, startIn: undefined });   // 前回の場所が無くなっていた等
    }
    const w = await handle.createWritable();
    await w.write(blob);
    await w.close();
    setLastHandle(handle);
    return { how: "picker", name: handle.name };
  }
  // ほかのブラウザ: いつものダウンロード（保存先はブラウザの設定による）
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
  return { how: "download", name };
}

async function doExport() {
  const box = $("#export-result");
  let name = $("#in-filename").value.trim() || "nest.dxf";
  if (!/\.dxf$/i.test(name)) name += ".dxf";
  try {
    const r = await withBusy("DXF を作っています…", () => apiRaw("POST", "/api/export", {
      name, origin: X.origin, angle: X.angle, include_scrap: $("#in-include-scrap").checked,
      scrap_color: +$("#in-scrap-color").value,
    }));
    const info = r.json;
    $("#in-filename").value = info.name;
    S.settings.out_name = info.name;
    const saved = await saveFile(r.data, info.name);
    if (!saved) return;
    S.exportedResult = S.result;
    const where = saved.how === "picker" ? `「${saved.name}」に保存しました` : `「${saved.name}」をダウンロードしました`;
    box.replaceChildren(msgHtml("ok", `${where}（部材 ${info.placed} 個、図形 ${info.entities} 個）`));
    if (info.warning) box.appendChild(msgHtml("warn", info.warning));
  } catch (e) {
    box.replaceChildren(msgHtml("bad", e.message));
  }
}

export function initExport() {
  $("#canvas").addEventListener("click", onClick);
  vp.onChange(() => { if (X.active) render(); });
  $$("[data-etool]").forEach((b) => (b.onclick = () => setTool(b.dataset.etool)));
  $("#btn-rot-m90").onclick = () => setAngle(X.angle - 90);
  $("#btn-rot-p90").onclick = () => setAngle(X.angle + 90);
  $("#btn-rot-180").onclick = () => setAngle(X.angle + 180);
  $("#in-export-angle").onchange = (e) => { const v = parseFloat(e.target.value); if (Number.isFinite(v)) setAngle(v); };
  $$("[data-corner]").forEach((b) => (b.onclick = () => setCorner(b.dataset.corner)));
  $("#btn-export").onclick = () => doExport();
  $("#btn-export-back").onclick = () => nav.goto("nest");
}

export function resetExportFrame() {
  X.origin = null;
  X.angle = 0;
  X.corner = "tl";
}

export function enterExport() {
  X.active = true;
  vp.flipY = true;
  clearLayers();
  drawRectImage(0.3);
  drawScrap();
  drawPlacements(S.result ? S.result.placements : []);
  X.pts = outlinePoints(S.outline);
  // 最初は左上の角を原点にする（SmartDIYs Cutter は加工機の原点が左上）
  if (!X.origin || X.corner) X.origin = cornerOrigin(X.corner || "tl");
  $("#in-include-scrap").checked = !!S.settings.include_scrap;
  $("#in-scrap-color").value = String(S.settings.scrap_color || 5);
  if (!$("#in-filename").value) $("#in-filename").value = S.settings.out_name || "nest.dxf";
  $("#export-result").replaceChildren();
  fitToScrap(vp);
  setTool("origin");
}

export function leaveExport() {
  X.active = false;
}
