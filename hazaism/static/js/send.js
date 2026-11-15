// スマホ側: 撮った写真を PC の Hazaism へ直接送るページ（send.html）
//
// PC の番号は QR の URL（#pc=…）で受け取り、このスマホにも覚えておく。ホーム画面に追加したときも番号が付いたままになるよう、
// URL の #pc=… は消さない（iPhone のホーム画面のアプリは、Safari とは別の保存領域を使うため）。

import { MAX_BYTES, Sender, loadPeerJs, sniffImage, storageGet, storageSet, validPcId } from "./phone-link.js";

const KEY = "hazaism.send";
const $ = (s) => document.querySelector(s);

const L = {
  pcId: null,
  peer: null,
  conn: null,
  sender: null,
  gen: 0,
  state: "idle",
  retryTimer: null,
  connectTimer: null,
  job: null,          // いま送っている（送ろうとしている）写真 { bytes, name, type, url, id, done }
  queued: false,
  sending: false,
};

// ---- PC の番号 ------------------------------------------------------------------------------

function readPcId() {
  const fromUrl = new URLSearchParams(location.hash.slice(1)).get("pc");
  if (validPcId(fromUrl)) {
    storageSet(KEY, { pc: fromUrl });
    return fromUrl;
  }
  const st = storageGet(KEY);
  if (st && validPcId(st.pc)) {
    history.replaceState(null, "", `#pc=${st.pc}`);
    return st.pc;
  }
  return null;
}

// ---- つながり ------------------------------------------------------------------------------

// 上の帯には短い言葉だけ出し、詳しい説明はその下に出す
const SHORT = { idle: "未接続", connecting: "つないでいます…", open: "PC とつながっています", missing: "PC が見つかりません", error: "つながりません" };

function setConn(state, text) {
  L.state = state;
  $("#conn").className = `conn ${state}`;
  $("#conn-text").textContent = SHORT[state] || "";
  const d = $("#conn-detail");
  d.textContent = ["open", "connecting", "idle"].includes(state) ? "" : text;
  d.className = `conn-detail ${state}`;
  d.hidden = !d.textContent;
}

function retryLater(ms = 5000) {
  clearTimeout(L.retryTimer);
  L.retryTimer = null;
  if (document.hidden) return;              // 画面に戻ったときにつなぎ直す
  L.retryTimer = setTimeout(connect, ms);
}

function peerErrorText(type) {
  if (type === "browser-incompatible") return "このブラウザは対応していません（Safari か Chrome で開いてください）";
  return "仲介のサーバーにつながりません。やり直しています…";
}

function onPeerError(peer, err) {
  if (L.peer !== peer) return;
  const type = err && err.type;
  if (type === "peer-unavailable") {
    // PC が待ち受けていない（Hazaism が閉じている・眠っている・「受け取りをやめる」になっている）
    if (L.conn && !L.conn.open) {
      L.conn = null;
      clearTimeout(L.connectTimer);
    }
    setConn("missing", "PC の Hazaism が開いていません。PC で開くと自動でつながります");
    retryLater(5000);
    return;
  }
  if (type === "webrtc") return;           // つながりごとの失敗は、conn の close で扱う
  // 仲介のサーバーとの問題。PC とつながっていれば、そのまま使い続ける（壊すと送信中の写真も切れる）。
  // 次につなぐときに ensurePeer() が作り直す
  if (L.conn && L.conn.open) return;
  L.peer = null;
  if (!peer.destroyed) peer.destroy();
  setConn("error", peerErrorText(type));
  retryLater(type === "browser-incompatible" ? 60_000 : 5000);
}

// 仲介のサーバーに登録する（スマホ側の番号は毎回おまかせ）
function ensurePeer(Peer) {
  if (L.peer && !L.peer.destroyed && L.peer.open) return Promise.resolve();
  if (L.peer && !L.peer.destroyed) L.peer.destroy();
  const peer = new Peer({ debug: 0 });
  L.peer = peer;
  peer.on("error", (err) => onPeerError(peer, err));
  // 仲介のサーバーと切れても、つながっている PC とのやり取りは続く（壊すと送信中の写真も切れる）。
  // 次につなぐときに ensurePeer() が作り直す
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error("仲介のサーバーにつながりません。やり直しています…")), 15_000);
    peer.once("open", () => { clearTimeout(t); resolve(); });
    peer.once("error", (e) => { clearTimeout(t); reject(new Error(peerErrorText(e && e.type))); });
  });
}

