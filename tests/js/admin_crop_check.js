// 候補の出所一致・手動への復帰・明示確認まで保存しないことを検証する。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../static/admin/admin.js'), 'utf8');
const modalCode = source.slice(source.indexOf('const _cropModal = {'), source.indexOf('_cropModal.init();'));
const helpers = source.slice(source.indexOf('function _validatedCropSuggestion('), source.indexOf('/**', source.indexOf('function _validatedCropSuggestion(')));
let saves = 0;
const context = vm.createContext({Number, URL, document: {body: {style: {}}, createElement: tag => ({tag})}, apiFetch: () => { saves++; }});
vm.runInContext(modalCode + helpers + '\nglobalThis.modal = _cropModal; globalThis.validate = _validatedCropSuggestion; globalThis.showEvidence = _showRecognitionEvidence;', context);
const candidate = {left: .1, top: .1, right: .9, bottom: .9, source_image_url: 'https://example.test/a.png'};
assert.equal(context.validate(candidate, 'https://example.test/b.png'), null);
for (const left of [NaN, Infinity, true, '0.1', -1]) {
  assert.equal(context.validate({...candidate, left}, candidate.source_image_url), null);
}
assert.equal(context.validate(null, candidate.source_image_url), null);
const hint = {};
Object.assign(context.modal, {img: {}, el: {querySelector: () => hint}, selEl: {style: {}}, confirmBtn: {}, suggestBtn: {}, restoreBtn: {}, resultEl: {}});
context.modal.open(1, {}, candidate.source_image_url, candidate);
assert.equal(context.modal._sel.left, .1);
assert.equal(context.modal.selEl.hidden, false);
assert.equal(context.modal.confirmBtn.disabled, false);
assert.match(hint.textContent, /候補枠/);
assert.equal(saves, 0);
context.modal.close();
assert.equal(saves, 0);
context.modal.open(2, {}, candidate.source_image_url);
assert.equal(context.modal._sel, null);
assert.equal(context.modal.selEl.hidden, true);
assert.equal(context.modal.confirmBtn.disabled, true);
context.modal.overlay = {getBoundingClientRect: () => ({left: 0, top: 0, width: 100, height: 100})};
context.modal._onDown({clientX: 10, clientY: 10, preventDefault() {}});
context.modal._onMove({clientX: 80, clientY: 90});
context.modal._onUp();
assert.equal(context.modal._sel.right, .8);
assert.equal(context.modal.confirmBtn.disabled, false);
assert.equal(saves, 0);
// 根拠表示用の最小DOMスタブ（子要素・textContent を再帰的に保持する）
const textOf = node => (node.textContent || '') + (node.children || []).map(textOf).join('');
// 偽DOM要素
function fakeEl(tag) {
  const el = {tag, children: [], className: '', appendChild(child) { this.children.push(child); return child; }};
  return el;
}
context.document.createElement = fakeEl;
context.document.createTextNode = text => ({textContent: text});
const panel = {children: [], replaceChildren() { this.children = []; }, appendChild(child) {this.children.push(child);}, get childElementCount() {return this.children.length;}};
// 全セレクタで panel を返す（panel.value は undefined のため current(key) は card[key] にフォールバックする）
const form = {querySelector: () => panel};
const findLink = node => node.tag === 'a' ? node : (node.children || []).map(findLink).find(Boolean);
const productUrl = 'https://www.yugioh-card.com/japan/products/test/';
const verifiedCard = {
  product_name: '商品A', release_date: '2026-10-31', card_type: 'モンスター',
  level: 4, rank: null, link_val: null,
  release_date_evidence: {status: 'verified', product_name: '商品A', release_date: '2026-10-31', source_url: productUrl},
  recognition_review: {status: 'verified', card_type: 'モンスター', values: {level: 4, rank: null, link_val: null}},
};

// 照合済み: 発売日・数値再読が1行に集約され、公式リンクがその行に入る
context.showEvidence(verifiedCard, form);
assert.equal(panel.children.length, 1);
assert.match(textOf(panel.children[0]), /照合済み：発売日（公式商品ページと一致）・レベル・ランク・リンク値（画像から再確認）/);
const okLink = findLink(panel.children[0]);
assert.equal(okLink.href, productUrl);
assert.equal(okLink.rel, 'noopener noreferrer');
assert.notEqual(panel.hidden, true);

