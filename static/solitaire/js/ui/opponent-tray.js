/**
 * opponent-tray.js — 相手の想定妨害ミニ盤面
 *
 * 機能:
 *   - 自分盤面の上に開閉式で表示（5スロット）
 *   - プリセット汎用カードチップからワンタップ追加
 *   - /api/suggest を使った自由検索追加
 *   - タップで「使用済み」トグル（グレーアウト＋チェック）
 *   - sessionStorage で開閉状態・カード・使用済み状態を永続化
 *
 * 設計上の注意:
 *   - 相手カードの img は class="opp-card-img"（tier-item クラス不使用）
 *     → main.js のリプレイ辞書登録・枚数バッジ・カードクエリから隔離される
 *   - card-state.js / drag-drop.js / save-load-service.js は無改修
 *   - リプレイ(replayLog)には記録しない
 */

import { DISRUPTION_PRESETS } from '../config/disruption-presets.js';
import { showCardByName } from './card-info-panel.js';
import { playActivateEffect } from '../components/card-effects.js';

/** sessionStorage キー */
const SESSION_KEY = 'sol-opp-tray';

/** スロット最大数 */
const MAX_SLOTS = 5;

/** サジェストデバウンス（ms） */
const SUGGEST_DEBOUNCE = 200;

// ---------- 内部状態 ----------
let _suggestTimer = null;
let _suggestAbort = null;

// ---------- 初期化 ----------

/** エントリーポイント。main.js の initializeApp() から呼ぶ */
export function initOpponentTray() {
  _buildSlots();
  _buildPresetChips();
  _bindToggle();
  _bindSearch();
  _bindClear();
  _restore();
  _syncCipPosition();
}

/**
 * カード詳細パネル(.sol-cip-col)と cipToggle の top を
 * #mainContainer の上端に動的に合わせる。
 *
 * トレイ（ノーマルフロー）の高さが変わると #mainContainer が
 * 上下するため、CSS の固定 top では合わなくなる。
 * トグル開閉・復元のたびに呼ぶことで常に正確な位置を保つ。
 */
function _syncCipPosition() {
  requestAnimationFrame(() => {
    const fieldArea = document.querySelector('.sol-field-area');
    const mainContainer = document.getElementById('mainContainer');
    const cipCol = document.querySelector('.sol-cip-col');
    const cipToggle = document.getElementById('cipToggle');
    if (!fieldArea || !mainContainer || !cipCol || !cipToggle) return;

    const faRect = fieldArea.getBoundingClientRect();
    const mcRect = mainContainer.getBoundingClientRect();
    // #mainContainer 上端からさらに 4px 下（既存の padding-top 相当）
    const topPx = Math.round(mcRect.top - faRect.top) + 4;

    cipCol.style.top = `${topPx}px`;
    cipToggle.style.top = `${topPx}px`;
  });
}

// ---------- スロット構築 ----------

function _buildSlots() {
  const container = document.getElementById('oppTraySlots');
  if (!container) return;
  container.innerHTML = '';
  for (let i = 0; i < MAX_SLOTS; i++) {
    const slot = document.createElement('div');
    slot.className = 'opp-slot';
    slot.dataset.slotIndex = String(i);
    container.appendChild(slot);
  }
}

// ---------- カード追加 ----------

/**
 * 名前でカードを検索し、空きスロットに追加する
 * @param {string} name カード名
 */
async function addDisruptionCard(name) {
  const emptySlot = _findEmptySlot();
  if (!emptySlot) return; // 5スロット満杯

  let src = null;
  try {
    const res = await fetch(`/api/card-image?name=${encodeURIComponent(name)}`);
    if (res.ok) {
      const data = await res.json();
      src = data.url || data.image_url || null;
    }
  } catch {
    // 画像取得失敗は無視（カードを追加しない）
  }

  if (!src) return;

  _placeCard(emptySlot, name, src, false);
  _persist();
}

/**
 * スロットにカードを配置し、インタラクションを紐付ける
 * @param {HTMLElement} slot  .opp-slot 要素
 * @param {string}      name  カード名
 * @param {string}      src   画像URL
 * @param {boolean}     used  使用済み状態
 */
