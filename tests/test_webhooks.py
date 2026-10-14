from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from app.clients import ClientRegistry
from app.main import create_app
from app.runtime import Runtime
from app.services.tasks import task_name
from app.webhook_signature import SignatureError, sign, verify
from tests.conftest import (
    WEBHOOK_SECRET,
    FakeLlm,
    FakeStorage,
    FakeTasks,
    NoNetwork,
    base_client_config,
    make_settings,
)


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


# ---- 同じ Recall.ai のアカウントを複数のクライアントで使うとき（metadata の client_id で振り分ける） ----

OTHER_SECRET = "whsec_b3RoZXItc2VjcmV0LWZvci10ZXN0"


def bot_event(name: str, client_id: str | None, record_id: str = "1234567890") -> bytes:
    metadata = {"record_id": record_id}
    if client_id is not None:
        metadata["client_id"] = client_id
    payload = {
        "event": name,
        "data": {"data": {"code": name.split(".")[1]}, "bot": {"id": "b1", "metadata": metadata}},
    }
    return json.dumps(payload).encode()


@pytest.fixture
def shared_registry(monkeypatch: pytest.MonkeyPatch) -> ClientRegistry:
    monkeypatch.setenv("TEST_OTHER_WEBHOOK_SECRET", OTHER_SECRET)
    config = base_client_config()
    default = config["clients"]["default"]
    shared = {"api_key": "env:TEST_RECALL_API_KEY", "webhook_secret": "env:TEST_RECALL_WEBHOOK_SECRET"}
    config["clients"]["monodemo"] = {"zoho": default["zoho"], "recall": shared}
    config["clients"]["separate"] = {
        "zoho": default["zoho"],
        "recall": {"api_key": "env:TEST_RECALL_API_KEY", "webhook_secret": "env:TEST_OTHER_WEBHOOK_SECRET"},
    }
    return ClientRegistry.from_dict(config)


@pytest.fixture
def shared_client(
    shared_registry: ClientRegistry, tasks: FakeTasks, storage: FakeStorage
) -> Iterator[TestClient]:
    rt = Runtime(
        make_settings(),
        shared_registry,
        storage=storage,
        tasks=tasks,
        llm=FakeLlm(),
        http=httpx.AsyncClient(transport=NoNetwork()),
    )
    with TestClient(create_app(rt)) as c:
        yield c


def _post(c: TestClient, body: bytes, secret: str = WEBHOOK_SECRET, msg_id: str = "msg_1") -> dict:
    res = c.post("/webhooks/recall", content=body, headers=signed_headers(body, msg_id=msg_id, secret=secret))
    assert res.status_code == 200
    return res.json()


@pytest.mark.parametrize("client_id", ["default", "monodemo"])
def test_shared_account_routes_by_metadata_client_id(
    shared_client: TestClient, tasks: FakeTasks, client_id: str
) -> None:
    assert _post(shared_client, bot_event("bot.done", client_id)) == {"ok": True, "queued": True}
    assert [t["payload"]["client_id"] for t in tasks.enqueued] == [client_id]
    assert tasks.enqueued[0]["name"] == task_name("wh", client_id, "msg_1")


def test_shared_account_without_client_id_is_not_processed(
    shared_client: TestClient, tasks: FakeTasks, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    assert _post(shared_client, bot_event("bot.done", None)) == {"ok": True, "queued": False}
    assert tasks.enqueued == []
    assert any(r.getMessage() == "webhook.client_unresolved" for r in caplog.records)


@pytest.mark.parametrize("claimed", ["separate", "unknown"])
def test_client_id_must_match_a_client_whose_secret_signed_it(
    shared_client: TestClient, tasks: FakeTasks, claimed: str
) -> None:
    # 共有のシークレットで署名された通知が、別のシークレットのクライアント（や無いクライアント）を名乗っても処理しない
    assert _post(shared_client, bot_event("bot.done", claimed)) == {"ok": True, "queued": False}
    assert tasks.enqueued == []


def test_single_verified_client_is_used_without_metadata(shared_client: TestClient, tasks: FakeTasks) -> None:
    assert _post(shared_client, bot_event("bot.done", None), secret=OTHER_SECRET) == {
        "ok": True,
        "queued": True,
    }
    assert tasks.enqueued[0]["payload"]["client_id"] == "separate"


def test_shared_account_rejects_bad_signature(shared_client: TestClient, tasks: FakeTasks) -> None:
    body = bot_event("bot.done", "monodemo")
    headers = signed_headers(body, secret="whsec_" + "QUJD" * 8)
    assert shared_client.post("/webhooks/recall", content=body, headers=headers).status_code == 401
    assert tasks.enqueued == []
