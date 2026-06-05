const CACHE_NAME = 'tcgym-v1';
const PRECACHE = [
  '/',
  '/static/favicon.svg',
  '/static/favicon-32.png',
  '/static/apple-touch-icon.png',
  '/static/icon-192.png',
  '/static/icon-512.png',
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

// プッシュ通知を受け取って表示する
self.addEventListener('push', e => {
  let data = {title: '値下がり情報', body: '購入候補カードの相場が下がりました', url: '/?tab=wishlist'};
  try { if (e.data) data = {...data, ...e.data.json()}; } catch {}
  e.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: '/static/icon-192.png',
      badge: '/static/favicon-32.png',
      data: {url: data.url},
      tag: 'price-drop',    // 同じタグの通知は上書きされる（スタック防止）
    })
  );
});

// 通知クリックでサイトを開く
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const target = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil(
    clients.matchAll({type: 'window', includeUncontrolled: true}).then(list => {
      for (const c of list) {
        if (c.url.includes(self.location.origin)) { c.focus(); return; }
      }
      return clients.openWindow(target);
    })
  );
});

// ネットワーク優先、失敗時にキャッシュを返す（価格データは常に最新を取得）
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // 画像URL解決APIはキャッシュファースト（カード名→URLの対応はほぼ不変）
  // GETリクエストのみ対象（POSTバッチはCache APIがキャッシュしないためHTTPヘッダに委ねる）
  if (
    e.request.method === 'GET' &&
    (url.pathname === '/api/card-image' || url.pathname === '/api/card-image-proxy')
  ) {
    e.respondWith(
      caches.open(CACHE_NAME).then(async cache => {
        const hit = await cache.match(e.request);
        if (hit) return hit;
        const res = await fetch(e.request);
        if (res.ok) cache.put(e.request, res.clone());
        return res;
      })
    );
    return;
  }

  // 価格系・その他APIはキャッシュしない（常に最新を取得）
  if (url.pathname.startsWith('/api/')) return;

  // ナビゲーション（HTMLページ遷移）はSWキャッシュ対象外 → ブラウザHTTPキャッシュに委ねる
  // JS/CSS/HTMLもキャッシュ対象外とし、旧版コードがPWAで固着するのを防ぐ
  if (
    e.request.mode === 'navigate' ||
    /\.(js|css|html)(\?|$)/.test(url.pathname)
  ) return;

  // アイコン・画像など静的アセットはキャッシュして高速化
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
