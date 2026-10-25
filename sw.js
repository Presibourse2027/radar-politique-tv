// Service worker : l'appli s'ouvre même sans réseau ; la grille est toujours demandée en ligne d'abord.
const VERSION = 'rpt-v7';
const COQUILLE = ['./', 'index.html', 'manifest.webmanifest', 'icon-192.png', 'icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(VERSION).then(c => c.addAll(COQUILLE)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== VERSION).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  // Données : réseau d'abord (l'appli garde aussi sa propre copie hors ligne)
  if (url.pathname.endsWith('programmes.json')) return;
  if (url.origin !== location.origin) return;
  // Pages et fichiers de l'appli : réseau d'abord, cache en secours (mises à jour visibles tout de suite)
  e.respondWith(
    fetch(req).then(r => {
      if (r.ok) { const copie = r.clone(); caches.open(VERSION).then(c => c.put(req, copie)); }
      return r;
    }).catch(() => caches.match(req).then(r => r || caches.match('index.html')))
  );
});
