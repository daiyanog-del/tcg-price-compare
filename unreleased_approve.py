"""未発売カードの承認処理（Flask 非依存）と、自動承認の実行。

2026-10-15 追加。これまで admin_unreleased.py の単体承認ルートの中にあった承認の本体を、Flask なしで呼べる関数
（approve_card）に切り出した。単体承認ルートはこれを呼ぶだけで、取込3経路（watch_x_unreleased.py・
watch_unreleased.py・admin の fetch-x-post）からは自動承認（maybe_auto_approve）経由で呼ぶ。

承認 ＝ 公開（status=approved, hidden=false）。承認処理の流れ（approve_card）:
  状態確認 → 発売日の補完と警告 → status と approved_at を同時に更新 → キャッシュ無効化 →
  承認記録（approval_snapshot）→ 画像取込 → 画像が取れたら X 投稿（この process に認証と有効化があれば即時。
  無ければ何もせず、回収ジョブ tools/post_unreleased_x.py が3時間ごとに拾う）

自動承認（maybe_auto_approve）:
  - 環境変数 AUTO_APPROVE_ENABLED=1 のときだけ動く（既定は無効。無効のときは何も読み書きしない）
  - 判定（extraction_raw.auto_approve_verdict.eligible）が通ったカードだけ approve_card(via="auto")
  - 1日(JST) AUTO_APPROVE_DAILY_LIMIT 件まで。超えた分は承認せず pending のまま、Discord に1日1回だけ通知
  - 承認のたびに Discord に通知（カード名・商品名・管理画面へのリンク）
  - 失敗しても取込は止めない（警告ログ＋Discord）。この関数は例外を投げない
"""

import logging
import os
from datetime import datetime, timezone
from urllib.parse import urlparse

import fetch_guard as _fetch_guard
import unreleased_x_post as _x_post
from constants import JST

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 承認処理
# ──────────────────────────────────────────────

# 承認時に読む列。後半は approval_snapshot（抽出時スナップショットとの差分）の計算に使う。
APPROVE_SELECT_COLUMNS = (
    "id, name, source_url, extraction_raw, status, release_date, product_name, "
    "card_type, attribute, race, level, rank, link_val, atk, def, "
    "effect_text, pendulum_scale, pendulum_effect"
)

# 承認できる元の状態
APPROVABLE_STATUSES = ("pending", "rejected", "needs_review")


def backfill_release_date(product_name: str, exclude_id: int) -> str | None:
    """他カードの多数決を使わず、商品に紐付く公式発売日だけを補完する。"""
    from release_date_resolver import resolve_release_date
    if not product_name:
        return None
    evidence = resolve_release_date(product_name)
    return evidence['release_date'] if evidence['status'] == 'verified' else None


def execute_approve_update(build_query, fields: dict):
    """status を approved にする UPDATE を実行する。fields には approved_at を含める。

    approved_at 列が未作成（SQL の適用前にコードだけ先にデプロイされた）ときは、承認自体を止めないよう
    approved_at 抜きで再実行する（警告ログ）。その間に承認されたカードは approved_at が NULL のため
    X に投稿されない。根本対応は unreleased_x_posts.sql の適用（手順は SQL 冒頭に記載）。
    build_query(fields) は execute 前のクエリを返す関数。
    """
    try:
        return build_query(fields).execute()
    except Exception as e:
        if "approved_at" not in fields or "approved_at" not in str(e):
            raise
        logger.warning(f"[approve] approved_at 列が無いため approved_at 抜きで承認します"
                       f"（unreleased_x_posts.sql を適用してください。それまで承認分は X に投稿されません）: {e}")
        return build_query({k: v for k, v in fields.items() if k != "approved_at"}).execute()


def _default_invalidate_cache():
    """公開サイト側の未発売カードのキャッシュを無効化する（別 process では効かないが、TTL 30秒で追いつく）。"""
    try:
        import card_display
        card_display.invalidate_cache()
    except Exception as e:
        logger.warning(f"[approve] キャッシュ無効化に失敗（TTLで追いつく）: {e}")


