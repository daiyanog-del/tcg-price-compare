"""
sync.py — 購入候補・保存デッキの端末間同期ロジック

設計文書: docs/design-sync-2026-08-09.md（第2版）「番号札方式（リビジョン制御）」

P1では購入候補（wishlist）のみを扱っていた。P2で保存デッキ（decks）を追加した。
app.py は本モジュールの関数を呼び出す薄い配線のみを持つ
（プロジェクトCLAUDE.md「新機能のロジックは新規モジュールに書く」方針）。

循環import回避のため、app.py の定数（MAX_CARD_NAME_LEN 等）を import せず、
このモジュール内で同値の定数として独立定義している。
"""

import re
from datetime import datetime, timezone

# 「<名前> (2)」「<名前> (3)」形式から元の名前を取り出す（merge_decks の名前衝突解決用）。
# 既に "(2)" が付いた名前が別の名前と再衝突した際、"(2) (2)" のような入れ子にせず
# 元の名前を基準に次の空き番号（(3)）を探すために使う
_DECK_NAME_SUFFIX_RE = re.compile(r"^(.*) \((\d+)\)$")

# ── 定数（設計文書 §9・§5.1。app.py の同名定数と同値） ──
MAX_CARD_NAME_LEN = 50
MAX_WISHLIST_ITEMS = 200      # 設計文書 §9
WISHLIST_QTY_MAX = 99          # 設計文書 §5.1（既存 wishSetRarity の頭打ちに合わせる）
PUSH_MAX_RETRY = 3             # 設計文書 §4.3

# P2: 保存デッキ（decks）関連の上限。app.py の MAX_DECK_PARAM_CHARS / MAX_DECK_CARDS と同値
MAX_DECKS = 50                 # 設計文書 §9
MAX_DECK_TEXT_CHARS = 6000     # app.py の MAX_DECK_PARAM_CHARS と同値
MAX_DECK_CARD_COUNT = 80       # app.py の MAX_DECK_CARDS と同値


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


def _deck_updated(deck) -> int:
    """デッキの updated（クライアント時計由来のミリ秒epoch）を安全に取り出す。不正値は0扱い。"""
    try:
        return int(deck.get("updated", 0))
    except (TypeError, ValueError):
        return 0


def merge_decks(server_decks, client_decks):
    """保存デッキの和集合マージ（設計文書 §5.2）。

    - キーは id
    - 同キーが両方にある場合は updated が大きい方を採用（同値ならserver側を維持）
    - id が違えば両方残す
    - デッキ名が衝突した場合（別idで同名）→ server → client の出現順で後から来た方を
      「<名前> (2)」にリネームする。(2) も既に使われていれば (3), (4) … と繰り上げる
    - 順序は server_items → client_items の出現順（id基準でのマージ結果の出現順を維持）
    """
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for source in (server_decks or [], client_decks or []):
        for d in source:
            did = d.get("id")
            if did is None:
                continue  # validate_decks で除外済みのはずだが念のため無視
            if did in by_id:
                if _deck_updated(d) > _deck_updated(by_id[did]):
                    by_id[did] = d
                # 同値または既存の方が新しければ既存を維持
            else:
                by_id[did] = d
                order.append(did)
    merged = [by_id[did] for did in order]

    # 名前衝突の解決（出現順に処理し、後から来た方をリネームする）
    used_names: set = set()
    result = []
    for d in merged:
        name = d.get("name", "") or ""
        if name in used_names:
            # 既に "(N)" が付いた名前どうしが衝突した場合に "(2) (2)" と入れ子にならないよう、
            # 元の名前（接尾辞を除いたもの）を基準に次の空き番号を探す
            m = _DECK_NAME_SUFFIX_RE.match(name)
            root = m.group(1) if m else name
            n = 2
            candidate = f"{root} ({n})"
            while candidate in used_names:
                n += 1
                candidate = f"{root} ({n})"
            d = dict(d)
            d["name"] = candidate
            name = candidate
        used_names.add(name)
        result.append(d)
    return result


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


def validate_decks(decks):
    """保存デッキリストの妥当性検証（設計文書 §9・§11）。

    返り値: (cleaned, error)
      - 妥当なら (cleaned_list, None)
      - 件数超過（50件超）・text長超過（6000文字超）・main/exの枚数超過（80枚超）は
        切り捨てず (None, エラーメッセージ) を返す（サイレントなデータ消失を防ぐため）
      - id が無い要素は個々に除外する（merge_decks のキーが作れないため）
      - main/ex は必須にしない（一人回しページ経由で保存されたデッキは持たないため。§11）
    """
    if not isinstance(decks, list):
        return None, "decks must be a list"
    if len(decks) > MAX_DECKS:
        return None, f"保存デッキは最大{MAX_DECKS}件までです"

    cleaned = []
    for d in decks:
        if not isinstance(d, dict):
            continue
        deck_id = d.get("id")
        if not deck_id:
            continue
        deck_id = str(deck_id)

        name = str(d.get("name", "") or "")
        text = str(d.get("text", "") or "")
        if len(text) > MAX_DECK_TEXT_CHARS:
            return None, f"デッキのテキストは{MAX_DECK_TEXT_CHARS}文字以内にしてください"

        main = d.get("main")
        ex = d.get("ex")
        main = main if isinstance(main, list) else []
        ex = ex if isinstance(ex, list) else []
        if len(main) > MAX_DECK_CARD_COUNT or len(ex) > MAX_DECK_CARD_COUNT:
            return None, f"デッキ枚数は最大{MAX_DECK_CARD_COUNT}枚までです"

        cleaned.append({
            "id": deck_id,
            "name": name,
            "text": text,
            "main": main,
            "ex": ex,
            "updated": _deck_updated(d),
        })
    return cleaned, None


