// 敷き詰めの探索（JavaScript）。Python（hazaism/nest/engine.py の prepare）が作った格子の画像を受け取り、
// 「置けない位置」の計算・貪欲配置・部分こわし直しを、時間いっぱい繰り返す。
// DOM を使わないので、作業スレッド（nest-worker.js）でも、検査のページでも動く。
//
// 格子の画像はどれも、行ごとに 32 ビットの整数（Int32Array）に詰めてある。画素 i は 語 i >> 5 の ビット i & 31。
// 位置 (x, y) は、部材のマスクの左上の画素を格子のどこに置くか（格子の画素の番号）。
//
// 1 回の貪欲配置（greedy）:
//   部材を 1 個ずつ、候補の向きそれぞれについて「置けない位置」B を求め（端材に対するもの base ＋ 置いた部材との重なり nfr）、
//   「寄せる方向」に対して部材の先端がいちばん手前に収まる位置と向きを選ぶ（奥から順に詰まり、残りがひとかたまりで残りやすい）。
// 配置どうしの比較は ①置けた面積 ②置けた個数 ③残った空き領域のうち最大のひとかたまりの面積。
// 部材は「間隔の半分」太らせた形で、画素が 1 つでも重なれば置けないとするので、結果は間隔と余白を満たす
// （最後に Python の verify.py で元の図形でも確かめる）。

export const N_GRAVITY = 8;
const CACHE_BASES = 400;          // 覚えておく「端材に対する置けない位置」の数
const CACHE_SHAPES = 6000;        // 覚えておく「置いた部材に対する置けない位置の形」の数（向きの組ごと。1 つ数 KB）

// ---- データを読む ----------------------------------------------------------------------------

function rowRuns(data, h, w, words) {
  // 行ごとの、1 が続く区間 [s, e) の並び（Int32Array に s, e, s, e, …）
  const out = [];
  const tmp = [];
  for (let j = 0; j < h; j++) {
    tmp.length = 0;
    let inRun = false;
    for (let i = 0; i < w; i++) {
      const bit = (data[j * words + (i >> 5)] >>> (i & 31)) & 1;
      if (bit && !inRun) { tmp.push(i); inRun = true; }
      else if (!bit && inRun) { tmp.push(i); inRun = false; }
    }
    if (inRun) tmp.push(w);
    out.push(Int32Array.from(tmp));
  }
  return out;
}

function freeRows(data, h, w, words) {
  // 0（空き）が続く区間の並び
  const out = [];
  const tmp = [];
  for (let j = 0; j < h; j++) {
    tmp.length = 0;
    let inRun = false;
    for (let i = 0; i < w; i++) {
      const free = ((data[j * words + (i >> 5)] >>> (i & 31)) & 1) === 0;
      if (free && !inRun) { tmp.push(i); inRun = true; }
      else if (!free && inRun) { tmp.push(i); inRun = false; }
    }
    if (inRun) tmp.push(w);
    out.push(Int32Array.from(tmp));
  }
  return out;
}

// prepare() のデータ（ArrayBuffer か Uint8Array）を読む
export function decodeProblem(input) {
  const u8 = input instanceof Uint8Array ? input : new Uint8Array(input);
  if (String.fromCharCode(u8[0], u8[1], u8[2], u8[3]) !== "HZN1") throw new Error("敷き詰めのデータではありません");
  const n = new DataView(u8.buffer, u8.byteOffset, u8.byteLength).getUint32(4, true);
  const head = JSON.parse(new TextDecoder().decode(u8.subarray(8, 8 + n)));
  const base = 8 + n;
  const words = (off, count) => {             // 4 バイト境界にそろった新しい配列に写す（ブラウザはどれも little endian）
    const out = new Int32Array(count);
    new Uint8Array(out.buffer).set(u8.subarray(base + off, base + off + count * 4));
    return out;
  };
  const g = head.grid;
  const blocked = { h: g.H, w: g.W, words: head.blocked.words, data: words(head.blocked.off, g.H * head.blocked.words) };
  const orients = head.orients.map((o) => {
    const data = words(o.off, o.h * o.words);
    const runs = rowRuns(data, o.h, o.w, o.words);
    // 同じ区間を 1 本の配列にも並べる（行 a の区間は rs[ro[a]] .. rs[ro[a + 1] − 1]）。計算の速い道で使う
    const ro = new Int32Array(o.h + 1);
    for (let a = 0; a < o.h; a++) ro[a + 1] = ro[a] + runs[a].length;
    const rs = new Int32Array(ro[o.h]);
    runs.forEach((r, a) => rs.set(r, ro[a]));
    const flat = [];
    runs.forEach((r, a) => { for (let t = 0; t < r.length; t += 2) flat.push(a, r[t], r[t + 1]); });
    return { part: o.part, angle: o.angle, flip: o.flip, h: o.h, w: o.w, mx0: o.mx0, my0: o.my0, words: o.words,
             data, runs, ro, rs, runFlat: Int32Array.from(flat) };
  });
  return { grid: g, blocked, parts: head.parts, orients, params: head.params };
}

