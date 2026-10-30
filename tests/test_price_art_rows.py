"""絵ごとの内訳の記録（price_history_art・buyback_art。2026-10-30 イラスト違いの段2・案B の段2a-1）"""

import art_variant as av
import price_persist as pp

TODAY = "2026-10-30"


def _row(shop, name, rarity, price, code="", url="", **kw):
    return {"shop": shop, "name": name, "rarity": rarity, "price": price, "code": code, "url": url,
            "condition": "-", "sold_out": False, **kw}


def _keyed(rows):
    return {(r["shop"], r["rarity"], r["art"]): r["min_price"] for r in rows}


def test_split_rarity_writes_every_art_and_plain_rarity_writes_nothing():
    # 灰流うらら シークレットは元と違う絵が並ぶ → 店×絵ごとに書く。ノーマルは元の絵だけ → 書かない（本表と同じ値なので）
    rows = [
        _row("遊々亭", "灰流うらら", "シークレット", 420, "PAC1-JP016"),
        _row("遊々亭", "灰流うらら(イラスト違い版)", "シークレット", 980, "PAC1-JP016"),
        _row("カードラボ", "灰流うらら(新イラスト)", "シークレット", 1000, "PAC1-JP016"),
        _row("カードラボ", "灰流うらら", "シークレット", 450, "RC04-JP009"),
        _row("遊々亭", "灰流うらら", "ノーマル", 50, "SD43-JP001"),
    ]
    out = pp.build_art_price_rows("灰流うらら", rows, TODAY)
    assert _keyed(out) == {
        ("遊々亭", "シークレット", av.ART_BASE): 420,
        ("遊々亭", "シークレット", av.ART_ALT): 980,
        ("カードラボ", "シークレット", av.ART_ALT): 1000,
        ("カードラボ", "シークレット", av.ART_BASE): 450,
    }
    assert all(r["recorded_at"] == TODAY and r["card_name"] == "灰流うらら" for r in out)


def test_caller_rows_are_not_changed():
    rows = [_row("遊々亭", "灰流うらら(イラスト違い版)", "シークレット", 980)]
    pp.build_art_price_rows("灰流うらら", rows, TODAY)
    assert "art" not in rows[0] and "edition" not in rows[0]


def test_only_base_and_unknown_is_not_split():
    # まんぞく屋（絵を書き分けない＝判別できない）と元の絵だけなら、本表の値と同じなので書かない
    rows = [
        _row("遊々亭", "灰流うらら", "シークレット", 420),
        _row("まんぞく屋", "灰流うらら", "シークレット", 400),
    ]
    assert pp.build_art_price_rows("灰流うらら", rows, TODAY) == []


def test_edition_and_unknown_are_recorded_by_name():
    rows = [
        _row("カードラッシュ", "☆アジア☆灰流うらら", "シークレット", 300),
        _row("遊々亭", "灰流うらら", "シークレット", 420),
        _row("まんぞく屋", "灰流うらら", "シークレット", 400),
    ]
    assert _keyed(pp.build_art_price_rows("灰流うらら", rows, TODAY)) == {
        ("カードラッシュ", "シークレット", "アジア版"): 300,
        ("遊々亭", "シークレット", av.ART_BASE): 420,
        ("まんぞく屋", "シークレット", av.ART_UNKNOWN): 400,
    }


def test_alt_only_rarity_is_written():
    # 三戦の号のスーパーは違う絵しか無い。別表に書いておけば、絵を選ぶ画面は別表だけを読めばよい
    rows = [
        _row("カーナベル", "三戦の号(イラスト違い)", "スーパー", 30, "CH01-JP030"),
        _row("遊々亭", "三戦の号", "スーパー", 40, "CH01-JP030"),
    ]
    assert _keyed(pp.build_art_price_rows("三戦の号", rows, TODAY)) == {
        ("カーナベル", "スーパー", av.ART_ALT): 30,
        ("遊々亭", "スーパー", av.ART_ALT): 40,
    }


def test_sold_out_and_floor_rows_do_not_decide_split():
    rows = [
        _row("遊々亭", "灰流うらら", "シークレット", 420),
        _row("遊々亭", "灰流うらら(イラスト違い版)", "シークレット", 980, sold_out=True),
        _row("カードラボ", "灰流うらら(新イラスト)", "シークレット", 5),
    ]
    assert pp.build_art_price_rows("灰流うらら", rows, TODAY) == []


def test_normal_condition_and_any_price_per_art():
    # 最安の決め方は本表と同じ（通常品の最安 min_price・状態を問わない最安 min_price_any）
    rows = [
        _row("トレコロCB", "灰流うらら", "シークレット", 300, condition="中古キズあり"),
        _row("トレコロCB", "灰流うらら", "シークレット", 400),
        _row("遊々亭", "灰流うらら(イラスト違い版)", "シークレット", 980),
    ]
    out = {r["art"]: r for r in pp.build_art_price_rows("灰流うらら", rows, TODAY) if r["shop"] == "トレコロCB"}
    assert (out[av.ART_UNKNOWN]["min_price"], out[av.ART_UNKNOWN]["min_price_any"]) == (400, 300)


