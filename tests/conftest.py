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
