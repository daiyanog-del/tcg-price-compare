"""
tests/test_clabo_catalog.py — カードラボ カタログ巡回（P1・403対策）の回帰テスト

背景:
  docs/audit-403-plan-2026-08-08.md・docs/decisions.md 2026-08-07 F10。
  夜間収集はカードラボだけ「カード単位の検索」をやめ、遊戯王カテゴリ14個の
  カタログページを巡回して全商品を取得し、ローカルで監視カードに名寄せする。

テスト方針:
  - safe_get をモックし、ネットワークに一切出ない状態で検証する。
  - 終端判定・ページ数算出は tests/test_pagination.py の scrape_clabo テストと
    同じ流儀（item_count 第一・ページャはフォールバック）で、HTML組み立ての
    ヘルパも同ファイルから再利用する。
  - 二段名寄せ（build_match_index + match_products）は「直接 is_target_card を
    呼んだ結果」との等価性を、最低1,000組の商品×カードの組合せで確認する。
"""

import re
import sys
from collections import defaultdict
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scraper
import clabo_catalog
from clabo_catalog import (
    CLABO_CATEGORIES,
    build_match_index,
    check_category_drift,
    collect_clabo_catalog,
    crawl_catalog,
    harvest_categories,
    match_products,
)
from tests.test_pagination import _clabo_item_html, _clabo_page_html


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """ページ間ウェイトでテストを待たせない"""
    monkeypatch.setattr(clabo_catalog.time, "sleep", lambda *_a, **_k: None)


# ── カタログ巡回（crawl_catalog） ──

def _install_catalog_pages(monkeypatch, pages: dict, calls: list):
    """(カテゴリID, ページ番号) -> HTML（または None＝取得失敗）の辞書で safe_get を差し替える"""
    def fake_get(url, timeout=15, retries=1):
        calls.append(url)
        m = re.search(r"/product-list/([^?]+)\?num=\d+(?:&page=(\d+))?", url)
        cid = m.group(1)
        page = int(m.group(2)) if m.group(2) else 1
        html = pages.get((cid, page))
        if html is None:
            return None
        return BeautifulSoup(html, "html.parser")

    monkeypatch.setattr(scraper, "safe_get", fake_get)


