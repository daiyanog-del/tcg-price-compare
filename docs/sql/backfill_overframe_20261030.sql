-- 遊々亭・カードラボの OF プリシクを「プリシク」で記録していた過去の行を「OFプリシク」に書き換える（2026-10-30）
--
-- 背景: 2店は OF をレアリティ欄に書かないため、同じ商品がほかの店の「OFプリシク」と別の札になっていた。
-- 取り込み側は overframe.py で直した（同日の PR）。ここでは過去の記録をそろえる。
--
-- 対象の決め方（取り込み側の infer_overframe と同じ考え方）:
--   型番 A = price_history_v2 で、2店以外のどこかが OFプリシク と書き、2店以外のどこも プリシク と書いていない (card_id, code)
--   - price_history_v2 : 2店の プリシク の行のうち (card_id, code) が A のもの
--   - 型番の無い表（price_daily_min・buyback_history）: A のカードのうち、A 以外の型番で プリシク が1行も無いカードだけ
--     （同じカードに普通のプリシクの別の版があると、型番の無い行は見分けられないので触らない）
-- 書き換えの決まり（10-30 の漫画風と同じ）: 同じキーにほかの行があれば1行に。日ごとの最安は安い方の店・同額なら両方、
-- 買取は高い方。元の行は _bak_of_20261030_* に退避（RLS 有効。v2_of はまとめる相手の OFプリシク の行）。
-- 番号: rarity_dim 21=OFプリシク / 46=プリシク、shop_dim 1=遊々亭 / 2=カードラボ（名前で引いて確かめる）。
-- price_history_art・price_daily_art・buyback_art は該当0件（10-30 確認）なので触らない。
--
-- ⚠ Supabase MCP の execute_sql は begin〜commit つきの文にエラーを返しても裏で確定していることがある。
--   エラーが出たら流し直す前に少し待って、退避表の有無と件数を確かめる（退避表の名前がぶつかれば全体が取り消される）。

begin;

do $$
begin
  if (select rarity_id from rarity_dim where rarity = 'OFプリシク') <> 21
     or (select rarity_id from rarity_dim where rarity = 'プリシク') <> 46
     or (select shop_id from shop_dim where shop = '遊々亭') <> 1
     or (select shop_id from shop_dim where shop = 'カードラボ') <> 2 then
    raise exception '番号が想定と違う';
  end if;
end $$;

create temp table of_codes on commit drop as
select card_id, code from price_history_v2
where code <> '' and rarity_id in (21, 46)
group by 1, 2
having bool_or(rarity_id = 21 and shop_id not in (1, 2))
   and not bool_or(rarity_id = 46 and shop_id not in (1, 2));

create temp table of_cards on commit drop as
select distinct a.card_id from of_codes a
where not exists (
  select 1 from price_history_v2 p
  where p.card_id = a.card_id and p.rarity_id = 46
    and not exists (select 1 from of_codes b where b.card_id = p.card_id and b.code = p.code));

-- 退避（書き換える行と、まとめる相手の行）
create table _bak_of_20261030_v2 as
select p.* from price_history_v2 p join of_codes a using (card_id, code)
where p.rarity_id = 46 and p.shop_id in (1, 2);

create table _bak_of_20261030_daily as
select d.* from price_daily_min d join of_cards using (card_id)
where d.rarity_id in (21, 46);

create table _bak_of_20261030_buyback as
select b.* from buyback_history b join card_dim c on c.card_name = b.card_name join of_cards using (card_id)
where b.shop in ('遊々亭', 'カードラボ') and b.rarity in ('プリシク', 'OFプリシク');

alter table _bak_of_20261030_v2 enable row level security;
alter table _bak_of_20261030_daily enable row level security;
alter table _bak_of_20261030_buyback enable row level security;

-- 1) price_history_v2: 同じ (card, shop, OFプリシク, 日) の行があれば1行にまとめ、無ければ番号だけ変える。
--    取り込みの直し（PR #288）の反映後は、検索の即時記録がその日の OFプリシク の行を書くので、反映した日に
--    同じキーの行ができる。まとめ方は price_persist と同じ: min_price は安い方・code は min_price を決めた行、
--    min_price_any は安い方・url は min_price_any を決めた行（同額なら OFプリシク の行のまま）
create table _bak_of_20261030_v2_of as
select q.* from price_history_v2 q join _bak_of_20261030_v2 p
  on q.card_id = p.card_id and q.shop_id = p.shop_id and q.recorded_at = p.recorded_at
