// 輪郭の幾何。Loop = [[x, y, bulge], ...]（閉じた列。bulge はその頂点から次の頂点までの円弧、0 は直線、正は反時計回り）

export const dist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);

// bulge つきの辺 → 円弧（中心・半径・開始角・掃引角）
export function arcOf(p0, p1, b) {
  const dx = p1[0] - p0[0], dy = p1[1] - p0[1];
  const L = Math.hypot(dx, dy);
  const sweep = 4 * Math.atan(b);
  const r = L / (2 * Math.sin(Math.abs(sweep) / 2));
  const d = (L / 2) / Math.tan(sweep / 2);
  const cx = (p0[0] + p1[0]) / 2 + (-dy / L) * d;
  const cy = (p0[1] + p1[1]) / 2 + (dx / L) * d;
  return { cx, cy, r, a0: Math.atan2(p0[1] - cy, p0[0] - cx), sweep };
}

const isLine = (b) => Math.abs(b) < 1e-9;

export function edgeD(p0, p1, b) {
  if (isLine(b) || dist(p0, p1) < 1e-9) return `L${p1[0]} ${p1[1]}`;
  const a = arcOf(p0, p1, b);
  return `A${a.r} ${a.r} 0 ${Math.abs(a.sweep) > Math.PI ? 1 : 0} ${b > 0 ? 1 : 0} ${p1[0]} ${p1[1]}`;
}

export function loopD(loop) {
  if (!loop.length) return "";
  let d = `M${loop[0][0]} ${loop[0][1]}`;
  for (let i = 0; i < loop.length; i++) {
    d += edgeD(loop[i], loop[(i + 1) % loop.length], loop[i][2]);
  }
  return d + "Z";
}

export const shapeD = (shape) => shape ? [shape.outer, ...(shape.holes || [])].map(loopD).join("") : "";

// 辺の上の点（t は 0〜1）
export function edgePoint(p0, p1, b, t) {
  if (isLine(b)) return [p0[0] + (p1[0] - p0[0]) * t, p0[1] + (p1[1] - p0[1]) * t];
  const a = arcOf(p0, p1, b);
  const ang = a.a0 + a.sweep * t;
  return [a.cx + a.r * Math.cos(ang), a.cy + a.r * Math.sin(ang)];
}

export function edgeLength(p0, p1, b) {
  if (isLine(b)) return dist(p0, p1);
  const a = arcOf(p0, p1, b);
  return Math.abs(a.sweep) * a.r;
}

// 点 q にいちばん近い、辺の上の点
export function nearestOnEdge(p0, p1, b, q) {
  if (isLine(b)) {
    const dx = p1[0] - p0[0], dy = p1[1] - p0[1];
    const L2 = dx * dx + dy * dy || 1e-12;
    const t = Math.max(0, Math.min(1, ((q[0] - p0[0]) * dx + (q[1] - p0[1]) * dy) / L2));
    const p = [p0[0] + dx * t, p0[1] + dy * t];
    return { p, t, d: dist(p, q) };
  }
  const a = arcOf(p0, p1, b);
  let rel = Math.atan2(q[1] - a.cy, q[0] - a.cx) - a.a0;
  const sgn = a.sweep > 0 ? 1 : -1;
  rel = ((rel * sgn) % (2 * Math.PI) + 2 * Math.PI) % (2 * Math.PI);     // 掃引の向きに測った角
  const span = Math.abs(a.sweep);
  let t;
  if (rel <= span) t = rel / span;
  else t = (rel - span) < (2 * Math.PI - rel) ? 1 : 0;                      // 円弧の外なら近い方の端
  const p = edgePoint(p0, p1, b, t);
  return { p, t, d: dist(p, q) };
}

// p0 → p1 の円弧が点 q を通るときの bulge
export function bulgeThrough(p0, p1, q) {
  const ax = p0[0] - q[0], ay = p0[1] - q[1], bx = p1[0] - q[0], by = p1[1] - q[1];
  const la = Math.hypot(ax, ay), lb = Math.hypot(bx, by);
  if (la < 1e-9 || lb < 1e-9) return 0;
  const ang = Math.acos(Math.max(-1, Math.min(1, (ax * bx + ay * by) / (la * lb))));   // q から両端を見込む角
  const side = (p1[0] - p0[0]) * (q[1] - p0[1]) - (p1[1] - p0[1]) * (q[0] - p0[0]);     // 正なら q は進行方向の左
  const b = Math.tan((Math.PI - ang) / 2);
  return side > 0 ? -b : b;       // 反時計回りの円弧は、進行方向の右へふくらむ
}

// 辺を t の位置で 2 つに分けたときの、それぞれの bulge
export function splitBulge(b, t) {
  if (isLine(b)) return [0, 0];
  const sweep = 4 * Math.atan(b);
  return [Math.tan(sweep * t / 4), Math.tan(sweep * (1 - t) / 4)];
}

export function loopBounds(loops) {
  let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
  for (const loop of loops) {
    for (let i = 0; i < loop.length; i++) {
      const p0 = loop[i], p1 = loop[(i + 1) % loop.length];
      const n = isLine(p0[2]) ? 1 : 16;
      for (let k = 0; k < n; k++) {
        const p = edgePoint(p0, p1, p0[2], k / n);
        minx = Math.min(minx, p[0]); maxx = Math.max(maxx, p[0]);
        miny = Math.min(miny, p[1]); maxy = Math.max(maxy, p[1]);
      }
    }
  }
  return [minx, miny, maxx, maxy];
}

export const cloneShape = (s) => s ? { outer: s.outer.map((p) => [...p]), holes: (s.holes || []).map((h) => h.map((p) => [...p])) } : null;

// 3 点を通る円
export function circle3(a, b, c) {
  const d = 2 * (a[0] * (b[1] - c[1]) + b[0] * (c[1] - a[1]) + c[0] * (a[1] - b[1]));
  if (Math.abs(d) < 1e-9) return null;
  const a2 = a[0] ** 2 + a[1] ** 2, b2 = b[0] ** 2 + b[1] ** 2, c2 = c[0] ** 2 + c[1] ** 2;
  const cx = (a2 * (b[1] - c[1]) + b2 * (c[1] - a[1]) + c2 * (a[1] - b[1])) / d;
  const cy = (a2 * (c[0] - b[0]) + b2 * (a[0] - c[0]) + c2 * (b[0] - a[0])) / d;
  return { cx, cy, r: Math.hypot(a[0] - cx, a[1] - cy) };
}

// 折れ線（[[x, y], ...] の列）→ SVG の path
export function polylinesD(paths) {
  let d = "";
  for (const p of paths) {
    if (p.length < 2) continue;
    d += "M" + p.map((q) => `${q[0]} ${q[1]}`).join("L");
    const a = p[0], b = p[p.length - 1];
    if (Math.abs(a[0] - b[0]) < 1e-6 && Math.abs(a[1] - b[1]) < 1e-6) d += "Z";
  }
  return d;
}
