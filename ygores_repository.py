"""
ygoresources データアクセス層
==============================
db.ygoresources.com への知識（URL・レスポンススキーマ・ヘッダ処理）をこのモジュールに
閉じ込める。アプリ側コードは CardDataRepository の返す正規化済みデータのみに依存し、
将来データソースを差し替えられるようにする。

公式の利用作法（https://db.ygoresources.com/about/api）:
- 必要なデータのみ取得し、ローカル（Supabase）にキャッシュする
- 全レスポンスの X-Cache-Revision を記録し、/manifest/<revision> で差分同期する
- 全件クロールはしない（初期投入は運営提供ダンプ → import_ygores_dump.py）

障害分離:
- 読み出しは常に Supabase キャッシュ優先。API障害時はキャッシュで応答し、ユーザー向け
  エラーにしない
- レートリミット（同時実行1・1秒間隔）、指数バックオフ、サーキットブレーカー内蔵
- 環境変数 YGORES_API_DISABLED=1 で外部APIを完全遮断できる（障害試験・緊急停止用）
"""

import os
import time
import logging
import threading
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

YGORES_BASE_URL = "https://db.ygoresources.com"
USER_AGENT = "card-souba/1.0 (contact: daiyanog@gmail.com)"

# 名前索引のパス（manifest の変更パス表記・ygores_blobs.path と一致させる）
NAME_INDEX_PATH = "idx/card/name/ja"
# revision 確認用の軽量エンドポイント（青眼の白龍、約20KB。全レスポンスに X-Cache-Revision が付く）
REVISION_CHECK_PATH = "data/card/4007"

MIN_REQUEST_INTERVAL_SEC = 1.0   # クライアント側レートリミット（同時実行1・最低1秒間隔）
MAX_ATTEMPTS = 3                 # 1リクエストあたりの最大試行回数
BACKOFF_BASE_SEC = 1.0           # 指数バックオフ: 1s → 2s → 4s
CIRCUIT_FAILURE_THRESHOLD = 5    # 連続失敗がこの回数に達したらAPIアクセスを一時停止
CIRCUIT_COOLDOWN_SEC = 600       # 停止時間（10分）


class YgoResourcesClient:
    """HTTP層。ygoresources のURLを知るのはこのクラスだけ。"""

    def __init__(self, session=None):
        self._session = session or requests.Session()
        self._lock = threading.Lock()        # 同時実行1を強制
        self._last_request_time = 0.0
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self.last_revision = None            # 最後に観測した X-Cache-Revision

    def api_available(self) -> bool:
        if os.environ.get("YGORES_API_DISABLED") == "1":
            return False
        return time.time() >= self._circuit_open_until

    def get_json(self, path: str, timeout: int = 15):
        """GET /<path> → dict。失敗時は None（呼び出し側はキャッシュで応答する）。"""
        if os.environ.get("YGORES_API_DISABLED") == "1":
            return None
        if time.time() < self._circuit_open_until:
            return None
        url = f"{YGORES_BASE_URL}/{path}"
        for attempt in range(MAX_ATTEMPTS):
            try:
                # ロック保持中にリクエストすることで同時実行1とリクエスト間隔を両立する
                with self._lock:
                    wait = self._last_request_time + MIN_REQUEST_INTERVAL_SEC - time.time()
                    if wait > 0:
                        time.sleep(wait)
                    resp = self._session.get(
                        url, timeout=timeout, headers={"User-Agent": USER_AGENT}
                    )
                    self._last_request_time = time.time()
                rev = resp.headers.get("X-Cache-Revision", "")
                if rev.isdigit():
                    self.last_revision = int(rev)
                if resp.status_code == 200:
                    self._record_success()
                    return resp.json()
                if resp.status_code == 403:
                    # ブロック疑い。リトライしても変わらないため即座に諦める
                    logger.error(f"[ygores] 403 Forbidden（ブロック疑い）: {path}")
                    self._record_failure()
                    return None
                if resp.status_code == 404:
                    # 存在しないID等。サーバーは正常なので失敗にカウントしない
                    self._record_success()
                    return None
                # 429/5xx → バックオフ後に再試行（即リトライしない）
                logger.warning(
                    f"[ygores] HTTP {resp.status_code}: {path} "
                    f"(試行 {attempt + 1}/{MAX_ATTEMPTS})"
                )
            except Exception as e:
                logger.warning(
                    f"[ygores] リクエスト失敗: {path}: {e} (試行 {attempt + 1}/{MAX_ATTEMPTS})"
                )
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(BACKOFF_BASE_SEC * (2 ** attempt))
        self._record_failure()
        return None

    def _record_success(self):
        self._consecutive_failures = 0

    def _record_failure(self):
        self._consecutive_failures += 1
        if self._consecutive_failures >= CIRCUIT_FAILURE_THRESHOLD:
            self._circuit_open_until = time.time() + CIRCUIT_COOLDOWN_SEC
            self._consecutive_failures = 0
            logger.error(
                f"[ygores] 連続{CIRCUIT_FAILURE_THRESHOLD}回失敗 — "
                f"{CIRCUIT_COOLDOWN_SEC}秒間APIアクセスを停止します（サーキットブレーカー）"
            )


