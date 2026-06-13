/**
 * admin.js — カード相場 管理画面スクリプト
 *
 * - ES module。proxy-card.js を動的 import して再利用する
 * - 認証キーは localStorage('admin_key') に保存
 * - 全APIリクエストに X-Admin-Key ヘッダを付与
 * - 401 が返ったらログイン画面に戻す
 */

// ──────────────────────────────────────────────
// 定数
// ──────────────────────────────────────────────

const STORAGE_KEY = 'admin_key';
const PROXY_CARD_JS = '/static/solitaire/js/components/proxy-card.js';

// ──────────────────────────────────────────────
// localStorage 安全アクセス
// ──────────────────────────────────────────────
// プライベートモードやプライバシー強化設定で localStorage アクセスが
// SecurityError を投げる環境がある。トップレベルで例外が出ると
// スクリプト全体が停止し、ログインボタン等が一切反応しなくなる。
// そのため全アクセスを try/catch で包む（保存できなくてもログインは可能）。

function _storageGet(key) {
  try { return localStorage.getItem(key); } catch (e) { return null; }
}
function _storageSet(key, value) {
  try { localStorage.setItem(key, value); } catch (e) { /* 保存不可でも続行 */ }
}
function _storageRemove(key) {
  try { localStorage.removeItem(key); } catch (e) { /* 削除不可でも続行 */ }
}

// ──────────────────────────────────────────────
// 状態
// ──────────────────────────────────────────────

let _adminKey = _storageGet(STORAGE_KEY) || '';
let _createProxyCardElement = null;  // proxy-card.js からロード後に設定

// ──────────────────────────────────────────────
// proxy-card.js の遅延ロード
// ──────────────────────────────────────────────

async function _loadProxyCard() {
  if (_createProxyCardElement) return;
  try {
    const mod = await import(PROXY_CARD_JS);
    _createProxyCardElement = mod.createProxyCardElement;
  } catch (e) {
    console.warn('[admin] proxy-card.js のロードに失敗しました:', e);
  }
}

// ──────────────────────────────────────────────
// APIリクエストヘルパー
// ──────────────────────────────────────────────

/**
 * 管理API用の fetch ラッパー。
 * 401 が返ったらログイン画面へ戻す。
 */
async function apiFetch(url, options = {}) {
  const headers = Object.assign({}, options.headers || {}, {
    'X-Admin-Key': _adminKey,
    'Content-Type': 'application/json',
  });
  const resp = await fetch(url, { ...options, headers });
  if (resp.status === 401) {
    // 認証失効 → ログアウト
    _logout();
    throw new Error('認証エラー');
  }
  return resp;
}

// ──────────────────────────────────────────────
// 画面切替
// ──────────────────────────────────────────────

function _showLogin() {
  document.getElementById('login-screen').hidden = false;
  document.getElementById('main-screen').hidden = true;
}

function _showMain() {
  document.getElementById('login-screen').hidden = true;
  document.getElementById('main-screen').hidden = false;
  // 初回表示時に承認待ちタブをロード
  _loadPending();
}

function _logout() {
  _adminKey = '';
  _storageRemove(STORAGE_KEY);
  _showLogin();
}

// ──────────────────────────────────────────────
// ログイン処理
// ──────────────────────────────────────────────

document.getElementById('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const key = document.getElementById('login-key-input').value.trim();
  const errorEl = document.getElementById('login-error');
  errorEl.hidden = true;

  if (!key) return;

  try {
    const resp = await fetch('/api/admin/auth-check', {
      method: 'POST',
      headers: { 'X-Admin-Key': key, 'Content-Type': 'application/json' },
    });

    if (resp.ok) {
      _adminKey = key;
      _storageSet(STORAGE_KEY, key);
      _showMain();
    } else if (resp.status === 429) {
      errorEl.textContent = '認証失敗が多すぎます。しばらく待ってから再試行してください。';
      errorEl.hidden = false;
    } else {
      errorEl.textContent = '管理キーが正しくありません。';
      errorEl.hidden = false;
    }
  } catch (err) {
    errorEl.textContent = 'ネットワークエラーが発生しました。';
    errorEl.hidden = false;
  }
});

// ──────────────────────────────────────────────
// ログアウトボタン
// ──────────────────────────────────────────────

document.getElementById('logout-btn').addEventListener('click', _logout);

// ──────────────────────────────────────────────
// タブ切替
// ──────────────────────────────────────────────

document.querySelectorAll('.tab-btn').forEach((btn) => {
  btn.addEventListener('click', () => {
    // アクティブ切替
    document.querySelectorAll('.tab-btn').forEach((b) => b.classList.remove('tab-btn--active'));
    btn.classList.add('tab-btn--active');

    const tabName = btn.dataset.tab;

    // セクション表示切替
    document.querySelectorAll('.tab-section').forEach((sec) => { sec.hidden = true; });
    document.getElementById(`tab-${tabName}`).hidden = false;

    // 初回ロード
    if (tabName === 'pending') _loadPending();
    if (tabName === 'approved') _loadApproved();
    if (tabName === 'settings') _loadSettings();
  });
});

