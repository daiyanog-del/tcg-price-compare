"""
tests/test_startup_jobs_disabled.py — DISABLE_STARTUP_JOBS=1 のとき app.py の import で
バックグラウンドのスレッドを1本も起動しないことを確かめる。

経緯（2026-10-22 CI run 39750671987）: 相場キャッシュ・Tier表・パック収録カードの先読みは
_claim_startup_job（.cache/startup_*.lock のファイルロック）だけで起動を判定しており、
DISABLE_STARTUP_JOBS の判定が届いていなかった。手元ではロックが5分以内に作られていて
起動されないことが多いが、ロックの無い新しい checkout（CI）では import 時にパック先読みが走る。
そのスレッドが数秒後に yugioh-wiki を取りに行き、requests.get を差し替えた
tests/test_solitaire_og.py の net フィクスチャに URL が記録されて、たまに落ちていた。

ロックファイルの有無で結果が変わらないよう、子プロセスの中で「ロックは無い（いつでも取れる）」
状態を作ってから import する。直す前のコードでは3本のスレッドが起動して、このテストは落ちる。
"""
import json
import os
import subprocess
import sys
import textwrap

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 子プロセスで実行する。startup_*.lock への問い合わせは「無い」と答え、作成は一時ディレクトリへ
# 逃がす（手元の本物のロックには触れない）。import 中に起動したスレッドの target を記録する。
_CHILD = textwrap.dedent("""
    import json, os, sys, tempfile, threading
    tmp = tempfile.mkdtemp()
    _exists, _open, _remove = os.path.exists, os.open, os.remove
    is_lock = lambda p: os.path.basename(str(p)).startswith("startup_") and str(p).endswith(".lock")
    os.path.exists = lambda p: False if is_lock(p) else _exists(p)
    os.open = lambda p, *a, **k: _open(os.path.join(tmp, os.path.basename(p)) if is_lock(p) else p, *a, **k)
    os.remove = lambda p, *a, **k: None if is_lock(p) else _remove(p, *a, **k)

    started = []
    _start = threading.Thread.start
    def start(self):
        target = getattr(self, "_target", None)
        started.append(getattr(target, "__qualname__", repr(target)))
        return _start(self)
    threading.Thread.start = start

    sys.path.insert(0, os.getcwd())
    import app  # noqa: F401
    print("STARTED=" + json.dumps(started))
    sys.stdout.flush()
    os._exit(0)  # 起動してしまったスレッドの終了を待たない
""")


def _threads_started_on_import(disable: str | None):
    env = dict(os.environ)
    env.pop("DISABLE_STARTUP_JOBS", None)
    if disable is not None:
        env["DISABLE_STARTUP_JOBS"] = disable
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run([sys.executable, "-c", _CHILD], cwd=ROOT, env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    line = next((l for l in proc.stdout.splitlines() if l.startswith("STARTED=")), None)
    assert line, f"子プロセスが結果を出さなかった: rc={proc.returncode}\n{proc.stderr[-2000:]}"
    return json.loads(line[len("STARTED="):])


def test_import_starts_no_threads_when_startup_jobs_disabled():
    assert _threads_started_on_import("1") == []


def test_claim_startup_job_is_refused_without_touching_lock(monkeypatch, tmp_path):
    import app as app_module
    touched = []
    monkeypatch.setattr(app_module.os, "makedirs", lambda *a, **k: touched.append(a))
    monkeypatch.setenv("DISABLE_STARTUP_JOBS", "1")
    assert app_module._claim_startup_job("anything") is False
    assert touched == []
