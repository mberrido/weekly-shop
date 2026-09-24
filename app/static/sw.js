// Weekly Shop service worker: keeps the app shell available offline so the
// shopping list (cached by the page in localStorage) still opens in a
// supermarket with no signal. API calls are never cached here.
const CACHE = "weekly-shop-v1";
const SHELL = ["/icons/icon-192.png", "/icons/favicon.svg", "/manifest.webmanifest"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", e => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;
  if (req.mode === "navigate" && url.pathname === "/") {
    e.respondWith(fetch(req).then(res => {
      if (res.ok && res.type === "basic") { const copy = res.clone(); caches.open(CACHE).then(c => c.put("/", copy)); }
      return res;
    }).catch(() => caches.match("/").then(r => r || Response.error())));
    return;
  }
  if (SHELL.includes(url.pathname)) {
    e.respondWith(caches.match(req).then(r => r || fetch(req)));
  }
});