// ──────────────────────────────────────────────
// 再読み込みボタン
// ──────────────────────────────────────────────

document.getElementById('reload-pending-btn').addEventListener('click', _loadPending);
document.getElementById('reload-approved-btn').addEventListener('click', _loadApproved);
document.getElementById('reload-settings-btn').addEventListener('click', _loadSettings);

// ──────────────────────────────────────────────
// ユーティリティ
// ──────────────────────────────────────────────

/**
 * 日付文字列をローカル形式に変換する
 * @param {string} iso
 * @returns {string}
 */
function _formatDate(iso) {
  if (!iso) return '';
  try {
    return new Date(iso).toLocaleString('ja-JP', {
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit',
    });
  } catch {
    return iso;
  }
}

/**
 * 信頼度バッジのクラスを返す
 * @param {string} confidence
 * @returns {string}
 */
function _confidenceClass(confidence) {
  if (confidence === 'high')   return 'confidence-badge--high';
  if (confidence === 'medium') return 'confidence-badge--medium';
  if (confidence === 'low')    return 'confidence-badge--low';
  return 'confidence-badge--medium';
}

/**
 * 信頼度バッジのラベルを返す
 * @param {string} confidence
 * @returns {string}
 */
function _confidenceLabel(confidence) {
  if (confidence === 'high')   return '高';
  if (confidence === 'medium') return '中';
  if (confidence === 'low')    return '低';
  return confidence || '';
}

/**
 * フォーム結果メッセージを表示する
 * @param {HTMLElement} el
 * @param {string} msg
 * @param {'ok'|'error'} type
 */
function _showResult(el, msg, type) {
  el.textContent = msg;
  el.className = `form-result form-result--${type}`;
  el.hidden = false;
  // 5秒後に自動で消す
  setTimeout(() => { el.hidden = true; }, 5000);
}

// ──────────────────────────────────────────────
// プロキシデータをカードデータから構築
// ──────────────────────────────────────────────

/**
 * unreleased_cards レコード（またはフォーム値）から
 * proxy-card.js 用のデータオブジェクトを構築する。
 * card_display.py の _build_proxy_data と同じロジック。
 * @param {Object} card
 * @returns {Object}
 */
function _buildProxyData(card) {
  const cardType = card.card_type || '';
  const ct = cardType.toLowerCase();

  let broadType = 'monster';
  if (ct.includes('魔法')) {
    broadType = 'spell';
  } else if (ct.includes('罠')) {
    broadType = 'trap';
  }

  const isExKeywords = ['融合', 'シンクロ', 'エクシーズ', 'リンク'];
  const isEx = isExKeywords.some((kw) => ct.includes(kw));

  return {
    name:         card.name || '',
    reading:      card.reading || '',
    card_type:    cardType,
    attribute:    card.attribute || '',
    race:         card.race || '',
    level:        card.level != null ? Number(card.level) : null,
    rank:         card.rank  != null ? Number(card.rank)  : null,
    link_val:     card.link_val != null ? Number(card.link_val) : null,
    atk:          card.atk  != null ? Number(card.atk)  : null,
    def:          card.def  || '',
    pendulum_scale: card.pendulum_scale != null ? Number(card.pendulum_scale) : null,
    pendulum_effect: card.pendulum_effect || '',
    effect_text:  card.effect_text || '',
    product_name: card.product_name || '',
    release_date: String(card.release_date || ''),
    broad_type:   broadType,
    is_ex:        isEx,
  };
}

/**
 * 編集フォームのプレビュースロットにプロキシカードを描画する
 * @param {HTMLElement} slot  .edit-preview__slot
 * @param {Object} cardData
 */
function _renderPreview(slot, cardData) {
  if (!_createProxyCardElement) return;
  slot.innerHTML = '';
  try {
    const el = _createProxyCardElement(_buildProxyData(cardData));
    slot.appendChild(el);
  } catch (e) {
    console.warn('[admin] プレビュー描画エラー:', e);
  }
}

/**
 * 編集フォームの画像切替タブを配線する。
 * タブをクリックすると対応するパネル（抽出元画像 / プロキシ表示）を表示する。
 * @param {HTMLElement} editForm  .card-row__edit-form
 */
function _wireMediaTabs(editForm) {
  editForm.querySelectorAll('.edit-media-tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      _setActiveMedia(editForm, tab.dataset.media);
    });
  });
}

/**
 * 編集フォームで指定した画像メディアを表示状態にする。
 * タブの is-active と、各パネルの表示/非表示を切り替える。
 * @param {HTMLElement} editForm  .card-row__edit-form
 * @param {string} media  'source'（抽出元画像）| 'proxy'（プロキシ表示）
 */
function _setActiveMedia(editForm, media) {
  editForm.querySelectorAll('.edit-media-tab').forEach((tab) => {
    tab.classList.toggle('is-active', tab.dataset.media === media);
  });
  editForm.querySelectorAll('.edit-media-panel').forEach((panel) => {
    panel.hidden = panel.dataset.media !== media;
  });
}