async function connect() {
  clearTimeout(L.retryTimer);
  L.retryTimer = null;
  if (!L.pcId || L.conn) return;           // つながっている・つなぎかけている
  if (!navigator.onLine) {
    setConn("error", "ネットにつながっていません");
    return;
  }
  const gen = ++L.gen;
  if (L.state !== "missing") setConn("connecting", "PC につないでいます…");
  let Peer;
  try {
    Peer = await loadPeerJs();
    if (gen !== L.gen) return;
    await ensurePeer(Peer);
  } catch (e) {
    if (gen !== L.gen) return;
    if (L.state !== "missing") setConn("error", e.message);
    retryLater(5000);
    return;
  }
  if (gen !== L.gen || L.conn) return;
  const conn = L.peer.connect(L.pcId, { serialization: "raw", reliable: true });
  L.conn = conn;
  let opened = false;
  clearTimeout(L.connectTimer);
  L.connectTimer = setTimeout(() => {
    if (L.conn !== conn || opened) return;
    L.conn = null;
    try { conn.close(); } catch (e) { /* もう閉じている */ }
    setConn("missing", "PC が見つかりません。PC の Hazaism が開いているか、同じ Wi-Fi か確かめてください");
    retryLater(5000);
  }, 20_000);
  conn.on("open", () => {
    if (L.conn !== conn) return;
    opened = true;
    clearTimeout(L.connectTimer);
    setConn("open", "PC とつながっています");
    L.sender = new Sender((d) => conn.send(d));
    flush();
  });
  conn.on("data", (d) => {
    if (L.conn === conn && typeof d === "string") onPcMessage(d);
  });
  const gone = () => {
    if (L.conn !== conn) return;
    L.conn = null;
    clearTimeout(L.connectTimer);
    if (L.sender) L.sender.abort(new Error("PC とのつながりが切れました"));
    L.sender = null;
    if (opened) setConn("missing", "PC とのつながりが切れました。つなぎ直しています…");
    else if (L.state !== "missing") setConn("missing", "PC が見つかりません。PC で Hazaism を開いてください");
    retryLater(opened ? 1500 : 5000);
  };
  conn.on("close", gone);
  conn.on("error", gone);
}

// ---- PC からの合図 ----------------------------------------------------------------------------

function onPcMessage(text) {
  let m;
  try { m = JSON.parse(text); } catch (e) { return; }
  if (m.t === "ack") {
    if (L.sender) L.sender.onMessage(m);
    return;
  }
  if (m.t !== "status" || !L.job || m.id !== L.job.id) return;
  const job = L.job;
  if (m.stage === "error") {
    if (L.sending && L.sender) L.sender.abort(new Error(m.error || "PC で受け取れませんでした"));
    else showFailure(m.error || "PC で受け取れませんでした");
  } else if (m.stage === "received") {
    setJob("PC が受け取りました", 1);
  } else if (m.stage === "confirm") {
    setJob("PC で確認を待っています", 1, "PC が作業の途中だったため、PC の画面で「読み込む」を押すと解析が始まります。");
  } else if (m.stage === "analyzing") {
    setJob("PC で解析しています…", 1, "20 秒ほどかかります。");
  } else if (m.stage === "declined") {
    job.done = true;
    setJob(m.reason === "replaced" ? "次の写真に置き換えました" : "PC で読み込みを取りやめました", null);
  } else if (m.stage === "done") {
    job.done = true;
    showResult(m);
  }
}

// ---- 表示 -----------------------------------------------------------------------------------

function setJob(title, progress = null, detail = "") {
  $("#job").hidden = false;
  $("#job-title").textContent = title;
  $("#job-progress").hidden = progress === null;
  if (progress !== null) $("#job-bar").style.width = `${Math.round(progress * 100)}%`;
  $("#job-detail").textContent = detail;
  $("#btn-retry").hidden = true;
}

function showFailure(message) {
  setJob("送れませんでした", null, message);
  $("#btn-retry").hidden = !L.job;
}

function msg(kind, text) {
  const d = document.createElement("div");
  d.className = `msg ${kind}`;
  d.textContent = text;
  return d;
}

const fmt = (v, d) => Number(v).toLocaleString("ja-JP", { minimumFractionDigits: d, maximumFractionDigits: d });

function showResult(m) {
  const box = $("#result");
  box.replaceChildren();
  if (!m.ok) {
    setJob("撮り直してください", null);
    box.appendChild(msg("bad", m.unreadable
      ? `PC で写真を読めませんでした（${m.error}）。もう一度撮って送ってください。`
      : `格子を見つけられませんでした${m.error ? `（${m.error}）` : ""}。格子マットが広く写るように、真上から撮り直してください。`));
    return;
  }
  const cells = m.cells ? `${m.cells[0]} × ${m.cells[1]} マス` : "";
  const rms = m.rms_mm !== null && m.rms_mm !== undefined ? `、誤差 ${fmt(m.rms_mm, 2)} mm` : "";
  if (!m.candidates) {
    setJob("板が見つかりません", null);
    box.appendChild(msg("warn", `格子は見つかりました（${cells}${rms}）が、板が見つかりませんでした。` +
      "板全体が写るように撮り直すか、PC で輪郭を描いてください。"));
  } else {
    setJob("PC に読み込みました", null);
    box.appendChild(msg("ok", `格子 ${cells}${rms}、板 ${m.candidates} 枚。PC で輪郭を確かめてください。`));
  }
  for (const w of m.warnings || []) box.appendChild(msg("warn", w));
}

