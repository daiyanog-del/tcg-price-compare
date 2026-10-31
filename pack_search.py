"""
新弾の収録カードを「型番の頭」で店ごとにまとめて取る（2026-10-31）
=================================================================

新弾の収集（collect_featured.py）は、収録カードを1枚ずつ店で検索していた（80種×店の数だけ取りに行く）。
多くの店は検索欄に型番の頭（例: "IMPH"）を入れると、そのパックの商品が数ページに並ぶ。
今のスクレイパーはカード名での絞り込み（scraper.is_target_card）を外せば同じページをそのまま読めるので、
取れた商品を収録カードごとに振り分けて使う。

2026-10-31 の実測（IMMORTAL PHOENIX、収録80種）:
  遊々亭      1ページ・166件で79種（1.4秒）
  カードラッシュ 273件・95の型番（4.9秒。ページ送りは今のスクレイパーのまま）
  まんぞく屋   1ページ目73種＋2ページ目20種（100件/ページ・安い順）
  トレコロ    対象外。ノーマルは版ごとではなくカードパスワードごとの共通の商品（URL が /g/g{8桁}/）で、型番の頭で探しても
              ほとんど出ない（2026-10-31 監査で確認・10/30 のトレコロのノーマル1,433行中1,432行は型番が空）。型番で取れたカードの
              ノーマルの行が日中に更新されなくなるので入れない
  カードラボ   型番では当たらない → 対象外（夜の収集はカタログ巡回）
  カーナベル   ES の型番の項目で引ける見込みだが未確認 → 対象外

使い方の決まり（collect_featured.py 側）:
  - 再録カード（発売前からほかのパックの版が売られている）は、型番で探すとこのパックの版しか出ず、
    安い昔の版を取りこぼす。再録はこれまでどおりカード名で探す
  - 店ごとに取得に失敗したら（fetch_errors>0）、その店は全カードをカード名で探す
  - 型番で探して見つからなかったカード（店に無い・名前の表記が違う・ページの上限）は、その店だけカード名で探す
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from functools import partial

import scraper

# 1パックで読むページの上限（無限ループの防止）。カードラッシュ 100件/ページ・まんぞく屋 100件/ページで、
# 80種のパックが 273件・2ページだった。再録の多い大きなパックでも足りるよう余裕を持たせる
PACK_MAX_PAGES = 10  # TODO: calibrate from data（大きなパックで最後のページまで届いたかを収集のログで見る）

# 型番の頭で探す店。値はその店のスクレイパー（引数は検索語だけ）
PACK_SEARCH_SHOPS = {
    "遊々亭": scraper.scrape_yuyu,
    "カードラッシュ": partial(scraper.scrape_cardrush, max_pages=PACK_MAX_PAGES),
    "まんぞく屋": partial(scraper.scrape_manzoku, max_pages=PACK_MAX_PAGES),
}

# ページ送りのある店の1ページの件数。読んだ件数が「(上限ページ−1)×この数」を超えたら、最後のページまで読んで
# 打ち切られたかもしれないので、その店は失敗扱い（全カードをカード名で探す）にする（2026-10-31 reviewer 中2。
# 件数はスクレイパーが BOX・サプライ・価格なしを除いた後なので、上限ページ×件数ちょうどには届かない。
# 打ち切りで高くなった値は collect_featured.drop_higher_than_today でも外れる）
PACK_PAGE_SIZES = {"カードラッシュ": scraper.CARDRUSH_PAGE_SIZE, "まんぞく屋": 100}

_CODE_HEAD_RE = re.compile(r"^([A-Z0-9]{2,5})-JP", re.IGNORECASE)


def pack_code_head(numbers: dict[str, str]) -> str | None:
    """{カード名: 型番}（pack_scraper.fetch_pack_card_numbers）から、パックの型番の頭（例 "IMPH"）を返す。

    いちばん多い頭を採る。収録カードの半分に届かなければ、パックの型番が決まらないとみなして None
    （再録ばかりのパック・Wiki の書き方の違いで、別のパックの型番を拾わないように）。"""
    heads = Counter()
    for code in numbers.values():
        m = _CODE_HEAD_RE.match((code or "").strip())
        if m:
            heads[m.group(1).upper()] += 1
    if not heads:
        return None
    head, n = heads.most_common(1)[0]
    return head if n * 2 >= len(numbers) else None


def _in_pack(code: str, head: str) -> bool:
    """商品の型番がこのパックのものか（"IMPH-JP001"・"IMPHJP001"・店の接尾辞つきも許す）。"""
    return bool(re.match(rf"{re.escape(head)}-?JP", (code or "").strip(), re.IGNORECASE))


@dataclass
class PackShopResult:
    shop: str
    by_card: dict[str, list[dict]] = field(default_factory=dict)  # 収録カード名 → その店の商品
    failed: bool = False       # 取得に失敗した（この店は全カードをカード名で探す）
    items: int = 0             # 取れた商品の数（このパックの型番のもの）
    unassigned: int = 0        # このパックの型番だが収録カードのどれにも当たらなかった商品（再録など収録リストに無いもの）


def assign_to_cards(items: list[dict], card_names: list[str]) -> tuple[dict[str, list[dict]], int]:
    """商品を収録カードに振り分ける。商品名に当たる収録カード名が複数あれば、いちばん長い名前を採る
    （「秋」と「秋の○○」のような短い名前に長い名前の商品を取られないように）。戻り値: (by_card, 当たらなかった数)"""
    by_card: dict[str, list[dict]] = {}
    unassigned = 0
    for it in items:
        name = it.get("name") or ""
        hits = [n for n in card_names if scraper.is_target_card(n, name)]
        if not hits:
            unassigned += 1
            continue
        by_card.setdefault(max(hits, key=len), []).append(it)
    return by_card, unassigned


def search_pack_shop(shop: str, head: str, card_names: list[str], fn=None) -> PackShopResult:
    """1店で型番の頭（例 "IMPH"）を検索し、収録カードごとに振り分ける。

    検索語は頭だけにする。"IMPH-JP" はスクレイパーの検索語の整え方でハイフンが空白になり「IMPH JP」の2語になる。
    カードラッシュでは「JP」がほぼ全商品に当たって重く、2026-10-31 に3回中2回が15秒で時間切れになった
    （頭だけなら2.5秒で同じ273件。遊々亭・まんぞく屋も件数は同じ）。"""
    fn = fn or PACK_SEARCH_SHOPS[shop]
    try:
        with scraper.name_filter_off():
            items, fetch_errors = scraper.run_shop_with_status(fn, head)
    except Exception as e:
        print(f"  [型番検索] {shop}: 失敗 {e}")
        return PackShopResult(shop, failed=True)
    if fetch_errors:
        # 途中のページで失敗すると、取れた分だけで最安を決めてしまう。この店は全カードをカード名で探す
        print(f"  [型番検索] {shop}: 取得の失敗 {fetch_errors}件 → この店はカード名で探す")
        return PackShopResult(shop, failed=True)
    page_size = PACK_PAGE_SIZES.get(shop)
    if page_size and len(items) > (PACK_MAX_PAGES - 1) * page_size:
        print(f"  [型番検索] {shop}: {len(items)}件で上限の{PACK_MAX_PAGES}ページに届いた → この店はカード名で探す")
        return PackShopResult(shop, failed=True)
    pack_items = [it for it in items if _in_pack(it.get("code", ""), head)]
    # 型番が読めなかった商品は捨てる。収録カードの名前に当たるものがあれば数だけ残す（カード名の検索なら拾える出品。
    # そのカードの値が今日の行より高くなれば、collect_featured の上書きしない確認でカード名の検索に回る）
    no_code = sum(1 for it in items if not (it.get("code") or "").strip()
                  and any(scraper.is_target_card(n, it.get("name") or "") for n in card_names))
    by_card, unassigned = assign_to_cards(pack_items, card_names)
    print(f"  [型番検索] {shop}: {len(pack_items)}件 → 収録{len(card_names)}種中 {len(by_card)}種"
          f"（どのカードにも当たらない商品 {unassigned}件・型番の読めない収録カードの商品 {no_code}件）")
    return PackShopResult(shop, by_card=by_card, items=len(pack_items), unassigned=unassigned)