// ──────────────────────────────────────────────
// 承認待ちタブ
// ──────────────────────────────────────────────

async function _loadPending() {
  const listEl = document.getElementById('pending-list');
  listEl.innerHTML = '<p class="loading-text">読み込み中...</p>';
  await _loadProxyCard();

  try {
    const resp = await apiFetch('/api/admin/unreleased?status=pending,needs_review');
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      listEl.innerHTML = `<p class="loading-text">エラー: ${data.error || resp.status}</p>`;
      return;
    }
    const { cards } = await resp.json();
    _renderPendingList(listEl, cards);
  } catch (e) {
    if (e.message !== '認証エラー') {
      listEl.innerHTML = '<p class="loading-text">読み込みに失敗しました。</p>';
    }
  }
}

// ──────────────────────────────────────────────
// 一括承認
// ──────────────────────────────────────────────

/**
 * 一括承認バーの選択件数ラベルとボタン有効状態を更新する
 */
function _updateBulkApproveBar() {
  const bar = document.getElementById('bulk-approve-bar');
  const btn = document.getElementById('bulk-approve-btn');
  const allCheck = document.getElementById('bulk-select-all');
  const checkboxes = document.querySelectorAll('#pending-list .card-row__select');
  const checked = document.querySelectorAll('#pending-list .card-row__select:checked');

  const total = checkboxes.length;
  const count = checked.length;

  btn.textContent = `選択を一括承認（${count}件）`;
  btn.disabled = count === 0;

  // 全選択チェックボックスの状態を同期
  if (total === 0) {
    allCheck.checked = false;
    allCheck.indeterminate = false;
  } else if (count === total) {
    allCheck.checked = true;
    allCheck.indeterminate = false;
  } else if (count === 0) {
    allCheck.checked = false;
    allCheck.indeterminate = false;
  } else {
    allCheck.checked = false;
    allCheck.indeterminate = true;
  }
}

// 全選択チェックボックス
document.getElementById('bulk-select-all').addEventListener('change', (e) => {
  const checked = e.target.checked;
  document.querySelectorAll('#pending-list .card-row__select').forEach((cb) => {
    cb.checked = checked;
  });
  _updateBulkApproveBar();
});

// 一括承認ボタン
document.getElementById('bulk-approve-btn').addEventListener('click', async () => {
  const checked = document.querySelectorAll('#pending-list .card-row__select:checked');
  const ids = Array.from(checked).map((cb) => {
    return Number(cb.closest('.card-row').dataset.cardId);
  });

  if (ids.length === 0) return;

  if (!confirm(`${ids.length}件を承認します。よろしいですか？`)) return;

  const btn = document.getElementById('bulk-approve-btn');
  btn.disabled = true;
  btn.textContent = '処理中...';

  try {
    const resp = await apiFetch('/api/admin/unreleased/bulk-approve', {
      method: 'POST',
      body: JSON.stringify({ ids }),
    });
    const data = await resp.json().catch(() => ({}));

    if (resp.ok) {
      // 承認済み行をDOMから除去
      checked.forEach((cb) => {
        cb.closest('.card-row').remove();
      });
      // 全選択を解除して件数を更新
      document.getElementById('bulk-select-all').checked = false;
      document.getElementById('bulk-select-all').indeterminate = false;
      _updateBulkApproveBar();
      alert(
        `${data.approved}件を承認しました。\n` +
        '画像は取り込みに数秒〜十数秒かかります。少し待ってから再読み込みしてください。'
      );
    } else {
      alert(data.error || '一括承認に失敗しました');
      _updateBulkApproveBar();
    }
  } catch (e) {
    if (e.message !== '認証エラー') {
      alert('通信エラーが発生しました');
    }
    _updateBulkApproveBar();
  }
});

/**
 * 承認待ちカード一覧を描画する
 * @param {HTMLElement} listEl
 * @param {Array} cards
 */
