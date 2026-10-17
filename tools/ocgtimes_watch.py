"""
ocgtimes_watch.py — 公式配信「遊戯王OCGタイムズ」の告知の検知と、配信後のキャプチャ取込（自宅PC用）

背景（2026-10-17）:
  OCGタイムズは新弾ごと（約3か月に1回）に配信され、公式サイトのトップに「遊戯王OCGタイムズ○月版 配信情報公開」
  （information/ocgtimes<商品コード>/）が配信の約1週間前に出る（7月版 07-03、10月版 10-09→配信 10-16）。
  番組ページには配信動画のリンクがあり、YouTube の動画ページから配信の開始・終了時刻と、概要欄の
  「本日の新規カードまとめ」の時刻が取れる。初公開カードは公式Xや公式サイトにすぐ出ないことがあるため、
  一覧画面のキャプチャを unreleased_upload.import_uploaded_image で承認待ちに入れる。

この PC で動く Claude の定期タスクがこのツールを呼ぶ（週2回の告知確認 ocgtimes-watch と、毎晩の取込 ocgtimes-import）。
無人の回ではアプリが定期タスクの作成を拒否するので、配信後の取込は予約せず「取込待ち」として状態ファイルに残す:
  check                 公式サイトのトップから新しい告知を探す（AI不要）。見つけたら番組ページと配信時刻を JSON で出す
  mark <slug> [...] [--video ID --start ISO --url URL --heading 見出し]
                        告知を処理済みにする。--video を付けると、その動画を「取込待ち」に入れる
  pending               取込待ちの動画と、配信が終わったか（ready）を JSON で出す
  done <video_id>       取込待ちから外す（取込が済んだら・取込めないと決めたら）
  status <video_id>     配信が終わったか、「新規カードまとめ」の時刻はどこかを JSON で出す
  import <video_id> --since <ISO> --times 6448,6458,...
                        ダウンロードフォルダの since 以降の画像（PNG/JPEG）を、時刻の順に取込む

状態は ~/.cardsouba/ocgtimes_state.json（git 管理外）。認証は ~/.cardsouba/watcher.env（SUPABASE_URL/KEY 等）。
キャプチャそのものは内蔵ブラウザで行う（動画のダウンロードはしない。YouTube の規約上、第三者ツールでの
ダウンロードは避ける）。
"""

import argparse
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sys
from datetime import datetime, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

logger = logging.getLogger("ocgtimes_watch")

TOP_URL = "https://www.yugioh-card.com/japan/"
PAGE_URL = "https://www.yugioh-card.com/japan/information/{slug}/"
WATCH_URL = "https://www.youtube.com/watch?v={vid}"
STATE_PATH = os.path.join(os.path.expanduser("~"), ".cardsouba", "ocgtimes_state.json")
FRAMES_ROOT = os.path.join(os.path.expanduser("~"), ".cardsouba", "ocgtimes_frames")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36", "Accept-Language": "ja"}

_SLUG_RE = re.compile(r'href="(?:/japan/)?information/(ocgtimes[a-z0-9]+)/"[^>]*>([^<]*)<')
_VIDEO_RE = re.compile(r'youtu(?:\.be/|be\.com/(?:live/|watch\?v=|embed/))([A-Za-z0-9_-]{11})')
_CHAPTER_RE = re.compile(r'^\s*((?:\d{1,2}:)?\d{1,2}:\d{2})\s*[┗└・\s]*(.+?)\s*$')


# ──────────────────────────────────────────────
# 解析（ネットワークなし。テスト対象）
# ──────────────────────────────────────────────

def parse_top(html: str) -> dict[str, str]:
    """トップのHTMLから OCGタイムズの告知（slug → 見出し）を取り出す。バナー（見出しなし）より文字のリンクを優先。"""
    found: dict[str, str] = {}
    for slug, text in _SLUG_RE.findall(html):
        text = text.strip()
        if slug not in found or (text and not found[slug]):
            found[slug] = text
    return found


def parse_page(html: str) -> dict:
    """番組ページから見出しと配信動画のIDを取り出す（チャンネルへのリンクは動画IDの形にならないので混ざらない）。"""
    title = re.search(r"<title>(.*?)</title>", html, re.S)
    vids = list(dict.fromkeys(_VIDEO_RE.findall(html)))
    return {"title": title.group(1).strip() if title else "", "video_ids": vids}


