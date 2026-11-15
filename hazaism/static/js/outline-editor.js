// ステップ 2: 輪郭の確認と修正

import { $, $$, api, fmt, svg, toast, withBusy } from "./api.js";
import { S, clearLayers, drawRectImage, layers, msgHtml, nav, screenLabel, setHint, vp } from "./ctx.js";
import * as G from "./geom.js";
import { isTyping } from "./viewport.js";

const E = {
  tool: "select",
  op: "add",
  sel: null,          // {kind: "v" | "e", li: 輪の番号（0 = 外周）, i: 頂点の番号}
  history: [],
  future: [],
  drag: null,
  draft: null,        // 描きかけの図形 {tool, pts: [...], cur: [x, y]}
  measure: null,      // [[x, y], [x, y]]
  dirty: false,       // 検出結果から手を入れたか
  problems: [],
  info: null,
  saveTimer: null,
  active: false,
};

const HINTS = {
  select: "頂点をドラッグで移動、ひし形をドラッグで円弧に。辺をダブルクリックで頂点を追加",
  rect: "ドラッグして矩形を描く",
  rect3: "辺の両端を 2 回クリック → 3 回目のクリックで幅を決める",
  circle: "中心から外へドラッグして円を描く",
  circle3: "円周上の 3 点をクリック",
  ellipse: "ドラッグして楕円を描く",
  polygon: "クリックで頂点を置き、ダブルクリック（または Enter）で閉じる",
  measure: "2 点をクリックすると距離を測れます",
};

const loops = () => (S.outline ? [S.outline.outer, ...(S.outline.holes || [])] : []);

// ---- 履歴 ----
function pushHistory() {
  E.history.push(G.cloneShape(S.outline));
  if (E.history.length > 200) E.history.shift();
  E.future = [];
  E.dirty = true;
}

function undo() {
  if (!E.history.length) return;
  E.future.push(G.cloneShape(S.outline));
  S.outline = E.history.pop();
  E.sel = null;
  changed();
}

function redo() {
  if (!E.future.length) return;
  E.history.push(G.cloneShape(S.outline));
  S.outline = E.future.pop();
  E.sel = null;
  changed();
}

// 輪郭が変わったあとの後始末: 描き直して、少し待ってからサーバーへ保存（面積と不具合を教えてもらう）
function changed() {
  S.result = null;
  render();
  clearTimeout(E.saveTimer);
  E.saveTimer = setTimeout(save, 350);
  nav.refresh();
}

async function save() {
  clearTimeout(E.saveTimer);
  E.saveTimer = null;
  try {
    const r = await api("POST", "/api/outline", { shape: S.outline });
    if (S.outline) {
      E.problems = r.problems || [];
      E.info = r.info;
    } else {
      E.problems = [];
      E.info = null;
    }
  } catch (e) {
    E.problems = [e.message];
  }
  renderInfo();
  nav.refresh();
}

export function outlineOk() {
  return !!S.outline && E.problems.length === 0;
}

