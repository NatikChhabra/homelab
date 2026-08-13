/**
 * Service worker for the JARVIS PWA shell.
 *
 * Caches the shell assets so the page is loadable offline.
 * All API calls (WebSocket, /api/*) go through the network — we never cache
 * live data. The precache list is tiny: HTML + JS + icon.
 */
'use strict';

const CACHE = 'jarvis-pwa-v1';
const ASSETS = [
  '/shell/pwa/',
  '/shell/pwa/index.html',
  '/shell/pwa/pwa.js',
  '/shell/pwa/manifest.json',
  '/shell/pwa/icon-192.png',
  '/shell/pwa/icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(ASSETS)));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  // Network-first for everything; fall back to cache for shell assets.
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});
