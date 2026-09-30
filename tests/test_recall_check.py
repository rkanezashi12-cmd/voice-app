"""scripts/recall_check.py：Recall.ai の応答の形だけを表示し、URL・人名・発言を出さないこと。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "recall_check.py"
_spec = importlib.util.spec_from_file_location("recall_check", _PATH)
assert _spec and _spec.loader
rc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rc)


def test_shape_shows_ids_and_statuses_but_hides_urls_and_names() -> None:
    bot = {
        "id": "bot-1",
        "meeting_url": "https://zoom.us/j/123?pwd=secret",
        "metadata": {"client_id": "default", "record_id": "4928352000068964038"},
        "recordings": [
            {
                "id": "rec-1",
                "media_shortcuts": {
                    "transcript": {
                        "status": {"code": "done"},
                        "data": {"download_url": "https://storage.example/t.json?sig=abc"},
                    }
                },
            }
        ],
        "meeting_participants": [{"name": "山田太郎", "id": 5}],
    }
    text = "\n".join(rc.shape(bot))
    assert 'metadata.record_id = "4928352000068964038"' in text
    assert 'recordings[0].media_shortcuts.transcript.status.code = "done"' in text
    assert "recordings[0].media_shortcuts.transcript.data.download_url = <URL・非表示>" in text
    assert "meeting_url = <URL・非表示>" in text
    assert "meeting_participants[0].name = <文字列 4文字>" in text
    assert "meeting_participants[0].id = 5" in text
    for secret in ("pwd=secret", "sig=abc", "山田太郎", "storage.example"):
        assert secret not in text


def test_shape_hides_urls_even_under_safe_keys() -> None:
    assert rc.shape({"status": "https://example.com/x"}) == ["status = <URL・非表示>"]


def test_shape_limits_long_lists() -> None:
    lines = rc.shape({"status_changes": [{"code": f"c{i}"} for i in range(25)]})
    assert lines[0] == "status_changes = [25件]"
    assert lines[-1] == "status_changes[20〜] = <省略>"


def test_show_rejects_unexpected_ids() -> None:
    with pytest.raises(SystemExit):
        rc.show("bot", "../../admin")


def test_main_prints_usage_for_unknown_commands(capsys: pytest.CaptureFixture[str]) -> None:
    assert rc.main(["delete", "bot-1"]) == 2
    assert "check-key" in capsys.readouterr().out
