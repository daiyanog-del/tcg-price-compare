"""
tests/test_x_poster_rpc.py — x_poster._rpc_rows_to_card_dates（RPC行→card_dates変換）の単体テスト

背景:
  post_big_movers の集計を Python 集計（旧 get_price_movers_by_rarity）から
  Supabase RPC `get_price_movers`（店舗粒度のガード①②適用済み）呼び出しに一本化した。
  RPC の戻り行を select_big_movers が受け取る card_dates 形式
  {(name, rarity): {date_str: price}} に変換する _rpc_rows_to_card_dates を対象にテストする。

  post_big_movers は per_card=999 でRPCを呼ぶため、同一カード名で複数レアリティの
  行が返りうる（RPC側で pct 最大の1レアリティに事前に畳み込むと、BIGMOVE閾値が
  diff にも依存するために取りこぼしが生じるため）。変換後も全レアリティが
  card_dates に個別キーとして保持されることを確認する。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import featured_pack
import discord_notify
from x_poster import (
    _rpc_rows_to_card_dates, select_big_movers, _select_featured_movers,
    format_featured_tweet, post_featured_movers,
)

OLD, NEW = "2026-07-09", "2026-07-10"


def rpc_row(name, rarity, prev_price, today_price, date_old=OLD, date_new=NEW, direction="up"):
    """get_price_movers RPC の戻り行を模したdict"""
    return {
        "card_name": name, "rarity": rarity,
        "today_price": today_price, "prev_price": prev_price,
        "diff": today_price - prev_price,
        "pct": round((today_price - prev_price) / prev_price * 100, 1),
        "date_new": date_new, "date_old": date_old,
        "direction": direction,
    }


class TestRpcRowsToCardDates:
    def test_empty_rows_returns_empty(self):
        card_dates, date_old, date_new = _rpc_rows_to_card_dates([])
        assert card_dates == {}
        assert date_old is None
        assert date_new is None

    def test_single_row_converted(self):
        rows = [rpc_row("ブラックマジシャン", "プリシク", 880, 5180)]
        card_dates, date_old, date_new = _rpc_rows_to_card_dates(rows)
        assert date_old == OLD
        assert date_new == NEW
        assert card_dates[("ブラックマジシャン", "プリシク")] == {OLD: 880, NEW: 5180}

    def test_multiple_rows_keyed_by_name_rarity(self):
        rows = [
            rpc_row("カードA", "ノーマル", 1000, 3000),
            rpc_row("カードB", "シク", 2000, 1000, direction="down"),
        ]
        card_dates, date_old, date_new = _rpc_rows_to_card_dates(rows)
        assert len(card_dates) == 2
        assert card_dates[("カードA", "ノーマル")] == {OLD: 1000, NEW: 3000}
        assert card_dates[("カードB", "シク")] == {OLD: 2000, NEW: 1000}

    def test_missing_keys_default_to_zero_and_empty_string(self):
        # RPC側で予期せず欠損した場合の防御的デフォルト（0円・空文字）
        rows = [{"card_name": "カードX", "date_new": NEW, "date_old": OLD}]
        card_dates, date_old, date_new = _rpc_rows_to_card_dates(rows)
        assert card_dates[("カードX", "")] == {OLD: 0, NEW: 0}

    def test_same_card_multiple_rarities_all_kept(self):
        # per_card=999 呼び出しにより同名カードの複数レアリティ行が返る想定。
        # (name, rarity) キーで別エントリとして全レアリティが保持されること
        rows = [
            rpc_row("青眼の白龍", "ノーマル", 1000, 3000),
            rpc_row("青眼の白龍", "シークレット", 2000, 5000),
            rpc_row("青眼の白龍", "20thシークレット", 8000, 12000),
        ]
        card_dates, date_old, date_new = _rpc_rows_to_card_dates(rows)
        assert len(card_dates) == 3
        assert card_dates[("青眼の白龍", "ノーマル")] == {OLD: 1000, NEW: 3000}
        assert card_dates[("青眼の白龍", "シークレット")] == {OLD: 2000, NEW: 5000}
        assert card_dates[("青眼の白龍", "20thシークレット")] == {OLD: 8000, NEW: 12000}


class TestRpcConversionFeedsSelectBigMovers(object):
    """変換結果が select_big_movers にそのまま食わせられることを確認する統合的な単体テスト"""

    def test_converted_rows_detected_as_bigmove_candidate(self):
        # RPC min_diff=50 は通過済みだが BIGMOVE 閾値(+2000円/+100%)も満たすケース
        rows = [rpc_row("急騰カード", "ノーマル", 880, 5180)]
        card_dates, date_old, date_new = _rpc_rows_to_card_dates(rows)
        result = select_big_movers(card_dates, date_old, date_new, top_n=3)
        assert len(result) == 1
        assert result[0]["name"] == "急騰カード"
        assert result[0]["direction"] == "up"

    def test_converted_rows_below_bigmove_threshold_excluded(self):
        # RPCのmin_diff=50は満たすが、BIGMOVE閾値(+2000円/+100%)には届かないケース
        rows = [rpc_row("小変動カード", "ノーマル", 1000, 1100)]  # diff=+100, pct=+10%
        card_dates, date_old, date_new = _rpc_rows_to_card_dates(rows)
        result = select_big_movers(card_dates, date_old, date_new, top_n=3)
        assert result == []


class TestSelectFeaturedMovers:
    """_select_featured_movers（post_featured_movers の通常モード用選抜）の単体テスト

    背景: 2026-08-06 監査F4で post_featured_movers が旧Python集計 get_price_movers
    （レアリティ横断min・ガード無し・fallback必ず投稿）を使っており、ガード済みRPC
    get_price_movers の確認済み変動と重なりが実測0件だったため、post_big_movers と
    同じ RPC ベースへ移行した。ここでは RPC 結果からの絞り込みロジックを固定する。
    """

    def test_allowed_filters_out_other_cards(self):
        # allowed（新弾収録カード集合）に含まれないカードは除外される
        rows = [
            rpc_row("新弾カードA", "ノーマル", 1000, 1500),  # +50%, allowed
            rpc_row("旧弾カードB", "ノーマル", 1000, 9000),  # +800%だがallowed外
        ]
        movers, date_old, date_new = _select_featured_movers(rows, allowed={"新弾カードA"})
        assert [m["name"] for m in movers] == ["新弾カードA"]
        assert date_old == OLD
        assert date_new == NEW

    def test_direction_down_excluded(self):
        # direction='up' 以外（値下がり）は対象外
        rows = [
            rpc_row("新弾カードA", "ノーマル", 1000, 1500, direction="up"),
            rpc_row("新弾カードB", "ノーマル", 2000, 1000, direction="down"),
        ]
        movers, _, _ = _select_featured_movers(rows, allowed={"新弾カードA", "新弾カードB"})
        assert [m["name"] for m in movers] == ["新弾カードA"]

    def test_sorted_by_pct_descending_not_diff(self):
        # pctとdiffの大小関係が逆行する入力で、ソート基準が本当にpctであることを判別する
        # （diffでソートしていた場合と結果順が変わるよう prev_price を意図的にばらけさせる）
        rows = [
            rpc_row("低額急騰", "ノーマル", 100, 200),      # diff=+100, pct=+100.0%
            rpc_row("高額微騰", "ノーマル", 10000, 10150),  # diff=+150(最大), pct=+1.5%(最小)
            rpc_row("中額急騰", "ノーマル", 50, 90),         # diff=+40(最小), pct=+80.0%
        ]
        allowed = {"低額急騰", "高額微騰", "中額急騰"}
        movers, _, _ = _select_featured_movers(rows, allowed=allowed)
        # pct降順なら 低額急騰(100%) → 中額急騰(80%) → 高額微騰(1.5%)。
        # diff降順であれば 高額微騰(150) → 低額急騰(100) → 中額急騰(40) になり一致しない。
        assert [m["name"] for m in movers] == ["低額急騰", "中額急騰", "高額微騰"]

    def test_sorted_by_pct_descending_top5(self):
        # pct降順で上位5件に絞られる（6件目は落ちる）
        rows = [
            rpc_row(f"カード{i}", "ノーマル", 1000, 1000 + i * 100, direction="up")
            for i in range(1, 7)  # pct: +10%, +20%, ..., +60%
        ]
        movers, _, _ = _select_featured_movers(rows, allowed={f"カード{i}" for i in range(1, 7)})
        assert len(movers) == 5
        assert [m["name"] for m in movers] == ["カード6", "カード5", "カード4", "カード3", "カード2"]

    def test_pct_tie_broken_by_card_name_ascending(self):
        # pctが同率の場合、card_name昇順で決定的に並ぶ（L3: ソートの決定性）
        rows = [
            rpc_row("Bカード", "ノーマル", 1000, 1500),  # pct=+50%
            rpc_row("Aカード", "ノーマル", 2000, 3000),  # pct=+50%（同率）
        ]
        movers, _, _ = _select_featured_movers(rows, allowed={"Aカード", "Bカード"})
        assert [m["name"] for m in movers] == ["Aカード", "Bカード"]

    def test_pct_as_string_is_cast_to_float(self):
        # RPCのnumeric型がクライアント経由で文字列で返るケースへの型防御（M2）
        row = rpc_row("新弾カードA", "ノーマル", 1000, 1500)
        row["pct"] = "50.0"  # 文字列で返るケースを模擬
        movers, _, _ = _select_featured_movers([row], allowed={"新弾カードA"})
        assert movers[0]["pct"] == 50.0
        assert isinstance(movers[0]["pct"], float)

    def test_no_matching_rows_returns_empty_and_no_dates(self):
        # 該当ゼロなら空リスト・日付もNone（フォールバック投稿は行わない仕様の裏付け）
        rows = [rpc_row("旧弾カード", "ノーマル", 1000, 1500, direction="up")]
        movers, date_old, date_new = _select_featured_movers(rows, allowed={"新弾カード"})
        assert movers == []
        assert date_old is None
        assert date_new is None

    def test_empty_rows_returns_empty(self):
        movers, date_old, date_new = _select_featured_movers([], allowed={"新弾カード"})
        assert movers == []
        assert date_old is None
        assert date_new is None

    def test_mover_dict_keys_match_format_featured_tweet(self):
        # format_featured_tweet が参照するキー（name/rarity/today/yesterday/diff/pct）を持つこと
        rows = [rpc_row("新弾カードA", "ノーマル", 1000, 1500)]
        movers, _, _ = _select_featured_movers(rows, allowed={"新弾カードA"})
        m = movers[0]
        assert m["name"] == "新弾カードA"
        assert m["rarity"] == "ノーマル"
        assert m["today"] == 1500
        assert m["yesterday"] == 1000
        assert m["diff"] == 500
        assert m["pct"] == 50.0


class TestFormatFeaturedTweet:
    """format_featured_tweet の出力文字列を実際に確認する（M3-b・M1のレアリティ表記込み）"""

    def test_output_includes_rarity_and_values(self):
        movers = [
            {"name": "青眼の白龍", "rarity": "シークレット", "today": 5180, "yesterday": 880, "diff": 4300, "pct": 488.6},
            {"name": "ブラック・マジシャン", "rarity": "ウルトラ", "today": 3000, "yesterday": 2000, "diff": 1000, "pct": 50.0},
        ]
        text = format_featured_tweet(list(movers), "テストパック", days_since_release=1,
                                      date_old="2026-08-05", date_new="2026-08-06")
        assert "【テストパック 値動き】発売2日目 8/5→8/6" in text
        assert "1. 青眼の白龍（シークレット） +488.6%(880→5,180円)" in text
        assert "2. ブラック・マジシャン（ウルトラ） +50.0%(2,000→3,000円)" in text
        assert "#遊戯王 #高騰" in text


# ──────────────────────────────────────────────
# post_featured_movers の通常モード分岐: RPC引数・例外時の早期return/Discord通知（M3-c）
# ──────────────────────────────────────────────

_FAKE_PACK = {"pack_name": "テストパック", "start_date": "2026-08-01"}


class _FakeRpcCall:
    """sb.rpc(...).execute() の戻りを模したスタブ。exc指定時は execute() で例外を送出する。"""

    def __init__(self, data=None, exc=None):
        self._data = data
        self._exc = exc

    def execute(self):
        if self._exc is not None:
            raise self._exc

        class _Resp:
            pass

        resp = _Resp()
        resp.data = self._data
        return resp


class _FakeSb:
    """rpc() 呼び出しの引数を記録するスタブ。table() は未実装のまま残す
    （_already_posted_today 側の try/except がAttributeErrorを捕捉しFalse扱いに
    フォールバックするため、投稿済みチェックの動作自体はこのテストの対象外）。"""

    def __init__(self, rpc_data=None, rpc_exc=None):
        self.rpc_calls = []
        self._rpc_data = rpc_data
        self._rpc_exc = rpc_exc

    def rpc(self, name, params):
        self.rpc_calls.append((name, dict(params)))
        return _FakeRpcCall(data=self._rpc_data, exc=self._rpc_exc)


def _patch_featured_pack(monkeypatch, days_since_release=2, cards=None):
    """post_featured_movers内のローカルimport（from featured_pack import ...）が
    実行時に参照する featured_pack モジュールの属性を差し替える。"""
    monkeypatch.setattr(featured_pack, "get_featured_pack", lambda sb: _FAKE_PACK)
    monkeypatch.setattr(featured_pack, "is_within_window", lambda pack: True)
    monkeypatch.setattr(featured_pack, "get_days_since_release", lambda pack: days_since_release)
    monkeypatch.setattr(featured_pack, "get_featured_cards", lambda sb, pack: cards or ["新弾カードA"])


class TestPostFeaturedMoversRpcIntegration:
    """post_featured_movers の通常モード分岐（RPCベース化後）の配線を固定する"""

    def test_rpc_called_with_expected_args(self, monkeypatch):
        # RPC結果を空にして画像取得等（ネットワークI/O）に到達する前に return させる
        _patch_featured_pack(monkeypatch)
        sb = _FakeSb(rpc_data=[])

        post_featured_movers(sb)

        assert len(sb.rpc_calls) == 1
        name, params = sb.rpc_calls[0]
        assert name == "get_price_movers"
        assert params["min_diff"] == 50
        assert params["top_n"] == 250          # H2: 500→250
        assert params["per_card"] == 1
        assert "cutoff_date" in params

    def test_rpc_exception_returns_early_and_notifies_discord(self, monkeypatch):
        _patch_featured_pack(monkeypatch)
        notified = []
        monkeypatch.setattr(discord_notify, "send_discord_message", lambda msg: notified.append(msg))
        sb = _FakeSb(rpc_exc=RuntimeError("接続失敗"))

        post_featured_movers(sb)  # 例外を送出せず早期returnすること

        assert len(sb.rpc_calls) == 1
        assert len(notified) == 1
        assert "get_price_movers" in notified[0]

    def test_output_without_rarity_has_no_parentheses(self):
        # rarity が空文字の場合は括弧表記を出さない（format_bigmove_tweet と同じ流儀）
        movers = [{"name": "レアリティ不明カード", "rarity": "", "today": 200, "yesterday": 100, "diff": 100, "pct": 100.0}]
        text = format_featured_tweet(list(movers), "テストパック", days_since_release=0)
        assert "1. レアリティ不明カード +100.0%(100→200円)" in text
