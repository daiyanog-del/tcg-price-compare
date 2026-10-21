// セッション復元（盤面＋リプレイ記録）の単体チェック。
// replay-service は import 行をスタブに差し替えて data:URL で読み込み、store は実ファイルをそのまま読む。
'use strict';
const fs = require('fs'), path = require('path'), assert = require('assert/strict');
const root = path.join(__dirname, '../..');
const js = path.join(root, 'static/solitaire/js');
const b64 = (src) => 'data:text/javascript;base64,' + Buffer.from(src).toString('base64');

(async () => {
  // ── replay-service: 往復で盤面の再構築が呼ばれないこと ──
  globalThis.__rebuild = [];
  const spy = (name) => `(...a)=>{globalThis.__rebuild.push('${name}');}`;
  const stubs = `
const returnAllCardsToDeck=${spy('returnAllCardsToDeck')};
const attachCardImageListeners=${spy('attachCardImageListeners')};
const applyCardState=${spy('applyCardState')};
const getCardState=${spy('getCardState')};
const restackCustomSlot=${spy('restackCustomSlot')};
const playActivateEffect=${spy('playActivateEffect')};
const flipMoveClone=${spy('flipMoveClone')};
const playSetFlip=${spy('playSetFlip')};
const flyBetweenRects=(c,f,t,cb)=>{globalThis.__rebuild.push('flyBetweenRects');globalThis.__flyCb=cb;};
const prefersReducedMotion=()=>true;
const createProxyCardElement=${spy('createProxyCardElement')};
const isMobilePortrait=()=>globalThis.__mobile===true;
const renderCommentView=()=>{};
const clampCommentText=(t)=>t;
const cursorAfterRemove=(c,i)=>(i<=c?c-1:c);
const isSameComment=()=>true;
const showCutin=()=>{};
const hideCutin=()=>{};
const clampCutinText=(t)=>t;
const cutinToShow=()=>null;
const playPauseMs=(ev,base,f)=>base;
`;
  let src = fs.readFileSync(path.join(js, 'services/replay-service.js'), 'utf8');
  const before = src;
  src = src.replace(/^import [\s\S]*?from '[^']+';\r?$/gm, '');
  assert.notEqual(src, before);
  assert(!/^import /m.test(src), 'import が残っている');

  // UI 要素のスタブ（カウンター表示だけ検証する）
  const els = {};
  for (const id of ['replaySlider', 'replayCounter', 'replayBack', 'replayFwd', 'replayPlay', 'replayUndo']) {
    els[id] = { textContent: '', disabled: false, value: 0, max: 0, classList: { toggle() {} } };
  }
  globalThis.document = { getElementById: (id) => els[id] || null, querySelectorAll: () => [] };

  const svc = await import(b64(src + stubs));
  svc.initReplay();
  assert.equal(svc.captureReplaySession(), null, '空の記録は退避しない');

  // setup ログ（範囲共有の初期配置）も退避・復元されることを見るため、先に setup ログだけ持つ状態にする
  svc.restoreReplaySession({ logs: [], setupLogs: [{ actionType: 'moveCard', cardId: 'item-2', zoneId: 'free-space', setup: true, seq: -1 }], cursor: -1 });
  svc.registerCardImage('item-1', 'img1'); svc.registerCardImage('item-2', 'img2');
  svc.registerCardName('item-1', '青眼の白龍'); svc.registerCardIsEx('item-2');
  for (let i = 0; i < 5; i++) svc.logEvent({ actionType: 'moveCard', cardId: 'item-1', zoneId: 'custom-slot-' + (i + 1) });
  // 途中位置（3手目）へ。盤面の再構築は呼び出し側の責務なので、ここでは変数だけ動かす
  svc.seekTo(2);
  globalThis.__rebuild.length = 0;
  const cursorBefore = svc.getCursor();
  const snap = JSON.parse(JSON.stringify(svc.captureReplaySession())); // sessionStorage 経由と同じ JSON 往復
  assert.equal(snap.cursor, cursorBefore);

  svc.initReplay(); // ページ再読込相当
  assert.equal(svc.getLogLength(), 0);
  globalThis.__rebuild.length = 0;
  svc.restoreReplaySession(snap);
  assert.deepEqual(svc.getLogs(), snap.logs);
  assert.deepEqual(svc.getSetupLogs(), snap.setupLogs);
  assert.equal(svc.getCursor(), cursorBefore, 'カーソルが動いた');
  assert.equal(svc.getLogLength(), 5);
  assert.deepEqual(svc.getImages(), { 'item-1': 'img1', 'item-2': 'img2' });
  assert.deepEqual(svc.getNames(), { 'item-1': '青眼の白龍' });
  assert.deepEqual(svc.getExCardIds(), ['item-2']);
  assert.equal(svc.isPlaying(), false);
  assert.equal(els.replayCounter.textContent, `${cursorBefore + 1}/5`);
  assert.deepEqual(globalThis.__rebuild, [], '盤面の再構築が呼ばれた: ' + globalThis.__rebuild);
  // 範囲外カーソルは丸める
  svc.restoreReplaySession({ ...snap, cursor: 99 });
  assert.equal(svc.getCursor(), 4);

  // ── session-resume-store: 容量超過フォールバック ──
  const store = await import(b64(fs.readFileSync(path.join(js, 'utils/session-resume-store.js'), 'utf8')));
  const identity = (s) => s;
  function makeStorage(failTimes) {
    const m = new Map(); let fails = failTimes;
    return {
      m,
      getItem: (k) => (m.has(k) ? m.get(k) : null),
      removeItem: (k) => m.delete(k),
      setItem(k, v) {
        if (k === store.SESSION_RESUME_KEY && fails > 0) { fails--; throw Object.assign(new Error('quota'), { name: 'QuotaExceededError' }); }
        m.set(k, v);
      },
    };
  }
  const state = { slots: { poolRow: [1] }, counters: {} };
  const replay = { logs: [1, 2], setupLogs: [], cursor: 1 };
  const warn = console.warn; let warned = 0; console.warn = () => { warned++; };

  // 通常: 記録込みで保存、フラグ無し
  let st = makeStorage(0);
  assert.deepEqual(store.storeSessionResume(st, identity, state, replay), { replayDropped: false });
  let taken = store.takeSessionResume(st, identity);
  assert.deepEqual(taken.state.replay, replay); assert.equal(taken.replayDropped, false);
  assert.equal(st.getItem(store.SESSION_RESUME_KEY), null, '取り出し後に削除される');

  // 1回目だけ失敗: 盤面のみ保存＋フラグ
  st = makeStorage(1);
  assert.deepEqual(store.storeSessionResume(st, identity, state, replay), { replayDropped: true });
  assert(warned >= 1, 'console.warn が出ていない');
  assert.equal(st.getItem(store.SESSION_REPLAY_DROPPED_KEY), '1');
  taken = store.takeSessionResume(st, identity);
  assert.equal(taken.state.replay, undefined); assert.deepEqual(taken.state.slots, state.slots);
  assert.equal(taken.replayDropped, true);
  assert.equal(st.getItem(store.SESSION_REPLAY_DROPPED_KEY), null, 'フラグは消費される');
  assert.equal(store.takeSessionResume(st, identity), null);

  // 記録なし: replay フィールド省略
  st = makeStorage(0);
  store.storeSessionResume(st, identity, state, null);
  assert.equal(JSON.parse(st.getItem(store.SESSION_RESUME_KEY)).replay, undefined);

  // 盤面だけでも失敗なら例外（呼び出し側で warn）
  st = makeStorage(2);
  assert.throws(() => store.storeSessionResume(st, identity, state, replay));
  console.warn = warn;


  // ── rebuildAtCursor: 演出なしでカーソル位置を作り直す。通常の復元（restoreReplaySession）では呼ばれない ──
  svc.initReplay();
  for (let i = 0; i < 5; i++) svc.logEvent({ actionType: 'moveCard', cardId: 'item-1', zoneId: 'custom-slot-' + (i + 1) });
  svc.seekTo(2);
  globalThis.__rebuild.length = 0;
  svc.restoreReplaySession(JSON.parse(JSON.stringify(svc.captureReplaySession())));
  assert(!globalThis.__rebuild.includes('returnAllCardsToDeck'), '通常経路で盤面の作り直しが呼ばれた');
  globalThis.__rebuild.length = 0;
  svc.rebuildAtCursor();
  assert(globalThis.__rebuild.includes('returnAllCardsToDeck'), 'rebuildAtCursor が盤面を作り直していない');
  assert(!globalThis.__rebuild.includes('flipMoveClone') && !globalThis.__rebuild.includes('flyBetweenRects'), '演出が呼ばれた');
  assert.equal(svc.getCursor(), 2, 'rebuildAtCursor でカーソルが動いた');
  assert.equal(svc.isPlaying(), false);

  // main.js: needsRebuild のときだけ rebuildAtCursor を呼ぶ／pagehide で再生中・演出中に needsRebuild を付ける
  const mainSrc = fs.readFileSync(path.join(js, 'main.js'), 'utf8');
  assert(/if \(replaySnap\?\.needsRebuild\) \{\s*rebuildAtCursor\(\);/.test(mainSrc), 'needsRebuild ガード付きの rebuildAtCursor 呼び出しが無い');
  assert.equal((mainSrc.match(/rebuildAtCursor\(\)/g) || []).length, 1, 'rebuildAtCursor の呼び出しが複数ある');
  // 範囲を選ぶパネル（pc-recording.js）を開いたまま離れたときも作り直す（2026-10-20）
  // 退避の中身は ui/undo-point.js captureSessionToResume（「元に戻す」の写しと共通。2026-10-21）。main.js の pagehide はそれを呼ぶ
  const undoSrc = fs.readFileSync(path.join(js, 'ui', 'undo-point.js'), 'utf8');
  assert(/if \(isPlaying\(\) \|\| isReplayAnimating\(\)( \|\| wasPicking)?\) replay\.needsRebuild = true;/.test(undoSrc));
  assert(/if \(wasPicking\) closePcRangePicker\(\);/.test(undoSrc), 'パネルを開いたまま離れたときに閉じていない');
  assert(/addEventListener\('pagehide', \(\) => \{[^]*?if \(window\.__solSkipResumeSave\) return;\s*captureSessionToResume\(\);/.test(mainSrc), 'pagehide が退避を呼んでいない（元に戻すときは書かない）');
  assert(/syncCardIdCounter\(collectReplayCardIds\(replaySnap\)\)/.test(mainSrc));

  // ── 純粋関数: visibility 除去・カードID収集・フラグ消去 ──
  assert.equal(store.stripVisibility('left: 10px; visibility: hidden; z-index: 3'), 'left: 10px; z-index: 3');
  assert.equal(store.stripVisibility('VISIBILITY:hidden;'), '');
  assert.equal(store.stripVisibility(null), '');
  assert.equal(store.stripVisibility('transform: rotate(90deg);'), 'transform: rotate(90deg)');
  assert.deepEqual(
    store.collectReplayCardIds({
      images: { 'item-007': 'x' },
      logs: [{ cardId: 'item-012' }, { actionType: 'resetDeck', drawnIds: ['item-020', 'item-021'] }],
      setupLogs: [{ cardId: 'item-030' }],
    }).sort(),
    ['item-007', 'item-012', 'item-020', 'item-021', 'item-030']);
  assert.deepEqual(store.collectReplayCardIds(null), []);
  st = makeStorage(0);
  st.setItem(store.SESSION_RESUME_KEY, 'a'); st.setItem(store.SESSION_REPLAY_DROPPED_KEY, '1');
  store.clearSessionResume(st);
  assert.equal(st.getItem(store.SESSION_RESUME_KEY), null); assert.equal(st.getItem(store.SESSION_REPLAY_DROPPED_KEY), null);

  // ── save-load-service: getCardsFromSlot が stripVisibility を使い、side-slot・記録だけでも保存対象 ──
  const slSrc = fs.readFileSync(path.join(js, 'services/save-load-service.js'), 'utf8');
  assert(/style:\s*stripVisibility\(wrapper\.getAttribute\('style'\)\)/.test(slSrc));
  assert(/side-slot-/.test(slSrc.split('export function saveSessionResume')[1]) && /!!replay/.test(slSrc));

  // ── card-manager: 退避したIDまでカウンターが進む（DOM に無いIDでも） ──
  let cm = fs.readFileSync(path.join(js, 'components/card-manager.js'), 'utf8');
  cm = cm.replace(/^import [\s\S]*?from '[^']+';\r?$/gm, '') + '\nexport const __count = () => itemCount;';
  const cmod = await import(b64(cm));
  assert.equal(cmod.__count(), 0);
  cmod.syncCardIdCounter(store.collectReplayCardIds({ images: { 'item-041': 'x' }, logs: [{ cardId: 'item-017' }] }));
  assert.equal(cmod.__count(), 42, 'DOM に無い退避IDまでカウンターが進んでいない');

  // ── isReplayAnimating: 開始で true・完了で false・作り直しで 0 に戻る ──
  const poolEl = { appendChild() {} };
  const cardEl = { style: {}, parentElement: {}, querySelector: () => null, getBoundingClientRect: () => ({ width: 10, left: 0, top: 0, height: 10 }) };
  cardEl.closest = () => cardEl;
  els['item-9'] = cardEl; els['poolRow'] = poolEl; els['solMobileDeckBtn'] = { getBoundingClientRect: () => ({ width: 10 }), classList: { add() {}, remove() {} } };
  svc.initReplay();
  assert.equal(svc.isReplayAnimating(), false);
  globalThis.__mobile = true;
  svc.logEvent({ actionType: 'returnToDeck', cardId: 'item-9', isEx: false });
  svc.logEvent({ actionType: 'returnToDeck', cardId: 'item-9', isEx: false });
  svc.seekTo(-1); // logEvent はカーソルを末尾へ進めるので先頭へ戻す
  svc.stepForward();
  assert.equal(svc.isReplayAnimating(), true, '開始で true にならない');
  globalThis.__flyCb();
  assert.equal(svc.isReplayAnimating(), false, '完了で false にならない');
  globalThis.__flyCb(); // 二重呼び出しでも負にならない
  assert.equal(svc.isReplayAnimating(), false);
  svc.stepForward();
  assert.equal(svc.isReplayAnimating(), true);
  const staleCb = globalThis.__flyCb;
  svc.rebuildAtCursor();
  assert.equal(svc.isReplayAnimating(), false, '作り直しで 0 に戻らない');
  svc.seekTo(0); svc.stepForward();
  assert.equal(svc.isReplayAnimating(), true);
  staleCb(); // 古い世代の完了は新しい演出の数に影響しない
  assert.equal(svc.isReplayAnimating(), true, '古い完了コールバックが数を狂わせた');
  svc.seekTo(-1);
  assert.equal(svc.isReplayAnimating(), false);
  globalThis.__mobile = false;
  const svcSrc = fs.readFileSync(path.join(js, 'services/replay-service.js'), 'utf8');
  // コメントのトーストは廃止（吹き出し・盤面メモの表示に置き換え。_updateUI が記録の位置から描く）
  assert(!/_maybeShowCommentToast|replayCommentToast/.test(svcSrc), 'コメントのトーストが残っている');
  assert(/renderCommentView\(_setupLogs, _logs, cur\)/.test(svcSrc), '_updateUI がコメント表示を描いていない');

  // restoreCardsToSlot が style 適用前に visibility を除く／mobile-ui の末尾送りトースト／0手の確認文
  assert(/setAttribute\('style', stripVisibility\(cardData\.style\)\)/.test(slSrc));
  const muSrc = fs.readFileSync(path.join(js, 'ui/mobile-ui.js'), 'utf8');
  assert(/seekTo\(getLogLength\(\) - 1\);[^\n]*\n\s*showToast\('最後の手まで進めました'\)/.test(muSrc));
  assert(/r\.steps > 0 \?/.test(fs.readFileSync(path.join(js, 'replays-page.js'), 'utf8')));

  console.log('セッション復元: 記録の往復（カーソル・辞書・EX・盤面再構築なし）と容量超過フォールバックを確認');
})().catch((e) => { console.error(e); process.exitCode = 1; });
