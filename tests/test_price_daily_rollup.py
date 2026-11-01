# 2026-10-22 DB容量対策（案D）: 店舗別の行は30日・31〜100日は日ごとの全店最安に畳む
# （price_daily_rollup.sql / docs/decisions.md 同日）。
#   - cleanup_old_data は畳み込みが成功したときだけ店舗別の行を消す
#   - /api/price-history と staple_post.fetch_history は畳んだ分を合わせて、畳む前と同じ値を返す
#   - ygores_cards の raw は日本語だけにして保存する
from datetime import datetime, timedelta

import pytest

from constants import JST, PRICE_RETENTION_DAYS, PRICE_DAILY_RETENTION_DAYS, REPLAY_RETENTION_DAYS


class _Resp:
    def __init__(self, data):
        self.data = data


class _Query:
    """eq / gte / lte / lt と range だけ解釈する偽のクエリ。delete は条件に合う行を消す。"""

    def __init__(self, store, name, log):
        self.store, self.name, self.log = store, name, log
        self.conds, self.op, self.rng, self.orders = [], "select", None, []

    def select(self, *_a, **_k):
        return self

    def delete(self, *_a, **_k):
        self.op = "delete"
        return self

    def eq(self, k, v):
        self.conds.append((k, lambda x, v=v: x == v))
        return self

    def gte(self, k, v):
        self.conds.append((k, lambda x, v=v: x >= v))
        return self

    def lte(self, k, v):
        self.conds.append((k, lambda x, v=v: x <= v))
        return self

    def lt(self, k, v):
        self.conds.append((k, lambda x, v=v: x < v))
        self.log.append((self.name, self.op, k, v))
        return self

    def order(self, k, desc=False):
        self.orders.append((k, desc))
        return self

    def range(self, a, b):
        self.rng = (a, b)
        return self

    def execute(self):
        rows = self.store.get(self.name)
        if rows is None:
            raise RuntimeError(f"relation {self.name} does not exist")
        hit = [r for r in rows if all(f(r.get(k)) for k, f in self.conds)]
        if self.op == "delete":
            self.store[self.name] = [r for r in rows if r not in hit]
            self.log.append((self.name, "deleted", len(hit)))
            return _Resp([])  # returning=minimal（消した行は返さない）
        for k, desc in reversed(self.orders):
            hit = sorted(hit, key=lambda r: r.get(k) or "", reverse=desc)
        if self.rng:
            hit = hit[self.rng[0]:self.rng[1] + 1]
        return _Resp(hit)


class _Rpc:
    def __init__(self, sb, name, params):
        self.sb, self.name, self.params = sb, name, params

    def execute(self):
        self.sb.rpc_calls.append((self.name, self.params))
        if self.sb.rpc_error:
            raise RuntimeError(self.sb.rpc_error)
        if self.name == "rollup_price_daily":
            lo, hi = self.params["p_from"], self.params["p_before"]
            self.sb.rolled_days.extend(sorted({r["recorded_at"] for r in self.sb.store["price_history_v2"]
                                               if lo <= r["recorded_at"] < hi}))
        return _Resp([{"rolled_rows": 3}])


class _Sb:
    def __init__(self, store, rpc_error=None):
        self.store, self.rpc_error = store, rpc_error
        self.log, self.rpc_calls, self.rolled_days = [], [], []

    def table(self, name):
        return _Query(self.store, name, self.log)

    def rpc(self, name, params):
        return _Rpc(self, name, params)


def _d(days_ago):
    return (datetime.now(JST).date() - timedelta(days=days_ago)).isoformat()


# ──────────────────────────────────────────────
# 夜間の掃除
# ──────────────────────────────────────────────

def _cleanup_store(old_days=(PRICE_RETENTION_DAYS + 2, PRICE_RETENTION_DAYS + 1)):
    v2 = []
    for ago in list(old_days) + [PRICE_RETENTION_DAYS, 1]:
        v2 += [{"recorded_at": _d(ago), "shop": "A"}, {"recorded_at": _d(ago), "shop": "B"}]
    return {
        "price_history_v2": v2,
        "price_daily_min": [{"recorded_at": _d(PRICE_DAILY_RETENTION_DAYS + 1)}, {"recorded_at": _d(40)}],
        "solitaire_replays": [],
        # 絵ごとの内訳（price_history_art.sql 適用後の形。2026-10-30）
        "price_history_art": [],
        "price_daily_art": [],
    }


