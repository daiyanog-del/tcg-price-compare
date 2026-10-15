/**
 * 盤面の保存・復元サービス
 * カード配置とゲーム状態を、ページ遷移をまたぐセッション復元（sessionStorage）用に写し取り・書き戻す。
 * （スロット保存 card-sim-save-slot-* は廃止。既存分は slot-migration.js がリプレイ一覧へ移す）
 */

import { getCardState, applyCardState, markTokenIfSrc } from '../components/card-state.js';
import { attachCardImageListeners, syncCardIdCounter } from '../components/card-manager.js';
import { createProxyCardElement } from '../components/proxy-card.js';
import { addCounterDeleteButton, initializeCounter } from '../components/counter-manager.js';
import { showToast } from '../utils/toast.js';
import { restackCustomSlot } from '../components/slot-stack.js';
import { registerCardName, registerCardType } from './replay-service.js';
import { clearSessionResume, stripVisibility, storeSessionResume, takeSessionResume } from '../utils/session-resume-store.js';

const API_CARD_IMAGES = '/api/card-images';

/**
 * スロット内のカード情報を取得
 * @param {Element} slot - スロット要素
 * @returns {Array} - カード情報の配列
 */
function getCardsFromSlot(slot) {
  const cards = [];
  const wrappers = slot.querySelectorAll('.tier-item-wrapper');

  wrappers.forEach(wrapper => {
    // img.tier-item（発売済み）または div.tier-item（プロキシ）どちらも .tier-item で取れる
    const cardEl = wrapper.querySelector('.tier-item');
    if (cardEl) {
      const cardName = cardEl.dataset.cardName || '';

      // src の決定:
      //   発売済み img: img.src（実URL）
      //   プロキシ div: 'card://カード名'（再解決センチネル）
      //
      // ロード時は restoreCardsToSlot で card:// を検出して /api/card-images で
      // 現時点の解決結果に変換する（発売後は正規画像、画像削除後はプロキシ）。
      //   未発売SAMPLE画像（wrapper に sample-art）: 名前があれば 'card://カード名'
      //     （ロード時に再解決。発売後は自動でクリーン画像になる）。名前が無ければ実URL＋source で保存。
      const isSample = wrapper.classList.contains('sample-art');
      let src;
      if (cardEl.tagName === 'IMG') {
        src = (isSample && cardName) ? `card://${cardName}` : (cardEl.src || '');
      } else {
        // div.tier-item（プロキシカード）: カード名があれば card:// センチネルで保存
        src = cardName ? `card://${cardName}` : '';
      }

      const state = getCardState(wrapper);
      cards.push({
        id:       wrapper.id,
        src,
        style:    stripVisibility(wrapper.getAttribute('style')), // 演出中の visibility:hidden は残さない
        cardName,
        state,   // { orientation, face }
        // 旧セーブデータには無いキー（省略時は種別不明＝従来どおり）。「場に出す」メニューの出し分けに使う
        ...(wrapper.dataset.cardType ? { cardType: wrapper.dataset.cardType } : {}),
        // 旧セーブデータには無いキー（省略時は従来どおり発売済み扱い）。
        // url は card:// 再解決が通信失敗したときだけ使う SAMPLE 画像の実URL
        ...(isSample ? { source: 'official_sample', url: cardEl.src || '' } : {}),
      });
    }
  });

  return cards;
}

/**
 * カウンター情報を取得
 * @returns {Array} - カウンター情報の配列
 */
