// 画面と、ブラウザ内の Python（作業スレッド）とのあいだ。
//   - core: 写真の解析・輪郭・部材・DXF。api() の頼みごとはここへ送る
//   - nest: 敷き詰めを並列に探す作業スレッド（何本か）。指揮はこのファイルでする

let core = null;
let coreReady = null;
let seq = 0;
const pending = new Map();
export const site = { version: "", build: "dev", ezdxf_wheel: "", pyodide: "" };

// ---- core --------------------------------------------------------------------------

async function loadSiteInfo() {
  try {
    const r = await fetch("version.json", { cache: "no-store" });
    Object.assign(site, await r.json());
  } catch (e) {
    throw new Error("アプリのファイルを読み込めません。ネットワークを確かめて、ページを再読み込みしてください");
  }
}

export function startCore(onProgress = () => {}) {
  if (coreReady) return coreReady;
  coreReady = (async () => {
    await loadSiteInfo();
    core = new Worker(new URL("./py-core-worker.js", import.meta.url), { type: "module" });
    await new Promise((resolve, reject) => {
      core.onmessage = (e) => {
        const m = e.data;
        if (m.type === "progress") onProgress(m);
        else if (m.type === "ready") resolve();
        else if (m.type === "fatal") reject(new Error(`読み込みに失敗しました: ${m.error}`));
        else {
          const done = pending.get(m.id);
          if (done) {
            pending.delete(m.id);
            done(m);
          }
        }
      };
      core.onerror = (e) => {
        const err = new Error("作業スレッドが止まりました。ページを再読み込みしてください" + (e.message ? `（${e.message}）` : ""));
        for (const done of pending.values()) done({ status: 500, json: JSON.stringify({ detail: err.message }) });
        pending.clear();
        reject(err);
      };
      core.postMessage({ cmd: "init", build: site.build, wheel: site.ezdxf_wheel });
    });
  })();
  return coreReady;
}

// 頼みごとを 1 つ送る。戻り値: { status, json(文字列), data(Uint8Array), ctype }
export async function call(method, path, body, files) {
  await coreReady;
  const id = ++seq;
  return new Promise((resolve) => {
    pending.set(id, resolve);
    core.postMessage({ cmd: "call", id, method, path, body: body === undefined || body === null ? null : JSON.stringify(body), files: files || null });
  });
}

// 画像（写真の縮小版・補正画像）を Blob の URL にする。同じ key なら作り直さない
const blobs = new Map();
export async function imageUrl(path, key) {
  const hit = blobs.get(path);
  if (hit && hit.key === key) return hit.url;
  const r = await call("GET", path);
  if (r.status >= 400 || !r.data) return "";
  if (hit) URL.revokeObjectURL(hit.url);
  const url = URL.createObjectURL(new Blob([r.data], { type: r.ctype || "image/jpeg" }));
  blobs.set(path, { key, url });
  return url;
}

// ---- 敷き詰めの作業スレッド ------------------------------------------------------------

const N = { workers: [], warming: null, run: null };

// 作業スレッドの本数。1 本あたり 350 MB ほど使うので、メモリが少ない端末では減らす
export function nestWorkerCount() {
  const hc = navigator.hardwareConcurrency || 4;
  let k = Math.max(1, Math.min(4, hc - 1));
  const dm = navigator.deviceMemory;          // Chrome/Edge だけが返す（GB、最大 8）
  if (dm && dm <= 4) k = Math.min(k, 2);
  if (dm && dm <= 2) k = 1;
  return k;
}

function spawnNestWorker(i) {
  const w = new Worker(new URL("./py-nest-worker.js", import.meta.url), { type: "module" });
  const rec = { w, i, alive: true, onTick: null, onDone: null };
  rec.ready = new Promise((resolve) => {
    w.onmessage = (e) => {
      const m = e.data;
      if (m.type === "ready") resolve(true);
      else if (m.type === "fatal") { rec.alive = false; resolve(false); }
      else if (m.type === "tick" && rec.onTick) rec.onTick(m);
      else if ((m.type === "done" || m.type === "error") && rec.onDone) rec.onDone(m);
    };
    w.onerror = () => {
      rec.alive = false;
      resolve(false);
      if (rec.onDone) rec.onDone({ type: "error", wid: i, runs: 0, error: "作業スレッドが止まりました（メモリ不足の可能性があります）" });
    };
  });
  w.postMessage({ cmd: "init", build: site.build });
  return rec;
}

// 敷き詰めの画面に入る前に、作業スレッドを立てて Python を読み込んでおく
export function warmNest() {
  if (N.warming) return N.warming;
  const k = nestWorkerCount();
  N.workers = [];
  for (let i = 0; i < k; i++) N.workers.push(spawnNestWorker(i));
  N.warming = Promise.all(N.workers.map((r) => r.ready)).then(() => N.workers.filter((r) => r.alive));
  return N.warming;
}

// 作業スレッドを止めてメモリを空ける（計算中は止めない）
export function releaseNest() {
  if (N.run && N.run.active) return;
  for (const r of N.workers) r.w.terminate();
  N.workers = [];
  N.warming = null;
}

const keyGreater = (a, b) => {
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    if ((a[i] ?? 0) !== (b[i] ?? 0)) return (a[i] ?? 0) > (b[i] ?? 0);
  }
  return false;
};

