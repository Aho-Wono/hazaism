// 画面全体の流れ（ステップの切り替え）と、ステップ 1: 写真

import { $, $$, api, fmt, isBusy, setBusyText, svg, toast, withBusy } from "./api.js";
import { imageUrl, nestRunning, site, startCore } from "./backend.js";
import { S, clearLayers, initCtx, layers, msgHtml, nav, screenLabel, setHint, vp } from "./ctx.js";
import { enterExport, initExport, leaveExport, resetExportFrame } from "./export.js";
import { enterNest, initNest, leaveNest } from "./nest-view.js";
import { enterOutline, initOutline, leaveOutline, outlineEdited, outlineOk, resetOutline, restoreOutline } from "./outline-editor.js";
import { addPartFiles, enterParts, initParts, leaveParts, partsOk } from "./parts.js";
import { initPhoneReceive, startPhoneReceive } from "./phone-receive.js";
import { isTyping } from "./viewport.js";

const STEPS = ["photo", "outline", "parts", "nest", "export"];
const ENTER = { photo: enterPhoto, outline: enterOutline, parts: enterParts, nest: enterNest, export: enterExport };
const LEAVE = { photo: leavePhoto, outline: leaveOutline, parts: leaveParts, nest: leaveNest, export: leaveExport };

function canEnter(step) {
  const photoOk = !!(S.photo && S.photo.ok && S.photo.rect);
  if (step === "photo") return true;
  if (step === "outline") return photoOk;
  if (step === "parts") return photoOk && !!S.outline && outlineOk();
  if (step === "nest") return canEnter("parts") && partsOk();
  if (step === "export") return canEnter("nest") && !!(S.result && S.result.placements && S.result.placements.length);
  return false;
}

function refresh() {
  const cur = STEPS.indexOf(S.step);
  $$("#steps button").forEach((b) => {
    const i = STEPS.indexOf(b.dataset.step);
    b.disabled = !canEnter(b.dataset.step);
    b.classList.toggle("current", i === cur);
    b.classList.toggle("done", i < cur);
  });
  $("#btn-photo-next").disabled = !canEnter("outline");
}

function goto(step) {
  if (!canEnter(step)) {
    toast("先に前のステップを済ませてください", "bad");
    return;
  }
  LEAVE[S.step]();
  S.step = step;
  $$(".pane").forEach((p) => (p.hidden = p.dataset.pane !== step));
  $("#coords").textContent = "";
  ENTER[step]();
  refresh();
}

// ---- ステップ 1: 写真 ----
const P = { active: false, calib: null, photoSeq: 0 };

function enterPhoto() {
  P.active = true;
  vp.flipY = false;
  clearLayers();
  setHint("");
  renderPhotoInfo();
  if (!S.photo) {
    setHint("写真を選ぶか、ここにドラッグ＆ドロップしてください");
    return;
  }
  const img = svg("image", { x: 0, y: 0, width: S.photo.width, height: S.photo.height,
    preserveAspectRatio: "none", style: "pointer-events: none" }, layers.image);
  imageUrl("/api/photo/preview.jpg", `${S.photo.width}x${S.photo.height}-${S.photo.rect ? S.photo.rect.version : 0}-${P.photoSeq}`)
    .then((url) => { if (url) img.setAttribute("href", url); });
  drawPhotoOverlay();
  vp.fit(0, 0, S.photo.width, S.photo.height);
}

function leavePhoto() {
  P.active = false;
  P.calib = null;
}

function drawPhotoOverlay() {
  layers.shape.replaceChildren();
  layers.over.replaceChildren();
  layers.labels.replaceChildren();
  const g = S.photo && S.photo.grid;
  if (g && !P.calib) {
    const grp = svg("g", { class: "photo-grid", "pointer-events": "none" }, layers.shape);
    for (const ln of g.lines) {
      svg("line", { x1: ln.p[0][0], y1: ln.p[0][1], x2: ln.p[1][0], y2: ln.p[1][1], class: ln.major ? "major" : "" }, grp);
    }
  }
  if (P.calib) {
    P.calib.forEach((p, i) => {
      svg("circle", { cx: p[0], cy: p[1], r: vp.px(6), class: "calib-pt" }, layers.over);
      screenLabel(p, ["左上", "右上", "右下", "左下"][i], { dx: 11, dy: -11, cls: "label calib" });
    });
  }
}

