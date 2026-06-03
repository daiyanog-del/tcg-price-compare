/**
 * neuron-preview-modal.js
 * PDFパース結果のプレビュー・確認モーダル
 * solitaire（ESM）とindex（非module経由）両ページで使用
 */

const STYLE_ID = 'neuron-preview-modal-styles';

function _injectStyles() {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement('style');
  style.id = STYLE_ID;
  style.textContent = `
    .npm-overlay {
      position: fixed; inset: 0;
      background: rgba(0,0,0,.6);
      z-index: 9999;
      display: flex; align-items: center; justify-content: center;
      padding: 16px; box-sizing: border-box;
    }
    .npm-container {
      background: #fff; border-radius: 12px;
      box-shadow: 0 8px 32px rgba(0,0,0,.3);
      display: flex; flex-direction: column;
      width: min(680px, 100%); max-height: 90vh;
      overflow: hidden;
    }
    .npm-header {
      padding: 16px 20px; border-bottom: 1px solid #e0e0e0;
      display: flex; justify-content: space-between; align-items: center;
      flex-shrink: 0;
    }
    .npm-header h2 { margin: 0; font-size: 1.1rem; color: #1a2a4a; }
    .npm-close {
      background: none; border: none; font-size: 1.6rem;
      color: #888; cursor: pointer; line-height: 1; padding: 0 4px;
    }
    .npm-close:hover { color: #333; }
    .npm-body {
      padding: 16px 20px; overflow-y: auto; flex: 1;
      display: flex; flex-direction: column; gap: 12px;
    }
    .npm-warnings {
      background: #fff8e1; border: 1px solid #ffe082;
      border-radius: 6px; padding: 8px 12px;
      font-size: .82rem; color: #7a5c00; line-height: 1.6;
    }
    .npm-section-label {
      font-size: .78rem; font-weight: 700; color: #2d3f5a;
      text-transform: uppercase; letter-spacing: .03em; margin-bottom: 4px;
    }
    .npm-textarea {
      width: 100%; box-sizing: border-box;
      border: 1px solid #cbd5e1; border-radius: 6px;
      padding: 8px 10px; font-size: .85rem; font-family: monospace;
      resize: vertical; min-height: 80px; max-height: 200px;
      line-height: 1.55; color: #1a2a4a; background: #f8fafc;
    }
    .npm-textarea:focus { outline: none; border-color: #3b82f6; background: #fff; }
    .npm-name-row {
      display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
    }
    .npm-name-label { font-size: .82rem; color: #64748b; white-space: nowrap; }
    .npm-name-input {
      flex: 1; min-width: 160px;
      border: 1px solid #cbd5e1; border-radius: 6px;
      padding: 6px 10px; font-size: .9rem; color: #1a2a4a;
    }
    .npm-name-input:focus { outline: none; border-color: #3b82f6; }
    .npm-footer {
      padding: 12px 20px; border-top: 1px solid #e0e0e0;
      display: flex; gap: 8px; justify-content: flex-end; flex-shrink: 0;
      flex-wrap: wrap;
    }
    .npm-btn {
      padding: 8px 18px; border-radius: 6px; font-size: .9rem;
      font-weight: 600; cursor: pointer; border: 2px solid;
      transition: background .15s, border-color .15s;
    }
    .npm-btn-primary {
      background: #1e40af; border-color: #1e40af; color: #fff;
    }
    .npm-btn-primary:hover { background: #1d4ed8; border-color: #1d4ed8; }
    .npm-btn-primary:disabled { background: #93c5fd; border-color: #93c5fd; cursor: default; }
    .npm-btn-secondary {
      background: #475569; border-color: #475569; color: #fff;
    }
    .npm-btn-secondary:hover { background: #334155; border-color: #334155; }
    .npm-btn-cancel {
      background: #fff; border-color: #cbd5e1; color: #475569;
    }
    .npm-btn-cancel:hover { background: #f1f5f9; }
    .npm-count { font-size: .75rem; color: #94a3b8; margin-left: 4px; }
    @media (max-width: 480px) {
      .npm-footer { flex-direction: column; }
      .npm-btn { width: 100%; text-align: center; }
    }
  `;
  document.head.appendChild(style);
}

/**
 * プレビューモーダル
 *
 * @param {Object} opts
 * @param {{main:{qty,name}[], ex:{qty,name}[], warnings:string[], ok:boolean}} opts.parsed
 *        parseNeuronPdfの戻り値
 * @param {string} opts.defaultName  デフォルトのデッキ名
 * @param {function({name:string, mainText:string, exText:string}):void} [opts.onSave]
 *        保存ボタン押下時のコールバック
 * @param {function({mainText:string, exText:string}):void} [opts.onLoad]
 *        「読み込む」ボタン押下時のコールバック（solitaireのみ。省略時はボタン非表示）
 */
