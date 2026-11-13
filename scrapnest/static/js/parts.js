// ステップ 3: 敷き詰める部材（DXF）の登録

import { $, api, esc, fmt, toast, withBusy } from "./api.js";
import { S, msgHtml, nav, partColor, setHint } from "./ctx.js";
import { polylinesD } from "./geom.js";

let active = false;

export const partsOk = () => S.parts.some((p) => p.qty > 0);

function thumb(p) {
  const [minx, miny, maxx, maxy] = p.bounds;
  const m = 0.06 * Math.max(maxx - minx, maxy - miny, 1);
  // 図面は y が上向きなので、上下を反転して描く
  return `<svg viewBox="${minx - m} ${-maxy - m} ${maxx - minx + 2 * m} ${maxy - miny + 2 * m}" preserveAspectRatio="xMidYMid meet">
    <path d="${polylinesD(p.paths)}" transform="scale(1 -1)" fill="none" stroke="${partColor(p.id)}" stroke-width="1.6"
      vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>`;
}

function card(p) {
  const el = document.createElement("div");
  el.className = "part-card";
  el.dataset.id = p.id;
  const units = ["mm", "cm", "m", "inch"].map((u) => `<option value="${u}"${u === p.unit ? " selected" : ""}>${u}</option>`).join("");
  const rots = [["free", "回転 自由"], ["90", "90° 刻み"], ["fixed", "回転しない"]]
    .map(([v, t]) => `<option value="${v}"${v === p.rotation ? " selected" : ""}>${t}</option>`).join("");
  el.innerHTML = `
    <div class="thumb">${thumb(p)}</div>
    <div class="name" title="${esc(p.name)}">${esc(p.name)}</div>
    <div class="size">${fmt(p.width, 1)} × ${fmt(p.height, 1)} mm　${fmt(p.area / 100, 1)} cm²　図形 ${p.entities} 個</div>
    <div class="ctl">
      <label>個数 <input type="number" class="qty" min="0" max="999" step="1" value="${p.qty}"></label>
      <label title="裏返して配置しません"><input type="checkbox" class="noflip"${p.no_flip ? " checked" : ""}> 反転禁止</label>
    </div>
    <div class="ctl">
      <select class="rot" title="配置するときの回転">${rots}</select>
      <label>単位 <select class="unit" title="図面の単位。外形寸法が実物と合うものを選びます">${units}</select></label>
      <button class="ghost small del" title="この部材を一覧から外す">削除</button>
    </div>
    ${p.warnings.map((w) => `<div class="warn">${esc(w)}</div>`).join("")}`;
  const patch = async (body) => {
    try {
      const np = await api("PATCH", `/api/parts/${p.id}`, body);
      S.parts[S.parts.findIndex((x) => x.id === p.id)] = np;
      S.result = null;
      if ("unit" in body) render(); else summary();
      nav.refresh();
    } catch (e) {
      toast(e.message, "bad");
      render();
    }
  };
  el.querySelector(".qty").onchange = (e) => patch({ qty: Math.max(0, Math.round(+e.target.value || 0)) });
  el.querySelector(".noflip").onchange = (e) => patch({ no_flip: e.target.checked });
  el.querySelector(".rot").onchange = (e) => patch({ rotation: e.target.value });
  el.querySelector(".unit").onchange = (e) => patch({ unit: e.target.value });
  el.querySelector(".del").onclick = async () => {
    try {
      await api("DELETE", `/api/parts/${p.id}`);
      S.parts = S.parts.filter((x) => x.id !== p.id);
      S.result = null;
      render();
      nav.refresh();
    } catch (e) {
      toast(e.message, "bad");
    }
  };
  return el;
}

function summary() {
  const n = S.parts.reduce((a, p) => a + p.qty, 0);
  const area = S.parts.reduce((a, p) => a + p.qty * p.area, 0);
  $("#parts-summary").innerHTML = S.parts.length
    ? `<span class="k">部材</span><b>${S.parts.length} 種類・${n} 個</b><br><span class="k">合計面積</span><b>${fmt(area / 100, 1)} cm²</b>`
    : "";
  $("#btn-parts-next").disabled = !partsOk();
}

function render() {
  if (!active) return;
  const board = $("#parts-board");
  board.replaceChildren();
  if (!S.parts.length) {
    const e = document.createElement("div");
    e.className = "empty";
    e.innerHTML = "ここに DXF ファイルをドラッグ＆ドロップ<br>（または右の「DXF を追加」）";
    board.appendChild(e);
  }
  for (const p of S.parts) board.appendChild(card(p));
  summary();
}

export async function addPartFiles(files) {
  const list = [...files].filter((f) => /\.dxf$/i.test(f.name));
  if (!list.length) { toast("DXF ファイルを選んでください", "bad"); return; }
  const fd = new FormData();
  for (const f of list) fd.append("files", f, f.name);
  try {
    const r = await withBusy("DXF を読み込んでいます…", () => api("POST", "/api/parts", fd));
    S.parts.push(...r.added);
    if (r.added.length) S.result = null;
    const box = $("#parts-errors");
    box.replaceChildren();
    for (const e of r.errors) box.appendChild(msgHtml("bad", `${e.name}: ${e.error}`));
    if (r.added.length) toast(`${r.added.length} 個の部材を追加しました`, "ok");
    render();
    nav.refresh();
  } catch (e) {
    toast(e.message, "bad");
  }
}

export function initParts() {
  $("#btn-pick-parts").onclick = () => $("#file-parts").click();
  $("#file-parts").onchange = (e) => { addPartFiles(e.target.files); e.target.value = ""; };
  $("#btn-parts-back").onclick = () => nav.goto("outline");
  $("#btn-parts-next").onclick = () => nav.goto("nest");
}

export function enterParts() {
  active = true;
  $("#parts-board").hidden = false;
  setHint("");
  render();
}

export function leaveParts() {
  active = false;
  $("#parts-board").hidden = true;
}