// ---- 写真を送る ------------------------------------------------------------------------------

// JPEG・PNG・WebP 以外（HEIC など）は、このスマホで JPEG にしてから送る（PC のブラウザでは開けないことがあるため）
async function toJpeg(file) {
  const bmp = await createImageBitmap(file);
  const scale = Math.min(1, Math.sqrt(16e6 / (bmp.width * bmp.height)));   // iPhone の canvas の上限（1600 万画素）
  const w = Math.round(bmp.width * scale), h = Math.round(bmp.height * scale);
  const c = document.createElement("canvas");
  c.width = w;
  c.height = h;
  c.getContext("2d").drawImage(bmp, 0, 0, w, h);
  if (bmp.close) bmp.close();
  const blob = await new Promise((resolve, reject) => c.toBlob((b) => (b ? resolve(b) : reject(new Error("JPEG にできません"))), "image/jpeg", 0.92));
  return blob.arrayBuffer();
}

async function takeFile(file) {
  if (!file) return;
  $("#result").replaceChildren();
  if (L.job && L.job.url) URL.revokeObjectURL(L.job.url);
  const url = URL.createObjectURL(file);
  $("#thumb").src = url;
  L.job = null;
  setJob("写真を準備しています…", null);
  try {
    if (file.size > MAX_BYTES) throw new Error(`写真が大きすぎます（${Math.round(MAX_BYTES / 1e6)} MB まで）`);
    let bytes = await file.arrayBuffer();
    let type = sniffImage(new Uint8Array(bytes));
    let name = file.name || "photo.jpg";
    if (!["image/jpeg", "image/png", "image/webp"].includes(type)) {
      bytes = await toJpeg(file);
      type = "image/jpeg";
      name = name.replace(/\.[^.]*$/, "") + ".jpg";
    }
    L.job = { bytes, name, type, url, id: null, done: false };
  } catch (e) {
    showFailure(`この写真は送れません: ${e.message}`);
    return;
  }
  L.queued = true;
  if (L.sender && L.conn && L.conn.open) flush();
  else {
    setJob("PC につながるのを待っています…", null, "つながったら自動で送ります。");
    connect();
  }
}

async function flush() {
  const job = L.job;
  if (!job || !L.queued || !L.sender || L.sending) return;
  L.queued = false;
  L.sending = true;
  setJob("送っています…", 0);
  try {
    const p = L.sender.send(job.bytes, job.name, job.type, (f) => {
      if (L.job === job && !job.done) setJob("送っています…", f, `${fmt(job.bytes.byteLength * f / 1e6, 1)} / ${fmt(job.bytes.byteLength / 1e6, 1)} MB`);
    });
    job.id = L.sender.id;
    await p;
    if (L.job === job && !job.done && $("#job-title").textContent.startsWith("送っています")) setJob("PC が受け取りました", 1);
  } catch (e) {
    if (L.job === job) showFailure(e.message);
  } finally {
    L.sending = false;
    if (L.queued) flush();                // 送っている間に次の写真を撮った
  }
}

// ---- ホーム画面に追加の案内 ---------------------------------------------------------------------

function showInstallHint() {
  const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  const hidden = storageGet("hazaism.send.installHint") === "hidden";
  if (standalone || hidden) return;
  const ios = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  $("#install-how").textContent = ios
    ? "Safari の共有ボタン（四角から上向きの矢印）→「ホーム画面に追加」"
    : "ブラウザのメニュー（︙）→「ホーム画面に追加」または「アプリをインストール」";
  $("#install").hidden = false;
  $("#btn-install-hide").onclick = () => {
    $("#install").hidden = true;
    storageSet("hazaism.send.installHint", "hidden");
  };
}

// ---- 起動 -----------------------------------------------------------------------------------

function main() {
  L.pcId = readPcId();
  if (!L.pcId) {
    $("#unpaired").hidden = false;
    setConn("idle", "PC とまだつないでいません");
    return;
  }
  $("#paired").hidden = false;
  $("#btn-shoot").onclick = () => $("#in-shoot").click();
  $("#btn-pick").onclick = () => $("#in-pick").click();
  for (const id of ["#in-shoot", "#in-pick"]) {
    $(id).onchange = (e) => {
      const f = e.target.files && e.target.files[0];
      e.target.value = "";
      takeFile(f);
    };
  }
  $("#btn-retry").onclick = () => {
    if (!L.job) return;
    L.queued = true;
    if (L.sender && L.conn && L.conn.open) flush();
    else {
      setJob("PC につながるのを待っています…", null, "つながったら自動で送ります。");
      connect();
    }
  };
  document.addEventListener("visibilitychange", () => { if (!document.hidden && !L.conn) connect(); });
  window.addEventListener("online", () => connect());
  window.addEventListener("offline", () => setConn("error", "ネットにつながっていません"));
  window.addEventListener("hashchange", () => location.reload());     // 別の PC の QR を読んだとき
  showInstallHint();
  connect();
}

main();
