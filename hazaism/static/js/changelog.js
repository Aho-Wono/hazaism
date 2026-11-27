// 更新履歴（CHANGELOG.md。tools/build_site.py がサイトに入れる）を画面で見せる。右上の版の番号を押すと開く。
// 新しい版になってから初めて開いたときは、画面の下で知らせる（前に見た版はブラウザに覚えておく。初めて使うときは知らせない）

import { $, toast } from "./api.js";

const KEY = "hazaism.seenVersion";
const URL_RE = /(https?:\/\/[^\s）)」、。]+)/;

// CHANGELOG.md を読む。書き方:
//   ## 0.6.1（2026-11-27）見出し   ← 版・日付・見出し（かっこは全角でも半角でもよい）
//   - 変わったこと                  ← 2 文字以上下げた「- 」は 1 つ上の項目の下に入る
//   ふつうの行                       ← 版の説明（段落）
// 最初の「## 版」より前と、版の形でない見出しの下は読まない
export function parseChangelog(text) {
  const out = [];
  let cur = null;
  let item = null;
  let para = null;
  for (const raw of String(text).replace(/\r\n?/g, "\n").split("\n")) {
    const line = raw.replace(/\s+$/, "");
    if (/^#/.test(line)) {
      const h = line.match(/^##\s+v?(\d+(?:\.\d+)*)\s*[（(]\s*([^）)]*?)\s*[）)]\s*(.*)$/);
      cur = h ? { version: h[1], date: h[2], title: h[3], notes: [], items: [] } : null;
      if (cur) out.push(cur);
      item = para = null;
      continue;
    }
    if (!cur) continue;
    if (!line.trim()) {
      item = para = null;
      continue;
    }
    const b = line.match(/^(\s*)[-*+]\s+(.*)$/);
    if (b) {
      const node = { text: b[2], sub: [] };
      const parent = cur.items[cur.items.length - 1];
      if (b[1].length >= 2 && parent) parent.sub.push(node);
      else cur.items.push(node);
      item = node;
      para = null;
    } else if (item && /^\s/.test(line)) {
      item.text = join(item.text, line.trim());
    } else if (para !== null) {
      cur.notes[para] = join(cur.notes[para], line.trim());
    } else {
      para = cur.notes.push(line.trim()) - 1;
      item = null;
    }
  }
  return out;
}

// 折り返した行をつなぐ（日本語どうしは詰め、英数字どうしは空白を入れる）
function join(a, b) {
  return /[\x21-\x7e]$/.test(a) && /^[\x21-\x7e]/.test(b) ? `${a} ${b}` : a + b;
}

// 版の番号の大小（0.10.0 > 0.9.0）。a が新しければ正
export function compareVersions(a, b) {
  const pa = String(a).split(".").map(Number);
  const pb = String(b).split(".").map(Number);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const d = (pa[i] || 0) - (pb[i] || 0);
    if (d) return d;
  }
  return 0;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

// 文を入れる（URL はリンクにする。HTML としては読まない）
function fill(node, text) {
  for (const part of text.split(URL_RE)) {
    if (!part) continue;
    if (URL_RE.test(part)) {
      const a = el("a", "", part);
      a.href = part;
      a.target = "_blank";
      a.rel = "noopener";
      node.append(a);
    } else {
      node.append(part);
    }
  }
  return node;
}

function list(items) {
  const ul = el("ul");
  for (const it of items) {
    const li = fill(el("li"), it.text);
    if (it.sub.length) li.append(list(it.sub));
    ul.append(li);
  }
  return ul;
}

// seen: 前に見た版（それより新しい版に「新しい」の印）。current: いま使っている版
export function renderChangelog(box, entries, { seen = null, current = "" } = {}) {
  box.replaceChildren();
  for (const e of entries) {
    const sec = el("section", "change");
    sec.dataset.version = e.version;
    const head = el("div", "change-head");
    head.append(el("span", "change-ver", `v${e.version}`));
    if (e.date) head.append(el("span", "change-date", e.date));
    // 前に見た版より新しく、いまの版まで（まだ配っていない版は書いてあっても印を付けない）
    const isNew = !!seen && compareVersions(e.version, seen) > 0 && (!current || compareVersions(e.version, current) <= 0);
    const isCurrent = !!current && compareVersions(e.version, current) === 0;
    if (isNew) {
      sec.classList.add("is-new");
      head.append(el("span", "change-tag new", "新しい"));
    }
    if (isCurrent) {
      sec.classList.add("is-current");
      head.append(el("span", "change-tag now", "いまの版"));
    }
    sec.append(head);
    if (e.title) sec.append(el("h4", "change-title", e.title));
    for (const n of e.notes) sec.append(fill(el("p"), n));
    if (e.items.length) sec.append(list(e.items));
    box.append(sec);
  }
  if (!entries.length) box.append(el("p", "msg", "更新履歴がありません。"));
}

function load() {
  try {
    return localStorage.getItem(KEY);
  } catch (e) {
    return null;
  }
}

function save(v) {
  try {
    localStorage.setItem(KEY, v);
  } catch (e) { /* 保存できないブラウザでは、毎回知らせないだけ */ }
}

// 右上の版の番号（#version）を押すと開く。version は今の版（version.json）
export function initChangelog(version) {
  const btn = $("#version");
  const dlg = $("#dlg-changes");
  const box = $("#changes-body");
  const seen = load();
  let entries = null;

  async function open() {
    btn.classList.remove("has-news");
    if (version) save(version);
    if (!dlg.open) dlg.showModal();
    if (entries) return;
    box.replaceChildren(el("p", "msg info-msg", "読み込んでいます…"));
    try {
      const r = await fetch("CHANGELOG.md", { cache: "no-cache" });
      if (!r.ok) throw new Error(String(r.status));
      entries = parseChangelog(await r.text());
      renderChangelog(box, entries, { seen, current: version });
    } catch (e) {
      box.replaceChildren(el("p", "msg bad", "更新履歴を読み込めません。ネットワークを確かめて、もう一度開いてください。"));
    }
  }

  btn.onclick = open;
  $("#btn-changes-close").onclick = () => dlg.close();
  if (!version) return;
  if (!seen) save(version);              // 初めて使う: 知らせない
  else if (compareVersions(version, seen) > 0) {
    btn.classList.add("has-news");
    toast(`新しい版（v${version}）になりました`, "", 15000, { label: "変わったことを見る", onClick: open });
  }
}
