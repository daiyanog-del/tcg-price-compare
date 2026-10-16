/**
 * cutin-disruption-picker.js
 * カットインの「相手の妨害」を選ぶ部品（PC の右パネル区画・スマホのシートで共通）。
 *   - よく使う妨害の札（config/disruption-presets.js の手札誘発・魔法・罠）。押すとその場で選ばれる
 *   - カード名の検索（/api/suggest の候補から選ぶ）
 * 選んだカード名は onPick に渡すだけ。記録への追加は呼び出し側（comment-ui.js / mobile-ui.js）が
 * replay-service の addComment({kind:'cutin', card}) で行う。
 *
 * 2026-10-16: 相手の想定妨害トレイ（盤面の上の5枠）を廃止し、カットインに一本化した。札と検索はトレイから移した。
 */

import { DISRUPTION_PRESETS } from '../config/disruption-presets.js';

/** 検索の候補を問い合わせるまでの待ち時間（ms。トレイと同じ値） */
const SUGGEST_DEBOUNCE_MS = 200;
/** 候補の最大件数（トレイと同じ値） */
const SUGGEST_MAX = 8;

/**
 * カットインの「言葉／相手の妨害」の切り替え。root の中の [data-cutin-mode] ボタンと [data-cutin-pane] 区画を
 * mode に合わせて出し分ける（押されている方を aria-pressed="true"）。
 * @param {ParentNode} root
 * @param {'words'|'disruption'} mode
 */
export function setCutinMode(root, mode) {
  root.querySelectorAll('[data-cutin-mode]').forEach((btn) => {
    btn.setAttribute('aria-pressed', String(btn.dataset.cutinMode === mode));
  });
  root.querySelectorAll('[data-cutin-pane]').forEach((pane) => {
    pane.hidden = pane.dataset.cutinPane !== mode;
  });
}

/**
 * 部品を container の中に作る。
 * @param {HTMLElement} container
 * @param {(name: string) => void} onPick  カード名を選んだとき
 * @returns {{reset: () => void, focusSearch: () => void}}  reset: 検索欄と候補を空にする
 */
export function buildDisruptionPicker(container, onPick) {
  container.replaceChildren();

  for (const group of DISRUPTION_PRESETS) {
    const label = document.createElement('p');
    label.className = 'sol-cutin-cat';
    label.textContent = group.cat;
    const chips = document.createElement('div');
    chips.className = 'sol-cutin-chips';
    for (const name of group.cards) {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'sol-cutin-chip';
      chip.textContent = name;
      chip.addEventListener('click', () => onPick(name));
      chips.appendChild(chip);
    }
    container.append(label, chips);
  }

  const wrap = document.createElement('div');
  wrap.className = 'sol-cutin-search';
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'sol-comment-input sol-cutin-search-input';
  input.placeholder = 'カード名で探す';
  input.autocomplete = 'off';
  input.setAttribute('aria-label', '相手の妨害のカード名で探す');
  const list = document.createElement('ul');
  list.className = 'sol-cutin-suggest';
  list.hidden = true;
  list.setAttribute('role', 'listbox');
  wrap.append(input, list);
  container.appendChild(wrap);

  let timer = null;
  let abort = null;

  const close = () => {
    list.hidden = true;
    list.replaceChildren();
  };
  const reset = () => {
    clearTimeout(timer);
    if (abort) abort.abort();
    input.value = '';
    close();
  };
  const pick = (name) => {
    reset();
    onPick(name);
  };
  const render = (names) => {
    list.replaceChildren();
    if (!names.length) { close(); return; }
    for (const name of names) {
      const li = document.createElement('li');
      li.className = 'sol-cutin-suggest-item';
      li.setAttribute('role', 'option');
      li.textContent = name; // 候補はサーバーの値だが、ほかと同じく textContent で入れる
      // blur より先に選ぶ（mousedown）。タッチでも pointerdown が先に来る
      li.addEventListener('pointerdown', (e) => { e.preventDefault(); pick(name); });
      list.appendChild(li);
    }
    list.hidden = false;
  };
  const fetchSuggest = async (q) => {
    if (abort) abort.abort();
    abort = new AbortController();
    try {
      const res = await fetch(`/api/suggest?q=${encodeURIComponent(q)}`, { signal: abort.signal });
      if (!res.ok) return;
      const data = await res.json();
      const names = Array.isArray(data) ? data : (data.suggestions || data.results || []);
      if (input.value.trim() !== q) return; // 打ち直した後に古い応答が来た
      render(names.filter((n) => typeof n === 'string' && n).slice(0, SUGGEST_MAX));
    } catch (err) {
      if (err?.name !== 'AbortError') console.warn('[cutin] カード名の候補の取得に失敗', err);
    }
  };

  input.addEventListener('input', () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (!q) { close(); return; }
    timer = setTimeout(() => fetchSuggest(q), SUGGEST_DEBOUNCE_MS);
  });
  input.addEventListener('blur', () => setTimeout(close, 150));
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !list.hidden) { e.preventDefault(); e.stopPropagation(); close(); return; }
    // 日本語入力の変換確定の Enter では選ばない。候補が出ていれば先頭を選ぶ
    if (e.key === 'Enter') {
      e.preventDefault();
      if (e.isComposing || e.keyCode === 229) return;
      const first = list.hidden ? null : list.querySelector('.sol-cutin-suggest-item');
      if (first) pick(first.textContent);
    }
  });

  return { reset, focusSearch: () => input.focus() };
}