@pytest.fixture
def discord(monkeypatch):
    import collect_prices
    sent = []
    monkeypatch.setattr(collect_prices, "send_discord_message", lambda m: sent.append(m))
    return sent


def _left(sb):
    return sorted({r["recorded_at"] for r in sb.store["price_history_v2"]})


def test_cleanup_rolls_up_one_day_at_a_time_oldest_first(discord):
    import collect_prices
    sb = _Sb(_cleanup_store())
    collect_prices.cleanup_old_data(sb)
    d2, d1, d0 = _d(PRICE_RETENTION_DAYS + 2), _d(PRICE_RETENTION_DAYS + 1), _d(PRICE_RETENTION_DAYS)
    assert sb.rpc_calls == [("rollup_price_daily", {"p_before": d1, "p_from": d2}),
                            ("rollup_price_daily", {"p_before": d0, "p_from": d1})]
    assert sb.rolled_days == [d2, d1]       # 畳んだ日と消した日が一致する
    assert _left(sb) == sorted([d0, _d(1)])  # 30日目（cutoff 当日）は残す
    assert [r["recorded_at"] for r in sb.store["price_daily_min"]] == [_d(40)]
    # リプレイは従来どおり90日（価格の30日に引きずられない）
    assert ("solitaire_replays", "delete", "created_at", _d(REPLAY_RETENTION_DAYS)) in sb.log
    assert discord == []


def test_cleanup_keeps_store_rows_and_notifies_when_rollup_fails(discord):
    import collect_prices
    sb = _Sb(_cleanup_store(), rpc_error="function rollup_price_daily does not exist")
    collect_prices.cleanup_old_data(sb)
    assert len(sb.store["price_history_v2"]) == 8  # 畳めなかった日は消さない
    assert len(discord) == 1 and "畳み込み" in discord[0]
    assert [r["recorded_at"] for r in sb.store["price_daily_min"]] == [_d(40)]  # 100日超の掃除は別に進む


def test_cleanup_caps_days_per_night_and_notifies_backlog(discord):
    import collect_prices
    n = collect_prices.MAX_ROLLUP_DAYS_PER_NIGHT
    old = [PRICE_RETENTION_DAYS + k for k in range(n + 2, 0, -1)]  # 上限より2日多い
    sb = _Sb(_cleanup_store(old_days=old))
    collect_prices.cleanup_old_data(sb)
    assert len(sb.rpc_calls) == n
    assert sb.rolled_days == [_d(a) for a in old[:n]]
    assert _left(sb)[0] == _d(old[n])
    assert len(discord) == 1 and "上限" in discord[0]


def test_retention_constants():
    assert (PRICE_RETENTION_DAYS, PRICE_DAILY_RETENTION_DAYS, REPLAY_RETENTION_DAYS) == (14, 100, 90)
    import collect_buyback
    from constants import BUYBACK_RETENTION_DAYS
    sb = _Sb({"buyback_history": [{"recorded_at": _d(31)}, {"recorded_at": _d(29)}]})
    collect_buyback.cleanup_old_buyback_data(sb)
    assert BUYBACK_RETENTION_DAYS == 30
    assert [r["recorded_at"] for r in sb.store["buyback_history"]] == [_d(29)]


# ──────────────────────────────────────────────
# /api/price-history
# ──────────────────────────────────────────────

@pytest.fixture
def app_client(monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, "_load_cardnames", lambda: None)
    monkeypatch.setattr(app_module, "_correct_cardname", lambda n: n)

    def use(store):
        monkeypatch.setattr(app_module, "_supabase_client", _Sb(store))
        return app_module.app.test_client()
    return use