// 要確認: reason 原文（HTML風文字列）は出さず固定の言い換え文のみ。textContent 描画なので HTML として解釈されない
for (const [cardType, expected] of [
  ['モンスター', /レベル・ランク・リンク値を画像から確定できませんでした/],
  ['魔法', /カード種別を画像から確定できませんでした/],
]) {
  context.showEvidence({
    card_type: cardType,
    recognition_review: {status: 'needs_review', reason: '<img src=x>', card_type: cardType, values: {}},
  }, form);
  assert.equal(panel.children.length, 1);
  assert.match(textOf(panel.children[0]), expected);
  assert.doesNotMatch(textOf(panel.children[0]), /<img src=x>/);
  assert.equal(panel.children[0].children.length, 0);
}
// 要確認: 発売日（公式リンクは同じ行に入る）
context.showEvidence({product_name: 'X', release_date: '', release_date_evidence: {status: 'needs_review', product_name: 'X', release_date: '', source_url: productUrl}}, form);
assert.match(textOf(panel.children[0]), /発売日を公式サイトで確認できませんでした/);
assert.equal(findLink(panel.children[0]).href, productUrl);

// 手動変更: date に product_name が無い・recognition に values が無いと手動変更扱い
context.showEvidence({release_date_evidence: {status: 'verified', source_url: productUrl}, recognition_review: {status: 'verified', reason: '<img src=x>'}}, form);
assert.equal(panel.children.length, 2);
assert.match(textOf(panel.children[0]), /発売日・収録商品は手動で変更されています/);
assert.match(textOf(panel.children[1]), /カード種別・レベル等は手動で変更されています/);

// 不正ドメインのリンクは作られない（発売日の要確認行のみ・リンク無し）
context.showEvidence({product_name: '', release_date: '', release_date_evidence: {status: 'needs_review', product_name: '', release_date: '', source_url: 'https://www.yugioh-card.com.evil.test/japan/products/test/'}}, form);
assert.equal(panel.children.length, 1);
assert.equal(findLink(panel.children[0]), undefined);
context.showEvidence({}, form);
assert.equal(panel.hidden, true);
console.log('admin_crop_check: 候補・手動操作・根拠表示の検証成功');

// 手動変更の判定（現在値が抽出時と違えば手動変更）
const snapshotCard = {product_name: '商品A', release_date: '2026-10-31', release_date_evidence: {status: 'verified', product_name: '商品A', release_date: '2026-10-31'}};
context.showEvidence(snapshotCard, form);
assert.doesNotMatch(textOf(panel.children[0]), /手動で変更されています/);
assert.match(textOf(panel.children[0]), /照合済み/);
context.showEvidence({...snapshotCard, release_date: '2025-10-31'}, form);
assert.match(textOf(panel.children[0]), /手動で変更されています/);
context.showEvidence(snapshotCard, {querySelector: selector => selector === '.recognition-evidence' ? panel : selector === '[name="release_date"]' ? {value: '2027-10-31'} : null});
assert.match(textOf(panel.children[0]), /手動で変更されています/);

(async () => {
  let respond;
  let requests = 0;
  context.apiFetch = () => {requests++; return new Promise(resolve => {respond = resolve;});};
  const response = {ok: true, json: async () => ({ok: true, crop_suggestion: candidate})};
  context.modal.open(3, {}, candidate.source_image_url);
  const pending = context.modal._suggest();
  await context.modal._suggest();
  assert.equal(requests, 1);
  respond(response);
  await pending;
  assert.equal(context.modal._sel.left, .1);
  assert.equal(saves, 0);
  const stale = context.modal._suggest();
  context.modal.close();
  context.modal.open(4, {}, candidate.source_image_url);
  respond(response);
  await stale;
  assert.equal(context.modal._sel, null);
  const manuallyChanged = context.modal._suggest();
  context.modal._onDown({clientX: 20, clientY: 20, preventDefault() {}});
  context.modal._onMove({clientX: 70, clientY: 70});
  context.modal._onUp();
  respond(response);
  await manuallyChanged;
  assert.equal(context.modal._sel.left, .2);
  assert.equal(saves, 0);
  console.log('admin_crop_check: 再生成・連打防止・遅延応答・手動変更表示の検証成功');
})().catch(error => {console.error(error); process.exitCode = 1;});
