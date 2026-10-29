/**
 * replay-image-policy.js
 * リプレイの画像辞書（{cardId: src}）に入っている値のうち、画面に出してよいものを決める（副作用なし・他を import しない）。
 *
 * 画像辞書は共有リンク・URLハッシュ・JSONファイルから読む、利用者が送った値なので、そのまま <img src> に入れない。
 * 2026-10-29 まで任意の外部URL・任意の data: 画像を共有リンクやみんなのリプレイで他人に見せられた（監査の指摘）。
 * URLハッシュ（#replay=）と JSON ファイルはサーバーを通らないので、ここが唯一の守りになる。
 * 通すのは次の3つだけ:
 *   - card://カード名     … 開くときに /api/card-images で名前から引き直す（このサービスが返す画像だけが出る）
 *   - data:（トークン）   … 中身は使わず、名前から描き直す（replay-service.js が utils/token-image.js で描く）
 *   - 下の発行元・パスの形の https URL … /api/card-images が発売済みカードに返す画像（既存のリプレイはこの形で入っている）
 * それ以外の値は card://カード名 に置き換える（名前が無ければ card:// のまま＝名前だけの札になる）。
 *
 * ALLOWED_IMAGE_SOURCES はサーバーの constants.py REPLAY_IMAGE_SOURCES と同じ値にする
 * （tests/test_replay_image_policy.py が両方の一致を確かめる）。
 */

import { isTokenSrc } from './token-src.js';

// [発行元, パスの形]。発行元だけで許すと、その発行元にある転送用の URL などを通してしまうため、出している形に絞る
export const ALLOWED_IMAGE_SOURCES = [
  ['https://artworks-jp-n.ygoresources.com', String.raw`^/\d+/\d+/\d+_1\.png$`],   // app.py _ygores_image_url（発売済みカード）
  ['https://www.ka-nabell.com', String.raw`^/img/s/[A-Za-z0-9_.-]+$`],            // scraper.py kanabell_card_image_url（予備）
];
const _SOURCES = ALLOWED_IMAGE_SOURCES.map(([origin, pat]) => [origin, new RegExp(pat)]);

/** 発売済みカードの画像として通してよい URL か（https・クエリ・#・認証情報つきは通さない） */
export function isAllowedImageUrl(src) {
  if (typeof src !== 'string') return false;
  let url;
  try { url = new URL(src); } catch { return false; }
  if (url.protocol !== 'https:' || url.search || url.hash || url.username || url.password) return false;
  return _SOURCES.some(([origin, re]) => url.origin === origin && re.test(url.pathname));
}

/**
 * 画像辞書の1件を、画面に出してよい形に分ける。
 * @param {*} src   辞書の値
 * @param {string} [name]  そのカードの名前（names の値）
 * @returns {{kind:'card'|'token'|'url', src:string}}
 *   kind='token' の src は元の値のまま（呼び出し側が名前から描き直す）
 */
export function classifyReplayImage(src, name) {
  if (typeof src === 'string') {
    if (src.startsWith('card://')) return { kind: 'card', src };
    if (isTokenSrc(src)) return { kind: 'token', src };
    if (isAllowedImageUrl(src)) return { kind: 'url', src };
  }
  const n = typeof name === 'string' ? name.trim() : '';
  return { kind: 'card', src: `card://${n}` };
}
