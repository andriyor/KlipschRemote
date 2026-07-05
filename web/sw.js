/*
 * sw.js — service worker that makes the Klipsch web remote installable and
 * offline-capable. It precaches the app shell (HTML/JS/CSS/icons) and runtime-
 * caches the Google Fonts (Material Symbols + Roboto) so a second launch — and
 * an installed PWA launched with no network — renders fully, glyphs included.
 *
 * The BLE control itself never touches the network; this is purely about
 * loading the page assets without a round-trip.
 *
 * Strategy:
 *   - app shell (navigation + same-origin CSS/JS/icons) -> NETWORK-first, so the
 *     HTML and the code it loads always come from the same source and can never
 *     skew. (Fresh HTML paired with a stale cached stylesheet was the "squished
 *     first load after a deploy" bug.) Each fetch refreshes the cache; offline we
 *     fall back to it (to index.html for navigations) so the app still launches.
 *   - Google Fonts (CSS + woff)                         -> cache-first (runtime)
 *
 * IMPORTANT: bump VERSION whenever any shelled file (index.html, app.js,
 * klipsch.js, styles.css, an icon, or this list) changes — the old cache is
 * keyed by it and is dropped on activate.
 */
"use strict";

const VERSION = "v17";
const SHELL_CACHE = `klipsch-shell-${VERSION}`;
const RUNTIME_CACHE = `klipsch-runtime-${VERSION}`;

// Everything needed to boot the app with no network. Relative URLs so the SW
// works whether the app is served from a domain root or a sub-path.
const SHELL = [
  "./",
  "./index.html",
  "./app.js",
  "./klipsch.js",
  "./styles.css",
  "./manifest.json",
  "./icon.png",
  "./icon-192.png",
  "./icon-512.png",
  "./maskable-512.png",
];

const FONT_ORIGINS = ["https://fonts.googleapis.com", "https://fonts.gstatic.com"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(SHELL_CACHE)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((k) => k !== SHELL_CACHE && k !== RUNTIME_CACHE)
            .map((k) => caches.delete(k))
        )
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;
  const url = new URL(request.url);

  // Google Fonts — cache-first into the runtime cache (big, immutable, opaque OK).
  if (FONT_ORIGINS.includes(url.origin)) {
    event.respondWith(cacheFirst(request, RUNTIME_CACHE));
    return;
  }

  // App shell (the page itself + its same-origin CSS/JS/icons) — network-first,
  // so a deploy is picked up immediately AND the HTML never pairs with a stale
  // cached stylesheet/script (that skew was the "squished first load" bug). Each
  // fetch refreshes the cache; offline we fall back to it (to index.html for
  // navigations) so the app still launches with no network.
  if (request.mode === "navigate") {
    event.respondWith(networkFirst(request, SHELL_CACHE, "./index.html"));
    return;
  }
  if (url.origin === self.location.origin) {
    event.respondWith(networkFirst(request, SHELL_CACHE));
  }
});

async function networkFirst(request, cacheName, fallbackUrl) {
  try {
    const response = await fetch(request);
    if (response && response.ok) {
      const cache = await caches.open(cacheName);
      cache.put(request, response.clone());
    }
    return response;
  } catch (err) {
    const cached =
      (await caches.match(request)) ||
      (fallbackUrl ? await caches.match(fallbackUrl) : undefined);
    if (cached) return cached;
    throw err;
  }
}

async function cacheFirst(request, cacheName) {
  const cached = await caches.match(request);
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response && (response.ok || response.type === "opaque")) {
      const cache = await caches.open(cacheName);
      cache.put(request, response.clone());
    }
    return response;
  } catch (err) {
    // Offline and never cached — nothing we can do; let the request fail.
    return caches.match(request);
  }
}
