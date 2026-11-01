"""
tests/conftest.py — pytest 全体の共通設定。

app.py は import 時点で起動時プリロード／cache_warmer 等のバックグラウンドスレッドを
起動する（本番ではgunicornワーカー起動時の挙動として必要）。pytestはテスト収集時に
app.py を何度もimportするため、このガードが無いとテストのたびにSupabase未接続の
プリロード失敗ログが出たり、テスト終了後もdaemonスレッドが残り続けたりする
（レビュー指摘Medium・2026-09-22）。

conftest.py はテストモジュールのimportより先にpytestが読み込むため、ここで
環境変数を設定すれば各 test_*.py の `import app as app_module` 実行時には
既に DISABLE_STARTUP_JOBS=1 が見えている。
"""
import os

os.environ.setdefault("DISABLE_STARTUP_JOBS", "1")
# 店の一時休止（shop_status.py）は読み込み時に店の一覧を変える。手元に設定が残っていても、
# 既存のテスト（カーナベルを例に使うもの）が崩れないように外す。休止のテストは別プロセスで設定する
os.environ.pop("SUSPENDED_SHOPS", None)


import pytest  # noqa: E402


class _NoDbClient:
    """読み直し採用の設定（reread_trust）がテスト中に本物の DB を読まないようにする。読もうとすると失敗＝安全側（無効）。"""
    def table(self, *_a, **_k):
        raise RuntimeError("テスト中は app_settings に接続しない")


@pytest.fixture(autouse=True)
def _isolate_reread_trust(monkeypatch):
    import reread_trust
    monkeypatch.setattr(reread_trust, "_client_override", _NoDbClient())
    monkeypatch.setattr(reread_trust, "_forced_off", False)
    reread_trust.invalidate_cache()
    yield
    reread_trust.invalidate_cache()


@pytest.fixture(autouse=True)
def _no_shared_price_rows():
    """購入候補の価格・まとめ買い・レアリティ候補が使い回す読み込み（price_history_reads の shared）を、テストごとに空にする。
    残すと前のテストの偽 DB の行が次のテストに出る"""
    import price_history_reads
    price_history_reads._reset_shared_for_tests()
    yield
    price_history_reads._reset_shared_for_tests()


@pytest.fixture(autouse=True)
def _no_x_post_quiet_hours(monkeypatch):
    """X 投稿の「投稿しない時間帯」（既定 JST 1〜7時）でテスト結果が実行時刻に左右されないよう無効にする。
    時間帯そのもののテストは各テストで環境変数を設定し直す。"""
    monkeypatch.setenv("UNRELEASED_X_POST_QUIET_HOURS", "off")


@pytest.fixture(autouse=True)
def _no_shop_cache_kv(monkeypatch):
    """店舗検索キャッシュの Key Value（SHOP_CACHE_REDIS_URL）を、手元の環境変数に関係なくテスト中は使わない。
    Key Value の経路のテストは各テストで設定し直す（tests/test_shop_cache_kv.py）。"""
    import shop_cache_kv
    monkeypatch.delenv("SHOP_CACHE_REDIS_URL", raising=False)
    shop_cache_kv._reset_for_tests()
    yield
    shop_cache_kv._reset_for_tests()


@pytest.fixture(autouse=True)
def _no_live_pack_matrix_watch(request, monkeypatch):
    """新弾の収集（collect_featured.main）を動かすテストが、10時台に流すと表の見張りから本番サイトへ問い合わせていた
    （2026-11-01 本番のログに /api/packs/cards?name=P の 429 が並んだ）。見張りのテスト自身（test_pack_matrix_watch）以外は止める"""
    if request.module.__name__.endswith("test_pack_matrix_watch"):
        return
    import collect_featured
    monkeypatch.setattr(collect_featured, "watch_pack_matrix", lambda pack, now: None)