class TestCrawlCatalog:
    """終端判定の再設計（reviewer高1/高2/低1）:
    break条件は「120件未満」「重複検出」「安全上限MAX_PAGES」の3つのみ。
    item_count（.item_count）は検証専用（巡回後に categories_failed へ計上するだけで、
    break条件としては使わない）。
    """

    def test_num_120_is_appended_to_every_category_url(self, monkeypatch):
        calls = []
        monkeypatch.setattr(clabo_catalog, "CLABO_CATEGORIES", {"111": "テスト"})
        _install_catalog_pages(monkeypatch, {("111", 1): _clabo_page_html(range(1, 11), total=10)}, calls)

        crawl_catalog()

        assert calls == ["https://www.c-labo-online.jp/product-list/111?num=120"]

    def test_retries_2_is_passed_to_safe_get(self, monkeypatch):
        """高4(a): 1ページの一時失敗で全カタログ破棄→フォールバックになるのを減らすため retries=2"""
        captured = []

        def fake_get(url, timeout=15, retries=1):
            captured.append(retries)
            return None

        monkeypatch.setattr(scraper, "safe_get", fake_get)
        monkeypatch.setattr(clabo_catalog, "CLABO_CATEGORIES", {"111": "A"})

        crawl_catalog()

        assert captured == [2]

    def test_collects_full_pages_until_under_120_and_records_per_category_stats(self, monkeypatch):
        """120件未満になるまで（item_countに関係なく）取得し、カテゴリ別統計を残す"""
        calls = []
        monkeypatch.setattr(clabo_catalog, "CLABO_CATEGORIES", {"111": "A", "222": "B"})
        _install_catalog_pages(monkeypatch, {
            ("111", 1): _clabo_page_html(range(1, 121), total=150),
            ("111", 2): _clabo_page_html(range(121, 151), total=150),
            ("222", 1): _clabo_page_html(range(1, 31), total=30),
        }, calls)

        products, stats = crawl_catalog()

        assert len(products) == 150 + 30
        assert len(calls) == 3
        assert stats["categories_total"] == 2
        assert stats["categories_failed"] == []
        assert stats["failed_pages"] == 0
        assert stats["pages_fetched"] == 3
        assert stats["products"] == 180
        assert stats["categories"]["111"] == {"total": 150, "seen": 150, "pages": 2, "ok": True, "verified": True}
        assert stats["categories"]["222"] == {"total": 30, "seen": 30, "pages": 1, "ok": True, "verified": True}
        assert stats["categories_unverified"] == []

    def test_ignores_item_count_for_break_decision_and_keeps_paging(self, monkeypatch):
        """item_count（total）が『1ページで足りる』と過小申告していても、実際に120件
        フルなら次ページを取りに行く（旧実装のバグ＝break条件がitem_count依存だった
        ことの再発防止。高1/高2の核心）。実際の取得数が申告数を上回る方向の乖離は
        「欠落」ではないためカテゴリ失敗にはしない（検証は under-collection のみ検出する）"""
        calls = []
        monkeypatch.setattr(clabo_catalog, "CLABO_CATEGORIES", {"111": "A"})
        _install_catalog_pages(monkeypatch, {
            ("111", 1): _clabo_page_html(range(1, 121), total=50),   # item_countは50と偽っている
            ("111", 2): _clabo_page_html(range(121, 151), total=50),
        }, calls)

        products, stats = crawl_catalog()

        assert len(calls) == 2               # item_countの過小申告を無視して2ページ目も取得
        assert len(products) == 150
        assert stats["categories_failed"] == []   # 実際の取得(150)が申告(50)を上回るのでokのまま
        assert stats["categories"]["111"]["seen"] == 150

    def test_item_count_mismatch_marks_category_failed_without_truncating_crawl(self, monkeypatch):
        """item_count が実際より過大でも、巡回自体は120件未満で正常に終わり取得分は残る。
        事後の突合だけがカテゴリを不成立にする"""
        calls = []
        monkeypatch.setattr(clabo_catalog, "CLABO_CATEGORIES", {"111": "A"})
        _install_catalog_pages(monkeypatch, {
            ("111", 1): _clabo_page_html(range(1, 101), total=130),  # 実際100件・申告130件
        }, calls)

        products, stats = crawl_catalog()

        assert len(products) == 100          # 取得できた分は捨てない
        assert stats["categories_failed"] == ["111"]
        assert stats["categories"]["111"] == {
            "total": 130, "seen": 100, "pages": 1, "ok": False, "verified": True,
        }

    def test_item_count_sentinel_10000_is_not_validated(self, monkeypatch):
        """item_count==10000 は検索ヒット件数キャップの疑いがあるため突合をスキップする
        （実際の件数がどれだけ少なくてもカテゴリ失敗にしない）"""
        calls = []
        monkeypatch.setattr(clabo_catalog, "CLABO_CATEGORIES", {"111": "A"})
        _install_catalog_pages(monkeypatch, {
            ("111", 1): _clabo_page_html(range(1, 51), total=10000),  # 実際50件・申告は明らかなキャップ値
        }, calls)

        products, stats = crawl_catalog()

        assert len(products) == 50
        assert stats["categories_failed"] == []
        assert stats["categories"]["111"] == {
            "total": 10000, "seen": 50, "pages": 1, "ok": True, "verified": True,
        }

    def test_item_count_zero_marks_category_failed(self, monkeypatch):
        """reviewer中2: total==0（正常時は14カテゴリ全て2件以上あり得ない）は不成立にする。
        under-collection専用のgap判定だと 0 は特殊値ですり抜けてしまうため明示的に弾く"""
        calls = []
        monkeypatch.setattr(clabo_catalog, "CLABO_CATEGORIES", {"111": "A"})
        _install_catalog_pages(monkeypatch, {
            ("111", 1): _clabo_page_html([], total=0),
        }, calls)

        products, stats = crawl_catalog()

        assert len(products) == 0
        assert stats["categories_failed"] == ["111"]
        assert stats["categories"]["111"]["ok"] is False

    def test_missing_item_count_with_natural_end_is_ok_but_unverified(self, monkeypatch):
        """reviewer中3b: item_countが読めなくても、120件未満で自然終端していれば
        不成立にしない（誤爆コストが大きいフォールバックを避ける）。
        verified=False・categories_unverified に積むところまで確認する"""
        calls = []
        monkeypatch.setattr(clabo_catalog, "CLABO_CATEGORIES", {"111": "A"})
        _install_catalog_pages(monkeypatch, {
            ("111", 1): _clabo_page_html(range(1, 31)),  # total を渡さない＝.item_countが無い
        }, calls)

        products, stats = crawl_catalog()

        assert len(products) == 30
        assert stats["categories_failed"] == []           # 不成立にしない
        assert stats["categories_unverified"] == ["111"]  # ただし未検証の印は残す
        assert stats["categories"]["111"] == {
            "total": None, "seen": 30, "pages": 1, "ok": True, "verified": False,
        }

    def test_missing_item_count_with_max_pages_end_stays_failed(self, monkeypatch):
        """reviewer中3b: 終端がMAX_PAGES到達（打ち切りの疑い）だった場合は、
        item_countが読めなくても緩めず従来どおり不成立のまま"""
        calls = []
        monkeypatch.setattr(clabo_catalog, "CLABO_CATEGORIES", {"111": "A"})
        monkeypatch.setattr(clabo_catalog, "CLABO_CATALOG_MAX_PAGES", 2)
        pages = {
            ("111", p): _clabo_page_html(range(p * 1000, p * 1000 + 120))  # totalなし
            for p in range(1, 5)
        }
        _install_catalog_pages(monkeypatch, pages, calls)

        products, stats = crawl_catalog()

        assert len(calls) == 2
        assert stats["categories_failed"] == ["111"]
        assert stats["categories"]["111"]["ok"] is False
        # MAX_PAGES到達由来の不成立は「未検証」ではなく通常の不成立として扱う
        # （item_countの有無に関係なく打ち切りの疑いそのものが理由のため）
        assert stats["categories_unverified"] == []

    def test_page_failure_stops_category_and_is_recorded(self, monkeypatch):
        """ページ取得失敗はそのカテゴリの残りを打ち切り、categories_failed / failed_pages に計上する。
        取れた分（1ページ目）は捨てない。この場合は item_count 検証を重ねて行わない"""
        calls = []
        monkeypatch.setattr(clabo_catalog, "CLABO_CATEGORIES", {"111": "A", "222": "B"})
        _install_catalog_pages(monkeypatch, {
            ("111", 1): _clabo_page_html(range(1, 121), total=250),
            ("111", 2): None,  # 失敗
            ("222", 1): _clabo_page_html(range(1, 31), total=30),
        }, calls)

        products, stats = crawl_catalog()

        assert len(products) == 120 + 30  # 111の1ページ目は残る
        assert stats["categories_failed"] == ["111"]     # 1カテゴリにつき1回だけ計上される
        assert stats["failed_pages"] == 1

    def test_stops_on_duplicate_page(self, monkeypatch):
        """範囲外ページが1ページ目を返す挙動への保険（重複検出で打ち切る）。
        item_countが実際の件数(120)と一致していれば検証も通る"""
        calls = []
        monkeypatch.setattr(clabo_catalog, "CLABO_CATEGORIES", {"111": "A"})
        first = _clabo_page_html(range(1, 121), total=120)
        _install_catalog_pages(monkeypatch, {("111", 1): first, ("111", 2): first}, calls)

        products, stats = crawl_catalog()

        assert len(products) == 120     # 2ページ目の重複分は加算されない
        assert len(calls) == 2
        assert stats["categories_failed"] == []

    def test_max_pages_reached_while_still_full_marks_category_failed(self, monkeypatch):
        """安全上限に達してもまだ120件フルが続いていた場合は打ち切りの疑いとして
        categories_failed に入れる（reviewer高1点4）。item_count は実際の取得数と
        一致させて、MAX_PAGES由来の失敗だけを切り分ける"""
        calls = []
        monkeypatch.setattr(clabo_catalog, "CLABO_CATEGORIES", {"111": "A"})
        monkeypatch.setattr(clabo_catalog, "CLABO_CATALOG_MAX_PAGES", 2)
        pages = {
            ("111", p): _clabo_page_html(range(p * 1000, p * 1000 + 120), total=240)
            for p in range(1, 5)   # 3・4ページ目は本来取得されないはず
        }
        _install_catalog_pages(monkeypatch, pages, calls)

        products, stats = crawl_catalog()

        assert len(calls) == 2                       # 安全上限で打ち切り、3ページ目は取りに行かない
        assert len(products) == 240
        assert stats["categories_failed"] == ["111"]
        assert stats["categories"]["111"]["ok"] is False
        assert stats["categories"]["111"]["pages"] == 2

    def test_filters_non_ygo_tag_and_rush_duel_without_card_name(self, monkeypatch):
        """他TCG（【遊戯】以外のタグ）とラッシュデュエルはカード照合前にここで落ちる"""
        calls = []
        monkeypatch.setattr(clabo_catalog, "CLABO_CATEGORIES", {"111": "A"})
        other_tcg = '<li><a href="/product/1">x</a><div class="inner_item_data">' \
                    '<span class="goods_name">【DM】宿命の決闘【VR】26EX2 30/89</span>' \
                    '<span class="figure">1,000円</span></div></li>'
        rush_duel = '<li><a href="/product/2">x</a><div class="inner_item_data">' \
                    '<span class="goods_name">【遊戯】火霊使いヒータ【ノーマル/RD】RD/KP01-JP001</span>' \
                    '<span class="figure">100円</span></div></li>'
        ygo = _clabo_item_html(1)
        html = f'<html><body><p class="item_count">3件</p><ul>{other_tcg}{rush_duel}{ygo}</ul></body></html>'
        _install_catalog_pages(monkeypatch, {("111", 1): html}, calls)

        products, stats = crawl_catalog()

        assert len(products) == 1
        assert stats["products"] == 1


