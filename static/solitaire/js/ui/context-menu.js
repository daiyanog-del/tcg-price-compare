/**
 * context-menu.js
 * カードの右クリック / 長押しコンテキストメニュー
 *
 * 表示項目（ゾーン別）:
 *   フィールド: 攻撃/守備の切替 / セット・表にする / 場に出す（攻撃/守備/裏側守備/表側/セット）※カード種別で出し分け
 *              / 一番下に重ねる / 墓地送り / 除外 / 手札に戻す / デッキに戻す / 削除（トークンのみ）
 *   プール    : 墓地送り / 除外 / 場に出す（カード種別で出し分け） / 削除（トークンのみ）
 *              （通常デッキ #poolRow のみ手札に戻すも表示。EXデッキ #poolRow2 は通常プレイで
 *               手札に加えることがほぼ無いため、誤操作防止のため手札に戻すを出さない
 *               （ドラッグ操作ではEXデッキのカードも手札へ置けるが、それとは別に、
 *               ワンタップで確定するメニュー項目としては誤操作のリスクが高いため出さない）。
 *               攻撃/守備トグル・セット・
 *               一番下に重ねる・デッキに戻すは既にデッキ内のカードには意味がないため出さない）
 *              （初期カード isInitial は現状維持で何も出さない。昔の設計の名残でスコープ外）
 *   その他    : 墓地送り / 除外 / 手札に戻す / デッキに戻す / 削除（トークンのみ）
 *   プール以外のすべてのカードに「吹き出しを付ける」（整理④。comment-ui.js の入力欄を開く）
 *              と「カウンターを置く」（値1で置き、カード上の＋−×で操作する）
 *   空いたマス（カードの無い .custom-slot の地）の右クリックは「トークンを出す」
 *              （token-generator.js が openContextMenuAt で開く）
 *   トークン以外のすべてのカードの末尾に「デッキに1枚足す（今 N枚）／デッキから1枚抜く」
 *              （デッキ編集。項目は deck-edit-ui.js が setExtraMenuItemsProvider で登録する）
 *
 * 「削除」は普通のカードには出さない（盤面は記録（リプレイ）だけで再現できるようにするため、
 * 記録に残せない消え方を作らない）。トークンだけは出せて、消すと removeCard として記録される。
 *
 * 「場に出す」系はマウス操作専用（isTouch=trueの呼び出し元には出さない。タッチの長押しメニューは
 * mousedown/keydownしか見ていない配置待ち状態機械と噛み合わず操作不能に陥りうるため）。
 * 2段階クリック方式: メニュー項目クリックでは何も確定させず配置待ち状態に入り、全 .custom-slot を
 * ハイライトする。スロットクリックで初めて状態とゾーン移動の両方を確定する。
 * Esc / スロット以外への外側クリック / 別カードの右クリック / 別カードのドラッグ開始で
 * キャンセルされる（cancelFieldPlacement を export し、drag-drop.js の dragstart から呼ぶ）。
 * 場のカードの上で押したときは、押した時点では確定せず、ドラッグせずに離した（click）ときに確定する
 * （押した時点で確定すると、場の別のカードをドラッグし始めたときにキャンセルより先に確定してしまうため。2026-10-21）。
 *
 * 依存: card-state.js のほか、固定ゾーン移動（墓地送り/除外/手札に戻す）のために
 * drag-drop.js の getDropZoneInfo/executeDrop/getZoneId を使う（実際の配置・リプレイ記録ロジックの二重持ちを避ける）。
 * main.js は drag-drop.js を先に import し、drag-drop.js が本ファイルを import するため、
 * 本ファイル（context-menu.js）の方が drag-drop.js より先にモジュール評価が完了する。
 * そのため本ファイルのトップレベル（関数の外）で drag-drop.js からの import 値を参照すると
 * 未初期化になる危険がある。参照は必ず関数内（呼び出し時点）に限ること。
 */

import {
  applyDefense,
  applySet,
  applyCardState,
  getCardState,
  isMonsterCard,
  isExCard,
  isTokenCard,
} from '../components/card-state.js';
import { playSetFlip, flipMoveClone } from '../components/card-effects.js';
import { getDropZoneInfo, executeDrop, getZoneId } from '../components/drag-drop.js';
import { restackCustomSlot } from '../components/slot-stack.js';
import { openCardCommentInput } from './comment-ui.js';
import { addCounterToCard } from '../components/counter-manager.js';
import { showCardByName } from './card-info-panel.js';