export class NeuronPreviewModal {
  constructor({ parsed, defaultName = '', onSave, onLoad }) {
    this.parsed = parsed;
    this.defaultName = defaultName;
    this.onSave = onSave;
    this.onLoad = onLoad;
    this._el = null;
  }

  show() {
    _injectStyles();
    const el = document.createElement('div');
    el.className = 'npm-overlay';
    el.innerHTML = this._html();
    document.body.appendChild(el);
    this._el = el;
    this._bind();
  }

  hide() {
    this._el?.remove();
    this._el = null;
  }

  _html() {
    const { parsed, defaultName } = this;
    const { main, ex, warnings, ok } = parsed;

    const mainText = main.map(e => `${e.qty} ${e.name}`).join('\n');
    const exText   = ex.map(e => `${e.qty} ${e.name}`).join('\n');
    const mainCount = main.reduce((s, e) => s + e.qty, 0);
    const exCount   = ex.reduce((s, e) => s + e.qty, 0);

    const warnHTML = warnings.length
      ? `<div class="npm-warnings">${warnings.map(w => `・${_esc(w)}`).join('<br>')}</div>`
      : '';

    const notOkMsg = !ok
      ? `<div class="npm-warnings">認識失敗: テキストを手動で入力してください。</div>`
      : '';

    const loadBtn = this.onLoad
      ? `<button class="npm-btn npm-btn-secondary" id="npmLoadBtn">読み込む（一人回し）</button>`
      : '';

    return `
      <div class="npm-container">
        <div class="npm-header">
          <h2>PDFデッキ取り込み確認</h2>
          <button class="npm-close" id="npmCloseBtn" aria-label="閉じる">&times;</button>
        </div>
        <div class="npm-body">
          ${notOkMsg}
          ${warnHTML}
          <div>
            <div class="npm-section-label">
              メインデッキ
              <span class="npm-count">${mainCount}枚</span>
            </div>
            <textarea class="npm-textarea" id="npmMainText" rows="10"
              placeholder="3 灰流うらら&#10;1 増殖するG">${_esc(mainText)}</textarea>
          </div>
          <div>
            <div class="npm-section-label">
              エクストラデッキ
              <span class="npm-count">${exCount}枚</span>
            </div>
            <textarea class="npm-textarea" id="npmExText" rows="6"
              placeholder="1 フルール・ド・バロネス">${_esc(exText)}</textarea>
          </div>
          <div class="npm-name-row">
            <span class="npm-name-label">デッキ名:</span>
            <input class="npm-name-input" id="npmDeckName" type="text"
              placeholder="デッキ名を入力" value="${_esc(defaultName)}" maxlength="60">
          </div>
        </div>
        <div class="npm-footer">
          <button class="npm-btn npm-btn-cancel" id="npmCancelBtn">キャンセル</button>
          ${loadBtn}
          <button class="npm-btn npm-btn-primary" id="npmSaveBtn">マイデッキに保存</button>
        </div>
      </div>
    `;
  }

  _bind() {
    const el = this._el;

    el.querySelector('#npmCloseBtn')?.addEventListener('click', () => this.hide());
    el.querySelector('#npmCancelBtn')?.addEventListener('click', () => this.hide());

    // オーバーレイ外クリックで閉じる
    el.addEventListener('click', (e) => {
      if (e.target === el) this.hide();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') this.hide();
    }, { once: true });

    // 保存ボタン
    el.querySelector('#npmSaveBtn')?.addEventListener('click', () => {
      const name = el.querySelector('#npmDeckName').value.trim();
      const mainText = el.querySelector('#npmMainText').value.trim();
      const exText   = el.querySelector('#npmExText').value.trim();
      if (!name) {
        el.querySelector('#npmDeckName').focus();
        return;
      }
      this.onSave?.({ name, mainText, exText });
      this.hide();
    });

    // 読み込むボタン（solitaireのみ）
    el.querySelector('#npmLoadBtn')?.addEventListener('click', () => {
      const mainText = el.querySelector('#npmMainText').value.trim();
      const exText   = el.querySelector('#npmExText').value.trim();
      this.onLoad?.({ mainText, exText });
      this.hide();
    });
  }
}

function _esc(str) {
  return (str || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