// ---- 描画 ----
function render() {
  if (!E.active) return;
  layers.under.replaceChildren();
  layers.shape.replaceChildren();
  layers.over.replaceChildren();
  layers.labels.replaceChildren();

  // ほかに検出された板（クリックで切り替え）
  const cands = (S.photo && S.photo.candidates) || [];
  cands.forEach((c, k) => {
    if (k === S.candIndex) return;
    const p = svg("path", { d: G.shapeD(c.shape), class: "cand-other" }, layers.under);
    p.dataset.kind = "cand";
    p.dataset.k = k;
    const t = svg("title", {}, p);
    t.textContent = `この板を使う（${fmt(c.area / 100, 1)} cm²）`;
  });

  const ls = loops();
  if (ls.length) {
    // 輪郭の線は、暗い板の上でも明るいマットの上でも見えるように、黒いふちどりの上に重ねる
    svg("path", { d: G.shapeD(S.outline), class: "outline-halo", "pointer-events": "none" }, layers.shape);
    svg("path", { d: G.shapeD(S.outline), class: "outline-fill", "pointer-events": "none" }, layers.shape);
    ls.forEach((loop, li) => {
      loop.forEach((p0, i) => {
        const p1 = loop[(i + 1) % loop.length];
        const e = svg("path", { d: `M${p0[0]} ${p0[1]}` + G.edgeD(p0, p1, p0[2]), class: "outline-hit" }, layers.shape);
        e.dataset.kind = "e"; e.dataset.li = li; e.dataset.i = i;
      });
    });
    if (E.sel && E.sel.kind === "e" && ls[E.sel.li] && ls[E.sel.li][E.sel.i]) {
      const loop = ls[E.sel.li], p0 = loop[E.sel.i], p1 = loop[(E.sel.i + 1) % loop.length];
      svg("path", { d: `M${p0[0]} ${p0[1]}` + G.edgeD(p0, p1, p0[2]), class: "outline-sel" }, layers.shape);
    }
    if (E.tool === "select") {
      const rv = vp.px(5), rm = vp.px(4.2), minLen = vp.px(26);
      ls.forEach((loop, li) => {
        loop.forEach((p0, i) => {
          const p1 = loop[(i + 1) % loop.length];
          if (G.dist(p0, p1) > minLen) {
            const m = G.edgePoint(p0, p1, p0[2], 0.5);
            const h = svg("rect", {
              x: m[0] - rm, y: m[1] - rm, width: 2 * rm, height: 2 * rm, class: "h-mid",
              transform: `rotate(45 ${m[0]} ${m[1]})`,
            }, layers.over);
            h.dataset.kind = "m"; h.dataset.li = li; h.dataset.i = i;
          }
        });
        loop.forEach((p, i) => {
          const sel = E.sel && E.sel.kind === "v" && E.sel.li === li && E.sel.i === i;
          const h = svg("circle", { cx: p[0], cy: p[1], r: sel ? rv * 1.25 : rv, class: "h-vertex" + (sel ? " sel" : "") }, layers.over);
          h.dataset.kind = "v"; h.dataset.li = li; h.dataset.i = i;
        });
      });
    }
  }
  renderDraft();
  renderMeasure();
  renderSel();
  renderInfo();
  renderCandidates();
  $("#btn-undo").disabled = !E.history.length;
  $("#btn-redo").disabled = !E.future.length;
}

function renderInfo() {
  const el = $("#outline-info");
  if (!S.outline) {
    el.innerHTML = "輪郭がありません。図形の道具で描いてください。";
  } else if (E.info) {
    const n = loops().reduce((a, l) => a + l.length, 0);
    el.innerHTML = `<span class="k">外形寸法</span><b>${fmt(E.info.width, 1)} × ${fmt(E.info.height, 1)} mm</b><br>` +
      `<span class="k">面積</span><b>${fmt(E.info.area / 100, 1)} cm²</b><br>` +
      `<span class="k">頂点</span>${n} 個` + (S.outline.holes.length ? `、穴 ${S.outline.holes.length} 個` : "");
  } else {
    el.innerHTML = "…";
  }
  const pr = $("#outline-problems");
  pr.replaceChildren();
  for (const m of E.problems) pr.appendChild(msgHtml("bad", m));
  const c = S.candIndex !== null && S.photo && S.photo.candidates[S.candIndex];
  if (c && c.touches_edge && !E.dirty) {
    pr.appendChild(msgHtml("warn", "板が写真や格子の外にはみ出しているようです。はみ出した部分は正しく測れていないので、撮り直すか、輪郭を手で直してください。"));
  }
}

function renderCandidates() {
  const el = $("#candidates");
  el.replaceChildren();
  const cands = (S.photo && S.photo.candidates) || [];
  if (cands.length < 2) return;
  const h = document.createElement("div");
  h.className = "note";
  h.textContent = "写真の中で見つかった板（クリックで切り替え）";
  el.appendChild(h);
  cands.forEach((c, k) => {
    const d = document.createElement("div");
    d.className = "cand" + (k === S.candIndex ? " active" : "");
    d.innerHTML = `<span class="swatch"></span>板 ${k + 1}　${fmt(c.area / 100, 1)} cm²` + (c.touches_edge ? "　（はみ出し）" : "");
    d.onclick = () => useCandidate(k);
    el.appendChild(d);
  });
}