// ── 単一インスタンス ──────────────────────────────────────────────
let _menuEl          = null;
let _removeListeners = null;
// 末尾に足す項目（デッキ編集の「デッキに1枚足す／抜く」。deck-edit-ui.js が登録する）。
// deck-edit-ui.js を直接 import すると card-manager.js → 本ファイル → deck-input-panel.js → card-manager.js の
// 循環になるため、向こうから登録してもらう。
let _extraItemsProvider = null;
/** @param {(wrapper: Element) => Array<{label:string, sub?:string, disabled?:boolean, action?:Function}>} fn */
export function setExtraMenuItemsProvider(fn) { _extraItemsProvider = fn; }

/**
 * コンテキストメニューを開く
 * @param {Element} wrapper  - .tier-item-wrapper
 * @param {Element} cardEl   - img.tier-item または div.tier-item（proxy-card）
 * @param {number}  x        - 表示位置 clientX
 * @param {number}  y        - 表示位置 clientY
 * @param {boolean} [isTouch=false] - タッチ（長押し）経由の呼び出しなら true。
 *   「場に出す」系項目はマウスの mousedown/keydown だけを監視する配置待ち状態機械
 *   に依存しているため、タッチ経由ではこの項目群を出さない（drag-drop.js の
 *   長押しメニューから呼ばれる場合に true を渡す）。
 */
export function openCardContextMenu(wrapper, cardEl, x, y, isTouch = false) {
  // 後方互換: 内部で img という名前を使っていた箇所を cardEl として統一する
  const img = cardEl;  // 既存の _buildMenuItems / _returnToDeck の引数に渡す
  closeContextMenu();
  // 配置待ち状態のまま別カードを右クリックした場合に操作不能へ陥らないようキャンセルする
  cancelFieldPlacement();

  const parent   = wrapper.parentElement;
  const isInPool = isCardInPool(wrapper);
  const isInitial = wrapper.id === 'initial';

  const items = _buildMenuItems(wrapper, img, parent, isInPool, isInitial, isTouch);
  // 初期カード（昔の設計の名残）以外には、デッキ編集の項目を区切って末尾に足す
  if (!isInitial && _extraItemsProvider) {
    const extra = _extraItemsProvider(wrapper) || [];
    if (extra.length) items.push({ separator: true }, ...extra);
  }
  if (items.length === 0) return;
  // 2026-10-21 マイデッキの右クリックと同じ形に（モック sim-refresh-20261021 の B）:
  // 先頭にカード名の見出し、最後に「カードの詳細」
  const detail = _cardDetailItem(img);
  if (detail) items.push({ separator: true }, detail);
  items.unshift({ heading: _cardNameOf(img) || 'カード', title: true });
  _showMenu(items, x, y);
}

/**
 * 項目を指定してメニューを開く（カード以外の場所の右クリック用。空いたマスの「トークンを出す」など）。
 * 項目の形は openCardContextMenu と同じ。加えて { heading } は見出し、{ element } は中身を自分で作った行。
 * @param {Array<Object>} items
 * @param {number} x - 表示位置 clientX
 * @param {number} y - 表示位置 clientY
 */
export function openContextMenuAt(items, x, y) {
  closeContextMenu();
  cancelFieldPlacement();
  if (!items.length) return;
  _showMenu(items, x, y);
}