// ---- ビットの小道具 ------------------------------------------------------------------------------

// dst の行（dbase から dwords 語）に、src の行（sbase から swords 語）を X ビットずらして OR する。はみ出す分は捨てる
function orInto(dst, dbase, dwords, src, sbase, swords, X) {
  const ws = X >> 5, bs = X & 31;
  const iEnd = Math.min(swords, dwords - ws);
  if (bs === 0) {
    for (let i = Math.max(0, -ws); i < iEnd; i++) {
      const v = src[sbase + i];
      if (v) dst[dbase + ws + i] |= v;
    }
    return;
  }
  const rs = 32 - bs;
  for (let i = Math.max(0, -ws - 1); i < iEnd; i++) {
    const v = src[sbase + i];
    if (!v) continue;
    const k = ws + i;
    if (k >= 0) dst[dbase + k] |= v << bs;
    if (k + 1 < dwords) dst[dbase + k + 1] |= v >>> rs;
  }
}

// 行のビット [x0, x1) を 1 にする
function setBits(A, base, x0, x1) {
  if (x0 >= x1) return;
  const w0 = x0 >> 5, w1 = (x1 - 1) >> 5;
  const m0 = -1 << (x0 & 31);
  const m1 = -1 >>> (31 - ((x1 - 1) & 31));
  if (w0 === w1) { A[base + w0] |= m0 & m1; return; }
  A[base + w0] |= m0;
  for (let w = w0 + 1; w < w1; w++) A[base + w] = -1;
  A[base + w1] |= m1;
}

// 重ならないよう左から並べた区間の表 IV（lo, hi, lo, hi, …、n 個）に [l, h) を足す（重なる・接するものとは合わせる）。
// 戻り値は新しい個数。合わさった区間はふつう数個しかないので、前から順に探せば足りる
function putIv(IV, n, l, h) {
  if (l >= h) return n;
  let s = 0;
  while (s < n && IV[2 * s + 1] < l) s++;
  let e = s;
  while (e < n && IV[2 * e] <= h) e++;
  if (s === e) {
    for (let k = 2 * n + 1; k > 2 * s + 1; k--) IV[k] = IV[k - 2];
    IV[2 * s] = l; IV[2 * s + 1] = h;
    return n + 1;
  }
  if (IV[2 * s] > l) IV[2 * s] = l;
  IV[2 * s + 1] = IV[2 * e - 1] > h ? IV[2 * e - 1] : h;
  const del = e - s - 1;
  if (del) {
    for (let k = 2 * (s + 1); k + 2 * del < 2 * n; k++) IV[k] = IV[k + 2 * del];
    n -= del;
  }
  return n;
}

// 行のビット [x0, x1) を 0 にする
function clearBits(A, base, x0, x1) {
  if (x0 >= x1) return;
  const w0 = x0 >> 5, w1 = (x1 - 1) >> 5;
  const m0 = -1 << (x0 & 31);
  const m1 = -1 >>> (31 - ((x1 - 1) & 31));
  if (w0 === w1) { A[base + w0] &= ~(m0 & m1); return; }
  A[base + w0] &= ~m0;
  for (let w = w0 + 1; w < w1; w++) A[base + w] = 0;
  A[base + w1] &= ~m1;
}