function getCounterStates() {
  const counters = [];
  const counterElements = document.querySelectorAll('.counter-container');

  counterElements.forEach((counter, index) => {
    // 元のカウンターはスキップ（親要素のidがparent）
    if (counter.id === 'parent') {
      return;
    }

    const textbox = counter.querySelector('.counter-textbox');
    const parentSlot = counter.parentElement;

    // カード上のカウンターは、カード要素（.tier-item-wrapper）固有のidで親を記録する。
    // className（全カード共通の文字列）だと盤面に同名カードが複数ある場合に
    // 復元時のquerySelectorが最初の1件しか拾えず、別カードに付け替わってしまうため。
    const cardWrapper = parentSlot ? parentSlot.closest('.tier-item-wrapper') : null;

    counters.push({
      value: textbox ? textbox.value : '1',
      parentId: cardWrapper ? cardWrapper.id : (parentSlot ? parentSlot.className : ''),
      parentDataSlot: parentSlot ? parentSlot.getAttribute('data-slot') : null,
      style: counter.getAttribute('style') || '',
    });
  });

  return counters;
}

/**
 * card:// センチネルを /api/card-images バッチ POST で解決する
 *
 * @param {Array} cards  getCardsFromSlot の返り値（srcに card:// を含む可能性）
 * @returns {Promise<Map<string, Object>>}
 *   カード名 -> { kind, url?, proxy? } の Map
 *   解決不要（実URL）のカードは含まない
 */
async function _resolveCardSentinels(cards) {
  const sentinelNames = [];
  for (const c of (cards || [])) {
    if (c.src && c.src.startsWith('card://')) {
      const name = c.src.slice('card://'.length);
      if (name && !sentinelNames.includes(name)) {
        sentinelNames.push(name);
      }
    }
  }

  if (sentinelNames.length === 0) return new Map();

  try {
    const res = await fetch(API_CARD_IMAGES, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ names: sentinelNames }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const images = data.images || {};

    const result = new Map();
    for (const name of sentinelNames) {
      // images[name] は文字列URL / {kind, url?, proxy?} / null
      const val = images[name] ?? null;
      if (!val) {
        result.set(name, { kind: 'none' });
      } else if (typeof val === 'string') {
        result.set(name, { kind: 'image', url: val });
      } else {
        result.set(name, val);
      }
    }
    return result;
  } catch (e) {
    console.warn('[restoreCardsToSlot] card:// 解決失敗（プロキシ風表示にフォールバック）:', e);
    // 失敗時は kind='none' 扱い（名前のみ表示）。resolveFailed は「再解決できなかった」印
    // （SAMPLE は保存済みの実URLで復元するため、解決成功で none が返った場合と区別する）
    const fallback = new Map();
    for (const name of sentinelNames) {
      fallback.set(name, { kind: 'none', resolveFailed: true });
    }
    return fallback;
  }
}

/**
 * カードを復元（非同期版: card:// センチネルを再解決してから DOM を構築）
 * @param {Element} slot - 復元先スロット
 * @param {Array} cards - カード情報の配列
 */