def test_many_arts_are_numbered_by_code(monkeypatch):
    # 絵が3種類以上のカードは、表の型番から何番目の絵か分かる違う絵に番号を付ける（青眼の白龍 KC01＝4・YAP1＝5）
    monkeypatch.setattr(av, "_art_table", lambda: {av._key("青眼の白龍"): (19, {"KC01-JP004": 4, "YAP1-JP001": 5})})
    rows = [
        _row("遊々亭", "青眼の白龍", "ウルトラ", 500, "LDS2-JP001"),
        _row("遊々亭", "青眼の白龍", "ウルトラ", 900, "KC01-JP004"),
        _row("カードラボ", "青眼の白龍", "ウルトラ", 1200, "YAP1-JP001"),
        _row("カーナベル", "青眼の白龍(イラスト違い)", "ウルトラ", 700, "XXXX-JP001"),
    ]
    assert _keyed(pp.build_art_price_rows("青眼の白龍", rows, TODAY)) == {
        ("遊々亭", "ウルトラ", av.ART_BASE): 500,
        ("遊々亭", "ウルトラ", "イラスト違い#4"): 900,
        ("カードラボ", "ウルトラ", "イラスト違い#5"): 1200,
        # 店の言葉だけの違う絵は何番目か分からない
        ("カーナベル", "ウルトラ", av.ART_ALT): 700,
    }


def test_two_art_card_keeps_plain_alt_name(monkeypatch):
    monkeypatch.setattr(av, "_art_table", lambda: {av._key("三戦の号"): (2, {"CH01-JP030": 2})})
    row = {**_row("遊々亭", "三戦の号", "スーパー", 40, "CH01-JP030"), "art": av.ART_ALT, "edition": ""}
    assert av.record_art(row, "三戦の号") == av.ART_ALT


def test_old_table_format_still_reads(monkeypatch, tmp_path):
    # 週1回の作り直しより前の表（型番の並び）でも読める（番号は不明＝番号なしの「イラスト違い」）
    path = tmp_path / "art_prints.json"
    path.write_text('{"三戦の号": ["CH01-JP030"]}', encoding="utf-8")
    monkeypatch.setattr(av, "_ART_PRINTS_PATH", path)
    av._art_table.cache_clear()
    try:
        assert av._art_prints() == {"三戦の号": frozenset({"CH01-JP030"})}
        assert av._art_numbers("三戦の号") == (0, {"CH01-JP030": 0})
    finally:
        av._art_table.cache_clear()


def test_buyback_art_rows_take_max_and_skip_sealed():
    rows = [
        _row("遊々亭", "灰流うらら", "シークレット", 100),
        _row("遊々亭", "灰流うらら(イラスト違い版)", "シークレット", 500),
        _row("遊々亭", "灰流うらら(イラスト違い版)", "シークレット", 450),
        _row("カードラッシュ", "(未開封)灰流うらら", "シークレット", 9000),
        _row("カードラッシュ", "灰流うらら", "シークレット", 300),
    ]
    out = {(r["shop"], r["art"]): r["max_price"] for r in pp.build_art_buyback_rows("灰流うらら", rows, TODAY)}
    assert out == {("遊々亭", av.ART_BASE): 100, ("遊々亭", av.ART_ALT): 500, ("カードラッシュ", av.ART_BASE): 300}


class _Resp:
    def __init__(self, data):
        self.data = data


class _SB:
    def __init__(self, exc=None, data=None):
        self.exc, self.data, self.calls = exc, data, []

    def rpc(self, fn, params):
        self.calls.append((fn, params))
        return self

    def execute(self):
        if self.exc:
            raise self.exc
        return _Resp(self.data)


def test_missing_rpc_disables_writes_for_process(monkeypatch):
    monkeypatch.setattr(pp, "_art_write_disabled", set())
    sb = _SB(exc=Exception("{'code': 'PGRST202', 'message': 'Could not find the function public.upsert_price_art_rows'}"))
    rows = [{"card_name": "x", "art": av.ART_ALT}]
    assert pp.upsert_price_art_rows(sb, rows) == 0
    assert pp.upsert_price_art_rows(sb, rows) == 0
    assert len(sb.calls) == 1  # 2回目は呼ばない


def test_other_errors_do_not_raise(monkeypatch):
    monkeypatch.setattr(pp, "_art_write_disabled", set())
    sb = _SB(exc=Exception("timeout"))
    assert pp.upsert_buyback_art_rows(sb, [{"card_name": "x"}]) == 0
    assert pp.upsert_buyback_art_rows(sb, [{"card_name": "x"}]) == 0
    assert len(sb.calls) == 2  # 一時的な失敗では止めない