// 部材（行ごとの区間 oRuns、h 行）の左上を置いたとき、各行の区間が空き区間（free）のどれかに収まる x の範囲。
// 行 y ごとに onRow(y, 区間の並び, 区間の数) を呼ぶ（区間は [lo, hi)、x は 0..Wk-1 の中）
function fitRows(free, oRuns, h, Hk, Wk, cap, onRow) {
  let cur = new Int32Array(cap), nxt = new Int32Array(cap);
  for (let y = 0; y < Hk; y++) {
    let n = 1;
    cur[0] = 0; cur[1] = Wk;
    for (let a = 0; a < h && n > 0; a++) {
      const R = oRuns[a];
      if (R.length === 0) continue;
      const F = free[y + a];
      for (let t = 0; t < R.length && n > 0; t += 2) {
        const s = R[t], e = R[t + 1];
        // [x + s, x + e) が空き区間 [L, R) に入る ⇔ x ∈ [L − s, R − e + 1)
        let m = 0, i = 0, j = 0;
        while (i < 2 * n && j < F.length) {
          const b0 = F[j] - s, b1 = F[j + 1] - e + 1;
          if (b1 <= b0) { j += 2; continue; }
          const a0 = cur[i], a1 = cur[i + 1];
          const lo = a0 > b0 ? a0 : b0, hi = a1 < b1 ? a1 : b1;
          if (lo < hi) { nxt[m++] = lo; nxt[m++] = hi; }
          if (a1 < b1) i += 2; else j += 2;
        }
        const t2 = cur; cur = nxt; nxt = t2;
        n = m >> 1;
      }
    }
    if (n > 0) onRow(y, cur, n);
  }
}

// ---- 乱数（種つき。sfc32） -------------------------------------------------------------------------

export function makeRng(seed) {
  let a = 0x9e3779b9 ^ seed, b = 0x243f6a88, c = 0xb7e15162, d = seed | 0;
  const next = () => {
    a |= 0; b |= 0; c |= 0; d |= 0;
    const t = (((a + b) | 0) + d) | 0;
    d = (d + 1) | 0;
    a = b ^ (b >>> 9);
    b = (c + (c << 3)) | 0;
    c = (c << 21) | (c >>> 11);
    c = (c + t) | 0;
    return (t >>> 0) / 4294967296;
  };
  for (let i = 0; i < 20; i++) next();
  let spare = null;
  return {
    random: next,
    int: (n) => Math.floor(next() * n),                      // 0 .. n-1
    normal() {
      if (spare !== null) { const v = spare; spare = null; return v; }
      let u = 0, v = 0;
      while (u === 0) u = next();
      v = next();
      const r = Math.sqrt(-2 * Math.log(u));
      spare = r * Math.sin(2 * Math.PI * v);
      return r * Math.cos(2 * Math.PI * v);
    },
    sample(n, k) {                                            // 0..n-1 から重複なしで k 個
      const idx = Array.from({ length: n }, (_, i) => i);
      for (let i = 0; i < k; i++) {
        const j = i + Math.floor(next() * (n - i));
        const tmp = idx[i]; idx[i] = idx[j]; idx[j] = tmp;
      }
      return idx.slice(0, k);
    },
  };
}

// ---- 1 本の作業スレッドの計算 ---------------------------------------------------------------------

// 1 回の貪欲配置で試す向きの上限（計算時間が短いときは少なくして、回数を稼ぐ）
export function maxOrientsFor(timeLimit) {
  if (timeLimit <= 1) return 16;
  if (timeLimit <= 5) return 24;
  if (timeLimit <= 30) return 36;
  return 60;
}

export class Nester {
  constructor(problem, opts = {}) {
    this.p = problem;
    const g = problem.grid;
    this.W = g.W; this.H = g.H; this.res = g.res; this.x0 = g.x0; this.y0 = g.y0;
    this.parts = problem.parts;
    this.orients = problem.orients;
    this.maxOrients = opts.maxOrients || maxOrientsFor(problem.params.time_limit || 10);
    this.free = freeRows(problem.blocked.data, this.H, this.W, problem.blocked.words);
    this.cap = 2 * (this.W + 4);
    this.coef = [];
    for (let k = 0; k < N_GRAVITY; k++) {
      const a = 2 * Math.PI * k / N_GRAVITY, gx = Math.cos(a), gy = Math.sin(a);
      // 寄せる方向の「奥行き」。わずかに横方向の成分を足して同点をなくす（Python 版と同じ）
      this.coef.push({ gx, gy, cx: gx - 1e-3 * gy, cy: gy + 1e-3 * gx });
    }
    this.bases = new Map();
    this.shapes = new Map();
    this.oriFixed = []; this.oriRest = []; this.oriPool = [];      // 部材ごとの、試す向き（plan を参照）
    this.ext = new Map();
    // 残りのかたまりを測るときの円（直径 8 mm ほど。Python 版の収縮と同じ大きさ）
    const ksz = Math.max(3, Math.round(8.0 / this.res) | 1), r = (ksz - 1) / 2;
    this.disk = [];
    for (let j = 0; j < ksz; j++) {
      const dx = Math.round(Math.sqrt(Math.max(0, r * r - (j - r) * (j - r))));
      this.disk.push(Int32Array.from([r - dx, r + dx + 1]));
    }
  }

