"""店の一時休止（SUSPENDED_SHOPS）。

店がサイトを止めている間（2026-10-21 カーナベルが PHP の脆弱性で遊戯王サイトを停止し、
新サイトへ移るまで休止）、その店を「買える店」として出さないためのスイッチ。
環境変数 SUSPENDED_SHOPS（カンマ区切りの店名）に入れた店は、次のすべてから外れる。

  - リアルタイム検索・買取検索（scraper.SHOPS / DEFAULT_SHOPS / BUYBACK_SHOPS から外す）
  - 夜間の収集3種（collect_prices / collect_buyback / collect_featured。上の一覧から作るため）
  - DB の読み出し（price_history_reads・相場キャッシュの RPC・新商品の表）
  - 店の検索キャッシュの読み出し・画像の予備（カーナベル画像）

止まった店の裏側の検索サービスがまだ在庫ありのデータを返すことがあるため（カーナベルは
Elasticsearch が 200 で在庫つきの結果を返し続けた）、取得できるかどうかでは判定せず、
人が決めて入れるスイッチにしている。価格の推移グラフ（/api/price-history）は過去の観測の
記録なので外さない。
"""
import os

SUSPENDED_SHOPS: frozenset[str] = frozenset(
    s.strip() for s in os.environ.get("SUSPENDED_SHOPS", "").split(",") if s.strip()
)


if SUSPENDED_SHOPS:
    import logging
    _KNOWN = {"遊々亭", "カードラッシュ", "トレコロCB", "カーナベル", "カードラボ", "まんぞく屋", "駿河屋"}
    logging.getLogger(__name__).warning(f"[shop_status] 休止中の店: {sorted(SUSPENDED_SHOPS)}")
    for _s in SUSPENDED_SHOPS - _KNOWN:
        # 書き間違いは黙って効かなくなるので知らせる
        logging.getLogger(__name__).warning(f"[shop_status] SUSPENDED_SHOPS に知らない店名: {_s}")


def is_suspended(shop: str | None) -> bool:
    return bool(shop) and shop in SUSPENDED_SHOPS


def drop_suspended_rows(rows: list[dict], key: str = "shop") -> list[dict]:
    """休止中の店の行を除く。key 列が無い行は残す（店の分からない行を勝手に消さない）。"""
    if not SUSPENDED_SHOPS:
        return rows
    return [r for r in rows if not is_suspended(r.get(key))]