def test_saved_rows_and_empty():
    sb = _SB(data=[{"saved_rows": 3}])
    assert pp.upsert_price_art_rows(sb, [{}, {}, {}], ignore_duplicates=True) == 3
    assert sb.calls[0] == ("upsert_price_art_rows", {"p_rows": [{}, {}, {}], "p_ignore_duplicates": True})
    assert pp.upsert_price_art_rows(_SB(), []) == 0


# ── 10-30 reviewer 指摘の修正 ──

class _Guarded:
    """夜の収集の見張り付きクライアント（db_write_guard.GuardedSupabase）の代わり。rpc を呼ばれたら失敗にする"""

    def __init__(self, raw):
        self.raw = raw

    def rpc(self, *a, **k):
        raise AssertionError("別表の書き込みが見張りを通った")


def test_art_writes_bypass_db_guard(monkeypatch):
    monkeypatch.setattr(pp, "_art_write_disabled", set())
    raw = _SB(data=[{"saved_rows": 1}])
    assert pp.upsert_price_art_rows(_Guarded(raw), [{}]) == 1
    assert raw.calls and raw.calls[0][0] == "upsert_price_art_rows"


def test_db_unavailable_stops_art_writes(monkeypatch):
    from postgrest.exceptions import APIError
    monkeypatch.setattr(pp, "_art_write_disabled", set())
    sb = _SB(exc=APIError({"code": "503", "message": "Service Unavailable"}))
    assert pp.upsert_buyback_art_rows(sb, [{}]) == 0
    assert pp.upsert_buyback_art_rows(sb, [{}]) == 0
    assert len(sb.calls) == 1


def test_clabo_catalog_rows_follow_loop_context():
    # 夜の収集: ほかの店の行ではシークレットが分かれ（カーナベルのイラスト違い）、カードラボは元の絵だけ。
    # カードラボだけで判定すると「分かれない」になって抜けるので、ループ側の判定を渡してそろえる
    loop_rows = [
        _row("カーナベル", "灰流うらら(イラスト違い)", "シークレット", 900, "QCAC-JP050"),
        _row("遊々亭", "灰流うらら", "シークレット", 420, "PAC1-JP016"),
    ]
    ctx = pp.art_price_context("灰流うらら", loop_rows)
    assert ctx["rarities"] == {"シークレット"}
    clabo = [_row("カードラボ", "灰流うらら", "シークレット", 400, "PAC1-JP016")]
    assert pp.build_art_price_rows("灰流うらら", clabo, TODAY) == []
    assert _keyed(pp.build_art_price_rows("灰流うらら", clabo, TODAY, ctx)) == {
        ("カードラボ", "シークレット", av.ART_BASE): 400}


def test_clabo_catalog_rows_take_alt_codes_from_loop():
    # 型番ごと違う絵の再録（カーナベルだけが書く）は、カードラボの無印の行も違う絵にそろえる
    loop_rows = [
        _row("カーナベル", "三戦の号(イラスト違い)", "スーパー", 30, "CH01-JP030"),
        _row("遊々亭", "三戦の号", "スーパー", 40, "CH01-JP030"),
    ]
    ctx = pp.art_price_context("三戦の号", loop_rows)
    clabo = [_row("カードラボ", "三戦の号", "スーパー", 35, "CH01-JP030")]
    assert _keyed(pp.build_art_price_rows("三戦の号", clabo, TODAY, ctx)) == {
        ("カードラボ", "スーパー", av.ART_ALT): 35}


def test_no_number_when_only_one_japanese_alt_art(monkeypatch):
    # Yugipedia の絵の数は英語版・ゲームの絵も数える。日本の型番から届く違う絵が1種類なら番号を付けない
    monkeypatch.setattr(av, "_art_table", lambda: {av._key("x"): (3, {"AAAA-JP001": 2})})
    row = {"shop": "遊々亭", "name": "x", "rarity": "ウルトラ", "code": "AAAA-JP001", "art": av.ART_ALT, "edition": ""}
    assert av.record_art(row, "x") == av.ART_ALT


def test_broken_table_entry_is_skipped(monkeypatch, tmp_path):
    path = tmp_path / "art_prints.json"
    path.write_text('{"a": {"arts": 2, "codes": {"X-JP1": "bad"}}, "b": {"arts": 2, "codes": {"Y-JP1": 2}}}',
                    encoding="utf-8")
    monkeypatch.setattr(av, "_ART_PRINTS_PATH", path)
    av._art_table.cache_clear()
    try:
        assert av._art_numbers("a") == (0, {})
        assert av._art_numbers("b") == (2, {"Y-JP1": 2})
    finally:
        av._art_table.cache_clear()


def test_buyback_empty_rarity_is_unknown_label():
    rows = [
        _row("遊々亭", "灰流うらら", "", 100),
        _row("遊々亭", "灰流うらら(イラスト違い版)", "", 500),
    ]
    assert {r["rarity"] for r in pp.build_art_buyback_rows("灰流うらら", rows, TODAY)} == {"(不明)"}
