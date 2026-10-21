-- 汎用カードの列（トップのランキング3列目・2026-10-21）用 RPC
-- 本番へは MCP 経由で適用（マイグレーション名: create_get_staple_movers）
--
-- 汎用カード（staple_cards）全件について、代表レアリティの最安値と p_days_back 日前からの変化を返す。
-- 集計の規約は get_top_movers（supabase_rpc_top_movers.sql）と同じ:
--   代表レアリティ = 当日に存在するレアリティのうち最安（'(不明)'・'' は除外）
--   変化は「当日と N 日前の両方に記録がある店舗（共通店舗）」の最安どうしで比べる
--   定着チェック = 共通店舗のうち前日もある店で、当日の最安が前日と同じか（stable）
-- get_top_movers と違い、変化なし・比較できないカードも返す（並べ替えはアプリ側）。
--   price_now      : 代表レアリティの当日の最安（全店）＝表示する値段
--   price_old/new  : 共通店舗どうしの最安（無ければ null）＝変化の計算に使う
create or replace function get_staple_movers(
  p_days_back      int default 7,
  p_stability_days int default 1
)
returns table (
  card_name   text,
  themes      int,
  rarity      text,
  price_now   int,
  price_old   int,
  price_new   int,
  stable      boolean,
  date_new    date,
  date_old    date
)
language plpgsql
stable
-- staple_cards は RLS（ポリシーなし）で公開APIから直接は読ませない。サイト（anon）からは
-- この関数を通してだけ読めるよう SECURITY DEFINER にする（search_path は固定）
security definer
set search_path = public
as $$
declare
  v_new  date;
  v_old  date;
  v_prev date;
begin
  select max(ph.recorded_at) into v_new from price_history_v2 ph;
  if v_new is null then
    return;
  end if;
  v_old  := v_new - p_days_back;
  v_prev := v_new - p_stability_days;

  return query
  with s as (select sc.card_name as cn, sc.themes as th from staple_cards sc),
  base as (
    select ph.card_name as cn, ph.shop as sh, ph.rarity as rr,
           min(ph.min_price) filter (where ph.recorded_at = v_new)  as np,
           min(ph.min_price) filter (where ph.recorded_at = v_old)  as op,
           min(ph.min_price) filter (where ph.recorded_at = v_prev) as pp
    from price_history ph
    join s on s.cn = ph.card_name
    where ph.recorded_at in (v_new, v_old, v_prev)
      and ph.min_price is not null
      and ph.min_price > 10
    group by 1, 2, 3
  ),
  rep as (
    select distinct on (b.cn) b.cn, b.rr, b.np
    from base b
    where b.np is not null and b.rr is not null and b.rr not in ('(不明)', '')
    order by b.cn, b.np asc
  ),
  agg as (
    select b.cn,
           min(b.np) filter (where b.op is not null) as p_new,
           min(b.op) filter (where b.np is not null) as p_old,
           min(b.np) filter (where b.op is not null and b.pp is not null) as new_in_s,
           min(b.pp) filter (where b.op is not null and b.pp is not null and b.np is not null) as prev_in_s
    from base b
    join rep r on r.cn = b.cn and r.rr = b.rr
    group by 1
  )
  select s.cn, s.th, r.rr, r.np, a.p_old, a.p_new,
         (a.new_in_s is null or a.new_in_s = a.prev_in_s),
         v_new, v_old
  from s
  join rep r on r.cn = s.cn
  left join agg a on a.cn = s.cn;
end;
$$;

revoke all on function get_staple_movers(int, int) from public;
grant execute on function get_staple_movers(int, int) to anon, authenticated, service_role;
