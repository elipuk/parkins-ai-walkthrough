'use strict';

// Bump CACHE_NAME when deploying breaking SPA changes — activate will delete old caches.
const CACHE_NAME = 'walkthrough-v3';

// ── Install: cache the SPA shell ───────────────────────────────────────────
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.add('/'))
  );
  self.skipWaiting();
});

// ── Activate: drop stale caches, claim clients ─────────────────────────────
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

// ── Fetch: route by content type ───────────────────────────────────────────
//
// Text/JSON API (manifest, panel JSON, articles): network-first.
//   → Live walkthrough additions (NHM etc.) are immediately visible when online.
//   → Falls back to cache only when truly offline.
//
// Binary media (images, audio) and SPA shell: stale-while-revalidate.
//   → Served instantly from cache; network revalidates in background.
//   → Binary assets at a given URL are immutable once uploaded to R2,
//     so stale content is never wrong — just potentially an old version of the shell.
//
self.addEventListener('fetch', event => {
  const { request } = event;
  const url = new URL(request.url);

  if (url.origin !== self.location.origin || request.method !== 'GET') return;

  // Navigation (typing a URL / link click): serve SPA shell
  if (request.mode === 'navigate') {
    event.respondWith(staleWhileRevalidate(new Request('/')));
    return;
  }

  const p = url.pathname;
  const isTextApi = p.startsWith('/api/') &&
    !p.includes('/asset/') &&
    !p.endsWith('/music') &&
    !p.endsWith('/cover');

  event.respondWith(isTextApi ? networkFirst(request) : staleWhileRevalidate(request));
});

async function networkFirst(request) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const res = await fetch(request);
    if (res.ok) cache.put(request, res.clone());
    return res;
  } catch {
    const cached = await cache.match(request, { ignoreVary: true });
    return cached || new Response(JSON.stringify({ error: 'offline' }), {
      status: 503, headers: { 'Content-Type': 'application/json' }
    });
  }
}

async function staleWhileRevalidate(request) {
  const cache = await caches.open(CACHE_NAME);
  const cached = await cache.match(request, { ignoreVary: true });

  const fetchAndUpdate = fetch(request).then(res => {
    if (res && res.ok) cache.put(request, res.clone());
    return res;
  });

  if (cached) {
    fetchAndUpdate.catch(() => {});
    return cached;
  }

  const res = await fetchAndUpdate.catch(() => null);
  if (res) return res;
  return new Response('Offline — this content has not been saved for offline use.', {
    status: 503, headers: { 'Content-Type': 'text/plain' }
  });
}

// ── Pre-cache on demand ("Save offline" button) ────────────────────────────
self.addEventListener('message', event => {
  if (!event.data || event.data.type !== 'PRECACHE_WALKTHROUGH') return;
  precacheWalkthrough(event.data.walkthroughId, event.data.manifest, event.source.id);
});

async function precacheWalkthrough(id, manifest, clientId) {
  const cache = await caches.open(CACHE_NAME);
  const panels = manifest.panels || [];
  let done = 0;
  const total = panels.length;

  async function notify(complete) {
    const client = await self.clients.get(clientId);
    if (!client) return;
    const msg = { type: 'PRECACHE_PROGRESS', done, total };
    if (complete) msg.complete = true;
    client.postMessage(msg);
  }

  async function tryCache(url) {
    const res = await fetch(url);
    if (res.ok) await cache.put(url, res);
  }

  // Manifest, cover, music
  try { await tryCache(`/api/walkthroughs/${id}`); } catch {}
  try { await tryCache(`/api/walkthroughs/${id}/cover`); } catch {}
  if (manifest.music) {
    try { await tryCache(`/api/walkthroughs/${id}/music`); } catch {}
  }

  for (const panel of panels) {
    try {
      let panelData = panel;
      const panelUrl = `/api/walkthroughs/${id}/panels/${panel.id}`;
      const panelRes = await fetch(panelUrl);
      if (panelRes.ok) {
        // Clone before reading body — cache gets the clone, we read the original
        await cache.put(panelUrl, panelRes.clone());
        try { panelData = await panelRes.json(); } catch {}
      }

      try { await tryCache(`/api/walkthroughs/${id}/article/${panel.id}`); } catch {}

      if (panelData.image)
        try { await tryCache(`/api/walkthroughs/${id}/asset/${panel.id}/${panelData.image}`); } catch {}

      if (panelData.narration)
        try { await tryCache(`/api/walkthroughs/${id}/asset/${panel.id}/${panelData.narration}`); } catch {}
    } catch {}

    done++;
    await notify(false);
  }

  await notify(true);
}
