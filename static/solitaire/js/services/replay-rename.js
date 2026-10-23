/**
 * replay-rename.js
 * リプレイの名前を変える処理（リプレイ一覧ページとシミュレーターで共通。DOM には触らない）。
 *
 * 名前を変えられるのは、このブラウザが持ち主と分かるものだけ:
 *   - 端末に保存したリプレイ（saved-replays.js）… ブラウザ内の title を書き換える（100文字まで）
 *   - 公開中のリプレイ（shared-replays.js の public と deleteToken を持つ項目）… 公開時の鍵で
 *     POST /api/solitaire/replay/<id>/rename。条件はサーバーの公開の条件と同じ（40文字まで・禁止語など）
 * 非公開の共有リンクは鍵が無いので変えられない。
 */

import { savedReplaysGet, renameReplayEntry } from './saved-replays.js';
import { sharedReplaysGet, renameSharedReplay, unpublishSharedReplay } from './shared-replays.js';

// 端末保存はシミュレーターの保存と同じ100文字（replay-ui.js REPLAY_TITLE_MAX_LEN）、
// 公開中はサーバーの公開の条件と同じ40文字（solitaire_routes.PUBLIC_TITLE_MAX_LEN）
export const SAVED_TITLE_MAX_LEN = 100;
export const PUBLIC_TITLE_MAX_LEN = 40;

/**
 * 名前を変えられる相手を探す。
 * @param {{savedReplayId?:string|null, replayId?:string|null}} ids
 *   savedReplayId … 端末保存の id（?saved=）／ replayId … 共有リンクの id（?replay=）
 * @returns {{kind:'saved'|'public', id:string, title:string, token?:string}|null}
 */
export function findRenamableReplay({ savedReplayId, replayId } = {}) {
  if (savedReplayId) {
    const item = savedReplaysGet().find(r => r.id === savedReplayId);
    return item ? { kind: 'saved', id: item.id, title: item.title || '' } : null;
  }
  if (replayId) {
    const item = sharedReplaysGet().find(r => r.id === replayId && r.public && r.deleteToken);
    return item ? { kind: 'public', id: item.id, title: item.title || '', token: item.deleteToken } : null;
  }
  return null;
}

/** 名前の上限（相手の種類で違う） */
export function renameMaxLength(target) {
  return target && target.kind === 'public' ? PUBLIC_TITLE_MAX_LEN : SAVED_TITLE_MAX_LEN;
}

/**
 * 名前を変える。
 * @param {{kind:'saved'|'public', id:string, token?:string}} target findRenamableReplay の戻り値
 * @param {string} name
 * @returns {Promise<{ok:true, title:string} | {ok:false, message:string, gone?:boolean}>}
 *   gone: 公開中でなくなっていた（鍵が合わない・取り下げ済み・運営の削除）。控えの鍵は外した
 */
export async function renameReplay(target, name) {
  if (target.kind === 'saved') {
    if (!renameReplayEntry(target.id, name).ok) {
      return { ok: false, message: '変えられませんでした（ブラウザの保存容量を確認してください）' };
    }
    const item = savedReplaysGet().find(r => r.id === target.id);
    return { ok: true, title: item ? item.title : name };
  }
  let res, data;
  try {
    res = await fetch('/api/solitaire/replay/' + encodeURIComponent(target.id) + '/rename', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: target.token, title: name }),
    });
    data = await res.json().catch(() => ({}));
  } catch {
    return { ok: false, message: '通信に失敗しました。しばらくしてからもう一度お試しください' };
  }
  if (res.ok) {
    const title = typeof data.title === 'string' ? data.title : name;
    renameSharedReplay(target.id, title);
    return { ok: true, title };
  }
  // 鍵を捨てるのは、サーバーが「名前を変えられない（鍵違い・公開でない）」と目印を付けたときだけ（取り下げと同じ）
  if (res.status === 403 && data.code === 'not_renamable') {
    unpublishSharedReplay(target.id);
    return { ok: false, gone: true, message: 'このリプレイはすでに公開されていないようです' };
  }
  return { ok: false, message: data.error || '名前を変えられませんでした。しばらくしてからもう一度お試しください' };
}