async function restoreCardsToSlot(slot, cards) {
  if (!slot || !cards) return;

  // 既存のカードをクリア（初期カードは残す）
  const existingWrappers = slot.querySelectorAll('.tier-item-wrapper:not(#initial)');
  existingWrappers.forEach(wrapper => wrapper.remove());

  // card:// センチネルをバッチ解決（該当なしなら即 return した空 Map）
  const resolved = await _resolveCardSentinels(cards);

  // カードを復元
  for (const cardData of cards) {
    // 初期カードはスキップ（既に存在するため）
    if (cardData.id === 'initial') continue;

    const wrapper = document.createElement('div');
    wrapper.className = 'tier-item-wrapper';
    wrapper.id = cardData.id;
    if (cardData.cardType) wrapper.dataset.cardType = cardData.cardType; // data-card-type を付け直す
    markTokenIfSrc(wrapper, cardData.src); // data: URL の画像はトークン（data-token="1"）
    if (cardData.style) {
      wrapper.setAttribute('style', stripVisibility(cardData.style)); // 過去のセーブに残った visibility:hidden 対策
    }

    let cardEl;

    if (cardData.src && cardData.src.startsWith('card://')) {
      // card:// センチネル: 再解決結果を使って描画
      const name = cardData.src.slice('card://'.length);
      let displayResult = resolved.get(name) || { kind: 'none' };
      // 再解決が通信失敗した SAMPLE: 保存済みの実URLで SAMPLE 画像として復元（プロキシに落とさない）
      if (displayResult.resolveFailed && cardData.source === 'official_sample' && cardData.url) {
        displayResult = { kind: 'image', url: cardData.url, source: 'official_sample' };
      }

      if (displayResult.kind === 'image' && displayResult.url) {
        // 発売済み or linked 後の正規画像
        const img = document.createElement('img');
        img.src = displayResult.url;
        img.className = 'tier-item';
        img.setAttribute('draggable', 'true');
        img.id = cardData.id.split(' ')[0];
        if (cardData.cardName) img.dataset.cardName = cardData.cardName;
        cardEl = img;
        // 発売済みクリーン画像にのみ透かし（SAMPLE画像は除外）
        if (displayResult.source !== 'official_sample') wrapper.classList.add('wm-released');
        else wrapper.classList.add('sample-art'); // 未発売SAMPLE（イラストぼかし設定の対象）
      } else if (displayResult.kind === 'proxy' && displayResult.proxy) {
        // 未発売プロキシ（まだ発売されていない）
        const proxyEl = createProxyCardElement(displayResult.proxy);
        proxyEl.setAttribute('draggable', 'true');
        proxyEl.id = cardData.id.split(' ')[0];
        if (cardData.cardName) proxyEl.dataset.cardName = cardData.cardName;
        cardEl = proxyEl;
      } else {
        // kind='none'（却下・削除済み等）: カード名のみのフォールバックプロキシ
        const fallbackProxy = { name: name || cardData.cardName || '不明なカード' };
        const proxyEl = createProxyCardElement(fallbackProxy);
        proxyEl.setAttribute('draggable', 'true');
        proxyEl.id = cardData.id.split(' ')[0];
        if (cardData.cardName) proxyEl.dataset.cardName = cardData.cardName;
        cardEl = proxyEl;
      }
    } else {
      // 通常の img（実URL）: 従来どおり。保存済みの実URLは発売済みクリーン画像
      const img = document.createElement('img');
      // img.src は常に元画像（裏面は CSS ::after で描画するため src は変更しない）
      img.src = cardData.src;
      img.className = 'tier-item';
      img.setAttribute('draggable', 'true');
      img.id = cardData.id.split(' ')[0];
      if (cardData.cardName) img.dataset.cardName = cardData.cardName;
      cardEl = img;
      // 実URL保存のSAMPLE（名前なし）は source で判別。旧データ（source無し）は発売済みクリーン画像
      if (cardData.source === 'official_sample') wrapper.classList.add('sample-art');
      else wrapper.classList.add('wm-released');
    }

    // イベントリスナーを再設定（dblclick による効果発動含む）
    attachCardImageListeners(wrapper, cardEl);

    wrapper.appendChild(cardEl);
    slot.appendChild(wrapper);

    // カード状態を復元（守備表示・セット）
    if (cardData.state) {
      applyCardState(wrapper, cardData.state);
    }
  }

  // 旧データの縦位置のずれを詰め直す（フィールドスロット以外では何もしない）
  restackCustomSlot(slot);
}

/**
 * カウンターを復元
 * @param {Array} counters - カウンター情報の配列
 */
