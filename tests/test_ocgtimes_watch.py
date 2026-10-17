"""OCGタイムズの告知検知・配信状態の解析・キャプチャの集め方（tools/ocgtimes_watch.py）を検証する。"""

import io
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import ocgtimes_watch as ow  # noqa: E402

ow._load, ow._save = ow.load_state, ow.save_state

TOP = """
<li><a href="information/ocgtimesimph/" class=""><img src="/japan/assets/banner/ocgtimes-imph.jpg" alt="遊戯王OCGタイムズ"></a></li>
<li class="other"><a href="information/ocgtimesimph/" class="">「遊戯王OCGタイムズ2026年10月版」配信情報公開</a></li>
<li class="other"><a href="/japan/information/ocgtimesbetb/" class="">「遊戯王OCGタイムズ2026年7月版」配信情報公開</a></li>
<li class="event"><a href="event/yugioh_day/" class="">遊戯王の日</a></li>
"""


def test_parse_top_finds_announcements_and_prefers_text_heading():
    assert ow.parse_top(TOP) == {
        "ocgtimesimph": "「遊戯王OCGタイムズ2026年10月版」配信情報公開",
        "ocgtimesbetb": "「遊戯王OCGタイムズ2026年7月版」配信情報公開",
    }


def test_parse_page_takes_video_ids_not_channel_links():
    html = ('<title>遊戯王OCGタイムズ 2026年10月版 | ニュース</title>'
            '<a href="https://youtube.com/live/v-QIhn_zLoM">配信</a>'
            '<a href="https://www.youtube.com/user/yugiohOCGch?sub_confirmation=1">登録</a>'
            '<a href="https://www.youtube.com/@yugiohOCGch/">チャンネル</a>'
            '<a href="https://youtu.be/v-QIhn_zLoM">同じ動画</a>')
    assert ow.parse_page(html) == {"title": "遊戯王OCGタイムズ 2026年10月版 | ニュース", "video_ids": ["v-QIhn_zLoM"]}


def _watch_html(details: dict, desc: str, extra: str = "") -> str:
    video = {"title": "【公式】「遊戯王OCGタイムズ」", "shortDescription": desc}
    return (f'var ytInitialPlayerResponse = {{"videoDetails":{json.dumps(video, ensure_ascii=False)},'
            f'"microformat":{{"playerMicroformatRenderer":{{"liveBroadcastDetails":{json.dumps(details)}}}}}{extra}}};')


DESC = ("▼今回のラインナップ\n00:00 配信開始\n12:52 新規「六花」カード公開\n"
        "1:38:42  ┗新規カード「パワー・オブ・ザ・デュエリスト」公開\n1:43:20 エンディング\n"
        "1:47:13 本日の新規カードまとめ\n※本番組は期間限定")


def test_parse_watch_after_stream():
    info = ow.parse_watch(_watch_html(
        {"isLiveNow": False, "startTimestamp": "2026-10-16T10:59:11+00:00", "endTimestamp": "2026-10-16T12:52:02+00:00"},
        DESC))
    assert info["start"] == "2026-10-16T10:59:11+00:00" and info["end"] == "2026-10-16T12:52:02+00:00"
    assert info["summary_seconds"] == 6433
    assert [c["seconds"] for c in info["chapters"]] == [0, 772, 5922, 6200, 6433]
    assert ow.is_ended(info)


def test_parse_watch_before_and_during_stream():
    before = ow.parse_watch(_watch_html({"isLiveNow": False, "startTimestamp": "2027-01-15T10:59:00+00:00"},
                                        "まだ目次なし", ',"scheduledStartTime":"1799319540"'))
    assert before["end"] is None and not ow.is_ended(before)
    assert before["scheduled_start"].startswith("2027-01-")
    assert before["summary_seconds"] is None
    live = ow.parse_watch(_watch_html({"isLiveNow": True, "startTimestamp": "2027-01-15T10:59:00+00:00"}, DESC))
    assert not ow.is_ended(live)


def test_parse_watch_handles_braces_inside_strings():
    info = ow.parse_watch(_watch_html({"isLiveNow": False, "startTimestamp": "x", "endTimestamp": "y"},
                                      "記号 {テスト} \"引用\"\n1:47:13 本日の新規カード一覧"))
    assert info["summary_seconds"] == 6433 and info["end"] == "y"


def test_state_roundtrip(tmp_path):
    p = str(tmp_path / "s.json")
    assert ow.load_state(p) == {"seen": {}}
    ow.save_state({"seen": {"ocgtimesimph": {"at": "x"}}}, p)
    assert ow.load_state(p)["seen"] == {"ocgtimesimph": {"at": "x"}}


def _png(color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, format="PNG")
    return buf.getvalue()


