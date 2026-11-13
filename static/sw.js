/* Rebuy service worker.
 *
 * Strategy, by request class — deliberately conservative, because this app is
 * an API-backed business tool where a stale answer is worse than no answer:
 *
 *   /api/*            network only. Never cached, never replayed. A shop owner
 *                     looking at "who is due today" must see the server's
 *                     answer or an honest error, not yesterday's queue.
 *   navigation        network first, cache fallback, then offline page.
 *                     Fresh HTML whenever the network is up (so a deploy is
 *                     picked up on the next load); the cached shell is only
 *                     the safety net when the device is offline.
 *   static assets     stale-while-revalidate. Icons, fonts and the manifest
 *                     change rarely and are content-addressed by version.
 *
 * Bump VERSION to invalidate every cache on the next visit.
 */
const VERSION = "rebuy-v1";
const SHELL_CACHE = `${VERSION}-shell`;
const ASSET_CACHE = `${VERSION}-assets`;

/* The minimum needed to show something recognisable with no network. */
const SHELL = ["/", "/offline.html", "/manifest.webmanifest",
               "/icons/icon-192.png", "/icons/icon-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(SHELL_CACHE);
    // addAll() rejects if any single URL fails, which would abort the whole
    // install; add them one at a time so one 404 cannot break the SW.
    await Promise.all(SHELL.map(async (url) => {
      try { await cache.add(new Request(url, { cache: "reload" })); }
      catch (err) { /* non-fatal: the asset is simply not precached */ }
    }));
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter((k) => !k.startsWith(VERSION)).map((k) => caches.delete(k)));
    await self.clients.claim();
  })());
});

/* Let the page trigger an immediate update after a deploy. */
self.addEventListener("message", (event) => {
  if (event.data === "skip-waiting") self.skipWaiting();
});

function isApi(url) {
  return url.pathname.startsWith("/api/") || url.pathname.startsWith("/webhook");
}

async function networkFirst(request) {
  try {
    const fresh = await fetch(request);
    if (fresh && fresh.ok) {
      const cache = await caches.open(SHELL_CACHE);
      cache.put(request, fresh.clone());
    }
    return fresh;
  } catch (err) {
    const cached = await caches.match(request);
    if (cached) return cached;
    const shell = await caches.match("/");
    if (shell) return shell;
    return (await caches.match("/offline.html")) ||
           new Response("Offline", { status: 503, headers: { "content-type": "text/plain" } });
  }
}

async function staleWhileRevalidate(request) {
  const cache = await caches.open(ASSET_CACHE);
  const cached = await cache.match(request);
  const network = fetch(request).then((res) => {
    if (res && res.ok) cache.put(request, res.clone());
    return res;
  }).catch(() => null);
  return cached || (await network) || new Response("", { status: 504 });
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;                 // never cache writes

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;      // same-origin only

  if (isApi(url)) return;                               // straight to network

  if (request.mode === "navigate") {
    event.respondWith(networkFirst(request));
    return;
  }

  event.respondWith(staleWhileRevalidate(request));
});

/* --- Web push ----------------------------------------------------------
 * The backend already carries per-device push tokens (/api/native/push-token).
 * Wiring VAPID keys is a deployment step; the handlers below make the payload
 * a plain JSON envelope: {title, body, url, tag}.
 */
self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = {}; }
  const title = data.title || "Rebuy";
  event.waitUntil(self.registration.showNotification(title, {
    body: data.body || "You have a reminder to look at.",
    icon: "/icons/icon-192.png",
    badge: "/icons/icon-96.png",
    tag: data.tag || "rebuy",
    renotify: Boolean(data.tag),
    data: { url: data.url || "/#/reminders" },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/#/dashboard";
  event.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const client of all) {
      if (new URL(client.url).origin === self.location.origin) {
        await client.focus();
        if ("navigate" in client) client.navigate(target);
        return;
      }
    }
    await self.clients.openWindow(target);
  })());
});
