/* Cape Surf Check service worker: lets the page open with the last forecast when there is no signal.
   Same-site files: network first, cache as a fallback, so updates show up as soon as you are online.
   Fonts and libraries: cache first. Forecast APIs and map tiles: always the network.
   Every cache step is optional: if the browser refuses storage, the page simply works online only. */
const VERSION = 'csc-__VERSION__';
const SHELL = ['./', 'index.html', 'manifest.webmanifest', 'assets/icon-192.png'];
const LIBS = ['cdnjs.cloudflare.com', 'fonts.googleapis.com', 'fonts.gstatic.com'];

function remember(req, res) {
  if (!res || !(res.ok || res.type === 'opaque')) return;
  const copy = res.clone();
  caches.open(VERSION).then(c => c.put(req, copy)).catch(() => {});
}

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(VERSION).then(c => c.addAll(SHELL)).catch(() => {}).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k.startsWith('csc-') && k !== VERSION).map(k => caches.delete(k))))
      .catch(() => {})
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  if (url.origin === self.location.origin) {
    event.respondWith(
      fetch(req)
        .then(res => { remember(req, res); return res; })
        .catch(() => caches.match(req, { ignoreSearch: true })
          .then(hit => hit || (req.mode === 'navigate' ? caches.match('./') : undefined))
          .catch(() => undefined)
          .then(hit => hit || Response.error()))
    );
    return;
  }

  if (LIBS.includes(url.hostname)) {
    event.respondWith(
      caches.match(req).catch(() => undefined)
        .then(hit => hit || fetch(req).then(res => { remember(req, res); return res; }))
    );
  }
});