function _renderPendingList(listEl, cards) {
  listEl.innerHTML = '';

  // 一括承認バーの表示/非表示を制御
  const bar = document.getElementById('bulk-approve-bar');

  if (!cards || cards.length === 0) {
    listEl.innerHTML = '<p class="empty-text">承認待ちのカードはありません。</p>';
    bar.hidden = true;
    return;
  }

  bar.hidden = false;

  const tmpl = document.getElementById('tmpl-pending-row');

  for (const card of cards) {
    const row = tmpl.content.cloneNode(true).querySelector('.card-row');
    row.dataset.cardId = card.id;

    // 概要行
    row.querySelector('.card-row__name').textContent     = card.name;
    row.querySelector('.card-row__product').textContent  = card.product_name || '';
    row.querySelector('.card-row__type').textContent     = card.card_type || '';
    row.querySelector('.card-row__extracted-at').textContent = _formatDate(card.extracted_at);

    // 信頼度バッジ
    const badge = row.querySelector('.confidence-badge');
    badge.textContent = _confidenceLabel(card.confidence);
    badge.classList.add(_confidenceClass(card.confidence));

    // 抽出元リンク
    const link = row.querySelector('.card-row__source-link');
    if (card.source_url && card.source_url !== 'manual') {
      link.href = card.source_url;
      link.title = card.source_url;
    } else {
      link.textContent = '手動登録';
      link.removeAttribute('href');
    }

    // 画像ありバッジ
    if (card.has_image) {
      row.querySelector('.card-row__has-image').hidden = false;
    }

    // 編集フォームにフィールド値をセット
    const editForm = row.querySelector('.card-row__edit-form');
    const fields = editForm.querySelectorAll('.edit-field');
    fields.forEach((input) => {
      const name = input.name;
      const val = card[name];
      if (val != null) input.value = val;
    });

    // 画像切替タブ（抽出元画像 / プロキシ表示）の初期化
    const sourceImageWrap = editForm.querySelector('.edit-source-image');
    const hasSource = !!card.card_image_url;
    if (hasSource) {
      const imgEl = sourceImageWrap.querySelector('.edit-source-image__img');
      const linkEl = sourceImageWrap.querySelector('.edit-source-image__link');
      imgEl.src = card.card_image_url;
      imgEl.alt = card.name;
      linkEl.href = card.card_image_url;
      // 抽出元画像がある場合のみタブを表示
      editForm.querySelector('.edit-media-tab[data-media="source"]').hidden = false;
    }
    _wireMediaTabs(editForm);
    // 抽出元画像があればそれを、無ければプロキシ表示を初期表示にする
    _setActiveMedia(editForm, hasSource ? 'source' : 'proxy');

    // プレビュー初期描画
    const slot = editForm.querySelector('.edit-preview__slot');
    _renderPreview(slot, card);

    // 編集フォームのリアルタイムプレビュー更新
    fields.forEach((input) => {
      input.addEventListener('input', () => {
        const current = _collectFormData(editForm);
        _renderPreview(slot, { ...card, ...current });
      });
    });

    // 編集フォーム開閉
    const toggleEditBtn = row.querySelector('.action-toggle-edit');
    toggleEditBtn.addEventListener('click', () => {
      const isHidden = editForm.hidden;
      editForm.hidden = !isHidden;
      toggleEditBtn.textContent = isHidden ? '閉じる' : '編集';
    });

    // キャンセルボタン
    row.querySelector('.action-cancel-edit').addEventListener('click', () => {
      editForm.hidden = true;
      toggleEditBtn.textContent = '編集';
    });

    // 保存ボタン
    row.querySelector('.action-save').addEventListener('click', () => {
      _saveCard(card.id, editForm, row);
    });

    // 承認ボタン
    row.querySelector('.action-approve').addEventListener('click', () => {
      _approveCard(card.id, row);
    });

    // 却下ボタン
    row.querySelector('.action-reject').addEventListener('click', () => {
      _rejectCard(card.id, row);
    });

    // 削除ボタン
    row.querySelector('.action-delete').addEventListener('click', () => {
      _deleteCard(card.id, row);
    });

    // 一括承認チェックボックス変更時に件数ラベルを更新
    row.querySelector('.card-row__select').addEventListener('change', _updateBulkApproveBar);

    listEl.appendChild(row);
  }

  // 初期状態のバー更新（全件未選択）
  _updateBulkApproveBar();
}

/**
 * 編集フォームからフィールド値を収集する
 * @param {HTMLElement} editForm
 * @returns {Object}
 */
function _collectFormData(editForm) {
  const result = {};
  editForm.querySelectorAll('.edit-field').forEach((input) => {
    const name = input.name;
    const raw = input.value.trim();

    // 数値フィールド
    if (['level', 'rank', 'link_val', 'atk', 'pendulum_scale'].includes(name)) {
      result[name] = raw !== '' ? Number(raw) : null;
    } else if (name === 'release_date') {
      result[name] = raw || null;
    } else {
      result[name] = raw;
    }
  });
  return result;
}

// ──────────────────────────────────────────────
// カード操作（承認待ち）
// ──────────────────────────────────────────────

async function _saveCard(cardId, editForm, row) {
  const resultEl = row.querySelector('.edit-result');
  const updates = _collectFormData(editForm);
  if (!updates.name) {
    _showResult(resultEl, 'カード名は必須です', 'error');
    return;
  }

  try {
    const resp = await apiFetch(`/api/admin/unreleased/${cardId}`, {
      method: 'PUT',
      body: JSON.stringify(updates),
    });
    const data = await resp.json().catch(() => ({}));
    if (resp.ok) {
      _showResult(resultEl, '保存しました', 'ok');
      // 概要行の名前等を更新
      if (data.card) {
        row.querySelector('.card-row__name').textContent    = data.card.name || '';
        row.querySelector('.card-row__product').textContent = data.card.product_name || '';
        row.querySelector('.card-row__type').textContent    = data.card.card_type || '';
      }
    } else {
      _showResult(resultEl, data.error || '保存に失敗しました', 'error');
    }
  } catch (e) {
    if (e.message !== '認証エラー') {
      _showResult(resultEl, '通信エラーが発生しました', 'error');
    }
  }
}

