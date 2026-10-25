from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.errors import ExternalServiceError
from app.routers.bots import compute_join_at, with_passcode
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


@pytest.mark.parametrize(
    ("url", "expected", "state"),
    [
        # Zoom：URL にパスコードが無ければ pwd を足す（ほかのクエリは残す）
        ("https://us02web.zoom.us/j/123", "https://us02web.zoom.us/j/123?pwd=a1B2", "added"),
        ("https://zoom.us/j/123?omn=9", "https://zoom.us/j/123?omn=9&pwd=a1B2", "added"),
        ("https://zoom.us/j/123?pwd=", "https://zoom.us/j/123?pwd=a1B2", "added"),
        # URL にパスコードがあれば、招待の URL のほうを使う
        ("https://zoom.us/j/123?pwd=enc", "https://zoom.us/j/123?pwd=enc", "in_url"),
        # Teams の短い URL（/meet/<会議 ID>）は p
        (
            "https://teams.microsoft.com/meet/4012?x=1",
            "https://teams.microsoft.com/meet/4012?x=1&p=a1B2",
            "added",
        ),
        ("https://teams.live.com/meet/9876", "https://teams.live.com/meet/9876?p=a1B2", "added"),
        (
            "https://teams.microsoft.com/meet/4012?p=zz",
            "https://teams.microsoft.com/meet/4012?p=zz",
            "in_url",
        ),
        # パスコードを入れる場所が無い URL は変えない
        (
            "https://teams.microsoft.com/l/meetup-join/x",
            "https://teams.microsoft.com/l/meetup-join/x",
            "unused",
        ),
        ("https://meet.google.com/abc-defg-hij", "https://meet.google.com/abc-defg-hij", "unused"),
    ],
)
def test_with_passcode(url: str, expected: str, state: str) -> None:
    assert with_passcode(url, "a1B2") == (expected, state)


def test_with_passcode_without_passcode_keeps_url() -> None:
    assert with_passcode("https://zoom.us/j/123", None) == ("https://zoom.us/j/123", "none")
    assert with_passcode("https://zoom.us/j/123?pwd=enc", None) == ("https://zoom.us/j/123?pwd=enc", "in_url")
    # 記号は URL の中で使える形にする
    assert with_passcode("https://zoom.us/j/1", "a+b&c")[0] == "https://zoom.us/j/1?pwd=a%2Bb%26c"


def test_passcode_is_added_to_meeting_url(
    client: TestClient, recall: FakeRecall, crm: FakeCrm, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        res = client.post(
            "/api/bots",
            # 招待からのコピーで全角・空白が混じっても使える
            json={
                "record_id": "5001",
                "meeting_url": "https://zoom.us/j/123",
                "meeting_passcode": " ａ1B 2 ",
            },
            headers=HEADERS,
        )
    assert res.status_code == 200
    assert recall.created_bots[0]["meeting_url"] == "https://zoom.us/j/123?pwd=a1B2"
    assert crm.writes_to("5001")[0][F.status] == S.reserved
    reserved = [r for r in caplog.records if r.getMessage() == "bot.reserved"]
    assert reserved[0].fields["passcode_state"] == "added"
    logged = " ".join(f"{r.getMessage()} {getattr(r, 'fields', '')}" for r in caplog.records)
    assert "a1B2" not in logged, "パスコードはログに出さない"


def test_bad_passcode_is_written_to_crm(client: TestClient, recall: FakeRecall, crm: FakeCrm) -> None:
    res = client.post(
        "/api/bots",
        json={"record_id": "5001", "meeting_url": "https://zoom.us/j/123", "meeting_passcode": "パスコード"},
        headers=HEADERS,
    )
    assert res.status_code == 422
    assert "パスコード" not in res.text.replace("会議パスコード", ""), "入れた値を応答に出さない"
    assert recall.created_bots == []
    write = crm.writes_to("5001")[0]
    assert write[F.status] == S.failed
    assert "会議パスコードは半角" in write[F.error_message]
