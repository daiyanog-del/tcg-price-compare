/**
 * tests/js/xss_escaping_check.js
 *
 * 2026-08-20 公開前修正 第2バッチ「escAttr() の導入と属性文脈の置換」の回帰テスト。
 *
 * 背景:
 *   esc()（テキストノード用。& < > のみ実体参照化）と escJs()（JS文字列用。\ ' < のみ
 *   処理）は、どちらも " をエスケープしないため、HTML属性値（href=/src=/id=/data-*=等）
 *   にそのまま使うと属性の外へ脱出できてしまう。escAttr()（& < > " ' を全て実体参照化）
 *   を新設し、属性文脈の esc()/escJs() 呼び出しを置換した。
 *
 *   onclick="...('${escJs(x)}')..." のようにJS文字列をHTML属性に埋める二重文脈は
 *   escAttr(escJs(x)) の重ね掛けに統一した。
 *
 * このスクリプトは複製ロジックではなく、templates/index.html の実際の <script> 中身
 * （esc/escJs/escAttr/safeUrl の定義そのものと、実際のレンダラー関数）をそのまま抽出して
 * Node の vm で実行し、脱出ペイロードに対して安全であることを直接検証する。
 *
 * 実行: node tests/js/xss_escaping_check.js
 * 終了コード0=全項目OK、非0=失敗（stderrにFAIL行）。
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const INDEX_HTML_PATH = path.join(__dirname, '..', '..', 'templates', 'index.html');
const html = fs.readFileSync(INDEX_HTML_PATH, 'utf-8');

// ── index.html の実際のインラインスクリプトを抽出する ──
// <script>...</script>（属性なし＝src読み込みでもld+jsonでもない、実行されるJS）を
// 出現順に全て連結する。ブラウザでも同じ順で1つのグローバルスコープに定義されるため、
// これで本物の実行時の関数定義（esc/escJs/escAttr/safeUrl/各レンダラー）が揃う。
const blocks = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
if (blocks.length === 0) {
  console.error('FAIL: templates/index.html から <script> ブロックを抽出できませんでした（テンプレート構造が変わった可能性）');
  process.exit(1);
}
let src = blocks.join('\n');

// Jinja テンプレート変数の置換（JS構文として不正なもののみ最小限）。
// クォートで囲まれた {{ ga_measurement_id }} のようなものは有効なJS文字列のまま
// なので触らない。裸の {{ ... }} だけを構文的に安全な値に置き換える。
const JINJA_REPLACEMENTS = [
  [/const _pageCardName=\{\{ card_name \| tojson \}\};/, 'const _pageCardName=null;'],
  [/const _pageMode=\{\{ page_mode \| tojson \}\};/, 'const _pageMode=null;'],
  [/window\.RARITY_CONFIG = \{\{ rarity_config_json \| safe \}\};/, 'window.RARITY_CONFIG = {};'],
  // クォートで囲まれた {{ ga_measurement_id }} はそのままでも有効なJS文字列だが、
  // 残存チェックの誤検知を避けるため無害な固定文字列に置き換える
  [/'\{\{ ga_measurement_id \}\}'/, "'GA_MEASUREMENT_ID_PLACEHOLDER'"],
];
for (const [pattern, replacement] of JINJA_REPLACEMENTS) {
  src = src.replace(pattern, replacement);
}
const leftoverJinja = src.match(/\{\{[^}]*\}\}/g);
if (leftoverJinja) {
  console.error('FAIL: 未置換のJinja変数が残っています（テンプレートが変更された可能性。JINJA_REPLACEMENTSを更新すること）: ' + leftoverJinja.join(', '));
  process.exit(1);
}

// ── 最小限のDOM/ブラウザAPIスタブ ──
// esc() は document.createElement('div').textContent=...; .innerHTML を使う。
// これはHTML Standardのテキストノードシリアライズ規則（& < > のみ実体参照化。
// 属性値ではないので " ' はエスケープしないのが仕様上正しい挙動）をそのまま再現する。
class FakeTextDiv {
  constructor() { this._text = ''; }
  set textContent(v) { this._text = String(v == null ? '' : v); }
  get textContent() { return this._text; }
  get innerHTML() {
    return this._text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
}

// 何を呼んでも自分自身（または無害な値）を返す寛容なスタブ。
// トップレベルで実行される addEventListener/querySelectorAll 等が例外で落ちないようにする。
function makePermissiveStub() {
  const handler = {
    get(target, prop) {
      if (prop === Symbol.toPrimitive || prop === 'valueOf' || prop === 'toString') return () => '';
      if (prop === 'length') return 0;
      if (prop === Symbol.iterator) return [][Symbol.iterator];
      if (prop === 'style' || prop === 'classList' || prop === 'dataset') return makePermissiveStub();
      return function () { return makePermissiveStub(); };
    },
    set() { return true; },
  };
  return new Proxy(function () {}, handler);
}

const fakeDocument = {
  createElement(tag) {
    if (tag === 'div' || tag === 'span') return new FakeTextDiv();
    return makePermissiveStub();
  },
  getElementById() { return makePermissiveStub(); },
  querySelector() { return makePermissiveStub(); },
  querySelectorAll() { return []; },
  addEventListener() {},
  removeEventListener() {},
  body: makePermissiveStub(),
  documentElement: makePermissiveStub(),
  cookie: '',
};

const sandbox = {
  document: fakeDocument,
  addEventListener() {},
  removeEventListener() {},
  location: { origin: 'https://example.test', href: 'https://example.test/', pathname: '/', search: '', hash: '' },
  navigator: { userAgent: 'node-verify' },
  localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
  fetch: () => new Promise(() => {}), // 呼ばれても解決しない（トップレベルfetchで例外を出さない）
  URL, // NodeネイティブのURLをそのまま使う（safeUrl()が使う）
  URLSearchParams,
  console,
  setTimeout,
  clearTimeout,
  requestAnimationFrame: (fn) => setTimeout(fn, 0),
  history: makePermissiveStub(),
  CSS: { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&') },
  matchMedia: () => ({ matches: false, addListener() {}, addEventListener() {} }),
};
sandbox.window = sandbox;
sandbox.self = sandbox;
sandbox.globalThis = sandbox;

const ctx = vm.createContext(sandbox);
try {
  vm.runInContext(src, ctx, { filename: 'templates/index.html (extracted inline scripts)' });
} catch (e) {
  console.error('FAIL: 抽出したスクリプトの実行中に例外が発生しました（トップレベルの副作用起因の可能性）: ' + e.message);
  console.error(e.stack);
  process.exit(1);
}

for (const fnName of ['esc', 'escJs', 'escAttr', 'safeUrl', 'wishBtnHtml', '_wishRaritySelectHtml']) {
  if (typeof ctx[fnName] !== 'function') {
    console.error(`FAIL: ${fnName} が抽出結果からグローバル関数として見つかりません（テンプレートの関数名/構造が変わった可能性）`);
    process.exit(1);
  }
}

// ── 検証本体 ──
let failures = 0;
function check(name, cond, detail) {
  if (cond) {
    console.log('OK: ' + name);
  } else {
    failures++;
    console.error('FAIL: ' + name + (detail ? ' — ' + detail : ''));
  }
}

// 生成されたHTMLに「エスケープされていない属性区切り文字（"または'）に続けて
// onmouseover= が出現する」パターンがあれば、属性からの脱出=注入成功とみなす。
// ダブルクォート属性からの脱出だけでなく、シングルクォート版ペイロードでの
// 脱出パターンも検出できるよう両方チェックする。
function attributeEscapes(html) {
  const dq = /onmouseover\s*=\s*"?alert/i.test(html) && /"[^>]*onmouseover/.test(html);
  const sq = /onmouseover\s*=\s*'?alert/i.test(html) && /'[^>]*onmouseover/.test(html);
  return dq || sq;
}

const XSS_DQ = 'テスト"onmouseover="alert(1)';
const XSS_SQ = "テスト'onmouseover='alert(1)";

// 1. escAttr() 単体（ダブルクォート/シングルクォートの実体参照化）
check('escAttr(): ダブルクォートを実体参照化する',
  ctx.escAttr(XSS_DQ).includes('&quot;') && !ctx.escAttr(XSS_DQ).includes('"'));
check('escAttr(): シングルクォートを実体参照化する',
  ctx.escAttr(XSS_SQ).includes('&#39;') && !ctx.escAttr(XSS_SQ).includes("'"));

// 2. esc()/escJs() は仕様通り quote を通す（だからこそ属性文脈では使えない、という前提の確認）
check('esc(): テキストノード用途の前提どおりダブルクォートは実体参照化しない',
  ctx.esc(XSS_DQ).includes('"'));
check('escJs(): JS文字列用途の前提どおりダブルクォートは対象外',
  ctx.escJs(XSS_DQ).includes('"'));

// 2b. escJs(): 生の改行/復帰が残っているとonclick等に埋め込んだJS文字列リテラルが
//     途中で改行されSyntaxErrorになる（2026-08-20修正）。エスケープされていることを確認
check('escJs(): 改行(\\n)がエスケープされ生の改行文字が残らない',
  !ctx.escJs('a\nb').includes('\n') && ctx.escJs('a\nb').includes('\\n'));
check('escJs(): 復帰(\\r)がエスケープされ生の復帰文字が残らない',
  !ctx.escJs('a\rb').includes('\r') && ctx.escJs('a\rb').includes('\\r'));

// 3. escAttr(escJs(x)) の重ね掛け: onclick 属性の脱出を防げるか
{
  const wrapped = ctx.escAttr(ctx.escJs(XSS_DQ));
  const htmlOut = `<button onclick="addToDeck('${wrapped}')">x</button>`;
  check('escAttr(escJs(x)): onclick属性からの脱出ペイロードを無害化する', !attributeEscapes(htmlOut), htmlOut);
  const attrValue = htmlOut.slice(htmlOut.indexOf("('") + 2, htmlOut.lastIndexOf("')"));
  check('escAttr(escJs(x)): 生成HTMLの属性値部分に生の " が含まれない（属性境界を壊さない）',
    !attrValue.includes('"'), attrValue);
}

// 4. 実際のレンダラー関数を実物のロジックで検証（脱出不可）。
//    ダブルクォート版・シングルクォート版の両方のペイロードを流す。
check('wishBtnHtml(): 実装関数の出力にonmouseover属性が注入されない（ダブルクォート版）',
  !attributeEscapes(ctx.wishBtnHtml(XSS_DQ, 1)), ctx.wishBtnHtml(XSS_DQ, 1));
check('wishBtnHtml(): 実装関数の出力にonmouseover属性が注入されない（シングルクォート版）',
  !attributeEscapes(ctx.wishBtnHtml(XSS_SQ, 1)), ctx.wishBtnHtml(XSS_SQ, 1));
check('_wishRaritySelectHtml(): data-name属性からの脱出ペイロードを無害化する（ダブルクォート版）',
  !attributeEscapes(ctx._wishRaritySelectHtml({ name: XSS_DQ, rarity: '' })),
  ctx._wishRaritySelectHtml({ name: XSS_DQ, rarity: '' }));
check('_wishRaritySelectHtml(): data-name属性からの脱出ペイロードを無害化する（シングルクォート版）',
  !attributeEscapes(ctx._wishRaritySelectHtml({ name: XSS_SQ, rarity: '' })),
  ctx._wishRaritySelectHtml({ name: XSS_SQ, rarity: '' }));

// 5. safeUrl() のスキーム検証（javascript:/data: を拒否、http/httpsのみ許可）
check('safeUrl(): javascript: スキームを空文字にする', ctx.safeUrl('javascript:alert(1)') === '');
check('safeUrl(): data: スキームを空文字にする',
  ctx.safeUrl('data:text/html,<script>alert(1)</script>') === '');
check('safeUrl(): https: は通す',
  ctx.safeUrl('https://example.com/item?x=1') === 'https://example.com/item?x=1');

// 6. 正常系: 通常のカード名・URLで表示が壊れないこと（代表ケース）
check('wishBtnHtml(): 正常なカード名で壊れず、期待どおりの属性を含む',
  ctx.wishBtnHtml('灰流うらら', 3).includes("wishAddFromBtn(this,'灰流うらら',3)"));
{
  const sel = ctx._wishRaritySelectHtml({ name: '青眼の白龍', rarity: 'アルティメットレア' });
  check('_wishRaritySelectHtml(): 正常なカード名・レアリティで壊れない',
    sel.includes('data-name="青眼の白龍"') && sel.includes('data-old="アルティメットレア"'), sel);
}
check('safeUrl(): 正常なURLはそのまま通る',
  ctx.safeUrl('https://www.cardrush.jp/product-list?keyword=abc') === 'https://www.cardrush.jp/product-list?keyword=abc');

// ── 7. 静的チェック: 属性文脈での esc(/escJs( の未ラップ残存が0であること ──
// reviewer監査と同じ走査ロジック: 「属性値の中に、escAttr()で包まれていない
// esc(/escJs( 呼び出しが（閉じクォートに達する前に）現れる」箇所を検出する。
// ソースファイルのテキストを直接走査する（レンダリング結果ではなく実装コード自体）。
{
  const STATIC_CHECK_TARGETS = [
    path.join(__dirname, '..', '..', 'templates', 'index.html'),
    path.join(__dirname, '..', '..', 'static', 'featured-matrix.js'),
    path.join(__dirname, '..', '..', 'static', 'packs.js'),
    path.join(__dirname, '..', '..', 'static', 'mydeck', 'deck-edit.js'),
  ];

  // attr="..." / attr='...' の属性値（"または'で閉じるまで）を、対象属性名について抜き出す。
  // href="${esc(x)}" のようなテンプレートリテラル形式と、
  // data-name="' + esc(x) + '" のような文字列連結形式のどちらも同じ正規表現で拾える
  // （このリポジトリのHTML属性は常にダブルクォートで書かれているため "..." のみで足りる）。
  const ATTR_RE = /\b(?:href|src|id|value|title|download|onclick|onmousedown|onchange|data-[\w-]+)\s*=\s*"([^"]*)"/g;

  // 文字列 s の中から escAttr( ... ) のバランスの取れた呼び出し全体を取り除く
  // （括弧の深さを数えて対応する閉じ括弧まで削除する）。
  function stripEscAttrCalls(s) {
    let result = '';
    let i = 0;
    while (i < s.length) {
      const idx = s.indexOf('escAttr(', i);
      if (idx === -1) { result += s.slice(i); break; }
      result += s.slice(i, idx);
      let depth = 1;
      let j = idx + 'escAttr('.length;
      while (j < s.length && depth > 0) {
        if (s[j] === '(') depth++;
        else if (s[j] === ')') depth--;
        j++;
      }
      i = j; // escAttr(...) 呼び出し全体を読み飛ばす（中身のescJs(等は安全とみなす）
    }
    return result;
  }

  // escAttr()で包まれずに esc( または escJs( が残っていれば違反
  function hasUnwrappedEscCall(attrValue) {
    const stripped = stripEscAttrCalls(attrValue);
    return /esc\(|escJs\(/.test(stripped);
  }

  let staticViolations = 0;
  for (const filePath of STATIC_CHECK_TARGETS) {
    const relPath = path.relative(path.join(__dirname, '..', '..'), filePath);
    const source = fs.readFileSync(filePath, 'utf-8');
    let match;
    ATTR_RE.lastIndex = 0;
    while ((match = ATTR_RE.exec(source)) !== null) {
      const attrValue = match[1];
      if (hasUnwrappedEscCall(attrValue)) {
        staticViolations++;
        console.error(`FAIL: ${relPath} に escAttr() で包まれていない esc(/escJs( が属性値内に残存: ${match[0]}`);
      }
    }
  }
  check(`静的チェック: templates/index.html・featured-matrix.js・packs.js・deck-edit.js の属性文脈に未ラップの esc(/escJs( が0件`,
    staticViolations === 0, `violations=${staticViolations}`);
}

console.log('\n' + (failures === 0 ? 'ALL OK' : failures + ' FAILURE(S)'));
process.exit(failures === 0 ? 0 : 1);
