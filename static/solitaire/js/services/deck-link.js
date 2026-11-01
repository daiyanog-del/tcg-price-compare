/**
 * deck-link.js — シミュレーターのデッキと「マイデッキ」のひも付け・編集の差分（2026-10-20）
 *
 * 状態:
 *   _link   … いま並んでいるデッキの出どころ。{ savedId, name, source }
 *              source = 'sample'（サンプルデッキから読み込んだ）のときだけ値が入る。見出しと一覧の「使用中」に使う。
 *              savedId があればマイデッキのそのデッキにひも付いている（「変更を保存」で上書き）。
 *              無ければ未登録（新着・環境デッキ・持ち込み・共有リプレイ等。「マイデッキに登録」）。
 *              デッキが無いときは null。
 *   _deltas … 読み込んでから足した／抜いた枚数の差分。キー = "main|ex" + "\u0000" + カード名、値 = 増減。
 *
 * 保存は「盤面の写し」ではなく「保存済みデッキ（その時点の最新）＋差分」で行う。
 *   - 画像の取得に失敗して並ばなかったカードを、保存で消してしまわないため
 *   - 読み込んだ後に別の場所（トップのマイデッキ・別タブ・他端末同期）で変えられていても、
 *     その変更を消さずに今回の増減だけを重ねられるため（上書き確認を出さずに済む）
 *
 * 純粋な関数（serializeDeck / parseDeckSections / applyDeltas / collectDeckFromDom / pickRemovable）は
 * node のテスト（tests/js/deck_link_check.js）から直接読み込めるよう、DOM・storage を引数で受ける。
 */

const SEP = '\u0000';
/** デッキに数えるカードの置き場所（デッキ欄・EXデッキ欄・盤面（墓地・除外を含む）・手札）。
 *  body 直下に一時的に出る演出用の複製（card-effects.js）を数えないよう、置き場所で絞る */
export const BOARD_WRAPPERS = '#poolRow .tier-item-wrapper, #poolRow2 .tier-item-wrapper, .main-content .tier-item-wrapper, .center-slot .tier-item-wrapper';

/**
 * デッキの中身に数えないカードか: トークン（data-token）とダミーカード（data-dummy・2026-11-01 utils/dummy-card.js）。
 * どちらも盤面で出したもので、マイデッキの保存・枚数の増減・デッキ画像には入れない。
 * @param {Element} w - .tier-item-wrapper
 */
export function isNotDeckCard(w) {
  return w?.dataset?.token === '1' || w?.dataset?.dummy === '1';
}
let _link = null;
let _deltas = new Map();
const _listeners = new Set();

function _emit() {
  for (const fn of _listeners) {
    try { fn(getDeckLink()); } catch (e) { console.warn('[deck-link] 通知先でエラー', e); }
  }
}

/** ひも付けの変化（読込・編集・保存）を受け取る。戻り値で解除。 */
export function onDeckLinkChange(fn) {
  _listeners.add(fn);
  return () => _listeners.delete(fn);
}

/** いまの状態。デッキが無ければ null。dirty = 保存していない増減があるか */
export function getDeckLink() {
  if (!_link) return null;
  return { savedId: _link.savedId, name: _link.name, source: _link.source, dirty: isDirty() };
}

/**
 * デッキを読み込んだ（入れ替えた）ときに呼ぶ。差分は空に戻す。
 * @param {{savedId?:string|null, name?:string, source?:'sample'|null}} link
 */
export function setDeckLink({ savedId = null, name = '', source = null } = {}) {
  _link = { savedId: savedId || null, name: String(name || '').trim() || 'デッキ', source: source || null };
  _deltas = new Map();
  _emit();
}

/** デッキが無くなった（すべて消去）ときに呼ぶ */
export function clearDeckLink() {
  _link = null;
  _deltas = new Map();
  _emit();
}

/**
 * 足した／抜いたを記録する。デッキがあるのにひも付けが無い（共有リプレイを開いた等）ときは未登録として始める。
 * @param {string} name
 * @param {'main'|'ex'} sec
 * @param {number} n  +1 / -1
 */
export function recordDelta(name, sec, n) {
  if (!_link) _link = { savedId: null, name: 'デッキ', source: null };
  const key = sec + SEP + name;
  const v = (_deltas.get(key) || 0) + n;
  if (v === 0) _deltas.delete(key); else _deltas.set(key, v);
  _emit();
}

export function isDirty() {
  return _deltas.size > 0;
}

/** 差分の写し（保存・取り消し・退避用） */
export function getDeltas() {
  return [..._deltas].map(([key, n]) => {
    const i = key.indexOf(SEP);
    return { sec: key.slice(0, i), name: key.slice(i + 1), n };
  });
}

/**
 * 保存が済んだときに呼ぶ。ひも付け先を更新して差分を空にする。
 * @param {{savedId:string, name:string}} link
 */
export function markSaved({ savedId, name }) {
  _link = { savedId, name: String(name || '').trim() || 'デッキ', source: null };
  _deltas = new Map();
  _emit();
}

/** 保存の取り消し用。保存前のひも付けと差分に戻す。 */
export function restoreLinkState(snap) {
  if (!snap) return;
  _link = snap.link ? { ...snap.link } : null;
  _deltas = new Map((snap.deltas || []).map(d => [d.sec + SEP + d.name, d.n]));
  _emit();
}

/** ページを離れるときの退避用（session-resume） */
export function captureLinkState() {
  if (!_link) return null;
  return { link: { ..._link }, deltas: getDeltas() };
}

// ── 純粋な関数 ────────────────────────────────────────────

