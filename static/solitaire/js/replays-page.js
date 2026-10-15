/**
 * replays-page.js
 * リプレイ一覧ページ（/solitaire/replays）の描画。
 *
 * このブラウザ（localStorage）にあるリプレイだけを扱う。
 *   1. 端末に保存したリプレイ（services/saved-replays.js）
 *   2. 共有リンクを作ったリプレイの履歴（services/shared-replays.js）
 * シミュレーター本体（main.js 等）は読み込まない。
 * 起動時に、廃止した盤面セーブ（スロット）の移行（services/slot-migration.js）と、
 * 「ファイルから開く」（書き出した JSON を端末保存へ登録してシミュレーターで開く）も受け持つ。
 * ユーザー由来の文字列（タイトル等）は必ず textContent で入れる（innerHTML には入れない）。
 */

import { savedReplaysGet, deleteReplayEntry, assertReplayPayload, saveReplayPayload } from './services/saved-replays.js';
import { migrateSlotsToReplays } from './services/slot-migration.js';
import { sharedReplaysGet, deleteSharedReplay } from './services/shared-replays.js';
import { shouldConfirmOpen } from './utils/session-resume-store.js';

/**
 * 行リンクを開くと、シミュレーターに退避中の記録が置き換わる場合は確認する。
 * 新しいタブで開く操作（修飾キー付き）は今のページが残るので確認しない。
 */
function confirmReplaceSessionRecord(ev) {
  if (!askReplaceSessionRecord(ev)) ev.preventDefault();
}

/** 置き換わる確認を出し、開いてよければ true（確認が要らない場合も true） */
function askReplaceSessionRecord(ev) {
  let r;
  try { r = shouldConfirmOpen(ev, sessionStorage); } catch (e) { return true; }
  if (!r.ask) return true;
  // 0手（setup ログだけ）のときは手数を出さない
  const what = r.steps > 0 ? `今の記録（${r.steps}手）` : '今の記録';
  return confirm(`シミュレーターの${what}は、このリプレイに置き換わります。開きますか？`);
}

/** 日時を「10/15 13:05」形式にする（不明なら空文字） */
function formatDateTime(ms) {
  if (!ms) return '';
  return new Date(ms).toLocaleString('ja-JP', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  });
}

/** 要素を作る小ヘルパー（テキストは textContent） */
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** 行のボタンを作る */
function actionButton(label, className, onClick) {
  const btn = el('button', 'rp-btn' + (className ? ' ' + className : ''), label);
  btn.type = 'button';
  btn.addEventListener('click', onClick);
  return btn;
}

/**
 * シミュレーターで開くリンクを、行のボタンとして作る（本物のリンクなので新しいタブでも開ける）。
 * 開くと今の記録が置き換わる場合は、行全体のリンクと同じ確認を出す。
 */
function openLinkButton(label, href) {
  const a = el('a', 'rp-btn rp-btn-open', label);
  a.href = href;
  a.addEventListener('click', confirmReplaceSessionRecord);
  return a;
}

/** クリップボードへコピーする（失敗時は execCommand にフォールバック）。成功なら true */
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch { ok = false; }
    ta.remove();
    return ok;
  }
}

// ── 1. 端末に保存したリプレイ ──

function renderSaved() {
  const listEl = document.getElementById('rpSavedList');
  const emptyEl = document.getElementById('rpSavedEmpty');
  if (!listEl) return;
  listEl.textContent = '';

  // 新しい順
  const items = savedReplaysGet().slice().sort((a, b) => (b.savedAt || 0) - (a.savedAt || 0));
  if (emptyEl) emptyEl.hidden = items.length > 0;

  for (const item of items) {
    const li = el('li', 'rp-row');

    // 行を押すとシミュレーターで開く（本物のリンクにして、新しいタブで開く等も使えるようにする）
    const openHref = '/solitaire?saved=' + encodeURIComponent(item.id);
    const link = el('a', 'rp-row-link');
    link.href = openHref;
    link.addEventListener('click', confirmReplaceSessionRecord);
    const info = el('span', 'rp-row-info');
    info.appendChild(el('span', 'rp-row-title', item.title || '無題'));
    const date = formatDateTime(item.savedAt);
    info.appendChild(el('span', 'rp-row-meta',
      `${date ? date + '・' : ''}${item.steps ?? 0}手・メイン${item.main ?? 0}枚・EX${item.ex ?? 0}枚`));
    link.appendChild(info);
    li.appendChild(link);

    const actions = el('div', 'rp-row-actions');
    actions.appendChild(openLinkButton('最初から再生', openHref));
    actions.appendChild(openLinkButton('最後の盤面から続ける', openHref + '&at=end'));
    actions.appendChild(actionButton('削除', 'rp-btn-danger', () => {
      if (!confirm(`「${item.title || '無題'}」を削除します。元に戻せません。よろしいですか？`)) return;
      deleteReplayEntry(item.id);
      renderSaved();
    }));
    li.appendChild(actions);
    listEl.appendChild(li);
  }
}