# ── カテゴリ差分監視（harvest_categories / check_category_drift） ──

def _category_select_html(options: list[tuple[str, str]]) -> str:
    opts = "".join(f'<option value="{v}">{label}</option>' for v, label in options)
    return f'<html><body><select name="main_category">{opts}</select></body></html>'


class TestHarvestCategories:
    def test_harvests_ocg_labelled_options_only(self, monkeypatch):
        """低4: ラベルは実機形式（件数サフィックス付き）で固定する"""
        html = _category_select_html([
            ("", "選択してください"),
            ("679", "遊戯王OCG:通常 (339)"),
            ("676", "遊戯王OCG:効果 (7675)"),
            ("1637", "遊戯王OCG:限定プロモ (10)"),
            ("9999", "遊戯王ラッシュデュエル (123)"),
        ])

        def fake_get(url, timeout=15, retries=1):
            return BeautifulSoup(html, "html.parser")

        monkeypatch.setattr(scraper, "safe_get", fake_get)

        harvested = harvest_categories()

        assert harvested == [
            ("679", "遊戯王OCG:通常 (339)"),
            ("676", "遊戯王OCG:効果 (7675)"),
            ("1637", "遊戯王OCG:限定プロモ (10)"),
        ]

    def test_retries_2_is_passed_to_safe_get(self, monkeypatch):
        captured = []

        def fake_get(url, timeout=15, retries=1):
            captured.append(retries)
            return None

        monkeypatch.setattr(scraper, "safe_get", fake_get)
        harvest_categories()
        assert captured == [2]

    def test_returns_none_on_fetch_failure(self, monkeypatch):
        monkeypatch.setattr(scraper, "safe_get", lambda *a, **k: None)
        assert harvest_categories() is None

    def test_returns_none_when_select_element_missing(self, monkeypatch):
        """低3: セレクトが見つからない（構造破損）場合は空リストではなく None にする。
        空リストを返すと check_category_drift が『全カテゴリ消滅』の偽警告14件を出すため"""
        html = "<html><body>no select here</body></html>"
        monkeypatch.setattr(scraper, "safe_get", lambda *a, **k: BeautifulSoup(html, "html.parser"))
        assert harvest_categories() is None