def approve_card(sb, card_id: int, via: str, *, fetch_image=None, invalidate_cache=None) -> dict:
    """指定IDのカードを承認する（status='approved'）。Flask 非依存。

    via: 承認の経路（"single" 管理画面の単体承認 / "auto" 自動承認）。approval_snapshot.via に記録する。
    fetch_image: (card_id=, source_url=, extraction_raw=) → (成功, 理由)。省略時は try_fetch_image_from_extraction。
    invalidate_cache: 引数なしの関数。省略時は card_display.invalidate_cache。

    戻り値 {"status": ...}:
      "not_found"      対象が無い
      "not_approvable" pending / rejected / needs_review 以外（既に承認済みなど）
      "update_failed"  承認の UPDATE が行を返さなかった
      "ok"             承認した。card / image_fetched / image_reason と、あれば release_date_note / release_date_warning
    DB エラーなどの想定外は例外のまま投げる（呼び出し側が扱う）。

    release_date ガード:
      - release_date が NULL の場合、商品名が一致する公式商品情報から補完を試みる
        （補完できれば release_date_note、できなければ release_date_warning を付加）
      - 有効な release_date が今日(JST)より前の場合、抽出時の年誤りの可能性を
        release_date_warning で警告する（承認自体はブロックしない）
    """
    from unreleased_auto_approve import record_approval_snapshots

    card_resp = sb.table("unreleased_cards").select(APPROVE_SELECT_COLUMNS).eq("id", card_id).execute()
    if not card_resp.data:
        return {"status": "not_found"}
    card_data = card_resp.data[0]
    if card_data["status"] not in APPROVABLE_STATUSES:
        return {"status": "not_approvable"}

    # release_date ガード判定
    release_date = card_data.get("release_date")
    product_name = card_data.get("product_name") or ""
    # approved_at は X 自動投稿の対象判定に使う。status と同じ UPDATE で入れる
    update_fields = {"status": "approved", "approved_at": datetime.now(timezone.utc).isoformat()}
    release_date_note = None
    release_date_warning = None

    if not release_date:
        backfilled = backfill_release_date(product_name, card_id)
        if backfilled:
            update_fields["release_date"] = backfilled
            release_date = backfilled
            release_date_note = f"発売日を公式商品情報から補完しました: {backfilled}"
            logger.info(
                f"[approve] release_date 補完: card_id={card_id}, "
                f"product_name={product_name!r}, release_date={backfilled}"
            )
        else:
            release_date_warning = (
                "発売日が未設定です。発売日を過ぎても未発売扱いのままになるため、"
                "判明したら編集で設定してください"
            )
            logger.warning(
                f"[approve] release_date 未設定のまま承認: card_id={card_id}, "
                f"product_name={product_name!r}"
            )

    if release_date:
        today = datetime.now(JST).strftime("%Y-%m-%d")
        if release_date < today:
            release_date_warning = (
                f"発売日が過去日です（{release_date}）。抽出時の年誤りの可能性があります"
            )
            logger.warning(
                f"[approve] release_date が過去日: card_id={card_id}, release_date={release_date}"
            )

    # ステータスを承認済みに更新（release_date 補完がある場合は同時に更新）
    upd_resp = execute_approve_update(
        lambda f: sb.table("unreleased_cards").update(f).eq("id", card_id),
        update_fields,
    )
    if not upd_resp.data:
        return {"status": "update_failed"}

    (invalidate_cache or _default_invalidate_cache)()

    # 承認時の記録（手直しの有無・切り抜き方法・判定結果・承認経路）。計測用で、失敗しても承認も投稿も止めない
    record_approval_snapshots(sb, [card_data], via)

    # 画像取込を試みる（失敗しても承認は成立）
    if fetch_image is None:
        def fetch_image(**kw):
            return try_fetch_image_from_extraction(sb, invalidate_cache=invalidate_cache, **kw)
    image_fetched, image_reason = fetch_image(
        card_id=card_id,
        source_url=card_data.get("source_url", ""),
        extraction_raw=card_data.get("extraction_raw") or {},
    )

    # X自動投稿（無効時・この process に認証が無いときは何もしない）。画像取込に成功したときだけ。
    # 失敗分・認証の無い process で承認した分は回収ジョブが拾う
    if image_fetched:
        _x_post.start_post_thread(sb, [card_id])

    result = {
        "status": "ok",
        "card": upd_resp.data[0],
        "image_fetched": image_fetched,
        "image_reason": image_reason,
    }
    if release_date_note:
        result["release_date_note"] = release_date_note
    if release_date_warning:
        result["release_date_warning"] = release_date_warning
    return result


# ──────────────────────────────────────────────
# 画像取込（承認時）
# ──────────────────────────────────────────────

