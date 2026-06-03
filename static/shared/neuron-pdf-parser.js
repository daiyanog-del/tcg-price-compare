/**
 * neuron-pdf-parser.js
 * ニューロン デッキシートPDFをパースしてカード一覧を返す
 * 依存: pdfjs-dist (CDNから動的import)
 */

const PDFJS_VER = '4.4.168';
const PDFJS_BASE = `https://cdn.jsdelivr.net/npm/pdfjs-dist@${PDFJS_VER}/build`;

let _pdfjs = null;

async function _getPdfJs() {
  if (_pdfjs) return _pdfjs;
  const mod = await import(`${PDFJS_BASE}/pdf.min.mjs`);
  mod.GlobalWorkerOptions.workerSrc = `${PDFJS_BASE}/pdf.worker.min.mjs`;
  _pdfjs = mod;
  return mod;
}

const IS_DIGIT = /^\d+$/;

// ヘッダー語を str から検出するマッチャー（includes で部分一致）
const HDR = {
  monster:  s => s.includes('モンスター'),
  spell:    s => s.includes('魔法カード') || (s.includes('魔法') && s.includes('カード')),
  trap:     s => s.includes('罠カード')   || (s.includes('罠') && s.includes('カード')),
  ex:       s => s.includes('エクストラ'),
  side:     s => s.includes('サイドデッキ') || (s.includes('サイド') && s.includes('デッキ')),
  qty:      s => s === '枚数' || s.trim() === '枚数',
};

// パース結果テキストから除外する語
const SKIP_RE = [
  /合計\s*>+/,
  /^>+/,
  /^メインデッキ/,
  /^かな$/,
  /^氏名$/,
  /^カードゲームID/,
  /^参加番号$/,
  /^日付$/,
  /^イベント名$/,
];

function _skipStr(str) {
  if (SKIP_RE.some(r => r.test(str))) return true;
  // ヘッダーワード自体
  if (HDR.monster(str) || HDR.spell(str) || HDR.trap(str)) return true;
  if (HDR.ex(str) || HDR.side(str)) return true;
  if (HDR.qty(str) || str === 'カード名') return true;
  // IDコード: 空白区切りの1桁数字が5個以上
  if (/^[\d\s]+$/.test(str) && str.replace(/\s+/g, '').length > 4) return true;
  return false;
}

/**
 * レイアウト情報（列ヘッダ位置・セクション境界）を検出
 * 失敗時は { ok: false, debugLines: [...] } を返す
 */
function _detectLayout(items) {
  // includes ベースで最初にマッチするアイテムを返す
  const find = (fn) => items.find(fn);

  const monH  = find(it => HDR.monster(it.str));
  const splH  = find(it => HDR.spell(it.str) && !HDR.monster(it.str));
  const trpH  = find(it => HDR.trap(it.str)  && !HDR.monster(it.str) && !HDR.spell(it.str));
  const exH   = find(it => HDR.ex(it.str));
  const sideH = find(it => HDR.side(it.str) && !HDR.ex(it.str));

  // 診断: 見つかったヘッダーをデバッグ用に返す
  const debugLines = [
    `[DEBUG] 総アイテム数: ${items.length}`,
    `モンスターH: ${monH ? `"${monH.str}" (${monH.x.toFixed(0)},${monH.y.toFixed(0)})` : '未検出'}`,
    `EXH: ${exH ? `"${exH.str}" (${exH.x.toFixed(0)},${exH.y.toFixed(0)})` : '未検出'}`,
    `先頭20件: ${items.slice(0, 20).map(it => `"${it.str}"`).join(', ')}`,
  ];

  if (!monH || !exH) return { ok: false, debugLines };

  // 列ヘッダをx昇順でソート
  const colHeaders = [
    { type: 'monster', x: monH.x, y: monH.y },
    splH && { type: 'spell',   x: splH.x, y: splH.y },
    trpH && { type: 'trap',    x: trpH.x, y: trpH.y },
  ].filter(Boolean).sort((a, b) => a.x - b.x);

  // ページ幅の推定
  const pageWidth = Math.max(...items.map(it => it.x + (it.w || 0))) + 50;

  // 各列のx範囲
  const colRanges = colHeaders.map((h, i) => {
    const prev = colHeaders[i - 1];
    const next = colHeaders[i + 1];
    return {
      type: h.type,
      startX: prev ? (prev.x + h.x) / 2 : 0,
      endX:   next ? (h.x + next.x) / 2 : pageWidth,
      headerX: h.x,
    };
  });

  // 「枚数」ヘッダのx位置を各列に対応付け
  const qtyItems = items.filter(it => HDR.qty(it.str));
  const qtyXPerCol = {};
  for (const qh of qtyItems) {
    const col = colRanges.find(r => qh.x >= r.startX && qh.x < r.endX);
    // メインデッキセクション内（EXヘッダより上）のみ
    if (col && qh.y > exH.y) {
      qtyXPerCol[col.type] = qh.x;
    }
  }

  // EXセクションの「枚数」x
  const exQtyItems = qtyItems.filter(it => it.y <= exH.y && (!sideH || it.y >= sideH.y));

  return {
    ok: true,
    colRanges,
    qtyXPerCol,
    mainHeaderY: monH.y,
    exHeaderY:   exH.y,
    sideHeaderY: sideH ? sideH.y : -Infinity,
    exH, sideH,
    exQtyItems,
    debugLines,
  };
}

/**
 * メインデッキ（モンスター／魔法／罠）をパース
 */
