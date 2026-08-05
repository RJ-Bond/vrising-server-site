const CACHE_NAME = 'vrising-v6';
const STATIC_ASSETS = [
  '/', '/index.html', '/servers.html', '/profile.html', '/events.html',
  '/offline.html', '/manifest.json', '/common.js',
  '/tailwind.min.css', '/icon-vrising.png',
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE_NAME).then(c => c.addAll(STATIC_ASSETS.filter(Boolean))).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // Images (incl. CSS background-image): NEVER intercept. Let the browser
  // fetch them natively with proper no-cors/Range/cache handling. Proxying an
  // image request through fetch() inside the SW breaks the response body, so
  // thumbnails failed to render on normal reload (F5) while hard reload worked.
  if (e.request.destination === 'image') {
    return;
  }

  // Never cache API calls — always network
  if (url.pathname.startsWith('/api/')) {
    e.respondWith(fetch(e.request).catch(() => new Response('{"error":"offline"}', {headers:{'Content-Type':'application/json'}})));
    return;
  }

  // For HTML pages: network-first, fallback to cache, then offline page
  if (e.request.mode === 'navigate') {
    e.respondWith(
      fetch(e.request)
        .then(r => { caches.open(CACHE_NAME).then(c => c.put(e.request, r.clone())); return r; })
        .catch(() => caches.match(e.request).then(r => r || caches.match('/offline.html')))
    );
    return;
  }

  // Static assets: cache-first
  e.respondWith(
    caches.match(e.request).then(r => r || fetch(e.request).then(resp => {
      if (resp.ok) caches.open(CACHE_NAME).then(c => c.put(e.request, resp.clone()));
      return resp;
    }))
  );
});

// ── Web Push ──────────────────────────────────────────────────────────────
// Payload shape is JSON `{title, body, url}`, written by send_push() in
// backend/helpers.py (see POST /api/push/subscribe in
// backend/routers/notifications.py for how a subscription is registered).
self.addEventListener('push', e => {
  let data;
  try { data = e.data ? e.data.json() : {}; } catch { data = {}; }
  const title = data.title || 'V Rising';
  const options = {
    body: data.body || '',
    icon: '/icon-vrising.png',
    badge: '/icon-vrising.png',
    data: { url: data.url || '/' },
  };
  e.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clientList => {
      const targetUrl = new URL(url, self.location.origin);
      for (const client of clientList) {
        const clientUrl = new URL(client.url);
        if (clientUrl.origin === targetUrl.origin && 'focus' in client) {
          if ('navigate' in client) client.navigate(targetUrl.href).catch(() => {});
          return client.focus();
        }
      }
      if (self.clients.openWindow) return self.clients.openWindow(targetUrl.href);
    })
  );
});