async function _approveCard(cardId, row) {
  if (!confirm(`ID ${cardId} のカードを承認しますか？`)) return;
  try {
    const resp = await apiFetch(`/api/admin/unreleased/${cardId}/approve`, { method: 'POST' });
    if (resp.ok) {
      row.remove();
    } else {
      const data = await resp.json().catch(() => ({}));
      alert(data.error || '承認に失敗しました');
    }
  } catch (e) {
    if (e.message !== '認証エラー') alert('通信エラーが発生しました');
  }
}

async function _rejectCard(cardId, row) {
  if (!confirm(`ID ${cardId} のカードを却下しますか？`)) return;
  try {
    const resp = await apiFetch(`/api/admin/unreleased/${cardId}/reject`, { method: 'POST' });
    if (resp.ok) {
      row.remove();
    } else {
      const data = await resp.json().catch(() => ({}));
      alert(data.error || '却下に失敗しました');
    }
  } catch (e) {
    if (e.message !== '認証エラー') alert('通信エラーが発生しました');
  }
}

async function _deleteCard(cardId, row) {
  if (!confirm(`ID ${cardId} のカードを完全削除しますか？\nこの操作は取り消せません。`)) return;
  try {
    const resp = await apiFetch(`/api/admin/unreleased/${cardId}`, { method: 'DELETE' });
    if (resp.ok) {
      row.remove();
    } else {
      const data = await resp.json().catch(() => ({}));
      alert(data.error || '削除に失敗しました');
    }
  } catch (e) {
    if (e.message !== '認証エラー') alert('通信エラーが発生しました');
  }
}

// ──────────────────────────────────────────────
// 公開中タブ
// ──────────────────────────────────────────────

async function _loadApproved() {
  const listEl = document.getElementById('approved-list');
  listEl.innerHTML = '<p class="loading-text">読み込み中...</p>';
  await _loadProxyCard();

  try {
    const resp = await apiFetch('/api/admin/unreleased?status=approved,linked');
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      listEl.innerHTML = `<p class="loading-text">エラー: ${data.error || resp.status}</p>`;
      return;
    }
    const { cards } = await resp.json();
    _renderApprovedList(listEl, cards);
  } catch (e) {
    if (e.message !== '認証エラー') {
      listEl.innerHTML = '<p class="loading-text">読み込みに失敗しました。</p>';
    }
  }
}

/**
 * 公開中カード一覧を描画する
 * @param {HTMLElement} listEl
 * @param {Array} cards
 */
function _renderApprovedList(listEl, cards) {
  listEl.innerHTML = '';

  if (!cards || cards.length === 0) {
    listEl.innerHTML = '<p class="empty-text">公開中のカードはありません。</p>';
    return;
  }

  const tmpl = document.getElementById('tmpl-approved-row');

  for (const card of cards) {
    const row = tmpl.content.cloneNode(true).querySelector('.card-row');
    row.dataset.cardId = card.id;

    row.querySelector('.card-row__name').textContent    = card.name;
    row.querySelector('.card-row__product').textContent = card.product_name || '';
    row.querySelector('.card-row__type').textContent    = card.card_type || '';

    // ステータスバッジ
    const statusBadge = row.querySelector('.card-row__status-badge');
    statusBadge.textContent = card.status === 'linked' ? 'linked' : '公開中';
    statusBadge.className = `status-badge status-badge--${card.status}`;

    // 画像ありバッジ
    if (card.has_image) {
      row.querySelector('.card-row__has-image').hidden = false;
    }

    // 非表示トグルボタン
    const toggleHiddenBtn = row.querySelector('.action-toggle-hidden');
    toggleHiddenBtn.textContent = card.hidden ? '再表示する' : '非表示にする';
    if (card.hidden) {
      row.style.opacity = '0.5';
    }
    toggleHiddenBtn.addEventListener('click', () => {
      _toggleHidden(card.id, row, toggleHiddenBtn);
    });

    // 取り下げボタン
    row.querySelector('.action-reject-approved').addEventListener('click', () => {
      _rejectCard(card.id, row);
    });

    // 画像取込フォーム開閉ボタン
    const imageForm = row.querySelector('.card-row__image-form');
    const toggleImageFormBtn = row.querySelector('.action-toggle-image-form');
    toggleImageFormBtn.addEventListener('click', () => {
      const isHidden = imageForm.hidden;
      imageForm.hidden = !isHidden;
      toggleImageFormBtn.textContent = isHidden ? '閉じる' : '画像取込';
      // 開いた際にサムネイルを読み込む（has_image が true の場合）
      if (isHidden && card.has_image) {
        _loadImageThumbnail(card.id, row);
      }
    });

    // 画像URLを指定して取込ボタン
    row.querySelector('.action-fetch-image').addEventListener('click', () => {
      _fetchImageByUrl(card.id, row);
    });

    // ── 編集フォーム（承認待ちタブと同一の配線） ──
    const editForm = row.querySelector('.card-row__edit-form');
    const fields = editForm.querySelectorAll('.edit-field');

    // 編集フォームにフィールド値をセット
    fields.forEach((input) => {
      const name = input.name;
      const val = card[name];
      if (val != null) input.value = val;
    });

    // プレビュー初期描画
    const slot = editForm.querySelector('.edit-preview__slot');
    _renderPreview(slot, card);
    _wireMediaTabs(editForm);
    // 初期はプロキシ表示（抽出元画像の有無は編集フォームを開いた時に判定する）
    _setActiveMedia(editForm, 'proxy');

    // 編集フォームのリアルタイムプレビュー更新
    fields.forEach((input) => {
      input.addEventListener('input', () => {
        const current = _collectFormData(editForm);
        _renderPreview(slot, { ...card, ...current });
      });
    });

    // 編集フォーム開閉
    const toggleEditBtn = row.querySelector('.action-toggle-edit');
    toggleEditBtn.addEventListener('click', async () => {
      const isHidden = editForm.hidden;
      editForm.hidden = !isHidden;
      toggleEditBtn.textContent = isHidden ? '閉じる' : '編集';
      // 開いた時に取り込み済みカード画像を遅延読み込みし、タブ表示を更新する
      if (isHidden) {
        await _loadEditCardImage(card, editForm);
        const hasSource = !!editForm.querySelector('.edit-source-image__img').getAttribute('src');
        editForm.querySelector('.edit-media-tab[data-media="source"]').hidden = !hasSource;
        _setActiveMedia(editForm, hasSource ? 'source' : 'proxy');
      }
    });

    // キャンセルボタン
    row.querySelector('.action-cancel-edit').addEventListener('click', () => {
      editForm.hidden = true;
      toggleEditBtn.textContent = '編集';
    });

    // 保存ボタン
    row.querySelector('.action-save').addEventListener('click', () => {
      _saveCard(card.id, editForm, row);
    });

    listEl.appendChild(row);
  }
}