def try_fetch_image_from_extraction(
    sb,
    card_id: int,
    source_url: str,
    extraction_raw: dict,
    invalidate_cache=None,
) -> tuple[bool, str]:
    """
    extraction_raw からこのカード個別の画像URLを決定して取り込む。

    X カード（source_domain="x.com"）は取り込み時点で画像保存済みのため、
    official_card_images に既存レコードがあればスキップする。

    URL の決定順:
      1. card_image_url（各カード固有の画像URL・新形式）があればそれを使う
      2. なければ card_image_urls（記事全体の画像URL一覧）の先頭を使う（後方互換）

    Returns:
        (成功フラグ, 理由文字列)
    """
    if not sb:
        return False, "Supabase 未接続"

    # 既存の画像レコードがあればスキップ（X取り込み時に保存済み）
    try:
        existing = (
            sb.table("official_card_images")
            .select("id")
            .eq("unreleased_card_id", card_id)
            .is_("deleted_at", "null")
            .execute()
        )
        if existing.data:
            logger.info(f"[approve] 画像取込済みのためスキップ: card_id={card_id}")
            return True, "取込済み（スキップ）"
    except Exception as e:
        logger.warning(f"[approve] 既存画像確認失敗（処理継続）: {e}")

    # 優先1: card_image_url（各カード固有・新形式）
    card_image_url = (extraction_raw.get("card_image_url") or "").strip()
    if card_image_url:
        logger.info(f"[approve] 画像URL決定（card_image_url）: {card_image_url!r}")
        return fetch_and_store_image(sb, card_id, card_image_url, source_url, invalidate_cache)

    # 優先2: card_image_urls の先頭（後方互換）
    card_image_urls = extraction_raw.get("card_image_urls", [])
    if card_image_urls:
        fallback_url = card_image_urls[0]
        logger.info(
            f"[approve] 画像URL決定（card_image_urls[0] フォールバック）: {fallback_url!r}"
        )
        return fetch_and_store_image(sb, card_id, fallback_url, source_url, invalidate_cache)

    return False, "card_image_url も card_image_urls も空です"


def fetch_and_store_image(
    sb,
    card_id: int,
    image_url: str,
    source_page_url: str,
    invalidate_cache=None,
) -> tuple[bool, str]:
    """
    指定URLの画像を取得してStorageに保存し、official_card_images にINSERTする。
    ホワイトリスト検証・Content-Type確認・5MB上限チェックを行う。

    Returns:
        (成功フラグ, 理由文字列)
    """
    if not sb:
        return False, "Supabase 未接続"

    # ホワイトリスト検証
    if not _fetch_guard.is_whitelisted(image_url):
        logger.warning(f"[approve] 画像URLがホワイトリスト外のためスキップ: {image_url!r}")
        return False, f"ホワイトリスト外のURL: {image_url!r}"

    # 画像取得
    try:
        resp = _fetch_guard.fetch_whitelisted(image_url)
    except _fetch_guard.WhitelistViolation as e:
        logger.warning(f"[approve] 画像取得ホワイトリスト違反: {e}")
        return False, f"ホワイトリスト違反: {e}"
    except Exception as e:
        logger.warning(f"[approve] 画像取得失敗: {e}")
        return False, f"取得エラー: {e}"

    # Content-Type の確認
    content_type = resp.headers.get("Content-Type", "")
    if not content_type.startswith("image/"):
        logger.warning(f"[approve] Content-Type が image/* でない: {content_type!r} ({image_url!r})")
        return False, f"Content-Type が image/* でない: {content_type!r}"

    # サイズ上限チェック（5MB）
    MAX_SIZE = 5 * 1024 * 1024
    content = resp.content
    if len(content) > MAX_SIZE:
        logger.warning(f"[approve] 画像が5MBを超えています: {len(content):,}バイト ({image_url!r})")
        return False, f"画像サイズ超過: {len(content):,}バイト（上限5MB）"

    # 拡張子を推定（Content-Type から）
    ext_map = {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
        "image/avif": "avif",
    }
    mime = content_type.split(";")[0].strip().lower()
    ext = ext_map.get(mime, "jpg")

    # Storage パスを決定
    storage_path = f"unreleased/{card_id}.{ext}"

    # Supabase Storage にアップロード（既存は上書き: upsert）
    try:
        # supabase-py: storage.from_(bucket).upload(path, data, options)
        # upsert に相当するには file_options で upsert=True を指定する
        sb.storage.from_("official-card-images").upload(
            path=storage_path,
            file=content,
            file_options={
                "content-type": mime,
                "upsert": "true",
            },
        )
    except Exception as e:
        # アップロードエラーをログに記録
        logger.error(f"[approve] Storage アップロード失敗: {e} (path={storage_path!r})")
        return False, f"Storage アップロード失敗: {e}"

    # public URL を取得
    try:
        url_resp = sb.storage.from_("official-card-images").get_public_url(storage_path)
        # supabase-py v2: get_public_url は文字列を返す
        if isinstance(url_resp, str):
            public_url = url_resp
        else:
            public_url = url_resp.get("publicUrl", "")
    except Exception as e:
        logger.warning(f"[approve] public URL 取得失敗: {e}")
        public_url = ""

    # source_domain を取得
    try:
        source_domain = urlparse(image_url).netloc
    except Exception:
        source_domain = ""

    # official_card_images に来歴付き INSERT
    try:
        sb.table("official_card_images").insert({
            "unreleased_card_id": card_id,
            "storage_path": storage_path,
            "public_url": public_url,
            "source_image_url": image_url,
            "source_page_url": source_page_url,
            "source_domain": source_domain,
        }).execute()
    except Exception as e:
        logger.error(f"[approve] official_card_images INSERT 失敗: {e}")
        return False, f"DB 登録失敗: {e}"

    (invalidate_cache or _default_invalidate_cache)()
    logger.info(f"[approve] 画像取込完了: card_id={card_id}, path={storage_path!r}")
    return True, f"取込成功: {storage_path}"


