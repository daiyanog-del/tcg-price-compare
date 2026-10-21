import {
  returnAllCardsToDeck,
  drawRandomCards,
} from '../components/card-manager.js';
import { registerCardImage, clearReplayLog } from '../services/replay-service.js';
import { initDeckInputPanel, loadDeckFromNeuron, savedDecksGet, savedDecksSet, parseDeckList } from './deck-input-panel.js';
import { parseNeuronPdf } from '/static/shared/neuron-pdf-parser.js';
import { NeuronPreviewModal } from '/static/shared/neuron-preview-modal.js';
import { setDeckLink } from '../services/deck-link.js';
import { showActionToast } from './inline-ui.js';

/**
 * UIイベントハンドラ（カード相場向け改変版）
 * ベース: Solo Mode (Fugarta, MIT) — 絵文字除去・リプレイフック・デッキ入力追加
 */

/**
 * ニューロンPDFファイル選択ハンドラ
 */
export async function handleNeuronPdfSelect(event) {
  const file = event.target.files[0];
  event.target.value = ''; // 同じファイルを再選択できるようリセット
  if (!file) return;

  let parsed;
  const defaultName = file.name.replace(/\.pdf$/i, '').replace(/[_\-]/g, ' ').trim();

  try {
    parsed = await parseNeuronPdf(file, { includeSide: false });
  } catch (e) {
    parsed = { main: [], ex: [], side: [], warnings: [e.message], ok: false };
  }

  new NeuronPreviewModal({
    parsed,
    defaultName,
    onSave: ({ name, mainText, exText }) => {
      // [EX]区切りを挿入（saveImportedDeck と同じ形式）
      const combined = exText ? mainText + '\n[EX]\n' + exText : mainText;
      // main/ex 配列も保存する（マイデッキは配列を優先して使うため text と同期させる）
      const main = parseDeckList(mainText || '');
      const ex = parseDeckList(exText || '');
      try {
        const list = savedDecksGet();
        const existing = list.find(d => d.name === name);
        let prev = null;
        if (existing) {
          // 以前は確認窓を出していたが、今は上書きして6秒間「元に戻す」を出す（マイデッキの削除・シミュレーターの保存と同じ）
          prev = { id: existing.id, text: existing.text, main: existing.main, ex: existing.ex };
          existing.text = combined;
          existing.main = main;
          existing.ex = ex;
          existing.updated = Date.now();
        } else {
          list.push({ id: 'd_' + Date.now(), name, text: combined, main, ex, updated: Date.now() });
        }
        savedDecksSet(list);
        if (prev) {
          const written = existing.updated;
          showActionToast(`マイデッキの「${name}」を上書きしました`, {
            label: '元に戻す',
            onClick: () => {
              const cur = savedDecksGet();
              const d = cur.find(x => x.id === prev.id);
              // 元に戻すまでの間に別の場所で変えられていたら戻さない（その変更を消さないため）
              if (!d || d.updated !== written) { showActionToast('その後に変更されているため、元に戻せませんでした'); return; }
              // 中身は上書き前に戻し、更新時刻は新しくする（古い時刻に戻すと端末間同期で上書き後が勝つ）
              Object.assign(d, { text: prev.text, main: prev.main, ex: prev.ex, updated: Date.now() });
              savedDecksSet(cur);
              showActionToast(`「${name}」を元に戻しました`);
            },
          });
        }
      } catch (e) {
        console.error('マイデッキ保存エラー:', e);
      }
    },
    onLoad: async ({ mainText, exText, name }) => {
      // L-8: メイン→EXの逐次読込を1組の sol-deck-load-start/end にまとめる
      // （個別に呼ぶとメイン完了時点で中間の「EX 0」トーストが出てしまうため）。
      // メインまで入れ替えたときは、取り込んだデッキとして未登録で始める（EXだけ足したときはひも付けを変えない）
      try {
        if (await loadDeckFromNeuron(mainText, exText) && mainText) setDeckLink({ name: name || defaultName || 'ニューロン取込' });
      } catch (e) {
        // カードを取得できず片付ける前で止まったときなど（盤面と記録はそのまま）。
        // 記録があれば読み込み終わりの知らせ（deck-input-panel.js）が出すので重ねない（e.notified）
        console.warn('[event-handlers] デッキ取込の読み込み失敗', e);
        if (!e?.notified) showActionToast(`デッキの読み込みに失敗しました: ${e?.message || e}`, { error: true });
      }
    },
  }).show();
}

/**
 * リセットボタンのイベントハンドラ
 * 場・手札・墓地・除外のカードをすべてデッキ（EXはEXデッキ）に戻し（カードの上のカウンターも外す）、記録（再生バー）も初期化する。
 * 5枚は引かない。確認ダイアログは出さない（2026-10-15 ユーザー指定）。
 * 読み込んだデッキ（カード・画像辞書・名前辞書・EX判定）は残す。
 * 全ログが空＝全カードがデッキの状態が初期状態になるため、setup ログ(resetDeck)は残さない。
 */
export function handleReset() {
  returnAllCardsToDeck();
  // 場を離れたカードのカウンターは無くなる（ルール上）。記録は0件になるため、残すと巻き戻しで戻せない
  document.querySelectorAll('.tier-item-wrapper > .counter-container').forEach(el => el.remove());
  clearReplayLog();
  // 録画状態（PC: replay-ui.js / スマホ: mobile-ui.js）を破棄させる。
  // 'sol-board-cleared'（クリア用）はデッキ消去を含意するため使わず、専用イベントで通知する。
  document.dispatchEvent(new CustomEvent('sol-replay-reset'));
}

/**
 * 1ドローボタンのイベントハンドラ
 */
export function handleDrawOne() {
  const selectedCards = drawRandomCards('poolRow', 1);
  const centerSlot = document.querySelector('.center-slot');
  selectedCards.forEach(card => {
    centerSlot.appendChild(card);
    // img.tier-item（発売済み）または div.tier-item（プロキシ）どちらも .tier-item で取れる
    const cardEl = card.querySelector('.tier-item');
    if (cardEl && typeof window.replayLog === 'function') {
      window.replayLog({ actionType: 'draw', cardId: cardEl.id, zoneId: 'center-slot' });
    }
  });
}

/**
 * ダブルクリックで表示/非表示を切り替え
 */
export function setupToggleVisibility(elements) {
  elements.forEach(element => {
    element.addEventListener('dblclick', () => {
      element.style.display = element.style.display === 'none' ? '' : 'none';
    });
  });
}

/**
 * すべてのイベントリスナーを設定
 */
export function initializeEventListeners() {
  document.getElementById('neuronPdfUpload')
    ?.addEventListener('change', handleNeuronPdfSelect);

  document.getElementById('resetButton')
    ?.addEventListener('click', handleReset);

  document.getElementById('randomButton')
    ?.addEventListener('click', handleDrawOne);

  // フリースペースと除外ゾーンのダブルクリック切り替え
  const sideSlotGroups = document.querySelectorAll('.side-slot-group');
  if (sideSlotGroups.length >= 3) {
    setupToggleVisibility([sideSlotGroups[0], sideSlotGroups[2]]);
  }

  // デッキ入力パネル初期化
  initDeckInputPanel();
}
