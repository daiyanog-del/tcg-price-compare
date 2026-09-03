/**
 * main.js — 一人回しシミュレータ エントリーポイント
 * ベース: Solo Mode (Fugarta, MIT) を改変
 *
 * 注意: このモジュールはエントリ専用。他モジュールから import しないこと。
 * templates/solitaire.html は `main.js?v=...` というクエリ付きURLで読み込むため、
 * 他モジュールが `import ... from './main.js'`（クエリ無し）とすると、ブラウザは
 * 別URL＝別モジュールインスタンスとして main.js をもう一度評価してしまい、
 * initializeApp() が二重に走る（イベント登録・MutationObserver等が全て二重化する）。
 * 共有したい関数は utils/ 配下の独立モジュールに切り出すこと（例: utils/toast.js）。
 */

import { initializeDesktopDragDrop, enableTouchDrag } from './components/drag-drop.js';
import { initializeCards } from './components/card-manager.js';
import { initializeCounter } from './components/counter-manager.js';
import { initializeEventListeners } from './ui/event-handlers.js';
import { initReplayUI } from './ui/replay-ui.js';
import { registerCardImage, registerCardIsEx } from './services/replay-service.js';
import { initTokenGenerator } from './ui/token-generator.js';
import { initRandomTools } from './ui/random-tools.js';
import { initCardInfoPanel } from './ui/card-info-panel.js';
import { saveSessionResume, loadSessionResume } from './services/save-load-service.js';
import { initOpponentTray, updateCipWidth } from './ui/opponent-tray.js';
import { initFeedbackModal } from './ui/feedback-modal.js';
import { initSidebarToggle } from './ui/sidebar-toggle.js';
import { initMobileUI } from './ui/mobile-ui.js';
import { isMobilePortrait } from './utils/viewport.js';
import { showToast } from './utils/toast.js';

/**
 * カード追加時にリプレイ画像辞書へ登録するフック
 * MutationObserver でプールの変化を監視し cardId→src を登録
 */
function initCardImageRegistration() {
  const observePool = (poolId) => {
    const pool = document.getElementById(poolId);
    if (!pool) return;
    const obs = new MutationObserver(mutations => {
      mutations.forEach(mut => {
        mut.addedNodes.forEach(node => {
          if (!(node instanceof Element)) return;
          // img.tier-item（発売済み）または div.tier-item（プロキシ）どちらでも .tier-item で取れる
          const cardEl = node.querySelector('.tier-item')
            ?? (node.matches?.('.tier-item') ? node : null);
          if (cardEl?.id) {
            // プロキシは src がないため 'proxy:カード名' センチネルを使う
            // （deck-input-panel.js が registerCardImage を呼ぶ前に MutationObserver が
            //   先に発火した場合のフォールバック。実際は deck-input-panel.js の方が先）
            const src = cardEl.src || cardEl.dataset?.proxySrc || '';
            if (src) registerCardImage(cardEl.id, src);
            if (poolId === 'poolRow2') registerCardIsEx(cardEl.id);
          }
        });
      });
    });
    obs.observe(pool, { childList: true });
  };

  observePool('poolRow');
  observePool('poolRow2');
}

/**
 * ビューポート高さに合わせて --slot-width を動的に設定する。
 *
 * 全縦スペースの内訳:
 *   固定:    nav高さ + トレイヘッダ高さ + リプレイバー + 各種パディング/マージン
 *   比例:    slot_w × 7.25
 *            = フィールド3行(slot_w×1.45×3) + center-row(slot_w×1.45)
 *              + imagePool(slot_w×1.45)
 *   (デッキ/EX行の余分な×1.1空白を撤去したため 7.54→7.25)
 *
 * トレイ開閉・ウィンドウリサイズのたびに再計算し、
 * 常にスクロールなしで全体が収まるよう自動調整する。
 */