def _json_after(text: str, key: str):
    """'"key":{...}' の {...} を括弧の対応で取り出して読む（正規表現の最短一致は入れ子で切れるため）。"""
    i = text.find(f'"{key}":')
    if i < 0:
        return None
    j = text.find("{", i)
    if j < 0:
        return None
    depth, k, in_str, esc = 0, j, False, False
    while k < len(text):
        c = text[k]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[j:k + 1])
                except ValueError:
                    return None
        k += 1
    return None


def _seconds(ts: str) -> int:
    parts = [int(p) for p in ts.split(":")]
    total = 0
    for p in parts:
        total = total * 60 + p
    return total


def parse_watch(html: str) -> dict:
    """YouTube の動画ページから、配信の状態・時刻・概要欄の目次を取り出す。

    戻り値: {title, is_live_now, start, end, scheduled_start, chapters:[{seconds, label}], summary_seconds}
    summary_seconds は「新規カード」と「まとめ」または「一覧」を含む目次の時刻（無ければ None）。
    """
    details = _json_after(html, "liveBroadcastDetails") or {}
    video = _json_after(html, "videoDetails") or {}
    desc = video.get("shortDescription") or ""
    sched = re.search(r'"scheduledStartTime":"(\d+)"', html)
    chapters = []
    for line in desc.splitlines():
        m = _CHAPTER_RE.match(line)
        if m:
            chapters.append({"seconds": _seconds(m.group(1)), "label": m.group(2)})
    summary = next((c["seconds"] for c in reversed(chapters)
                    if "新規カード" in c["label"] and ("まとめ" in c["label"] or "一覧" in c["label"])), None)
    return {
        "title": video.get("title") or "",
        "is_live_now": bool(details.get("isLiveNow")),
        "start": details.get("startTimestamp"),
        "end": details.get("endTimestamp"),
        "scheduled_start": (datetime.fromtimestamp(int(sched.group(1)), timezone.utc).isoformat()
                            if sched else None),
        "chapters": chapters,
        "summary_seconds": summary,
    }


def is_ended(info: dict) -> bool:
    """配信が終わってアーカイブになったか（終了時刻があり、今は配信中でない）。"""
    return bool(info.get("end")) and not info.get("is_live_now")


# ──────────────────────────────────────────────
# 状態
# ──────────────────────────────────────────────

def load_state(path: str = STATE_PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"seen": {}}


