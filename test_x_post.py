"""X投稿のみテスト — 価格収集をスキップして投稿だけ確認"""
import os
from supabase import create_client
from x_poster import post_daily_movers

sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
post_daily_movers(sb)