function renderSel() {
  const el = $("#sel-info");
  el.replaceChildren();
  const ls = loops();
  if (!E.sel || !ls[E.sel.li] || !ls[E.sel.li][E.sel.i]) {
    E.sel = null;
    return;
  }
  const { kind, li, i } = E.sel;
  const loop = ls[li];
  const where = li === 0 ? "外周" : `穴 ${li}`;
  if (kind === "v") {
    const p = loop[i];
    el.innerHTML = `<div class="row"><b>${where}の頂点</b>
      <label>X <input type="number" step="0.1" id="sel-x" value="${p[0].toFixed(2)}"></label>
      <label>Y <input type="number" step="0.1" id="sel-y" value="${p[1].toFixed(2)}"></label></div>
      <div class="row"><button id="sel-del">この頂点を削除</button>${li > 0 ? '<button id="sel-hole">この穴を削除</button>' : ""}</div>`;
    const set = () => {
      const x = parseFloat($("#sel-x").value), y = parseFloat($("#sel-y").value);
      if (Number.isFinite(x) && Number.isFinite(y)) { pushHistory(); p[0] = x; p[1] = y; changed(); }
    };
    $("#sel-x").onchange = set;
    $("#sel-y").onchange = set;
    $("#sel-del").onclick = deleteSelected;
  } else {
    const p0 = loop[i], p1 = loop[(i + 1) % loop.length];
    const len = G.edgeLength(p0, p1, p0[2]);
    if (Math.abs(p0[2]) < 1e-9) {
      el.innerHTML = `<div class="row"><b>${where}の直線</b> 長さ ${fmt(len, 2)} mm</div>
        <div class="row"><button id="sel-arc">円弧にする</button><button id="sel-add">頂点を追加</button>${li > 0 ? '<button id="sel-hole">この穴を削除</button>' : ""}</div>`;
      $("#sel-arc").onclick = () => { pushHistory(); p0[2] = 0.2; changed(); };
    } else {
      const a = G.arcOf(p0, p1, p0[2]);
      el.innerHTML = `<div class="row"><b>${where}の円弧</b>
        <label>半径 <input type="number" step="0.1" min="0" id="sel-r" value="${a.r.toFixed(2)}"> mm</label></div>
        <div class="row">長さ ${fmt(len, 2)} mm、弦 ${fmt(G.dist(p0, p1), 2)} mm</div>
        <div class="row"><button id="sel-line">直線にする</button><button id="sel-flip">ふくらみを逆に</button><button id="sel-add">頂点を追加</button>${li > 0 ? '<button id="sel-hole">この穴を削除</button>' : ""}</div>`;
      $("#sel-line").onclick = () => { pushHistory(); p0[2] = 0; changed(); };
      $("#sel-flip").onclick = () => { pushHistory(); p0[2] = -p0[2]; changed(); };
      $("#sel-r").onchange = () => {
        const r = parseFloat($("#sel-r").value), L = G.dist(p0, p1);
        if (!Number.isFinite(r) || r < L / 2) { toast(`半径は弦の半分（${fmt(L / 2, 2)} mm）以上にしてください`, "bad"); renderSel(); return; }
        pushHistory();
        p0[2] = Math.sign(p0[2]) * Math.tan(Math.asin(L / (2 * r)) / 2);     // 小さい方の円弧
        changed();
      };
    }
    $("#sel-add").onclick = () => insertVertex(li, i, 0.5);
  }
  const hb = $("#sel-hole");
  if (hb) hb.onclick = () => { pushHistory(); S.outline.holes.splice(li - 1, 1); E.sel = null; changed(); };
}

// ---- 編集操作 ----
function insertVertex(li, i, t) {
  const loop = loops()[li];
  const p0 = loop[i], p1 = loop[(i + 1) % loop.length];
  const q = G.edgePoint(p0, p1, p0[2], t);
  const [b1, b2] = G.splitBulge(p0[2], t);
  pushHistory();
  p0[2] = b1;
  loop.splice(i + 1, 0, [q[0], q[1], b2]);
  E.sel = { kind: "v", li, i: i + 1 };
  changed();
}

function deleteSelected() {
  if (!E.sel || E.sel.kind !== "v") return;
  const { li, i } = E.sel;
  const loop = loops()[li];
  const min = loop.some((p) => Math.abs(p[2]) > 1e-9) ? 2 : 3;
  if (loop.length <= min) {
    if (li > 0) { pushHistory(); S.outline.holes.splice(li - 1, 1); E.sel = null; changed(); }
    else toast("これ以上は頂点を減らせません", "bad");
    return;
  }
  const n = loop.length;
  const prev = loop[(i - 1 + n) % n], cur = loop[i], next = loop[(i + 1) % n];
  pushHistory();
  // 前後どちらかが円弧なら、消す頂点を通る 1 本の円弧にまとめる。両方とも直線なら角を落として直線でつなぐ
  const wasArc = Math.abs(prev[2]) > 1e-9 || Math.abs(cur[2]) > 1e-9;
  let b = wasArc ? G.bulgeThrough(prev, next, cur) : 0;
  if (Math.abs(b) < 1e-4 || Math.abs(b) > 20) b = 0;
  prev[2] = b;
  loop.splice(i, 1);
  E.sel = null;
  changed();
}

