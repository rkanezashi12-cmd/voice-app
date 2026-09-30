from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.errors import ExternalServiceError
from app.routers.bots import compute_join_at
from tests.conftest import API_KEY, F, FakeCrm, FakeRecall, S

HEADERS = {"X-API-Key": API_KEY}
ZOOM = "https://us02web.zoom.us/j/123456789?pwd=abc"


def test_schedules_bot_and_writes_recall_id_once(
    client: TestClient, recall: FakeRecall, crm: FakeCrm
) -> None:
    start = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    res = client.post(
        "/api/bots", json={"record_id": "5001", "meeting_url": ZOOM, "start_at": start}, headers=HEADERS
    )
    assert res.status_code == 200
    body = res.json()
    assert body["bot_id"] == "bot-1"
    assert body["join_at"] is not None
    created = recall.created_bots[0]
    assert created["metadata"] == {"client_id": "default", "record_id": "5001"}
    assert created["bot_name"] == "議事録ボット（録音中）"
    assert crm.writes_to("5001") == [{F.recall_id: "bot-1", F.status: S.reserved, F.error_message: None}]


def test_joins_immediately_when_meeting_is_soon(client: TestClient, recall: FakeRecall) -> None:
    start = (datetime.now(UTC) + timedelta(minutes=3)).isoformat()
    client.post(
        "/api/bots", json={"record_id": "5001", "meeting_url": ZOOM, "start_at": start}, headers=HEADERS
    )
    assert recall.created_bots[0]["join_at"] is None


def test_compute_join_at() -> None:
    now = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    assert compute_join_at(now + timedelta(hours=1), now, 60) == now + timedelta(minutes=59)
    assert compute_join_at(now + timedelta(minutes=5), now, 60) is None
    assert compute_join_at(None, now, 60) is None


def test_recall_failure_is_written_to_crm(client: TestClient, recall: FakeRecall, crm: FakeCrm) -> None:
    recall.fail_create = ExternalServiceError("recall", "空きボットがありません", status=507)
    res = client.post("/api/bots", json={"record_id": "5001", "meeting_url": ZOOM}, headers=HEADERS)
    assert res.status_code == 502
    write = crm.writes_to("5001")[0]
    assert write[F.status] == S.failed
    assert "ボットを予約できませんでした" in write[F.error_message]
    assert "開始日時を10分以上先にして" in write[F.error_message], (
        "507 は空き不足。予約にすれば起きないと案内する"
    )
    assert "空きボットがありません" in write[F.error_message], "Recall.ai が返した理由も残す"


def test_other_recall_failures_have_no_schedule_hint(
    client: TestClient, recall: FakeRecall, crm: FakeCrm
) -> None:
    recall.fail_create = ExternalServiceError("recall", "meeting_url が不正です", status=400)
    res = client.post("/api/bots", json={"record_id": "5001", "meeting_url": ZOOM}, headers=HEADERS)
    assert res.status_code == 502
    message = crm.writes_to("5001")[0][F.error_message]
    assert "meeting_url が不正です" in message
    assert "開始日時を10分以上先にして" not in message


def test_rejects_non_meeting_urls(client: TestClient, recall: FakeRecall) -> None:
    for url in ["http://zoom.us/j/1", "https://evil.example.com/zoom.us", "https://zoom.us.evil.com/j/1"]:
        res = client.post("/api/bots", json={"record_id": "5001", "meeting_url": url}, headers=HEADERS)
        assert res.status_code == 422, url
    for url in ["https://teams.microsoft.com/l/meetup-join/x", "https://meet.google.com/abc-defg-hij"]:
        res = client.post("/api/bots", json={"record_id": "5002", "meeting_url": url}, headers=HEADERS)
        assert res.status_code == 200, url


def test_requires_timezone_in_start_at(client: TestClient) -> None:
    res = client.post(
        "/api/bots",
        json={"record_id": "5001", "meeting_url": ZOOM, "start_at": "2026-10-01T10:00:00"},
        headers=HEADERS,
    )
    assert res.status_code == 422


def test_requires_api_key(client: TestClient) -> None:
    assert client.post("/api/bots", json={"record_id": "5001", "meeting_url": ZOOM}).status_code == 401