/**
 * サムネイル画像を読み込んで表示する（公開中タブ）
 * @param {number} cardId
 * @param {HTMLElement} row
 */
async function _loadImageThumbnail(cardId, row) {
  const imgEl = row.querySelector('.image-thumbnail');
  const noneEl = row.querySelector('.image-thumbnail-none');

  try {
    // official_card_images の public_url を取得するために approved-list API を再利用せず
    // 直接 card_display API 経由で画像URLを取得する
    const resp = await fetch(`/api/card-image?name=${encodeURIComponent(row.querySelector('.card-row__name').textContent)}`);
    if (resp.ok) {
      const data = await resp.json();
      if (data.kind === 'image' && data.url) {
        imgEl.src = data.url;
        imgEl.alt = row.querySelector('.card-row__name').textContent;
        imgEl.hidden = false;
        noneEl.hidden = true;
        return;
      }
    }
  } catch {
    // 無視
  }
  imgEl.hidden = true;
  noneEl.hidden = false;
}

/**
 * 公開中タブの編集フォームに、取り込み済みの公式カード画像を表示する。
 * フォームを開いた時に1枚だけ遅延読み込みする（一覧表示時は読まない）。
 * 取り込み済み画像が無ければ抽出元画像URLにフォールバックする。
 * @param {Object} card  一覧APIのカードレコード
 * @param {HTMLElement} editForm  .card-row__edit-form
 */
async function _loadEditCardImage(card, editForm) {
  const wrap = editForm.querySelector('.edit-source-image');
  const imgEl = wrap.querySelector('.edit-source-image__img');
  const linkEl = wrap.querySelector('.edit-source-image__link');

  // 既に読み込み済みなら再取得しない
  // 注: 空の <img src=""> でも imgEl.src は絶対URLに解決され空にならないため、
  //     生の属性値（getAttribute）で判定する。
  if (imgEl.getAttribute('src')) return;

  let url = '';

  // 優先: 取り込み済みの公式画像（実際にサイトに出ている画像）
  if (card.has_image) {
    try {
      const resp = await fetch(`/api/card-image?name=${encodeURIComponent(card.name)}`);
      if (resp.ok) {
        const data = await resp.json();
        if (data.kind === 'image' && data.url) url = data.url;
      }
    } catch {
      // 取得失敗時はフォールバックへ
    }
  }

  // フォールバック: 抽出元画像URL（手動登録カードには無いことが多い）
  if (!url && card.card_image_url) url = card.card_image_url;

  if (url) {
    imgEl.src = url;
    imgEl.alt = card.name;
    linkEl.href = url;
    wrap.hidden = false;
  }
}

/**
 * 画像URLを指定して取込する（公開中タブ）
 * @param {number} cardId
 * @param {HTMLElement} row
 */
