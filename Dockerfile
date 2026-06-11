FROM python:3.12-slim

WORKDIR /app

# 日本語フォント（deck_image.py のデッキ画像生成で使用）
RUN apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

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
