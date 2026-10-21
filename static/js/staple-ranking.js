// トップのランキング3列目「汎用カード」（2026-10-21・ユーザー承認 案1）
// /api/staple-movers（staple_ranking.py）を読む。既定は値下がり順（1週間で下がった金額の大きい順）。
// 行の見た目・読込中・画像の読み込みは index-main.js の値動き列と同じ部品を使う
// （esc / escAttr / escJs / cssClass / _setMoversHtml / _setMoversLoading / _fmtDate /
//   loadRankingImages / rankingLinkClick / searchCard / _ga。どれも index-main.js が先に定義する）。
(function () {
  let sort = 'drop';
  // タブ切替のたびに増やす世代番号。切替前の読込が切替後の表示を上書きしないよう、応答時に世代が変わっていたら捨てる
  let seq = 0;

  function rowHtml(item, i) {
    const rarityBadge = item.rarity ? `<span class="rb rb-${cssClass(item.rarity)}">${esc(item.rarity)}</span>` : '';
    const themes = item.themes ? `<span class="movers-sub">${item.themes}テーマで採用</span>` : '';
    let diffHtml = '';
    if (item.diff === 0) {
      diffHtml = '<span class="movers-diff" style="color:var(--text-d)">7日間 変動なし</span>';
    } else if (item.diff !== null && item.diff !== undefined) {
      const isUp = item.diff > 0;
      const sign = isUp ? '+' : '';
      // 販売の値段なので 値上がり=赤（up）・値下がり=緑（down）。買取とは色の意味が逆
      diffHtml = `<span class="movers-diff ${isUp ? 'up' : 'down'}">${sign}${item.diff.toLocaleString()} (${sign}${item.pct}%)</span>`;
    }
    return `<a class="movers-item" href="/card/${escAttr(encodeURIComponent(item.name))}" onclick="if(!rankingLinkClick(event))return true;StapleRanking.open('${escAttr(escJs(item.name))}',${i + 1})" data-card="${escAttr(item.name)}">
      <span class="movers-rank">${i + 1}</span>
      <span class="movers-thumb"></span>
      <span class="movers-name"><span class="movers-name-text">${esc(item.name)}</span><span class="movers-rarity">${rarityBadge} ${themes}</span></span>
      <span class="movers-price">
        <span class="movers-price-now">&yen;${item.price.toLocaleString()}</span>
        ${diffHtml}
      </span>
    </a>`;
  }

  function load(retry) {
    retry = retry || 0;
    const mySeq = seq;
    const list = document.getElementById('stapleList');
    const period = document.getElementById('staplePeriod');
    if (!list || !period) return;
    if (!retry) { _setMoversLoading(list); period.textContent = ''; }
    fetch(`/api/staple-movers?sort=${sort}&limit=10`)
      .then(r => r.json())
      .then(data => {
        if (mySeq !== seq) return;
        if (data.error) {
          if (retry < 3) {
            _setMoversHtml(list, '<div class="movers-fail">集計中です。少しお待ちください…</div>');
            setTimeout(() => { if (mySeq === seq) load(retry + 1); }, 2000);
            return;
          }
          _setMoversHtml(list, '<div class="movers-fail">汎用カードの取得に失敗しました</div>');
          return;
        }
        const items = data.items || [];
        if (data.date_old && data.date_new) {
          period.textContent = sort === 'drop'
            ? `${_fmtDate(data.date_old)} → ${_fmtDate(data.date_new)} の最安値の変化（共通店舗のみ）`
            : `入賞デッキで採用されたテーマが多い順 · ${_fmtDate(data.date_new)} の最安値`;
        }
        if (!items.length) {
          _setMoversHtml(list, `<div class="movers-fail">${sort === 'drop' ? 'この1週間で値下がりした汎用カードはありません' : '該当するカードがありません'}</div>`);
          return;
        }
        _setMoversHtml(list, items.map(rowHtml).join(''));
        loadRankingImages('#stapleList .movers-item', 'movers-thumb');
      })
      .catch(() => {
        if (mySeq !== seq) return;
        if (retry < 2) { setTimeout(() => { if (mySeq === seq) load(retry + 1); }, 2000); return; }
        _setMoversHtml(list, '<div class="movers-fail">汎用カードの取得に失敗しました</div>');
      });
  }

  function switchSort(next) {
    sort = next;
    seq++;
    document.querySelectorAll('#stapleSortTabs .movers-tab').forEach(b => b.classList.toggle('active', b.dataset.sort === next));
    load();
  }

  function open(name, rank) {
    _ga('staple_click', { rank: rank, sort: sort });
    searchCard(name);
  }

  window.StapleRanking = { load: () => load(), switchSort, open };
  // index-main.js のトップの欄の読込がこのファイルより先に走った場合
  if (window._stapleRankingPending) { window._stapleRankingPending = false; load(); }
})();