def test_price_history_merges_daily_rows_before_store_rows(app_client):
    card = "灰流うらら"
    store = {
        "price_history": [
            {"card_name": card, "shop": "A", "rarity": "ウルトラ", "min_price": 900, "recorded_at": _d(1)},
            {"card_name": card, "shop": "B", "rarity": "ウルトラ", "min_price": 950, "recorded_at": _d(1)},
            # 畳み込みと削除の間で両方に残っている日
            {"card_name": card, "shop": "A", "rarity": "ウルトラ", "min_price": 1000, "recorded_at": _d(30)},
        ],
        "price_daily": [
            {"card_name": card, "shop": "A", "rarity": "ウルトラ", "min_price": 800, "recorded_at": _d(60)},
            {"card_name": card, "shop": "B", "rarity": "ウルトラ", "min_price": 800, "recorded_at": _d(60)},
            {"card_name": card, "shop": "A", "rarity": "ウルトラ", "min_price": 1000, "recorded_at": _d(30)},
            {"card_name": card, "shop": "A", "rarity": "ウルトラ", "min_price": 700,
             "recorded_at": _d(PRICE_DAILY_RETENTION_DAYS + 1)},  # 期間外
            {"card_name": "別カード", "shop": "A", "rarity": "ウルトラ", "min_price": 1, "recorded_at": _d(60)},
        ],
    }
    data = app_client(store).get(f"/api/price-history?card={card}").get_json()["data"]
    assert [(r["date"], r["shop"], r["price"]) for r in data] == [
        (_d(60), "A", 800), (_d(60), "B", 800),        # 畳んだ分（同額の店は両方）
        (_d(30), "A", 1000), (_d(1), "A", 900), (_d(1), "B", 950),  # 店舗別の行（重複日は1回だけ）
    ]


@pytest.mark.parametrize("n", [0, 999, 1000, 1001, 2001])
def test_price_history_reads_every_page(app_client, n):
    """1ページ目と日ごとの最安を同時に読み、足りなければ続きを順に読む（2026-11-01）。ページの境目で行を落とさない"""
    card = "灰流うらら"
    store = {
        "price_history": [{"card_name": card, "shop": f"店{i:05d}", "rarity": "ウルトラ", "min_price": i,
                           "recorded_at": _d(1 + i % 20)} for i in range(n)],
        "price_daily": [{"card_name": card, "shop": "A", "rarity": "ウルトラ", "min_price": 1, "recorded_at": _d(60)}],
    }
    data = app_client(store).get(f"/api/price-history?card={card}").get_json()["data"]
    assert len(data) == n + 1
    assert sorted(r["price"] for r in data[1:]) == list(range(n))
    assert [r["date"] for r in data] == sorted(r["date"] for r in data)


def test_price_history_falls_back_to_store_rows_without_daily_view(app_client):
    card = "灰流うらら"
    store = {"price_history": [
        {"card_name": card, "shop": "A", "rarity": "ウルトラ", "min_price": 900, "recorded_at": _d(1)}]}
    data = app_client(store).get(f"/api/price-history?card={card}").get_json()["data"]
    assert [(r["date"], r["price"]) for r in data] == [(_d(1), 900)]


def test_price_history_folds_kanabell_manga_rarity(app_client):
    # カーナベルの MANGAシク は画面の札と同じ「シークレット」の系列で返す（畳んだ分も）。2026-10-30
    card = "ベビードラゴン"
    store = {
        "price_history": [
            {"card_name": card, "shop": "カーナベル", "rarity": "MANGAシク", "min_price": 1180, "recorded_at": _d(1)},
            {"card_name": card, "shop": "遊々亭", "rarity": "シークレット", "min_price": 980, "recorded_at": _d(1)},
        ],
        "price_daily": [
            {"card_name": card, "shop": "カーナベル", "rarity": "MANGAウル", "min_price": 480, "recorded_at": _d(60)},
        ],
    }
    data = app_client(store).get(f"/api/price-history?card={card}").get_json()["data"]
    assert [(r["shop"], r["rarity"]) for r in data] == [
        ("カーナベル", "ウルトラ"), ("カーナベル", "シークレット"), ("遊々亭", "シークレット")]


# ──────────────────────────────────────────────
# 汎用カードX投稿のグラフ
# ──────────────────────────────────────────────

