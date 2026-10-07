/* Pool & Spa service worker: exists only so the app is installable.
 * Deliberately caches nothing -- the page shows live pool state, and a stale
 * cached page or API response would show the wrong thing. Served at /sw.js
 * (scope "/"). */
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
/* Chrome's installability check wants a fetch handler. Not calling
 * respondWith() lets every request go to the network exactly as without a SW. */
self.addEventListener("fetch", () => {});