function _showMenu(items, x, y) {
  // ── メニュー要素を生成 ────────────────────────────────────────
  const menu = document.createElement('ul');
  menu.className = 'sol-context-menu';
  menu.setAttribute('role', 'menu');

  items.forEach(({ label, action, separator, sub, disabled, heading, title, element, swatch }) => {
    if (separator) {
      const hr = document.createElement('li');
      hr.className = 'sol-context-menu-sep';
      menu.appendChild(hr);
      return;
    }
    // 見出し（押せない）
    if (heading) {
      const h = document.createElement('li');
      h.className = 'sol-context-menu-heading' + (title ? ' is-title' : '');
      h.textContent = heading;
      if (title) h.title = heading; // 長い名前は省略して出すので、全文はツールチップで
      menu.appendChild(h);
      return;
    }
    // 中身を呼び出し側が作った行（入力欄など。押しても閉じない）
    if (element) {
      const row = document.createElement('li');
      row.className = 'sol-context-menu-row';
      row.appendChild(element);
      menu.appendChild(row);
      return;
    }
    const li = document.createElement('li');
    li.className = 'sol-context-menu-item';
    li.textContent = label;
    li.setAttribute('role', 'menuitem');
    // 色見本（トークンの色など）。項目名の前に置く
    if (swatch) {
      const sw = document.createElement('span');
      sw.className = 'sol-context-menu-swatch';
      sw.style.background = swatch;
      li.prepend(sw);
    }
    // 補足（「今 3枚」など）。textContent で入れる
    if (sub) {
      const s = document.createElement('span');
      s.className = 'sol-context-menu-sub';
      s.textContent = sub;
      li.appendChild(s);
    }
    // 押せない項目: 見せるが何もしない（押したらメニューだけ閉じる）
    if (disabled) {
      li.classList.add('is-disabled');
      li.setAttribute('aria-disabled', 'true');
      const closeOnly = (e) => { e.preventDefault(); e.stopPropagation(); closeContextMenu(); };
      li.addEventListener('mousedown', closeOnly);
      li.addEventListener('touchend', closeOnly);
      menu.appendChild(li);
      return;
    }
    li.addEventListener('mousedown', (e) => {
      e.stopPropagation();
      action();
      closeContextMenu();
    });
    // タッチ対応（touchend でアクション）
    li.addEventListener('touchend', (e) => {
      e.preventDefault();
      e.stopPropagation();
      action();
      closeContextMenu();
    });
    menu.appendChild(li);
  });

  // ── 位置調整（画面端クランプ） ────────────────────────────────
  document.body.appendChild(menu);
  const mw = menu.offsetWidth  || 160;
  const mh = menu.offsetHeight || items.length * 36;
  menu.style.left = `${Math.min(x, window.innerWidth  - mw - 6)}px`;
  menu.style.top  = `${Math.min(y, window.innerHeight - mh - 6)}px`;

  _menuEl = menu;

  // ── 外クリック/スクロールで閉じる ────────────────────────────
  const closeOnOutside = (e) => {
    if (!menu.contains(e.target)) closeContextMenu();
  };
  // 同一イベントで即座に閉じないよう setTimeout で登録
  const tid = setTimeout(() => {
    document.addEventListener('mousedown', closeOnOutside);
    document.addEventListener('touchstart', closeOnOutside);
    document.addEventListener('scroll', closeContextMenu, { passive: true, capture: true });
  }, 0);

  _removeListeners = () => {
    clearTimeout(tid);
    document.removeEventListener('mousedown', closeOnOutside);
    document.removeEventListener('touchstart', closeOnOutside);
    document.removeEventListener('scroll', closeContextMenu, { capture: true });
  };
}

// ── スマホ アクションシート用（mobile-ui.js から呼ぶ）──────────────
// _buildMenuItems 内のアクションと同じ処理を export し、ロジックの複製を避ける。

/**
 * カードがプール内（#poolRow / #poolRow2 の直接の子）かどうかを判定する。
 * openCardContextMenu の isInPool 判定と同じ条件を共有する（A-3・司令塔決定。判定の二重持ち禁止）。
 * @param {Element} wrapper - .tier-item-wrapper
 * @returns {boolean}
 */
export function isCardInPool(wrapper) {
  const parent = wrapper.parentElement;
  const poolRow  = document.getElementById('poolRow');
  const poolRow2 = document.getElementById('poolRow2');
  return parent === poolRow || parent === poolRow2;
}

/**
 * 守備表示をトグルする（コンテキストメニュー「守備表示にする/攻撃表示にする」と同じ処理）
 * @param {Element} wrapper - .tier-item-wrapper
 */
export function toggleCardDefense(wrapper) {
  const isDefense = wrapper.dataset.orientation === 'defense';
  applyDefense(wrapper, !isDefense);
  _logState(wrapper, { inPlace: true });
}

/**
 * セット状態をトグルする（コンテキストメニュー「セット（裏向きにする）/表にする」と同じ処理）。
 * セット解除時、コンテキストメニューはモンスターに対して攻撃/守備を選ばせるが、
 * アクションシートは単純トグルのため攻撃表示に固定する。
 * @param {Element} wrapper - .tier-item-wrapper
 */
export function toggleCardSet(wrapper) {
  const isSet = wrapper.dataset.face === 'down';
  playSetFlip(wrapper, () => {
    applySet(wrapper, !isSet);
    _logState(wrapper, { inPlace: true });
  });
}

