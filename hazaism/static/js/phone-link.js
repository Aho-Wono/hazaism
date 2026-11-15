// スマホ → PC の写真の受け渡し（スマホのページ send.html と、PC の Hazaism の両方で使う）
//
// 2 つのページをブラウザどうし（WebRTC）で直接つなぐ。相手を見つける仲介だけ PeerJS の公開サーバー（0.peerjs.com）を使い、
// 写真そのものはスマホから PC へ直接、暗号化されて流れる（直接つながらない回線では PeerJS の中継を通るが、暗号化されたまま）。
//
// 送り方の取り決め（PeerJS の serialization は "raw"。文字列は JSON の合図、ArrayBuffer は写真の中身）:
//   スマホ → PC   {t:"photo", id, name, type, size}  → 中身を CHUNK ずつ → {t:"end", id}
//   PC → スマホ   {t:"ack", id, bytes}               ACK_EVERY ごとと最後（スマホは返事を待ちながら送る）
//                 {t:"status", id, stage, ...}       stage: received / confirm / analyzing / done / declined / error

export const PUBLIC_SEND_URL = "https://aho-wono.github.io/hazaism/send.html";
export const CHUNK = 16 * 1024;              // Safari との相互接続でも安全な大きさ
export const ACK_EVERY = 1024 * 1024;
export const MAX_BYTES = 50 * 1024 * 1024;
const WINDOW = 4 * ACK_EVERY;                 // 返事を待たずに送ってよい量
const ACK_TIMEOUT = 30_000;

// ---- 小道具 ----------------------------------------------------------------------------

const ALNUM = "abcdefghijklmnopqrstuvwxyz0123456789";
export function randomId(n) {
  const a = new Uint8Array(n);
  crypto.getRandomValues(a);
  return [...a].map((v) => ALNUM[v % ALNUM.length]).join("");
}

// PC の待ち受けの番号（PeerJS の ID の規則 /^[A-Za-z0-9]+(?:[ _-][A-Za-z0-9]+)*$/ に合う形）
export const newPcId = () => `hazaism-${randomId(24)}`;
export const validPcId = (s) => typeof s === "string" && /^hazaism-[a-z0-9]{16,64}$/.test(s);

export function storageGet(key) {
  try {
    const v = localStorage.getItem(key);
    return v ? JSON.parse(v) : null;
  } catch (e) {
    return null;
  }
}

export function storageSet(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
    return true;
  } catch (e) {
    return false;
  }
}

// PeerJS（同梱の vendor/peerjs.min.js）を一度だけ読み込む
let peerjsLoading = null;
export function loadPeerJs() {
  if (window.peerjs && window.peerjs.Peer) return Promise.resolve(window.peerjs.Peer);
  if (!peerjsLoading) {
    peerjsLoading = new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = new URL("./vendor/peerjs.min.js", import.meta.url).href;
      s.onload = () => (window.peerjs && window.peerjs.Peer ? resolve(window.peerjs.Peer) : reject(new Error("PeerJS を読み込めません")));
      s.onerror = () => {
        peerjsLoading = null;
        s.remove();
        reject(new Error("通信の部品を読み込めません。ネット接続を確かめてください"));
      };
      document.head.appendChild(s);
    });
  }
  return peerjsLoading;
}