function renderPhotoInfo() {
  const info = $("#photo-info"), warn = $("#photo-warnings"), calib = $("#calib");
  warn.replaceChildren();
  info.hidden = true;
  calib.hidden = !S.photo;
  if (!S.photo) return;
  if (!S.photo.ok) {
    warn.appendChild(msgHtml("bad", `格子を自動で見つけられませんでした（${S.photo.error}）。撮り直すか、下の「格子を手動で合わせる」を使ってください。`));
    calib.open = true;
    return;
  }
  const g = S.photo.grid;
  info.hidden = false;
  info.innerHTML =
    `<span class="k">格子</span><b>${g.manual ? "手動で合わせました" : "見つかりました"}</b>（${g.cells[0]} × ${g.cells[1]} マス）<br>` +
    (g.rms_mm !== null ? `<span class="k">当てはめ誤差</span><b>${fmt(g.rms_mm, 2)} mm</b><br>` : "") +
    `<span class="k">細かさ</span><b>${fmt(g.mm_per_px, 2)} mm / 画素</b><br>` +
    `<span class="k">撮影の傾き</span><b>${fmt(g.tilt_deg, 0)}°</b>（0° が真上）<br>` +
    `<span class="k">見つかった板</span><b>${S.photo.candidates.length} 枚</b>`;
  for (const w of S.photo.warnings || []) warn.appendChild(msgHtml("warn", w));
  if (!S.photo.candidates.length) {
    warn.appendChild(msgHtml("warn", "板が見つかりませんでした。次の画面で感度を上げて検出し直すか、図形の道具で輪郭を描いてください。"));
  }
}

function adoptPhoto(info) {
  S.photo = info;
  P.photoSeq++;
  S.result = null;
  resetOutline();
  resetExportFrame();
  P.calib = null;
  if (S.step !== "photo") goto("photo"); else enterPhoto();
  refresh();
  if (info.ok) toast("写真を読み込みました。格子の線が写真の線に重なっているか確認してください", "ok", 5000);
}

// ブラウザで画像を開いて JPEG にする（Python で読めない形式、たとえば HEIC 用。開けるかはブラウザによる）
async function toJpeg(file) {
  const bmp = await createImageBitmap(file);
  const c = new OffscreenCanvas(bmp.width, bmp.height);
  c.getContext("2d").drawImage(bmp, 0, 0);
  bmp.close();
  const blob = await c.convertToBlob({ type: "image/jpeg", quality: 0.95 });
  return new File([blob], (file.name || "photo").replace(/\.[^.]+$/, "") + ".jpg", { type: "image/jpeg" });
}

// 戻り値: { info }（解析した結果）か { error }（読めなかった）
async function uploadPhoto(file) {
  const send = (f) => {
    const fd = new FormData();
    fd.append("file", f, f.name || "photo.jpg");
    return api("POST", "/api/photo", fd);
  };
  try {
    const info = await withBusy("写真を解析しています…（20 秒ほどかかります）", async () => {
      try {
        return await send(file);
      } catch (e) {
        if (e.code !== "unsupported_image") throw e;
        let jpg;
        try {
          jpg = await toJpeg(file);
        } catch (e2) {
          throw new Error("この形式の写真は読めません（HEIC なら JPEG に変換してから読み込んでください）");
        }
        return await send(jpg);
      }
    });
    adoptPhoto(info);
    return { info };
  } catch (e) {
    toast(e.message, "bad", 6000);
    return { error: e.message };
  }
}

// ---- スマホから届いた写真 ----
// 作業を消さずに済むときは、すぐ読み込んで解析する。作業の途中なら PC で確かめてから
const IN = { pending: null };     // PC で確認を待っている写真 { file, reply, url }

function quietToReplace() {
  if (isBusy() || nestRunning()) return false;
  if (!S.photo) return true;
  if (S.result) return S.exportedResult === S.result;      // 敷き詰めた結果は、DXF に保存してあれば消してよい
  return !outlineEdited();
}

function whatIsLost() {
  if (nestRunning()) return "敷き詰めの計算を止めて、";
  if (isBusy()) return "いまの処理のかわりに、";
  if (S.result) return "まだ DXF に保存していない敷き詰めの結果を捨てて、";
  return "手で直した輪郭を捨てて、";
}

