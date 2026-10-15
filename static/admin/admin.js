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
    if (tabName === 'rejected') _loadRejected();
    if (tabName === 'settings') _loadSettings();
  });
});

// ──────────────────────────────────────────────
// 再読み込みボタン
// ──────────────────────────────────────────────

document.getElementById('reload-pending-btn').addEventListener('click', _loadPending);
document.getElementById('reload-approved-btn').addEventListener('click', _loadApproved);
document.getElementById('reload-rejected-btn').addEventListener('click', _loadRejected);
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
  const rejectBtn = document.getElementById('bulk-reject-btn');
  const allCheck = document.getElementById('bulk-select-all');
  const checkboxes = document.querySelectorAll('#pending-list .card-row__select');
  const checked = document.querySelectorAll('#pending-list .card-row__select:checked');

  const total = checkboxes.length;
  const count = checked.length;

  btn.textContent = `選択を一括承認（${count}件）`;
  btn.disabled = count === 0;
  rejectBtn.textContent = `選択を一括却下（${count}件）`;
  rejectBtn.disabled = count === 0;

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

// 一度に処理できる件数の上限（サーバー側の bulk-approve/bulk-reject と同じ上限）
const BULK_LIMIT = 500;

// 全選択チェックボックス
document.getElementById('bulk-select-all').addEventListener('change', (e) => {
  const checkboxes = document.querySelectorAll('#pending-list .card-row__select');

  if (e.target.checked && checkboxes.length > BULK_LIMIT) {
    alert(`一度に処理できるのは${BULK_LIMIT}件までです。絞り込んでから実行してください。`);
    e.target.checked = false;
    return;
  }

  const checked = e.target.checked;
  checkboxes.forEach((cb) => {
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
      let message =
        `${data.approved}件を承認しました。\n` +
        '画像は取り込みに数秒〜十数秒かかります。少し待ってから再読み込みしてください。';
      if (data.release_date_backfilled > 0) {
        message += `\n発売日を${data.release_date_backfilled}件補完しました。`;
      }
      if (data.release_date_warnings && data.release_date_warnings.length > 0) {
        message += '\n⚠ 発売日に注意が必要なカード:\n';
        message += data.release_date_warnings
          .map((w) => `ID ${w.id} ${w.name}（${w.reason}）`)
          .join('\n');
      }
      alert(message);
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

// 一括却下ボタン
document.getElementById('bulk-reject-btn').addEventListener('click', async () => {
  const checked = document.querySelectorAll('#pending-list .card-row__select:checked');
  const ids = Array.from(checked).map((cb) => {
    return Number(cb.closest('.card-row').dataset.cardId);
  });

  if (ids.length === 0) return;

  if (ids.length > BULK_LIMIT) {
    alert(`一度に処理できるのは${BULK_LIMIT}件までです。絞り込んでから実行してください。`);
    return;
  }

  // confirm ダイアログに対象カード名の先頭数件を表示する
  const namePreviewCount = 3;
  const names = Array.from(checked)
    .slice(0, namePreviewCount)
    .map((cb) => cb.closest('.card-row').querySelector('.card-row__name').textContent);
  const namesLabel = names.join('、') + (ids.length > namePreviewCount ? ' 他' : '');
  if (!confirm(`以下を含む${ids.length}件を却下します: ${namesLabel}\nよろしいですか？`)) return;

  const btn = document.getElementById('bulk-reject-btn');
  btn.disabled = true;
  btn.textContent = '処理中...';

  try {
    const resp = await apiFetch('/api/admin/unreleased/bulk-reject', {
      method: 'POST',
      body: JSON.stringify({ ids }),
    });
    const data = await resp.json().catch(() => ({}));

    if (resp.ok) {
      const skippedCount = Array.isArray(data.skipped) ? data.skipped.length : (data.skipped || 0);
      let message = `${data.rejected}件却下しました。`;
      if (skippedCount > 0) {
        message = `${data.rejected}件却下、${skippedCount}件は対象外（既に承認済み等）でした。`;
      }
      alert(message);
      await _loadPending();
    } else {
      alert(data.error || '一括却下に失敗しました');
    }
  } catch (e) {
    if (e.message !== '認証エラー') {
      alert('通信エラーが発生しました');
    }
  } finally {
    // _loadPending() が失敗した場合でも必ずバー状態を更新し、
    // ボタンが「処理中...」のまま固着しないようにする
    _updateBulkApproveBar();
  }
});

/**
 * 承認待ちカード一覧を描画する
 * @param {HTMLElement} listEl
 * @param {Array} cards
 */
/**
 * 自動承認の判定の札を描く（表示のみ。承認操作には影響しない）。
 * 理由の日本語ラベルはサーバーが返す auto_approve_reason_labels を使う。
 * 値は textContent だけで設定する（innerHTML 不使用）。
 * @param {HTMLElement|null} el
 * @param {{auto_approve_verdict?: {eligible: boolean, reasons: string[]}|null, auto_approve_reason_labels?: string[]}} card
 */
function _renderAutoVerdict(el, card) {
  if (!el) return;
  const verdict = card.auto_approve_verdict;
  if (!verdict || typeof verdict.eligible !== 'boolean') return;  // 旧データ・手動登録は札なし
  if (verdict.eligible) {
    el.textContent = '自動承認候補';
    el.classList.add('auto-verdict-badge--eligible');
  } else {
    const labels = Array.isArray(card.auto_approve_reason_labels) ? card.auto_approve_reason_labels : [];
    el.textContent = '自動承認不可: ' + (labels.length ? labels.join(' / ') : '理由不明');
    el.classList.add('auto-verdict-badge--ineligible');
  }
  el.hidden = false;
}

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

    // 自動承認の判定（シャドーモード: 表示のみ）
    _renderAutoVerdict(row.querySelector('.auto-verdict-badge'), card);

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

    // 要確認カードの紐付け候補（概要行と編集フォームの間に挿入）
    _attachReviewBlock(row, card, _loadPending);

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
      // 自動クロップ済みなら控えめな注記を出す
      const autoNote = sourceImageWrap.querySelector('.edit-source-image__auto-note');
      if (autoNote && card.crop_state && card.crop_state.mode === 'auto') autoNote.hidden = false;
    }
    _wireMediaTabs(editForm);
    // 抽出元画像があればそれを、無ければプロキシ表示を初期表示にする
    _setActiveMedia(editForm, hasSource ? 'source' : 'proxy');
    _showRecognitionEvidence(card, editForm);

    // 抽出元画像がある場合のみクロップUIを配線
    if (hasSource) {
      _setupCropUI(card, editForm);
    }

    // プレビュー初期描画
    const slot = editForm.querySelector('.edit-preview__slot');
    _renderPreview(slot, card);

    // 編集フォームのリアルタイムプレビュー更新
    fields.forEach((input) => {
      input.addEventListener('input', () => {
        _showRecognitionEvidence(card, editForm);
        const current = _collectFormData(editForm);
        _renderPreview(slot, { ...card, ...current });
      });
    });

    // 編集フォーム開閉
    const toggleEditBtn = row.querySelector('.action-toggle-edit');
    toggleEditBtn.addEventListener('click', () => {
      const isHidden = editForm.hidden;
      editForm.hidden = !isHidden;
      toggleEditBtn.textContent = isHidden ? '編集を閉じる' : '編集';
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

// review_candidates.reason の日本語ラベル
const REVIEW_REASON_LABELS = {
  near_match: '発売済みの1字違いのカード名があります',
  multiple_exact: '同じ名前の発売済みカードが複数あります',
  release_passed_no_match: '発売日を過ぎましたが発売済みカードと一致しません',
};

/**
 * 「要確認」表示の対象か判定する。
 *  - approved: review_candidates があり、dismissed_at が無いもの（公開中タブ）
 *  - needs_review: review_candidates があるもの（旧方式で作られた既存データ向け。現行の照合は
 *    status を変えないため新規には発生しない）
 * @param {Object} card
 * @returns {boolean}
 */
function _needsReview(card) {
  const review = card.review_candidates;
  if (!review) return false;
  if (card.status === 'needs_review') return true;
  return card.status === 'approved' && !review.dismissed_at;
}

/**
 * 要確認カードの理由ラベルと紐付け候補の表示ブロックを作る。対象外なら null。
 * 文字列は必ず _escapeHtml を通す。
 * @param {Object} card
 * @returns {HTMLElement|null}
 */
function _buildReviewBlock(card) {
  if (!_needsReview(card)) return null;
  const review = card.review_candidates;

  const reasonLabel = REVIEW_REASON_LABELS[review.reason] || '要確認です';
  const candidates = Array.isArray(review.candidates) ? review.candidates : [];
  const total = Number(review.total_candidates) || candidates.length;
  const rest = total - candidates.length;

  const candidatesHtml = candidates.map((c) => {
    const konamiId = Number(c.konami_id);
    return `
      <li class="review-candidates__item">
        <span class="review-candidates__name">${_escapeHtml(c.name)}</span>
        <span class="review-candidates__id">ID ${_escapeHtml(konamiId)}</span>
        <button class="btn btn-primary btn-sm action-link-candidate"
                data-konami-id="${_escapeHtml(konamiId)}"
                data-official-name="${_escapeHtml(c.name)}">このカードに紐付け</button>
      </li>`;
  }).join('');

  const restHtml = rest > 0
    ? `<div class="review-candidates__rest">ほか ${_escapeHtml(rest)} 件（上位${_escapeHtml(candidates.length)}件のみ表示）</div>`
    : '';
  // 公開中（approved）のカードだけ「確認済み」を選べる
  // multiple_exact は同名の発売済みカードが既にあるため「まだ未発売」にはできない（紐付けのみ）
  const dismissHtml = (card.status === 'approved' && review.reason !== 'multiple_exact')
    ? '<button class="btn btn-ghost btn-sm action-review-dismiss">確認済み（まだ未発売）</button>'
    : '';

  const el = document.createElement('div');
  el.className = 'review-candidates';
  el.innerHTML = `
    <div class="review-candidates__head">
      <span class="review-candidates__badge">要確認</span>
      <span class="review-candidates__reason">${_escapeHtml(reasonLabel)}</span>
      ${dismissHtml}
    </div>
    ${candidates.length ? `<ul class="review-candidates__list">${candidatesHtml}</ul>` : ''}
    ${restHtml}`;
  return el;
}

/**
 * 行の概要行の直後に要確認ブロックを挿入し、ボタンを配線する。
 * @param {HTMLElement} row
 * @param {Object} card
 * @param {Function} reload  操作成功後に呼ぶ一覧再読込関数
 */
function _attachReviewBlock(row, card, reload) {
  const reviewEl = _buildReviewBlock(card);
  if (!reviewEl) return;
  row.querySelector('.card-row__summary').after(reviewEl);
  reviewEl.querySelectorAll('.action-link-candidate').forEach((btn) => {
    btn.addEventListener('click', () => {
      _linkCandidate(card, Number(btn.dataset.konamiId), btn.dataset.officialName, reload);
    });
  });
  const dismissBtn = reviewEl.querySelector('.action-review-dismiss');
  if (dismissBtn) {
    dismissBtn.addEventListener('click', () => _dismissReview(card, reload));
  }
}

/**
 * 要確認カードを候補の正式カードに紐付ける。成功したら一覧を再読込する。
 * @param {Object} card  登録名を持つ未発売カード
 * @param {number} konamiId
 * @param {string} officialName  紐付け先の正式名
 * @param {Function} reload
 */
async function _linkCandidate(card, konamiId, officialName, reload) {
  if (!confirm(`「${card.name}」を「${officialName}」に紐付けます。よろしいですか？`)) return;
  try {
    const resp = await apiFetch(`/api/admin/unreleased/${card.id}/link`, {
      method: 'POST',
      body: JSON.stringify({ konami_id: konamiId }),
    });
    if (resp.ok) {
      await reload();
    } else {
      const data = await resp.json().catch(() => ({}));
      alert(data.error || '紐付けに失敗しました');
    }
  } catch (e) {
    if (e.message !== '認証エラー') alert('通信エラーが発生しました');
  }
}

/**
 * 「確認済み（まだ未発売）」にする。以後、自動照合は印を付け直さない。
 */
async function _dismissReview(card, reload) {
  if (!confirm(`「${card.name}」を確認済み（まだ未発売）にします。以後、自動照合は要確認の印を付けません。`)) return;
  try {
    const resp = await apiFetch(`/api/admin/unreleased/${card.id}/review-dismiss`, { method: 'POST' });
    if (resp.ok) {
      await reload();
    } else {
      const data = await resp.json().catch(() => ({}));
      alert(data.error || '確認済みの記録に失敗しました');
    }
  } catch (e) {
    if (e.message !== '認証エラー') alert('通信エラーが発生しました');
  }
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
      const data = await resp.json().catch(() => ({}));
      row.remove();
      // release_date ガードの補完・警告メッセージを表示（補完成功かつ過去日のような両立ケースもあるため連結する）
      const guardMessages = [];
      if (data.release_date_note) guardMessages.push(data.release_date_note);
      if (data.release_date_warning) guardMessages.push(`⚠ ${data.release_date_warning}`);
      if (guardMessages.length > 0) alert(guardMessages.join('\n'));
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

/**
 * 公開中カードを承認待ちに戻す（やり直し用）。
 * レコード・画像は保持されるので、承認待ちタブで編集・再クロップ・再承認できる。
 */
async function _unpublishCard(cardId, row) {
  if (!confirm(`ID ${cardId} のカードを承認待ちに戻しますか？\n（承認待ちタブで編集・再クロップ・再承認できます）`)) return;
  try {
    const resp = await apiFetch(`/api/admin/unreleased/${cardId}/unpublish`, { method: 'POST' });
    if (resp.ok) {
      row.remove();
    } else {
      const data = await resp.json().catch(() => ({}));
      alert(data.error || '承認待ちに戻す処理に失敗しました');
    }
  } catch (e) {
    if (e.message !== '認証エラー') alert('通信エラーが発生しました');
  }
}

/**
 * 公開中カードを却下する（ブロックリスト入り）。
 * 自動巡回でも今後取り込まれなくなる旨を確認文言で明示する。
 */
async function _rejectApprovedCard(cardId, row) {
  if (!confirm(
    `ID ${cardId} のカードを却下しますか？\n` +
    '却下するとブロックリストに入り、自動巡回でも今後取り込まれなくなります。\n' +
    '（一時的に下げてやり直したい場合は「承認待ちに戻す」を使ってください）'
  )) return;
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

  // 要確認カードを先頭に並べ（元の順序は維持）、タブ上部に件数を出す
  const flagged = cards.filter(_needsReview);
  cards = [...flagged, ...cards.filter((c) => !_needsReview(c))];
  if (flagged.length > 0) {
    const summaryEl = document.createElement('div');
    summaryEl.className = 'review-summary';
    summaryEl.textContent = `要確認 ${flagged.length}件`;
    listEl.appendChild(summaryEl);
  }

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

    // 画像ありバッジ / 画像なし警告バッジ
    if (card.has_image) {
      row.querySelector('.card-row__has-image').hidden = false;
    } else {
      // 承認済みなのに公式画像が未登録 → 警告バッジを表示
      row.querySelector('.card-row__no-image').hidden = false;
      // 抽出元画像URLがあればワンクリック取込ボタンを有効化する
      // （URLが無いカードは手動登録等なので、従来どおり「画像取込」フォームで対応）
      const quickBtn = row.querySelector('.action-quick-fetch-image');
      if (card.card_image_url) {
        quickBtn.hidden = false;
        quickBtn.addEventListener('click', () => {
          _quickFetchImage(card, row, quickBtn);
        });
      }
    }

    // 要確認カードの紐付け候補・確認済みボタン
    _attachReviewBlock(row, card, _loadApproved);

    // 非表示トグルボタン
    const toggleHiddenBtn = row.querySelector('.action-toggle-hidden');
    toggleHiddenBtn.textContent = card.hidden ? '再表示する' : '非表示にする';
    if (card.hidden) {
      row.style.opacity = '0.5';
    }
    toggleHiddenBtn.addEventListener('click', () => {
      _toggleHidden(card.id, row, toggleHiddenBtn);
    });

    // 承認待ちに戻すボタン（やり直し用：レコード・画像は保持）
    row.querySelector('.action-unpublish').addEventListener('click', () => {
      _unpublishCard(card.id, row);
    });

    // 却下ボタン（ブロックリスト入り：自動巡回でも今後取り込まれない）
    row.querySelector('.action-reject-approved').addEventListener('click', () => {
      _rejectApprovedCard(card.id, row);
    });

    // 画像取込フォーム開閉ボタン
    const imageForm = row.querySelector('.card-row__image-form');
    const toggleImageFormBtn = row.querySelector('.action-toggle-image-form');
    toggleImageFormBtn.addEventListener('click', () => {
      const isHidden = imageForm.hidden;
      imageForm.hidden = !isHidden;
      toggleImageFormBtn.textContent = isHidden ? '画像を閉じる' : '画像取込';
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
      toggleEditBtn.textContent = isHidden ? '編集を閉じる' : '編集';
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

/**
 * 抽出元画像URL（card.card_image_url）をワンクリックで取り込む（公開中タブ）。
 * URLを貼り直す手間なく、承認済みなのに画像が無いカードを即復旧するための導線。
 * 取込ロジック自体は _fetchImageByUrl と同じ fetch-image エンドポイントを使う。
 * @param {Object} card  一覧APIのカードレコード（card_image_url を含む）
 * @param {HTMLElement} row
 * @param {HTMLElement} btn  クリックされた取込ボタン
 */
async function _quickFetchImage(card, row, btn) {
  if (!card.card_image_url) return;
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = '取込中…';
  try {
    const resp = await apiFetch(`/api/admin/unreleased/${card.id}/fetch-image`, {
      method: 'POST',
      body: JSON.stringify({ image_url: card.card_image_url }),
    });
    const data = await resp.json().catch(() => ({}));
    if (resp.ok && data.ok) {
      // 画像なし → 画像あり にバッジを切り替え、取込ボタンを隠す
      row.querySelector('.card-row__has-image').hidden = false;
      row.querySelector('.card-row__no-image').hidden = true;
      btn.hidden = true;
    } else {
      alert(data.error || '取込に失敗しました');
      btn.disabled = false;
      btn.textContent = original;
    }
  } catch (e) {
    if (e.message !== '認証エラー') alert('通信エラーが発生しました');
    btn.disabled = false;
    btn.textContent = original;
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
// 却下済みタブ
// ──────────────────────────────────────────────

async function _loadRejected() {
  const listEl = document.getElementById('rejected-list');
  listEl.innerHTML = '<p class="loading-text">読み込み中...</p>';

  try {
    const resp = await apiFetch('/api/admin/unreleased?status=rejected');
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      listEl.innerHTML = `<p class="loading-text">エラー: ${data.error || resp.status}</p>`;
      return;
    }
    const { cards } = await resp.json();
    _renderRejectedList(listEl, cards);
  } catch (e) {
    if (e.message !== '認証エラー') {
      listEl.innerHTML = '<p class="loading-text">読み込みに失敗しました。</p>';
    }
  }
}

/**
 * 却下済みカード一覧を描画する
 * @param {HTMLElement} listEl
 * @param {Array} cards
 */
function _renderRejectedList(listEl, cards) {
  listEl.innerHTML = '';

  if (!cards || cards.length === 0) {
    listEl.innerHTML = '<p class="empty-text">却下済みのカードはありません。</p>';
    return;
  }

  const tmpl = document.getElementById('tmpl-rejected-row');

  for (const card of cards) {
    const row = tmpl.content.cloneNode(true).querySelector('.card-row');
    row.dataset.cardId = card.id;

    row.querySelector('.card-row__name').textContent    = card.name;
    row.querySelector('.card-row__product').textContent = card.product_name || '';
    row.querySelector('.card-row__type').textContent    = card.card_type || '';
    row.querySelector('.card-row__extracted-at').textContent = _formatDate(card.extracted_at);

    // 抽出元リンク
    const link = row.querySelector('.card-row__source-link');
    if (card.source_url && card.source_url !== 'manual') {
      link.href = card.source_url;
      link.title = card.source_url;
    } else {
      link.textContent = '手動登録';
      link.removeAttribute('href');
    }

    // 承認待ちに戻すボタン
    row.querySelector('.action-restore').addEventListener('click', () => {
      _restoreCard(card.id, row);
    });

    listEl.appendChild(row);
  }
}

/**
 * 却下済みカードを承認待ちに戻す。
 * @param {number} cardId
 * @param {HTMLElement} row
 */
async function _restoreCard(cardId, row) {
  if (!confirm(`ID ${cardId} のカードを承認待ちに戻しますか？`)) return;
  try {
    const resp = await apiFetch(`/api/admin/unreleased/${cardId}/restore`, { method: 'POST' });
    if (resp.ok) {
      row.remove();
    } else {
      const data = await resp.json().catch(() => ({}));
      alert(data.error || '承認待ちに戻す処理に失敗しました');
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
// クロップモーダル（グローバル・全画面）
// ──────────────────────────────────────────────

const _cropModal = {
  el:         null,
  img:        null,
  overlay:    null,
  selEl:      null,
  confirmBtn: null,
  cancelBtn:  null,
  resultEl:   null,

  // 現在開いているカードの情報
  _cardId:   null,
  _editForm: null,
  _dragging: false,
  _startX:   0,
  _startY:   0,
  _sel:      null,
  _revision: 0,
  _suggesting: false,
  _busy: false,   // クロップ確定・元画像に戻す の処理中
  _imgSrc: '',

  init() {
    this.el         = document.getElementById('crop-modal');
    this.img        = this.el.querySelector('.crop-modal__img');
    this.overlay    = this.el.querySelector('.crop-modal__overlay');
    this.selEl      = this.el.querySelector('.crop-modal__selection');
    this.confirmBtn = this.el.querySelector('.crop-modal__confirm');
    this.cancelBtn  = this.el.querySelector('.crop-modal__cancel');
    this.resultEl   = this.el.querySelector('.crop-modal__result');
    this.suggestBtn = this.el.querySelector('.crop-modal__suggest');
    this.restoreBtn = this.el.querySelector('.crop-modal__restore');
    this.restoreBtn.addEventListener('click', () => this._submit('restore'));
    this.suggestBtn.addEventListener('click', () => this._suggest());

    this.cancelBtn.addEventListener('click', () => this.close());
    this.el.querySelector('.crop-modal__backdrop').addEventListener('click', () => this.close());

    this.overlay.addEventListener('mousedown', (e) => this._onDown(e));
    document.addEventListener('mousemove',     (e) => this._onMove(e));
    document.addEventListener('mouseup',       (e) => this._onUp(e));

    this.confirmBtn.addEventListener('click', () => this._submit('crop'));
  },

  open(cardId, editForm, imgSrc, suggestion = null) {
    this._revision++;
    this._imgSrc = imgSrc;
    this._cardId   = cardId;
    this._editForm = editForm;
    this._sel      = null;
    this._dragging = false;
    this.img.src   = imgSrc;
    this.selEl.hidden      = true;
    this.resultEl.hidden   = true;
    this.el.hidden         = false;
    document.body.style.overflow = 'hidden';
    const candidate = _validatedCropSuggestion(suggestion, imgSrc);
    const hint = this.el.querySelector('.crop-modal__hint');
    hint.textContent = candidate
      ? '自動検出した候補枠です。欠け・余白を確認し、必要ならドラッグで選び直して確定してください'
      : 'ドラッグしてカード範囲を選択してください';
    if (candidate) {
      this._sel = candidate;
      this._updateSel();
    }
    this._refreshButtons();
  },

  // 確定・元画像に戻す・自動で枠を提案 の有効/無効を状態から一括で決める
  _refreshButtons() {
    const s = this._sel;
    const big = !!s && (s.right - s.left) > 0.02 && (s.bottom - s.top) > 0.02;
    this.confirmBtn.disabled = this._busy || this._dragging || !big;
    this.restoreBtn.disabled = this._busy;
    this.suggestBtn.disabled = this._busy || this._suggesting;
  },

  close() {
    this._revision++;
    this.el.hidden = true;
    document.body.style.overflow = '';
  },

  _relPos(e) {
    const rect = this.overlay.getBoundingClientRect();
    const cx = e.clientX;
    const cy = e.clientY;
    return {
      x: Math.max(0, Math.min(1, (cx - rect.left)  / rect.width)),
      y: Math.max(0, Math.min(1, (cy - rect.top) / rect.height)),
    };
  },

  _updateSel() {
    const s = this._sel;
    if (!s) return;
    this.selEl.style.left   = `${s.left   * 100}%`;
    this.selEl.style.top    = `${s.top    * 100}%`;
    this.selEl.style.width  = `${(s.right  - s.left)   * 100}%`;
    this.selEl.style.height = `${(s.bottom - s.top) * 100}%`;
    this.selEl.hidden = false;
  },

  _onDown(e) {
    if (this.el.hidden || this._busy) return;
    this._revision++;
    const pos = this._relPos(e);
    this._startX   = pos.x;
    this._startY   = pos.y;
    this._dragging = true;
    this._refreshButtons();
    e.preventDefault();
  },

  _onMove(e) {
    if (!this._dragging) return;
    const pos = this._relPos(e);
    this._sel = {
      left:   Math.min(this._startX, pos.x),
      top:    Math.min(this._startY, pos.y),
      right:  Math.max(this._startX, pos.x),
      bottom: Math.max(this._startY, pos.y),
    };
    this._updateSel();
  },

  _onUp() {
    if (!this._dragging) return;
    this._dragging = false;
    this._refreshButtons();
  },

  // 編集フォームの画像・リンクを差し替え、自動クロップ注記を消す（応答時点の編集フォームではなく、送信時のフォームに対して行う）
  _applyNewImage(editForm, newUrl) {
    if (!editForm) return;
    const imgEl  = editForm.querySelector('.edit-source-image__img');
    if (imgEl && newUrl) {
      imgEl.src = newUrl;
      const linkEl = editForm.querySelector('.edit-source-image__link');
      if (linkEl) linkEl.href = newUrl;
    }
    const note = editForm.querySelector('.edit-source-image__auto-note');
    if (note) note.hidden = true;
  },

  /**
   * クロップ確定（kind='crop'）／元画像に戻す（kind='restore'）を送信する。
   * 開始時に revision・editForm・cardId を退避し、応答時に照合する（_suggest と同じ流儀）。
   * サーバー側は既に保存済みなので画像の差し替えは送信時のフォームへ常に行うが、
   * モーダルを閉じる・エラー表示は、モーダルが同じ状態のままのときだけ行う。
   * 処理中は 確定・元画像に戻す・自動で枠を提案 をまとめて無効化する。
   */
  async _submit(kind) {
    if (this._busy || this.el.hidden || !this._cardId) return;
    if (kind === 'crop' && !this._sel) return;
    const isCrop = kind === 'crop';
    const revision = this._revision;
    const editForm = this._editForm;
    const cardId = this._cardId;
    const btn = isCrop ? this.confirmBtn : this.restoreBtn;
    const idleLabel = isCrop ? '確定' : '元画像に戻す';
    const failMsg = isCrop ? 'クロップに失敗しました' : '元画像に戻せませんでした';
    const path = isCrop ? 'crop-image' : 'restore-original-image';
    const body = isCrop ? { ...this._sel } : {};
    this._busy = true;
    this._refreshButtons();
    btn.textContent = '処理中...';
    try {
      const resp = await apiFetch(`/api/admin/unreleased/${cardId}/${path}`, {
        method: 'POST',
        body: JSON.stringify(body),
      });
      const data = await resp.json().catch(() => ({}));
      const current = !this.el.hidden && this._revision === revision && this._cardId === cardId;
      if (resp.ok && data.ok) {
        this._applyNewImage(editForm, data.new_url || '');
        if (current) this.close();
      } else if (current) {
        _showResult(this.resultEl, data.error || failMsg, 'error');
      }
    } catch (e) {
      const current = !this.el.hidden && this._revision === revision && this._cardId === cardId;
      if (current && e.message !== '認証エラー') {
        _showResult(this.resultEl, '通信エラーが発生しました', 'error');
      }
    } finally {
      this._busy = false;
      btn.textContent = idleLabel;
      this._refreshButtons();
    }
  },

  async _suggest() {
    if (this._suggesting || this._busy || this.el.hidden) return;
    this._suggesting = true;
    this._refreshButtons();
    this.suggestBtn.textContent = '候補を取得中...';
    this.resultEl.hidden = true;
    const revision = this._revision;
    const cardId = this._cardId;
    try {
      const resp = await apiFetch(`/api/admin/unreleased/${cardId}/suggest-crop`, {
        method: 'POST', body: JSON.stringify({}),
      });
      const data = await resp.json();
      // 閉じた後・カード移動後・手動選択開始後には古い応答を反映しない。
      if (this.el.hidden || this._revision !== revision || this._cardId !== cardId) return;
      const candidate = _validatedCropSuggestion(data.crop_suggestion, this._imgSrc);
      if (!resp.ok || !data.ok || !candidate) {
        _showResult(this.resultEl, data.error || '元画像と一致する候補枠を取得できませんでした', 'error');
        return;
      }
      this._sel = candidate;
      this._updateSel();
      this._refreshButtons();
      this.el.querySelector('.crop-modal__hint').textContent = '自動検出した候補枠です。欠け・余白を確認し、必要ならドラッグで選び直して確定してください';
    } catch (e) {
      if (!this.el.hidden && this._revision === revision && e.message !== '認証エラー') {
        _showResult(this.resultEl, '候補枠を取得できませんでした。手動で範囲を選択するか再試行してください', 'error');
      }
    } finally {
      this._suggesting = false;
      this._refreshButtons();
      this.suggestBtn.textContent = '自動で枠を提案';
    }
  },
};

_cropModal.init();

function _validatedCropSuggestion(suggestion, imgSrc) {
  if (!suggestion || !imgSrc || suggestion.source_image_url !== imgSrc) return null;
  const {left, top, right, bottom} = suggestion;
  if (![left, top, right, bottom].every(v => typeof v === 'number' && Number.isFinite(v))) return null;
  if (!(0 <= left && left < right && right <= 1 && 0 <= top && top < bottom && bottom <= 1)) return null;
  return {left, top, right, bottom};
}

// 発売日の照合 reason → 管理者向けの短い補足
const _DATE_REASON_HINTS = {
  '商品名が未指定': '収録商品が空欄です',
  '公式商品一覧に一致する商品がない（付録・特典等は手動確認）': '公式の商品一覧に同じ名前の商品がありません（雑誌付録・特典などは手動で確認してください）',
  '一致商品の独立した公式詳細ページがない': '公式に商品の詳細ページがありません',
  '公式商品一覧に発売日が完全な年月日で載っていない（詳細ページ未公開）': '公式の商品一覧にまだ発売日が載っていません',
  '同名商品が複数あり詳細ページを一意に選べない': '公式に同じ名前の商品が複数あります',
  '商品名が一致するheader内に発売日の根拠がない': '公式の商品ページに発売日の記載が見つかりません',
};

function _dateReasonHint(reason) {
  if (typeof reason !== 'string') return '';
  if (Object.hasOwn(_DATE_REASON_HINTS, reason)) return _DATE_REASON_HINTS[reason];
  if (reason.startsWith('公式発売日の取得・検証失敗')) return '公式サイトの取得に失敗しました';
  return '';
}

// 公式商品ページへの小リンク（https・www.yugioh-card.com・/japan/products/ のみ許可）
function _officialDateLink(date) {
  if (!date || typeof date.source_url !== 'string') return null;
  try {
    const url = new URL(date.source_url);
    if (url.protocol === 'https:' && url.hostname === 'www.yugioh-card.com'
        && !url.username && !url.password && !url.port
        && url.pathname.startsWith('/japan/products/')) {
      const link = document.createElement('a');
      link.href = url.href;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.className = 'recognition-evidence__link';
      link.textContent = '公式商品情報で発売日を確認';
      return link;
    }
  } catch (_) { /* 不正な参照URLはリンクとして表示しない。 */ }
  return null;
}

function _showRecognitionEvidence(card, editForm) {
  const panel = editForm.querySelector('.recognition-evidence');
  if (!panel) return;
  panel.replaceChildren();
  const date = card.release_date_evidence && typeof card.release_date_evidence === 'object'
    ? card.release_date_evidence : null;
  const recognition = card.recognition_review && typeof card.recognition_review === 'object'
    ? card.recognition_review : null;
  const current = key => {
    const input = editForm.querySelector(`[name="${key}"]`);
    return input && input.value !== undefined ? input.value : card[key];
  };
  const normalized = value => value == null ? '' : String(value);

  // 手動変更の判定（抽出時の根拠を現在値の保証として扱わない）
  let dateState = null;
  let dateChanged = false;
  // 記事内の日付を補助採用した場合、抽出時の値は公式値ではなく candidate_date
  const adoptedFromArticle = !!date && date.adopted_from === 'article'
    && typeof date.candidate_date === 'string' && date.candidate_date !== '';
  if (date) {
    const extractedDate = adoptedFromArticle ? date.candidate_date : date.release_date;
    dateChanged = !Object.hasOwn(date, 'product_name')
      || normalized(current('product_name')) !== normalized(date.product_name)
      || normalized(current('release_date')) !== normalized(extractedDate);
    dateState = dateChanged ? 'manual' : (date.status === 'verified' ? 'ok' : 'review');
  }
  let recState = null;
  let isMonster = false;
  if (recognition) {
    const values = recognition.values || {};
    const recChanged = !recognition.values
      || normalized(current('card_type')) !== normalized(recognition.card_type)
      || ['level', 'rank', 'link_val'].some(key => normalized(current(key)) !== normalized(values[key]));
    recState = recChanged ? 'manual' : (recognition.status === 'verified' ? 'ok' : 'review');
    isMonster = String(recognition.card_type || '').includes('モンスター');
  }

  const addLine = (className, text) => {
    const line = document.createElement('p');
    line.className = `recognition-evidence__line ${className}`;
    line.textContent = text;
    panel.appendChild(line);
    return line;
  };

  // 照合済み（1行にまとめる）
  const okItems = [];
  if (dateState === 'ok') okItems.push('発売日（公式商品ページと一致）');
  if (recState === 'ok') okItems.push('レベル・ランク・リンク値（画像から再確認）');
  if (okItems.length) {
    const line = document.createElement('p');
    line.className = 'recognition-evidence__line recognition-evidence__line--ok';
    const mark = document.createElement('span');
    mark.className = 'recognition-evidence__mark';
    mark.textContent = '✓';
    line.appendChild(mark);
    line.appendChild(document.createTextNode(` 照合済み：${okItems.join('・')}`));
    if (dateState === 'ok') {
      const link = _officialDateLink(date);
      if (link) {
        line.appendChild(document.createTextNode(' '));
        line.appendChild(link);
      }
    }
    panel.appendChild(line);
  }

  // 手動変更
  if (dateState === 'manual') {
    addLine('recognition-evidence__line--manual',
      '発売日・収録商品は手動で変更されています（抽出時の照合結果は現在の値には当てはまりません）');
  }
  if (recState === 'manual') {
    addLine('recognition-evidence__line--manual',
      'カード種別・レベル等は手動で変更されています（抽出時の照合結果は現在の値には当てはまりません）');
  }

  // 要確認
  if (dateState === 'review') {
    let text;
    if (adoptedFromArticle) {
      text = `⚠ 発売日は記事内の日付です（公式未確認）：${date.candidate_date}`;
    } else if (date.status === 'conflict') {
      text = '⚠ 公式情報の中で発売日が食い違っています';
    } else {
      text = '⚠ 発売日を公式サイトで確認できませんでした';
      const hint = _dateReasonHint(date.reason);
      if (hint) text += `：${hint}`;
    }
    if (typeof date.candidate_date === 'string' && date.candidate_date
        && normalized(current('release_date')) === '') {
      text += `（投稿内の日付：${date.candidate_date}）`;
    }
    const line = addLine('recognition-evidence__line--review', text);
    const link = _officialDateLink(date);
    if (link) {
      line.appendChild(document.createTextNode(' '));
      line.appendChild(link);
    }
  }
  if (recState === 'review') {
    addLine('recognition-evidence__line--review', isMonster
      ? '⚠ レベル・ランク・リンク値を画像から確定できませんでした。画像で確認してください'
      : '⚠ カード種別を画像から確定できませんでした。画像で確認してください');
  }
  panel.hidden = panel.childElementCount === 0;
}

/**
 * クロップボタンをモーダル開閉に配線する。
 * クロップは常にオリジナル画像（source_image_url）に対して行う必要があるため、
 * 編集フォームの表示画像（クロップ済みの場合あり）ではなく card.source_image_url を優先して渡す。
 * @param {object} card
 * @param {HTMLElement} editForm
 */
function _setupCropUI(card, editForm) {
  const cropBtn = editForm.querySelector('.crop-btn');
  if (!cropBtn) return;
  cropBtn.addEventListener('click', () => {
    let src = card.source_image_url || '';
    if (!src) {
      // 後方互換: source_image_url が無い場合（手動取込など）は表示画像にフォールバック
      const imgEl = editForm.querySelector('.edit-source-image__img');
      src = imgEl ? imgEl.getAttribute('src') : '';
    }
    if (!src) return;
    _cropModal.open(card.id, editForm, src, card.crop_suggestion);
  });
}

// ──────────────────────────────────────────────
// Xポスト取込タブ
// ──────────────────────────────────────────────

document.getElementById('x-fetch-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const resultEl = document.getElementById('x-fetch-result');
  const btn = document.getElementById('x-fetch-btn');
  resultEl.hidden = true;

  const tweetUrl = document.getElementById('x-tweet-url').value.trim();
  const force = document.getElementById('x-fetch-force').checked;

  if (!tweetUrl) {
    _showResult(resultEl, 'XポストURLを入力してください', 'error');
    return;
  }

  btn.disabled = true;
  btn.textContent = '処理中...';

  try {
    const resp = await apiFetch('/api/admin/unreleased/fetch-x-post', {
      method: 'POST',
      body: JSON.stringify({ tweet_url: tweetUrl, force }),
    });
    const data = await resp.json().catch(() => ({}));

    if (resp.ok || resp.status === 201) {
      if (data.skipped) {
        _showResult(resultEl, `スキップ: ${data.reason}`, 'error');
      } else if (data.inserted === 0) {
        _showResult(resultEl, `抽出0件（SAMPLEウォーターマークなし、または既発売カードの可能性）${data.reason ? ` — ${data.reason}` : ''}`, 'error');
      } else {
        const names = (data.cards || []).map((c) => c.name).filter(Boolean).join('、');
        _showResult(resultEl, `${data.inserted}件取り込みました: ${names || '（名前なし）'}`, 'ok');
      }
    } else {
      _showResult(resultEl, data.error || '取り込みに失敗しました', 'error');
    }
  } catch (err) {
    if (err.message !== '認証エラー') {
      _showResult(resultEl, '通信エラーが発生しました', 'error');
    }
  } finally {
    btn.disabled = false;
    btn.textContent = '取り込む';
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