// 先頭のバイトから画像の種類を見る（画像でなければ null）
export function sniffImage(b) {
  const s = (i, j) => String.fromCharCode(...b.subarray(i, j));
  if (b.length < 12) return null;
  if (b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return "image/jpeg";
  if (b[0] === 0x89 && s(1, 4) === "PNG") return "image/png";
  if (s(0, 4) === "RIFF" && s(8, 12) === "WEBP") return "image/webp";
  if (s(4, 8) === "ftyp" && /^(heic|heix|hevc|hevx|heim|heis|mif1|msf1)$/.test(s(8, 12))) return "image/heic";
  return null;
}

const EXT = { "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/heic": ".heic" };

function safeName(name, type) {
  let n = String(name || "").replace(/[\\/:*?"<>|\u0000-\u001f]/g, "_").slice(-80);
  if (!n) n = "phone";
  if (!/\.[a-z0-9]{2,5}$/i.test(n)) n += EXT[type] || "";
  return n;
}

function toArrayBuffer(data) {
  if (data instanceof ArrayBuffer) return data;
  if (ArrayBuffer.isView(data)) return data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength);
  return null;
}

// ---- 受け取る側（PC） -------------------------------------------------------------------

// 戻り値: 届いたものを渡す関数 feed(data)。写真がそろったら onFile(id, File) を呼ぶ
//   sendText(obj): 相手へ合図を送る
export function createReceiver(sendText, onFile) {
  let cur = null;
  let chain = Promise.resolve();
  const fail = (id, error) => sendText({ t: "status", id, stage: "error", error });

  async function step(data) {
    if (typeof data === "string") {
      let m;
      try { m = JSON.parse(data); } catch (e) { return; }
      if (m.t === "photo") {
        cur = null;
        const size = Number(m.size);
        if (!(size > 0)) return fail(m.id, "写真が空です");
        if (size > MAX_BYTES) return fail(m.id, `写真が大きすぎます（${Math.round(MAX_BYTES / 1e6)} MB まで）`);
        cur = { id: String(m.id), name: m.name, size, buf: new Uint8Array(size), got: 0, nextAck: ACK_EVERY };
      } else if (m.t === "end" && cur && m.id === cur.id) {
        const c = cur;
        cur = null;
        if (c.got !== c.size) return fail(c.id, "写真が途中で途切れました。もう一度送ってください");
        const type = sniffImage(c.buf);
        if (!type) return fail(c.id, "画像ではないファイルです");
        onFile(c.id, new File([c.buf], safeName(c.name, type), { type }));
      }
      return;
    }
    if (!cur) return;
    const ab = data instanceof Blob ? await data.arrayBuffer() : toArrayBuffer(data);
    if (!ab) return;
    if (cur.got + ab.byteLength > cur.size) {
      const id = cur.id;
      cur = null;
      return fail(id, "写真の大きさが合いません。もう一度送ってください");
    }
    cur.buf.set(new Uint8Array(ab), cur.got);
    cur.got += ab.byteLength;
    if (cur.got >= cur.nextAck || cur.got === cur.size) {
      sendText({ t: "ack", id: cur.id, bytes: cur.got });
      while (cur.nextAck <= cur.got) cur.nextAck += ACK_EVERY;
    }
  }

  // Blob で届いたときの読み出しは非同期なので、届いた順に 1 つずつ処理する
  return (data) => {
    chain = chain.then(() => step(data)).catch((e) => console.warn("phone-link receive", e));
  };
}

// ---- 送る側（スマホ） -------------------------------------------------------------------

export class Sender {
  // send(data): 文字列か ArrayBuffer をそのまま相手へ送る
  constructor(send) {
    this._send = send;
    this.id = null;
    this.size = 0;
    this.acked = 0;
    this.onProgress = null;
    this._wake = null;
    this._err = null;
  }

  // 相手からの合図（JSON を解いたもの）。ack なら進める
  onMessage(m) {
    if (m.t !== "ack" || m.id !== this.id) return;
    this.acked = Math.max(this.acked, Number(m.bytes) || 0);
    if (this.onProgress && this.size) this.onProgress(Math.min(1, this.acked / this.size));
    this._ping();
  }

  // つながりが切れたときなど
  abort(err) {
    this._err = err || new Error("送れませんでした");
    this._ping();
  }

  _ping() {
    const w = this._wake;
    this._wake = null;
    if (w) w();
  }

  _wait() {
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => {
        this._wake = null;
        reject(new Error("PC から返事がありません"));
      }, ACK_TIMEOUT);
      this._wake = () => { clearTimeout(t); resolve(); };
    });
  }

  // bytes: ArrayBuffer。PC がすべて受け取ったら、送った写真の id を返す
  async send(bytes, name, type, onProgress = null) {
    const id = randomId(10);
    Object.assign(this, { id, size: bytes.byteLength, acked: 0, _err: null, onProgress });
    const size = bytes.byteLength;
    this._send(JSON.stringify({ t: "photo", id, name, type, size }));
    for (let off = 0; off < size; off += CHUNK) {
      while (off - this.acked > WINDOW) {
        if (this._err) throw this._err;
        await this._wait();
      }
      if (this._err) throw this._err;
      this._send(bytes.slice(off, Math.min(size, off + CHUNK)));
    }
    this._send(JSON.stringify({ t: "end", id }));
    while (this.acked < size) {
      if (this._err) throw this._err;
      await this._wait();
    }
    return id;
  }
}
