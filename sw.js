/**
 * NEON GRID — Service Worker
 * Handles:
 *   1. PWA caching (offline shell)
 *   2. Web Push notifications — shows OTP alert even when tab is CLOSED
 *
 * For push to work your backend must:
 *   - Generate VAPID keys:  npx web-push generate-vapid-keys
 *   - Send a push payload to the user's subscription when OTP arrives
 *   - Payload JSON: { otp: "123456", raw_message: "Your OTP is 123456" }
 */

const CACHE = 'neon-grid-v1';

// ── INSTALL: cache the app shell ─────────────────────────────────
self.addEventListener('install', function(e) {
  self.skipWaiting();
  e.waitUntil(
    caches.open(CACHE).then(function(c) {
      return c.addAll([
        '/',
        '/dashboard.html',   // adjust to your actual page path
      ]).catch(function() {
        // Fail silently — caching is best-effort
      });
    })
  );
});

// ── ACTIVATE ─────────────────────────────────────────────────────
self.addEventListener('activate', function(e) {
  e.waitUntil(
    caches.keys().then(function(keys) {
      return Promise.all(
        keys.filter(function(k) { return k !== CACHE; })
            .map(function(k)   { return caches.delete(k); })
      );
    }).then(function() { return self.clients.claim(); })
  );
});

// ── FETCH: serve from cache, fall back to network ─────────────────
self.addEventListener('fetch', function(e) {
  if (e.request.method !== 'GET') return;
  e.respondWith(
    caches.match(e.request).then(function(cached) {
      return cached || fetch(e.request);
    })
  );
});

// ── PUSH: fired by server even when tab is fully closed ───────────
self.addEventListener('push', function(e) {
  var data = {};
  try { data = e.data ? e.data.json() : {}; } catch(_) {}

  var otp     = data.otp     || '------';
  var rawMsg  = data.raw_message || ('Your OTP code is ' + otp);
  var title   = '🔑 OTP Received';

  var options = {
    body:     rawMsg.substring(0, 140),
    icon:     '/icon-192.png',
    badge:    '/icon-192.png',
    tag:      'otp-alert',       // replaces previous OTP notification
    renotify: true,
    vibrate:  [200, 100, 200, 100, 200],  // triple buzz
    sound:    '/notification.mp3',        // Android only — optional
    data:     { otp: otp, url: self.registration.scope },
    actions:  [
      { action: 'copy',    title: '📋 Copy OTP' },
      { action: 'dismiss', title: '✕ Dismiss'  },
    ],
  };

  e.waitUntil(
    self.registration.showNotification(title, options)
  );
});

// ── NOTIFICATION CLICK ────────────────────────────────────────────
self.addEventListener('notificationclick', function(e) {
  e.notification.close();

  if (e.action === 'dismiss') return;

  var targetUrl = (e.notification.data && e.notification.data.url) || '/';

  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true })
      .then(function(clients) {
        // If a tab is already open → focus it
        for (var i = 0; i < clients.length; i++) {
          var c = clients[i];
          if (c.url.includes(self.registration.scope) && 'focus' in c) {
            return c.focus();
          }
        }
        // Otherwise open a new tab
        if (self.clients.openWindow) {
          return self.clients.openWindow(targetUrl);
        }
      })
  );
});

// ── MESSAGE: tab → SW communication ──────────────────────────────
self.addEventListener('message', function(e) {
  if (e.data && e.data.type === 'SKIP_WAITING') self.skipWaiting();
});
