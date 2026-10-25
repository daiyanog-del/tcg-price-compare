"""
unreleased_rejected.py -- 却下した未発売カード行の扱い（取込の4つの入口で共用）

却下（status='rejected'）は「誤読・重複を自動巡回でもう拾わない」ためのブロックリストだが、
以前は名前が同じなら出どころに関係なく取込を止めていた。そのため、低品質の抽出を却下した後に
公式記事や公式Xから正しい情報が来ても入らなかった（2026-10-25 暗黒の太陽神－ラーの翼神竜。
08-14 に旧URLから低確度で入って却下 → 10-15 の記事と 10-22 の X 投稿が「取込済み」でスキップ）。

方針（2026-10-25 ユーザー裁定）:
  - 却下した行と同じ出どころ（同じ投稿・同じ記事・同じ動画）からは今まで通り取り込まない
  - 別の出どころから同名が来たら、却下した行を新しい抽出結果で書き換えて承認待ち（pending）に戻す
  - 戻した行は自動承認しない（unreleased_approve.precheck_auto_approve が止める）。Discord で知らせる
  - 却下以外に同名の行（公開中・承認待ち）があるときは戻さない（重複として却下した行を生き返らせない）
  - カード本体（効果・数値）が読めない抽出では戻さない（呼び出し側で lacks_card_body を見る）

入口: watch_x_unreleased（X の定期取込）・admin_unreleased.admin_fetch_x_post（管理画面の X 取込）・
      watch_unreleased（公式サイト巡回）・unreleased_upload.register_rows（画像アップロード）
"""

import logging
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from name_normalize import fuzzy_key

logger = logging.getLogger(__name__)

# extraction_raw に残す復活の記録のキー（precheck_auto_approve もこれを見て自動承認を止める）
REVIVED_KEY = "revived_from_rejected"

_PAGE = 1000  # Supabase の1回の select 上限

# 復活時に初期化する列（承認・発売後紐付けの痕跡を消して、新規取込と同じ状態にする）
_RESET_COLUMNS = {
    "status": "pending",
    "hidden": False,
    "approved_at": None,
    "approved_via": None,
    "linked_at": None,
    "konami_id": None,
    "review_candidates": None,
}

_X_HOSTS = ("x.com", "twitter.com", "fxtwitter.com", "vxtwitter.com", "fixupx.com", "fixvx.com")
_YT_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")


