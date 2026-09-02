"""
tests/test_featured_pack_table_missing.py — featured_pack テーブル未作成時の
問い合わせ抑制（2026-09-02）

背景:
  featured_pack テーブルは手動オーバーライド用で未作成であることが設計上許容
  されているが、get_featured_pack は毎回問い合わせて例外→print していたため、
  本番で1日24回404ログが出ていた。テーブル未作成と判定できるエラー
  （.code が PGRST205/PGRST202/42P01、または取れない場合の文字列一致
  PGRST205/42P01）のときだけ判定時刻を記憶し、1時間は sb.table("featured_pack")
  を呼ばない。

  2026-09-02 レビュー差し戻し: 当初 "does not exist" の文字列一致も条件に
  含めていたが、これは列不存在（42703 等）まで「テーブル未作成」と誤判定して
  しまうため外した。

テスト方針: ネットワーク不使用。sb を最小限のフェイクで模倣する。
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import featured_pack


class _FakeQuery:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def lte(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        if self._exc is not None:
            raise self._exc
        return SimpleNamespace(data=self._response or [])


class _FakeSb:
    """table() 呼び出しを記録するフェイク。pack_list は常に空を返す
    （テストの主眼は featured_pack への問い合わせ抑制のため）。"""

    def __init__(self, featured_exc=None):
        self.featured_exc = featured_exc
        self.table_calls: list = []

    def table(self, name):
        self.table_calls.append(name)
        if name == "featured_pack":
            return _FakeQuery(exc=self.featured_exc)
        if name == "pack_list":
            return _FakeQuery(response=[])
        raise AssertionError(f"想定外のテーブル呼び出し: {name}")


@pytest.fixture(autouse=True)
def _reset_missing_state(monkeypatch):
    """モジュール変数はプロセス内で共有されるため、テスト間で必ずリセットする"""
    monkeypatch.setattr(featured_pack, "_FEATURED_PACK_TABLE_MISSING_SINCE", None)
    yield


def test_table_missing_error_is_remembered_and_skips_next_query(monkeypatch):
    """PGRST205（テーブル未作成）の場合、判定後は sb.table('featured_pack') が
    呼ばれなくなる"""
    exc = Exception('{"code":"PGRST205","message":"Could not find the table '
                     "'public.featured_pack' in the schema cache\"}")
    sb = _FakeSb(featured_exc=exc)

    result1 = featured_pack.get_featured_pack(sb)
    assert result1 is None
    assert sb.table_calls == ["featured_pack", "pack_list"]

    sb.table_calls.clear()
    result2 = featured_pack.get_featured_pack(sb)
    assert result2 is None
    # 未作成と判定済みのため featured_pack への問い合わせはスキップされる
    assert sb.table_calls == ["pack_list"]


def test_transient_error_is_not_remembered_and_retries_every_time(monkeypatch):
    """テーブル未作成と断定できない一過性エラーは毎回リトライする（記憶しない）"""
    exc = Exception("connection reset by peer")
    sb = _FakeSb(featured_exc=exc)

    featured_pack.get_featured_pack(sb)
    assert sb.table_calls == ["featured_pack", "pack_list"]

    sb.table_calls.clear()
    featured_pack.get_featured_pack(sb)
    # 一過性エラーは記憶されないため、次回も featured_pack への問い合わせが発生する
    assert sb.table_calls == ["featured_pack", "pack_list"]


class _FakeAPIError(Exception):
    """postgrest.exceptions.APIError の .code 属性だけを模したフェイク"""

    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code


def test_table_missing_error_detected_via_code_attribute(monkeypatch):
    """.code 属性を持つ例外（postgrest APIError相当）でも PGRST205 は記憶される"""
    exc = _FakeAPIError("PGRST205", "Could not find the table 'public.featured_pack'")
    sb = _FakeSb(featured_exc=exc)

    featured_pack.get_featured_pack(sb)
    sb.table_calls.clear()
    featured_pack.get_featured_pack(sb)
    assert sb.table_calls == ["pack_list"]


def test_column_missing_error_is_not_remembered(monkeypatch):
    """42703（列不存在）はテーブル未作成ではないため記憶せず、毎回リトライする
    （2026-09-02 レビュー差し戻し: "does not exist" 文字列一致で誤判定していた
    バグの回帰テスト）"""
    exc = _FakeAPIError("42703", 'column "window_days" does not exist')
    sb = _FakeSb(featured_exc=exc)

    featured_pack.get_featured_pack(sb)
    assert sb.table_calls == ["featured_pack", "pack_list"]

    sb.table_calls.clear()
    featured_pack.get_featured_pack(sb)
    # 記憶されていないため、次回も featured_pack への問い合わせが発生する
    assert sb.table_calls == ["featured_pack", "pack_list"]