/**
 * カードをデッキに戻す（コンテキストメニュー「デッキに戻す」と同じ処理）
 * @param {Element} wrapper - .tier-item-wrapper
 */
export function returnCardToDeckMenu(wrapper) {
  const img = wrapper.querySelector('.tier-item');
  if (img) _returnToDeck(wrapper, img);
}

// L-2: removeCardMenu は A-3 でアクションシートから「削除」を外した際に未使用になったため削除。
// トークンの削除は _buildMenuItems 内の長押しメニュー項目（PC 右クリックメニューと共通）が担う。

/**
 * トークンを盤面から消す。消す前にいたゾーンと一緒に removeCard として記録する。
 * 画像・名前の辞書（リプレイ側）にはカードが残るため、巻き戻したときは辞書から作り直せる。
 * 普通のカードは消せない（呼ばれても何もしない）。
 * @param {Element} wrapper - .tier-item-wrapper
 */
export function deleteTokenCard(wrapper) {
  if (!isTokenCard(wrapper)) return;
  const cardEl = wrapper.querySelector('.tier-item');
  const sourceSlot = wrapper.parentElement;
  if (cardEl?.id && typeof window.replayLog === 'function') {
    window.replayLog({
      actionType: 'removeCard',
      cardId: cardEl.id,
      zoneId: sourceSlot ? getZoneId(sourceSlot) : 'unknown', // 消す前にいたゾーン
    });
  }
  wrapper.remove();
  restackCustomSlot(sourceSlot); // フィールドの重ねを詰め直す（.custom-slot 以外は何もしない）
}

/**
 * 現在のコンテキストメニューを閉じる
 */
export function closeContextMenu() {
  if (_menuEl) {
    _menuEl.remove();
    _menuEl = null;
  }
  if (_removeListeners) {
    _removeListeners();
    _removeListeners = null;
  }
}

// ── 内部: メニュー項目の組み立て ──────────────────────────────────

