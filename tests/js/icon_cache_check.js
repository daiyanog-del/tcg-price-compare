// SWの更新でカード画像を保持し、旧アイコンだけを置き換えることを確認する。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const origin = 'https://tcgym.example';
const card = origin + '/api/card-image?name=test';
const otherImage = origin + '/static/og-image.png';
const oldIcon = origin + '/static/icon-192.png';
const oldVersion = origin + '/static/favicon.svg?v=3';
const entries = new Map([card, otherImage, oldIcon, oldVersion].map(url => [url, '既存']));
const listeners = {};
let claimed = false;
let notification;
const cache = {
  async addAll(urls) { for (const url of urls) entries.set(origin + url, '更新'); },
  async keys() { return [...entries.keys()].map(url => ({url})); },
  async delete(req) { return entries.delete(req.url); },
};
const context = {
  URL, Set, Promise,
  caches: {
    async keys() { return ['tcgym-v2']; },
    async open(name) { assert.equal(name, 'tcgym-v2'); return cache; },
    async delete() { assert.fail('カード画像を含むキャッシュ全体を削除した'); },
  },
  self: {
    location: {origin},
    addEventListener(name, fn) { listeners[name] = fn; },
    async skipWaiting() {},
    clients: {async claim() { claimed = true; }},
    registration: {async showNotification(title, options) { notification = options; }},
  },
};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../../static/sw.js'), 'utf8'), context);
async function dispatch(name) {
  let work;
  listeners[name]({waitUntil(promise) { work = promise; }});
  await work;
}
(async () => {
  await dispatch('install');
  await dispatch('activate');
  assert.equal(claimed, true);
  assert.equal(entries.get(card), '既存');
  assert.equal(entries.get(otherImage), '既存');
  assert.equal(entries.has(oldIcon), false);
  assert.equal(entries.has(oldVersion), false);
  assert.equal(entries.size, 10);
  await dispatch('push');
  assert.match(notification.icon, /icon-192\.png\?v=[a-f0-9]{12}$/);
  assert.match(notification.badge, /favicon-32\.png\?v=[a-f0-9]{12}$/);
  console.log('アイコン更新・カード画像保持・通知参照の検証成功');
})().catch(error => { console.error(error); process.exitCode = 1; });
