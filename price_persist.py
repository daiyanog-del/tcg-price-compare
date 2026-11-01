"""価格履歴 (price_history) への書き込みを集約するモジュール。

collect_prices.py（定期収集）と app.py（検索・見積もりの即時反映）の両方から
呼ばれる共通ヘルパ。書き込み経路を 1 か所に集約することで、スキーマ変更や
冪等性ロジックの修正を 1 か所で完結させる。

前提（2026-09-02 整数キー化。price_history_v2.sql 参照）:
- 正本テーブルは price_history_v2 で、主キーは (card_id, shop_id, rarity_id, recorded_at)
  （card_name/shop/rarity は card_dim/shop_dim/rarity_dim の整数IDに正規化済み）
- price_history は従来の列名で読める同名の「ビュー」。読み取り側は無変更
- 書き込みは RPC upsert_price_rows(p_rows jsonb, p_ignore_duplicates boolean) に一本化。
  card_name/shop/rarity → card_id/shop_id/rarity_id の名前解決と辞書表への追加は
  RPC 側（SQL）で行うため、Python 側は従来どおり文字列キーの行を渡すだけでよい
- フェーズ3 P4/P2（docs/design-phase3-generation-side.md）: min_price_any 列（状態を
  問わない本当の最安値）・code 列（min_price を決めた出品の型番）を追加済み
  （price_history_phase3_columns.sql）
"""
from __future__ import annotations
import logging

import art_variant
from db_write_guard import is_db_unavailable
from rarity import UNKNOWN_RARITY_LABEL
from constants import PRICE_FLOOR

logger = logging.getLogger(__name__)


# フェーズ3 P4（状態2値記録）: 店舗別の「通常品」判定テーブル。
# 通常品 = キズあり・状態難などの割引条件がついていない出品で、変動検出・グラフ用の
# min_price 系列を構成する。判定基準は docs/design-phase3-generation-side.md
# P4-B の推奨に従う:
#   - condition が "-"（無条件）の出品は常に通常品
#   - カーナベル「状態:S」「状態:A」= 傷なし〜目立たない傷のため通常品として扱う（B/C/D は除外）。
#     旧サイト（〜2026-10-21）の「状態:SA」は新サイト（2026-10-24〜）で S と A に分かれた
#   - 遊々亭「セール」= 実在の販売価格であり本物の値下げのため通常品に含める
#   - トレコロCBの「中古キズあり」（正価×0.8 SKU）は通常品から除外
#   - カードラッシュの〔状態…〕表記は状態難の出品として通常品から除外（デフォルトの
#     除外扱いに従う。個別の例外登録はしない）
_NORMAL_CONDITION_EXCEPTIONS = {
    ("カーナベル", "状態:S"),
    ("カーナベル", "状態:A"),
    ("遊々亭", "セール"),
}


def _is_normal_condition(shop: str, condition: str) -> bool:
    """(shop, condition) が「通常品」（状態を揃えた変動検出用系列の対象）かどうかを判定する。"""
    cond = condition or "-"
    if cond == "-":
        return True
    return (shop, cond) in _NORMAL_CONDITION_EXCEPTIONS