function useCandidate(k) {
  const c = S.photo.candidates[k];
  if (!c) return;
  if (E.dirty && !confirm("手で直した内容は消えます。この板に切り替えますか？")) return;
  S.outline = G.cloneShape(c.shape);
  S.candIndex = k;
  E.history = []; E.future = []; E.sel = null; E.dirty = false;
  changed();
  fitToOutline();
}

function fitToOutline() {
  const r = S.photo && S.photo.rect;
  if (S.outline) {
    const [a, b, c, d] = G.loopBounds([S.outline.outer]);
    const m = Math.max(15, 0.12 * Math.max(c - a, d - b));
    vp.fit(a - m, b - m, c + m, d + m);
  } else if (r) {
    vp.fit(0, 0, r.width_mm, r.height_mm);
  }
}

// ---- 図形（足す／引く） ----
function draftPrim() {
  const d = E.draft;
  if (!d) return null;
  const pts = d.cur ? [...d.pts, d.cur] : d.pts;
  if (d.tool === "rect" && pts.length === 2) {
    const [a, b] = pts;
    return { type: "rect", x: Math.min(a[0], b[0]), y: Math.min(a[1], b[1]), w: Math.abs(b[0] - a[0]), h: Math.abs(b[1] - a[1]) };
  }
  if (d.tool === "ellipse" && pts.length === 2) {
    const [a, b] = pts;
    return { type: "ellipse", cx: (a[0] + b[0]) / 2, cy: (a[1] + b[1]) / 2, rx: Math.abs(b[0] - a[0]) / 2, ry: Math.abs(b[1] - a[1]) / 2 };
  }
  if (d.tool === "circle" && pts.length === 2) {
    return { type: "circle", cx: pts[0][0], cy: pts[0][1], r: G.dist(pts[0], pts[1]) };
  }
  if (d.tool === "circle3" && pts.length === 3) {
    const c = G.circle3(pts[0], pts[1], pts[2]);
    return c ? { type: "circle", cx: c.cx, cy: c.cy, r: c.r } : null;
  }
  if (d.tool === "rect3" && pts.length === 3) {
    const [a, b, c] = pts;
    const ux = b[0] - a[0], uy = b[1] - a[1], L = Math.hypot(ux, uy);
    if (L < 1e-6) return null;
    const nx = -uy / L, ny = ux / L;
    const w = (c[0] - a[0]) * nx + (c[1] - a[1]) * ny;
    return { type: "polygon", pts: [a, b, [b[0] + nx * w, b[1] + ny * w], [a[0] + nx * w, a[1] + ny * w]] };
  }
  if (d.tool === "polygon" && pts.length >= 2) return { type: "polygon", pts };
  return null;
}

function primD(prim) {
  if (prim.type === "rect") return `M${prim.x} ${prim.y}h${prim.w}v${prim.h}h${-prim.w}Z`;
  if (prim.type === "circle") return `M${prim.cx - prim.r} ${prim.cy}a${prim.r} ${prim.r} 0 1 0 ${2 * prim.r} 0a${prim.r} ${prim.r} 0 1 0 ${-2 * prim.r} 0Z`;
  if (prim.type === "ellipse") return `M${prim.cx - prim.rx} ${prim.cy}a${prim.rx} ${prim.ry} 0 1 0 ${2 * prim.rx} 0a${prim.rx} ${prim.ry} 0 1 0 ${-2 * prim.rx} 0Z`;
  return "M" + prim.pts.map((p) => `${p[0]} ${p[1]}`).join("L") + "Z";
}

