// PC 側: スマホから届く写真を待ち受ける（上の帯の「スマホ」ボタンと、QR を出す画面）
//
// 一度「使い始める」を押すと、次からは Hazaism を開くたびに自動で待ち受ける（番号と設定はこのブラウザに保存）。
// 待ち受けの番号を知っているスマホだけが送れる（番号は推測できない長さの乱数。QR を作り直すと前の番号は使えなくなる）。

import { $ } from "./api.js";
import { PUBLIC_SEND_URL, createReceiver, loadPeerJs, newPcId, storageGet, storageSet, validPcId } from "./phone-link.js";

const KEY = "hazaism.phone";
const R = {
  id: null,
  enabled: false,
  persisted: true,
  allowed: false,      // 作業の状態を読み終えてから待ち受ける（それまでに届くと状態を上書きしてしまう）
  peer: null,
  gen: 0,
  conns: new Set(),
  state: "off",        // off / connecting / ready / taken / error
  error: "",
  retryTimer: null,
  failures: 0,
  takenTries: 0,
  onPhoto: null,
};

function save() {
  R.persisted = storageSet(KEY, { id: R.id, enabled: R.enabled });
}

// スマホで開くページ。手元の配信（127.0.0.1）はスマホから開けないので、公開版を指す
export function sendPageUrl() {
  const local = location.protocol === "file:" || /^(localhost|127\.0\.0\.1|\[::1\])$/.test(location.hostname);
  const base = local ? PUBLIC_SEND_URL : new URL("send.html", location.href).href;
  return `${base}#pc=${R.id}`;
}

export const phoneState = () => ({ id: R.id, enabled: R.enabled, state: R.state, connections: R.conns.size });

// ---- 待ち受け ----------------------------------------------------------------------------

function setState(state, error = "") {
  R.state = state;
  R.error = error;
  render();
}

function clearRetry() {
  clearTimeout(R.retryTimer);
  R.retryTimer = null;
}

function scheduleRetry(ms) {
  clearRetry();
  R.retryTimer = setTimeout(start, ms);
}

const backoff = () => Math.min(60_000, 5000 * 2 ** Math.max(0, R.failures - 1));

function stopPeer() {
  clearRetry();
  R.gen++;
  const p = R.peer;
  R.peer = null;
  for (const c of R.conns) {
    try { c.close(); } catch (e) { /* もう閉じている */ }
  }
  R.conns.clear();
  if (p && !p.destroyed) p.destroy();
}

function errorText(type) {
  if (type === "browser-incompatible") return "このブラウザはスマホからの受け取りに対応していません（Chrome か Edge を使ってください）";
  if (["network", "server-error", "socket-error", "socket-closed", "disconnected"].includes(type)) {
    return "仲介のサーバーにつながりません。ネット接続を確かめてください";
  }
  return `受け取りの準備に失敗しました（${type || "不明"}）`;
}

async function start() {
  if (!R.enabled) return;
  if (!R.allowed) {
    setState("connecting");       // 読み込みが終わったら startPhoneReceive() から始まる
    return;
  }
  stopPeer();
  const gen = R.gen;
  setState("connecting");
  let Peer;
  try {
    Peer = await loadPeerJs();
  } catch (e) {
    if (gen !== R.gen) return;
    R.failures++;
    setState("error", e.message);
    scheduleRetry(backoff());
    return;
  }
  if (gen !== R.gen || !R.enabled) return;
  const peer = new Peer(R.id, { debug: 0 });
  R.peer = peer;
  peer.on("open", () => {
    if (R.peer !== peer) return;
    R.failures = 0;
    R.takenTries = 0;
    setState("ready");
  });
  peer.on("connection", (conn) => {
    if (R.peer === peer) accept(conn);
    else conn.close();
  });
  // 仲介のサーバーとの間が切れた（タブが裏にあって通信が間引かれたときなど）。つなぎ直す
  peer.on("disconnected", () => {
    if (R.peer !== peer || peer.destroyed) return;
    setState("connecting");
    clearRetry();
    R.retryTimer = setTimeout(() => {
      if (R.peer === peer && !peer.destroyed && peer.disconnected) peer.reconnect();
    }, 1000);
  });
  peer.on("error", (err) => {
    if (R.peer !== peer) return;
    const type = err && err.type;
    if (type === "peer-unavailable" || type === "webrtc") return;      // 個々のつながりの失敗（待ち受けは続く）
    if (type === "unavailable-id") {
      // 同じ番号で別のタブが待ち受けている（または開き直した直後で、前の分がまだ残っている）
      R.takenTries++;
      stopPeer();
      setState(R.takenTries > 3 ? "taken" : "connecting");
      scheduleRetry(R.takenTries > 3 ? 30_000 : 3000);
      return;
    }
    R.failures++;
    if (LOST_SERVER.includes(type) && !peer.destroyed && R.failures <= 6) {
      // 仲介のサーバーと切れただけ。つながっているスマホとのやり取り（送信中の写真も）は続けたまま、間隔を空けてつなぎ直す
      setState("error", errorText(type));
      clearRetry();
      R.retryTimer = setTimeout(() => {
        if (R.peer === peer && !peer.destroyed && peer.disconnected) peer.reconnect();
      }, backoff());
      return;
    }
    stopPeer();
    setState("error", errorText(type));
    scheduleRetry(backoff());
  });
}