function fitFieldToViewport() {
  const tray   = document.getElementById('opponentTray');
  const replay = document.getElementById('replayBarContainer');
  if (!tray) return;

  // 旧ヘッダー(.sol-nav)はサイドバーへ統合し廃止。盤面上部に占有高さは無い。
  const navH    = 0;
  const trayH   = tray.offsetHeight;   // 閉=28px 開=閉+ボディ高さ
  const replayH = replay ? replay.offsetHeight + 4 : 36; // margin-top(4px)込み

  // スマホ検出: 短辺 < 500px → スマホ（縦/横向き問わず検知）。横向き分岐はこの基準のまま（司令塔決定A-1）。
  const isPhone     = Math.min(window.innerWidth, window.innerHeight) < 500;
  const isLandscape = window.innerWidth > window.innerHeight;
  // 縦向きスマホ分岐の条件は CSS の @media (max-width:767px) and (orientation:portrait) と
  // 完全に一致させる（司令塔決定A-1）。isPhone(短辺<500)基準のままだと500〜767px縦長で
  // CSSはモバイルレイアウトなのにJSはPC計算式を使う不整合が起きるため。
  const isPortraitMobile = isMobilePortrait();

  // 高さ基準:
  //   横向きスマホ   → 盤面3行のみをビューポートに収め、手札/デッキはスクロール
  //   縦向きスマホ   → 設計書 §8.2: 下部固定バー52px込みで縦スクロールなしに収める
  //   PC/タブレット  → 全体フィット (比例係数 7.25)
  let slotW_h;
  if (isPhone && isLandscape) {
    // mainContainer padding-top(4) + row-gaps(20) + sol-field-area padding-bottom(6)
    const BOARD_FIXED = 30;
    const availForBoard = window.innerHeight - navH - trayH - replayH - BOARD_FIXED;
    slotW_h = Math.floor(availForBoard / 4.35);
  } else if (isPortraitMobile) {
    // 設計書 §A-2（2026-09-03 第2次実機検収対応）: 墓地｜除外の左右2分割をやめ、
    // 盤面3段＋手札＋墓地＋除外＋EXの7段が縦に並ぶ → 7×1.45=10.15
    // 固定分（2026-09-03 追加修正: 375×660 Playwright実測に基づき圧縮。旧158pxから
    //   mobile.css の gap/padding 圧縮とあわせて再計算）:
    //   下部バー52 + .sol-field-area内の要素間gap 6×4(盤面↔手札↔墓地↔除外↔EX)=24
    //   + 盤面内gap 8×2=16 + 4行の枠余白(padding2px×2+border2px=6)×4行=24
    //   + 盤面custom-slotのborder(1px×2辺×3行)=6 + フィールドpadding-bottom2 + 安全余白2
    //   = 52+24+16+24+6+2+2 = 126px（実測 fieldHeight=605.7px で基準606px以内に収まることを確認）。
    // 値（10.15・126・mobile.css の gap:6px/8px・padding:2px・padding-bottom:2px、
    // 下記の幅式の 8・50）を変更する場合は同条件で再実測すること。
    const MOBILE_PORTRAIT_FIXED = 126;
    slotW_h = Math.floor((window.innerHeight - MOBILE_PORTRAIT_FIXED) / 10.15);
  } else {
    // slot-width に依存しない固定オーバーヘッド（実測ベースに引き直し）:
    //   mainContainer padding-top              :  4px
    //   行間(center-row mt4 + imagePool mt4)   :  8px
    //   center-row内固定(label11+mb4+pad4+border1): 22px (やや余裕)
    //   imagePool内固定(label11+mb4+pad4+border1) : 22px (やや余裕)
    //   sol-field-area padding-bottom          :  6px
    // ※やや大きめ(アンダーシュート寄り)に置き、実測補正の縮小ループを避ける
    const FIXED_MISC = 4 + 8 + 22 + 22 + 6; // 62px
    const fixed      = navH + trayH + replayH + FIXED_MISC;
    const available  = window.innerHeight - fixed;
    slotW_h = Math.floor(available / 7.25);
  }

  // 横幅基準: .sol-main の実測幅を使う。サイドバー開閉で幅が変わるため実測値を使用。
  // ・縦向きスマホ: CSS で side-slots-container が縦積みになるため 7.2 列 / 88px
  //     (6列グリッド + 1.2列サイド) × slot + (グリッドgap50 + mainContentgap10 + 側面padding16 + 各種余白12)
  // ・横向きスマホ: side-slots-container が横並びのため 8.4 列 / 126px
  //     (6列グリッド + 2.4列サイド×2) × slot + (gap50 + gap10 + gap10 + 側面padding32 + 各種余白24)
  // ・PC/タブレット: 既存の 7.1 列 / 86px
  const mainEl  = document.querySelector('.sol-main');
  const availW  = mainEl ? mainEl.clientWidth : window.innerWidth;

  let slotW_w;
  if (isPortraitMobile) {
    // 設計書 §8.2: 6列・サイド無し（墓地/除外は下段の横並び2分割になるため列数に含めない）
    // 2026-09-03 司令塔が 375/412/744 幅で実測済み（上記コメント参照）。
    slotW_w = Math.floor((availW - 8 - 50) / 6);
  } else if (isPhone && isLandscape) {
    slotW_w = Math.floor((availW - 126) / 8.4);
  } else {
    slotW_w = Math.floor((availW - 86) / 7.1);
  }

  const minW = isPhone ? 26 : 60;
  const maxW = isPhone ? 200 : 124;
  let slotW = Math.max(minW, Math.min(maxW, Math.min(slotW_h, slotW_w)));
  applySlotWidth(slotW);

  // 実測補正: 推定で適用後、.sol-field-area の下端がビューポートを超えていれば縮小。
  // 横向きスマホは「盤面3行のみフィット・手札/デッキはスクロール許容」が仕様のため除外。
  // 縦向きスマホは下部固定バー(52px)の分だけ基準を上に詰める（設計書 §8.2）。
  // 2026-09-03 司令塔が 375/412/744 幅で実測: 縦横スクロールなし・カード幅 52/59/113px を確認。
  if (!(isPhone && isLandscape)) {
    const bottomReserve = isPortraitMobile ? 52 : 0;
    slotW = correctSlotWidthByMeasurement(slotW, minW, bottomReserve);
  }

  // 盤面の実測高さを CSS 変数へ反映 → 墓地・除外の下端を盤面と一致させる
  // correctSlotWidthByMeasurement が同期 reflow 済みのため offsetHeight は最終値を返す。
  const fieldEl = document.querySelector('.custom-layout');
  if (fieldEl) {
    document.documentElement.style.setProperty('--field-height', `${fieldEl.offsetHeight}px`);
  }

  // パネル幅は最終 slotW 確定後に1回だけ再計算
  requestAnimationFrame(() => updateCipWidth());
}