def source_key(url: str) -> str:
    """出どころの比較キー。同じ投稿・記事・動画なら同じ値になる。比較できない（空・manual）なら ""。

    - X: 投稿ID（?s=20 などの付加や x.com / twitter.com / fxtwitter 等の違いを無視）
    - YouTube: 動画ID（&t= の再生位置を無視。watch?v= / youtu.be / live / shorts / embed を同じに扱う）
    - yu-gi-oh.jp: 旧形式 news_detail.php?id=N を /news/oid-N/ に正規化
    - その他: ホスト＋パス（クエリ・末尾スラッシュを無視）
    """
    url = (url or "").strip()
    if not url or url == "manual":
        return ""
    if "://" not in url:
        url = "https://" + url  # http なしで貼られた URL（x.com/... など）
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    for prefix in ("www.", "mobile.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    if host in _X_HOSTS:
        m = re.search(r"/status(?:es)?/(\d+)", parsed.path)
        if m:
            return f"x:{m.group(1)}"
    if host in _YT_HOSTS:
        if host == "youtu.be":
            vid = parsed.path.strip("/").split("/")[0]
        else:
            m = re.match(r"/(?:live|shorts|embed|v)/([^/?#]+)", parsed.path)
            vid = m.group(1) if m else (parse_qs(parsed.query).get("v") or [""])[0]
        if vid:
            return f"yt:{vid}"
    if host == "yu-gi-oh.jp" and parsed.path == "/news_detail.php":
        ids = parse_qs(parsed.query).get("id") or [""]
        if ids[0].isdigit():
            return f"yu-gi-oh.jp/news/oid-{ids[0]}"
    if not host:
        return url
    return host + parsed.path.rstrip("/")


def _revived_record(card: dict) -> dict:
    """復活の記録。load_rejected は列を絞って revived に入れて読むので、両方を見る。"""
    rec = card.get("revived")
    if not isinstance(rec, dict):
        rec = (card.get("extraction_raw") or {}).get(REVIVED_KEY)
    return rec if isinstance(rec, dict) else {}


def _past_source_keys(card: dict) -> set[str]:
    """この却下行が却下された出どころ（今の source_url と、以前の復活前の出どころ）。"""
    keys = {source_key(card.get("source_url") or "")}
    for url in _revived_record(card).get("previous_sources") or []:
        keys.add(source_key(url))
    keys.discard("")
    return keys


class RejectedIndex:
    """却下した行の索引（fuzzy_key → 行のリスト）。"""

    def __init__(self, cards=None):
        self.cards: dict[str, list[dict]] = {}
        for card in cards or []:
            key = fuzzy_key(card.get("name") or "")
            if key:
                self.cards.setdefault(key, []).append(card)
        for rows in self.cards.values():
            rows.sort(key=lambda c: c.get("id") or 0)

    def __bool__(self) -> bool:
        return bool(self.cards)

    def has(self, name: str) -> bool:
        return fuzzy_key(name or "") in self.cards

    def blocks(self, name: str, source_url: str) -> bool:
        """同名の却下行が、この出どころで却下されたものか（＝取り込まない）。"""
        src = source_key(source_url)
        if not src:
            return False
        return any(src in _past_source_keys(c) for c in self.cards.get(fuzzy_key(name or ""), []))

    def revivable(self, row: dict, source_url: str, *, active_keys, force: bool = False) -> dict | None:
        """別の出どころから来た同名の却下行（承認待ちに戻す行）。戻さないなら None。

        - active_keys: 却下以外の行のカード名（fuzzy_key の集合）。同名が公開中・承認待ちにあれば戻さない
          （重複として却下した行を生き返らせると同じカードが2行になる）
        - 同じ出どころで却下されていれば戻さない。force=True（管理画面の強制取込）は出どころが同じでも戻す
        - 名前と商品名が完全一致する却下行を優先する（別の行を選ぶと一意制約 (name, product_name) に当たる）
        """
        name = row.get("name") or ""
        key = fuzzy_key(name)
        rows = self.cards.get(key)
        if not rows or key in active_keys:
            return None
        if not force and self.blocks(name, source_url):
            return None
        product = row.get("product_name")
        exact = [c for c in rows if c.get("name") == name and c.get("product_name") == product]
        return (exact or rows)[0]

    def take(self, name: str) -> None:
        """復活させた名前を索引から外す（同じ実行で同名がもう一度来ても二重に書き換えない）。"""
        self.cards.pop(fuzzy_key(name or ""), None)


def _select_all(sb, columns: str, *, rejected: bool) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        q = sb.table("unreleased_cards").select(columns)
        q = q.eq("status", "rejected") if rejected else q.neq("status", "rejected")
        # 並び順を固定しないとページ境界で行が抜けたり重なったりする
        page = list(q.order("id").range(offset, offset + _PAGE - 1).execute().data or [])
        rows.extend(page)
        if len(page) < _PAGE:
            return rows
        offset += _PAGE


def load_rejected(sb) -> RejectedIndex:
    """却下した行を読む。例外は呼び出し側へ（読めないまま進むと却下を素通りさせてしまうため）。

    extraction_raw は大きい（紹介場面の取込など）ので、復活の記録だけを revived として読む。
    """
    return RejectedIndex(_select_all(
        sb, f"id, name, product_name, source_url, extracted_at, revived:extraction_raw->{REVIVED_KEY}",
        rejected=True))


def load_active_keys(sb) -> set[str]:
    """却下以外の行のカード名（fuzzy_key）。取込済みとして飛ばす判定と、却下行を戻さない判定に使う。"""
    return {fuzzy_key(r["name"]) for r in _select_all(sb, "name", rejected=False) if r.get("name")}


def _default_notify(message: str) -> None:
    try:
        from discord_notify import send_discord_message
        send_discord_message(message, no_mentions=True)
    except Exception as e:  # noqa: BLE001 — 通知の失敗で取込を止めない
        logger.warning(f"[rejected] Discord 通知失敗: {e}")


def revive_rejected(sb, card: dict, row: dict, *, notify=None, raise_errors: bool = False) -> dict | None:
    """却下行 card を、別の出どころの抽出結果 row で書き換えて承認待ちに戻す。書き換えた行を返す。

    - 先に古い画像行を論理削除してから行を書き換える（行だけ戻って古い誤読の画像が見えたまま、を作らない。
      取込側の画像保存は見えている画像行があると「取込済み」で飛ばすため、後からも直らない）
    - extraction_raw は今回の抽出結果に置き換え、以前の出どころを REVIVED_KEY に残す
      （previous_sources は以後の「同じ出どころ」判定に使う）
    - もう却下状態でなかった（他の処理が先に変えた）ときは None
    - DB の失敗は Discord に知らせて None。raise_errors=True なら呼び出し側へ投げる
      （X の定期取込は投げて since_id を進めない。進めるとその投稿を二度と取り込めない）
    """
    notify = notify or _default_notify
    cid = card.get("id")
    name = row.get("name") or card.get("name") or "?"
    now = datetime.now(timezone.utc).isoformat()
    prev_sources = list(_revived_record(card).get("previous_sources") or [])
    if card.get("source_url") and card["source_url"] not in prev_sources:
        prev_sources.append(card["source_url"])
    raw = dict(row.get("extraction_raw") or {})
    raw[REVIVED_KEY] = {
        "at": now,
        "previous_name": card.get("name"),
        "previous_product_name": card.get("product_name"),
        "previous_extracted_at": card.get("extracted_at"),
        "previous_sources": prev_sources,
    }
    updates = {k: v for k, v in row.items() if k not in ("id", "extraction_raw")}
    updates.update(_RESET_COLUMNS)
    updates["extraction_raw"] = raw
    updates.setdefault("extracted_at", now)
    try:
        # 他の処理が先に却下を外していたら触らない（画像を隠してから気づくと、生きている行の画像を消してしまう）
        cur = sb.table("unreleased_cards").select("id, status").eq("id", cid).execute().data or []
        if not cur or cur[0].get("status") != "rejected":
            logger.warning(f"[rejected] 却下行が見つからないため復活しない: id={cid} {name!r}")
            return None
        old = (sb.table("official_card_images").select("id").eq("unreleased_card_id", cid)
               .is_("deleted_at", "null").execute().data or [])
        if old:
            sb.table("official_card_images").update({"deleted_at": now}).in_(
                "id", sorted(r["id"] for r in old)).execute()
        resp = (sb.table("unreleased_cards").update(updates)
                .eq("id", cid).eq("status", "rejected").execute())
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[rejected] 却下行の復活に失敗: id={cid} {name!r}: {e}")
        notify(f"[未発売] 却下していたカードを承認待ちに戻せませんでした: {name}（id={cid}） "
               f"今回: {row.get('source_url') or '不明'} {type(e).__name__}: {e}")
        if raise_errors:
            raise
        return None
    if not resp.data:
        logger.warning(f"[rejected] 却下行が見つからないため復活しない: id={cid} {name!r}")
        return None
    revived = resp.data[0]
    logger.info(f"[rejected] 却下行を承認待ちに戻した: id={cid} {name!r} "
                f"前={card.get('source_url')} 今回={row.get('source_url')}")
    notify(f"[未発売] 却下していたカードが別の出どころで再登場したため、承認待ちに戻しました（自動承認しません）: "
           f"{name}（id={cid}） 前: {card.get('source_url') or '不明'} 今回: {row.get('source_url') or '不明'}")
    return revived


def bust_image_url(sb, card: dict) -> None:
    """戻した行は同じ id・同じ保存先に画像を上書きするので、公開URLに時刻を付けて古い画像のキャッシュを避ける。"""
    if not (card.get("extraction_raw") or {}).get(REVIVED_KEY) or card.get("id") is None:
        return
    from unreleased_upload import _bust_new_image_url
    _bust_new_image_url(sb, card["id"])