function renderDraft() {
  const d = E.draft;
  if (!d) return;
  const prim = draftPrim();
  if (prim) svg("path", { d: primD(prim), class: "draft" + (E.op === "sub" ? " sub" : "") }, layers.over);
  else if (d.cur && d.pts.length) {
    svg("path", { d: `M${d.pts[d.pts.length - 1].join(" ")}L${d.cur.join(" ")}`, class: "draft" }, layers.over);
  }
  for (const p of d.pts) svg("circle", { cx: p[0], cy: p[1], r: vp.px(3.5), class: "draft-pt" }, layers.over);
  if (prim && (prim.type === "rect" || prim.type === "circle" || prim.type === "ellipse")) {
    const text = prim.type === "rect" ? `${fmt(prim.w, 1)} × ${fmt(prim.h, 1)}`
      : prim.type === "circle" ? `R ${fmt(prim.r, 1)}` : `${fmt(2 * prim.rx, 1)} × ${fmt(2 * prim.ry, 1)}`;
    label(d.cur || d.pts[d.pts.length - 1], text);
  }
}

const label = (p, text) => screenLabel(p, text, { dx: 14, dy: -14 });

function renderMeasure() {
  const m = E.measure;
  if (!m || !m.length) return;
  const g = svg("g", { class: "measure", "pointer-events": "none" }, layers.over);
  for (const p of m) svg("circle", { cx: p[0], cy: p[1], r: vp.px(4) }, g);
  if (m.length === 2) {
    svg("line", { x1: m[0][0], y1: m[0][1], x2: m[1][0], y2: m[1][1] }, g);
    label([(m[0][0] + m[1][0]) / 2, (m[0][1] + m[1][1]) / 2], `${fmt(G.dist(m[0], m[1]), 2)} mm`);
  }
}

async function commitDraft() {
  const prim = draftPrim();
  E.draft = null;
  if (!prim) { render(); return; }
  const tiny = (prim.type === "rect" && (prim.w < 0.2 || prim.h < 0.2)) || (prim.type === "circle" && prim.r < 0.2) ||
    (prim.type === "ellipse" && (prim.rx < 0.2 || prim.ry < 0.2)) || (prim.type === "polygon" && prim.pts.length < 3);
  if (tiny) { render(); return; }
  if (!S.outline && E.op === "sub") { toast("輪郭がないので引けません。先に「足す」で描いてください", "bad"); render(); return; }
  try {
    const r = await api("POST", "/api/outline/boolean", { shape: S.outline, prim, op: E.op });
    pushHistory();
    S.outline = r.shape;
    if (S.candIndex !== null) E.dirty = true;
    E.sel = null;
    if (r.dropped) toast(`離れた破片 ${r.dropped} 個は捨てました（輪郭は 1 つながりの板です）`);
    changed();
  } catch (e) {
    toast(e.message, "bad");
    render();
  }
}

// ---- ポインタ ----
function onDown(e) {
  if (!E.active || e.button !== 0 || vp.panning || vp.spaceDown) return;
  if (isDouble(e) && onDouble(e)) return;
  const w = vp.toWorld(e.clientX, e.clientY);
  const t = e.target.dataset || {};
  if (E.tool === "select") {
    if (t.kind === "cand") { useCandidate(+t.k); return; }
    if (t.kind === "v") {
      E.sel = { kind: "v", li: +t.li, i: +t.i };
      E.drag = { kind: "v", li: +t.li, i: +t.i, start: w, orig: [...loops()[+t.li][+t.i]], moved: false };
    } else if (t.kind === "m") {
      E.sel = { kind: "e", li: +t.li, i: +t.i };
      E.drag = { kind: "m", li: +t.li, i: +t.i, moved: false };
    } else if (t.kind === "e") {
      E.sel = { kind: "e", li: +t.li, i: +t.i };
    } else {
      E.sel = null;
    }
    if (E.drag) e.currentTarget.setPointerCapture(e.pointerId);
    render();
    return;
  }
  if (E.tool === "measure") {
    E.measure = !E.measure || E.measure.length >= 2 ? [w] : [...E.measure, w];
    render();
    return;
  }
  if (["rect", "circle", "ellipse"].includes(E.tool)) {
    E.draft = { tool: E.tool, pts: [w], cur: w, dragging: true };
    e.currentTarget.setPointerCapture(e.pointerId);
    render();
    return;
  }
  // クリックで点を置いていく道具
  if (!E.draft || E.draft.tool !== E.tool) E.draft = { tool: E.tool, pts: [], cur: w };
  const d = E.draft;
  if (E.tool === "polygon" && d.pts.length >= 3 && G.dist(w, d.pts[0]) < vp.px(9)) { d.cur = null; commitDraft(); return; }
  d.pts.push(w);
  d.cur = w;
  const need = { rect3: 3, circle3: 3 }[E.tool];
  if (need && d.pts.length >= need) { d.cur = null; commitDraft(); return; }
  render();
}