/**
 * --slot-width CSS変数を適用するヘルパー。
 * correctSlotWidthByMeasurement と fitFieldToViewport の両方から呼ぶ。
 */
function applySlotWidth(w) {
  document.documentElement.style.setProperty('--slot-width', `${w}px`);
}

/**
 * 推定で適用した slotW を実測で補正する。
 *
 * .sol-field-area の下端が viewport を超えていたら、超過分だけ
 * slotW を比率縮小して再適用する。getBoundingClientRect() は同期 reflow を
 * 強制するので、ループ内で即座に新レイアウトを観測でき RAF 不要。
 * 縮小のみ（既に収まっている場合は何もしない）。
 *
 * 収束根拠:
 *   フィールド高さ H(slotW) ≈ a·slotW + b（a>0 比例項、b>0 固定項）の
 *   アフィン関数なので、比率縮小の残差は反復ごとに b/H (<1) 倍に幾何減衰し、
 *   PC では 2〜3 反復でサブピクセル以下に収まる。
 *
 * @param {number} slotW         推定で適用済みの slot-width(px)
 * @param {number} minW          下限クランプ(px)
 * @param {number} [bottomReserve=0]  下部に確保する固定領域(px)。縦向きスマホの下部固定バー(52px)分（設計書 §8.2）
 * @returns {number}      補正後の slot-width(px)
 */
function correctSlotWidthByMeasurement(slotW, minW, bottomReserve = 0) {
  const field = document.querySelector('.sol-field-area');
  if (!field) return slotW;

  const SAFE_MARGIN = 2;    // サブピクセル/丸め誤差ぶんの安全余白(px)
  const MAX_ITER    = 4;    // 反復上限
  const MIN_RATIO   = 0.80; // 1反復あたりの最小縮小比（過補正の暴走防止）

  for (let i = 0; i < MAX_ITER; i++) {
    const rect     = field.getBoundingClientRect(); // 同期 reflow で確定値を取得
    const overflow = rect.bottom - (window.innerHeight - bottomReserve - SAFE_MARGIN);
    if (overflow <= 0) break; // 収まっている → 終了（拡大はしない）

    const fieldHeight = rect.height;
    if (fieldHeight <= 0) break;

    // 現在の高さに対し「overflow ぶん削る比率」を slotW に適用
    let ratio = (fieldHeight - overflow) / fieldHeight;
    ratio = Math.max(MIN_RATIO, ratio); // 1反復で過剰に縮みすぎるのを抑制

    let next = Math.floor(slotW * ratio);
    if (next >= slotW) next = slotW - 1; // 縮小のみ: 最低でも -1px 進める

    if (next < minW) {                   // 下限クランプ
      next = minW;
      if (slotW === minW) break;         // 既に下限 → CSS overflow に委ねて終了
    }

    slotW = next;
    applySlotWidth(slotW);
  }

  return slotW;
}

let _fitTimer = null;

/**
 * アプリケーション初期化
 */
