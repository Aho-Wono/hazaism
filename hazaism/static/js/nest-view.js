// ステップ 4: 敷き詰め

import { $, api, esc, fmt, toast } from "./api.js";
import { releaseNest, warmNest } from "./backend.js";
import { S, clearLayers, drawRectImage, msgHtml, nav, partColor, setHint, vp } from "./ctx.js";
import { drawPlacements, drawScrap, fitToScrap } from "./layout.js";
import { drawChart, drawHistoryTable } from "./nest-chart.js";

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

const PHASE = {
  prepare: "準備しています…（部材の向きごとの画像を作っています）",
  warmup: "敷き詰めの作業スレッドを起動しています…",
  verify: "結果を検査しています…",
};

// 数値の一覧（左に名前、右に値）
function metric(label, value, note = "") {
  return `<div class="m"><span class="ml">${label}</span><span class="mv">${value}</span>${note ? `<span class="mn">${note}</span>` : ""}</div>`;
}

function showMetrics(st, r, running) {
  const el = $("#nest-metrics");
  const m = (r && r.metrics) || {};
  const runs = running ? st.runs : m.runs;
  const elapsed = running ? st.elapsed : m.elapsed;
  const rps = running ? st.runs_per_sec : m.runs_per_sec;
  const workers = running ? st.workers : m.workers;
  const history = running ? st.history : m.history;
  if (!r && !running) { el.replaceChildren(); $("#nest-chart").replaceChildren(); $("#nest-history").hidden = true; return; }
  const rows = [];
  if (!running && m.prepare_s !== undefined && m.verify_s !== undefined) {
    // 計算時間のほかに、準備（部材の画像づくり）と確かめ（元の図形での検査）にかかった時間
    const total = m.prepare_s + (elapsed || 0) + m.verify_s;
    rows.push(metric("かかった時間", `${fmt(total, 1)} 秒`, `準備 ${fmt(m.prepare_s, 1)}・計算 ${fmt(elapsed || 0, 1)}・確かめ ${fmt(m.verify_s, 1)}`));
  } else if (elapsed !== undefined) {
    rows.push(metric("経過時間", `${fmt(elapsed || 0, 1)} 秒`, running ? `/ ${fmt(st.time_limit, 0)} 秒` : ""));
  }
  if (runs !== undefined) rows.push(metric("試した置き方", `${runs || 0} 通り`, rps ? `1 秒あたり ${fmt(rps, 1)}` : ""));
  if (workers) rows.push(metric("作業スレッド", `${workers} 本`));
  if (r) {
    rows.push(metric("利用率", `${fmt(r.utilization, 1)}%`, `${fmt(r.area / 100, 1)} / ${fmt(r.scrap_area / 100, 1)} cm²`));
    rows.push(metric("置けた数", `${r.placed} / ${r.requested} 個`));
    if (m.leftover !== undefined) rows.push(metric("残りの最大のかたまり", `${fmt(m.leftover / 100, 1)} cm²`, "次に使える大きさの目安"));
    if (!running && m.min_spacing !== undefined) {
      rows.push(metric("最小の間隔（実測）", m.min_spacing === null ? "—" : `${fmt(m.min_spacing, 2)} mm`, `設定 ${fmt(r.spacing, 1)} mm`));
      rows.push(metric("最小の余白（実測）", m.min_margin === null ? "—" : `${fmt(m.min_margin, 2)} mm`, `設定 ${fmt(r.margin, 1)} mm`));
    }
  }
  el.innerHTML = rows.join("");
  drawChart($("#nest-chart"), history, elapsed, running ? st.time_limit : elapsed);
  $("#nest-history").hidden = !(history && history.length);
  drawHistoryTable($("#nest-history-table"), history);
}

// st: /api/nest/status の返事（state は idle / running / done、phase は running の中の段階）
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
  if (running) $("#nest-bar").style.width = Math.min(100, 100 * (st.elapsed || 0) / st.time_limit) + "%";
  $("#btn-nest-next").disabled = running || !(S.result && S.result.placements.length);
  showMetrics(st, r, running);

  const el = $("#nest-status");
  if (st.error) {
    el.replaceChildren(msgHtml("bad", "計算中にエラーが起きました: " + String(st.error).split("\n")[0]));
    return;
  }
  if (running && st.phase && PHASE[st.phase]) {
    el.textContent = PHASE[st.phase];
    return;
  }
  if (!r) {
    el.innerHTML = running ? "計算しています…" : (st.state === "done" ? "1 個も置けませんでした。間隔や余白、部材の大きさを確認してください。" : "");
    return;
  }
  el.textContent = running ? "計算中…" : (st.state === "done" ? "完了しました" : "前回の結果");
  if (r.dropped) el.appendChild(msgHtml("warn", `検査で ${r.dropped} 個を外しました（間隔か余白が足りなかったため）`));
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
  if (st.state === "done") S.result = st.result || null;
  show(st);
  nav.refresh();
  if (st.state === "running") timer = setTimeout(poll, st.time_limit && st.time_limit <= 5 ? 200 : 500);
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
  show({ state: "running", phase: "prepare", elapsed: 0, time_limit: params.time_limit, runs: 0, result: null, history: [] });
  nav.refresh();
  timer = setTimeout(poll, 200);
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
  warmNest();                 // 作業スレッドを先に起動しておく（「敷き詰める」を押してから待たないように）
  poll();
}

export function leaveNest() {
  active = false;
  releaseNest();              // 計算中でなければ、作業スレッドを止めてメモリを空ける
}