function _buildMenuItems(wrapper, img, parent, isInPool, isInitial, isTouch = false) {
  const items = [];

  if (isInPool) {
    if (isInitial) return items; // 初期カードは現状維持で何も出さない（昔の設計の名残・スコープ外）

    // 今 EXデッキの欄にあるか（#poolRow2 の直接の子か）。「手札に戻す」を出すかどうかに使う。
    // EXデッキのカードかどうか（デッキに戻すときの戻り先）は別の問いで、card-state.js の isExCard で見る
    const isExPool = parent === document.getElementById('poolRow2');

    // 墓地送り / 除外（通常デッキ・EXデッキ両方に表示）
    const graveZoneEl  = _getGraveZoneElement();
    const banishZoneEl = _getBanishZoneElement();
    items.push({
      label: '墓地送り',
      action: () => { _moveToFixedZone(wrapper, graveZoneEl); },
    });
    items.push({
      label: '除外',
      action: () => { _moveToFixedZone(wrapper, banishZoneEl); },
    });

    // 手札に戻す: 通常デッキのみ（EXデッキは誤操作防止のため出さない。ドラッグ操作ではEXデッキの
    // カードも手札へ置けるが、ワンタップで確定するメニュー項目としては誤操作のリスクが高いため出さない）
    if (!isExPool) {
      const handZoneEl = _getHandZoneElement();
      items.push({
        label: '手札に戻す',
        action: () => { _moveToFixedZone(wrapper, handZoneEl); },
      });
    }

    // 場に出す（2段階クリック方式・マウス操作専用。タッチ長押しメニューでは出さない）
    if (!isTouch) {
      items.push({ separator: true });
      items.push(..._fieldPlacementItems(wrapper));
    }

    if (isTokenCard(wrapper)) {
      items.push({ separator: true });
      items.push({ label: '削除', action: () => { deleteTokenCard(wrapper); } });
    }
    return items;
  }

  const isDefense = wrapper.dataset.orientation === 'defense';
  const isSet     = wrapper.dataset.face === 'down';
  const isCustom  = parent?.classList.contains('custom-slot');
  // 置き場所に合わない形の切り替えは出さない（2026-10-21 モック B。スマホのアクションシートは手札で既に隠している）。
  //   守備表示: 場だけ。セット: 場と除外（裏側で除外することがあるため）。
  //   今その形になっているカードは、戻せるように場所を問わず出す
  const showDefense = isCustom || isDefense;
  const showSet     = isCustom || parent === _getBanishZoneElement() || isSet;

  // 守備表示切替
  if (showDefense) {
    items.push({
      label: isDefense ? '攻撃表示にする' : '守備表示にする',
      action: () => {
        applyDefense(wrapper, !isDefense);
        _logState(wrapper, { inPlace: true });
      },
    });
  }

  // セット切替（フリップアニメ付き: scaleX=0の瞬間に状態変更）
  if (!showSet) {
    // 出さない
  } else if (isSet && isMonsterCard(wrapper)) {
    // モンスターのセット解除：攻撃表示か守備表示を選べる
    items.push({
      label: '表にする（攻撃表示）',
      action: () => {
        playSetFlip(wrapper, () => {
          applySet(wrapper, false);
          _logState(wrapper, { inPlace: true });
        });
      },
    });
    items.push({
      label: '表にする（守備表示）',
      action: () => {
        playSetFlip(wrapper, () => {
          applyCardState(wrapper, { orientation: 'defense', face: '' });
          _logState(wrapper, { inPlace: true });
        });
      },
    });
  } else {
    items.push({
      label: isSet ? '表にする' : 'セット（裏向きにする）',
      action: () => {
        playSetFlip(wrapper, () => {
          applySet(wrapper, !isSet);
          _logState(wrapper, { inPlace: true });
        });
      },
    });
  }

  // 場に出す（2段階クリック方式・マウス操作専用）。プール以外（フィールド/固定ゾーン）なら無条件表示。
  // 形の切り替えを出さなかったとき（手札など）は、先頭の区切りを付けない
  // カード種別で項目を出し分ける: モンスター=攻撃/守備/裏側守備、魔法・罠=表側/セット
  // （モンスターに「セット」を出すと applyCardState の適用順の都合で裏側攻撃表示という
  //   不正な状態になるため出さない。裏側にしたい場合は「裏側守備」を使う想定）。
  // isTouch=true（タッチ長押しメニュー）では、mousedown/keydownしか見ない配置待ち状態機械
  // と噛み合わないため項目自体を出さない。
  if (!isTouch) {
    if (items.length) items.push({ separator: true });
    items.push(..._fieldPlacementItems(wrapper));
  }

  // 下重ね（フィールドスロット、かつ他のカードがある場合）
  if (isCustom && parent.querySelectorAll('.tier-item-wrapper').length > 1) {
    items.push({
      label: '一番下に重ねる',
      action: () => { _placeUnder(wrapper, parent); },
    });
  }

  // 固定ゾーンへの移動（墓地送り / 除外 / 手札に戻す）
  // 既に対象ゾーンにいるカードにはその項目を出さない（無意味な1手がリプレイに記録されるのを防ぐ）
  const graveZoneEl  = _getGraveZoneElement();
  const banishZoneEl = _getBanishZoneElement();
  const handZoneEl   = _getHandZoneElement();
  const fixedZoneItems = [];
  if (parent !== graveZoneEl) {
    fixedZoneItems.push({
      label: '墓地送り',
      action: () => { _moveToFixedZone(wrapper, graveZoneEl); },
    });
  }
  if (parent !== banishZoneEl) {
    fixedZoneItems.push({
      label: '除外',
      action: () => { _moveToFixedZone(wrapper, banishZoneEl); },
    });
  }
  if (parent !== handZoneEl) {
    fixedZoneItems.push({
      label: '手札に戻す',
      action: () => { _moveToFixedZone(wrapper, handZoneEl); },
    });
  }

  if (fixedZoneItems.length > 0) {
    if (items.length) items.push({ separator: true });
    items.push(...fixedZoneItems);
  }

  // 吹き出しを付ける（整理④。デッキ内のカードには付けないので、上のプール内の分岐には入れない）
  items.push({ separator: true });
  items.push({
    label: '吹き出しを付ける',
    action: () => { openCardCommentInput(wrapper); },
  });
  // カウンターを置く（2026-10-20 右パネルのドラッグで置くカウンターから置き換え。1から始め、カード上の＋−で増減）
  items.push({
    label: 'カウンターを置く',
    action: () => { addCounterToCard(wrapper); },
  });

  items.push({ separator: true });

  // デッキに戻す
  items.push({
    label: 'デッキに戻す',
    action: () => { _returnToDeck(wrapper, img); },
  });

  // 削除（トークンのみ。普通のカードは盤面から消せない）
  if (isTokenCard(wrapper)) {
    items.push({
      label: '削除',
      action: () => { deleteTokenCard(wrapper); },
    });
  }

  return items;
}