async function initializeApp() {
  // 端末間同期（P2）: 一人回しページを開いたときも pull する（設計文書 §11）。
  // 保存デッキは savedDecksSet/renderSavedDecks 経由で適用されデッキ一覧に反映される。
  // 購入候補はこのページに適用先(wishSave等)が無いため、sync-client.js側でrevを
  // 進めずに据え置く（次に購入候補タブのあるページを開いたときに正しく取得・適用される）
  if (window.SyncClient) window.SyncClient.pullIfNeeded();

  // デスクトップドラッグ&ドロップを初期化
  initializeDesktopDragDrop();

  // ダミー画像なし（デッキ読込後にカードが追加される）
  initializeCards([]);

  // カウンターを初期化
  document.querySelectorAll('.counter-container').forEach(container => {
    initializeCounter(container);
  });

  // タッチ対応
  const parentCounter = document.querySelector('#parent.counter-container');
  if (parentCounter) {
    parentCounter.addEventListener('touchstart', enableTouchDrag, { passive: false });
  }

  // イベントリスナーを設定
  initializeEventListeners();

  // リプレイUI初期化（window.replayLog バインド含む）
  initReplayUI();

  // トークン生成UI初期化
  initTokenGenerator();

  // コイントス・ダイスロール初期化
  initRandomTools();

  // カード詳細パネル初期化
  initCardInfoPanel();

  // 相手の想定妨害ミニ盤面を初期化
  initOpponentTray();

  // 左サイドバー・右パネルの開閉を初期化
  initSidebarToggle();

  // スマホ縦向き専用UI（下部バー・アクションシート・詳細パネル・簡易メニュー等）を初期化。
  // isPhone && !isLandscape 以外では内部で何もしない（フェーズ1・設計書 §8）
  initMobileUI();

  // フィードバック（不具合・要望）モーダルを初期化
  initFeedbackModal();

  // ゾーン枚数カウント初期化
  initZoneCounts();

  // カード追加時の画像登録
  initCardImageRegistration();

  // ページ遷移後の盤面復元（card:// センチネル対応のため await が必要）
  const restored = await loadSessionResume();
  if (restored) {
    // 復元したカードをリプレイ画像辞書に登録
    // img.tier-item（発売済み）または div.tier-item（プロキシ）どちらも .tier-item で取れる
    document.querySelectorAll('.tier-item').forEach(cardEl => {
      const src = cardEl.src || cardEl.dataset?.proxySrc || '';
      if (cardEl.id && src) {
        registerCardImage(cardEl.id, src);
        const wrapper = cardEl.closest('.tier-item-wrapper');
        if (wrapper?.id.includes(' ex')) registerCardIsEx(cardEl.id);
      }
    });
    _showToast('前回の盤面を復元しました');
  }
  // M-3: 復元の有無に関わらず、盤面確定後に mobile-ui.js の初見導線CTA判定を確定させる
  // （restored=false でも updateEmptyCta() は呼ぶ必要があるため if の外に出す）。
  document.dispatchEvent(new CustomEvent('sol-session-restored'));

  // ビューポートに合わせて --slot-width を初期設定
  // ※ fitFieldToViewport 内の RAF でパネル幅（--cip-width）も更新される
  fitFieldToViewport();

  // ウィンドウリサイズ時に再計算（デバウンス 150ms）
  window.addEventListener('resize', () => {
    clearTimeout(_fitTimer);
    _fitTimer = setTimeout(fitFieldToViewport, 150);
  });

  // デスクトップPWA（standalone）では window.resize が発火しないケースがある（Chromium既知挙動）。
  // visualViewport の resize を追加のリスナーとして登録しフォールバックとする。
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', () => {
      clearTimeout(_fitTimer);
      _fitTimer = setTimeout(fitFieldToViewport, 150);
    });
  }

  // 相手妨害トレイの開閉に連動して再計算
  window.addEventListener('opp-tray-resize', fitFieldToViewport);

  console.log('一人回しシミュレータ initialized');
}

/**
 * 墓地・除外スロットのカード重なり量を枚数に応じて動的調整する。
 * 親エリア(.sol-side-area)の確定高さを基準にし、
 * 枚数が増えるほど深く重ねて高さを抑制する。
 * 最小圧縮でも収まらない場合は overflow-y:auto によりゾーン内スクロールが発動する。
 */