# ──────────────────────────────────────────────
# 自動承認
# ──────────────────────────────────────────────

AUTO_APPROVE_ENABLED_ENV = "AUTO_APPROVE_ENABLED"

# 1日(JST)に自動承認してよい件数の上限。誤判定が出たときの被害（そのまま公開・X投稿される）を抑える安全弁。
# 新弾の発表日は1日に十数枚まとまることがあるので、超えた分は pending のまま人が見る。
# TODO: calibrate from data（運用で1日の自動承認数と、自動承認後に非公開へ戻された数が溜まったら見直す）
AUTO_APPROVE_DAILY_LIMIT = 10

# 通知に載せる管理画面のURL
ADMIN_URL = "https://tcg-price-compare.onrender.com/admin"

# 上限到達の通知を1日1回にするための記録（app_settings のキー）。process をまたぐのでDBに持つ
_CAP_NOTIFIED_KEY = "AUTO_APPROVE_CAP_NOTIFIED"
_cap_notified_dates: set[str] = set()   # DBに書けなかったときの、同じ process 内の保険


def is_auto_approve_enabled() -> bool:
    """自動承認が有効か（既定は無効）。"""
    return os.environ.get(AUTO_APPROVE_ENABLED_ENV, "") == "1"


def _notify(message: str) -> None:
    """Discord 通知。メンションは無効化（カード名などの外部由来の文字列を載せるため）。失敗しても握りつぶす。"""
    try:
        from discord_notify import send_discord_message
        send_discord_message(message, no_mentions=True)
    except Exception as e:
        logger.warning("[auto_approve] Discord通知失敗: %s", e)


def _jst_day_start_utc(now: datetime | None = None) -> str:
    now = now or datetime.now(JST)
    start_jst = now.astimezone(JST).replace(hour=0, minute=0, second=0, microsecond=0)
    return start_jst.astimezone(timezone.utc).isoformat()


def count_auto_approved_today(sb, now: datetime | None = None) -> int:
    """JST の当日に自動承認した件数（approved_at が当日0時(JST)以降で approval_snapshot.via=="auto"）。

    後から非公開に戻した分も数える（上限は「その日に自動で出した件数」の安全弁のため）。
    読めなければ例外を投げる（呼び出し側は安全側＝承認しない、に倒す）。
    """
    start = _jst_day_start_utc(now)
    total, offset = 0, 0
    while True:
        rows = (sb.table("unreleased_cards").select("id, extraction_raw").gte("approved_at", start)
                .order("id").range(offset, offset + 999).execute().data or [])
        for r in rows:
            snap = (r.get("extraction_raw") or {}).get("approval_snapshot")
            if isinstance(snap, dict) and snap.get("via") == "auto":
                total += 1
        if len(rows) < 1000:
            return total
        offset += 1000


