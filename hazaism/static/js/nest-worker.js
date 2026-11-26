// 作業スレッド（敷き詰め）: nest/engine.js の探索を回す。何本か立てて並列に探す（Python は読まないので、起動は一瞬）。
//   少し（chunk 秒）回しては結果を画面へ送り、その合間に「止めて」を受け取る
import { Search, decodeProblem } from "./nest/engine.js";

let stopFlag = false;

const pause = () => new Promise((r) => setTimeout(r, 0));

async function run({ payload, wid, deadline, chunk }) {
  stopFlag = false;
  let runs = 0;
  try {
    const search = new Search(decodeProblem(payload), wid);
    while (!stopFlag && Date.now() < deadline) {
      const out = search.runFor(Math.max(0.01, Math.min(chunk, (deadline - Date.now()) / 1000)));
      runs = out.runs;
      postMessage({ type: "tick", wid, runs, best: out.best });
      await pause();                        // ここで「止めて」を受け取る
    }
    postMessage({ type: "done", wid, runs });
  } catch (err) {
    postMessage({ type: "error", wid, runs, error: String(err && err.message ? err.message : err) });
  }
}

onmessage = (e) => {
  const m = e.data;
  if (m.cmd === "init") postMessage({ type: "ready" });
  else if (m.cmd === "start") run(m);
  else if (m.cmd === "stop") stopFlag = true;
};
