"""リプレイの字幕（services/replay-caption-model.js）の純粋ロジックの単体検証。"""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which('node') is None, reason='node が見つかりません')
def test_replay_caption_model():
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        ['node', str(root / 'tests/js/replay_caption_model_check.mjs')],
        cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