function _parseMain(items, layout, warnings) {
  const mainItems = items.filter(it =>
    it.y < layout.mainHeaderY && it.y > layout.exHeaderY && !_skipStr(it.str)
  );

  const result = [];
  const QTY_TOL = 30;

  for (const colRange of layout.colRanges) {
    const colItems = mainItems
      .filter(it => it.x >= colRange.startX && it.x < colRange.endX)
      .sort((a, b) => b.y - a.y);

    const qtyX = layout.qtyXPerCol[colRange.type];

    // qtyXが未検出の場合: 列内の最右端digit列を量とみなす
    let effectiveQtyX = qtyX;
    if (effectiveQtyX === undefined) {
      const digitXs = colItems.filter(it => IS_DIGIT.test(it.str)).map(it => it.x);
      if (digitXs.length > 0) effectiveQtyX = Math.max(...digitXs);
    }

    const quantities = [];
    const names = [];

    for (const item of colItems) {
      if (IS_DIGIT.test(item.str)) {
        const isQty = effectiveQtyX !== undefined && Math.abs(item.x - effectiveQtyX) <= QTY_TOL;
        if (isQty) quantities.push({ val: parseInt(item.str, 10), y: item.y });
        // else: 行番号→捨てる
      } else {
        names.push({ str: item.str, y: item.y });
      }
    }

    // y降順で並んでいるためインデックスでペアリング
    for (let i = 0; i < names.length; i++) {
      const qty = quantities[i]?.val;
      const safeQty = (qty && qty >= 1 && qty <= 3) ? qty : 1;
      if (!quantities[i]) {
        warnings.push(`「${names[i].str}」の枚数が読み取れません（1枚として扱います）`);
      }
      result.push({ qty: safeQty, name: names[i].str });
    }
  }

  return result;
}

/**
 * エクストラデッキをパース
 */
function _parseEx(items, layout, warnings) {
  const { exH, sideH } = layout;

  const exItems = items.filter(it =>
    it.y < exH.y &&
    (sideH ? it.y >= sideH.y : true) &&
    !_skipStr(it.str)
  );

  if (exItems.length === 0) return [];

  // EXとSideの列分割
  const splitX = sideH ? (exH.x + sideH.x) / 2 : Infinity;
  const exColItems = exItems.filter(it => it.x < splitX).sort((a, b) => b.y - a.y);

  const exQtyX = layout.exQtyItems
    .filter(it => it.x < splitX)
    .map(it => it.x)[0];

  let effectiveQtyX = exQtyX;
  if (effectiveQtyX === undefined) {
    const digitXs = exColItems.filter(it => IS_DIGIT.test(it.str)).map(it => it.x);
    if (digitXs.length > 0) effectiveQtyX = Math.max(...digitXs);
  }

  const quantities = [];
  const names = [];
  const QTY_TOL = 30;

  for (const item of exColItems) {
    if (IS_DIGIT.test(item.str)) {
      const val = parseInt(item.str, 10);
      const isQty = effectiveQtyX !== undefined
        ? Math.abs(item.x - effectiveQtyX) <= QTY_TOL
        : val >= 1 && val <= 3;
      if (isQty) quantities.push({ val, y: item.y });
    } else {
      names.push({ str: item.str, y: item.y });
    }
  }

  const result = [];
  for (let i = 0; i < names.length; i++) {
    const qty = quantities[i]?.val;
    const safeQty = (qty && qty >= 1 && qty <= 3) ? qty : 1;
    if (!quantities[i]) {
      warnings.push(`EX「${names[i].str}」の枚数が読み取れません（1枚として扱います）`);
    }
    result.push({ qty: safeQty, name: names[i].str });
  }
  return result;
}

/**
 * ニューロン デッキシートPDFをパース
 * @param {File} file
 * @param {Object} [options]
 * @param {boolean} [options.includeSide=false]
 * @returns {Promise<{main:[{qty,name}], ex:[{qty,name}], side:[], warnings:string[], ok:boolean}>}
 */
export async function parseNeuronPdf(file, { includeSide = false } = {}) {
  const warnings = [];

  let pdfjs;
  try {
    pdfjs = await _getPdfJs();
  } catch (e) {
    throw new Error(`PDF.jsの読み込みに失敗しました。ネット接続を確認してください。(${e.message})`);
  }

  const buf = await file.arrayBuffer();
  let pdf;
  try {
    pdf = await pdfjs.getDocument({ data: buf }).promise;
  } catch (e) {
    throw new Error(`PDFの読み込みに失敗しました。ファイルが破損している可能性があります。(${e.message})`);
  }

  if (pdf.numPages === 0) throw new Error('PDFにページがありません');
  if (pdf.numPages > 1) warnings.push(`複数ページ(${pdf.numPages}ページ)のPDFです。1ページ目のみ処理します。`);

  const page = await pdf.getPage(1);
  const tc = await page.getTextContent();

  const items = tc.items
    .filter(it => it.str?.trim())
    .map(it => ({
      str: it.str.trim(),
      x: it.transform[4],
      y: it.transform[5],
      w: it.width || 0,
    }))
    .sort((a, b) => b.y - a.y || a.x - b.x);

  const layout = _detectLayout(items);

  if (!layout.ok) {
    // 診断情報をwarningsに含めてモーダルで確認できるようにする
    warnings.push('ニューロン形式のデッキシートとして認識できませんでした。手動で修正してください。');
    warnings.push(...layout.debugLines);
    return { main: [], ex: [], side: [], warnings, ok: false };
  }

  const main = _parseMain(items, layout, warnings);
  const ex   = _parseEx(items, layout, warnings);

  return {
    main,
    ex,
    side: [],
    warnings,
    ok: main.length > 0 || ex.length > 0,
  };
}

/**
 * [{qty, name}] → "3 カード名\n" 形式のテキストに変換
 */
export function deckListToText(entries) {
  return entries.map(e => `${e.qty} ${e.name}`).join('\n');
}