/**
 * 「場に出す」メニュー項目を組み立てる（カード種別で出し分け）
 * @param {Element} wrapper - .tier-item-wrapper
 * @returns {Array<{label: string, action: Function}>}
 */
function _fieldPlacementItems(wrapper) {
  if (isMonsterCard(wrapper)) {
    // 「場に出す（セット）」は出さない: applyCardState はapplySet→applyDefenseの順に
    // 適用するため、モンスターに { orientation: '', face: 'down' } を渡すと
    // applySet(true)の副作用（守備表示化）を直後のapplyDefense(false)が打ち消し、
    // 「裏側攻撃表示」という遊戯王に存在しない状態になってしまう。
    // 裏側にしたい場合は下の「場に出す（裏側守備）」で同じ結果になるため項目として冗長でもある。
    return [
      { label: '場に出す（攻撃）',     action: () => _startFieldPlacement(wrapper, { orientation: '',        face: '' }) },
      { label: '場に出す（守備）',     action: () => _startFieldPlacement(wrapper, { orientation: 'defense', face: '' }) },
      { label: '場に出す（裏側守備）', action: () => _startFieldPlacement(wrapper, { orientation: 'defense', face: 'down' }) },
    ];
  }
  return [
    { label: '場に出す（表側）', action: () => _startFieldPlacement(wrapper, { orientation: '', face: '' }) },
    { label: '場に出す（セット）', action: () => _startFieldPlacement(wrapper, { orientation: '', face: 'down' }) },
  ];
}

// ── 「場に出す」配置待ち状態機械（PC版専用・2段階クリック方式） ──────

// 配置待ち中の情報。null = 配置待ちでない。
let _pcPlacement        = null; // { wrapper, state, slots }
let _pcPlacementCleanup = null; // イベントリスナー解除関数

/**
 * 「場に出す」の配置待ちを開始する。
 * 対象カードの状態を先に確定させず、全フィールドスロットをハイライトして
 * ユーザーの配置先クリックを待つ。
 * @param {Element} wrapper - .tier-item-wrapper
 * @param {{orientation: string, face: string}} state - 確定時に適用する状態
 */
function _startFieldPlacement(wrapper, state) {
  cancelFieldPlacement(); // 念のための冪等化

  // カードが既にいるスロットはハイライト・確定対象から除外する（無意味な1手が
  // リプレイに記録されるのを防ぐ。墓地送り/除外/手札に戻す3項目と同じ方針）。
  // 状態だけ変えたい場合は既存の守備/セット切替メニュー項目を使ってもらう想定。
  const currentSlot = wrapper.parentElement;
  const slots = Array.from(document.querySelectorAll('.custom-slot')).filter((s) => s !== currentSlot);
  slots.forEach((slot) => slot.classList.add('pc-placement-target'));
  _pcPlacement = { wrapper, state, slots };

  // 場のカードの上で押されたスロット。押した時点では確定せず、click（ドラッグしなかった）で確定する
  let clickSlot = null;
  const onOutsideMouseDown = (e) => {
    // 左クリック（button===0）のスロットクリックのみ配置を確定する。
    // 右クリック（スロット内の既存カードへの右クリック含む）は次のコンテキストメニューを
    // 開くための操作であり、確定ではなくキャンセル扱いにする
    // （openCardContextMenu 側の cancelFieldPlacement 呼び出しと合わせて二重の安全策）。
    const slot = e.button === 0 ? e.target.closest('.custom-slot') : null;
    clickSlot = null;
    if (slot && slots.includes(slot)) {
      // 場のカードの上で押した: ドラッグの始まりかもしれない。ドラッグが始まれば drag-drop.js の dragstart が
      // キャンセルし（ドラッグした後は click が来ない）、ドラッグせずに離せば下の onClick で確定する
      if (e.target.closest('.tier-item-wrapper')) { clickSlot = slot; return; }
      _confirmFieldPlacement(slot);
    } else {
      cancelFieldPlacement();
    }
  };
  const onClick = (e) => {
    if (!clickSlot) return;
    const slot = clickSlot;
    clickSlot = null;
    if (_pcPlacement && e.target.closest('.custom-slot') === slot) _confirmFieldPlacement(slot);
    else cancelFieldPlacement();
  };
  const onKeydown = (e) => {
    if (e.key === 'Escape') cancelFieldPlacement();
  };

  // openCardContextMenu の外クリック処理と同じパターン: 配置待ちを開始した
  // 同一クリック（mousedown→click）で即座に反応しないよう setTimeout で登録する。
  const tid = setTimeout(() => {
    document.addEventListener('mousedown', onOutsideMouseDown);
    document.addEventListener('click', onClick);
    document.addEventListener('keydown', onKeydown);
  }, 0);

  _pcPlacementCleanup = () => {
    clearTimeout(tid);
    document.removeEventListener('mousedown', onOutsideMouseDown);
    document.removeEventListener('click', onClick);
    document.removeEventListener('keydown', onKeydown);
  };
}