const LOST_SERVER = ["network", "socket-error", "socket-closed", "disconnected", "server-error"];

function accept(conn) {
  const send = (obj) => {
    if (!conn.open) return;
    try { conn.send(JSON.stringify(obj)); } catch (e) { /* 切れた */ }
  };
  const feed = createReceiver(send, {
    onStart: (m) => banner("start", m),
    onProgress: (m) => banner("progress", m),
    onFail: (m) => banner("fail", m),
    onFile: (id, file) => {
      banner("done");
      const dlg = $("#dlg-phone");
      if (dlg && dlg.open) dlg.close();          // 受け取れたので QR の画面は閉じる
      if (document.hidden) markTitle();
      const reply = (stage, extra = {}) => send({ t: "status", id, stage, ...extra });
      reply("received");
      if (R.onPhoto) R.onPhoto(file, reply);
      else reply("error", { error: "PC の準備ができていません" });
    },
  });
  conn.on("open", () => {
    R.conns.add(conn);
    render();
  });
  conn.on("data", feed);
  const gone = () => {
    if (feed.active()) banner("fail", { error: "スマホとのつながりが途中で切れました。もう一度送ってください" });
    if (R.conns.delete(conn)) render();
  };
  conn.on("close", gone);
  conn.on("error", gone);
}

// ---- 受け取りのお知らせ（画面の上の帯。QR の画面の上にも出るよう、ポップオーバーで最前面に出す） ----

const B = { hideTimer: null, receiving: false, title: null };

function showBanner(el) {
  if (el.popover !== undefined && typeof el.showPopover === "function") {
    if (el.matches(":popover-open")) el.hidePopover();       // 開いているダイアログより手前に出し直す
    el.showPopover();
  } else {
    el.hidden = false;
  }
}

function hideBanner(el) {
  if (typeof el.hidePopover === "function" && el.matches(":popover-open")) el.hidePopover();
  el.hidden = true;
}

const mb = (n) => (n / 1e6).toLocaleString("ja-JP", { minimumFractionDigits: 1, maximumFractionDigits: 1 });

function banner(kind, m = {}) {
  const el = $("#incoming-banner");
  if (!el) return;
  clearTimeout(B.hideTimer);
  if (kind === "start" || kind === "progress") {
    const f = m.size ? Math.min(1, (m.got || 0) / m.size) : 0;
    if (kind === "start" || !B.receiving) {
      el.className = "incoming-banner busy";
      $("#incoming-banner-title").textContent = "スマホから写真を受け取っています…";
      el.hidden = false;
      showBanner(el);
    }
    B.receiving = true;
    $("#incoming-banner-bar").style.width = `${Math.round(f * 100)}%`;
    $("#incoming-banner-detail").textContent = `${Math.round(f * 100)} %（${mb(m.got || 0)} / ${mb(m.size || 0)} MB）`;
  } else if (kind === "done") {
    B.receiving = false;
    el.className = "incoming-banner ok";
    $("#incoming-banner-title").textContent = "スマホから写真を受け取りました";
    $("#incoming-banner-bar").style.width = "100%";
    $("#incoming-banner-detail").textContent = "解析を始めます";
    showBanner(el);
    B.hideTimer = setTimeout(() => hideBanner(el), 1500);
  } else if (kind === "fail") {
    B.receiving = false;
    el.className = "incoming-banner bad";
    $("#incoming-banner-title").textContent = "写真を受け取れませんでした";
    $("#incoming-banner-detail").textContent = m.error || "";
    showBanner(el);
    B.hideTimer = setTimeout(() => hideBanner(el), 5000);
  }
  render();
}

// タブが裏にあるとき、タイトルで届いたことを知らせる（タブを見たら戻す）
function markTitle() {
  if (B.title === null) B.title = document.title;
  document.title = `【写真が届きました】${B.title}`;
}

function restoreTitle() {
  if (B.title !== null) {
    document.title = B.title;
    B.title = null;
  }
}

// タブが見えたとき・ネットが戻ったときに、止まっていればすぐやり直す
function wake() {
  if (!R.enabled || !R.allowed) return;
  if (R.state === "error" || R.state === "taken" || !R.peer) {
    R.failures = 0;
    R.takenTries = 0;
    start();
  } else if (R.peer.disconnected && !R.peer.destroyed) {
    R.peer.reconnect();
  }
}