function restoreCounters(counters) {
  if (!counters) return;

  // 既存のカウンターをクリア（親要素のもの以外）
  const existingCounters = document.querySelectorAll('.counter-container:not(#parent)');
  existingCounters.forEach(counter => counter.remove());

  // 旧形式データ等で復元先が見つからずスキップした件数。1件でもあればトーストで通知する
  // （console.warnだけだと復元のたびに無言でカウンターが消える）。
  let skippedCount = 0;

  // カウンターを復元
  counters.forEach(counterData => {
    // 親となるスロットを検索
    let parentSlot = null;
    if (counterData.parentDataSlot) {
      parentSlot = document.querySelector(`[data-slot="${counterData.parentDataSlot}"]`);
    } else if (counterData.parentId) {
      // 新形式: parentId はカード要素（.tier-item-wrapper）固有のid
      parentSlot = document.getElementById(counterData.parentId);
      if (!parentSlot) {
        // 旧形式（class名ベースのparentId）を含む壊れた古いデータへの配慮。
        // 同名カードが複数あると誤ったカードに復元されてしまう旧バグを再現しない
        // よう、ここでは推測復元せずスキップする（根本修正は保存側で完了済み）。
        console.warn('[restoreCounters] カウンターの復元先カードが見つかりませんでした（旧形式データの可能性）。このカウンターの復元をスキップします:', counterData.parentId);
        skippedCount++;
      }
    }

    if (!parentSlot) return;

    // カウンターを作成
    const counter = document.createElement('div');
    counter.className = 'counter-container';
    counter.setAttribute('draggable', 'true');
    if (counterData.style) {
      counter.setAttribute('style', counterData.style);
    }

    const textbox = document.createElement('input');
    textbox.type = 'text';
    textbox.className = 'counter-textbox';
    textbox.value = counterData.value;
    textbox.readOnly = true;

    const isOnCard = parentSlot.classList.contains('tier-item-wrapper');

    if (isOnCard) {
      // カード上: [−][数値][+] のフラット構造
      const downBtn = document.createElement('button');
      downBtn.className = 'triangle-button down';
      downBtn.textContent = '−';

      const upBtn = document.createElement('button');
      upBtn.className = 'triangle-button up';
      upBtn.textContent = '+';

      counter.appendChild(downBtn);
      counter.appendChild(textbox);
      counter.appendChild(upBtn);
      addCounterDeleteButton(counter); // 復元後もカード上カウンターは×で削除できるようにする
      initializeCounter(counter); // 復元後も+/-ボタンが反応するようにイベントを登録する
    } else {
      // パネル内: ボタンコンテナ（縦積み）＋数値
      const buttonContainer = document.createElement('div');
      buttonContainer.className = 'triangle-button-container';

      const upButton = document.createElement('button');
      upButton.className = 'triangle-button up';
      upButton.textContent = '+';

      const downButton = document.createElement('button');
      downButton.className = 'triangle-button down';
      downButton.textContent = '−';

      buttonContainer.appendChild(upButton);
      buttonContainer.appendChild(downButton);
      counter.appendChild(buttonContainer);
      counter.appendChild(textbox);
    }

    // イベントリスナーを再設定
    counter.addEventListener('dragstart', window.drag);

    parentSlot.appendChild(counter);
  });

  if (skippedCount > 0) {
    showToast('古い保存データのためカウンターを復元できませんでした');
  }
}

// ── セッション復元（ページ遷移をまたいだ盤面の自動保存・復元） ──

/**
 * 現在の盤面状態をsessionStorageに保存（pagehide時に呼ぶ）
 * デッキが空なら保存しない
 * リプレイ記録は ui/ を import すると循環依存になるため、呼び出し側（main.js）が集めて渡す。
 * @param {Object|null} [replay] - リプレイ退避データ（記録が空なら省略）
 */
export function saveSessionResume(replay = null) {
  try {
    const state = _captureCurrentState();
    const hasContent = (
      state.slots.poolRow?.length > 0 ||
      state.slots.poolRow2?.length > 0 ||
      Object.keys(state.slots).some(k => k.startsWith('custom-slot-') && state.slots[k].length > 0) ||
      state.slots['center-slot']?.length > 0 ||
      Object.keys(state.slots).some(k => k.startsWith('side-slot-') && state.slots[k].length > 0) || // 墓地・除外だけでも盤面として意味がある
      state.slots['free-space']?.length > 0 ||
      !!replay                                  // 記録があるなら保存する
    );
    if (!hasContent) return;

    // 全体を1つの圧縮データで保存。容量超過なら記録を外して盤面のみ保存（詳細は session-resume-store.js）
    storeSessionResume(sessionStorage, (s) => LZString.compress(s), state, replay);
  } catch (e) {
    console.warn('セッション保存失敗:', e);
  }
}