// スマホへ返す、解析の結果のまとめ
function photoSummary(r) {
  if (r.error) return { ok: false, unreadable: true, error: r.error };
  const i = r.info, g = i.grid || {};
  return {
    ok: !!i.ok, error: i.ok ? null : i.error, cells: g.cells || null, rms_mm: g.rms_mm ?? null,
    mm_per_px: g.mm_per_px ?? null, tilt_deg: g.tilt_deg ?? null,
    candidates: (i.candidates || []).length, warnings: i.warnings || [],
  };
}

async function loadFromPhone(file, reply) {
  reply("analyzing");
  toast("スマホから写真が届きました", "ok");
  const r = await uploadPhoto(file);
  reply("done", photoSummary(r));
}

function dropPending(reason) {
  const p = IN.pending;
  if (!p) return;
  IN.pending = null;
  URL.revokeObjectURL(p.url);
  p.reply("declined", { reason });
}

function receivePhoto(file, reply) {
  dropPending("replaced");            // 確認待ちの写真があれば、新しいほうに置き換える
  if (quietToReplace()) {
    loadFromPhone(file, reply);
    return;
  }
  IN.pending = { file, reply, url: URL.createObjectURL(file) };
  $("#incoming-thumb").src = IN.pending.url;
  $("#incoming-what").textContent = whatIsLost();
  const dlg = $("#dlg-incoming");
  if (!dlg.open) dlg.showModal();
  reply("confirm");
}

function initIncoming() {
  const dlg = $("#dlg-incoming");
  $("#btn-incoming-load").onclick = () => {
    const p = IN.pending;
    IN.pending = null;
    dlg.close();
    if (!p) return;
    setTimeout(() => URL.revokeObjectURL(p.url), 1000);
    loadFromPhone(p.file, p.reply);
  };
  $("#btn-incoming-skip").onclick = () => dlg.close();
  dlg.addEventListener("close", () => dropPending("declined"));      // 「読み込まない」と Esc
}

function initPhoto() {
  $("#btn-pick-photo").onclick = () => $("#file-photo").click();
  $("#file-photo").onchange = (e) => { if (e.target.files[0]) uploadPhoto(e.target.files[0]); e.target.value = ""; };
  $("#btn-photo-next").onclick = () => goto("outline");
  vp.onChange(() => { if (P.active) drawPhotoOverlay(); });

  // 手動校正
  $("#btn-calib-start").onclick = () => {
    P.calib = [];
    $("#btn-calib-apply").disabled = true;
    setHint("格子の交点を 左上 → 右上 → 右下 → 左下 の順にクリック");
    drawPhotoOverlay();
  };
  $("#canvas").addEventListener("click", (e) => {
    if (!P.active || !P.calib || e.button !== 0 || vp.panning || vp.spaceDown || P.calib.length >= 4) return;
    P.calib.push(vp.toWorld(e.clientX, e.clientY));
    $("#btn-calib-apply").disabled = P.calib.length < 4;
    if (P.calib.length === 4) setHint("マス数を確かめて「この 4 点で合わせる」を押してください");
    drawPhotoOverlay();
  });
  $("#btn-calib-apply").onclick = async () => {
    try {
      const info = await withBusy("格子を合わせています…", () => api("POST", "/api/calibrate", {
        points: P.calib, cells_u: +$("#calib-u").value, cells_v: +$("#calib-v").value,
      }));
      adoptPhoto(info);
    } catch (e) {
      toast(e.message, "bad", 6000);
    }
  };
}

