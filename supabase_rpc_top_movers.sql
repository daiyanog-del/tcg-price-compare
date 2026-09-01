-- ⚠ 2026-09-01: 値動きの定義がこのプロジェクトには**2つ**ある。
--   この get_top_movers はサイトのトップ表示専用。X投稿は supabase_rpc_movers.sql の
--   get_price_movers を使い、ガードの構成が違う（あちら=複数店同方向 / こちら=定着チェック）。
--   統合しなかった理由と実測値は docs/decisions.md 2026-09-01 の項。
--
-- トップ（最初の画面）の価格推移ランキング用 RPC
-- Supabase の SQL Editor で実行してください
-- 2026-09-01 追加。本番へは MCP 経由で適用済み（マイグレーション名: create_get_top_movers）
--
-- なぜ RPC にしたか:
--   従来はアプリ側で1日分の price_history を全行取得して Python で集計していた。
--   本番実測で1日あたり 30,851 行あり、3日分＝約9万行を PostgREST 経由で読むため
--   キャッシュ失効後の初回計算に約15秒かかり、トップに「取得に失敗しました」と
--   誤表示していた（フロントの再試行が4秒しかなかった）。
--
-- なぜ get_price_movers（supabase_rpc_movers.sql）と別関数なのか:
--   あちらは「複数店が同方向に動いた」ガード②を持つ。7月の偽陽性98%対策として
--   正しい設計だが、本番実測でその条件を満たすカードが**ほぼ毎日0件**になり
--   （価格が動いた1,523件 → 閾値通過55件 → ガード②で0件）、画面表示の素材に
--   ならなかった。この関数はガード②の代わりに「定着チェック」を使う。
--   X投稿（x_poster.py）は引き続き get_price_movers を使う＝**値動きの定義が
--   2つ並立している**ことに注意。
--
-- 集計の規約（top_page.aggregate_common_shop_movers と同じ意味であること）:
--   代表レアリティ: 当日に存在するレアリティのうち最安（'(不明)'・'' は除外）。
--     レアリティを跨いで最安を取ると「安いレアリティの在庫切れ→高いレアリティへの
--     繰り上がり」が偽の値上がりとして出る（本番実測で同一(カード×店)に複数
--     レアリティがある組は 7,557/11,747＝64% と多数派）。
--   ガード①（共通店舗）: 当日と p_days_back 日前の**両方に記録がある店舗**だけで
--     最安を出す。これを外すと「動いた」の約1/4が店舗欠測由来の偽の動きになる。
--   閾値: 当日・N日前の**両方**に p_min_price を適用する（片側だけだと、7日前は
--     安かったカードの一時的な倍増が1位に出る。例: 次元融合 ¥540→¥1,080）。
--   定着チェック: 在庫入替（最安の1枚が売れて次点へ繰り上がる）は一時的な変動なので
--     「当日の価格が前日も同じか」で見分ける。判定は「共通店舗のうち前日データも
--     ある店舗」の集合Sを先に確定し、**当日側の最安もSに限定して**比較する
--     （当日側だけ全共通店舗の min を使うと非対称になり偽陽性・偽陰性の両方が出る）。
--   前日データが1行も無い日は定着チェックをスキップして通す（欠測でランキングが
--     空になる方が害が大きい）。stability_checked でその旨を返す。
--
-- 引数:
--   p_min_price      int  カード単位の最安値の下限（当日・N日前の両方に適用）
--   p_days_back      int  比較する過去日数
--   p_stability_days int  定着チェックで参照する「前日」までの日数
--   p_limit          int  変化率の絶対値の降順で上位何件返すか（up/down 合算）
--
-- 戻り値 (行ごと): card_name, rarity, price_old, price_new, pct,
--                  date_new, date_old, stability_checked
--
-- 性能の経緯（実測・2026-09-01）:
--   1) SQL言語版（日付を CTE から取得）        : 94,201ms — プランナが
--      price_history_uniq の recorded_at を Index Cond に使えず、1組合せあたり
--      90日ぶん（実測51行）を読んでから捨てていた
--   2) plpgsql化 + 自己結合をやめ3日分を1スキャンして条件付き集計:  3,418ms
--   3) 被覆インデックス idx_price_history_date_card_shop_rarity 追加:  844ms
--   本番API実測: 初回1,111ms / キャッシュ後 209〜223ms（切替前は約15秒）
--   ※ anon ロールの statement_timeout は 3s、authenticated は 8s。
--     3) を入れないと anon から 57014(statement timeout) で落ちる。
create or replace function get_top_movers(
  p_min_price      int default 1000,
  p_days_back      int default 7,
  p_stability_days int default 1,
  p_limit          int default 10
)
returns table (
  card_name        text,
  rarity           text,
  price_old        int,
  price_new        int,
  pct              numeric,
  date_new         date,
  date_old         date,
  stability_checked boolean
)
language plpgsql
stable
as $$
declare
  v_new    date;
  v_old    date;
  v_prev   date;
begin
  select max(ph.recorded_at) into v_new from price_history ph;
  if v_new is null then
    return;
  end if;
  v_old  := v_new - p_days_back;
  v_prev := v_new - p_stability_days;

  return query
  with base as (  -- 3日ぶんを1回だけ読む（被覆インデックスでindex-only scan）
    select ph.card_name as cn, ph.shop as sh, ph.rarity as rr,
           min(ph.min_price) filter (where ph.recorded_at = v_new)  as np,
           min(ph.min_price) filter (where ph.recorded_at = v_old)  as op,
           min(ph.min_price) filter (where ph.recorded_at = v_prev) as pp
    from price_history ph
    where ph.recorded_at in (v_new, v_old, v_prev)
      and ph.min_price is not null
    group by 1, 2, 3
  ),
  prev_n as (select count(*) as n from base b where b.pp is not null),
  rep as (
    select distinct on (b.cn) b.cn, b.rr
    from base b
    where b.np is not null and b.rr is not null and b.rr not in ('(不明)', '')
    order by b.cn, b.np asc
  ),
  pair as (
    select b.cn, b.sh, b.np, b.op, b.pp
    from base b
    join rep r on r.cn = b.cn and r.rr = b.rr
    where b.np is not null and b.op is not null
  ),
  agg as (
    select p.cn,
           min(p.np) as p_new,
           min(p.op) as p_old,
           min(p.np) filter (where p.pp is not null) as new_in_s,
           min(p.pp) filter (where p.pp is not null) as prev_in_s
    from pair p group by 1
  )
  select a.cn, r.rr, a.p_old, a.p_new,
         round(((a.p_new - a.p_old)::numeric / a.p_old) * 100, 1),
         v_new, v_old, (pn.n > 0)
  from agg a
  join rep r on r.cn = a.cn
  cross join prev_n pn
  where a.p_new >= p_min_price
    and a.p_old >= p_min_price
    and a.p_new <> a.p_old
    and (pn.n = 0 or (a.new_in_s is not null and a.new_in_s = a.prev_in_s))
  order by abs((a.p_new - a.p_old)::numeric / a.p_old) desc
  limit p_limit;
end;
$$;

-- 必須の被覆インデックス。これが無いと日付で絞ったあと3万行×3日ぶんのヒープを
-- 読むことになり、実測3.4秒で anon の statement_timeout(3s) を超える。
create index if not exists idx_price_history_date_card_shop_rarity
  on public.price_history (recorded_at, card_name, shop, rarity)
  include (min_price);
