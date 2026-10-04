"""公式商品ページの根拠だけで発売日を解決する。AIや他カードの値は使わない。"""

import json
import logging
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from datetime import date, datetime, timezone
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)
PRODUCTS_URL = "https://www.yugioh-card.com/japan/products/"
# 既存pack_scraperの商品一覧キャッシュと同じ鮮度上限。
_CACHE_TTL = 12 * 60 * 60
_MAX_BYTES = 2 * 1024 * 1024
_cache = OrderedDict()
_cache_lock = threading.Lock()


def _normalize_name(value):
    value = unicodedata.normalize("NFKC", value)
    value = value.translate(str.maketrans("‐‑‒–—−", "------"))
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"\s*-\s*", "-", value)
    return value.removeprefix("遊戯王OCGデュエルモンスターズ ").strip()


def _validate_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "www.yugioh-card.com"
            or parsed.query or parsed.fragment
            or not re.fullmatch(r"/japan/products/(?:[A-Za-z0-9_-]+/)*", parsed.path)):
        raise ValueError("公式商品ページ以外のURLは取得しない")


def _fetch_html(url):
    """各リダイレクトの送信前に検証し、展開後の本文サイズも制限する。"""
    for _ in range(4):
        _validate_url(url)
        with requests.get(url, timeout=(5, 15), stream=True, allow_redirects=False) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location:
                    raise ValueError("リダイレクト先が不明")
                url = urljoin(url, location)
                continue
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_content(64 * 1024):
                data.extend(chunk)
                if len(data) > _MAX_BYTES:
                    raise ValueError("公式ページのサイズ上限超過")
            return data.decode("utf-8", errors="strict")
    raise ValueError("公式ページのリダイレクト上限超過")


def _cached_html(url):
    # 同時リクエストによる重複取得を防ぎ、メモリ使用量にも上限を設ける。
    with _cache_lock:
        now = time.monotonic()
        cached = _cache.get(url)
        if cached and now - cached[0] < _CACHE_TTL:
            _cache.move_to_end(url)
            return cached[1], cached[2]
        html = _fetch_html(url)
        checked_at = datetime.now(timezone.utc).isoformat()
        _cache[url] = (time.monotonic(), html, checked_at)
        _cache.move_to_end(url)
        while len(_cache) > 128:
            _cache.popitem(last=False)
        return html, checked_at


def _field(body, name):
    match = re.search(r'"' + re.escape(name) + r'"\s*:\s*("(?:[^"\\]|\\.)*")', body)
    return json.loads(match.group(1)) if match else ""


def _dates(text):
    matches = re.findall(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日", text)
    return {date(*map(int, match)).isoformat() for match in matches}


def resolve_release_date(product_name: str, *, source_text: str = "") -> dict:
    """verifiedだけが確定値。本文の日付は商品との対応を保証できず採用しない。"""
    result = {"release_date": None, "status": "unverified", "source_url": None,
              "evidence": "", "checked_at": datetime.now(timezone.utc).isoformat(),
              "reason": "公式商品名と発売日を一意に確認できない"}
    if not isinstance(product_name, str) or not product_name.strip():
        result["reason"] = "商品名が未指定"
        return result
    name = _normalize_name(product_name)
    try:
        index, checked_at = _cached_html(PRODUCTS_URL)
        result["checked_at"] = checked_at
        rows = re.findall(r"p\[\d+\]\s*=\s*\{([^}]+)\}", index, re.DOTALL)
        if not rows:
            raise ValueError("公式商品一覧の構造を認識できない")
        matches = [row for row in rows if _normalize_name(_field(row, "title")) == name]
        if not matches:
            result["reason"] = "公式商品一覧に一致する商品がない（付録・特典等は手動確認）"
            return result
        urls = set()
        list_dates = set()
        for row in matches:
            path = _field(row, "url")
            if not path or path.startswith("#"):
                result["reason"] = "一致商品の独立した公式詳細ページがない"
                return result
            url = urljoin(PRODUCTS_URL, path)
            if not url.endswith("/"):
                url += "/"
            _validate_url(url)
            urls.add(url)
            list_dates.update(_dates(_field(row, "release-date")))
        if len(urls) != 1:
            result["reason"] = "同名商品が複数あり詳細ページを一意に選べない"
            return result
        result["source_url"] = next(iter(urls))
        html, checked_at = _cached_html(result["source_url"])
        result["checked_at"] = checked_at
        soup = BeautifulSoup(html, "html.parser")
        evidence = []
        values = set()
        for heading in soup.select("header h1"):
            if _normalize_name(heading.get_text(" ", strip=True)) != name:
                continue
            header = heading.find_parent("header")
            # 複数商品をまとめたheaderから別商品の発売日を拾わない。
            if len(header.select("h1")) != 1:
                continue
            for node in header.select(".release-date"):
                raw_date = node.get_text(" ", strip=True)
                evidence.append(raw_date)
                parsed = _dates(raw_date)
                if not parsed:
                    raise ValueError("発売日が完全な年月日で明示されていない")
                values.update(parsed)
        result["evidence"] = " / ".join(evidence)
        if not values:
            result["reason"] = "商品名が一致するheader内に発売日の根拠がない"
        elif len(values | list_dates) != 1:
            result.update(status="conflict", reason="公式の商品一覧または詳細ページに発売日の矛盾がある")
        else:
            result.update(release_date=next(iter(values)), status="verified",
                          reason="公式詳細ページの商品名と発売日を照合済み")
    except (requests.RequestException, ValueError, UnicodeError) as exc:
        logger.warning("公式発売日照合失敗: 商品=%r 理由=%s", product_name, exc)
        result["reason"] = f"公式発売日の取得・検証失敗: {exc}"
    return result
