/* Kasflow service worker.
 *
 * Only the static shell is cached. Authenticated HTML pages and API responses
 * are never stored, so one account's data can never be served to another
 * account on a shared device. Navigations fall back to offline.html.
 */
const VERSION = 'kasflow-v5';
const SHELL = [
  'offline.html',
  'fonts.css',
  'fonts/jakarta-normal-latin.woff2',
  'fonts/inter-normal-latin.woff2',
  'app-styles.css',
  'app-shell.js',
  'icon.svg',
  'manifest.webmanifest',
  'login.html',
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches
      .open(VERSION)
      .then(cache => Promise.allSettled(SHELL.map(url => cache.add(url))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches
      .keys()
      .then(keys => Promise.all(keys.filter(key => key !== VERSION).map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/')) return;

  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(() =>
        caches
          .match('offline.html')
          .then(cached => cached || new Response('Je bent offline.', { status: 503 }))
      )
    );
    return;
  }

  // Network first: pages and app-shell.js must come from the same deploy, so a
  // cached copy is only used when the network is unavailable.
  event.respondWith(
    fetch(request)
      .then(response => {
        if (response.ok && response.type === 'basic') {
          const copy = response.clone();
          caches.open(VERSION).then(cache => cache.put(request, copy));
        }
        return response;
      })
      .catch(() => caches.match(request).then(cached => cached || Response.error()))
  );
});
