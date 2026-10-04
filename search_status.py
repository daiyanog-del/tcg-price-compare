"""価格検索の取得範囲を結果件数と分離して表現する。"""


def search_coverage(selected, successful, results):
    """正常0件、部分失敗、全失敗を同じ完了イベントに潰さない。"""
    shops = list(dict.fromkeys(selected))
    successful = set(successful)
    failed = [shop for shop in shops if shop not in successful]
    status = "partial" if failed else ("ok" if results else "empty")
    if failed and not successful and not results:
        status = "failed"
    return {
        "status": status,
        "successful_shops": [shop for shop in shops if shop in successful],
        "failed_shops": failed,
        "shop_count": len(shops),
    }