  dims(oi) {
    const o = this.orients[oi];
    const Hk = this.H - o.h + 1, Wk = this.W - o.w + 1;
    return { o, Hk, Wk, KW: (Wk + 31) >> 5 };
  }

  // 端材だけに対する「置けない位置」（1 = 置けない）。どこにも置けなければ null。覚えておいて、毎回写して使う
  base(oi) {
    if (this.bases.has(oi)) {
      const v = this.bases.get(oi);
      this.bases.delete(oi); this.bases.set(oi, v);
      return v;
    }
    const { o, Hk, Wk, KW } = this.dims(oi);
    let B = null;
    if (Hk > 0 && Wk > 0) {
      B = new Int32Array(Hk * KW).fill(-1);
      let any = false;
      fitRows(this.free, o.runs, o.h, Hk, Wk, this.cap, (y, iv, n) => {
        any = true;
        for (let t = 0; t < 2 * n; t += 2) clearBits(B, y * KW, iv[t], iv[t + 1]);
      });
      if (!any) B = null;
    }
    this.bases.set(oi, B);
    if (this.bases.size > CACHE_BASES) this.bases.delete(this.bases.keys().next().value);
    return B;
  }

  // 向き oi の「置けない位置」B に、置いた部材 pk（左上 px, py）と重なる位置を足す。
  // O の行 a の区間 [s, e) と P の行 b の区間 [q0, q1) が重なるのは、y = py + b − a、x ∈ [px + q0 − e + 1, px + q1 − s)。
  // 行 y（d = b − a を決めたもの）ごとに、その区間をすべて合わせた範囲を「置けない」にする。
  // a の順に見ていくと、同じ組（O の i 番目の区間と P の j 番目の区間）の区間は少しずつずれるだけなので、
  // 前の区間と重なっている間は 1 つの区間（最小〜最大）にまとめておき、離れたときだけ表に入れる。
  // 穴や凹みで行の区間の数が変わったら、まとめたものを表に入れ直す。表の中は重ならないよう左から並べておく。
  // この形（置けない位置の、P の左上からの相対位置）は向きの組だけで決まるので、覚えておいて使い回す
  nfr(B, oi, pk, px, py) {
    const { o, Hk, KW } = this.dims(oi);
    const S = this.nfrShape(oi, pk);
    const xMax = KW * 32, off = o.h - 1, rows = S.rows, iv = S.iv;
    const t0 = Math.max(0, off - py), t1 = Math.min(rows.length - 1, Hk + off - py);
    for (let t = t0; t < t1; t++) {
      const row = (py + t - off) * KW;
      for (let k = rows[t]; k < rows[t + 1]; k += 2) {
        let l = px + iv[k], h = px + iv[k + 1];
        if (l < 0) l = 0;
        if (h > xMax) h = xMax;
        if (l < h) setBits(B, row, l, h);
      }
    }
  }