def test_staple_history_uses_over10_min_from_daily_rows():
    import staple_post
    name, rar = "灰流うらら", "ウルトラ"
    today = _d(0)
    store = {
        "price_history": [
            {"card_name": name, "rarity": rar, "min_price": 5, "recorded_at": _d(1)},     # 10円以下は捨てる
            {"card_name": name, "rarity": rar, "min_price": 900, "recorded_at": _d(1)},
        ],
        "price_daily": [
            {"card_name": name, "rarity": rar, "min_price_over10": 800, "recorded_at": _d(50)},
            {"card_name": name, "rarity": rar, "min_price_over10": 800, "recorded_at": _d(50)},  # 同額の店で2行
            {"card_name": name, "rarity": rar, "min_price_over10": None, "recorded_at": _d(51)},  # 全店10円以下
            {"card_name": name, "rarity": rar, "min_price_over10": 1, "recorded_at": _d(1)},     # 店舗別の行がある日
        ],
    }
    assert staple_post.fetch_history(_Sb(store), name, rar, today) == [(_d(50), 800), (_d(1), 900)]


def test_staple_history_without_daily_view():
    import staple_post
    store = {"price_history": [{"card_name": "X", "rarity": "R", "min_price": 300, "recorded_at": _d(2)}]}
    assert staple_post.fetch_history(_Sb(store), "X", "R", _d(0)) == [(_d(2), 300)]


# ──────────────────────────────────────────────
# ygores_cards の raw
# ──────────────────────────────────────────────

def test_ygores_raw_keeps_only_japanese_card_data():
    from ygores_repository import CardDataRepository
    ja = {"name": "青眼の白龍", "cardType": "monster", "prints": [{"code": "LB-01"}]}
    api = {"cardData": {"ja": ja, "en": {"name": "Blue-Eyes"}, "ko": {}}, "faqData": {"x": 1}}
    row = CardDataRepository._card_row(4007, api, "now")
    assert row["raw"] == {"cardData": {"ja": ja}, "faqData": {"x": 1}}
    assert row["name"] == "青眼の白龍"
    assert api["cardData"]["en"] == {"name": "Blue-Eyes"}  # 呼び元の dict は書き換えない
    no_ja = {"cardData": {"en": {"name": "X"}}}
    assert CardDataRepository._card_row(1, no_ja, "now")["raw"] is no_ja
    mirror = {"name": "ミラー形式", "type": "monster"}
    assert CardDataRepository._card_row(2, mirror, "now")["raw"] is mirror


# ──────────────────────────────────────────────
# 絵ごとの内訳（price_history_art・2026-10-30 イラスト違いの段2）
# ──────────────────────────────────────────────

def test_cleanup_rolls_up_art_rows_like_main_table(discord):
    import collect_prices
    store = _cleanup_store()
    d2, d1 = _d(PRICE_RETENTION_DAYS + 2), _d(PRICE_RETENTION_DAYS + 1)
    store["price_history_art"] = [{"recorded_at": d2}, {"recorded_at": _d(1)}]
    store["price_daily_art"] = [{"recorded_at": _d(PRICE_DAILY_RETENTION_DAYS + 1)}, {"recorded_at": _d(40)}]
    sb = _Sb(store)
    collect_prices.cleanup_old_data(sb)
    assert ("rollup_price_daily_art", {"p_before": d1, "p_from": d2}) in sb.rpc_calls
    assert [r["recorded_at"] for r in sb.store["price_history_art"]] == [_d(1)]
    assert [r["recorded_at"] for r in sb.store["price_daily_art"]] == [_d(40)]
    assert discord == []


class _NoArtSb(_Sb):
    """price_history_art.sql を適用する前の DB（PostgREST は表が無いと PGRST205 を返す）"""

    def table(self, name):
        if name in ("price_history_art", "price_daily_art"):
            raise RuntimeError("{'code': 'PGRST205', 'message': \"Could not find the table 'public.%s' in the schema cache\"}" % name)
        return super().table(name)


def test_cleanup_without_art_tables_is_silent(discord):
    import collect_prices
    store = _cleanup_store()
    del store["price_history_art"], store["price_daily_art"]
    sb = _NoArtSb(store)
    collect_prices.cleanup_old_data(sb)
    assert not any(name == "rollup_price_daily_art" for name, _ in sb.rpc_calls)
    assert _left(sb) == sorted([_d(PRICE_RETENTION_DAYS), _d(1)])  # 本表の畳み込みはいつもどおり
    assert discord == []