def test_collect_frames_by_content_order_and_dedupe(tmp_path):
    since = datetime.now(timezone.utc) - timedelta(seconds=5)
    old = tmp_path / "old.png"
    old.write_bytes(_png((9, 9, 9)))
    t_old = time.time() - 3600
    os.utime(old, (t_old, t_old))
    a, b, dup, txt = (tmp_path / "a.tmp"), (tmp_path / "b.png"), (tmp_path / "c.tmp"), (tmp_path / "d.txt")
    a.write_bytes(_png((1, 0, 0)))
    b.write_bytes(_png((2, 0, 0)))
    dup.write_bytes(_png((1, 0, 0)))
    txt.write_text("not image")
    now = time.time()
    for i, f in enumerate([a, b, dup, txt]):
        os.utime(f, (now + i, now + i))

    frames = ow.collect_frames(str(tmp_path), since)

    # 中身で画像と判定（.tmp でも可）、更新時刻の順、同じ中身は1回、古いものは対象外
    assert [os.path.basename(p) for p, _, _ in frames] == ["a.tmp", "b.png"]
    assert all(kind == "image/png" for _, _, kind in frames)


def test_cmd_import_uses_times_and_keeps_frames(tmp_path, monkeypatch):
    import types
    import unreleased_upload as up
    since = datetime.now(timezone.utc) - timedelta(seconds=5)
    for i, color in enumerate([(1, 0, 0), (2, 0, 0)]):
        f = tmp_path / f"{i}.tmp"
        f.write_bytes(_png(color))
        os.utime(f, (time.time() + i, time.time() + i))
    calls = []

    def fake_import(sb, **kw):
        calls.append(kw)
        return {"inserted": [{"id": len(calls), "name": f"カード{len(calls)}"}], "skipped": [],
                "images": {len(calls): "取込成功"}, "extracted": 1}

    monkeypatch.setattr(up, "import_uploaded_image", fake_import)
    monkeypatch.setitem(sys.modules, "supabase", types.SimpleNamespace(create_client=lambda *a: object()))
    monkeypatch.setenv("SUPABASE_URL", "https://x.example")
    monkeypatch.setenv("SUPABASE_KEY", "k")
    monkeypatch.setattr(ow, "FRAMES_ROOT", str(tmp_path / "keep"))
    notes = []
    monkeypatch.setattr(ow, "_notify", notes.append)
    args = types.SimpleNamespace(video_id="v-QIhn_zLoM", since=since.isoformat(), times="6448,6458",
                                 note="メモ", dir=str(tmp_path), no_sheet=True, no_notify=False,
                                 layout="list", product="")

    out = ow.cmd_import(args)

    assert [c["source_url"] for c in calls] == ["https://www.youtube.com/watch?v=v-QIhn_zLoM&t=6448s",
                                                "https://www.youtube.com/watch?v=v-QIhn_zLoM&t=6458s"]
    assert [c["filename"] for c in calls] == ["list1_t6448.png", "list2_t6458.png"]
    assert calls[0]["data_url"].startswith("data:image/png;base64,") and calls[0]["note"] == "メモ"
    assert sorted(os.listdir(tmp_path / "keep" / "v-QIhn_zLoM")) == ["list1_t6448.png", "list2_t6458.png"]
    assert out["summary"] == "2画面・追加2枚" and "追加2枚" in notes[0]


def test_cmd_import_rejects_mismatched_times(tmp_path, monkeypatch):
    import types
    f = tmp_path / "a.png"
    f.write_bytes(_png((1, 0, 0)))
    args = types.SimpleNamespace(video_id="x", since=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                                 times="1,2", note="", dir=str(tmp_path), no_sheet=True, no_notify=True)
    monkeypatch.setattr(ow, "_load_env", lambda: None)
    assert "合いません" in ow.cmd_import(args)["error"]


def test_pending_flow(tmp_path, monkeypatch):
    import types
    monkeypatch.setattr(ow, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(ow, "load_state", lambda path=None: ow.__dict__["_load"](str(tmp_path / "state.json")))
    monkeypatch.setattr(ow, "save_state", lambda st, path=None: ow.__dict__["_save"](st, str(tmp_path / "state.json")))
    ended = {"v": False}
    html = lambda: _watch_html({"isLiveNow": not ended["v"], "startTimestamp": "x",
                                **({"endTimestamp": "y"} if ended["v"] else {})}, DESC)
    monkeypatch.setattr(ow, "_get", lambda url: html())
    future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()

    ow.cmd_mark(types.SimpleNamespace(slugs=["ocgtimesnext"], note="", video="VID00000001", start=future, url="", heading="1月版"))
    ow.cmd_mark(types.SimpleNamespace(slugs=["ocgtimesnow"], note="", video="VID00000002", start=past, url="", heading="今"))
    out = {p["video_id"]: p for p in ow.cmd_pending(None)["pending"]}
    assert out["VID00000001"]["ready"] is False and "ended" not in out["VID00000001"]   # まだ始まっていない
    assert out["VID00000002"]["ready"] is False and out["VID00000002"]["ended"] is False  # 配信中
    ended["v"] = True
    out = {p["video_id"]: p for p in ow.cmd_pending(None)["pending"]}
    assert out["VID00000002"]["ready"] is True and out["VID00000002"]["summary_seconds"] == 6433

    assert ow.cmd_done(types.SimpleNamespace(video_id="VID00000002", note="取込済み"))["found"] is True
    assert [p["video_id"] for p in ow.cmd_pending(None)["pending"]] == ["VID00000001"]