  // 向き oi を置けない位置の、置いた部材 pk の左上からの相対位置。行 t（y = py + t − (oi の高さ − 1)）ごとの区間の並び
  nfrShape(oi, pk) {
    const key = oi * this.orients.length + pk;
    let S = this.shapes.get(key);
    if (S) {
      this.shapes.delete(key); this.shapes.set(key, S);
      return S;
    }
    const o = this.orients[oi], P = this.orients[pk];
    const nd = o.h + P.h - 1;
    const rows = new Int32Array(nd + 1);
    if (!this.iv) {
      this.iv = new Int32Array(2 * (this.W + 8));
      this.chOn = new Uint8Array(64); this.chLo = new Int32Array(64); this.chHi = new Int32Array(64);
      this.chPl = new Int32Array(64); this.chPh = new Int32Array(64);
      this.shapeBuf = new Int32Array(1 << 14);
    }
    let buf = this.shapeBuf, m = 0;
    const ors = o.rs, oro = o.ro, prs = P.rs, pro = P.ro, px = 0;
    const IV = this.iv;
    const on = this.chOn, cl = this.chLo, ch = this.chHi, pl = this.chPl, ph = this.chPh;
    for (let t = 0; t < nd; t++) {
      const d = t - (o.h - 1);
      const a0 = d < 0 ? -d : 0, a1 = Math.min(o.h, P.h - d);
      let n = 0;                                          // 表に入っている区間の数
      let rc0 = -1, qc0 = -1, slots = 0;
      // 区間が 1 つずつの行が続く間の、まとめ（いちばん多い場合なので別に持つ）
      let sOn = false, sLo = 0, sHi = 0, sPl = 0, sPh = 0;
      for (let a = a0; a < a1; a++) {
        const r0 = oro[a], r1 = oro[a + 1], q0 = pro[a + d], q1 = pro[a + d + 1];
        if (r0 === r1 || q0 === q1) continue;
        if (r1 - r0 === 2 && q1 - q0 === 2) {
          if (slots) {                                    // 区間の数が変わった: まとめていたものを表へ
            for (let k = 0; k < slots; k++) if (on[k]) { n = putIv(IV, n, cl[k], ch[k]); on[k] = 0; }
            slots = 0; rc0 = qc0 = -1;
          }
          const l = px + prs[q0] - ors[r1 - 1] + 1, h = px + prs[q1 - 1] - ors[r0];
          if (sOn && l <= sPh && h >= sPl) {              // 前の区間と重なる（接する）: まとめを広げる
            if (l < sLo) sLo = l;
            if (h > sHi) sHi = h;
          } else {
            if (sOn) n = putIv(IV, n, sLo, sHi);
            sOn = true; sLo = l; sHi = h;
          }
          sPl = l; sPh = h;
          continue;
        }
        if (sOn) { n = putIv(IV, n, sLo, sHi); sOn = false; }
        const rc = (r1 - r0) >> 1, qc = (q1 - q0) >> 1;
        if (rc !== rc0 || qc !== qc0) {
          for (let k = 0; k < slots; k++) if (on[k]) { n = putIv(IV, n, cl[k], ch[k]); on[k] = 0; }
          rc0 = rc; qc0 = qc; slots = rc * qc;
          if (slots > 64) slots = 0;
        }
        if (slots === 0) {                                // 区間がとても多い行（まれ）: 1 つずつ表へ
          for (let i = r0; i < r1; i += 2) for (let j = q0; j < q1; j += 2) n = putIv(IV, n, px + prs[j] - ors[i + 1] + 1, px + prs[j + 1] - ors[i]);
          continue;
        }
        for (let i = 0, k = 0; i < rc; i++) {
          const s = ors[r0 + 2 * i], e = ors[r0 + 2 * i + 1];
          for (let j = 0; j < qc; j++, k++) {
            const l = px + prs[q0 + 2 * j] - e + 1, h = px + prs[q0 + 2 * j + 1] - s;
            if (on[k] && l <= ph[k] && h >= pl[k]) {
              if (l < cl[k]) cl[k] = l;
              if (h > ch[k]) ch[k] = h;
            } else {
              if (on[k]) n = putIv(IV, n, cl[k], ch[k]);
              on[k] = 1; cl[k] = l; ch[k] = h;
            }
            pl[k] = l; ph[k] = h;
          }
        }
      }
      if (sOn) n = putIv(IV, n, sLo, sHi);
      for (let k = 0; k < slots; k++) if (on[k]) { n = putIv(IV, n, cl[k], ch[k]); on[k] = 0; }
      if (m + 2 * n > buf.length) {
        const nb = new Int32Array(Math.max(buf.length * 2, m + 2 * n));
        nb.set(buf.subarray(0, m));
        buf = this.shapeBuf = nb;
      }
      for (let k = 0; k < 2 * n; k++) buf[m++] = IV[k];
      rows[t + 1] = m;
    }
    S = { rows, iv: Int16Array.from(buf.subarray(0, m)) };   // 相対位置は格子の幅の 2 倍まで（16 ビットで足りる）
    this.shapes.set(key, S);
    if (this.shapes.size > CACHE_SHAPES) this.shapes.delete(this.shapes.keys().next().value);
    return S;
  }

