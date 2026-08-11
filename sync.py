"""
sync.py — 購入候補・保存デッキの端末間同期ロジック

設計文書: docs/design-sync-2026-08-09.md（第2版）「番号札方式（リビジョン制御）」

P1（本ファイル作成時点）では購入候補（wishlist）のみを扱う。保存デッキ（decks）は
P2で追加する。app.py は本モジュールの関数を呼び出す薄い配線のみを持つ
（プロジェクトCLAUDE.md「新機能のロジックは新規モジュールに書く」方針）。

循環import回避のため、app.py の定数（MAX_CARD_NAME_LEN 等）を import せず、
このモジュール内で同値の定数として独立定義している。
"""

from datetime import datetime, timezone

# ── 定数（設計文書 §9・§5.1。app.py の MAX_CARD_NAME_LEN=50 と同値） ──
MAX_CARD_NAME_LEN = 50
MAX_WISHLIST_ITEMS = 200      # 設計文書 §9
WISHLIST_QTY_MAX = 99          # 設計文書 §5.1（既存 wishSetRarity の頭打ちに合わせる）
PUSH_MAX_RETRY = 3             # 設計文書 §4.3


def _now_iso() -> str:
    """timestamptz カラムへ書き込むためのUTC ISO8601文字列。"""
    return datetime.now(timezone.utc).isoformat()


def merge_wishlist(server_items, client_items):
    """購入候補の和集合マージ（設計文書 §5.1）。

    - キーは (name, rarity) のタプル。文字列連結ではなくタプルにすることで、
      カード名に空白が含まれていてもキーが衝突しない
    - 同キーが両方にある場合は qty の大きい方を採用（上限 WISHLIST_QTY_MAX）
    - 片方にしかない要素はそのまま残る
    - 順序は server_items → client_items の出現順（新規に現れた要素を末尾に追加）
    """
    merged: dict[tuple, int] = {}
    order: list[tuple] = []
    for source in (server_items or [], client_items or []):
        for it in source:
            name = it.get("name", "")
            rarity = it.get("rarity", "") or ""
            key = (name, rarity)
            try:
                qty = int(it.get("qty", 1))
            except (TypeError, ValueError):
                qty = 1
            qty = max(1, min(qty, WISHLIST_QTY_MAX))
            if key in merged:
                merged[key] = max(merged[key], qty)
            else:
                merged[key] = qty
                order.append(key)
    return [{"name": k[0], "rarity": k[1], "qty": merged[k]} for k in order]


def validate_wishlist(items):
    """購入候補リストの妥当性検証（設計文書 §9）。

    返り値: (cleaned, error)
      - 妥当なら (cleaned_list, None)
      - 件数超過（200件超）・カード名の長さ超過（50文字超）は切り捨てず
        (None, エラーメッセージ) を返す（サイレントなデータ消失を防ぐため）
      - それ以外の型不整合な要素（dict でない・name が空等）は個々に除外する
        （設計文書に規定がないため、既存の /api/push/subscribe 等の緩いサニタイズに倣う）
    """
    if not isinstance(items, list):
        return None, "items must be a list"
    if len(items) > MAX_WISHLIST_ITEMS:
        return None, f"購入候補は最大{MAX_WISHLIST_ITEMS}件までです"

    cleaned = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name", "")).strip()
        if not name:
            continue
        if len(name) > MAX_CARD_NAME_LEN:
            return None, f"カード名は{MAX_CARD_NAME_LEN}文字以内にしてください"
        rarity = str(it.get("rarity", "") or "")
        try:
            qty = int(it.get("qty", 1))
        except (TypeError, ValueError):
            qty = 1
        qty = max(1, min(qty, WISHLIST_QTY_MAX))
        cleaned.append({"name": name, "rarity": rarity, "qty": qty})
    return cleaned, None