def build_min_price_rows(card_name: str, scrape_results: list, today: str) -> list[dict]:
    """スクレイピング結果から price_history への書き込み行を組み立てる。

    (shop, rarity) ごとに2種類の最安値を並行集計する（フェーズ3 P4）:
      - min_price     : 状態を揃えた「通常品」の最安値（変動検出・グラフ用）
      - min_price_any : 状態を問わない本当の最安値（表示・簡易見積り用）
    通常品の出品が1件もない (shop, rarity) では、min_price は min_price_any に
    フォールバックする（行自体を欠測させない。表示すべき最安値がある限り欠測は
    偽の空白日を生むため、より安全側に倒す実装判断）。

    code（フェーズ3 P2）は min_price を決めた出品の型番を属性として添える
    （通常品が無く min_price_any にフォールバックした場合はその出品の型番）。
    店舗横断のキーとしては使えない（店舗ごとに表記形式が異なる）ため、あくまで
    「その日その店のそのレアリティの最安は、どの収録だったか」の監査情報。

    url は code と役割分担が非対称: 常に min_price_any（本当の最安値）を決めた
    出品のURLを添える（code は min_price 側のまま変えない）。通常品とany品が
    別出品の場合、code と url は別の出品を指すことになる（意図どおり）。
    出品に有効なURLが無ければ None（NULL維持）。

    rarity 抽出失敗行（空文字）は "(不明)" ラベルへ明示し、正規レアリティ系列に
    混ぜない（フェーズ3 P3。除外は読み出し側の代表レアリティ選定で行う）。

    sold_out や PRICE_FLOOR（10円）未満はノイズとして除外する。
    （畳みキー・除外条件は collect_prices.py:156-175 から切り出したロジックを踏襲）
    """
    # key(shop, rarity) -> {"any": (price, code, url), "normal": (price, code) | None}
    best: dict[tuple[str, str], dict] = {}
    for item in scrape_results:
        if item.get("sold_out"):
            continue
        price = item.get("price", 0)
        if price < PRICE_FLOOR:
            continue
        shop = item.get("shop", "")
        if not shop:
            continue
        rarity = item.get("rarity", "") or ""
        if not rarity:
            rarity = UNKNOWN_RARITY_LABEL
        code = item.get("code", "") or ""
        condition = item.get("condition", "") or ""
        url = item.get("url", "") or ""

        key = (shop, rarity)
        entry = best.setdefault(key, {"any": None, "normal": None})

        if entry["any"] is None or price < entry["any"][0]:
            entry["any"] = (price, code, url)

        if _is_normal_condition(shop, condition):
            if entry["normal"] is None or price < entry["normal"][0]:
                entry["normal"] = (price, code)

    rows = []
    for (shop, rarity), entry in best.items():
        any_price, any_code, any_url = entry["any"]
        if entry["normal"] is not None:
            min_price, min_code = entry["normal"]
        else:
            min_price, min_code = any_price, any_code
        rows.append({
            "card_name": card_name, "shop": shop, "rarity": rarity,
            "min_price": min_price, "min_price_any": any_price,
            "code": min_code, "url": any_url or None, "recorded_at": today,
        })
    return rows


def upsert_price_rows(sb, rows: list[dict], ignore_duplicates: bool = False,
                      raise_on_error: bool = False) -> int:
    """price_history_v2 に RPC upsert_price_rows 経由で upsert する。

    主キー (card_id, shop_id, rarity_id, recorded_at) に基づき、既存があれば
    全列上書き（直近の scraper の結果を最新として採用 = last-write-wins）。
    card_name/shop/rarity → 整数ID の解決と辞書表（card_dim/shop_dim/rarity_dim）
    への追加は RPC 側で行うため、ここでは従来どおり文字列キーのまま渡す。

    ignore_duplicates=True の場合は既存行があれば書き込まない（フェーズ3 P6b。
    網羅性の低い経路 /api/deck の即時upsertが、夜間収集の網羅的な最安値を
    同日上書きしないようにするための切替。/api/search 等は既定の False のまま）。

    RPC は `RETURNS TABLE(saved_rows integer)` で、PostgREST 経由では
    `resp.data == [{"saved_rows": N}]` という list[dict] の形で返る（スカラー
    RETURNS だと supabase-py の APIResponse が list 以外を弾いて HTTP 200 なのに
    APIError になるため、レビュー班の指摘でテーブル返却に変更した）。

    戻り値の意味は「渡した行数」から「実際に挿入/更新した行数（saved_rows）」に
    変わった点に注意。ignore_duplicates=True の経路では、渡した行がすべて既存行
    （スキップ対象）だった場合に 0 が返るのが正常値であり、異常ではない。

    resp.data が期待した list[dict] 形でない場合（None・空list・saved_rows キー欠落等）
    は渡した行数にフォールバックし、その旨を warning ログに残す（RPC側の戻り値仕様が
    崩れている可能性があるため debug ではなく warning）。

    saved_rows が渡した行数より小さい場合、RPC 側が必須列 NULL 等で行を除外した
    可能性があるため warning ログを出す。ただし ignore_duplicates=True のときは
    既存行スキップによる正常な減少と区別がつかないため warning は出さない。

    エラー時は 0 を返し、ERROR ログのみ出す（呼び元の処理は止めない）。
    raise_on_error=True のときは ERROR ログを出したうえで例外をそのまま送出する
    （夜間収集が「DBの失敗」と「保存0行」を区別して連続失敗を数えるため。db_write_guard.py）。
    """
    if not rows:
        return 0
    try:
        resp = sb.rpc(
            "upsert_price_rows",
            {"p_rows": rows, "p_ignore_duplicates": ignore_duplicates},
        ).execute()
        data = resp.data
        saved = None
        if isinstance(data, list) and data and isinstance(data[0], dict) and "saved_rows" in data[0]:
            saved = data[0]["saved_rows"]
        if not isinstance(saved, int):
            logger.warning(
                f"[price_persist] upsert_price_rows RPC の戻り値が想定外の形 "
                f"({type(data).__name__}: {data!r})。渡した行数 {len(rows)} にフォールバック"
            )
            return len(rows)
        if saved < len(rows) and not ignore_duplicates:
            logger.warning(
                f"[price_persist] upsert_price_rows: {len(rows)}行中 {saved}行のみ保存"
                "（RPC側で必須列NULL等により除外された可能性）"
            )
        return saved
    except Exception as e:
        # ERRORレベル: ここが無言で失敗し続けると価格履歴が丸ごと止まる
        # （例: RPC未作成のままコードだけデプロイした場合）
        logger.error(f"[price_persist] upsert 失敗 ({len(rows)}行): {e}")
        if raise_on_error:
            raise
        return 0