  // 寄せる方向 g で、いちばん手前に置ける位置 {score, x, y}（なければ null）。
  // 点数は x・y の 1 次式なので、各行では左端か右端の空きだけ見ればよい
  lowest(B, oi, g) {
    const { Hk, Wk, KW } = this.dims(oi);
    const { cx, cy } = this.coef[g];
    const tail = Wk - 32 * (KW - 1);
    const lastMask = tail >= 32 ? -1 : (1 << tail) - 1;
    const minX = cx >= 0 ? 0 : cx * (Wk - 1);
    let best = Infinity, bx = -1, by = -1;
    const y0 = cy >= 0 ? 0 : Hk - 1, dy = cy >= 0 ? 1 : -1;
    for (let y = y0; y >= 0 && y < Hk; y += dy) {
      if (cy * y + minX >= best) break;                      // これより先の行は、どこでも今より奥
      const base = y * KW;
      let x = -1;
      if (cx >= 0) {
        for (let i = 0; i < KW; i++) {
          let v = ~B[base + i];
          if (i === KW - 1) v &= lastMask;
          if (v) { x = (i << 5) + (31 - Math.clz32(v & -v)); break; }
        }
      } else {
        for (let i = KW - 1; i >= 0; i--) {
          let v = ~B[base + i];
          if (i === KW - 1) v &= lastMask;
          if (v) { x = (i << 5) + (31 - Math.clz32(v)); break; }
        }
      }
      if (x < 0) continue;
      const sc = cx * x + cy * y;
      if (sc < best) { best = sc; bx = x; by = y; }
    }
    return bx < 0 ? null : { score: best, x: bx, y: by };
  }

  // 部材の先端（寄せる方向にいちばん出ている画素）の奥行き
  extent(oi, g) {
    const key = oi * N_GRAVITY + g;
    let e = this.ext.get(key);
    if (e === undefined) {
      const o = this.orients[oi], { gx, gy } = this.coef[g];
      e = -Infinity;
      const rf = o.runFlat;
      for (let t = 0; t < rf.length; t += 3) {
        const j = rf[t], s = rf[t + 1], en = rf[t + 2] - 1;
        e = Math.max(e, Math.max(gx * s, gx * en) + gy * j);
      }
      this.ext.set(key, e);
    }
    return e;
  }

  // -- 1 回の貪欲配置。fixed は先に置いたままにしておく配置 [[向き, x, y], …] --
  greedy(order, sets, g, fixed = null) {
    const placed = fixed ? fixed.slice() : [];
    const live = new Map();
    const failed = new Set();
    for (const pi of order) {
      if (failed.has(pi)) continue;
      let best = null;
      for (const oi of sets[pi]) {
        let lv = live.get(oi);
        if (!lv) {
          const B0 = this.base(oi);
          if (!B0) continue;
          lv = { B: B0.slice(), n: 0 };
          live.set(oi, lv);
        }
        for (let k = lv.n; k < placed.length; k++) this.nfr(lv.B, oi, placed[k][0], placed[k][1], placed[k][2]);
        lv.n = placed.length;
        const r = this.lowest(lv.B, oi, g);
        if (!r) continue;
        const score = r.score + this.extent(oi, g);
        if (!best || score < best.score) best = { score, oi, x: r.x, y: r.y };
      }
      if (!best) { failed.add(pi); continue; }
      placed.push([best.oi, best.x, best.y]);
    }
    let area = 0;
    for (const [oi] of placed) area += this.parts[this.orients[oi].part].area;
    return { raw: placed, area, count: placed.length, leftover: null };
  }

  // 残った空き領域のうち、直径 8 mm ほどの円が通れる、最大のひとかたまりの面積 [mm2]
  leftover(lay) {
    if (lay.leftover !== null) return lay.leftover;
    const GW = this.p.blocked.words;
    const occ = this.p.blocked.data.slice();
    for (const [oi, x, y] of lay.raw) {
      const P = this.orients[oi];
      for (let b = 0; b < P.h; b++) orInto(occ, (y + b) * GW, GW, P.data, b * P.words, P.words, x);
    }
    const free = freeRows(occ, this.H, this.W, GW);
    const h = this.disk.length;
    const Hk = this.H - h + 1, Wk = this.W - h + 1;
    const rows = [];                        // 行ごとの区間（[lo, hi, 番号] の並び）
    let nid = 0;
    fitRows(free, this.disk, h, Hk, Wk, this.cap, (y, iv, n) => {
      const r = [];
      for (let t = 0; t < 2 * n; t += 2) r.push(iv[t], iv[t + 1], nid++);
      rows[y] = r;
    });
    const parent = new Int32Array(nid).map((_, i) => i);
    const area = new Float64Array(nid);
    const find = (i) => { while (parent[i] !== i) { parent[i] = parent[parent[i]]; i = parent[i]; } return i; };
    for (let y = 0; y < Hk; y++) {
      const r = rows[y];
      if (!r) continue;
      for (let t = 0; t < r.length; t += 3) area[r[t + 2]] = r[t + 1] - r[t];
      const q = rows[y - 1];
      if (!q) continue;
      let i = 0, j = 0;                     // 上の行の区間と重なれば（4 近傍）同じかたまり
      while (i < r.length && j < q.length) {
        if (r[i] < q[j + 1] && q[j] < r[i + 1]) {
          const a = find(r[i + 2]), b = find(q[j + 2]);
          if (a !== b) parent[a] = b;
        }
        if (r[i + 1] < q[j + 1]) i += 3; else j += 3;
      }
    }
    const tot = new Float64Array(nid);
    let mx = 0;
    for (let i = 0; i < nid; i++) { const k = find(i); tot[k] += area[i]; if (tot[k] > mx) mx = tot[k]; }
    lay.leftover = mx * this.res * this.res;
    return lay.leftover;
  }