def save_state(state: dict, path: str = STATE_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ──────────────────────────────────────────────
# ネットワーク
# ──────────────────────────────────────────────

def _get(url: str) -> str:
    import requests
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    r.encoding = r.apparent_encoding if not r.encoding or r.encoding.lower() == "iso-8859-1" else r.encoding
    return r.text


def cmd_check(args) -> dict:
    state = load_state()
    found = parse_top(_get(TOP_URL))
    new = []
    for slug, heading in found.items():
        if slug in state["seen"]:
            continue
        page = parse_page(_get(PAGE_URL.format(slug=slug)))
        videos = []
        for vid in page["video_ids"]:
            info = parse_watch(_get(WATCH_URL.format(vid=vid)))
            info.pop("chapters", None)
            videos.append({"video_id": vid, "url": WATCH_URL.format(vid=vid), **info})
        new.append({"slug": slug, "heading": heading or page["title"], "page_url": PAGE_URL.format(slug=slug),
                    "videos": videos})
    return {"checked_at": datetime.now(timezone.utc).isoformat(), "found": sorted(found), "new": new}


def cmd_mark(args) -> dict:
    state = load_state()
    now = datetime.now(timezone.utc).isoformat()
    for slug in args.slugs:
        state["seen"][slug] = {"at": now, "note": args.note or ""}
    if getattr(args, "video", None):
        if len(args.slugs) != 1:
            raise SystemExit("--video を付けるときは告知を1つだけ指定する")
        state.setdefault("pending", {})[args.video] = {
            "slug": args.slugs[0], "start": args.start or None, "url": args.url or WATCH_URL.format(vid=args.video),
            "heading": args.heading or "", "added_at": now}
    save_state(state)
    return {"marked": args.slugs, "pending": sorted(state.get("pending", {}))}


def cmd_pending(args) -> dict:
    """取込待ちの動画。開始時刻を過ぎたものは YouTube で配信が終わったかを見る（ready = 取込んでよい）。"""
    state = load_state()
    now = datetime.now(timezone.utc)
    items = []
    for vid, item in sorted(state.get("pending", {}).items()):
        entry = {"video_id": vid, **item, "ready": False}
        start = item.get("start")
        started = True
        if start:
            try:
                started = datetime.fromisoformat(start) <= now
            except ValueError:
                started = True
        if started:
            try:
                info = parse_watch(_get(WATCH_URL.format(vid=vid)))
                entry.update(ended=is_ended(info), summary_seconds=info["summary_seconds"], end=info["end"])
                entry["ready"] = entry["ended"]
            except Exception as e:  # noqa: BLE001 — 取れなければ次の晩にまた見る
                entry["error"] = repr(e)
        items.append(entry)
    return {"checked_at": now.isoformat(), "pending": items}


def cmd_done(args) -> dict:
    state = load_state()
    item = state.get("pending", {}).pop(args.video_id, None)
    if item is not None:
        item.update(done_at=datetime.now(timezone.utc).isoformat(), note=args.note or "")
        state.setdefault("done", {})[args.video_id] = item
    save_state(state)
    return {"done": args.video_id, "found": item is not None}


def cmd_status(args) -> dict:
    info = parse_watch(_get(WATCH_URL.format(vid=args.video_id)))
    return {"video_id": args.video_id, "ended": is_ended(info), **info}


def _image_kind(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    return None


def collect_frames(folder: str, since: datetime) -> list[tuple[str, bytes, str]]:
    """フォルダの since 以降に作られた画像を、更新時刻の順に返す（拡張子は見ない。.tmp 名でも中身で判定）。

    内蔵ブラウザのダウンロードは .tmp の名前のまま残ることがある（2026-10-17 確認）。同じ中身は1回だけ。
    """
    out, seen = [], set()
    entries = []
    for name in os.listdir(folder):
        p = os.path.join(folder, name)
        if not os.path.isfile(p):
            continue
        mtime = datetime.fromtimestamp(os.path.getmtime(p), timezone.utc)
        if mtime >= since:
            entries.append((mtime, p))
    for _, p in sorted(entries):
        with open(p, "rb") as f:
            data = f.read()
        kind = _image_kind(data)
        if not kind:
            continue
        h = hashlib.sha256(data).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        out.append((p, data, kind))
    return out


def cmd_import(args) -> dict:
    import base64
    _load_env()
    since = datetime.fromisoformat(args.since)
    if since.tzinfo is None:
        raise SystemExit("--since はタイムゾーンつきの ISO 形式で（例: 2026-10-16T22:30:00+09:00）")
    times = [t.strip() for t in (args.times or "").split(",") if t.strip()]
    frames = collect_frames(args.dir, since)
    if not frames:
        return {"error": "取込む画像が見つかりません", "dir": args.dir, "since": args.since}
    if times and len(times) != len(frames):
        return {"error": f"画像の数（{len(frames)}）と --times の数（{len(times)}）が合いません",
                "frames": [p for p, _, _ in frames]}
    keep = os.path.join(FRAMES_ROOT, args.video_id)
    os.makedirs(keep, exist_ok=True)

    from supabase import create_client
    import unreleased_upload as up
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    note = args.note or ""
    results = []
    for i, (path, data, kind) in enumerate(frames):
        t = times[i] if times else ""
        name = f"list{i + 1}" + (f"_t{t}" if t else "") + (".png" if kind == "image/png" else ".jpg")
        shutil.copyfile(path, os.path.join(keep, name))
        source = WATCH_URL.format(vid=args.video_id) + (f"&t={t}s" if t else "")
        try:
            r = up.import_uploaded_image(sb, data_url=f"data:{kind};base64," + base64.b64encode(data).decode(),
                                         source_url=source, note=note, filename=name)
            results.append({"frame": name, "extracted": r["extracted"],
                            "inserted": [{"id": c["id"], "name": c["name"]} for c in r["inserted"]],
                            "skipped": r["skipped"], "images": {str(k): v for k, v in r["images"].items()}})
        except Exception as e:  # noqa: BLE001 — 1画面の失敗で他を止めない
            results.append({"frame": name, "error": repr(e)})
    sheet = _contact_sheet(results, keep) if not args.no_sheet else None
    summary = _summary(results)
    if not args.no_notify:
        _notify(f"[OCGタイムズ] 取込: {summary}。承認待ちで画像と照合してください"
                f"（{WATCH_URL.format(vid=args.video_id)}）")
    return {"video_id": args.video_id, "frames_kept": keep, "results": results, "summary": summary,
            "contact_sheet": sheet}


def _summary(results: list[dict]) -> str:
    ins = sum(len(r.get("inserted", [])) for r in results)
    fail = sum(1 for r in results if r.get("error"))
    img_fail = sum(1 for r in results for v in (r.get("images") or {}).values() if str(v).startswith("失敗"))
    return f"{len(results)}画面・追加{ins}枚" + (f"・失敗{fail}画面" if fail else "") + (
        f"・画像保存失敗{img_fail}枚" if img_fail else "")


def _contact_sheet(results: list[dict], folder: str) -> str | None:
    """取込んだカードの保存画像を1枚に並べる（報告用）。失敗しても取込は成功扱い。"""
    try:
        import urllib.request
        from PIL import Image, ImageDraw
        base = os.environ["SUPABASE_URL"].rstrip("/") + "/storage/v1/object/public/official-card-images/unreleased/"
        ids = [c["id"] for r in results for c in r.get("inserted", [])]
        if not ids:
            return None
        w, h, cols = 240, 350, 8
        rows = (len(ids) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * (w + 8) + 8, rows * (h + 28) + 8), (24, 28, 40))
        draw = ImageDraw.Draw(sheet)
        for k, cid in enumerate(ids):
            with urllib.request.urlopen(f"{base}{cid}.png?v=sheet", timeout=30) as resp:
                im = Image.open(io.BytesIO(resp.read())).convert("RGB")
            x, y = 8 + (k % cols) * (w + 8), 8 + (k // cols) * (h + 28)
            sheet.paste(im.resize((w, int(im.height * w / im.width))), (x, y + 24))
            draw.text((x, y + 4), f"id {cid}", fill=(230, 230, 230))
        out = os.path.join(folder, "contact_sheet.png")
        sheet.save(out)
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("一覧画像を作れません: %s", e)
        return None


def _notify(message: str) -> None:
    try:
        _load_env()
        from discord_notify import send_discord_message
        send_discord_message(message, no_mentions=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("Discord 通知失敗: %s", e)


def _load_env() -> None:
    """~/.cardsouba/watcher.env から未設定の環境変数を補う（値は出力しない）。"""
    path = os.path.join(os.path.expanduser("~"), ".cardsouba", "watcher.env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


def main(argv=None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    m = sub.add_parser("mark")
    m.add_argument("slugs", nargs="+")
    m.add_argument("--note", default="")
    m.add_argument("--video", default="")
    m.add_argument("--start", default="")
    m.add_argument("--url", default="")
    m.add_argument("--heading", default="")
    sub.add_parser("pending")
    dn = sub.add_parser("done")
    dn.add_argument("video_id")
    dn.add_argument("--note", default="")
    s = sub.add_parser("status")
    s.add_argument("video_id")
    i = sub.add_parser("import")
    i.add_argument("video_id")
    i.add_argument("--since", required=True)
    i.add_argument("--times", default="")
    i.add_argument("--note", default="")
    i.add_argument("--dir", default=os.path.join(os.path.expanduser("~"), "Downloads"))
    i.add_argument("--no-sheet", action="store_true")
    i.add_argument("--no-notify", action="store_true")
    args = ap.parse_args(argv)
    result = {"check": cmd_check, "mark": cmd_mark, "pending": cmd_pending, "done": cmd_done,
              "status": cmd_status, "import": cmd_import}[args.cmd](args)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
