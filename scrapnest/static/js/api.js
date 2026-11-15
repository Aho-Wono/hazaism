// ブラウザ内の Python への頼みごとと、画面下のお知らせ

import { call, nestCall, stopNestIfRunning } from "./backend.js";

export class ApiError extends Error {
  constructor(msg, status, code) {
    super(msg);
    this.status = status;
    this.code = code || null;
  }
}

// FormData の中のファイルを、作業スレッドへ渡せる形（名前と中身）にする
async function filesOf(fd) {
  const out = [];
  for (const [, v] of fd.entries()) {
    if (v instanceof Blob) out.push({ name: v.name || "file", data: new Uint8Array(await v.arrayBuffer()) });
  }
  return out;
}

// 頼みごとを送って、JSON の答えを返す（中身のファイルも要るときは apiRaw）
export async function api(method, url, body) {
  return (await apiRaw(method, url, body)).json;
}

// 戻り値: { json, data(Uint8Array or null), ctype }
export async function apiRaw(method, url, body) {
  if (url.startsWith("/api/nest/") && !url.startsWith("/api/nest/prepare") && !url.startsWith("/api/nest/finish")) {
    return { json: await nestCall(method, url, body), data: null, ctype: null };
  }
  // 結果が変わる操作（輪郭・部材・写真など）が来たら、計算中の敷き詰めを止める
  if (method !== "GET" && !url.startsWith("/api/settings") && !url.startsWith("/api/nest/")) stopNestIfRunning();
  let files = null;
  if (body instanceof FormData) {
    files = await filesOf(body);
    body = undefined;
  }
  const r = await call(method, url, body, files);
  let data = null;
  try { data = r.json ? JSON.parse(r.json) : null; } catch (e) { /* 本文なし */ }
  if (r.status >= 400) {
    const d = data && data.detail;
    throw new ApiError(typeof d === "string" ? d : `エラー (${r.status})`, r.status, data && data.code);
  }
  return { json: data, data: r.data || null, ctype: r.ctype || null };
}

export function toast(msg, kind = "", ms = 3500) {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  document.getElementById("toasts").appendChild(el);
  setTimeout(() => el.remove(), ms);
}

const busyEl = () => document.getElementById("busy");
let busyCount = 0;

export function setBusyText(text) {
  document.getElementById("busy-text").textContent = text;
}

export async function withBusy(text, fn) {
  busyCount++;
  setBusyText(text);
  busyEl().hidden = false;
  try {
    return await fn();
  } finally {
    if (--busyCount === 0) busyEl().hidden = true;
  }
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const SVG_NS = "http://www.w3.org/2000/svg";
export function svg(tag, attrs = {}, parent = null) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v !== null && v !== undefined) el.setAttribute(k, v);
  }
  if (parent) parent.appendChild(el);
  return el;
}

export function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

export function fmt(v, d = 1) {
  let n = Number(v);
  if (Math.abs(n) < 0.5 * 10 ** -d) n = 0;      // 「-0.0」と出さない
  return n.toLocaleString("ja-JP", { minimumFractionDigits: d, maximumFractionDigits: d });
}
