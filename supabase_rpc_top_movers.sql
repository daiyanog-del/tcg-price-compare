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
language sql
stable
as $$
with l as (select max(ph.recorded_at) as d from price_history ph),
rep as (
  select distinct on (ph.card_name) ph.card_name, ph.rarity
  from price_history ph, l
  where ph.recorded_at = l.d and ph.min_price is not null
    and ph.rarity is not null and ph.rarity not in ('(不明)', '')
  order by ph.card_name, ph.min_price asc
),
pair as (
  select a.card_name, a.shop,
         min(a.min_price) as np,
         min(b.min_price) as op
  from price_history a
  join price_history b
    on a.card_name = b.card_name and a.shop = b.shop and a.rarity = b.rarity
  join rep r on r.card_name = a.card_name and r.rarity = a.rarity
  cross join l
  where a.recorded_at = l.d
    and b.recorded_at = l.d - p_days_back
    and a.min_price is not null and b.min_price is not null
  group by 1, 2
),
card as (
  select p.card_name, min(p.np) as p_new, min(p.op) as p_old
  from pair p group by 1
),
stab as (
  select p.card_name,
         min(p.np) as new_in_s,
         min(c.min_price) as prev_in_s
  from pair p
  join l on true
  join rep r on r.card_name = p.card_name
  join price_history c
    on c.card_name = p.card_name and c.shop = p.shop and c.rarity = r.rarity
   and c.recorded_at = l.d - p_stability_days and c.min_price is not null
  group by 1
),
prev_any as (
  select count(*) as n from price_history ph, l where ph.recorded_at = l.d - p_stability_days
)
select c.card_name,
       r.rarity,
       c.p_old,
       c.p_new,
       round(((c.p_new - c.p_old)::numeric / c.p_old) * 100, 1) as pct,
       l.d as date_new,
       (l.d - p_days_back) as date_old,
       (prev_any.n > 0) as stability_checked
from card c
join rep r on r.card_name = c.card_name
left join stab s on s.card_name = c.card_name
cross join l
cross join prev_any
where c.p_new >= p_min_price
  and c.p_old >= p_min_price
  and c.p_new <> c.p_old
  and (prev_any.n = 0 or (s.card_name is not null and s.new_in_s = s.prev_in_s))
order by abs((c.p_new - c.p_old)::numeric / c.p_old) desc
limit p_limit;
$$;