  // a は b より良いか（①面積 ②個数 ③残りのかたまり。③は必要なときだけ測る）
  better(a, b) {
    if (!b) { this.leftover(a); return true; }
    const aa = Math.round(a.area * 100), ba = Math.round(b.area * 100);
    if (aa !== ba) { if (aa > ba) { this.leftover(a); return true; } return false; }
    if (a.count !== b.count) { if (a.count > b.count) { this.leftover(a); return true; } return false; }
    return Math.round(this.leftover(a)) > Math.round(this.leftover(b));
  }

  // -- 順番と向きの決め方 --
  plan(rng, run, elite) {
    const inst = [];
    this.parts.forEach((p, i) => { for (let q = 0; q < p.qty; q++) inst.push(i); });
    const sigma = run === 0 ? 0 : [0, 0.25, 0.6, 1.2][rng.int(4)];
    const keys = inst.map((i) => Math.log(this.parts[i].area + 1e-9) + (sigma ? rng.normal() * sigma : 0));
    const order = inst.map((_, k) => k).sort((a, b) => keys[b] - keys[a]).map((k) => inst[k]);
    const sets = this.parts.map((p, i) => {
      const lst = p.orients;
      if (lst.length <= this.maxOrients) return lst;
      // 多すぎるときは、90° 単位の向き ＋「試す向きの組」＋ これまでの最良の配置で使った向き。
      // 組は回ごとに 4 分の 1 ずつ入れ替える（回を重ねるうちに全部の向きを試す。少しずつ変えるので、
      // 前の回に計算した「置けない位置の形」をそのまま使えることが多い）
      if (!this.oriFixed[i]) {
        this.oriFixed[i] = lst.filter((oi) => Math.abs(this.orients[oi].angle / 90 - Math.round(this.orients[oi].angle / 90)) < 1e-9);
        this.oriRest[i] = lst.filter((oi) => !this.oriFixed[i].includes(oi));
      }
      const fixed = this.oriFixed[i], rest = this.oriRest[i];
      const k = Math.min(rest.length, Math.max(0, this.maxOrients - fixed.length));
      let pool = this.oriPool[i];
      if (!pool) {
        const step = Math.max(1, Math.floor(rest.length / Math.max(1, k)));
        pool = [];
        for (let j = 0; j < rest.length && pool.length < k; j += step) pool.push(rest[j]);
      } else if (run > 0 && rest.length > k) {
        pool = pool.slice();
        const inPool = new Set(pool);
        const swaps = Math.max(1, Math.round(k / 4));
        for (let s = 0; s < swaps; s++) {
          let cand = rest[rng.int(rest.length)];
          for (let tries = 0; inPool.has(cand) && tries < 8; tries++) cand = rest[rng.int(rest.length)];
          if (inPool.has(cand)) continue;
          const at = rng.int(pool.length);
          inPool.delete(pool[at]);
          pool[at] = cand;
          inPool.add(cand);
        }
      }
      this.oriPool[i] = pool;
      const chosen = fixed.concat(pool);
      if (elite) for (const [oi] of elite.raw) if (this.orients[oi].part === i && !chosen.includes(oi)) chosen.push(oi);
      return chosen;
    });
    return { order, sets };
  }