def shallow_rows_to_write(new_rows: list[dict], existing: list[dict], today: str) -> tuple[list[dict], list[dict]]:
    """浅い取り方（トレコロの1ページ目・カードラボのカード名の検索）の結果のうち、記録に書いてよい行（2026-11-01）。

    浅い結果で確かなのは「この値段で売っている出品がある」ことだけで、「このレアリティの最安」「このレアリティは無い」は
    見落としかもしれない。そこで店ごとに次のとおり書く。
      - その店に当日も前日も記録が無い（発売直後の新しいカードなど）: 全部書く
      - 当日の記録がある: 当日に無いレアリティは足し、あるレアリティは安くなった値だけ下げる（下げない欄は当日の値のまま）
      - 前日の記録だけある（深夜0時〜朝の収集まで）: 書かない。1つでも当日の行を作ると、画面は前日の行を使わなくなり
        （featured_matrix はカード×店で当日を優先）、深い収集でしか見つからないレアリティが朝まで消えるため
    new_rows / existing は build_min_price_rows の形（existing は当日と前日の記録。recorded_at で見分ける）。
    戻り値: (足す行, 下げる行)。足す行は既存の行を上書きしない書き方（ignore_duplicates）で書くこと
    （読んでから書くまでの間に朝の収集が同じ組を書いたら、そちらを残すため。reviewer 中3）
    """
    today_by_shop: dict[str, dict] = {}
    known_shops: set = set()
    for r in existing:
        known_shops.add(r.get("shop"))
        if str(r.get("recorded_at") or "")[:10] == today:
            today_by_shop.setdefault(r.get("shop"), {})[r.get("rarity")] = r
    adds: list[dict] = []
    lowers: list[dict] = []
    for row in new_rows:
        shop = row.get("shop")
        if shop not in known_shops:
            adds.append(row)
            continue
        today_rows = today_by_shop.get(shop)
        if not today_rows:
            continue  # 前日だけ（深夜）
        old = today_rows.get(row.get("rarity"))
        if old is None:
            adds.append(row)
            continue
        merged = {k: old.get(k) for k in row}
        merged.update({"card_name": row["card_name"], "shop": shop, "rarity": row["rarity"], "recorded_at": today})
        lower = False
        if old.get("min_price") is None or row["min_price"] < old["min_price"]:
            merged["min_price"], merged["code"] = row["min_price"], row.get("code")
            lower = True
        if old.get("min_price_any") is None or row["min_price_any"] < old["min_price_any"]:
            merged["min_price_any"], merged["url"] = row["min_price_any"], row.get("url")
            lower = True
        if lower:
            lowers.append(merged)
    return adds, lowers