class CardDataRepository:
    """アプリが使う唯一の窓口。読み出しは Supabase キャッシュ優先 → API → write-through。"""

    def __init__(self, client=None, supabase_client=None):
        self.client = client or YgoResourcesClient()
        self._sb = supabase_client
        self._sb_resolved = supabase_client is not None
        self._sb_lock = threading.Lock()

    # ------------------------------------------------------------------ Supabase
    def _supabase(self):
        """環境変数から遅延生成（Webアプリ・GitHub Actions のどちらからでも使えるように）"""
        with self._sb_lock:
            if self._sb_resolved:
                return self._sb
            self._sb_resolved = True
            url = os.environ.get("SUPABASE_URL")
            key = os.environ.get("SUPABASE_KEY")
            if url and key:
                try:
                    from supabase import create_client
                    self._sb = create_client(url, key)
                except Exception as e:
                    logger.warning(f"[ygores] Supabase接続失敗（キャッシュなしで動作）: {e}")
            return self._sb

    def _cache_select(self, table, key_col, key, value_col="raw"):
        sb = self._supabase()
        if sb is None:
            return None
        try:
            rows = (sb.table(table).select(value_col)
                    .eq(key_col, key).limit(1).execute().data)
            return rows[0][value_col] if rows else None
        except Exception as e:
            logger.warning(f"[ygores] キャッシュ読み出し失敗 {table}/{key}: {e}")
            return None

    def _cache_upsert(self, table, row):
        sb = self._supabase()
        if sb is None:
            return False
        try:
            sb.table(table).upsert(row).execute()
            return True
        except Exception as e:
            logger.warning(f"[ygores] キャッシュ書き込み失敗 {table}: {e}")
            return False

    def _select_all_keys(self, table, column):
        """テーブルの主キー一覧（PostgRESTの1000行制限があるためページング）"""
        sb = self._supabase()
        if sb is None:
            return []
        out = []
        page = 1000
        start = 0
        try:
            while True:
                rows = (sb.table(table).select(column).order(column)
                        .range(start, start + page - 1).execute().data)
                out.extend(r[column] for r in rows)
                if len(rows) < page:
                    break
                start += page
        except Exception as e:
            logger.warning(f"[ygores] 一覧取得失敗 {table}: {e}")
        return out

    # ------------------------------------------------------------------ 読み出し
    def get_card_raw(self, konami_id: int):
        """/data/card/<id> の生JSON。キャッシュ優先、ミス時はAPI→write-through。失敗時None"""
        raw = self._cache_select("ygores_cards", "konami_id", konami_id)
        if raw is not None:
            return raw
        data = self.client.get_json(f"data/card/{konami_id}", timeout=10)
        if data is not None:
            self.save_card(konami_id, data)
        return data

    # EXデッキの召喚種別（API形式=propertiesの整数コード / ミラー形式=日本語文字列）
    _EX_PROP_IDS = {11, 18, 19, 23}   # 11=融合,18=エクシーズ,19=シンクロ,23=リンク
    _EX_PROP_JA = ("融合", "シンクロ", "エクシーズ", "リンク")

    def get_card_summary(self, konami_id: int):
        """正規化済み内部モデル。ygoresources のスキーマ知識はここまでで吸収する。
        ライブAPI形式（/data/card）と GitHubミラー形式（yugioh-card-history）の
        両方を受け付け、同じ形のdictに正規化する。返り値dict または None。
        """
        raw = self.get_card_raw(konami_id)
        if not raw:
            return None
        return self._summarize(konami_id, raw)

    @classmethod
    def _summarize(cls, konami_id, raw):
        # --- ライブAPI形式（{"cardData": {"ja": {...}}}）---
        if isinstance(raw, dict) and "cardData" in raw:
            ja = (raw.get("cardData") or {}).get("ja") or {}
            if not ja:
                return None
            props = ja.get("properties") or []
            return {
                "konami_id": konami_id,
                "name": ja.get("name", ""),
                "card_type": ja.get("cardType", ""),   # "monster" | "spell" | "trap"
                "atk": ja.get("atk"),
                "def": ja.get("def"),
                "level": ja.get("level"),
                "rank": ja.get("rank"),
                "link_rating": ja.get("linkRating"),
                "attribute": ja.get("attribute"),      # 英語キー（"light"等）
                "property": ja.get("property"),         # 英語キー（"quickplay"等）
                "effect_text": ja.get("effectText", ""),
                "prints": ja.get("prints") or [],
                "is_ex": any(p in cls._EX_PROP_IDS for p in props),
                "race_ja": None,                        # APIは日本語種族を持たない
            }
        # --- GitHubミラー形式（{"id","type","name","properties":[日本語]...}）---
        if isinstance(raw, dict) and "type" in raw and "name" in raw:
            broad = raw.get("type", "")               # "monster" | "spell" | "trap"
            props = raw.get("properties") or []        # 例: ["ドラゴン族","シンクロ","効果"]
            is_monster = broad == "monster"
            return {
                "konami_id": konami_id,
                "name": raw.get("name", ""),
                "card_type": broad,
                "atk": raw.get("atk"),
                "def": raw.get("def"),
                "level": raw.get("level"),
                "rank": raw.get("rank"),
                "link_rating": raw.get("linkRating"),
                "attribute": raw.get("englishAttribute"),   # "light" / "spell" / "trap"
                "property": raw.get("englishProperty"),      # "quickplay" / "continuous" / None
                "effect_text": raw.get("effectText", ""),
                "prints": [],                                # ミラーは収録情報を持たない
                "is_ex": is_monster and any(s in props for s in cls._EX_PROP_JA),
                "race_ja": (props[0] if (is_monster and props) else None),
            }
        return None

    def get_name_index(self) -> dict:
        """日本語カード名→[konami_id] の索引。キャッシュ優先。両方失敗時は {}"""
        raw = self._cache_select("ygores_blobs", "path", NAME_INDEX_PATH)
        if raw is not None:
            return raw
        data = self.client.get_json(f"data/{NAME_INDEX_PATH}", timeout=30)
        if data is not None:
            self.save_blob(NAME_INDEX_PATH, data)
            return data
        return {}

    def get_qa_raw(self, qa_id: int):
        """/data/qa/<id> の生JSON（一人回しツール基盤用に先行整備）。失敗時None"""
        raw = self._cache_select("ygores_qa", "qa_id", qa_id)
        if raw is not None:
            return raw
        data = self.client.get_json(f"data/qa/{qa_id}", timeout=10)
        if data is not None:
            self.save_qa(qa_id, data)
        return data

    # ------------------------------------------------------------------ 書き込み（同期ジョブ・importer用）
    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @classmethod
    def _card_row(cls, konami_id, raw, now):
        """raw（API形式/ミラー形式どちらでも）から ygores_cards の行dictを作る"""
        s = cls._summarize(konami_id, raw) or {}
        return {
            "konami_id": int(konami_id),
            "name": s.get("name", ""),
            "card_type": s.get("card_type", ""),
            "raw": raw,
            "fetched_at": now,
        }

    def save_card(self, konami_id: int, raw: dict) -> bool:
        return self._cache_upsert("ygores_cards", self._card_row(konami_id, raw, self._now_iso()))

    def save_cards_bulk(self, items) -> int:
        """(konami_id, raw) の列を一括upsert（ダンプimport用）。保存件数を返す。
        raw は API形式・GitHubミラー形式のどちらでもよい。"""
        sb = self._supabase()
        if sb is None:
            return 0
        now = self._now_iso()
        rows = [self._card_row(konami_id, raw, now) for konami_id, raw in items]
        saved = 0
        chunk = 500
        for i in range(0, len(rows), chunk):
            try:
                sb.table("ygores_cards").upsert(rows[i:i + chunk]).execute()
                saved += len(rows[i:i + chunk])
            except Exception as e:
                logger.warning(f"[ygores] 一括書き込み失敗 (offset {i}): {e}")
        return saved

    def save_blob(self, path: str, raw) -> bool:
        return self._cache_upsert("ygores_blobs", {
            "path": path, "raw": raw, "fetched_at": self._now_iso(),
        })

    def save_qa(self, qa_id: int, raw: dict) -> bool:
        return self._cache_upsert("ygores_qa", {
            "qa_id": qa_id, "raw": raw, "fetched_at": self._now_iso(),
        })

    def fetch_and_store_card(self, konami_id: int) -> bool:
        """キャッシュを見ずにAPIから再取得して保存（差分同期用）"""
        data = self.client.get_json(f"data/card/{konami_id}", timeout=10)
        if data is None:
            return False
        return self.save_card(konami_id, data)

    def fetch_and_store_blob(self, path: str) -> bool:
        data = self.client.get_json(f"data/{path}", timeout=30)
        if data is None:
            return False
        return self.save_blob(path, data)

    def fetch_and_store_qa(self, qa_id: int) -> bool:
        data = self.client.get_json(f"data/qa/{qa_id}", timeout=10)
        if data is None:
            return False
        return self.save_qa(qa_id, data)

    # ------------------------------------------------------------------ 同期メタデータ・保持パス一覧
    def cached_card_ids(self) -> list:
        return self._select_all_keys("ygores_cards", "konami_id")

    def cached_blob_paths(self) -> list:
        return self._select_all_keys("ygores_blobs", "path")

    def cached_qa_ids(self) -> list:
        return self._select_all_keys("ygores_qa", "qa_id")

    def get_sync_meta(self, key: str):
        return self._cache_select("ygores_sync_meta", "key", key, value_col="value")

    def set_sync_meta(self, key: str, value) -> bool:
        return self._cache_upsert("ygores_sync_meta", {
            "key": key, "value": str(value), "updated_at": self._now_iso(),
        })


# プロセス内で共有するシングルトン（app.py / バッチスクリプト共通）
repository = CardDataRepository()