def _notify_cap_once(sb, today: str) -> None:
    """1日の上限に達したことを Discord に通知する。同じ日（JST）には1回だけ。"""
    if today in _cap_notified_dates:
        return
    try:
        resp = sb.table("app_settings").select("value").eq("key", _CAP_NOTIFIED_KEY).execute()
        if resp.data and (resp.data[0].get("value") or {}).get("date") == today:
            _cap_notified_dates.add(today)
            return
    except Exception as e:
        logger.warning("[auto_approve] 上限通知の記録を読めない（通知は出す）: %s", e)
    _cap_notified_dates.add(today)
    _notify(f"[自動承認] 本日(JST)の自動承認の上限 {AUTO_APPROVE_DAILY_LIMIT} 件に達しました。以降の取込分は承認待ちのままです"
            f"（管理画面で確認: {ADMIN_URL}）")
    try:
        sb.table("app_settings").upsert({"key": _CAP_NOTIFIED_KEY, "value": {"date": today}}, on_conflict="key").execute()
    except Exception as e:
        logger.warning("[auto_approve] 上限通知の記録に失敗（同じ日に重複通知する可能性）: %s", e)


def maybe_auto_approve(sb, cards, *, approve=None, now: datetime | None = None) -> dict:
    """取込・挿入した直後のカード行の中から、判定が通ったものを自動承認する。結果コードの件数を返す。

    cards: 挿入した行（id と extraction_raw.auto_approve_verdict を含むもの。name / product_name は通知に使う）。
    無効（AUTO_APPROVE_ENABLED が1でない）なら何もしない。例外は投げない（取込・since_id の進行を止めないため）。
    画像: X 経由は取込時に保存済み（呼び出し側は画像の保存後に呼ぶこと）、公式経由は approve_card が承認時に取り込む。
    戻り値のキー: approved / not_eligible / limit / failed / error
    """
    counts: dict[str, int] = {}

    def bump(k):
        counts[k] = counts.get(k, 0) + 1

    if not is_auto_approve_enabled():
        return counts
    try:
        candidates = []
        for card in cards or []:
            verdict = (card.get("extraction_raw") or {}).get("auto_approve_verdict")
            if card.get("id") is not None and isinstance(verdict, dict) and verdict.get("eligible") is True:
                candidates.append(card)
            else:
                bump("not_eligible")
        if not candidates:
            return counts
        today = (now or datetime.now(JST)).astimezone(JST).date().isoformat()
        try:
            done_today = count_auto_approved_today(sb, now)
        except Exception as e:
            # 上限を数えられないときは承認しない（安全側）。pending のまま人が見る
            logger.warning("[auto_approve] 本日の自動承認数を数えられないため自動承認しない（根本確認が必要）: %s", e)
            _notify(f"[自動承認] 本日の自動承認数を数えられなかったため、{len(candidates)}件を承認待ちのままにしました: {e}")
            counts["error"] = len(candidates)
            return counts
        approve = approve or approve_card
        for i, card in enumerate(candidates):
            if done_today >= AUTO_APPROVE_DAILY_LIMIT:
                logger.warning("[auto_approve] 本日(JST)の上限 %d 件に達したため残り%d件は承認待ちのまま",
                               AUTO_APPROVE_DAILY_LIMIT, len(candidates) - i)
                counts["limit"] = len(candidates) - i
                _notify_cap_once(sb, today)
                break
            name = card.get("name", "?")
            try:
                result = approve(sb, card["id"], "auto")
            except Exception as e:
                logger.warning("[auto_approve] 自動承認に失敗（取込は続行） card_id=%s %s: %s", card["id"], name, e)
                _notify(f"[自動承認] 失敗 card_id={card['id']} 「{name}」: {type(e).__name__}: {e}")
                bump("failed")
                continue
            if result.get("status") != "ok":
                logger.warning("[auto_approve] 自動承認できず card_id=%s %s: %s", card["id"], name, result.get("status"))
                _notify(f"[自動承認] 承認できませんでした card_id={card['id']} 「{name}」: {result.get('status')}")
                bump("failed")
                continue
            done_today += 1
            bump("approved")
            product = card.get("product_name") or ""
            _notify(f"[自動承認] 「{name}」" + (f"（{product}）" if product else "")
                    + f" を自動承認して公開しました（card_id={card['id']}）。問題があれば管理画面の公開中一覧で"
                      f"「承認待ちに戻す」: {ADMIN_URL}")
    except Exception as e:  # 想定外でも取込を止めない
        logger.warning("[auto_approve] 想定外のエラー（取込は続行）: %s", e)
        _notify(f"[自動承認] 想定外のエラー: {type(e).__name__}: {e}")
        bump("error")
    return counts