async function _fetchImageByUrl(cardId, row) {
  const urlInput = row.querySelector('.image-url-input');
  const resultEl = row.querySelector('.image-fetch-result');
  const imageUrl = (urlInput.value || '').trim();

  if (!imageUrl) {
    _showResult(resultEl, '画像URLを入力してください', 'error');
    return;
  }

  const fetchBtn = row.querySelector('.action-fetch-image');
  fetchBtn.disabled = true;

  try {
    const resp = await apiFetch(`/api/admin/unreleased/${cardId}/fetch-image`, {
      method: 'POST',
      body: JSON.stringify({ image_url: imageUrl }),
    });
    const data = await resp.json().catch(() => ({}));
    if (resp.ok && data.ok) {
      _showResult(resultEl, '画像を取り込みました', 'ok');
      urlInput.value = '';
      // 画像ありバッジを表示
      row.querySelector('.card-row__has-image').hidden = false;
      // サムネイルを更新
      _loadImageThumbnail(cardId, row);
    } else {
      _showResult(resultEl, data.error || '取込に失敗しました', 'error');
    }
  } catch (e) {
    if (e.message !== '認証エラー') {
      _showResult(resultEl, '通信エラーが発生しました', 'error');
    }
  } finally {
    fetchBtn.disabled = false;
  }
}

async function _toggleHidden(cardId, row, btn) {
  try {
    const resp = await apiFetch(`/api/admin/unreleased/${cardId}/toggle-hidden`, { method: 'POST' });
    if (resp.ok) {
      const data = await resp.json().catch(() => ({}));
      const isHidden = data.hidden;
      btn.textContent = isHidden ? '再表示する' : '非表示にする';
      row.style.opacity = isHidden ? '0.5' : '1';
    } else {
      const data = await resp.json().catch(() => ({}));
      alert(data.error || '切替に失敗しました');
    }
  } catch (e) {
    if (e.message !== '認証エラー') alert('通信エラーが発生しました');
  }
}

// ──────────────────────────────────────────────
// 手動登録タブ
// ──────────────────────────────────────────────

document.getElementById('manual-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target;
  const resultEl = document.getElementById('manual-result');
  resultEl.hidden = true;

  // フォームデータ収集
  const body = {};
  const numFields = ['level', 'rank', 'link_val', 'atk', 'pendulum_scale'];
  for (const el of form.elements) {
    if (!el.name) continue;
    const raw = el.value.trim();
    if (numFields.includes(el.name)) {
      body[el.name] = raw !== '' ? Number(raw) : null;
    } else if (el.name === 'release_date') {
      body[el.name] = raw || null;
    } else {
      body[el.name] = raw;
    }
  }

  try {
    const resp = await apiFetch('/api/admin/unreleased', {
      method: 'POST',
      body: JSON.stringify(body),
    });
    const data = await resp.json().catch(() => ({}));
    if (resp.ok || resp.status === 201) {
      _showResult(resultEl, `「${data.card?.name || body.name}」を登録しました。承認待ちタブに表示されます。`, 'ok');
      form.reset();
    } else {
      _showResult(resultEl, data.error || '登録に失敗しました', 'error');
    }
  } catch (e) {
    if (e.message !== '認証エラー') {
      _showResult(resultEl, '通信エラーが発生しました', 'error');
    }
  }
});

// ──────────────────────────────────────────────
// 設定タブ
// ──────────────────────────────────────────────

async function _loadSettings() {
  const toggle = document.getElementById('official-image-toggle');
  const resultEl = document.getElementById('settings-result');
  toggle.disabled = true;
  resultEl.hidden = true;

  try {
    const resp = await apiFetch('/api/admin/settings');
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      _showResult(resultEl, data.error || '設定取得に失敗しました', 'error');
      return;
    }
    const { settings } = await resp.json();
    const imageDisplaySetting = settings.find((s) => s.key === 'OFFICIAL_IMAGE_DISPLAY');
    if (imageDisplaySetting) {
      toggle.checked = Boolean(imageDisplaySetting.value?.enabled);
    }
    toggle.disabled = false;
  } catch (e) {
    if (e.message !== '認証エラー') {
      _showResult(resultEl, '通信エラーが発生しました', 'error');
    }
  }

  // 画像出所ドメイン一覧を読み込む
  _loadDomains();
}

// ──────────────────────────────────────────────
// 設定タブ — 画像の出所管理
// ──────────────────────────────────────────────

async function _loadDomains() {
  const listEl = document.getElementById('domains-list');
  listEl.innerHTML = '<p class="loading-text">読み込み中...</p>';

  try {
    const resp = await apiFetch('/api/admin/images/domains');
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      listEl.innerHTML = `<p class="loading-text">エラー: ${data.error || resp.status}</p>`;
      return;
    }
    const { domains } = await resp.json();
    _renderDomainList(listEl, domains);
  } catch (e) {
    if (e.message !== '認証エラー') {
      listEl.innerHTML = '<p class="loading-text">読み込みに失敗しました。</p>';
    }
  }
}

/**
 * ドメイン一覧を描画する
 * @param {HTMLElement} listEl
 * @param {Array} domains
 */
