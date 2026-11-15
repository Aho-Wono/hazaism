// 作業スレッド（敷き詰め）: ブラウザ内の Python で engine.NestWorker を回す。何本か立てて並列に探す。
//   少し（chunk 秒）回しては結果を画面へ送り、その合間に「止めて」を受け取る
import { NEST_PACKAGES, PYODIDE_URL } from "./pyconfig.js";

let py = null;
let makeWorker = null;
let stopFlag = false;
const SITE = new URL("../", self.location.href);

async function init({ build }) {
  const { loadPyodide } = await import(PYODIDE_URL + "pyodide.mjs");
  py = await loadPyodide({ indexURL: PYODIDE_URL });
  await py.loadPackage(NEST_PACKAGES);
  const zip = await (await fetch(new URL(`py/hazaism.zip?v=${build}`, SITE))).arrayBuffer();
  py.unpackArchive(zip, "zip", { extractDir: "/app" });
  py.runPython(`
import sys, json
sys.path.insert(0, "/app")
from hazaism.nest import engine

class _Runner:
    def __init__(self, buf, wid):
        self.w = engine.NestWorker(bytes(buf.to_py()), int(wid))
    def run_for(self, seconds):
        return json.dumps(self.w.run_for(float(seconds)))
`);
  makeWorker = py.globals.get("_Runner");
}

const pause = () => new Promise((r) => setTimeout(r, 0));

async function run({ payload, wid, deadline, chunk }) {
  stopFlag = false;
  let runner = null;
  let runs = 0;
  try {
    runner = makeWorker(payload, wid);
    while (!stopFlag && Date.now() < deadline) {
      const out = JSON.parse(runner.run_for(chunk));
      runs = out.runs;
      postMessage({ type: "tick", wid, runs, best: out.best });
      await pause();                        // ここで「止めて」を受け取る
    }
    postMessage({ type: "done", wid, runs });
  } catch (err) {
    postMessage({ type: "error", wid, runs, error: String(err && err.message ? err.message : err) });
  } finally {
    if (runner) runner.destroy();
  }
}

onmessage = (e) => {
  const m = e.data;
  if (m.cmd === "init") {
    init(m).then(
      () => postMessage({ type: "ready", mem: py._module.HEAPU8.length }),
      (err) => postMessage({ type: "fatal", error: String(err && err.message ? err.message : err) }),
    );
  } else if (m.cmd === "start") {
    run(m);
  } else if (m.cmd === "stop") {
    stopFlag = true;
  }
};
