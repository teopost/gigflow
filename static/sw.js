// Service worker per Palcoscenici: mette in cache la "shell" statica
// dell'app (HTML/manifest/icone) per superare i requisiti di installabilità
// di Chrome/Android e per avere un fallback quando il server non è
// raggiungibile. Le chiamate a /api/ non vengono MAI messe in cache: i dati
// del CRM devono sempre arrivare live dal server.
//
// L'app cambia spesso in questa fase, quindi il documento HTML principale
// usa una strategia "network-first": se il server risponde si vede sempre
// l'ultima versione; la cache serve solo come fallback quando sei offline.

const CACHE_NAME = "palcoscenici-shell-v23";
const SHELL_ASSETS = [
  "/",
  "/manifest.json?v=2",
  "/icons/icon-192.png?v=2",
  "/icons/icon-512.png?v=2",
  "/icons/apple-touch-icon.png?v=2",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(SHELL_ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

function isHtmlDocument(req, url) {
  return req.mode === "navigate" || url.pathname === "/" || url.pathname === "/index.html";
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  if (url.pathname.startsWith("/api/")) return; // dati live, mai dalla cache

  if (isHtmlDocument(req, url)) {
    // Network-first: mentre l'app è in sviluppo attivo, mostra sempre
    // l'ultima versione quando il server risponde. La cache è solo il
    // paracadute per quando sei offline.
    event.respondWith(
      fetch(req)
        .then((res) => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(req, copy));
          }
          return res;
        })
        .catch(() => caches.match(req))
    );
    return;
  }

  // Altri asset statici (icone, manifest): stale-while-revalidate va bene,
  // cambiano di rado.
  event.respondWith(
    caches.match(req).then((cached) => {
      const network = fetch(req)
        .then((res) => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(req, copy));
          }
          return res;
        })
        .catch(() => cached);
      return cached || network;
    })
  );
});