function _placeCard(slot, name, src, used) {
  // 既存コンテンツをクリア
  slot.innerHTML = '';
  slot.classList.add('filled');
  if (used) {
    slot.classList.add('opp-used');
    slot.dataset.used = '1';
  } else {
    slot.classList.remove('opp-used');
    slot.dataset.used = '';
  }

  // カード画像
  const img = document.createElement('img');
  img.className = 'opp-card-img';
  img.src = src;
  img.alt = name;
  img.dataset.cardName = name;
  img.title = name;
  img.loading = 'lazy';

  // シングルクリック → カード詳細パネルを更新
  img.addEventListener('click', (e) => {
    e.stopPropagation();
    showCardByName(name, src);
  });

  // ダブルクリック → 発動アニメーション → 使用済みマーク
  img.addEventListener('dblclick', (e) => {
    e.stopPropagation();
    playActivateEffect(slot);
    // アニメーション完了（~500ms）後に使用済み状態へ
    setTimeout(() => {
      slot.classList.add('opp-used');
      slot.dataset.used = '1';
      _persist();
    }, 500);
  });

  // 削除ボタン（ホバー時に表示、CSSで制御）
  const removeBtn = document.createElement('button');
  removeBtn.className = 'opp-remove-btn';
  removeBtn.type = 'button';
  removeBtn.textContent = '×';
  removeBtn.title = `${name} を削除`;
  removeBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    _clearSlot(slot);
    _persist();
  });

  slot.appendChild(img);
  slot.appendChild(removeBtn);
}

/** スロットを空に戻す */
function _clearSlot(slot) {
  slot.innerHTML = '';
  slot.classList.remove('filled', 'opp-used');
  slot.dataset.used = '';
}

/** 最初の空スロットを返す（満杯時は null） */
function _findEmptySlot() {
  return document.querySelector('.opp-slot:not(.filled)') || null;
}

// ---------- プリセットチップ ----------

function _buildPresetChips() {
  const container = document.getElementById('oppPresetChips');
  if (!container) return;
  container.innerHTML = '';

  DISRUPTION_PRESETS.forEach((group, gi) => {
    // カテゴリラベル
    const label = document.createElement('span');
    label.className = 'opp-preset-cat-label';
    label.textContent = group.cat + ':';

    // チップグループ
    const chipGroup = document.createElement('div');
    chipGroup.className = 'opp-preset-group';
    chipGroup.appendChild(label);

    group.cards.forEach(name => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'opp-preset-chip';
      btn.textContent = name;
      btn.title = `${name} を追加`;
      btn.addEventListener('click', () => addDisruptionCard(name));
      chipGroup.appendChild(btn);
    });

    container.appendChild(chipGroup);

    // カテゴリ間のセパレータ
    if (gi < DISRUPTION_PRESETS.length - 1) {
      const sep = document.createElement('span');
      sep.className = 'opp-preset-sep';
      container.appendChild(sep);
    }
  });
}

// ---------- 開閉トグル ----------

function _bindToggle() {
  const toggle = document.getElementById('oppTrayToggle');
  const tray = document.getElementById('opponentTray');
  const body = document.getElementById('oppTrayBody');
  if (!toggle || !tray || !body) return;

  toggle.addEventListener('click', () => {
    const collapsed = tray.classList.toggle('collapsed');
    toggle.setAttribute('aria-expanded', String(!collapsed));
    if (collapsed) {
      body.setAttribute('hidden', '');
    } else {
      body.removeAttribute('hidden');
    }
    _persistToggleState(!collapsed);
    // トレイ高さ変化に伴い #mainContainer が移動するため
    // カード詳細パネルの top を再計算する
    _syncCipPosition();
  });
}

// ---------- 検索 ----------

