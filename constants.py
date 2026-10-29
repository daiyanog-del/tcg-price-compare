"""
共通定数モジュール。
標準ライブラリのみ使用（外部依存ゼロ）。
GitHub Actions の各ワークフローから安全に import できる。
"""
from datetime import timezone, timedelta

# 日本標準時（JST = UTC+9）
JST = timezone(timedelta(hours=9))

# 保持期間（日数）。2026-10-22 DB容量（無料枠500MB）対策で表ごとに分けた（docs/decisions.md 同日）
# 販売価格の店舗別の行（price_history_v2）。値動き・購入候補など店舗別の値を読む機能は最長30日
PRICE_RETENTION_DAYS = 30
# 販売価格の「日×カード×レアリティの全店最安」（price_daily_min）。相場グラフと汎用カードX投稿が読む
PRICE_DAILY_RETENTION_DAYS = 100
# 買取価格（buyback_history）。読むのは買取の値動き（3日）と収集の見張り（15日）だけ
BUYBACK_RETENTION_DAYS = 30
# 一人回しリプレイ（非公開分）
REPLAY_RETENTION_DAYS = 90

# 共有リプレイの画像辞書に実URLのまま残してよい画像（発売済みカードの画像。それ以外の URL は card://カード名 に直す）。
# (発行元, パスの形の正規表現)。発行元だけで許すと、その発行元にある転送用の URL などを通してしまうため、出している形に絞る。
# static/solitaire/js/utils/replay-image-policy.js の ALLOWED_IMAGE_SOURCES と同じ値にする（tests/test_replay_image_policy.py が確かめる）
REPLAY_IMAGE_SOURCES = (
    ("https://artworks-jp-n.ygoresources.com", r"^/\d+/\d+/\d+_1\.png$"),   # app.py _ygores_image_url
    ("https://yg-card-images.ka-nabell.com", r"^/card/card\d+_\d+\.jpg$"),  # scraper.py kanabell_card_image_url（予備）
)

# Web Push 購読管理用 VAPID クレーム
VAPID_CLAIMS = {"sub": "mailto:tcg.price.compare@gmail.com"}