def init_account(client, wishlist, decks=None):
    """sync_id を新規発行する（設計文書 §6.3）。

    渡された wishlist・decks をそのまま初期値として保存し、双方リビジョン1から開始する。
    DBエラーはここでキャッチせず呼び出し側（app.py）に送出する
    （app.py 側で db_unavailable として扱うため）。
    """
    payload = {
        "wishlist": wishlist or [],
        "wishlist_rev": 1,
        "decks": decks or [],
        "decks_rev": 1,
    }
    resp = client.table("sync_accounts").insert(payload).execute()
    row = resp.data[0]
    return {
        "ok": True,
        "sync_id": row["sync_id"],
        "wishlist_rev": row["wishlist_rev"],
        "decks_rev": row.get("decks_rev", 1),
    }


def _push(client, sync_id, base_rev, items, *, list_col, rev_col, merge_fn):
    """条件付き更新の共通実装（設計文書 §4.3）。push_wishlist / push_decks から呼ばれる。

    通常経路: {rev_col} = base_rev の行だけを更新する1回のSQL（原子的）。更新行数が1なら成功。
    競合経路: 更新行数が0なら他端末が先に更新している。現在の内容を読み、merge_fn で
      和集合マージしてから、読んだ時点のリビジョンを条件に再度更新する。
      最大 PUSH_MAX_RETRY 回リトライする。

    返り値:
      - 成功（通常）: {"ok": True, "status": "applied", "rev": N}
      - 成功（マージ）: {"ok": True, "status": "merged", "rev": N, "items": [...]}
      - リトライ上限到達: {"ok": False, "reason": "conflict_retry_exceeded"}
      - 行が存在しないことが確定: {"reason": "not_found"}
        （呼び出し側が新規発行するかどうかを判断する。DBエラーはここでキャッチせず送出する）
    """
    new_rev = base_rev + 1
    resp = (client.table("sync_accounts")
            .update({list_col: items, rev_col: new_rev, "last_seen_at": _now_iso()})
            .eq("sync_id", sync_id)
            .eq(rev_col, base_rev)
            .execute())
    if resp.data:
        return {"ok": True, "status": "applied", "rev": new_rev}

    # 競合経路（最大 PUSH_MAX_RETRY 回リトライ）
    for _ in range(PUSH_MAX_RETRY):
        cur = (client.table("sync_accounts")
               .select(f"{list_col},{rev_col}")
               .eq("sync_id", sync_id)
               .execute())
        if not cur.data:
            return {"reason": "not_found"}
        current_row = cur.data[0]
        current_items = current_row.get(list_col) or []
        current_rev = current_row[rev_col]
        merged = merge_fn(current_items, items)
        next_rev = current_rev + 1
        resp2 = (client.table("sync_accounts")
                 .update({list_col: merged, rev_col: next_rev, "last_seen_at": _now_iso()})
                 .eq("sync_id", sync_id)
                 .eq(rev_col, current_rev)
                 .execute())
        if resp2.data:
            return {"ok": True, "status": "merged", "rev": next_rev, "items": merged}
    return {"ok": False, "reason": "conflict_retry_exceeded"}


def push_wishlist(client, sync_id, base_rev, items):
    """購入候補を条件付き更新する（設計文書 §4.3）。_push の wishlist_rev 版。"""
    return _push(client, sync_id, base_rev, items,
                 list_col="wishlist", rev_col="wishlist_rev", merge_fn=merge_wishlist)


def push_decks(client, sync_id, base_rev, items):
    """保存デッキを条件付き更新する（設計文書 §4.3）。_push の decks_rev 版。

    購入候補とはリビジョン（decks_rev）を分けているため、片方の更新がもう片方の
    競合を誘発しない（設計文書 §3.1）。
    """
    return _push(client, sync_id, base_rev, items,
                 list_col="decks", rev_col="decks_rev", merge_fn=merge_decks)


def pull(client, sync_id, wishlist_rev, decks_rev):
    """リビジョンが変わっていれば購入候補・保存デッキを返す（設計文書 §4.4・§6.2）。

    リビジョンが一致するリストは中身を返さない（unchanged）。
    返り値:
      - {"ok": True, "wishlist": {...}, "decks": {...}}
        各要素は {"unchanged": True} または {"rev": N, "items": [...]}
      - {"reason": "not_found"}（行が存在しないことが確定した場合。DBエラーは送出する）
    """
    resp = (client.table("sync_accounts")
            .select("wishlist,wishlist_rev,decks,decks_rev")
            .eq("sync_id", sync_id)
            .execute())
    if not resp.data:
        return {"reason": "not_found"}
    row = resp.data[0]

    result = {"ok": True}
    current_wishlist_rev = row["wishlist_rev"]
    if current_wishlist_rev == wishlist_rev:
        result["wishlist"] = {"unchanged": True}
    else:
        result["wishlist"] = {"rev": current_wishlist_rev, "items": row.get("wishlist") or []}

    current_decks_rev = row.get("decks_rev", 0)
    if current_decks_rev == decks_rev:
        result["decks"] = {"unchanged": True}
    else:
        result["decks"] = {"rev": current_decks_rev, "items": row.get("decks") or []}

    return result