def upsert_buyback_rows(sb, rows: list[dict], raise_on_error: bool = False) -> int:
    """buyback_history に upsert する。

    UNIQUE(card_name, shop, rarity, recorded_at) 前提。同日の再実行は上書き
    （その日の最高買取額の最新値を採用）。

    成功した行数を返す。エラー時は 0 を返し、警告ログのみ出す（呼び元の処理は止めない）。
    raise_on_error=True のときはログを出したうえで例外を送出する（upsert_price_rows と同じ）。
    """
    if not rows:
        return 0
    try:
        sb.table("buyback_history").upsert(
            rows,
            on_conflict="card_name,shop,rarity,recorded_at",
        ).execute()
        return len(rows)
    except Exception as e:
        logger.error(f"[price_persist] buyback upsert 失敗 ({len(rows)}行): {e}")
        if raise_on_error:
            raise
        return 0


# ── 絵ごとの内訳（price_history_art・buyback_art。2026-10-30 イラスト違いの段2・案B の段2a-1） ──
# 本表（price_history_v2・buyback_history）は今までどおり絵を混ぜた値のまま書き、絵が分かれるカード×レアリティだけ、
# 絵ごとの値をここで別表に書く。本表を「代表の絵」に切り替えるのは段2a-2（別表がたまってから過去の行も書き直す）。
# 書けなくても本表の記録は止めない（ログだけ）。別表の SQL（price_history_art.sql）がまだ無い環境では、
# 1回目の失敗でそのプロセスの書き込みを止める（夜の収集で毎カード同じエラーを出さない）
_art_write_disabled: set[str] = set()


def _annotated_copies(card_name: str, scrape_results: list, context: dict | None = None) -> list[dict]:
    """絵の印を付けた複製（呼び元の行は書き換えない。検索の画面に返す行と同じ dict を共有していることがあるため）。
    annotate は全店の行を見て型番ごとの絵をそろえるので、そのカードの行をまとめて渡す。
    context（art_price_context）は、同じカードのほかの店の行で分かったこと（1店だけの行をあとから判定するとき）"""
    rows = [dict(r) for r in scrape_results]
    art_variant.annotate(rows, card_name, extra_alt_codes=(context or {}).get("alt_codes"))
    return rows


def _split_rarities(rows: list[dict], card_name: str,
                    force_rarities: set[str] | None = None) -> dict[tuple[str, int], str]:
    """(レアリティ, 行の位置) → 記録の絵。絵が分かれるレアリティ（art_variant.needs_art_rows）の行だけ。
    force_rarities のレアリティは、この行だけでは分かれなくても書く（ほかの店の行で分かれると分かっている）"""
    arts_by_rarity: dict[str, set[str]] = {}
    marked: list[tuple[str, int, str]] = []
    for i, row in enumerate(rows):
        rarity = row.get("rarity", "") or UNKNOWN_RARITY_LABEL
        art = art_variant.record_art(row, card_name)
        arts_by_rarity.setdefault(rarity, set()).add(art)
        marked.append((rarity, i, art))
    force = force_rarities or set()
    return {(rarity, i): art for rarity, i, art in marked
            if rarity in force or art_variant.needs_art_rows(arts_by_rarity[rarity])}


def _valid_price_rows(card_name: str, scrape_results: list, context: dict | None = None) -> list[dict]:
    return [r for r in _annotated_copies(card_name, scrape_results, context)
            if not r.get("sold_out") and r.get("price", 0) >= PRICE_FLOOR and r.get("shop")]


def art_price_context(card_name: str, scrape_results: list) -> dict:
    """そのカードの全店の行で分かったこと（絵が分かれるレアリティ・型番ごと違う絵の型番）。夜の収集は
    カードラボだけを別の経路（カタログ巡回）で後から保存するので、カードラボの行の判定をこれでほかの店にそろえる（10-30 reviewer 指摘）"""
    rows = _valid_price_rows(card_name, scrape_results)
    split = _split_rarities(rows, card_name)
    alt_codes = {rows[i].get("code") for (_, i), art in split.items()
                 if art.startswith(art_variant.ART_ALT) and rows[i].get("code")}
    return {"rarities": {rarity for rarity, _ in split}, "alt_codes": alt_codes}


