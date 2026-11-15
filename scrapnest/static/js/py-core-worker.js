// 作業スレッド（core）: ブラウザ内の Python で scrapnest.webapi を動かす。画面からの頼みごとを 1 つずつ順に処理する。
//   作業データは /data に置き、ブラウザ内の保存領域（IndexedDB）とつないで、ページを開き直しても残るようにする
import { CORE_PACKAGES, PYODIDE_URL } from "./pyconfig.js";

let py = null;
let call = null;
let queue = Promise.resolve();      // 頼みごとを順番に処理するための列
const SITE = new URL("../", self.location.href);

const progress = (text, pct) => postMessage({ type: "progress", text, pct });

function syncfs(populate) {
  return new Promise((resolve, reject) => py.FS.syncfs(populate, (err) => (err ? reject(err) : resolve())));
}

async function init({ build, wheel }) {
  progress("Python を読み込んでいます…", 3);
  const { loadPyodide } = await import(PYODIDE_URL + "pyodide.mjs");
  py = await loadPyodide({ indexURL: PYODIDE_URL });
  let pct = 20;
  const names = { 0: "基本のライブラリ", 1: "計算のライブラリ", 2: "画像処理のライブラリ（大きいので少し待ちます）" };
  for (const [i, group] of CORE_PACKAGES.entries()) {
    progress(`${names[i] || "ライブラリ"}を読み込んでいます…`, pct);
    await py.loadPackage(group);
    pct += 22;
  }
  progress("ScrapNest を準備しています…", 88);
  const micropip = py.pyimport("micropip");
  await micropip.install.callKwargs(new URL(`py/${wheel}`, SITE).href, { deps: false });
  micropip.destroy();
  const zip = await (await fetch(new URL(`py/scrapnest.zip?v=${build}`, SITE))).arrayBuffer();
  py.unpackArchive(zip, "zip", { extractDir: "/app" });

  py.FS.mkdirTree("/data");
  py.FS.mount(py.FS.filesystems.IDBFS, {}, "/data");
  await syncfs(true);                       // 前回の作業を IndexedDB から戻す
  py.runPython(`
import os, sys
os.environ["SCRAPNEST_HOME"] = "/data"
sys.path.insert(0, "/app")
import scrapnest.webapi as webapi
webapi.init()

def _call(method, path, body_json, files_js):
    files = None
    if files_js is not None:
        files = [(str(f.name), bytes(f.data.to_py())) for f in files_js]
    r = webapi.handle(method, path, body_json, files)
    return r.status, r.json, r.data, r.content_type
`);
  call = py.globals.get("_call");
  progress("準備ができました", 100);
}

async function handle(m) {
  // JS の null は Python では None にならない（JsNull になる）ので、無いものは undefined で渡す
  const res = call(m.method, m.path, m.body ?? undefined, m.files ?? undefined);
  let status, json, data, ctype;
  try {
    [status, json, data, ctype] = res.toJs();
  } finally {
    res.destroy();
  }
  // Python の bytes は Uint8Array として受け取る。画面へは中身ごと渡す（コピーせずに移す）
  const bytes = data ? new Uint8Array(data) : null;
  if (m.method !== "GET" && status < 500) {
    try {
      await syncfs(false);                  // 変わったファイルを IndexedDB へ書き出す
    } catch (e) {
      console.warn("作業の保存に失敗しました", e);
    }
  }
  postMessage({ id: m.id, status, json, data: bytes, ctype }, bytes ? [bytes.buffer] : []);
}

onmessage = (e) => {
  const m = e.data;
  if (m.cmd === "init") {
    queue = queue.then(() => init(m)).then(
      () => postMessage({ type: "ready" }),
      (err) => postMessage({ type: "fatal", error: String(err && err.message ? err.message : err) }),
    );
  } else if (m.cmd === "call") {
    queue = queue.then(() => handle(m)).catch((err) => {
      postMessage({ id: m.id, status: 500, json: JSON.stringify({ detail: `内部エラー: ${err}`, code: "internal" }) });
    });
  }
};