// ── 2. 共有リンクを作ったリプレイ ──

function renderShared() {
  const listEl = document.getElementById('rpSharedList');
  const emptyEl = document.getElementById('rpSharedEmpty');
  if (!listEl) return;
  listEl.textContent = '';

  const items = sharedReplaysGet(); // 新しい順に保存されている
  if (emptyEl) emptyEl.hidden = items.length > 0;

  for (const item of items) {
    const li = el('li', 'rp-row');
    const idParam = encodeURIComponent(item.id);

    const openHref = '/solitaire?replay=' + idParam;
    const link = el('a', 'rp-row-link');
    link.href = openHref;
    link.addEventListener('click', confirmReplaceSessionRecord);

    const thumb = document.createElement('img');
    thumb.className = 'rp-thumb';
    thumb.src = '/solitaire/og/' + idParam + '.png';
    thumb.alt = '';
    thumb.loading = 'lazy';
    thumb.width = 120;
    thumb.height = 63;
    // 画像が取れないときは、壊れた画像アイコンを出さずに隠す
    thumb.addEventListener('error', () => { thumb.hidden = true; });
    link.appendChild(thumb);

    const info = el('span', 'rp-row-info');
    info.appendChild(el('span', 'rp-row-title', item.title || '無題'));
    const date = formatDateTime(item.createdAt);
    info.appendChild(el('span', 'rp-row-meta', `${date ? date + '・' : ''}${item.steps ?? 0}手`));
    link.appendChild(info);
    li.appendChild(link);

    const actions = el('div', 'rp-row-actions');
    actions.appendChild(openLinkButton('最初から再生', openHref));
    actions.appendChild(openLinkButton('最後の盤面から続ける', openHref + '&at=end'));
    const copyBtn = actionButton('リンクをコピー', '', async () => {
      const ok = await copyText(location.origin + '/solitaire?replay=' + idParam);
      copyBtn.textContent = ok ? 'コピーしました' : 'コピーできませんでした';
      setTimeout(() => { copyBtn.textContent = 'リンクをコピー'; }, 2000);
    });
    actions.appendChild(copyBtn);
    actions.appendChild(actionButton('履歴から消す', 'rp-btn-danger', () => {
      if (!confirm(`「${item.title || '無題'}」をこの一覧から消します。共有リンクは作成から90日で自動的に消えます。よろしいですか？`)) return;
      deleteSharedReplay(item.id);
      renderShared();
    }));
    li.appendChild(actions);
    listEl.appendChild(li);
  }
}

// ── 3. ファイルから開く ──

const SAVE_FAIL_MESSAGES = {
  too_large: 'このリプレイは大きすぎて保存できません',
  quota: '保存できませんでした（端末の空き容量）',
  no_lzstring: '圧縮ライブラリを読み込めませんでした。ページを再読み込みしてください',
};

function showOpenError(message) {
  const errEl = document.getElementById('rpOpenError');
  if (!errEl) return;
  errEl.textContent = message;
  errEl.hidden = !message;
}

/**
 * 書き出した JSON（シミュレーターの「JSON保存」）を選んで、端末保存のリプレイとして登録し、
 * シミュレーターで開く。読めないファイルはその場でエラーを表示して、何も登録しない。
 */
async function handleOpenFile(e) {
  const input = e.target;
  const file = input.files?.[0];
  if (!file) return;
  showOpenError('');
  try {
    let payload;
    try {
      payload = JSON.parse(await file.text());
    } catch {
      throw new Error('JSON として読めませんでした');
    }
    assertReplayPayload(payload);
    // 開くと今の記録が置き換わる場合の確認（キャンセルなら何も登録しない）
    if (!askReplaceSessionRecord({ button: 0 })) return;
    const title = (typeof payload.title === 'string' && payload.title.trim())
      ? payload.title : file.name.replace(/\.json$/i, '');
    const saved = saveReplayPayload(payload, { title });
    if (!saved.ok) throw new Error(SAVE_FAIL_MESSAGES[saved.reason] || '保存に失敗しました');
    location.href = '/solitaire?saved=' + encodeURIComponent(saved.id);
  } catch (err) {
    showOpenError('開けませんでした: ' + err.message);
  } finally {
    input.value = ''; // 同じファイルをもう一度選べるようにする
  }
}

document.getElementById('rpOpenFile')?.addEventListener('change', handleOpenFile);

// 廃止した盤面セーブ（スロット1〜5）が残っていれば、0手のリプレイとして先に一覧へ移す
migrateSlotsToReplays();
renderSaved();
renderShared();
