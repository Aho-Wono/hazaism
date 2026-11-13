// ステップ 4: 敷き詰め

import { $, api, esc, fmt, toast } from "./api.js";
import { S, clearLayers, drawRectImage, msgHtml, nav, partColor, setHint, vp } from "./ctx.js";
import { drawPlacements, drawScrap, fitToScrap } from "./layout.js";

let active = false;
let timer = null;
let shownKey = "";     // いま図に出している配置（同じなら描き直さない）

const IDS = { spacing_mm: "#in-spacing", margin_mm: "#in-margin", angle_step: "#in-angle", time_limit: "#in-time" };

function readParams() {
  const p = {};
  for (const [k, id] of Object.entries(IDS)) p[k] = parseFloat($(id).value);
  p.use_holes = $("#in-holes").checked;
  return p;
}

function fillParams() {
  for (const [k, id] of Object.entries(IDS)) {
    const el = $(id);
    el.value = S.settings[k];
    if (el.tagName === "SELECT" && el.value !== String(S.settings[k])) {      // 一覧にない値なら足す
      const o = document.createElement("option");
      o.value = S.settings[k];
      o.textContent = k === "angle_step" ? `${S.settings[k]}°` : `${S.settings[k]} 秒`;
      el.appendChild(o);
      el.value = S.settings[k];
    }
  }
  $("#in-holes").checked = !!S.settings.use_holes;
}

function table(result) {
  const el = $("#nest-table");
  if (!result) { el.replaceChildren(); return; }
  const rows = S.parts.filter((p) => result.counts[p.id]).map((p) => {
    const [got, want] = result.counts[p.id];
    return `<tr class="${got < want ? "short" : ""}"><td><span class="chip" style="background:${partColor(p.id)}"></span>${esc(p.name)}</td>
      <td class="n">${got} / ${want}</td></tr>`;
  }).join("");
  el.innerHTML = `<table class="result"><thead><tr><th>部材</th><th style="text-align:right">置けた数</th></tr></thead><tbody>${rows}</tbody></table>`;
}

// st: サーバーの /api/nest/status の返事（state は idle / running / done）
function show(st) {
  if (!active) return;
  const running = st.state === "running";
  const r = running ? st.result : S.result;
  const key = JSON.stringify(r ? r.placements : null);
  if (key !== shownKey) {
    shownKey = key;
    drawPlacements(r ? r.placements : []);
  }
  table(r);
  $("#btn-nest-start").disabled = running;
  $("#btn-nest-stop").disabled = !running;
  $("#nest-progress").hidden = !running;
  if (running) $("#nest-bar").style.width = Math.min(100, 100 * st.elapsed / st.time_limit) + "%";
  $("#btn-nest-next").disabled = running || !(S.result && S.result.placements.length);

  const el = $("#nest-status");
  if (st.error) {
    el.replaceChildren(msgHtml("bad", "計算中にエラーが起きました: " + String(st.error).split("\n")[0]));
    return;
  }
  if (!r) {
    el.innerHTML = running ? "準備しています…" : (st.state === "done" ? "1 個も置けませんでした。間隔や余白、部材の大きさを確認してください。" : "");
    return;
  }
  const head = running ? `計算中…（${st.runs} 通り試しました）` : (st.state === "done" ? `完了（${st.runs} 通り試しました）` : "前回の結果");
  el.innerHTML = `${head}<br><span class="k">置けた数</span><b>${r.placed} / ${r.requested} 個</b><br>` +
    `<span class="k">使った面積</span><b>${fmt(r.area / 100, 1)} cm²（端材の ${fmt(r.utilization, 1)}%）</b>` +
    (r.dropped ? `<br>検査で ${r.dropped} 個を外しました` : "");
  if (!running && r.placed < r.requested) {
    el.appendChild(msgHtml("warn", `${r.requested - r.placed} 個は入りきりませんでした。間隔や余白を小さくするか、計算時間を延ばすと入ることがあります。`));
  }
}

async function poll() {
  clearTimeout(timer);
  timer = null;
  let st;
  try {
    st = await api("GET", "/api/nest/status");
  } catch (e) {
    toast(e.message, "bad");
    return;
  }
  if (st.state !== "running") S.result = st.result;
  show(st);
  nav.refresh();
  if (st.state === "running") timer = setTimeout(poll, 600);
}

async function start() {
  const params = readParams();
  Object.assign(S.settings, params);
  S.result = null;
  try {
    await api("POST", "/api/nest/start", { shape: S.outline, params });
  } catch (e) {
    toast(e.message, "bad");
    return;
  }
  show({ state: "running", elapsed: 0, time_limit: params.time_limit, runs: 0, result: null });
  nav.refresh();
  timer = setTimeout(poll, 400);
}

export function initNest() {
  $("#btn-nest-start").onclick = start;
  $("#btn-nest-stop").onclick = () => api("POST", "/api/nest/stop").catch((e) => toast(e.message, "bad"));
  $("#btn-nest-back").onclick = () => nav.goto("parts");
  $("#btn-nest-next").onclick = () => nav.goto("export");
}

export function enterNest() {
  active = true;
  vp.flipY = true;
  clearLayers();
  drawRectImage(0.35);
  drawScrap();
  fillParams();
  fitToScrap(vp);
  setHint("");
  shownKey = "";
  show({ state: "idle" });
  poll();
}

export function leaveNest() {
  active = false;
}
