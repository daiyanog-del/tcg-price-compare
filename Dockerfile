# JS・CSS を縮小する段（2026-10-22 LCP）。リポジトリの static は読みやすいまま残し、本番のイメージにだけ縮小版を入れる。
# JS は空白・コメントの削除と書き方の短縮だけ（関数名などは変えない。HTML の onclick などから名前で呼ぶため）。
# 日本語は \uXXXX にしない（--charset=utf8）。brotli 後で JS 453→約229KB・CSS 106→53KB（スマホ相当の遅い回線で約0.7秒の差）
FROM node:20-slim AS assets
WORKDIR /src
RUN npm install -g esbuild@0.28.2
COPY static ./static
RUN find static -name '*.js' ! -name '*.min.js' -print0 | xargs -0 esbuild --minify-whitespace --minify-syntax --charset=utf8 --log-level=warning --outdir=/out --outbase=static \
 && find static -name '*.css' ! -name '*.min.css' -print0 | xargs -0 esbuild --minify --charset=utf8 --log-level=warning --outdir=/out --outbase=static

FROM python:3.12-slim

WORKDIR /app

# 日本語フォント（card_og_image.py・replay_og_image.py の共有画像で使用。取得は deck_image._get_font）
RUN apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# 縮小した JS・CSS で上書きする（上の assets 段）。?v= の内容ハッシュは縮小後の中身から作られる
COPY --from=assets /out/ ./static/

# Create cache directory
RUN mkdir -p .cache

# Non-root user
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

# glibcのスレッド別mallocアリーナ数を制限する。
# gthread(8スレッド)でHTML解析を繰り返すとアリーナが乱立し、解放済みメモリが
# OSに返却されずRSSが定常330MB前後まで膨らんでいた（2026-06 OOM調査で実測）。
ENV MALLOC_ARENA_MAX=2

EXPOSE 5000

CMD ["gunicorn", "app:app", \
     "--bind", "0.0.0.0:5000", \
     "--workers", "1", \
     "--threads", "8", \
     "--timeout", "120", \
     "--worker-class", "gthread", \
     "--access-logfile", "-"]
