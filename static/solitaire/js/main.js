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

import { initializeDesktopDragDrop } from './components/drag-drop.js';
import { initializeCards, syncCardIdCounter } from './components/card-manager.js';
import { collectReplayCardIds } from './utils/session-resume-store.js';
import { initializeCounter } from './components/counter-manager.js';
import { initializeEventListeners } from './ui/event-handlers.js';
import { initReplayUI } from './ui/replay-ui.js';
import { registerCardImage, registerCardIsEx, registerCardName, registerCardType, restoreReplaySession, rebuildAtCursor } from './services/replay-service.js';
import { initTokenGenerator } from './ui/token-generator.js';
import { initRandomTools } from './ui/random-tools.js';
import { initCardInfoPanel, showCardByName } from './ui/card-info-panel.js';
import { initSampleBlur } from './ui/sample-blur.js';
import { loadSessionResume } from './services/save-load-service.js';
import { migrateSlotsToReplays } from './services/slot-migration.js';
import { updateCipWidth, syncCipPosition } from './ui/cip-layout.js';
import { initFeedbackModal } from './ui/feedback-modal.js';
import { initSidebarToggle } from './ui/sidebar-toggle.js';
import { initSideTips } from './ui/side-tips.js';
import { initMobileUI, setMobileRecordingState, resumePlaybackModeIfNeeded, openCardDetailByName, enterPlaybackModeForRecView, reservePlaybackSpaceForUrlReplay } from './ui/mobile-ui.js';
import { initRecView } from './ui/rec-view.js';
import { setPcRecordingState } from './ui/pc-recording.js';
import { isMobilePortrait } from './utils/viewport.js';
import { showToast } from './utils/toast.js';
import { hasReplayInUrl as isReplayInUrl, isReplayAtEnd } from './utils/replay-url.js';
import { initStartChoice } from './ui/start-choice.js';
import { loadDeckFromNeuron, clearEverything } from './ui/deck-input-panel.js';
import { receiveDeckHandoff } from '../../shared/deck-handoff.js';
import { receiveNewCards } from '../../shared/newcards-entry.js';
import { initDeckEditUI, setDeckLink, restoreLinkState } from './ui/deck-edit-ui.js';
import { initDeckReplaysUI } from './ui/deck-replays-ui.js';
import { initReplayRenameUI } from './ui/replay-rename-ui.js';
import { captureSessionToResume, consumeStartupOffers, takeUndoRestored } from './ui/undo-point.js';

/**
 * リプレイ画像辞書に登録する src を決める。
 * 未発売SAMPLE（wrapper に sample-art）で名前があれば 'card://カード名'（再生時に再解決＝発売後はクリーン画像）。
 * それ以外は従来どおり実URL / プロキシのセンチネル。
 */
function registrySrcOf(cardEl) {
  const wrapper = cardEl.closest?.('.tier-item-wrapper');
  const name = cardEl.dataset?.cardName;
  if (wrapper?.classList.contains('sample-art') && name) return `card://${name}`;
  return cardEl.src || cardEl.dataset?.proxySrc || '';
}

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
            const src = registrySrcOf(cardEl);
            if (src) registerCardImage(cardEl.id, src);
            registerCardName(cardEl.id, cardEl.dataset?.cardName); // 名前も記録（共有リプレイの names・リンク画像に必要）
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
 *   固定:    nav高さ + リプレイバー + 各種パディング/マージン
 *   比例:    slot_w × 7.25
 *            = フィールド3行(slot_w×1.45×3) + center-row(slot_w×1.45)
 *              + imagePool(slot_w×1.45)
 *   (デッキ/EX行の余分な×1.1空白を撤去したため 7.54→7.25)
 *   （2026-10-16: 盤面の上の「相手の想定妨害」トレイを廃止し、その高さを固定分から外した）
 *
 * 左右パネルの開閉・ウィンドウリサイズのたびに再計算し、
 * 常にスクロールなしで全体が収まるよう自動調整する。
 */
