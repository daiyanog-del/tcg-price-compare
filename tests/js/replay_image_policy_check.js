// リプレイの画像辞書を出してよい形に直すことの単体チェック（2026-10-29 監査の指摘）。
// ① utils/replay-image-policy.js の振り分け（実物をそのまま読む）
// ② replay-service.js がリプレイを読み込むとき、必ずその振り分けを通すこと
//    （replay_service_comment_check.js と同じく import 行をスタブに差し替えて data:URL で読み込む。
//     振り分けは実物のコードを差し込み、トークンを描く処理だけをスタブにする）
'use strict';
const fs = require('fs'), path = require('path'), assert = require('assert/strict');
const { pathToFileURL } = require('url');
const root = path.join(__dirname, '../..');
const js = path.join(root, 'static/solitaire/js');
const b64 = (src) => 'data:text/javascript;base64,' + Buffer.from(src).toString('base64');
const strip = (src) => src.replace(/^import [\s\S]*?from '[^']+';\r?$/gm, '').replace(/^export /gm, '');

const YGORES = 'https://artworks-jp-n.ygoresources.com/1/23/45_1.png';
const KANABELL = 'https://yg-card-images.ka-nabell.com/card/card100290576_1.jpg';

(async () => {
  // ── ① 振り分け ──
  const policy = await import(pathToFileURL(path.join(js, 'utils/replay-image-policy.js')).href);
  const c = policy.classifyReplayImage;
  assert.deepEqual(c('card://灰流うらら', 'x'), { kind: 'card', src: 'card://灰流うらら' });
  assert.equal(c('data:image/jpeg;base64,AAAA', '羊').kind, 'token');
  assert.deepEqual(c(YGORES, 'x'), { kind: 'url', src: YGORES });
  assert.deepEqual(c(KANABELL, 'x'), { kind: 'url', src: KANABELL });
  for (const bad of [
    'https://evil.example/a.png',
    'http://artworks-jp-n.ygoresources.com/1/23/45_1.png',            // https でない
    'https://artworks-jp-n.ygoresources.com/redirect?to=https://evil', // 出していない形・クエリつき
    'https://artworks-jp-n.ygoresources.com/1/23/45_1.png?x=1',
    'https://artworks-jp-n.ygoresources.com/1/23/45_1.png#x',
    'https://u:p@artworks-jp-n.ygoresources.com/1/23/45_1.png',
    'https://artworks-jp-n.ygoresources.com@evil.example/1/23/45_1.png',
    'https://www.ka-nabell.com/redirect?url=https://evil',
    'https://www.ka-nabell.com/img/s/../../redirect',
    'https://www.ka-nabell.com/img/s/abc_123.jpg',                       // 旧サイト（2026-10-21 停止）の画像
    'https://yg-card-images.ka-nabell.com/card/../redirect',
    'blob:https://artworks-jp-n.ygoresources.com/x',
    'javascript:alert(1)',
    ' card://x',
  ]) {
    assert.deepEqual(c(bad, ' 増殖するG '), { kind: 'card', src: 'card://増殖するG' }, bad);
  }
  assert.deepEqual(c(123, undefined), { kind: 'card', src: 'card://' }, '名前が無ければ名前だけの札');
  assert.deepEqual(c(null, null), { kind: 'card', src: 'card://' });

  // ── ② replay-service が読み込みのたびに振り分けを通す ──
  const noop = '(...a)=>{}';
  const stubs = `
const returnAllCardsToDeck=${noop}; const attachCardImageListeners=${noop}; const sortPoolCards=${noop};
const applyCardState=${noop}; const getCardState=${noop}; const isTokenCard=()=>false; const markTokenIfSrc=${noop};
const createCardCounter=${noop}; const restackCustomSlot=${noop};
const playActivateEffect=${noop}; const flipMoveClone=${noop}; const playSetFlip=${noop}; const flyBetweenRects=${noop};
const prefersReducedMotion=()=>true; const createProxyCardElement=${noop}; const isMobilePortrait=()=>false;
const renderCommentView=${noop}; const showCutin=${noop}; const hideCutin=${noop};
globalThis.__drawn=[]; const generateTokenDataURL=async (n)=>{ globalThis.__drawn.push(n); return 'data:image/jpeg;base64,DRAWN'; };
const tokenNameOf=(r)=>String(r ?? '').trim().slice(0, 12) || 'トークン';
`;
  const tokenSrc = strip(fs.readFileSync(path.join(js, 'utils/token-src.js'), 'utf8'));
  const policySrc = strip(fs.readFileSync(path.join(js, 'utils/replay-image-policy.js'), 'utf8'));
  const model = strip(fs.readFileSync(path.join(js, 'services/comment-model.js'), 'utf8'));
  let src = fs.readFileSync(path.join(js, 'services/replay-service.js'), 'utf8');
  src = src.replace(/^import [\s\S]*?from '[^']+';\r?$/gm, '');
  assert(!/^import /m.test(src));
  globalThis.document = { getElementById: () => null, querySelectorAll: () => [] };
  const svc = await import(b64(src + stubs + tokenSrc + policySrc + model));

  await svc._setReplayData(
    { a: 'https://evil.example/a.png', b: YGORES, c: 'data:image/png;base64,EVILPICTURE', d: 'https://evil.example/d.png', e: 'card://灰流うらら' },
    { a: '増殖するG', b: '本物', c: ' 羊 ', e: '灰流うらら' },
    [],
  );
  assert.deepEqual(svc.getImages(), {
    a: 'card://増殖するG',
    b: YGORES,
    c: 'data:image/jpeg;base64,DRAWN',   // 送られた絵は使わず、名前から描き直す
    d: 'card://',
    e: 'card://灰流うらら',
  });
  assert.deepEqual(globalThis.__drawn, ['羊']);

  // トークンを描けなくても読み込みは止めない（トークンの印だけ残す）
  const svc2 = await import(b64(src + stubs.replace("globalThis.__drawn.push(n); return 'data:image/jpeg;base64,DRAWN';", "throw new Error('no canvas');") + tokenSrc + policySrc + model + '\n//2'));
  const warn = console.warn; console.warn = () => {};
  try {
    await svc2._setReplayData({ t: 'data:image/png;base64,X' }, { t: '羊' }, []);
  } finally { console.warn = warn; }
  assert.deepEqual(svc2.getImages(), { t: 'data:image/png;base64,' });

  console.log('replay_image_policy_check: OK');
})().catch((e) => { console.error(e); process.exit(1); });
