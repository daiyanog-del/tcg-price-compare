"""店舗ごとの送料ルールと最低注文金額。

まとめ買い比較（/api/wish-shop-totals）で「商品代金の合計」だけを並べると、
送料と最低注文金額のせいで実際の支払額や購入可否が変わる店舗を見誤る。
このモジュールはその2つのルールだけを持つ。

## 前提（2026-08-05 にユーザーと合意した固定条件）
- **最安の配送方法・最小の厚さ**で固定する。送料は実際には「配送方法 × 厚さ（枚数）
  × 地域」で決まるが、枚数から厚さを正確に出す手段がない（スリーブ・梱包材で変わる）。
  そのため各店で最も安い条件を採用し、表示は「最低でもこれだけかかる」の下限見積もりとして扱う。
- 送料無料しきい値の判定基準は **商品代金の合計**（送料・手数料を含まない）。
  カードラッシュの原文が「商品5000円以上」、遊々亭の最低注文金額が
  「送料・各手数料を除く」であることに合わせた。
- 会員価格が選べる店舗は**会員**を前提にする（トレコロCB）。

## 更新のしかた
各店が送料を改定すると値が古くなる。定期スクレイプはせず手動更新とし、
出典URL・確認日をルールごとに併記する（2026-08-05 決定）。
値を直したら `checked_on` も必ず更新すること。
"""

from __future__ import annotations

# 送料ルールを確認した日。UI の注記に出す。値を更新したらこの日付も更新する。
SHIPPING_CHECKED_ON = "2026-08-05"


class ShippingRule:
    """1店舗ぶんの送料・最低注文金額ルール。

    base_fee:        最安の配送方法・最小厚さでの送料（円）
    free_threshold:  この商品代金以上で送料無料（None = 無料になる条件なし）
    min_order:       この商品代金未満は注文できない（None = 制限なし）
    """

    __slots__ = ("shop", "base_fee", "free_threshold", "min_order",
                 "method", "source_url", "checked_on", "note")

    def __init__(self, shop: str, base_fee: int, free_threshold: int | None,
                 min_order: int | None, method: str, source_url: str,
                 checked_on: str = SHIPPING_CHECKED_ON, note: str = ""):
        self.shop = shop
        self.base_fee = base_fee
        self.free_threshold = free_threshold
        self.min_order = min_order
        self.method = method
        self.source_url = source_url
        self.checked_on = checked_on
        self.note = note


# 店舗名は scraper.SHOPS の表記に合わせること（キー突合に使う）
SHIPPING_RULES: dict[str, ShippingRule] = {
    "遊々亭": ShippingRule(
        shop="遊々亭",
        base_fee=0,
        free_threshold=0,   # 常に無料（金額・サイズ問わず）
        min_order=1000,
        method="おまかせ便（全国一律）",
        source_url="https://yuyu-tei.jp/info/faq/answer?faq_id=44",
        note="「ご請求金額が1,000円以上(送料・各手数料を除く)よりご注文を受け付け」。"
             "おまかせ便は購入金額・商品サイズ問わず全国送料無料（https://yuyu-tei.jp/info/about）",
    ),
    "カーナベル": ShippingRule(
        shop="カーナベル",
        base_fee=100,
        # TODO: calibrate from data — 送料無料条件は公式の送料ページ・FAQ のどちらにも
        # 記載が無く、一次情報で確認できなかった（検索エンジン上には「4,000円以上で無料」
        # という記述があるが裏が取れない）。確認できるまで「無料条件なし」として扱う。
        free_threshold=None,
        min_order=2000,
        method="日時指定なし（全国一律）",
        source_url="https://www.ka-nabell.com/sendinfo.html",
        note="「カート内金額が2,000円未満の場合：商品代金2,000円以上から注文可能です」"
             "（販売よくある質問「注文が確定できない」）。日時指定ありは送料+600円、"
             "代金引換+325円、コンビニ後払い+300円だが最安条件のみを採る",
    ),
    "トレコロCB": ShippingRule(
        shop="トレコロCB",
        base_fee=150,
        free_threshold=3000,
        min_order=None,
        method="会員・中古シングル（全国一律）",
        source_url="https://www.torecolo.jp/shop/t/t1019/",
        note="会員は3,000円未満が150〜250円（購入枚数による）・3,000円以上で無料。"
             "非会員は5,000円未満が250円・5,000円以上で無料。会員かつ最小枚数を前提に150円を採用。"
             "代金引換またはシングル181枚以上は30,000円以上で無料という別体系になる",
    ),
    "カードラッシュ": ShippingRule(
        shop="カードラッシュ",
        base_fee=180,
        free_threshold=5000,
        min_order=None,
        method="ゆうパケット 厚さ1cm",
        source_url="https://www.cardrush.jp/phone/help",
        note="「ゆうパケットご利用時に商品5000円以上をご購入されたお客様は送料無料」。"
             "厚さ1cm=180円 / 2cm=230円 / 3cm=320円のうち最小厚さを採用",
    ),
    "まんぞく屋": ShippingRule(
        shop="まんぞく屋",
        base_fee=245,
        free_threshold=None,
        min_order=None,
        method="ネコポス（全国一律）",
        source_url="https://shopmanzokuya.com/guide",
        note="ネコポス全国一律245円。宅急便は沖縄1,000円・他全国一律800円。"
             "送料無料になる条件の記載は無い。同梱に非対応で注文ごとに送料が発生する",
    ),
    "カードラボ": ShippingRule(
        shop="カードラボ",
        base_fee=290,
        free_threshold=None,
        min_order=None,
        method="ネコポス（全国一律）",
        source_url="https://www.c-labo-online.jp/help",
        note="ネコポス全国一律290円。レターパック600円、宅急便は地域別。"
             "送料無料になる条件の記載は無い",
    ),
}


