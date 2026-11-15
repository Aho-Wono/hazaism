// 敷き詰めの「最良の結果の推移」グラフ（利用率 % と時間）。1 系列なので凡例は置かず、題名で名前を示す。
//   線は 2px の階段（良くなるまで同じ値が続く）、良くなった点に印。指でなぞると、その時点の数値が出る。

import { fmt, svg } from "./api.js";

const W = 320, H = 132;
const PAD = { l: 36, r: 46, t: 10, b: 24 };
const INK = { line: "#2563eb", grid: "#e6e9ef", text: "#667085", strong: "#1d2330", surface: "#ffffff" };

const niceMax = (v) => {
  for (const m of [10, 20, 25, 40, 50, 60, 80, 100]) if (v <= m) return m;
  return 100;
};

// history: [{t, utilization, placed, leftover}]（良くなった時だけの記録）。now: いまの経過秒。tmax: 横軸の終わり
export function drawChart(host, history, now, tmax) {
  host.replaceChildren();
  if (!history || !history.length) return;
  const T = Math.max(tmax || 0, now || 0, history[history.length - 1].t, 1);
  const top = niceMax(Math.max(...history.map((h) => h.utilization), 1));
  const x = (t) => PAD.l + (W - PAD.l - PAD.r) * (t / T);
  const y = (u) => PAD.t + (H - PAD.t - PAD.b) * (1 - u / top);

  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", role: "img",
    "aria-label": `利用率の推移。最後は ${fmt(history[history.length - 1].utilization, 1)}%` }, host);
  // 目盛り（横線は控えめに 3 本）
  for (const u of [0, top / 2, top]) {
    svg("line", { x1: PAD.l, x2: W - PAD.r, y1: y(u), y2: y(u), stroke: INK.grid, "stroke-width": 1 }, root);
    const t = svg("text", { x: PAD.l - 6, y: y(u), "text-anchor": "end", "dominant-baseline": "middle", class: "ch-tick" }, root);
    t.textContent = `${u}%`;
  }
  for (const t of [0, T / 2, T]) {
    const el = svg("text", { x: x(t), y: H - 6, "text-anchor": t === 0 ? "start" : t === T ? "end" : "middle", class: "ch-tick" }, root);
    el.textContent = `${fmt(t, 0)} 秒`;
  }
  // 階段の線: 0 秒から最初の結果まではまだ何もない。最後の値は「いま」まで伸ばす
  let d = "";
  history.forEach((h, i) => {
    d += i === 0 ? `M${x(h.t)} ${y(h.utilization)}` : `H${x(h.t)}V${y(h.utilization)}`;
  });
  const last = history[history.length - 1];
  d += `H${x(Math.max(now || last.t, last.t))}`;
  svg("path", { d, fill: "none", stroke: INK.line, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, root);
  for (const h of history) {
    svg("circle", { cx: x(h.t), cy: y(h.utilization), r: 4, fill: INK.line, stroke: INK.surface, "stroke-width": 2 }, root);
  }
  // 最後の値だけ線の端に書く（全部の点には書かない）
  const endX = x(Math.max(now || last.t, last.t));
  // 右側の余白（PAD.r）は、この文字のために空けてある
  const lab = svg("text", { x: endX + 6, y: y(last.utilization), "dominant-baseline": "middle", "text-anchor": "start", class: "ch-end" }, root);
  lab.textContent = `${fmt(last.utilization, 1)}%`;

  // なぞったときの縦線と読み取り（いちばん近い「良くなった点」に合わせる）
  const cross = svg("line", { y1: PAD.t, y2: H - PAD.b, stroke: INK.text, "stroke-width": 1, visibility: "hidden" }, root);
  const tip = document.createElement("div");
  tip.className = "ch-tip";
  tip.hidden = true;
  host.appendChild(tip);
  const hit = svg("rect", { x: PAD.l, y: 0, width: W - PAD.l - PAD.r, height: H, fill: "transparent" }, root);
  const showAt = (clientX) => {
    const box = root.getBoundingClientRect();
    const t = ((clientX - box.left) * (W / box.width) - PAD.l) / (W - PAD.l - PAD.r) * T;
    let h = history[0];
    for (const c of history) if (Math.abs(c.t - t) < Math.abs(h.t - t)) h = c;
    cross.setAttribute("x1", x(h.t));
    cross.setAttribute("x2", x(h.t));
    cross.setAttribute("visibility", "visible");
    tip.replaceChildren();
    const v = document.createElement("b");
    v.textContent = `${fmt(h.utilization, 1)}%`;
    const rest = document.createElement("span");
    rest.textContent = `  ${fmt(h.t, 1)} 秒・${h.placed} 個`;
    tip.append(v, rest);
    tip.hidden = false;
    const px = (x(h.t) / W) * box.width;
    tip.style.left = `${Math.max(0, Math.min(box.width - 150, px - 75))}px`;
  };
  hit.addEventListener("pointermove", (e) => showAt(e.clientX));
  hit.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); tip.hidden = true; });
}

// 同じ数値を表でも見られるようにする（色や線に頼らない読み方）
export function drawHistoryTable(host, history) {
  host.replaceChildren();
  if (!history || !history.length) return;
  const table = document.createElement("table");
  table.className = "result";
  const head = table.createTHead().insertRow();
  for (const [t, right] of [["時間", true], ["利用率", true], ["置けた数", true], ["残りの最大のかたまり", true]]) {
    const th = document.createElement("th");
    th.textContent = t;
    if (right) th.style.textAlign = "right";
    head.appendChild(th);
  }
  const body = table.createTBody();
  for (const h of history) {
    const row = body.insertRow();
    for (const v of [`${fmt(h.t, 1)} 秒`, `${fmt(h.utilization, 1)}%`, `${h.placed} 個`, `${fmt((h.leftover || 0) / 100, 1)} cm²`]) {
      const td = row.insertCell();
      td.className = "n";
      td.textContent = v;
    }
  }
  host.appendChild(table);
}