def build_art_price_rows(card_name: str, scrape_results: list, today: str, context: dict | None = None) -> list[dict]:
    """絵が分かれるカード×レアリティについて、店×レアリティ×絵ごとの最安の行（price_history_art 用）。
    最安・code・url の決め方は build_min_price_rows と同じ（絵の欄 art が足されるだけ）。
    絵が分かれるかは売り切れ・下限未満を除いた行で決める（本表に入る行と同じ範囲）"""
    rows = _valid_price_rows(card_name, scrape_results, context)
    split = _split_rarities(rows, card_name, (context or {}).get("rarities"))
    if not split:
        return []
    by_art: dict[str, list[dict]] = {}
    for (_, i), art in split.items():
        by_art.setdefault(art, []).append(rows[i])
    out = []
    for art, items in by_art.items():
        for row in build_min_price_rows(card_name, items, today):
            out.append({**row, "art": art})
    return out


def build_art_buyback_rows(card_name: str, results: list, today: str) -> list[dict]:
    """絵が分かれるカード×レアリティについて、店×レアリティ×絵ごとの最高買取額（buyback_art 用）。
    除外の条件は collect_buyback と同じ（売り切れ・未開封・付属品つき・下限未満）。レアリティは本表と同じく取得時の値のまま
    （空だけ "(不明)"）。同じ日の再実行で、もう出ない絵の行は消さない（買取の記録は夜の1回だけなので受け入れる）"""
    rows = [r for r in _annotated_copies(card_name, results)
            if not r.get("sold_out") and not art_variant.row_is_sealed(r) and r.get("price", 0) >= PRICE_FLOOR]
    split = _split_rarities(rows, card_name)
    best: dict[tuple[str, str, str], int] = {}
    for (_, i), art in split.items():
        row = rows[i]
        # 空のレアリティは販売と同じ "(不明)" に寄せる（辞書 rarity_dim に空の名前を作らない。本表の buyback_history は "" のまま）
        key = (row.get("shop", ""), row.get("rarity", "") or UNKNOWN_RARITY_LABEL, art)
        if key not in best or row["price"] > best[key]:
            best[key] = row["price"]
    return [{"card_name": card_name, "shop": shop, "rarity": rarity, "art": art,
             "max_price": price, "recorded_at": today}
            for (shop, rarity, art), price in best.items()]


def _upsert_art(sb, fn: str, params: dict, n_rows: int) -> int:
    """別表の RPC を呼ぶ。失敗しても例外を出さない（本表の記録を止めない）。RPC が無い・DB が応答しないときは
    そのプロセスでは以後呼ばない。夜の収集の見張り付きクライアント（db_write_guard.GuardedSupabase）は通さない
    （別表の失敗を数えて収集ごと打ち切ったり、1枚ごとに待ったりしないため。10-30 reviewer 指摘）"""
    if fn in _art_write_disabled:
        return 0
    sb = getattr(sb, "raw", sb)
    try:
        resp = sb.rpc(fn, params).execute()
        data = resp.data
        if isinstance(data, list) and data and isinstance(data[0], dict) and isinstance(data[0].get("saved_rows"), int):
            return data[0]["saved_rows"]
        return n_rows
    except Exception as e:
        text = str(e)
        if "PGRST202" in text or "Could not find the function" in text:
            _art_write_disabled.add(fn)
            logger.warning(f"[price_persist] {fn} が DB に無い（price_history_art.sql 未適用）。このプロセスでは絵ごとの記録を止める")
        elif is_db_unavailable(e):
            # DB が重い・止まっているときに小さい別表の書き込みを重ねない（本表の記録を優先する）
            _art_write_disabled.add(fn)
            logger.warning(f"[price_persist] {fn}: DB が応答しない（{e}）。このプロセスでは絵ごとの記録を止める")
        else:
            logger.warning(f"[price_persist] {fn} 失敗 ({n_rows}行・本表の記録は続ける): {e}")
        return 0


def upsert_price_art_rows(sb, rows: list[dict], ignore_duplicates: bool = False) -> int:
    """price_history_art に RPC upsert_price_art_rows で書く。ignore_duplicates の意味は upsert_price_rows と同じ"""
    if not rows:
        return 0
    return _upsert_art(sb, "upsert_price_art_rows", {"p_rows": rows, "p_ignore_duplicates": ignore_duplicates}, len(rows))


def upsert_buyback_art_rows(sb, rows: list[dict]) -> int:
    """buyback_art に RPC upsert_buyback_art_rows で書く（同じ日の再実行は上書き）"""
    if not rows:
        return 0
    return _upsert_art(sb, "upsert_buyback_art_rows", {"p_rows": rows}, len(rows))
