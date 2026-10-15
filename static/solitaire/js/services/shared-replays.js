/**
 * shared-replays.js
 * 共有リンクを作った記録の履歴（localStorage）
 *
 * 共有リンク（Supabase 保存・90日で自動削除）を後から探せるようにするための、このブラウザ内だけの控え。
 * saved-replays.js と同じ作法（キー1本にJSON配列を保持・読み書きは try/catch）。
 * 項目は {id, title, createdAt, steps}。新しい順で最大 MAX_SHARED_REPLAYS 件。
 * 「みんなのリプレイに公開」した項目には public:true と deleteToken（取り下げの鍵）も持つ。
 * 鍵はこのブラウザにしか無い（サーバーにはハッシュだけ）。取り下げたら unpublishSharedReplay で外す。
 * 履歴の記録に失敗しても共有そのものは失敗させない（呼び出し側は戻り値を無視してよい）。
 */

const SHARED_REPLAYS_KEY = 'sol-shared-replays';
// 暫定値（履歴は id+題名+日時+手数だけで軽いので多めに持つ）。TODO: calibrate from data
const MAX_SHARED_REPLAYS = 50;

/**
 * 共有履歴を取得する（新しい順に保存されている）。
 * @returns {Array<{id:string, title:string, createdAt:number, steps:number}>}
 */
export function sharedReplaysGet() {
  try {
    const list = JSON.parse(localStorage.getItem(SHARED_REPLAYS_KEY));
    // 壊れた中身（配列でない・null 等の要素）で一覧や保存が落ちないようにする
    return Array.isArray(list) ? list.filter(r => r && typeof r === 'object') : [];
  } catch {
    return [];
  }
}

/**
 * 共有履歴を保存する。
 * @param {Array} list
 * @returns {{ok:boolean}}
 */
export function sharedReplaysSet(list) {
  try {
    localStorage.setItem(SHARED_REPLAYS_KEY, JSON.stringify(list));
    return { ok: true };
  } catch (e) {
    console.warn('[shared-replays] localStorage への保存に失敗しました:', e);
    return { ok: false };
  }
}

/**
 * 共有リンクを作った記録を1件、先頭に追加する。同じ id が既にあれば置き換える。
 * 上限を超えた分は古いものから落とす。
 * @param {{id:string, title:string, steps:number, public?:boolean, deleteToken?:string}} entry
 * @returns {{ok:boolean}}
 */
export function addSharedReplay(entry) {
  if (!entry || !entry.id) return { ok: false };
  const item = {
    id: String(entry.id),
    title: String(entry.title || ''),
    createdAt: Date.now(),
    steps: Number.isFinite(entry.steps) ? entry.steps : 0,
  };
  if (entry.public === true && entry.deleteToken) {
    item.public = true;
    item.deleteToken = String(entry.deleteToken);
  }
  const list = [item, ...sharedReplaysGet().filter(r => r.id !== item.id)].slice(0, MAX_SHARED_REPLAYS);
  return sharedReplaysSet(list);
}

/**
 * 公開を取り下げた項目から public と deleteToken を外す（履歴の項目自体は残す）。
 * @param {string} id
 * @returns {Array} 更新後の一覧
 */
export function unpublishSharedReplay(id) {
  const list = sharedReplaysGet().map((r) => {
    if (r.id !== id) return r;
    const { public: _p, deleteToken: _t, ...rest } = r;
    return rest;
  });
  sharedReplaysSet(list);
  return list;
}

/**
 * 共有履歴から1件消す（共有リンク自体は消えない）。
 * @param {string} id
 * @returns {Array} 消した後の一覧
 */
export function deleteSharedReplay(id) {
  const list = sharedReplaysGet().filter(r => r.id !== id);
  sharedReplaysSet(list);
  return list;
}