class TestCheckCategoryDrift:
    def test_detects_new_and_missing_categories(self, monkeypatch):
        monkeypatch.setattr(clabo_catalog, "CLABO_CATEGORIES", {"679": "通常", "676": "効果"})
        harvested = [("679", "遊戯王OCG:通常"), ("9001", "遊戯王OCG:新種別")]

        warnings = check_category_drift(harvested)

        assert any("9001" in w for w in warnings)      # 追加を検出
        assert any("676" in w for w in warnings)        # 消滅を検出

    def test_no_drift_when_identical(self, monkeypatch):
        monkeypatch.setattr(clabo_catalog, "CLABO_CATEGORIES", {"679": "通常"})
        assert check_category_drift([("679", "遊戯王OCG:通常")]) == []

    def test_passes_through_none_on_fetch_failure(self):
        assert check_category_drift(None) is None


# ── 二段名寄せ（build_match_index + match_products） ──

def _make_card_names() -> list[str]:
    """記号ゆれ・型番付き・区切りの有無など、実在系のパターンを含む監視カード名の集合"""
    return [
        "青眼の白龍", "ブラック・マジシャン", "ブラックマジシャン", "死者蘇生",
        "召喚魔術－「剣」", "融合", "スターダスト・ドラゴン", "E・HERO",
        "E-HERO", "磁石の戦士α", "磁石の戦士β", "青眼の究極竜",
        "青眼の白龍(BLUE EYES WHITE DRAGON)", "灰流うらら", "増殖するG",
        "屋敷わらし", "無限泡影", "ハーピィの羽根帚", "エフェクト・ヴェーラー",
        "墓穴の指名者", "命削りの宝札", "サイコロ『天霆號アーゼウス』",
        "冀望郷－バリアン－", "聖なるバリア－ミラーフォース－", "炎舞－「天枢」",
        "PSYフレームギア・γ", "オルターガイスト・メリュシーク",
        "M∀LICE＜C＞GWC－０６", "No.39 希望皇ホープ", "ドラゴン族",
        "サイバー・ドラゴン", "サイバー・ドラゴン・ネオス", "六武衆", "閃刀姫",
        "灼熱の火霊使いヒータ", "水晶機巧－ハリファイバー", "混沌帝龍",
        "サンダー・ドラゴン", "青き眼の乙女", "光の護封剣", "召喚魔術",
    ]


