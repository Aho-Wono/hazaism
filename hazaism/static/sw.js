// 一度読み込んだファイルをブラウザに残して、次から速く（ネットが無くても）開けるようにする。
//   - Pyodide 本体とライブラリ（CDN、版ごとに URL が変わる）と、配っている wheel: 残っていればそれを使う
//   - アプリのファイル: まずネットから取り（常に最新版を使う）、つながらなければ残っているものを使う
const PYODIDE = "__PYODIDE__";              // tools/build_site.py が pyconfig.js の版に置き換える
const CDN_CACHE = `hazaism-pyodide-${PYODIDE}`;
const SITE_CACHE = "hazaism-site";

self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (e) => {
  e.waitUntil((async () => {
    for (const k of await caches.keys()) {
      if (k.startsWith("hazaism-pyodide-") && k !== CDN_CACHE) await caches.delete(k);    // 古い版の Pyodide
      if (k.startsWith("scrapnest-")) await caches.delete(k);         // 旧名（ScrapNest）のときに残したもの
    }
    await self.clients.claim();
  })());
});

async function cacheFirst(req, name) {
  const cache = await caches.open(name);
  const hit = await cache.match(req);
  if (hit) return hit;
  const res = await fetch(req);
  if (res.ok) cache.put(req, res.clone());
  return res;
}

async function networkFirst(req) {
  const cache = await caches.open(SITE_CACHE);
  try {
    const res = await fetch(req);
    if (res.ok) cache.put(req, res.clone());
    return res;
  } catch (err) {
    const hit = await cache.match(req, { ignoreSearch: true });
    if (hit) return hit;
    throw err;
  }
}

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.hostname === "cdn.jsdelivr.net" && url.pathname.startsWith(`/pyodide/v${PYODIDE}/`)) {
    e.respondWith(cacheFirst(req, CDN_CACHE));
  } else if (url.origin === self.location.origin) {
    if (/\/py\/[^/]+\.whl$/.test(url.pathname)) e.respondWith(cacheFirst(req, SITE_CACHE));     // ファイル名に版が入っている
    else e.respondWith(networkFirst(req));
  }
});