function _bindSearch() {
  const input = document.getElementById('oppSearchInput');
  const suggest = document.getElementById('oppSearchSuggest');
  if (!input || !suggest) return;

  input.addEventListener('input', () => {
    clearTimeout(_suggestTimer);
    const q = input.value.trim();
    if (!q) {
      _closeSuggest(suggest);
      return;
    }
    _suggestTimer = setTimeout(() => _fetchSuggest(q, input, suggest), SUGGEST_DEBOUNCE);
  });

  // フォーカスが外れたら閉じる（クリック猶予あり）
  input.addEventListener('blur', () => {
    setTimeout(() => _closeSuggest(suggest), 150);
  });

  // Escape で閉じる
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') _closeSuggest(suggest);
  });
}

async function _fetchSuggest(q, input, suggest) {
  if (_suggestAbort) _suggestAbort.abort();
  _suggestAbort = new AbortController();
  try {
    const res = await fetch(
      `/api/suggest?q=${encodeURIComponent(q)}`,
      { signal: _suggestAbort.signal }
    );
    if (!res.ok) return;
    const data = await res.json();
    const names = Array.isArray(data) ? data : (data.suggestions || data.results || []);
    _renderSuggest(names.slice(0, 8), input, suggest);
  } catch {
    // AbortError や fetch 失敗は無視
  }
}

function _renderSuggest(names, input, suggest) {
  suggest.innerHTML = '';
  if (!names.length) {
    _closeSuggest(suggest);
    return;
  }
  names.forEach(name => {
    const li = document.createElement('li');
    li.className = 'opp-suggest-item';
    li.textContent = name;
    li.addEventListener('mousedown', (e) => {
      e.preventDefault(); // blur より先に処理
      input.value = '';
      _closeSuggest(suggest);
      addDisruptionCard(name);
    });
    suggest.appendChild(li);
  });
  suggest.removeAttribute('hidden');
}

function _closeSuggest(suggest) {
  suggest.setAttribute('hidden', '');
  suggest.innerHTML = '';
}

// ---------- 全消去 ----------

function _bindClear() {
  const btn = document.getElementById('oppTrayClear');
  if (!btn) return;
  btn.addEventListener('click', () => {
    document.querySelectorAll('.opp-slot.filled').forEach(slot => _clearSlot(slot));
    _persist();
  });
}

// ---------- 永続化（sessionStorage） ----------

/** トレイ全体の状態を保存 */
function _persist() {
  const cards = [];
  document.querySelectorAll('.opp-slot').forEach(slot => {
    const img = slot.querySelector('.opp-card-img');
    if (img) {
      cards.push({
        name: img.dataset.cardName || '',
        src: img.src,
        used: slot.dataset.used === '1',
      });
    } else {
      cards.push(null); // 空スロット
    }
  });

  const tray = document.getElementById('opponentTray');
  const open = tray ? !tray.classList.contains('collapsed') : false;

  try {
    sessionStorage.setItem(SESSION_KEY, JSON.stringify({ cards, open }));
  } catch {
    // sessionStorage が使えない環境では無視
  }
}

/** トグル開閉状態のみ保存（カード変更なし） */
function _persistToggleState(open) {
  try {
    const raw = sessionStorage.getItem(SESSION_KEY);
    const state = raw ? JSON.parse(raw) : { cards: [], open: false };
    state.open = open;
    sessionStorage.setItem(SESSION_KEY, JSON.stringify(state));
  } catch {
    // 無視
  }
}

/** 保存済み状態を復元 */
function _restore() {
  let state;
  try {
    const raw = sessionStorage.getItem(SESSION_KEY);
    if (!raw) return;
    state = JSON.parse(raw);
  } catch {
    return;
  }

  const tray = document.getElementById('opponentTray');
  const toggle = document.getElementById('oppTrayToggle');
  const body = document.getElementById('oppTrayBody');

  // 開閉状態を復元
  if (state.open && tray) {
    tray.classList.remove('collapsed');
    if (toggle) toggle.setAttribute('aria-expanded', 'true');
    if (body) body.removeAttribute('hidden');
  }

  // カード配置を復元
  if (!Array.isArray(state.cards)) return;
  const slots = document.querySelectorAll('.opp-slot');
  state.cards.forEach((card, i) => {
    if (!card || !slots[i]) return;
    _placeCard(slots[i], card.name, card.src, card.used);
  });

  // 復元後にトレイ高さが確定するため、カード詳細パネルを再配置
  _syncCipPosition();
}