def _make_products(card_names: list[str]) -> list[dict]:
    """記号入り・※プレフィックス・型番付き・別カード名を含む紛らわしい組を含める"""
    import random
    random.seed(20260809)

    templates = [
        "{name}",
        "※プレイ用特価品※{name}",
        "《未開封》{name}",
        "《開封済み》{name}",
        "{name}【ウルトラ/通常】TEST-JP{n:03d}",
        "{name}／バスター",           # 別カード名の延長として弾かれるはず
        "限定{name}セット",
        "{name}{other}",              # 別カード名2つの連結（紛らわしい組）
        "{other}{name}",
        "{name}(BLUE EYES WHITE DRAGON)",
        "スリーブ『{name}』",          # SUPPLY_KEYWORDS には非該当だが注記の一種
        "  {name}  ",
        "{name}・改",
        "純金製カード『{name}』",
    ]

    products = []
    n = 0
    for i, name in enumerate(card_names):
        for t in templates:
            other = card_names[(i + 7) % len(card_names)]
            n += 1
            raw = t.format(name=name, other=other, n=n)
            products.append({"name": raw})
    # 完全に無関係な商品も少し混ぜる
    for i in range(20):
        products.append({"name": f"謎の商品{i}号"})
    random.shuffle(products)
    return products


class TestTwoStageMatchEquivalence:
    def test_matches_direct_is_target_card_for_every_pair(self):
        """二段名寄せの結果が、直接 is_target_card を呼んだ結果と全組合せで一致すること
        （build_match_index の粗いパターンが取りこぼしゼロの前提を崩していないかの検証）"""
        card_names = _make_card_names()
        products = _make_products(card_names)
        pair_count = len(card_names) * len(products)
        assert pair_count >= 1000, f"組数が不足: {pair_count}"

        index = build_match_index(card_names)
        matched, stats = match_products(products, index)

        matched_per_product: dict[int, set] = defaultdict(set)
        for card_name, plist in matched.items():
            for p in plist:
                matched_per_product[id(p)].add(card_name)

        checked = 0
        for product in products:
            direct_hits = {c for c in card_names if scraper.is_target_card(c, product["name"])}
            two_stage_hits = matched_per_product.get(id(product), set())
            assert direct_hits == two_stage_hits, (
                product["name"], "direct=", direct_hits, "two_stage=", two_stage_hits
            )
            checked += len(card_names)

        assert checked == pair_count

    def test_multi_match_is_counted_when_a_product_matches_multiple_cards(self):
        """半角/全角・区切り記号ゆれで同一カードが二重登録されているケース
        （実例: TASKS.md「カード名DBの半角/全角二重登録」）で multi_match が計上される"""
        card_names = ["ブラック・マジシャン", "ブラックマジシャン"]
        products = [{"name": "ブラックマジシャン"}]

        index = build_match_index(card_names)
        matched, stats = match_products(products, index)

        assert stats["multi_match_products"] == 1
        assert stats["matched_products"] == 1
        assert set(matched.keys()) == {"ブラック・マジシャン", "ブラックマジシャン"}

    def test_unmatched_product_is_counted(self):
        index = build_match_index(["青眼の白龍"])
        matched, stats = match_products([{"name": "無関係な商品"}], index)

        assert stats["unmatched_products"] == 1
        assert stats["matched_products"] == 0
        assert matched == {}

    def test_empty_pattern_card_name_never_matches(self):
        """区切り記号だけのカード名は候補にすら残さない（is_target_card は常にFalse）"""
        index = build_match_index(["☆", "青眼の白龍"])
        assert [name for name, _ in index] == ["青眼の白龍"]