// ---- 画面 ----------------------------------------------------------------------------------

const STATE_TEXT = {
  off: "止まっています",
  connecting: "準備しています…",
  taken: "ほかのタブ（またはウィンドウ）の Hazaism で受け取っています。そちらを閉じると、ここで受け取ります",
};

function statusText() {
  if (R.state === "ready") {
    return R.conns.size ? `スマホ ${R.conns.size} 台とつながっています` : "待ち受けています（スマホで送信のページを開くと、つながります）";
  }
  if (R.state === "error") return `${R.error}（自動でやり直します）`;
  return STATE_TEXT[R.state] || "";
}

function render() {
  const btn = $("#btn-phone");
  if (btn) {
    const cls = { off: "off", connecting: "wait", ready: R.conns.size ? "live" : "ready", taken: "err", error: "err" }[R.state];
    btn.className = `ghost phone-btn ${B.receiving ? "recv" : cls}`;
    btn.title = R.enabled ? `スマホから送る: ${statusText()}` : "スマホで撮った写真を、この画面へ直接送れるようにします";
  }
  const st = $("#phone-status");
  if (st) {
    st.textContent = statusText();
    st.className = "msg " + ({ ready: "ok", error: "bad", taken: "warn" }[R.state] || "info-msg");
  }
}

async function drawQr(el, text) {
  const { default: qrcode } = await import("./vendor/qrcode.mjs");
  const qr = qrcode(0, "M");
  qr.addData(text);         // URL は ASCII だけ（そのままバイトとして入る）
  qr.make();
  const n = qr.getModuleCount();
  const q = 4;              // まわりの余白（4 マス）
  const size = n + 2 * q;
  let d = "";
  for (let r = 0; r < n; r++) {
    for (let c = 0; c < n; c++) if (qr.isDark(r, c)) d += `M${c + q} ${r + q}h1v1h-1z`;
  }
  el.innerHTML = `<svg viewBox="0 0 ${size} ${size}" shape-rendering="crispEdges" role="img" aria-label="スマホのカメラで読む QR コード">` +
    `<rect width="${size}" height="${size}" fill="#fff"/><path d="${d}" fill="#000"/></svg>`;
}

function renderDialog() {
  $("#phone-off").hidden = R.enabled;
  $("#phone-on").hidden = !R.enabled;
  if (!R.enabled) return;
  const url = sendPageUrl();
  $("#phone-url").textContent = url;
  $("#phone-volatile").hidden = R.persisted;
  drawQr($("#qr"), url).catch((e) => { $("#qr").textContent = `QR を作れません: ${e.message}`; });
  render();
}

function openDialog() {
  renderDialog();
  const dlg = $("#dlg-phone");
  if (!dlg.open) dlg.showModal();
}

// onPhoto(file, reply): 写真が届いたとき。reply(stage, extra) でスマホに進み具合を返す
export function initPhoneReceive(onPhoto) {
  R.onPhoto = onPhoto;
  const st = storageGet(KEY);
  if (st && validPcId(st.id)) {
    R.id = st.id;
    R.enabled = !!st.enabled;
  } else {
    R.id = newPcId();
    R.enabled = false;
  }
  if (R.enabled) R.state = "connecting";      // 作業の状態を読み終えたら待ち受けを始める
  $("#btn-phone").onclick = openDialog;
  $("#btn-photo-phone").onclick = openDialog;
  $("#btn-phone-close").onclick = () => $("#dlg-phone").close();
  $("#btn-phone-close-off").onclick = () => $("#dlg-phone").close();
  $("#btn-phone-enable").onclick = () => {
    R.enabled = true;
    save();
    renderDialog();
    start();
  };
  $("#btn-phone-disable").onclick = () => {
    R.enabled = false;
    save();
    stopPeer();
    setState("off");
    renderDialog();
  };
  $("#btn-phone-regen").onclick = () => {
    if (!confirm("新しい QR を作ります。いままで使っていたスマホのページからは送れなくなるので、もう一度 QR を読んでください。")) return;
    R.id = newPcId();
    save();
    renderDialog();
    start();
  };
  $("#btn-phone-copy").onclick = async () => {
    try {
      await navigator.clipboard.writeText(sendPageUrl());
      $("#btn-phone-copy").textContent = "コピーしました";
      setTimeout(() => ($("#btn-phone-copy").textContent = "URL をコピー"), 2000);
    } catch (e) { /* コピーできないブラウザ */ }
  };
  window.addEventListener("online", wake);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    restoreTitle();
    wake();
  });
  render();
}

// 作業の状態を読み終えたら呼ぶ。使い始めていれば待ち受けを始める
export function startPhoneReceive() {
  R.allowed = true;
  if (R.enabled) start();
}
