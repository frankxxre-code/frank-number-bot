// NEON GRID NETWORK — Service Worker v1
const CACHE = 'neongrid-v1';
self.addEventListener('install', function(e) {
  e.waitUntil(caches.open(CACHE).then(function(c) { return c.addAll(['/']); }));
  self.skipWaiting();
});
self.addEventListener('activate', function(e) {
  e.waitUntil(caches.keys().then(function(keys) {
    return Promise.all(keys.filter(function(k) { return k !== CACHE; }).map(function(k) { return caches.delete(k); }));
  }));
  self.clients.claim();
});
self.addEventListener('fetch', function(e) {
  var url = new URL(e.request.url);
  var isAPI = url.pathname.startsWith('/api') || url.pathname.startsWith('/ws');
  if (isAPI || e.request.method !== 'GET') { e.respondWith(fetch(e.request)); return; }
  if (e.request.mode === 'navigate') {
    e.respondWith(fetch(e.request).catch(function() { return caches.match('/'); }));
    return;
  }
  e.respondWith(fetch(e.request).catch(function() { return caches.match(e.request); }));
});