/**
 * 配置待ちを確定し、対象スロットへカードを移動する。
 * 状態は先に確定してから executeDrop に渡すため、executeDrop 内のリプレイ記録
 * （getCardState を参照）で正しい orientation/face が1回だけ記録される。
 * @param {Element} slot - .custom-slot
 */
function _confirmFieldPlacement(slot) {
  const { wrapper, state } = _pcPlacement;
  // 配置待ち中にデッキ再読込/盤面ロード/リプレイ巻き戻し等でカードがDOMから
  // 切り離されている場合、そのまま挿入すると削除済みカードが復活してしまうためガードする
  // （drag-drop.js の _tapTargetEl.isConnected ガードと同じ作法）。
  if (!wrapper || !wrapper.isConnected) {
    cancelFieldPlacement();
    return;
  }
  // FLIPアニメーション用に移動前の座標を、状態変更（applyCardState）より前に取得する
  const firstRect = wrapper.getBoundingClientRect();
  applyCardState(wrapper, state);
  const dropZoneInfo = getDropZoneInfo(slot);
  if (dropZoneInfo) {
    executeDrop({ type: 'card', element: wrapper }, dropZoneInfo, slot, {});
    flipMoveClone(wrapper, firstRect);
  }
  cancelFieldPlacement();
}

/**
 * 「場に出す」の配置待ちをキャンセルする（ハイライト解除・リスナー解除）。
 * 配置待ちでない場合は何もしない（他の呼び出し元からの冪等呼び出しを許容）。
 */
export function cancelFieldPlacement() {
  if (!_pcPlacement) return;
  _pcPlacement.slots.forEach((slot) => slot.classList.remove('pc-placement-target'));
  if (_pcPlacementCleanup) {
    _pcPlacementCleanup();
    _pcPlacementCleanup = null;
  }
  _pcPlacement = null;
}

// ── 内部: カード操作ヘルパ ───────────────────────────────────────

/** カード名（card-info-panel.js の名前解決と同じ: data-card-name → 画像の alt） */
function _cardNameOf(cardEl) {
  if (!cardEl) return '';
  return (cardEl.dataset && cardEl.dataset.cardName) || (cardEl.tagName === 'IMG' ? (cardEl.alt || '') : '') || '';
}

/**
 * 「カードの詳細」: 左のカード情報パネルに出す（クリックしたときと同じ）。
 * パネルを畳んでいたら開く。パネルが無い・見えない画面（スマホ縦）では出さない（スマホはアクションシートの「詳細」）。
 */
function _cardDetailItem(cardEl) {
  const col = document.querySelector('.sol-cip-col');
  if (!col || !col.getClientRects().length) return null;
  const name = _cardNameOf(cardEl);
  return {
    label: 'カードの詳細',
    action: () => {
      if (col.classList.contains('hidden')) document.getElementById('cipToggle')?.click();
      showCardByName(name, cardEl.src || '', cardEl.closest('.sample-art') ? 'official_sample' : undefined);
    },
  };
}

/**
 * カードをデッキに戻す（リプレイログ付き、状態クリア）
 */
function _returnToDeck(wrapper, img) {
  const isEx   = isExCard(wrapper); // 今の置き場所ではなく、EXデッキのカードかどうかで戻り先を決める
  const cardId = img.id;

  // FLIPアニメーション用に移動前の座標を確定処理より前に取得する
  // （スタイルリセットで見た目上の位置が変わる前の、ユーザーに見えている位置を使う）
  const firstRect = wrapper.getBoundingClientRect();

  if (typeof window.replayLog === 'function') {
    window.replayLog({ actionType: 'returnToDeck', cardId, isEx });
  }

  // スタイルと状態をクリア
  wrapper.style = '';
  applyCardState(wrapper, {});

  const sourceSlot = wrapper.parentElement;
  const pool = document.getElementById(isEx ? 'poolRow2' : 'poolRow');
  pool.appendChild(wrapper);
  restackCustomSlot(sourceSlot); // 元がフィールドスロットなら残りを詰め直す

  flipMoveClone(wrapper, firstRect);
}

