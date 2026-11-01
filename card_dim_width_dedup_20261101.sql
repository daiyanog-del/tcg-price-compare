-- 名簿（card_dim）の全角・半角の2行のまとめ（2026-11-01）
-- 経緯: card_dim に全角英数字の名前が96行あり、うち51組は半角の名前の行と2つに分かれていた（初期の名簿作成時の残り）。
--   51組のうち、価格の記録が両方にあるのは3組だけで、そのどれも古い側（半角）は8〜9月の日ごとの最安（price_daily_min）だけ。
--   残りの片側は記録が1行も無い空の行。読み手は名前の完全一致を優先するので、空の行に当たると「記録なし」になりうる。
-- やること:
--   1) 3組（Case of K9 / K9－17号 “Ripper” / K9－17号 イヅナ）の古い日ごとの最安を正式名の側へ寄せる
--      （同じレアリティ・日付が両方にあれば安いほうを残す）
--   2) どの表からも参照されない、正式名でない名前の行（51行）と、1) で空になった3行を消す
-- 消す前の行は _bak_dimdup_20261101_* に写す（RLS を有効にして公開の鍵からは読めないようにする）。
-- 正式名は data/cardnames_ja.json と照合済み（消す51行はどれも正式名ではない）。
-- 適用順: 保存名をそろえるコード（app._persist_card_name）を本番に出してから流す（旧コードが全角の名前で書き足すと件数がずれる）。
-- 54行ちょうど消せなければ例外で全体を戻す。
begin;

create table _bak_dimdup_20261101_dim as
  select * from card_dim where card_id in (1,153,156,
    4,6,8,25,86,88,90,102,110,118,120,134,139,141,147,151,158,161,179,180,182,184,194,207,216,220,234,251,257,260,262,
    268,270,276,278,280,320,322,324,348,442,960,1070,1227,1229,1697,2078,2153,2694,2696,2906);
create table _bak_dimdup_20261101_pdm as
  select * from price_daily_min where card_id in (1,2,153,154,156,157);
alter table _bak_dimdup_20261101_dim enable row level security;
alter table _bak_dimdup_20261101_pdm enable row level security;

with m(old_id, new_id) as (values (1,2),(153,154),(156,157))
update price_daily_min n
   set min_price = least(n.min_price, o.min_price),
       min_price_over10 = least(n.min_price_over10, o.min_price_over10),
       min_shop_ids = case when o.min_price < n.min_price then o.min_shop_ids
                           when o.min_price = n.min_price then
                             (select array_agg(distinct x order by x) from unnest(n.min_shop_ids || o.min_shop_ids) x)
                           else n.min_shop_ids end
  from price_daily_min o, m
 where o.card_id = m.old_id and n.card_id = m.new_id
   and n.rarity_id = o.rarity_id and n.recorded_at = o.recorded_at;

with m(old_id, new_id) as (values (1,2),(153,154),(156,157))
insert into price_daily_min (card_id, rarity_id, recorded_at, min_price, min_shop_ids, min_price_over10)
select m.new_id, o.rarity_id, o.recorded_at, o.min_price, o.min_shop_ids, o.min_price_over10
  from price_daily_min o join m on o.card_id = m.old_id
 where not exists (select 1 from price_daily_min n
                    where n.card_id = m.new_id and n.rarity_id = o.rarity_id and n.recorded_at = o.recorded_at);

delete from price_daily_min where card_id in (1,153,156);

-- 参照の無い行だけ消し、残った行があれば（＝54行ちょうど消せなければ）全体を戻す
delete from card_dim c
 where c.card_id in (select card_id from _bak_dimdup_20261101_dim)
   and not exists (select 1 from price_history_v2 x where x.card_id = c.card_id)
   and not exists (select 1 from price_daily_min x where x.card_id = c.card_id)
   and not exists (select 1 from price_history_art x where x.card_id = c.card_id)
   and not exists (select 1 from price_daily_art x where x.card_id = c.card_id)
   and not exists (select 1 from buyback_art x where x.card_id = c.card_id);
do $$ begin
  if (select count(*) from _bak_dimdup_20261101_dim) <> 54 then
    raise exception '写した名簿の行が54行ではありません。全体を戻します';
  end if;
  if (select count(*) from card_dim where card_id in (select card_id from _bak_dimdup_20261101_dim)) <> 0 then
    raise exception '消せなかった名簿の行が残っています（参照あり）。全体を戻します';
  end if;
end $$;

commit;