# ── collect_clabo_catalog（オーケストレータ） ──

class TestCollectClaboCatalog:
    def test_returns_none_when_crawl_has_failures(self, monkeypatch):
        monkeypatch.setattr(clabo_catalog, "harvest_categories", lambda: None)
        monkeypatch.setattr(
            clabo_catalog, "crawl_catalog",
            lambda: ([], {
                "categories_total": 14, "categories_failed": ["672"],
                "pages_fetched": 10, "products": 0, "failed_pages": 1,
                "categories": {},
            }),
        )

        matched, stats = collect_clabo_catalog(["青眼の白龍"])

        assert matched is None
        assert stats["categories_failed"] == ["672"]
        assert stats["category_drift"] is None  # harvest_categoriesが取得失敗のため

    def test_returns_matched_dict_when_crawl_succeeds(self, monkeypatch):
        monkeypatch.setattr(clabo_catalog, "harvest_categories", lambda: [("679", "遊戯王OCG:通常 (339)")])
        products = [{"name": "青眼の白龍", "price": 100}]
        monkeypatch.setattr(
            clabo_catalog, "crawl_catalog",
            lambda: (products, {
                "categories_total": 14, "categories_failed": [],
                "pages_fetched": 14, "products": 1, "failed_pages": 0,
                "categories": {},
            }),
        )

        matched, stats = collect_clabo_catalog(["青眼の白龍"])

        assert matched == {"青眼の白龍": products}
        assert stats["matched_products"] == 1

    def test_returns_none_when_products_is_zero_even_without_category_failures(self, monkeypatch):
        """reviewer中2の保険条件: categories_failed/failed_pages が両方クリアでも
        products==0（全カテゴリ0件のカタログ）なら不成立にする"""
        monkeypatch.setattr(clabo_catalog, "harvest_categories", lambda: None)
        monkeypatch.setattr(
            clabo_catalog, "crawl_catalog",
            lambda: ([], {
                "categories_total": 14, "categories_failed": [], "pages_fetched": 14,
                "products": 0, "failed_pages": 0, "categories": {},
            }),
        )

        matched, stats = collect_clabo_catalog(["青眼の白龍"])

        assert matched is None
        assert stats["products"] == 0

    def test_category_drift_warnings_are_included_in_stats(self, monkeypatch):
        """reviewer中2: check_category_drift の結果を stats["category_drift"] に永続化する"""
        monkeypatch.setattr(clabo_catalog, "CLABO_CATEGORIES", {"679": "通常"})
        monkeypatch.setattr(clabo_catalog, "harvest_categories", lambda: [("679", "遊戯王OCG:通常 (339)"), ("9001", "遊戯王OCG:新種別 (5)")])
        monkeypatch.setattr(
            clabo_catalog, "crawl_catalog",
            lambda: ([], {
                "categories_total": 1, "categories_failed": [], "pages_fetched": 1,
                "products": 0, "failed_pages": 0, "categories": {},
            }),
        )

        matched, stats = collect_clabo_catalog([])

        assert stats["category_drift"] == ["新しいカテゴリを検出: 9001 = 遊戯王OCG:新種別 (5)（CLABO_CATEGORIES未登録）"]