  // -- 探索の 1 手。最初のうちと 4 回に 1 回は一から、それ以外は最良の配置を一部こわして置き直す（ruin & recreate） --
  step(rng, run, best, gOffset = 0) {
    const { order, sets } = this.plan(rng, run, best);
    if (!best || !best.raw.length || run < 2 || rng.random() < 0.25) return this.greedy(order, sets, (run + gOffset) % N_GRAVITY);
    const raw = best.raw, n = raw.length;
    const k = 1 + rng.int(Math.max(2, Math.floor(n / 2) + 1) - 1);
    let drop;
    if (rng.random() < 0.6) {          // 近くにある部材をまとめて外す（空いた場所がひとかたまりになる）
      const ctr = raw.map(([oi, x, y]) => [x + this.orients[oi].w / 2, y + this.orients[oi].h / 2]);
      const s = ctr[rng.int(n)];
      const idx = ctr.map((c, i) => [Math.hypot(c[0] - s[0], c[1] - s[1]), i]).sort((a, b) => a[0] - b[0]);
      drop = new Set(idx.slice(0, k).map((t) => t[1]));
    } else {
      drop = new Set(rng.sample(n, k));
    }
    const kept = raw.filter((_, i) => !drop.has(i));
    const have = new Map();
    for (const [oi] of kept) { const pi = this.orients[oi].part; have.set(pi, (have.get(pi) || 0) + 1); }
    const rest = [];
    for (const pi of order) {          // 残してある個数ぶんを、置く予定から差し引く
      const c = have.get(pi) || 0;
      if (c > 0) have.set(pi, c - 1); else rest.push(pi);
    }
    return this.greedy(rest, sets, rng.int(N_GRAVITY), kept);
  }

  // 画面と Python の検査に渡す形（座標は mm。tx = x0 + x·res − mx0 は Python 版と同じ）
  toDict(lay) {
    const placements = lay.raw.map(([oi, x, y]) => {
      const o = this.orients[oi];
      return { part: this.parts[o.part].index, flip: o.flip, angle: o.angle,
               tx: this.x0 + x * this.res - o.mx0, ty: this.y0 + y * this.res - o.my0 };
    });
    const leftover = this.leftover(lay);
    return { placements, placed_area: lay.area, leftover, key: [Math.round(lay.area * 100) / 100, lay.count, Math.round(leftover)] };
  }
}

// 1 本の作業スレッドで回す探索。番号ごとに乱数と寄せる方向を変えて、スレッドどうしで違う配置を探す
export class Search {
  constructor(problem, wid = 0, opts = {}) {
    this.nester = new Nester(problem, opts);
    this.rng = makeRng(((problem.params.seed || 0) * 1000 + wid + 1) | 0);
    this.wid = wid;
    this.best = null;
    this.runs = 0;
  }

  // 少なくとも 1 回、seconds 秒たつまで探す。戻り値: {runs: これまでの回数, best: 良くなったときだけ結果}
  runFor(seconds) {
    const tEnd = performance.now() + seconds * 1000;
    let improved = false;
    do {
      const run = this.wid === 0 ? this.runs : this.runs + 1;
      const lay = this.nester.step(this.rng, run, this.best, this.wid * 3);
      this.runs++;
      if (this.nester.better(lay, this.best)) { this.best = lay; improved = true; }
    } while (performance.now() < tEnd);
    return { runs: this.runs, best: improved ? this.nester.toDict(this.best) : null };
  }
}

// ---- 検査用 ----------------------------------------------------------------------------------------

// 向き oi の「置けない位置」を 0/1 の画像（Hk × Wk、行優先）にする。placed = [[向き, x, y], …] を置いたあと
export function blockedImage(problem, oi, placed = [], withBase = true) {
  const n = new Nester(problem);
  const { Hk, Wk, KW } = n.dims(oi);
  if (Hk <= 0 || Wk <= 0) return { Hk, Wk, bits: null };
  let B;
  if (withBase) {
    const B0 = n.base(oi);
    B = B0 ? B0.slice() : new Int32Array(Hk * KW).fill(-1);
  } else {
    B = new Int32Array(Hk * KW);
  }
  for (const [pk, px, py] of placed) n.nfr(B, oi, pk, px, py);
  const bits = new Uint8Array(Hk * Wk);
  for (let y = 0; y < Hk; y++) for (let x = 0; x < Wk; x++) bits[y * Wk + x] = (B[y * KW + (x >> 5)] >>> (x & 31)) & 1;
  return { Hk, Wk, bits };
}
