const CACHE_NAME = 'cardprice-v1';
const PRECACHE = [
  '/',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/logo_icon.png',
  '/static/logo_text.png',
  '/static/favicon.png',
];

// インストール時に基本ファイルをキャッシュ
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(PRECACHE))
      .then(() => self.skipWaiting())
  );
});

// 古いキャッシュを削除
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// ネットワーク優先、失敗時にキャッシュを返す（価格データは常に最新を取得）
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // API・検索リクエストはキャッシュしない
  if (url.pathname.startsWith('/api/')) return;

  e.respondWith(
    fetch(e.request)
      .then(res => {
        // 成功したレスポンスをキャッシュに保存
        if (res.ok) {
          const clone = res.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(e.request, clone));
        }
        return res;
      })
      .catch(() => caches.match(e.request))
  );
});
