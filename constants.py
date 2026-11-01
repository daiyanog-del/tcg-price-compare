"""
共通定数モジュール。
標準ライブラリのみ使用（外部依存ゼロ）。
GitHub Actions の各ワークフローから安全に import できる。
"""
from datetime import timezone, timedelta

# 値段の下限。これ未満は収集の異常値として捨てる（記録・グラフ・値動き・見積もりで共通）。
# 2026-03 にカーナベルの1円などで騰落率が壊れたため入れた。当初は「10円以下」を捨てていたが、
# 本物の ¥10 の出品（カーナベル新サイトの 墓穴の指名者 ノーマル 等）まで落とすので、
# 2026-10-29 に「10円未満」へ変えた。SQL 側（supabase_rpc_*.sql・price_daily_rollup.sql）は
# 同じ値を直書きしているので、変えるときは両方そろえる
PRICE_FLOOR = 10

# 日本標準時（JST = UTC+9）
JST = timezone(timedelta(hours=9))

# 保持期間（日数）。2026-10-22 DB容量（無料枠500MB）対策で表ごとに分けた（docs/decisions.md 同日）
# 販売価格の店舗別の行（price_history_v2）。値動き・購入候補など店舗別の値を読む機能は最長8日。
# 2026-11-01 に30日→14日（DB のメモリに収まらず Disk IO を使い切って止まったため。docs/decisions.md 同日）。
# 15日目以前を読む相場グラフ・汎用カードX投稿・購入候補の予備は日ごとの全店最安（price_daily）から読む
PRICE_RETENTION_DAYS = 14
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