function fitFieldToViewport() {
  const replay = document.getElementById('replayBarContainer');

  // 旧ヘッダー(.sol-nav)はサイドバーへ統合し廃止。盤面上部に占有高さは無い。
  const navH    = 0;
  const replayH = replay ? replay.offsetHeight + 4 : 36; // margin-top(4px)込み

  // スマホ検出: 短辺 < 500px → スマホ（縦/横向き問わず検知）。横向き分岐はこの基準のまま（司令塔決定A-1）。
  const isPhone     = Math.min(window.innerWidth, window.innerHeight) < 500;
  const isLandscape = window.innerWidth > window.innerHeight;
  // 縦向きスマホ分岐の条件は CSS の @media (max-width:767px) and (orientation:portrait) と
  // 完全に一致させる（司令塔決定A-1）。isPhone(短辺<500)基準のままだと500〜767px縦長で
  // CSSはモバイルレイアウトなのにJSはPC計算式を使う不整合が起きるため。
  const isPortraitMobile = isMobilePortrait();
  // 画面録画用の表示（縦向きスマホは再生モードと同じ計算で、下部の予約量だけ rec-view.js が変える）。
  // 横向きスマホも PC と同じ並び（盤面3段＋手札/EX を全部収める）にする（2026-10-29 ユーザー要望）
  const isRecView = document.body.classList.contains('sol-recview') && !isPortraitMobile;

  // 高さ基準:
  //   横向きスマホ   → 盤面3行のみをビューポートに収め、手札/デッキはスクロール
  //   縦向きスマホ   → 設計書 §8.2: 下部固定バー52px込みで縦スクロールなしに収める
  //   PC/タブレット  → 全体フィット (比例係数 7.25)
  let slotW_h;
  let mobileBottomReserve = 0; // タスクB-3: isPortraitMobile分岐でのみ実測値に更新される（それ以外は0のまま未使用）
  if (isPhone && isLandscape && !isRecView) {
    // mainContainer padding-top(4) + row-gaps(20) + sol-field-area padding-bottom(6)
    const BOARD_FIXED = 30;
    const availForBoard = window.innerHeight - navH - replayH - BOARD_FIXED;
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
    // タスクB-3: 126px のうち下部バー分(既定52px)を --sol-mobile-bottom-reserve から読む。
    // 再生モード時は mobile-ui.js がこの変数を88pxへ書き換えて再フィットさせる（既定52pxなら計算結果は不変）。
    const MOBILE_PORTRAIT_FIXED_BASE = 74; // 126 - 52（下部バー分を除いた固定分）
    mobileBottomReserve = getMobileBottomReserve();
    const MOBILE_PORTRAIT_FIXED = MOBILE_PORTRAIT_FIXED_BASE + mobileBottomReserve;
    slotW_h = Math.floor((window.innerHeight - MOBILE_PORTRAIT_FIXED) / 10.15);
  } else if (isRecView) {
    // 画面録画用の表示（rec-view.js・rec-view.css）: デッキ（#imagePool）と再生バーを隠し、上に帯を置く。
    // 盤面3段＋手札/EXの1段 → 4×1.45=5.8。固定分は下の通常時から imagePool の行（mt4＋22）を除き、帯（＋余白8）を足したもの
    // 横長の横向きスマホは帯を置かない（高さ0。rec-view.css）ので、0 をそのまま使う
    // 横向きスマホは手札/EX の見出しを縦書きで枠の左に置くので、center-row 内の固定分は余白と枠（3+3+1+1）だけになる
    const bandH = document.getElementById('solRecBand')?.offsetHeight ?? 56;
    const rowFixed = document.body.classList.contains('sol-recview-landscape') ? 8 : 22;
    const FIXED_REC = 4 + 4 + rowFixed + 6 + bandH + 8;
    slotW_h = Math.floor((window.innerHeight - FIXED_REC) / 5.8);
  } else {
    // slot-width に依存しない固定オーバーヘッド（実測ベースに引き直し）:
    //   mainContainer padding-top              :  4px
    //   行間(center-row mt4 + imagePool mt4)   :  8px
    //   center-row内固定(label11+mb4+pad4+border1): 22px (やや余裕)
    //   imagePool内固定(label11+mb4+pad4+border1) : 22px (やや余裕)
    //   sol-field-area padding-bottom          :  6px
    // ※やや大きめ(アンダーシュート寄り)に置き、実測補正の縮小ループを避ける
    const FIXED_MISC = 4 + 8 + 22 + 22 + 6; // 62px
    const fixed      = navH + replayH + FIXED_MISC;
    const available  = window.innerHeight - fixed;
    slotW_h = Math.floor(available / 7.25);
  }

  // 縦向きスマホは5列固定。PC/横向きは適用後の実寸から横幅を決める。
  const mainEl = document.querySelector('.sol-field-area');
  const availW = mainEl ? mainEl.clientWidth : window.innerWidth;
  const slotW_w = isPortraitMobile
    ? Math.floor((availW - 8 - 40) / 5)
    : Infinity;

  const minW = isPhone ? 26 : 60;
  // 録画用の表示は左右のパネルが無い分、PC でも通常（124px）より大きく出す
  const maxW = isPhone ? 200 : (isRecView ? 200 : 124);
  let slotW = Math.max(minW, Math.min(maxW, Math.min(slotW_h, slotW_w)));
  applySlotWidth(slotW);
  if (!isPortraitMobile) {
    slotW = fitBoardWidthByMeasurement(slotW, minW);
  }

  // 実測補正: 推定で適用後、.sol-field-area の下端がビューポートを超えていれば縮小。
  // 横向きスマホは「盤面3行のみフィット・手札/デッキはスクロール許容」が仕様のため除外（画面録画用の表示は全部収めるので補正する）。
  // 縦向きスマホは下部固定バー(52px)の分だけ基準を上に詰める（設計書 §8.2）。
  // 2026-09-03 司令塔が 375/412/744 幅で実測: 縦横スクロールなし・カード幅 52/59/113px を確認。
  if (!(isPhone && isLandscape) || isRecView) {
    const bottomReserve = isPortraitMobile ? mobileBottomReserve : 0;
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
 * 盤面6列・サイド領域・カード詳細用余白の実寸から、収まる最大カード幅を探す。
 * .sol-main は右操作パネルも含むため幅の基準にしない。
 * 最小幅でも収まらない場合はカードの操作性を優先し、横スクロールを残す。
 */
function fitBoardWidthByMeasurement(slotW, minW) {
  const field = document.querySelector('.sol-field-area');
  const board = document.getElementById('mainContainer');
  const content = board?.querySelector('.main-content');
  if (!field || !board || !content) return slotW;

  const fits = () => {
    const fieldStyle = getComputedStyle(field);
    const boardStyle = getComputedStyle(board);
    const contentStyle = getComputedStyle(content);
    const children = [...content.children].filter(el => getComputedStyle(el).display !== 'none');
    const padding = style => parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
    const available = field.clientWidth - padding(fieldStyle) - padding(boardStyle);
    // flex の中央揃えで左にもはみ出すため、scrollWidth だけでは超過を測れない。
    const required = children.reduce((sum, el) => {
      const style = getComputedStyle(el);
      return sum + Math.max(el.getBoundingClientRect().width, el.scrollWidth)
        + parseFloat(style.marginLeft) + parseFloat(style.marginRight);
    }, 0) + (parseFloat(contentStyle.columnGap) || 0) * Math.max(0, children.length - 1);
    return required <= available;
  };

  if (fits()) return slotW;
  let low = minW;
  let high = slotW - 1;
  while (low < high) {
    const candidate = Math.ceil((low + high) / 2);
    applySlotWidth(candidate);
    if (fits()) low = candidate;
    else high = candidate - 1;
  }
  applySlotWidth(low);
  return low;
}

/**
 * タスクB-3: 縦向きスマホの下部予約量(px)を CSS 変数 --sol-mobile-bottom-reserve から読む。
 * 通常時は mobile.css が既定値 52px を定義する。再生モード中は mobile-ui.js がこの変数を
 * 88px に書き換えてから resize イベントを発火し、fitFieldToViewport を再実行させる。
 * 変数が未定義（PC・横向き等）の場合は 52px にフォールバックする。
 * @returns {number}
 */
function getMobileBottomReserve() {
  const raw = getComputedStyle(document.documentElement).getPropertyValue('--sol-mobile-bottom-reserve');
  const v = parseFloat(raw);
  return Number.isFinite(v) ? v : 52;
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
  // URL にリプレイ指定（?replay= / #replay= / ?saved=）があるかは、ここで一度だけ評価する。
  // initReplayUI() 内の読み込み処理は URL を書き換える（?saved= を外す）ため、それより後で
  // location を見ると判定がぶれ、前回盤面の復元が開いたリプレイを上書きしてしまう（順序依存の根絶）。
  const hasReplayInUrl = isReplayInUrl();
  // スマホ縦で開いたリプレイは読み込み後に再生モードへ入る。盤面を後から縮め直さないよう、再生バーの分を先に取る
  // （読み込みを始める initReplayUI と、最初の盤面合わせ fitFieldToViewport より前に呼ぶ）
  if (hasReplayInUrl && !isReplayAtEnd()) reservePlaybackSpaceForUrlReplay();
  // 起動時の2択（盤面が空のときだけ出す）。リプレイを開くときは出さない
  initStartChoice({ hasReplayInUrl });

  // 端末間同期（P2）: 一人回しページを開いたときも pull する（設計文書 §11）。
  // 保存デッキは savedDecksSet/renderSavedDecks 経由で適用されデッキ一覧に反映される。
  // 購入候補は deck-wish-ui.js が置く wishSave で適用される（2026-10-20。以前はこのページに適用先が無く、
  // rev を据え置いていた）
  if (window.SyncClient) window.SyncClient.pullIfNeeded();

  // デスクトップドラッグ&ドロップを初期化
  initializeDesktopDragDrop();

  // ダミー画像なし（デッキ読込後にカードが追加される）
  initializeCards([]);

  // カウンターを初期化
  document.querySelectorAll('.counter-container').forEach(container => {
    initializeCounter(container);
  });

  // イベントリスナーを設定
  initializeEventListeners();

  // 画面録画用の表示。共有リンクの題名（sol-replay-loaded-from-url）を受け取るため、読み込みを始める initReplayUI より先に用意する
  initRecView({ enterMobilePlayback: enterPlaybackModeForRecView });

  // このデッキのリプレイ（左サイドバー・スマホのデッキシート・開いたリプレイの差の帯）。
  // 保存したリプレイを開いた知らせ（sol-replay-loaded-from-url）を受けるため、読み込みを始める initReplayUI より先に用意する
  initDeckReplaysUI();

  // 開いているリプレイの名前を変える（再生バーの名前のボタン）。sol-replay-loaded-from-url を受けるため initReplayUI より先に用意する
  initReplayRenameUI();

  // リプレイUI初期化（window.replayLog バインド含む）
  initReplayUI();

  // トークン生成（空いたマスの右クリックメニュー）初期化
  initTokenGenerator();

  // コイントス・ダイスロール初期化
  initRandomTools();

  // カード詳細パネル初期化
  initCardInfoPanel();

  // 未発売SAMPLE画像のぼかし設定（localStorage復元・トグル配線）
  initSampleBlur();

  // カード詳細パネルの上端を盤面に合わせる（旧 initOpponentTray の初期化時の処理を引き継ぐ）
  syncCipPosition();

  // 左サイドバー・右パネルの開閉を初期化
  initSidebarToggle();
  // 右パネルの吹き出し（リセット・クリア）をパネルの左に出す
  initSideTips();

  // スマホ縦向き専用UI（下部バー・アクションシート・詳細パネル・簡易メニュー等）を初期化。
  // isPhone && !isLandscape 以外では内部で何もしない（フェーズ1・設計書 §8）
  initMobileUI();

  // デッキ編集（カードを追加・右クリックの足す／抜く・マイデッキへの保存）
  initDeckEditUI();

  // フィードバック（不具合・要望）モーダルを初期化
  initFeedbackModal();

  // ゾーン枚数カウント初期化
  initZoneCounts();

  // カード追加時の画像登録
  initCardImageRegistration();

  // 盤面の大きさを先に画面へ合わせてから見せる（それまでは .sol-field-area.sol-fitting で非表示）。
  // 下の引き継ぎ・新着読込・復元は通信を待つため、その前に行う（待っている間も盤面と読込中表示が見える）。
  // 盤面の大きさは画面サイズで決まりデッキの枚数には依存しない。読込後に下で再フィットする
  fitFieldToViewport();
  requestAnimationFrame(() => requestAnimationFrame(() => {
    document.querySelector('.sol-field-area')?.classList.remove('sol-fitting');
  }));

  // ページ遷移後の盤面復元（card:// センチネル対応のため await が必要）
  // URL に共有リプレイ（?replay=ID / #replay=）または端末保存（?saved=ID）が付いているときは前回の盤面を復元しない。
  // 復元が共有データを上書きし、受け手側で再生モードが開かない競合があった（2026-09-04 実測）。
  // hasReplayInUrl は initializeApp の先頭で評価済み（理由はそこのコメント）
  const handedOff = await receiveDeckHandoff({
    loadDeck: loadDeckFromNeuron, clearDeck: clearEverything,
    getCounts: () => ({
      main: document.querySelectorAll('#poolRow .tier-item').length,
      ex: document.querySelectorAll('#poolRow2 .tier-item').length,
    }),
    // トップのマイデッキから開いたデッキ。保存済みのデッキなら、そのデッキにひも付ける
    onLoaded: ({ name, savedId }) => setDeckLink({ savedId, name }),
  });
  // ?newcards=1（X投稿などからの直行）。リプレイ指定・持込があれば何もしない（判定は receiveNewCards 内でも行う）
  const newCardsLoaded = hasReplayInUrl || handedOff ? false : await receiveNewCards({
    loadDeck: loadDeckFromNeuron, clearDeck: clearEverything,
    // 状態表示。スマホ縦の読込後の失敗は sol-deck-load-end の既存トーストが出るので重ねない
    notify: (msg, opt) => { if (!(opt?.afterLoad && isMobilePortrait())) showToast(msg); },
    // 新着を読み込めたときだけ「自分のデッキで回す」案内を出す（PC は見出し行・スマホは詳細シート）
    // 新着カードはマイデッキに無いデッキなので、未登録として始める（見出しに「マイデッキに登録」が出る）
    onLoaded: () => setDeckLink({ name: '新着カード' }),
    // スマホ縦は既存のスマホ用詳細UI、それ以外は左パネル（カードをタップ/クリックした時と同じ表示）
    showCard: el => isMobilePortrait()
      ? openCardDetailByName(el.dataset.cardName, el.src || '')
      : showCardByName(el.dataset.cardName, el.src || '', el.closest('.sample-art') ? 'official_sample' : ''),
  });
  const restored = hasReplayInUrl || handedOff || newCardsLoaded ? false : await loadSessionResume();
  // リプレイ記録の復元（盤面は loadSessionResume が写しから復元済み。ここでは記録の変数を戻すだけ）。
  // 画像辞書は保存されていた辞書を先に入れ、次の DOM からの登録で上書き・追加する（順序を変えない）
  const replaySnap = restored && restored.replay ? restored.replay : null;
  // マイデッキとのひも付け・保存していない増減を戻す（退避に無ければ、見出しは未登録のデッキとして出る）
  if (restored && restored.deckLink) restoreLinkState(restored.deckLink);
  if (replaySnap) {
    restoreReplaySession(replaySnap);
    // 記録に出てくるカードID（DOM に無いものも含む）を使わないよう、ID カウンターを進める
    syncCardIdCounter(collectReplayCardIds(replaySnap));
    setPcRecordingState(replaySnap.pcRecording);
    setMobileRecordingState(replaySnap.mobileRecording);
  }
  if (restored) {
    // 「元に戻す」で開き直した直後か（復元のお知らせを「元に戻しました」にする。印は一度だけ読む。残った印は consumeStartupOffers が捨てる）
    const undoRestored = takeUndoRestored();
    // 復元したカードをリプレイ画像辞書に登録
    // img.tier-item（発売済み）または div.tier-item（プロキシ）どちらも .tier-item で取れる
    document.querySelectorAll('.tier-item').forEach(cardEl => {
      const src = registrySrcOf(cardEl);
      if (cardEl.id && src) {
        registerCardImage(cardEl.id, src);
        // 名前も復元する（無いと共有リプレイの names が欠け、受け手で「不明なカード」・リンク画像で「？」になる）
        registerCardName(cardEl.id, cardEl.dataset.cardName);
        const wrapper = cardEl.closest('.tier-item-wrapper');
        registerCardType(cardEl.id, wrapper?.dataset.cardType); // 記録の復元で上書きされた種類辞書をDOMから補う
        if (wrapper?.id.includes(' ex')) registerCardIsEx(cardEl.id);
      }
    });
    // 記録を容量超過で落としていた場合は、2つのトーストが重ならないよう1つにまとめる
    // 記録を落としていたら、元に戻した場合もそれを優先して伝える（記録までは戻っていないため）
    _showToast(restored.replayDropped
      ? '前回の盤面を復元しました（記録は大きすぎて引き継げませんでした）'
      : undoRestored ? '元に戻しました'
      : replaySnap ? '前回の盤面と記録を復元しました' : '前回の盤面を復元しました');
    // スマホで再生モード中に離れていたなら、同じ手を表示したまま再生モードへ入り直す
    // 自動再生・演出の途中で離れた退避は盤面が中間状態の可能性があるので、カーソル位置で作り直す
    if (replaySnap?.needsRebuild) {
      rebuildAtCursor();
    }
    if (replaySnap) resumePlaybackModeIfNeeded(!!replaySnap.mobilePlayback);
  }
  // 復元の有無に関わらず、盤面確定後に起動時の2択（start-choice.js）を出すかを確定させる
  // （restored=false でも判定は要るため if の外に出す）。
  document.dispatchEvent(new CustomEvent('sol-session-restored'));

  // 廃止した盤面セーブ（スロット1〜5）を、0手のリプレイとしてリプレイ一覧へ移す（失敗したスロットは残して次回再試行）。
  // 復元トーストと重ならないよう、復元の後に知らせる。リプレイ一覧ページ側でも同じ移行を呼ぶ（何度呼んでも同じ結果）
  const slotMigration = migrateSlotsToReplays();
  if (slotMigration.migrated > 0) {
    _showToast(`盤面セーブ（${slotMigration.migrated}件）をリプレイ一覧に移しました`);
  }
  // 移せなかったスロットは消さずに残してある。利用者が気づけるよう、読み込みのたびに知らせる
  if (slotMigration.failed > 0) {
    _showToast(`移せなかった盤面セーブが${slotMigration.failed}件あります（リプレイ一覧で確認できます）`);
  }
  // 前のページから引き継いだお知らせと「元に戻す」（リプレイ一覧から開いた・記録を置き換えて開いた等。ui/undo-point.js）
  consumeStartupOffers();

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

  // 左右パネルの開閉など、盤面の入れ物の大きさが変わったら再計算（sidebar-toggle.js が発火）
  window.addEventListener('sol-layout-resize', fitFieldToViewport);

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

/** ページ遷移直前に盤面をsessionStorageへ退避（中身は ui/undo-point.js captureSessionToResume。「元に戻す」の写しと共通） */
window.addEventListener('pagehide', () => {
  // 「元に戻す」で開き直すときは、写しを戻した退避を今の盤面（消した後・入れ替えた後）で上書きしない
  if (window.__solSkipResumeSave) return;
  captureSessionToResume();
});

/**
 * 短時間表示のトースト通知。実体は utils/toast.js（mobile-ui.js からもそちらを import する。
 * main.js は他モジュールから import されないエントリ専用のため、ここには置かない）。
 * 既存の内部呼び出し（_showToast(...)）はそのまま残し、実体だけ showToast へ委譲する。
 */
function _showToast(msg) { showToast(msg); }

window.addEventListener('DOMContentLoaded', initializeApp);