where q.rarity_id = 21;
alter table _bak_of_20261030_v2_of enable row level security;

update price_history_v2 o set
  min_price = least(o.min_price, p.min_price),
  code = case when p.min_price < o.min_price then p.code else o.code end,
  min_price_any = least(o.min_price_any, p.min_price_any),
  url = case when p.min_price_any < o.min_price_any then p.url else o.url end
from _bak_of_20261030_v2 p
where o.rarity_id = 21 and o.card_id = p.card_id and o.shop_id = p.shop_id and o.recorded_at = p.recorded_at;

delete from price_history_v2 d
using _bak_of_20261030_v2_of o
where d.rarity_id = 46 and d.card_id = o.card_id and d.shop_id = o.shop_id and d.recorded_at = o.recorded_at;

update price_history_v2 p set rarity_id = 21
from _bak_of_20261030_v2 b
where p.card_id = b.card_id and p.shop_id = b.shop_id and p.recorded_at = b.recorded_at and p.rarity_id = 46;

-- 2) price_daily_min: OFプリシク の行がある日はまとめ、無い日は番号を変える
update price_daily_min o set
  min_price = least(o.min_price, p.min_price),
  min_shop_ids = case
    when p.min_price < o.min_price then p.min_shop_ids
    when p.min_price > o.min_price then o.min_shop_ids
    else (select array_agg(distinct s order by s) from unnest(o.min_shop_ids || p.min_shop_ids) s)
  end,
  min_price_over10 = least(o.min_price_over10, p.min_price_over10)
from _bak_of_20261030_daily p
where p.rarity_id = 46 and o.rarity_id = 21 and o.card_id = p.card_id and o.recorded_at = p.recorded_at;

delete from price_daily_min d
using _bak_of_20261030_daily p
where p.rarity_id = 46 and d.rarity_id = 46 and d.card_id = p.card_id and d.recorded_at = p.recorded_at
  and exists (select 1 from _bak_of_20261030_daily o
              where o.rarity_id = 21 and o.card_id = p.card_id and o.recorded_at = p.recorded_at);

update price_daily_min d set rarity_id = 21
from _bak_of_20261030_daily p
where p.rarity_id = 46 and d.rarity_id = 46 and d.card_id = p.card_id and d.recorded_at = p.recorded_at;

-- 3) buyback_history: 同じ (card, shop, OFプリシク, 日) があれば高い方を残し、無ければ名前を変える
update buyback_history o set max_price = greatest(o.max_price, p.max_price)
from _bak_of_20261030_buyback p
where p.rarity = 'プリシク' and o.rarity = 'OFプリシク'
  and o.card_name = p.card_name and o.shop = p.shop and o.recorded_at = p.recorded_at;

delete from buyback_history d
using _bak_of_20261030_buyback p
where p.rarity = 'プリシク' and d.id = p.id
  and exists (select 1 from _bak_of_20261030_buyback o
              where o.rarity = 'OFプリシク' and o.card_name = p.card_name and o.shop = p.shop and o.recorded_at = p.recorded_at);

update buyback_history d set rarity = 'OFプリシク'
from _bak_of_20261030_buyback p
where p.rarity = 'プリシク' and d.id = p.id;

commit;

-- 確かめ（流したあと）:
-- select count(*) from _bak_of_20261030_v2;                                   -- 書き換えた販売の行
-- select count(*) from price_history_v2 p join _bak_of_20261030_v2 b using (card_id, shop_id, recorded_at) where p.rarity_id = 46;  -- 0
-- select count(*) from price_daily_min d join (select distinct card_id from _bak_of_20261030_daily) c using (card_id) where d.rarity_id = 46;  -- 0
-- select count(*) from buyback_history b join _bak_of_20261030_buyback k using (id) where b.rarity = 'プリシク';  -- 0
