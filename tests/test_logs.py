from __future__ import annotations

import json
import logging

from app.logs import REDACTED, JsonFormatter, log_event, redact


def test_redacts_secret_and_content_keys() -> None:
    out = redact(
        {
            "record_id": "123",
            "api_key": "k",
            "upload_token": "t",
            "download_url": "https://s3/x?sig",
            "transcript": "本文",
            "nested": {"summary": "要約", "chars": 10},
            "items": [{"text": "発言"}],
        }
    )
    assert out["record_id"] == "123"
    assert out["api_key"] == REDACTED
    assert out["upload_token"] == REDACTED
    assert out["download_url"] == REDACTED
    assert out["transcript"] == REDACTED
    assert out["nested"] == {"summary": REDACTED, "chars": 10}
    assert out["items"] == [{"text": REDACTED}]


def test_json_formatter_outputs_cloud_logging_fields() -> None:
    logger = logging.getLogger("test.json")
    record = logger.makeRecord("test.json", logging.WARNING, __file__, 1, "pipeline.step", (), None)
    record.fields = {"record_id": "r1", "refresh_token": "secret"}
    data = json.loads(JsonFormatter().format(record))
    assert data["severity"] == "WARNING"
    assert data["message"] == "pipeline.step"
    assert data["record_id"] == "r1"
    assert data["refresh_token"] == REDACTED


def test_log_event_attaches_fields(caplog) -> None:
    logger = logging.getLogger("test.event")
    with caplog.at_level(logging.INFO):
        log_event(logger, "x.done", record_id="r1")
    assert caplog.records[0].fields == {"record_id": "r1"}