// ---- ドラッグ＆ドロップと貼り付け ----
function initDrop() {
  const dz = $("#dropzone");
  let depth = 0;
  const hasFiles = (e) => e.dataTransfer && [...e.dataTransfer.types].includes("Files");
  window.addEventListener("dragenter", (e) => { if (hasFiles(e)) { depth++; dz.hidden = false; e.preventDefault(); } });
  window.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
  window.addEventListener("dragleave", () => { depth = Math.max(0, depth - 1); if (!depth) dz.hidden = true; });
  window.addEventListener("drop", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth = 0;
    dz.hidden = true;
    const files = [...e.dataTransfer.files];
    const dxf = files.filter((f) => /\.dxf$/i.test(f.name));
    const img = files.find((f) => f.type.startsWith("image/") || /\.(heic|heif|jpe?g|png|webp|bmp|tiff?)$/i.test(f.name));
    if (dxf.length) {
      addPartFiles(dxf).then(() => { if (S.step !== "parts" && canEnter("parts")) goto("parts"); });
    } else if (img) {
      uploadPhoto(img);
    } else {
      toast("写真か DXF ファイルをドロップしてください", "bad");
    }
  });
  window.addEventListener("paste", (e) => {
    if (isTyping(e)) return;
    const f = [...(e.clipboardData ? e.clipboardData.files : [])].find((x) => x.type.startsWith("image/"));
    if (f) { e.preventDefault(); uploadPhoto(f); }
  });
}

// ---- 起動 ----
async function loadPython() {
  // 初回は Python とライブラリ（数十 MB）を読み込むので、進み具合を出す
  const busy = $("#busy");
  busy.hidden = false;
  busy.classList.add("loading");
  setBusyText("準備しています…");
  const bar = $("#busy-bar");
  try {
    await startCore((m) => {
      setBusyText(m.text + (m.pct < 100 ? "（初回は 1 分ほどかかります）" : ""));
      if (bar) bar.style.width = `${m.pct}%`;
    });
  } finally {
    busy.hidden = true;
    busy.classList.remove("loading");
  }
}

function registerServiceWorker() {
  // 一度読み込んだファイルをブラウザに残して、次から速く開けるようにする（オフラインでも開ける）
  if ("serviceWorker" in navigator && location.protocol.startsWith("http")) {
    navigator.serviceWorker.register("sw.js").catch((e) => console.warn("service worker", e));
  }
}

async function main() {
  initCtx();
  registerServiceWorker();
  nav.goto = goto;
  nav.refresh = refresh;
  initPhoto();
  initOutline();
  initParts();
  initNest();
  initExport();
  initDrop();
  initIncoming();
  initPhoneReceive(receivePhoto);
  $$("#steps button").forEach((b) => (b.onclick = () => goto(b.dataset.step)));
  $("#btn-fit").onclick = () => vp.refit();
  const dlgReset = $("#dlg-reset");
  $("#btn-reset").onclick = () => dlgReset.showModal();
  $("#btn-reset-cancel").onclick = () => dlgReset.close();
  const doReset = async (keepParts) => {
    dlgReset.close();
    try {
      const st = await api("POST", "/api/reset", { keep_parts: keepParts });
      // 部材の一覧は、サーバーに残っているものをそのまま表示する（画面とサーバーで食い違わないように）
      Object.assign(S, { settings: st.settings, photo: null, outline: null, candIndex: null, parts: st.parts || [], result: null });
      resetExportFrame();
      $("#in-filename").value = "";
      LEAVE[S.step]();
      S.step = "photo";
      $$(".pane").forEach((p) => (p.hidden = p.dataset.pane !== "photo"));
      enterPhoto();
      refresh();
    } catch (e) {
      toast(e.message, "bad");
    }
  };
  $("#btn-reset-keep").onclick = () => doReset(true);
  $("#btn-reset-all").onclick = () => doReset(false);

  try {
    await loadPython();
  } catch (e) {
    $("#busy").hidden = false;
    setBusyText(e.message);
    $(".spinner").hidden = true;
    return;
  }
  $("#version").textContent = site.version ? `v${site.version}` : "";

  try {
    const st = await api("GET", "/api/state");
    Object.assign(S, { settings: st.settings, photo: st.photo, outline: st.outline, parts: st.parts || [], result: st.result });
    restoreOutline();
  } catch (e) {
    toast(e.message, "bad", 8000);
  }
  // 前回の続きがあれば、輪郭の画面から始める
  S.step = "photo";
  $$(".pane").forEach((p) => (p.hidden = p.dataset.pane !== "photo"));
  if (canEnter("outline") && S.outline) {
    S.step = "outline";
    $$(".pane").forEach((p) => (p.hidden = p.dataset.pane !== "outline"));
    enterOutline();
  } else {
    enterPhoto();
  }
  refresh();
  startPhoneReceive();          // 作業の状態を読み終えてから、スマホからの写真を受け付ける
}

main();