/**
 * sessionStorageから盤面を復元して盤面に反映する（非同期版）
 * 呼び出し後にデータは削除される
 * リプレイ記録の適用は呼び出し側（main.js）が行うため、退避データを返す。
 * @returns {Promise<false|{replay:Object|null, replayDropped:boolean}>} 復元できなければ false
 */
export async function loadSessionResume() {
  try {
    const taken = takeSessionResume(sessionStorage, (s) => LZString.decompress(s));
    if (!taken) return false;

    await _applyState(taken.state);
    return { replay: taken.state.replay || null, replayDropped: taken.replayDropped };
  } catch (e) {
    console.warn('セッション復元失敗:', e);
    clearSessionResume(sessionStorage);
    return false;
  }
}

function _captureCurrentState() {
  const state = { slots: {}, counters: getCounterStates() };
  state.slots.poolRow  = getCardsFromSlot(document.getElementById('poolRow'));
  state.slots.poolRow2 = getCardsFromSlot(document.getElementById('poolRow2'));
  document.querySelectorAll('.custom-slot').forEach(slot => {
    const n = slot.getAttribute('data-slot');
    if (n) state.slots[`custom-slot-${n}`] = getCardsFromSlot(slot);
  });
  document.querySelectorAll('.side-slot').forEach((slot, i) => {
    state.slots[`side-slot-${i}`] = getCardsFromSlot(slot);
  });
  const center = document.querySelector('.center-slot');
  if (center) state.slots['center-slot'] = getCardsFromSlot(center);
  const free = document.getElementById('free-space');
  if (free) state.slots['free-space'] = getCardsFromSlot(free);
  return state;
}

async function _applyState(state) {
  // 全スロットを並列復元（card:// 解決の待機がスロット数分重ならないよう Promise.all を使う）
  const tasks = [];

  tasks.push(restoreCardsToSlot(document.getElementById('poolRow'),  state.slots.poolRow));
  tasks.push(restoreCardsToSlot(document.getElementById('poolRow2'), state.slots.poolRow2));

  document.querySelectorAll('.custom-slot').forEach(slot => {
    const n = slot.getAttribute('data-slot');
    if (n) tasks.push(restoreCardsToSlot(slot, state.slots[`custom-slot-${n}`]));
  });
  document.querySelectorAll('.side-slot').forEach((slot, i) => {
    tasks.push(restoreCardsToSlot(slot, state.slots[`side-slot-${i}`]));
  });

  const center = document.querySelector('.center-slot');
  if (center && state.slots['center-slot']) {
    tasks.push(restoreCardsToSlot(center, state.slots['center-slot']));
  }
  const free = document.getElementById('free-space');
  if (free && state.slots['free-space']) {
    tasks.push(restoreCardsToSlot(free, state.slots['free-space']));
  }

  await Promise.all(tasks);

  // 復元済みカードIDに合わせてitemCountを進める（重複ID防止。詳細はsyncCardIdCounterのコメント参照）
  // state.slots由来のIDだけだと、保存データに存在しないスロットキー（盤面レイアウト変更前後など）の
  // 場合にrestoreCardsToSlotが早期returnして残存カードが消えず、そのIDがカウンターに反映されない。
  // 実際にDOM上へ存在する全カードのIDを対象にすることで、残存・復元・プロキシいずれも漏らさず拾う。
  const domIds = [...document.querySelectorAll('.tier-item-wrapper')].map(w => w.id);
  syncCardIdCounter(domIds);

  // 復元したカードの名前をリプレイ辞書へ（無いと共有で名前が欠ける）
  document.querySelectorAll('.tier-item[id]').forEach(el => {
    registerCardName(el.id, el.dataset.cardName);
    // 復元したカードの種類もリプレイ辞書へ（無いと共有・保存した記録の再生で data-card-type が失われる）
    registerCardType(el.id, el.closest('.tier-item-wrapper')?.dataset.cardType);
  });

  restoreCounters(state.counters);
}
