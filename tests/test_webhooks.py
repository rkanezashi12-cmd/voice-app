from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from app.webhook_signature import SignatureError, sign, verify
from tests.conftest import WEBHOOK_SECRET, FakeTasks


def signed_headers(
    body: bytes, msg_id: str = "msg_1", ts: int | None = None, secret: str = WEBHOOK_SECRET
) -> dict:
    ts = int(time.time()) if ts is None else ts
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": str(ts),
        "webhook-signature": sign(secret, msg_id, ts, body),
        "Content-Type": "application/json",
    }


def event(name: str = "bot.done", bot_id: str = "bot-1") -> bytes:
    payload = {
        "event": name,
        "data": {"data": {"code": name.split(".")[1]}, "bot": {"id": bot_id, "metadata": {}}},
    }
    return json.dumps(payload).encode()


def test_verify_accepts_valid_and_rejects_tampered() -> None:
    body = b'{"event":"bot.done"}'
    headers = {k.lower(): v for k, v in signed_headers(body).items()}
    assert verify(WEBHOOK_SECRET, headers, body) == "msg_1"
    with pytest.raises(SignatureError):
        verify(WEBHOOK_SECRET, headers, body + b" ")
    with pytest.raises(SignatureError):
        verify("whsec_b3RoZXItc2VjcmV0", headers, body)


def test_verify_rejects_old_timestamp_and_accepts_rotated_signatures() -> None:
    body = b"{}"
    old = {k.lower(): v for k, v in signed_headers(body, ts=int(time.time()) - 3600).items()}
    with pytest.raises(SignatureError):
        verify(WEBHOOK_SECRET, old, body)
    headers = {k.lower(): v for k, v in signed_headers(body).items()}
    headers["webhook-signature"] = "v1,AAAA " + headers["webhook-signature"]
    assert verify(WEBHOOK_SECRET, headers, body) == "msg_1"
    svix = {k.replace("webhook-", "svix-"): v for k, v in headers.items()}
    assert verify(WEBHOOK_SECRET, svix, body) == "msg_1"


def test_webhook_enqueues_task_and_returns_immediately(client: TestClient, tasks: FakeTasks) -> None:
    body = event("bot.done")
    res = client.post("/webhooks/recall", content=body, headers=signed_headers(body))
    assert res.status_code == 200
    assert res.json() == {"ok": True, "queued": True}
    task = tasks.enqueued[0]
    assert task["path"] == "/internal/recall-event"
    assert task["payload"]["client_id"] == "default"
    assert task["payload"]["payload"]["event"] == "bot.done"


def test_duplicate_notifications_are_queued_once(client: TestClient, tasks: FakeTasks) -> None:
    body = event("bot.in_waiting_room")
    for _ in range(3):
        res = client.post("/webhooks/recall", content=body, headers=signed_headers(body, msg_id="msg_dup"))
        assert res.status_code == 200
    assert len(tasks.enqueued) == 1


def test_invalid_signature_is_rejected(client: TestClient, tasks: FakeTasks) -> None:
    body = event()
    headers = signed_headers(body, secret="whsec_b3RoZXItc2VjcmV0LWZvci10ZXN0")
    assert client.post("/webhooks/recall", content=body, headers=headers).status_code == 401
    assert client.post("/webhooks/recall", content=body).status_code == 401
    assert tasks.enqueued == []


def test_unhandled_events_are_acknowledged_but_not_queued(client: TestClient, tasks: FakeTasks) -> None:
    body = json.dumps({"event": "participant_events.join", "data": {}}).encode()
    res = client.post("/webhooks/recall", content=body, headers=signed_headers(body))
    assert res.json() == {"ok": True, "queued": False}
    assert tasks.enqueued == []