/**
 * {main,ex} をマイデッキのテキストへ。deck-edit.js（トップ）の _serializeDeck と同じ形式
 * （1枚は名前だけ・2枚以上は「N 名前」・EX があれば [EX] 区切り）。トップの読込と往復できる。
 */
export function serializeDeck(deck) {
  const line = c => (c.qty > 1 ? c.qty + ' ' + c.name : c.name);
  const mainLines = (deck.main || []).map(line);
  const exLines = (deck.ex || []).map(line);
  let out = mainLines.join('\n');
  if (exLines.length) out += (out ? '\n' : '') + '[EX]\n' + exLines.join('\n');
  return out;
}

/**
 * マイデッキのテキストを {main,ex} へ。[EX] 行より後を EX とする（トップの parseDeckSections と同じ解釈）。
 * 同じ名前が複数行にあれば枚数を足す。
 */
export function parseDeckSections(text) {
  const out = { main: [], ex: [] };
  let sec = 'main';
  for (const raw of String(text || '').replace(/\r/g, '').split('\n')) {
    const line = raw.trim();
    if (!line) continue;
    if (line === '[EX]') { sec = 'ex'; continue; }
    const m = line.match(/^(\d+)\s+(.+)$/);
    const qty = m ? parseInt(m[1], 10) : 1;
    const name = (m ? m[2] : line).trim();
    if (!name || !(qty > 0)) continue;
    const hit = out[sec].find(c => c.name === name);
    if (hit) hit.qty += qty; else out[sec].push({ qty, name });
  }
  return out;
}

/**
 * 保存済みデッキに差分を重ねた新しい {main,ex} を返す（元は変えない）。
 * 既にある行は並びを保って枚数だけ変え、新しいカードは末尾に足す。0枚以下になった行は消す。
 * 抜く側のカードが見つからない（別の場所で既に消された）ときは何もしない。
 */
export function applyDeltas(deck, deltas) {
  const out = {
    main: (deck.main || []).map(c => ({ qty: c.qty, name: c.name })),
    ex: (deck.ex || []).map(c => ({ qty: c.qty, name: c.name })),
  };
  for (const { sec, name, n } of deltas) {
    const s = sec === 'ex' ? 'ex' : 'main';
    // 同じカードが反対側（メイン⇔EX）に入っている保存データもあるので、まずそちらを探す
    // 足すときも同じ: [EX] 無しで貼り付けたデッキなど、EX のカードがメイン側にある保存データでは
    // そちらの行を増やす（同じカードがメインと EX に分かれて保存されないように）
    const other = s === 'ex' ? 'main' : 'ex';
    let arr = out[s];
    let hit = arr.find(c => c.name === name);
    if (!hit) {
      const alt = out[other].find(c => c.name === name);
      if (alt) { arr = out[other]; hit = alt; }
    }
    if (hit) {
      hit.qty += n;
      if (hit.qty <= 0) arr.splice(arr.indexOf(hit), 1);
    } else if (n > 0) {
      out[s].push({ qty: n, name });
    }
  }
  return out;
}

/**
 * 保存済みデッキの中身（{main,ex}）。テキストを正とし、テキストが空なら main/ex 配列を使う
 * （トップの normalizeDeck と同じ規則。配列だけの古いデータで「変更を保存」すると中身が消えるのを防ぐ）。
 */
export function deckContents(deck) {
  if (String(deck?.text || '').trim()) return parseDeckSections(deck.text);
  const norm = arr => (Array.isArray(arr) ? arr : [])
    .filter(c => c && typeof c.name === 'string' && c.name.trim() && Number(c.qty) > 0)
    .map(c => ({ qty: Number(c.qty), name: c.name.trim() }));
  return { main: norm(deck?.main), ex: norm(deck?.ex) };
}

/**
 * 盤面全体（デッキ・EX・場・手札・墓地・除外）からデッキの中身を数える。トークンは数えない。
 * EX かどうかはカードの id（"item-NNN ex"）で見る（場に出ても変わらない）。
 * @param {ParentNode} root
 * @returns {{main:Array<{qty,name}>, ex:Array<{qty,name}>}}
 */
export function collectDeckFromDom(root) {
  const out = { main: [], ex: [] };
  for (const w of root.querySelectorAll(BOARD_WRAPPERS)) {
    if (isNotDeckCard(w)) continue;
    const name = w.querySelector('.tier-item')?.dataset.cardName;
    if (!name) continue;
    const sec = / ex$/.test(w.id || '') ? 'ex' : 'main';
    const hit = out[sec].find(c => c.name === name);
    if (hit) hit.qty++; else out[sec].push({ qty: 1, name });
  }
  return out;
}

/**
 * 抜いてよいカードを1枚選ぶ。デッキ欄（#poolRow / #poolRow2）にあって、リプレイ記録に出てこないもの。
 * 記録に出てくるカードを抜くと、巻き戻したときに相手のいない操作が残って再生が壊れるため。
 * @param {Element[]} candidates  同じ名前の .tier-item-wrapper（優先したい順）
 * @param {Set<string>} loggedIds 記録に出てくるカードID
 * @returns {Element|null}
 */
export function pickRemovable(candidates, loggedIds) {
  for (const w of candidates) {
    const pid = w.parentElement?.id;
    if (pid !== 'poolRow' && pid !== 'poolRow2') continue;
    if (isNotDeckCard(w)) continue;
    const id = w.querySelector('.tier-item')?.id;
    if (!id || loggedIds.has(id)) continue;
    return w;
  }
  return null;
}