function onMove(e) {
  if (!E.active) return;
  const w = vp.toWorld(e.clientX, e.clientY);
  $("#coords").textContent = `X ${w[0].toFixed(1)}  Y ${w[1].toFixed(1)} mm`;
  if (E.drag) {
    const loop = loops()[E.drag.li];
    if (!E.drag.moved) { pushHistory(); E.drag.moved = true; }
    if (E.drag.kind === "v") {
      const p = loop[E.drag.i];
      p[0] = E.drag.orig[0] + (w[0] - E.drag.start[0]);
      p[1] = E.drag.orig[1] + (w[1] - E.drag.start[1]);
    } else {
      const p0 = loop[E.drag.i], p1 = loop[(E.drag.i + 1) % loop.length];
      let b = G.bulgeThrough(p0, p1, w);
      if (Math.abs(b) * G.dist(p0, p1) / 2 < vp.px(4)) b = 0;      // ほぼまっすぐなら直線に吸着
      p0[2] = Math.max(-8, Math.min(8, b));
    }
    render();
    return;
  }
  if (E.draft) {
    E.draft.cur = w;
    render();
  }
}

function onUp(e) {
  if (!E.active) return;
  if (E.drag) {
    const moved = E.drag.moved;
    E.drag = null;
    if (moved) changed();
    return;
  }
  if (E.draft && E.draft.dragging) {
    E.draft.pts = [E.draft.pts[0], vp.toWorld(e.clientX, e.clientY)];
    E.draft.cur = null;
    commitDraft();
  }
}

// 点 w にいちばん近い辺
function nearestEdge(w) {
  let best = null;
  loops().forEach((loop, li) => {
    loop.forEach((p0, i) => {
      const n = G.nearestOnEdge(p0, loop[(i + 1) % loop.length], p0[2], w);
      if (!best || n.d < best.d) best = { li, i, t: n.t, d: n.d };
    });
  });
  return best;
}

// ダブルクリック。クリックのたびに図を描き直す（押した要素が作り直される）ので、ブラウザの dblclick は
// 当てにならない。押した時刻と位置から自分で判定し、辺も位置から探す
function isDouble(e) {
  const now = performance.now(), last = E.lastDown;
  E.lastDown = { t: now, x: e.clientX, y: e.clientY };
  if (last && now - last.t < 400 && Math.hypot(e.clientX - last.x, e.clientY - last.y) < 6) {
    E.lastDown = null;
    return true;
  }
  return false;
}

function onDouble(e) {
  if (E.tool === "select") {
    const hit = nearestEdge(vp.toWorld(e.clientX, e.clientY));
    if (hit && hit.d < vp.px(9) && hit.t > 0.02 && hit.t < 0.98) insertVertex(hit.li, hit.i, hit.t);
    return true;
  }
  if (E.tool === "polygon" && E.draft && E.draft.pts.length >= 3) {
    E.draft.cur = null;      // 1 回目のクリックで置いた点までで閉じる
    commitDraft();
    return true;
  }
  return false;
}

function onKey(e) {
  if (!E.active || isTyping(e)) return;
  const k = e.key;
  if ((e.ctrlKey || e.metaKey) && k.toLowerCase() === "z") { e.preventDefault(); e.shiftKey ? redo() : undo(); return; }
  if ((e.ctrlKey || e.metaKey) && k.toLowerCase() === "y") { e.preventDefault(); redo(); return; }
  if (k === "Escape") {
    if (E.draft) { E.draft = null; render(); }
    else if (E.measure) { E.measure = null; render(); }
    else if (E.tool !== "select") setTool("select");
    else { E.sel = null; render(); }
    return;
  }
  if (k === "Enter" && E.draft && E.tool === "polygon") { E.draft.cur = null; commitDraft(); return; }
  if (k === "Delete" || k === "Backspace") {
    if (E.sel && E.sel.kind === "v") { e.preventDefault(); deleteSelected(); }
    return;
  }
  if (k.toLowerCase() === "v" && !e.ctrlKey) { setTool("select"); return; }
  if (k.startsWith("Arrow") && E.sel && E.sel.kind === "v") {
    e.preventDefault();
    const step = e.shiftKey ? 1 : 0.1;
    const p = loops()[E.sel.li][E.sel.i];
    pushHistory();
    if (k === "ArrowLeft") p[0] -= step;
    if (k === "ArrowRight") p[0] += step;
    if (k === "ArrowUp") p[1] += step;
    if (k === "ArrowDown") p[1] -= step;
    changed();
  }
}

