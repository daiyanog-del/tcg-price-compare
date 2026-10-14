// 「元画像に戻す」「確定」の送信経路: 処理中の一括無効化・連打防止・古い応答の扱いを検証する。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../static/admin/admin.js'), 'utf8');
const modalCode = source.slice(source.indexOf('const _cropModal = {'), source.indexOf('_cropModal.init();'));
const shown = [];
const context = vm.createContext({
  Number, URL, JSON,
  document: {body: {style: {}}},
  _validatedCropSuggestion: () => null,
  _showResult: (el, msg, kind) => shown.push([msg, kind]),
  apiFetch: null,
});
vm.runInContext(modalCode + '\nglobalThis.modal = _cropModal;', context);
const modal = context.modal;

function fakeForm() {
  const img = {src: 'old', };
  const link = {href: 'old'};
  const note = {hidden: false};
  return {img, link, note, querySelector(sel) {
    return sel === '.edit-source-image__img' ? img : sel === '.edit-source-image__link' ? link : sel === '.edit-source-image__auto-note' ? note : null;
  }};
}
function setup() {
  Object.assign(modal, {
    img: {}, el: {hidden: true, querySelector: () => ({})}, selEl: {style: {}},
    confirmBtn: {disabled: true, textContent: '確定'}, restoreBtn: {disabled: false, textContent: '元画像に戻す'},
    suggestBtn: {disabled: false, textContent: '自動で枠を提案'}, resultEl: {},
    _busy: false, _suggesting: false, _dragging: false,
  });
  shown.length = 0;
}
const box = {left: .1, top: .1, right: .6, bottom: .9, source_image_url: 'x'};

(async () => {
  // 1) restore: 処理中は3ボタンが無効、成功で画像差し替え・注記消去・モーダルを閉じる
  setup();
  let form = fakeForm();
  let respond; const urls = [];
  context.apiFetch = (url) => { urls.push(url); return new Promise(r => { respond = r; }); };
  modal.open(5, form, 'x');
  const p = modal._submit('restore');
  assert.equal(modal.confirmBtn.disabled, true);
  assert.equal(modal.restoreBtn.disabled, true);
  assert.equal(modal.suggestBtn.disabled, true);
  assert.equal(modal.restoreBtn.textContent, '処理中...');
  await modal._submit('restore');   // 連打は無視される
  assert.equal(urls.length, 1);
  assert.equal(urls[0], '/api/admin/unreleased/5/restore-original-image');
  respond({ok: true, json: async () => ({ok: true, new_url: 'new?t=1'})});
  await p;
  assert.equal(form.img.src, 'new?t=1');
  assert.equal(form.link.href, 'new?t=1');
  assert.equal(form.note.hidden, true);
  assert.equal(modal.el.hidden, true);
  assert.equal(modal.restoreBtn.textContent, '元画像に戻す');
  assert.equal(modal.restoreBtn.disabled, false);

  // 2) restore 失敗: モーダルは開いたまま、エラー表示
  setup();
  form = fakeForm();
  modal.open(5, form, 'x');
  const p2 = modal._submit('restore');
  respond({ok: false, json: async () => ({ok: false, error: '失敗です'})});
  await p2;
  assert.equal(modal.el.hidden, false);
  assert.deepEqual(shown, [['失敗です', 'error']]);
  assert.equal(form.img.src, 'old');

  // 3) 応答待ちの間に別カードを開いた場合: 画像は送信時のフォームへ反映、新しいモーダルは閉じない・エラーも出さない
  setup();
  const formA = fakeForm(), formB = fakeForm();
  modal.open(5, formA, 'x');
  const p3 = modal._submit('restore');
  modal.close();
  modal.open(6, formB, 'y');
  respond({ok: true, json: async () => ({ok: true, new_url: 'A-new'})});
  await p3;
  assert.equal(formA.img.src, 'A-new');
  assert.equal(formB.img.src, 'old');
  assert.equal(modal.el.hidden, false);
  assert.equal(modal._cardId, 6);

  // 3b) 同様に失敗応答でも新しいモーダルへエラーを出さない
  setup();
  modal.open(5, fakeForm(), 'x');
  const p3b = modal._submit('restore');
  modal.close();
  modal.open(6, fakeForm(), 'y');
  respond({ok: false, json: async () => ({ok: false, error: '古い失敗'})});
  await p3b;
  assert.deepEqual(shown, []);

  // 4) crop 確定: 選択がある時だけ送信し、処理中は3ボタン無効
  setup();
  form = fakeForm();
  urls.length = 0;
  modal.open(7, form, 'x');
  await modal._submit('crop');      // 選択なし → 送信しない
  assert.equal(urls.length, 0);
  modal._sel = {...box};
  modal._refreshButtons();
  assert.equal(modal.confirmBtn.disabled, false);
  const p4 = modal._submit('crop');
  assert.equal(modal.confirmBtn.disabled, true);
  assert.equal(modal.restoreBtn.disabled, true);
  assert.equal(modal.suggestBtn.disabled, true);
  respond({ok: true, json: async () => ({ok: true, new_url: 'c?t=1'})});
  await p4;
  assert.equal(urls[0], '/api/admin/unreleased/7/crop-image');
  assert.equal(form.img.src, 'c?t=1');
  assert.equal(modal.el.hidden, true);

  // 5) 処理中は _suggest が走らない
  setup();
  modal.open(8, fakeForm(), 'x');
  const p5 = modal._submit('restore');
  urls.length = 0;
  await modal._suggest();
  assert.equal(urls.length, 0);
  respond({ok: true, json: async () => ({ok: true, new_url: 'z'})});
  await p5;
  console.log('admin_crop_restore_check: 元に戻す・確定の競合制御の検証成功');
})().catch(error => { console.error(error); process.exitCode = 1; });