def init_account(client, wishlist):
    """sync_id を新規発行する（設計文書 §6.3）。

    渡された wishlist をそのまま初期値として保存し、リビジョン1から開始する。
    DBエラーはここでキャッチせず呼び出し側（app.py）に送出する
    （app.py 側で db_unavailable として扱うため）。
    """
    payload = {
        "wishlist": wishlist or [],
        "wishlist_rev": 1,
    }
    resp = client.table("sync_accounts").insert(payload).execute()
    row = resp.data[0]
    return {
        "ok": True,
        "sync_id": row["sync_id"],
        "wishlist_rev": row["wishlist_rev"],
    }


def push_wishlist(client, sync_id, base_rev, items):
    """購入候補を条件付き更新する（設計文書 §4.3）。

    通常経路: wishlist_rev = base_rev の行だけを更新する1回のSQL（原子的）。
      更新行数が1なら成功。
    競合経路: 更新行数が0なら他端末が先に更新している。現在の内容を読み、
      和集合マージ（merge_wishlist）してから、読んだ時点のリビジョンを条件に
      再度更新する。最大 PUSH_MAX_RETRY 回リトライする。

    返り値:
      - 成功（通常）: {"ok": True, "status": "applied", "rev": N}
      - 成功（マージ）: {"ok": True, "status": "merged", "rev": N, "items": [...]}
      - リトライ上限到達: {"ok": False, "reason": "conflict_retry_exceeded"}
      - 行が存在しないことが確定: {"reason": "not_found"}
        （呼び出し側が新規発行するかどうかを判断する。DBエラーはここでキャッチせず送出する）
    """
    new_rev = base_rev + 1
    resp = (client.table("sync_accounts")
            .update({"wishlist": items, "wishlist_rev": new_rev, "last_seen_at": _now_iso()})
            .eq("sync_id", sync_id)
            .eq("wishlist_rev", base_rev)
            .execute())
    if resp.data:
        return {"ok": True, "status": "applied", "rev": new_rev}

    # 競合経路（最大 PUSH_MAX_RETRY 回リトライ）
    for _ in range(PUSH_MAX_RETRY):
        cur = (client.table("sync_accounts")
               .select("wishlist,wishlist_rev")
               .eq("sync_id", sync_id)
               .execute())
        if not cur.data:
            return {"reason": "not_found"}
        current_row = cur.data[0]
        current_items = current_row.get("wishlist") or []
        current_rev = current_row["wishlist_rev"]
        merged = merge_wishlist(current_items, items)
        next_rev = current_rev + 1
        resp2 = (client.table("sync_accounts")
                 .update({"wishlist": merged, "wishlist_rev": next_rev, "last_seen_at": _now_iso()})
                 .eq("sync_id", sync_id)
                 .eq("wishlist_rev", current_rev)
                 .execute())
        if resp2.data:
            return {"ok": True, "status": "merged", "rev": next_rev, "items": merged}
    return {"ok": False, "reason": "conflict_retry_exceeded"}


def pull(client, sync_id, wishlist_rev):
    """リビジョンが変わっていれば購入候補を返す（設計文書 §4.4）。

    リビジョンが一致する場合は中身を返さない（unchanged）。
    返り値:
      - {"ok": True, "wishlist": {"unchanged": True}}
      - {"ok": True, "wishlist": {"rev": N, "items": [...]}}
      - {"reason": "not_found"}（行が存在しないことが確定した場合。DBエラーは送出する）
    """
    resp = (client.table("sync_accounts")
            .select("wishlist,wishlist_rev")
            .eq("sync_id", sync_id)
            .execute())
    if not resp.data:
        return {"reason": "not_found"}
    row = resp.data[0]
    current_rev = row["wishlist_rev"]
    if current_rev == wishlist_rev:
        return {"ok": True, "wishlist": {"unchanged": True}}
    return {"ok": True, "wishlist": {"rev": current_rev, "items": row.get("wishlist") or []}}