/**
 * 墓地ゾーンのDOM要素を取得する（mobile-ui.js の getSideSlot(true) と同一セレクタ）。
 * @returns {Element|null}
 */
function _getGraveZoneElement() {
  return document.querySelector('.sol-grave .side-slot');
}

/**
 * 除外ゾーンのDOM要素を取得する（mobile-ui.js の getSideSlot(false) と同一セレクタ）。
 * 墓地(.sol-grave)を除外することで、想定妨害トレイ等が将来増えても誤って墓地を掴まない。
 * @returns {Element|null}
 */
function _getBanishZoneElement() {
  return document.querySelector('.side-slots-container .sol-side-area:not(.sol-grave) .side-slot');
}

/**
 * 手札ゾーン（center-slot）のDOM要素を取得する。
 * @returns {Element|null}
 */
function _getHandZoneElement() {
  return document.querySelector('.sol-hand-area .center-slot');
}

/**
 * カードを固定ゾーン（墓地/除外/手札）へ移動する。
 * drag-drop.js の executeDrop をそのまま使い、配置・リプレイ記録ロジックを複製しない
 * （moveMobileSelectionTo と同じパターン）。
 * @param {Element} wrapper     - .tier-item-wrapper
 * @param {Element|null} zoneEl - 移動先のゾーンDOM要素
 */
function _moveToFixedZone(wrapper, zoneEl) {
  if (!zoneEl) {
    console.warn('[context-menu] 移動先のゾーン要素が見つかりません');
    return;
  }
  const dropZoneInfo = getDropZoneInfo(zoneEl);
  if (!dropZoneInfo) {
    console.warn('[context-menu] ゾーン種別を判定できませんでした');
    return;
  }
  // FLIPアニメーション用に移動前の座標を executeDrop（DOM移動）より前に取得する
  const firstRect = wrapper.getBoundingClientRect();
  executeDrop({ type: 'card', element: wrapper }, dropZoneInfo, zoneEl, {});
  flipMoveClone(wrapper, firstRect);
}

/**
 * カードを同スロットの一番下（z-index最背面）に移動
 */
function _placeUnder(wrapper, slot) {
  const items = Array.from(slot.querySelectorAll('.tier-item-wrapper'));
  if (items.length <= 1) return;

  // DOMの先頭に移動（先頭＝最背面）し、z-index / top は共通関数で詰め直す
  slot.insertBefore(wrapper, slot.firstChild);
  restackCustomSlot(slot);

  // リプレイ記録
  _logState(wrapper);
}

/**
 * 現在のゾーン・状態でリプレイログを記録
 * @param {Element} wrapper
 * @param {{inPlace?: boolean}} [opts] inPlace=true は「同じゾーンの中で表示形式だけを変えた」記録。
 *   再生側は位置（重なりの順番・手札の並び）を動かさず、状態だけを適用する。
 *   省略（一番下に重ねる等、位置も変える操作）は従来どおり zIndex から位置を決める。
 */
function _logState(wrapper, { inPlace = false } = {}) {
  if (typeof window.replayLog !== 'function') return;
  // img.tier-item または div.tier-item（プロキシ）どちらでも .tier-item で取れる
  const cardEl = wrapper.querySelector('.tier-item');
  if (!cardEl) return;
  const state  = getCardState(wrapper);
  const zoneEl = wrapper.parentElement;
  // drag-drop.js の getZoneId は zoneElement が null だと className アクセスで例外になるため、
  // 呼び出し側（ここ）でガードする（_getZoneId 複製時にあったnullガードを踏襲）
  window.replayLog({
    actionType:  'moveCard',
    cardId:      cardEl.id,
    zoneId:      zoneEl ? getZoneId(zoneEl) : 'unknown',
    zIndex:      wrapper.style.zIndex || '1',
    transform:   wrapper.style.transform || '',
    orientation: state.orientation,
    face:        state.face,
    ...(inPlace ? { inPlace: true } : {}),
  });
}
