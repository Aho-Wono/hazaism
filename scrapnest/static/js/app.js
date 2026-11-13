// 画面全体の流れ（ステップの切り替え）と、ステップ 1: 写真

import { $, $$, api, fmt, svg, toast, withBusy } from "./api.js";
import { S, clearLayers, initCtx, layers, msgHtml, nav, screenLabel, setHint, vp } from "./ctx.js";
import { enterExport, initExport, leaveExport, resetExportFrame } from "./export.js";
import { enterNest, initNest, leaveNest } from "./nest-view.js";
import { enterOutline, initOutline, leaveOutline, outlineOk, resetOutline, restoreOutline } from "./outline-editor.js";
import { addPartFiles, enterParts, initParts, leaveParts, partsOk } from "./parts.js";
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
const P = { active: false, calib: null };

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
  svg("image", { href: `/api/photo/preview.jpg?t=${Date.now()}`, x: 0, y: 0, width: S.photo.width, height: S.photo.height,
    preserveAspectRatio: "none", style: "pointer-events: none" }, layers.image);
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
  S.result = null;
  resetOutline();
  resetExportFrame();
  P.calib = null;
  if (S.step !== "photo") goto("photo"); else enterPhoto();
  refresh();
  if (info.ok) toast("写真を読み込みました。格子の線が写真の線に重なっているか確認してください", "ok", 5000);
}

async function uploadPhoto(file) {
  const fd = new FormData();
  fd.append("file", file, file.name || "photo.jpg");
  try {
    adoptPhoto(await withBusy("写真を解析しています…（10〜20 秒かかります）", () => api("POST", "/api/photo", fd)));
  } catch (e) {
    toast(e.message, "bad", 6000);
  }
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

  // スマホから送る
  const dlg = $("#dlg-mobile");
  let pollTimer = null;
  const stopPoll = () => { clearInterval(pollTimer); pollTimer = null; };
  $("#btn-mobile").onclick = async () => {
    try {
      const r = await api("POST", "/api/mobile/start");
      $("#qr").innerHTML = r.qr;
      $("#mobile-url").textContent = r.url;
      dlg.showModal();
      stopPoll();
      pollTimer = setInterval(async () => {
        try {
          const s = await api("GET", "/api/mobile/poll");
          if (!s.waiting) return;
          stopPoll();
          dlg.close();
          adoptPhoto(await withBusy("スマホから届いた写真を解析しています…", () => api("POST", "/api/photo/inbox")));
        } catch (e) {
          stopPoll();
          toast(e.message, "bad", 6000);
        }
      }, 1200);
    } catch (e) {
      toast(e.message, "bad");
    }
  };
  $("#btn-mobile-close").onclick = () => dlg.close();
  dlg.addEventListener("close", stopPoll);
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
async function main() {
  initCtx();
  nav.goto = goto;
  nav.refresh = refresh;
  initPhoto();
  initOutline();
  initParts();
  initNest();
  initExport();
  initDrop();
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
      $("#in-path").value = "";
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

  // 版の表示と、新しい版のお知らせ
  api("GET", "/api/version").then((v) => {
    $("#version").textContent = "v" + v.version;
    if (v.update) {
      const el = $("#update-note");
      el.hidden = false;
      el.textContent = `新しい版 v${v.latest} があります`;
      el.title = "ScrapNest を終了して、フォルダーの update.bat を実行すると更新できます";
      el.onclick = (e) => { e.preventDefault(); toast("ScrapNest を終了して、フォルダーの update.bat を実行すると更新できます", "", 7000); };
    }
  }).catch(() => {});

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
}

main();
