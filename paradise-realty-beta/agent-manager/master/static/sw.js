/* Minimal service worker — caches the PWA shell, never caches API calls. */
const CACHE = "agentmgr-v5";
const SHELL = [
  "/",
  "/static/app.js",
  "/static/style.css",
  "/static/icon.svg",
  "/manifest.json",
];
const API_PREFIXES = [
  "/chat", "/upload", "/passkey", "/password", "/approvals", "/sessions",
  "/agents", "/cloudrun", "/memory", "/health",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  // API traffic is always live — never served from cache.
  if (
    e.request.method !== "GET" ||
    API_PREFIXES.some((p) => url.pathname === p || url.pathname.startsWith(p + "/"))
  ) {
    return;
  }
  e.respondWith(caches.match(e.request).then((r) => r || fetch(e.request)));
});
