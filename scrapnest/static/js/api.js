// サーバーとのやりとりと、画面下のお知らせ

export async function api(method, url, body) {
  const opt = { method };
  if (body instanceof FormData) {
    opt.body = body;
  } else if (body !== undefined) {
    opt.headers = { "Content-Type": "application/json" };
    opt.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(url, opt);
  } catch (e) {
    throw new Error("アプリ本体と通信できません（ScrapNest のウィンドウが閉じていないか確認してください）");
  }
  let data = null;
  try { data = await res.json(); } catch (e) { /* 本文なし */ }
  if (!res.ok) {
    const d = data && data.detail;
    throw new Error(typeof d === "string" ? d : `エラー (${res.status})`);
  }
  return data;
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

export async function withBusy(text, fn) {
  busyCount++;
  document.getElementById("busy-text").textContent = text;
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