function adjustSideStack(slotEl) {
  const area = slotEl.closest('.sol-side-area');
  if (!area) return;

  // フィールド高さを取得: fitFieldToViewport が設定した実測値 (--field-height) を優先し、
  // 未設定時は CSS 変数から計算する（初回描画前などのフォールバック）。
  const rootStyle = getComputedStyle(document.documentElement);
  const slotWidth = parseFloat(rootStyle.getPropertyValue('--slot-width')) || 120;
  const slotHeight = slotWidth * 1.45; // --slot-height = --slot-width * 1.45
  const gap = parseFloat(rootStyle.getPropertyValue('--gap')) || 12;
  const fieldH = parseFloat(rootStyle.getPropertyValue('--field-height'));
  const areaMaxH = fieldH || (3 * slotHeight + 2 * gap); // CSS の max-height と同じ基準

  // side-slotの利用可能高さ = エリア上限 - ラベル - 縦padding
  const label = area.querySelector('.pool-label');
  const labelH = label ? label.offsetHeight : 0;
  const areaStyle = getComputedStyle(area);
  const paddingV = parseFloat(areaStyle.paddingTop) + parseFloat(areaStyle.paddingBottom);
  const avail = areaMaxH - labelH - paddingV;

  const wrappers = slotEl.querySelectorAll('.tier-item-wrapper');
  const N = wrappers.length;

  // 1枚以下は重なり調整不要: 変数をリセットしてCSS既定（90%重ね）に戻す
  if (N <= 1) {
    slotEl.style.removeProperty('--stack-overlap');
    return;
  }

  // カード高さ: 先頭ラッパーの実測値、無ければCSS変数から算出
  const cardH = wrappers[0].offsetHeight || slotHeight;

  const MIN_VISIBLE = 6;            // カードが識別できる最小ステップ(px)
  const naturalStep = cardH * 0.1; // デフォルト（90%重ね）時の増分

  // 全枚数を avail に収めるための理想ステップを計算
  const idealStep = avail > cardH ? (avail - cardH) / (N - 1) : MIN_VISIBLE;

  // 下限: MIN_VISIBLE、上限: naturalStep（デフォルトより広げない）
  const step = Math.max(MIN_VISIBLE, Math.min(naturalStep, idealStep));

  // CSS変数に反映（margin-top = -(cardH - step)）
  slotEl.style.setProperty('--stack-overlap', -(cardH - step) + 'px');
}

/** 手札・EXデッキ・デッキ・墓地・除外の枚数をラベル横にリアルタイム表示 */
function initZoneCounts() {
  const zones = [
    { label: document.querySelector('#imagePool .pool-label'),     cards: document.getElementById('poolRow') },
    { label: document.querySelector('#imagePool2 .pool-label'),    cards: document.getElementById('poolRow2') },
    { label: document.querySelector('.sol-hand-area .pool-label'), cards: document.querySelector('.center-slot') },
    { label: document.querySelector('.sol-grave .pool-label'),     cards: document.querySelector('.sol-grave .side-slot'), isSideSlot: true },
    { label: document.querySelector('.side-slots-container .sol-side-area:not(.sol-grave) .pool-label'),
      cards:  document.querySelector('.side-slots-container .sol-side-area:not(.sol-grave) .side-slot'), isSideSlot: true },
  ];

  // リサイズ時に全墓地・除外スロットを再調整（デバウンス付き）
  let _resizeTimer = null;
  const sideSlotEls = zones.filter(z => z.isSideSlot).map(z => z.cards).filter(Boolean);
  window.addEventListener('resize', () => {
    clearTimeout(_resizeTimer);
    _resizeTimer = setTimeout(() => {
      sideSlotEls.forEach(adjustSideStack);
    }, 150);
  });

  for (const { label, cards, isSideSlot } of zones) {
    if (!label || !cards) continue;
    const badge = document.createElement('span');
    badge.className = 'zone-count';
    label.appendChild(badge);
    const update = () => {
      badge.textContent = cards.querySelectorAll('.tier-item-wrapper').length;
      if (isSideSlot) adjustSideStack(cards);
    };
    update();
    new MutationObserver(update).observe(cards, { childList: true, subtree: true });
  }
}

/** ページ遷移直前に盤面をsessionStorageへ退避 */
window.addEventListener('pagehide', () => {
  saveSessionResume();
});

/**
 * 短時間表示のトースト通知。実体は utils/toast.js（mobile-ui.js からもそちらを import する。
 * main.js は他モジュールから import されないエントリ専用のため、ここには置かない）。
 * 既存の内部呼び出し（_showToast(...)）はそのまま残し、実体だけ showToast へ委譲する。
 */
function _showToast(msg) { showToast(msg); }

window.addEventListener('DOMContentLoaded', initializeApp);