function _renderDomainList(listEl, domains) {
  listEl.innerHTML = '';

  if (!domains || domains.length === 0) {
    listEl.innerHTML = '<p class="empty-text">取り込んだ画像はありません。</p>';
    return;
  }

  for (const d of domains) {
    const row = document.createElement('div');
    row.className = 'domain-row';
    row.innerHTML = `
      <div class="domain-row__info">
        <span class="domain-row__name">${_escapeHtml(d.domain)}</span>
        <span class="domain-row__count">${d.total}件（非表示: ${d.hidden}件）</span>
      </div>
      <div class="domain-row__actions">
        <button class="btn btn-warning btn-sm action-hide-domain" data-domain="${_escapeHtml(d.domain)}">非表示化</button>
        <button class="btn btn-danger btn-sm action-delete-domain" data-domain="${_escapeHtml(d.domain)}">完全削除</button>
      </div>
    `;

    // 非表示化ボタン（第1段階）
    row.querySelector('.action-hide-domain').addEventListener('click', () => {
      _purgeDomain(d.domain, false);
    });

    // 完全削除ボタン（第2段階）
    row.querySelector('.action-delete-domain').addEventListener('click', () => {
      _purgeDomain(d.domain, true);
    });

    listEl.appendChild(row);
  }
}

/**
 * HTML特殊文字をエスケープする
 * @param {string} str
 * @returns {string}
 */
function _escapeHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/**
 * ドメイン一括削除を実行する（2段階確認）
 * @param {string} domain
 * @param {boolean} physical  true=物理削除、false=非表示化のみ
 */
async function _purgeDomain(domain, physical) {
  const resultEl = document.getElementById('domains-result');
  const action = physical ? '完全削除（Storage物理削除含む）' : '非表示化';

  // 第1確認: 操作内容の確認
  if (!confirm(`ドメイン「${domain}」の画像を${action}します。\nこの操作は取り消せません。続行しますか？`)) {
    return;
  }

  // 第2確認: ドメイン名の手入力確認
  const inputDomain = prompt(`確認のため、ドメイン名を入力してください:\n${domain}`);
  if (inputDomain !== domain) {
    alert('ドメイン名が一致しません。操作を中止しました。');
    return;
  }

  try {
    const resp = await apiFetch('/api/admin/images/purge-domain', {
      method: 'POST',
      body: JSON.stringify({ domain, physical }),
    });
    const data = await resp.json().catch(() => ({}));
    if (resp.ok && data.ok) {
      const msg = physical
        ? `完全削除完了: ${data.hidden_count}件を非表示化・${data.deleted_count}件を削除しました`
        : `非表示化完了: ${data.hidden_count}件を非表示化しました`;
      _showResult(resultEl, msg, 'ok');
      // 一覧を再読み込み
      _loadDomains();
    } else {
      _showResult(resultEl, data.error || '処理に失敗しました', 'error');
    }
  } catch (e) {
    if (e.message !== '認証エラー') {
      _showResult(resultEl, '通信エラーが発生しました', 'error');
    }
  }
}

document.getElementById('reload-domains-btn').addEventListener('click', _loadDomains);

document.getElementById('official-image-toggle').addEventListener('change', async (e) => {
  const toggle = e.target;
  const resultEl = document.getElementById('settings-result');
  const enabled = toggle.checked;
  toggle.disabled = true;
  resultEl.hidden = true;

  try {
    const resp = await apiFetch('/api/admin/settings/official-image-display', {
      method: 'POST',
      body: JSON.stringify({ enabled }),
    });
    const data = await resp.json().catch(() => ({}));
    if (resp.ok) {
      _showResult(
        resultEl,
        `公式画像表示を${enabled ? '有効' : '無効'}にしました。最大30秒以内に全ワーカーへ反映されます。`,
        'ok',
      );
    } else {
      // 失敗時は元の状態に戻す
      toggle.checked = !enabled;
      _showResult(resultEl, data.error || '設定更新に失敗しました', 'error');
    }
  } catch (e) {
    toggle.checked = !enabled;
    if (e.message !== '認証エラー') {
      _showResult(resultEl, '通信エラーが発生しました', 'error');
    }
  } finally {
    toggle.disabled = false;
  }
});

// ──────────────────────────────────────────────
// 起動時処理
// ──────────────────────────────────────────────

/**
 * ページロード時に localStorage にキーがあれば自動ログイン検証する
 */
async function _init() {
  if (!_adminKey) {
    _showLogin();
    return;
  }

  // 既存キーを検証
  try {
    const resp = await fetch('/api/admin/auth-check', {
      method: 'POST',
      headers: { 'X-Admin-Key': _adminKey, 'Content-Type': 'application/json' },
    });
    if (resp.ok) {
      _showMain();
    } else {
      // キーが無効 → ログイン画面へ
      _adminKey = '';
      _storageRemove(STORAGE_KEY);
      _showLogin();
    }
  } catch {
    // ネットワークエラーはそのままログイン画面を表示
    _showLogin();
  }
}

_init();