function previewResult(run) {
  const b = run.best;
  if (!b || !run.meta) return null;
  const counts = {};
  for (const k of Object.keys(run.meta.qty)) counts[k] = [0, run.meta.qty[k]];
  let area = 0;
  for (const p of b.placements) {
    const k = String(p.part);
    if (counts[k]) counts[k][0]++;
    area += run.meta.part_area[k] || 0;
  }
  const requested = Object.values(run.meta.qty).reduce((a, q) => a + q, 0);
  return {
    placements: b.placements, counts, placed: b.placements.length, requested,
    area: Math.round(area * 10) / 10, scrap_area: run.meta.scrap_area,
    utilization: run.meta.scrap_area ? Math.round(1000 * area / run.meta.scrap_area) / 10 : 0,
    dropped: 0, spacing: run.meta.spacing, margin: run.meta.margin,
    metrics: { leftover: Math.round((b.leftover || 0) * 10) / 10 },
  };
}

function totalRuns(run) {
  let n = 0;
  for (const v of run.runs.values()) n += v;
  return n;
}

function elapsed(run) {
  if (!run.computeStart) return 0;
  return ((run.computeEnd || Date.now()) - run.computeStart) / 1000;
}

async function nestStart(body) {
  if (N.run && N.run.active) await nestStop();
  const params = (body && body.params) || {};
  const run = {
    active: true, phase: "prepare", stopped: false, error: null, meta: null, best: null, result: null,
    runs: new Map(), history: [], timeLimit: Number(params.time_limit) || 30, workers: 0,
    computeStart: 0, computeEnd: 0,
  };
  N.run = run;
  (async () => {
    try {
      const warm = warmNest();                      // 準備の計算と同時に、作業スレッドの読み込みを進める
      const r = await call("POST", "/api/nest/prepare", body);
      const meta = r.json ? JSON.parse(r.json) : null;
      if (r.status >= 400) throw new Error((meta && meta.detail) || `エラー (${r.status})`);
      run.meta = meta;
      const payload = r.data;
      run.phase = "warmup";
      const workers = await warm;
      if (!workers.length) throw new Error("敷き詰めの作業スレッドを起動できません（メモリ不足の可能性があります）");
      if (run.stopped) { run.phase = "done"; run.active = false; return; }
      run.workers = workers.length;
      run.phase = "running";
      run.computeStart = Date.now();
      const deadline = run.computeStart + run.timeLimit * 1000;
      await Promise.all(workers.map((rec, wid) => new Promise((resolve) => {
        rec.onTick = (m) => {
          run.runs.set(wid, m.runs);
          if (m.best && (!run.best || keyGreater(m.best.key, run.best.key))) {
            run.best = m.best;
            run.history.push({
              t: Math.round((Date.now() - run.computeStart) / 100) / 10,
              utilization: run.meta.scrap_area ? Math.round(1000 * previewResult(run).area / run.meta.scrap_area) / 10 : 0,
              placed: m.best.placements.length, leftover: Math.round(m.best.leftover || 0),
            });
          }
        };
        rec.onDone = (m) => {
          run.runs.set(wid, m.runs || run.runs.get(wid) || 0);
          if (m.type === "error") console.warn("敷き詰めの作業スレッド", wid, m.error);
          rec.onTick = rec.onDone = null;
          resolve();
        };
        const copy = payload.slice();
        rec.w.postMessage({ cmd: "start", payload: copy, wid, deadline, chunk: 0.4 }, [copy.buffer]);
      })));
      run.computeEnd = Date.now();
      run.phase = "verify";
      const stats = { runs: totalRuns(run), elapsed: elapsed(run), workers: run.workers, history: run.history };
      const f = await call("POST", "/api/nest/finish", { best: run.best, stats });
      const res = f.json ? JSON.parse(f.json) : null;
      if (f.status >= 400) throw new Error((res && res.detail) || `エラー (${f.status})`);
      run.result = res;
      run.phase = "done";
    } catch (e) {
      run.error = e.message || String(e);
      run.phase = "error";
    } finally {
      run.computeEnd = run.computeEnd || Date.now();
      run.active = false;
    }
  })();
  return { ok: true };
}

async function nestStop() {
  const run = N.run;
  if (!run || !run.active) return { ok: true };
  run.stopped = true;
  for (const rec of N.workers) rec.w.postMessage({ cmd: "stop" });
  return { ok: true };
}

function nestStatus() {
  const run = N.run;
  if (!run) return { state: "idle" };
  const el = elapsed(run);
  const runs = totalRuns(run);
  return {
    state: run.active ? "running" : "done",
    phase: run.phase,
    runs,
    elapsed: Math.round(el * 10) / 10,
    time_limit: run.timeLimit,
    workers: run.workers || N.workers.length,
    runs_per_sec: el > 0 ? Math.round(100 * runs / el) / 100 : 0,
    history: run.history,
    error: run.error,
    result: run.active ? previewResult(run) : run.result,
  };
}

// 敷き詰め中に、結果が変わる操作（輪郭・部材・写真）が来たら計算を止める
export function stopNestIfRunning() {
  if (N.run && N.run.active) nestStop();
}

export async function nestCall(method, path, body) {
  if (path === "/api/nest/start") return nestStart(body);
  if (path === "/api/nest/stop") return nestStop();
  if (path === "/api/nest/status") return nestStatus();
  throw new Error(`そのような操作はありません: ${path}`);
}
