"""/internal/* は Cloud Tasks の OIDC トークン（決められたサービスアカウント）だけを受け付ける。"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.runtime import Runtime
from tests.conftest import SERVICE_URL, TASKS_SA

BODY = {"client_id": "default", "source": "web_recording", "record_id": "r1"}


@pytest.fixture
def raw_client(runtime: Runtime) -> Iterator[TestClient]:
    with TestClient(create_app(runtime)) as c:
        yield c


@pytest.fixture
def fake_verify(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"claims": None, "error": None, "calls": []}

    def verify(token: str, request: Any, audience: str) -> dict[str, Any]:
        state["calls"].append((token, audience))
        if state["error"]:
            raise ValueError(state["error"])
        return state["claims"]

    monkeypatch.setattr("google.oauth2.id_token.verify_oauth2_token", verify)
    return state


def test_rejects_missing_token(raw_client: TestClient) -> None:
    assert raw_client.post("/internal/process", json=BODY).status_code == 401
    assert (
        raw_client.post("/internal/recall-event", json={"client_id": "default", "payload": {}}).status_code
        == 401
    )


def test_rejects_invalid_token(raw_client: TestClient, fake_verify: dict[str, Any]) -> None:
    fake_verify["error"] = "bad signature"
    res = raw_client.post("/internal/process", json=BODY, headers={"Authorization": "Bearer x"})
    assert res.status_code == 401


def test_rejects_other_service_accounts(raw_client: TestClient, fake_verify: dict[str, Any]) -> None:
    fake_verify["claims"] = {"email": "someone@example.com", "email_verified": True}
    res = raw_client.post("/internal/process", json=BODY, headers={"Authorization": "Bearer x"})
    assert res.status_code == 403


def test_accepts_tasks_service_account(raw_client: TestClient, fake_verify: dict[str, Any]) -> None:
    fake_verify["claims"] = {"email": TASKS_SA, "email_verified": True}
    res = raw_client.post(
        "/internal/recall-event",
        json={"client_id": "default", "payload": {"event": "unknown.event"}},
        headers={"Authorization": "Bearer good"},
    )
    assert res.status_code == 200
    assert fake_verify["calls"] == [("good", SERVICE_URL)], "audience はサービスの URL"
