"""
共通定数モジュール。
標準ライブラリのみ使用（外部依存ゼロ）。
GitHub Actions の各ワークフローから安全に import できる。
"""
from datetime import timezone, timedelta

# 日本標準時（JST = UTC+9）
JST = timezone(timedelta(hours=9))

# 価格履歴の保持期間（日数）
RETENTION_DAYS = 90

# Web Push 購読管理用 VAPID クレーム
VAPID_CLAIMS = {"sub": "mailto:tcg.price.compare@gmail.com"}