function setTool(tool) {
  E.tool = tool;
  E.draft = null;
  if (tool !== "measure") E.measure = null;
  $$("#tools button").forEach((b) => b.classList.toggle("active", b.dataset.tool === tool));
  $("#canvas").classList.toggle("tool-draw", tool !== "select");
  setHint(HINTS[tool]);
  render();
}

// ---- 検出し直す ----
async function redetect() {
  if (E.dirty && !confirm("手で直した内容は消えます。検出し直しますか？")) return;
  const thickness = parseFloat($("#in-thickness").value) || 0;
  const sensitivity = parseFloat($("#in-sensitivity").value);
  try {
    const r = await withBusy("板を検出しています…", () => api("POST", "/api/detect", { thickness_mm: thickness, sensitivity }));
    S.photo.candidates = r.candidates;
    S.photo.rect = r.rect;
    S.settings.thickness_mm = thickness;
    S.settings.sensitivity = sensitivity;
    adoptFirstCandidate();
    clearLayers();
    drawRectImage();
    changed();
    if (!r.candidates.length) toast("板が見つかりませんでした。感度を上げるか、図形の道具で描いてください", "bad", 6000);
  } catch (e) {
    toast(e.message, "bad");
  }
}

function adoptFirstCandidate() {
  const cands = (S.photo && S.photo.candidates) || [];
  S.outline = cands.length ? G.cloneShape(cands[0].shape) : null;
  S.candIndex = cands.length ? 0 : null;
  E.history = []; E.future = []; E.sel = null; E.dirty = false;
}

// ---- 出入り ----
export function initOutline() {
  const c = $("#canvas");
  c.addEventListener("pointerdown", onDown);
  c.addEventListener("pointermove", onMove);
  c.addEventListener("pointerup", onUp);
  window.addEventListener("keydown", onKey);
  vp.onChange(() => { if (E.active) render(); });
  $$("#tools button").forEach((b) => (b.onclick = () => setTool(b.dataset.tool)));
  $$("#boolmode button").forEach((b) => (b.onclick = () => {
    E.op = b.dataset.op;
    $$("#boolmode button").forEach((x) => x.classList.toggle("active", x === b));
    render();
  }));
  $("#btn-undo").onclick = undo;
  $("#btn-redo").onclick = redo;
  $("#btn-redetect").onclick = redetect;
  $("#btn-revert").onclick = () => {
    if (!E.dirty || confirm("手で直した内容を捨てて、検出した輪郭に戻しますか？")) { adoptFirstCandidate(); changed(); }
  };
  $("#btn-outline-back").onclick = () => nav.goto("photo");
  $("#btn-outline-next").onclick = async () => {
    await save();
    if (!S.outline) { toast("輪郭がありません", "bad"); return; }
    if (E.problems.length) { toast("輪郭に不具合があります。直してから進んでください", "bad"); return; }
    nav.goto("parts");
  };
}

// 新しい写真を読み込んだとき
export function resetOutline() {
  adoptFirstCandidate();
  E.problems = []; E.info = null;
}

// 保存してあった作業を開いたとき: 輪郭が候補のどれかと同じなら「手を入れていない」とみなす
export function restoreOutline() {
  const cands = (S.photo && S.photo.candidates) || [];
  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const k = S.outline ? cands.findIndex((c) => same(c.shape, S.outline)) : -1;
  S.candIndex = k >= 0 ? k : null;
  E.dirty = !!S.outline && k < 0;
  if (!S.outline && cands.length) adoptFirstCandidate();
}

export function enterOutline() {
  E.active = true;
  vp.flipY = true;
  clearLayers();
  drawRectImage();
  $("#in-thickness").value = S.settings.thickness_mm;
  $("#in-sensitivity").value = S.settings.sensitivity;
  setTool("select");
  fitToOutline();
  save();
}

export function leaveOutline() {
  E.active = false;
  E.drag = null; E.draft = null; E.measure = null;
  $("#canvas").classList.remove("tool-draw");
  $("#coords").textContent = "";
  if (E.saveTimer) save();
}
