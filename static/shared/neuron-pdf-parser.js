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

// 除外すべきヘッダ語・ラベル
const SKIP_SET = new Set([
  'モンスターカード', '魔法カード', '罠カード',
  'エクストラデッキ', 'サイドデッキ',
  'カード名', '枚数',
  'かな', '氏名', 'カードゲームID', '参加番号', '日付', 'イベント名',
]);

function _skip(str) {
  if (SKIP_SET.has(str)) return true;
  if (/合計\s*>>/.test(str)) return true;
  if (/^>>>/.test(str)) return true;
  // 1桁数字のみで構成される長い列（プレイヤーID）
  if (/^[\d\s]+$/.test(str) && str.replace(/\s/g, '').length > 3) return true;
  return false;
}

const IS_DIGIT = /^\d+$/;

/**
 * y座標でアイテムをグルーピングしてvisual rowsを作る
 */
function _clusterRows(items, tol) {
  const rows = [];
  for (const item of items) {
    let found = false;
    for (const row of rows) {
      if (Math.abs(row.y - item.y) <= tol) {
        row.items.push(item);
        // 加重平均でy更新
        row.y = row.items.reduce((s, it) => s + it.y, 0) / row.items.length;
        found = true;
        break;
      }
    }
    if (!found) rows.push({ y: item.y, items: [item] });
  }
  rows.forEach(r => r.items.sort((a, b) => a.x - b.x));
  return rows.sort((a, b) => b.y - a.y); // y降順=上から下
}

/**
 * レイアウト情報（列ヘッダ位置・セクション境界）を検出
 */
function _detectLayout(items) {
  const find = (fn) => items.find(fn);

  const monH  = find(it => it.str === 'モンスターカード');
  const splH  = find(it => it.str === '魔法カード');
  const trpH  = find(it => it.str === '罠カード');
  const exH   = find(it => it.str === 'エクストラデッキ');
  const sideH = find(it => it.str === 'サイドデッキ');

  if (!monH || !exH) return { ok: false };

  // 列ヘッダをx昇順でソート
  const colHeaders = [
    monH  && { type: 'monster', x: monH.x,  y: monH.y  },
    splH  && { type: 'spell',   x: splH.x,  y: splH.y  },
    trpH  && { type: 'trap',    x: trpH.x,  y: trpH.y  },
  ].filter(Boolean).sort((a, b) => a.x - b.x);

  // ページ幅の推定（全アイテムのx最大値 + 余裕）
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

  // 「枚数」ヘッダのx位置を各列に対応付け（量の列を特定するため）
  const qtyHeaders = items.filter(it => it.str === '枚数');
  const qtyXPerCol = {};
  for (const qh of qtyHeaders) {
    const col = colRanges.find(r => qh.x >= r.startX && qh.x < r.endX);
    if (col && qh.y > exH.y) { // メインデッキセクション内のみ
      qtyXPerCol[col.type] = qh.x;
    }
  }

  // EXセクションの「枚数」x
  const exQtyHeaders = qtyHeaders.filter(it => it.y <= exH.y && (!sideH || it.y >= sideH.y));

  return {
    ok: true,
    colRanges,
    qtyXPerCol,
    mainHeaderY: monH.y,
    exHeaderY:   exH.y,
    sideHeaderY: sideH ? sideH.y : -Infinity,
    exH, sideH,
    exQtyHeaders,
  };
}

/**
 * x座標から列タイプを特定
 */
function _getColType(x, colRanges) {
  return colRanges.find(r => x >= r.startX && x < r.endX)?.type ?? null;
}

/**
 * メインデッキ（モンスター／魔法／罠）をパース
 * strategy: 各列で量(qty)と名前(name)をy降順で並べ、インデックスでペアリング
 */
function _parseMain(items, layout, warnings) {
  const mainItems = items.filter(it =>
    it.y < layout.mainHeaderY && it.y > layout.exHeaderY && !_skip(it.str)
  );

  const result = [];
  const QTY_TOL = 30; // pt単位で「枚数」ヘッダとのx許容誤差

  for (const colRange of layout.colRanges) {
    const colItems = mainItems
      .filter(it => it.x >= colRange.startX && it.x < colRange.endX)
      .sort((a, b) => b.y - a.y); // y降順（上から下）

    const qtyX = layout.qtyXPerCol[colRange.type];

    // 枚数ヘッダが見つからない場合: 列内の最右端digit列を量とみなす
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
        if (isQty) {
          quantities.push({ val: parseInt(item.str, 10), y: item.y });
        }
        // else: 行番号とみなして捨てる
      } else {
        names.push({ str: item.str, y: item.y });
      }
    }

    // インデックスでペアリング（y降順で並んでいるため順序が一致する）
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
 * エクストラとサイドは横並びのため、x範囲で分割
 */
function _parseEx(items, layout, warnings) {
  const { exH, sideH } = layout;

  // EXセクション内のアイテム
  const exItems = items.filter(it =>
    it.y < exH.y &&
    (sideH ? it.y >= sideH.y : true) &&
    !_skip(it.str)
  );

  if (exItems.length === 0) return [];

  // EXとSideの列分割: sideH.xより小さい = EX、大きい = Side
  const splitX = sideH ? (exH.x + sideH.x) / 2 : Infinity;
  const exColItems = exItems.filter(it => it.x < splitX).sort((a, b) => b.y - a.y);

  // 量ヘッダのx（EX列内の「枚数」）
  const exQtyX = layout.exQtyHeaders
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
      // 行番号(1-15)と量(1-3)の区別: 枚数ヘッダx基準 or 値で判断
      const isQty = effectiveQtyX !== undefined
        ? Math.abs(item.x - effectiveQtyX) <= QTY_TOL
        : val >= 1 && val <= 3;
      if (isQty) {
        quantities.push({ val, y: item.y });
      }
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
 * @param {File} file - PDFファイル
 * @param {Object} options
 * @param {boolean} [options.includeSide=false] - サイドデッキも含める（未使用、将来用）
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
  if (pdf.numPages > 1) warnings.push('複数ページのPDFです。1ページ目のみ処理します。');

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

  // デバッグ用: console.logでアイテム確認可能
  // console.log('PDF items:', items.map(it => `(${it.x.toFixed(0)},${it.y.toFixed(0)}) "${it.str}"`).join('\n'));

  const layout = _detectLayout(items);

  if (!layout.ok) {
    warnings.push('ニューロン形式のデッキシートとして認識できませんでした。手動で修正してください。');
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