def calc_shipping(shop: str, subtotal: int) -> int | None:
    """商品代金の合計から送料を求める。ルール未登録の店舗は None（＝送料不明）。

    subtotal は送料・手数料を含まない商品代金の合計。
    subtotal が 0（＝1枚も揃わない店舗）なら送料は発生しないので 0 を返す。
    """
    rule = SHIPPING_RULES.get(shop)
    if rule is None:
        return None
    if subtotal <= 0:
        return 0
    if rule.free_threshold is not None and subtotal >= rule.free_threshold:
        return 0
    return rule.base_fee


def amount_to_free_shipping(shop: str, subtotal: int) -> int | None:
    """送料無料まであといくら必要かを返す。

    None を返すのは「無料条件が無い店舗」「ルール未登録」「すでに無料」
    「1枚も揃わない店舗」のいずれか。UI では「あと¥◯で送料無料」の案内に使う。
    """
    rule = SHIPPING_RULES.get(shop)
    if rule is None or rule.free_threshold is None or subtotal <= 0:
        return None
    remain = rule.free_threshold - subtotal
    return remain if remain > 0 else None


def amount_to_min_order(shop: str, subtotal: int) -> int | None:
    """最低注文金額まであといくら必要かを返す。満たしている／制限なしなら None。

    subtotal が 0（＝1枚も揃わない店舗）は判定対象外として None を返す。
    「最低注文金額に届かない」ではなく「そもそも買う物が無い」ためで、
    こちらは covered_qty 側の表示で伝わる。
    """
    rule = SHIPPING_RULES.get(shop)
    if rule is None or rule.min_order is None or subtotal <= 0:
        return None
    remain = rule.min_order - subtotal
    return remain if remain > 0 else None


def is_orderable(shop: str, subtotal: int) -> bool:
    """その商品代金でこの店舗に注文できるか。ルール未登録の店舗は True 扱い。"""
    return amount_to_min_order(shop, subtotal) is None


def rules_for_client() -> dict[str, dict]:
    """フロントに配る送料ルール（JSON化できる形）。

    購入候補リストの画面は DB 相場で一度描画したあと、リアルタイム検索の結果で
    合計金額が後から増える。そのとき送料無料しきい値をまたぐと送料が変わるため、
    フロント側でも同じ計算をやり直す必要がある。テーブルを Python と JS に
    二重に書くとズレるので、サーバのルールをそのまま配って JS は適用するだけにする。
    """
    return {
        shop: {
            "base_fee": r.base_fee,
            "free_threshold": r.free_threshold,
            "min_order": r.min_order,
            "method": r.method,
            "source_url": r.source_url,
            "checked_on": r.checked_on,
        }
        for shop, r in SHIPPING_RULES.items()
    }
