from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.errors import ExternalServiceError
from tests.conftest import API_KEY, FM, F, FakeCrm, FakeStorage, FakeTasks, recording_token

SESSION = "1727488500123-a-abc123"


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_issue_test_url_requires_api_key(client: TestClient) -> None:
    res = client.post("/api/recordings", json={"record_id": "test-1", "test": True})
    assert res.status_code == 401
    res = client.post(
        "/api/recordings", json={"record_id": "test-1", "test": True}, headers={"X-API-Key": "wrong"}
    )
    assert res.status_code == 401


def test_issue_test_url(client: TestClient) -> None:
    res = client.post(
        "/api/recordings", json={"record_id": "test-1", "test": True}, headers={"X-API-Key": API_KEY}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["recording_url"].startswith("https://meeting-notes.example.run.app/recorder/#")
    assert body["crm_updated"] is False
    token = body["recording_url"].split("#", 1)[1]
    info = client.get("/api/recordings/test-1/session", headers=auth(token)).json()
    assert info["test"] is True
    assert info["record_id"] == "test-1"


def test_session_rejects_other_record(client: TestClient) -> None:
    token = recording_token("rec-1")
    assert client.get("/api/recordings/rec-2/session", headers=auth(token)).status_code == 403
    assert client.get("/api/recordings/rec-1/session").status_code == 401
    assert client.get("/api/recordings/rec-1/session", headers=auth("x.y")).status_code == 401


def test_session_refuses_processed_record(client: TestClient, crm: FakeCrm) -> None:
    """処理が済んだ商談記録では録音させない（録音しても共通処理が処理を飛ばして音声を消すため）。"""
    crm.add(F.module, "5001", {F.status: FM.status.no_account, F.transcript: "話者A: よろしくお願いします"})
    res = client.get("/api/recordings/5001/session", headers=auth(recording_token("5001")))
    assert res.status_code == 409
    assert "新しい商談記録" in res.json()["detail"]
    assert crm.writes == []


def test_session_allows_record_not_yet_processed(client: TestClient, crm: FakeCrm) -> None:
    """未処理・失敗した商談記録は録音できる（失敗したら録音し直せる）。"""
    crm.add(F.module, "5002", {F.status: FM.status.failed})
    crm.add(F.module, "5003", {})
    for record_id in ("5002", "5003"):
        res = client.get(f"/api/recordings/{record_id}/session", headers=auth(recording_token(record_id)))
        assert res.status_code == 200
        assert res.json()["test"] is False


def test_session_reports_missing_record(client: TestClient) -> None:
    res = client.get("/api/recordings/5004/session", headers=auth(recording_token("5004")))
    assert res.status_code == 404
    assert "見つかりません" in res.json()["detail"]


def test_session_does_not_block_recording_when_crm_is_unavailable(client: TestClient, crm: FakeCrm) -> None:
    """CRM に問い合わせられないときは録音を止めない（商談の場で録音の機会を逃さない）。"""

    async def unavailable(*_: Any) -> None:
        raise ExternalServiceError("zoho_crm", "一時的に使えません", status=503, retryable=True)

    crm.get_record = unavailable  # type: ignore[method-assign]
    res = client.get("/api/recordings/5005/session", headers=auth(recording_token("5005")))
    assert res.status_code == 200


def test_expired_token(client: TestClient) -> None:
    token = recording_token("rec-1", ttl=-10)
    res = client.get("/api/recordings/rec-1/session", headers=auth(token))
    assert res.status_code == 401
    assert "有効期限" in res.json()["detail"]


def test_upload_url(client: TestClient, storage: FakeStorage) -> None:
    token = recording_token("rec-1")
    res = client.post(
        "/api/recordings/rec-1/upload-url",
        json={"session_id": SESSION, "seq": 3, "mime_type": "audio/mp4;codecs=mp4a.40.2"},
        headers=auth(token),
    )
    assert res.status_code == 200
    body = res.json()
    assert body["headers"] == {"Content-Type": "audio/mp4"}
    assert storage.signed == [(f"recordings/default/rec-1/{SESSION}/000003.m4a", "audio/mp4", 900)]


def test_upload_url_rejects_bad_input(client: TestClient) -> None:
    token = recording_token("rec-1")
    bad = [
        {"session_id": "../../etc", "seq": 0, "mime_type": "audio/webm"},
        {"session_id": SESSION, "seq": -1, "mime_type": "audio/webm"},
        {"session_id": SESSION, "seq": 10000, "mime_type": "audio/webm"},
        {"session_id": SESSION, "seq": 0, "mime_type": "video/mp4"},
    ]
    for body in bad:
        res = client.post("/api/recordings/rec-1/upload-url", json=body, headers=auth(token))
        assert res.status_code in (400, 422), body


def test_events_are_logged_without_content(client: TestClient) -> None:
    token = recording_token("rec-1")
    events = [
        {"type": "interrupted", "at": 1, "session_id": SESSION, "detail": {"reason": "mic_ended", "ms": 12}}
    ]
    res = client.post("/api/recordings/rec-1/events", json={"events": events}, headers=auth(token))
    assert res.status_code == 204
    res = client.post(
        "/api/recordings/rec-1/events",
        json={"events": [{"type": "Bad Type", "at": 1}]},
        headers=auth(token),
    )
    assert res.status_code == 422


def _put(storage: FakeStorage, record: str, session: str, seqs: list[int], ext: str = "webm") -> None:
    for seq in seqs:
        storage.objects[f"recordings/default/{record}/{session}/{seq:06d}.{ext}"] = b"x" * 10


def test_complete_enqueues_processing_once(
    client: TestClient, storage: FakeStorage, tasks: FakeTasks
) -> None:
    _put(storage, "rec-1", SESSION, [0, 1, 2])
    token = recording_token("rec-1")
    body = {"sessions": [{"session_id": SESSION, "chunks": 3}], "duration_ms": 180000}
    res = client.post("/api/recordings/rec-1/complete", json=body, headers=auth(token))
    assert res.status_code == 200
    assert res.json() == {"status": "queued", "chunks": 3, "missing": 0}
    assert tasks.enqueued[0]["path"] == "/internal/process"
    assert tasks.enqueued[0]["payload"] == {
        "client_id": "default",
        "source": "web_recording",
        "record_id": "rec-1",
    }
    # 二度押ししても二重に処理しない
    client.post("/api/recordings/rec-1/complete", json=body, headers=auth(token))
    assert len(tasks.enqueued) == 1


def test_complete_reports_missing_chunks(client: TestClient, storage: FakeStorage, tasks: FakeTasks) -> None:
    _put(storage, "rec-1", SESSION, [0, 2])
    token = recording_token("rec-1")
    body = {"sessions": [{"session_id": SESSION, "chunks": 3}]}
    res = client.post("/api/recordings/rec-1/complete", json=body, headers=auth(token))
    assert res.status_code == 409
    assert res.json()["detail"]["missing"] == [f"{SESSION}/1"]
    assert tasks.enqueued == []
    res = client.post(
        "/api/recordings/rec-1/complete", json={**body, "allow_missing": True}, headers=auth(token)
    )
    assert res.status_code == 200
    assert res.json()["missing"] == 1


def test_complete_with_no_audio(client: TestClient) -> None:
    token = recording_token("rec-1")
    body = {"sessions": [{"session_id": SESSION, "chunks": 1}], "allow_missing": True}
    assert client.post("/api/recordings/rec-1/complete", json=body, headers=auth(token)).status_code == 409


def test_test_recording_does_not_start_processing(
    client: TestClient, storage: FakeStorage, tasks: FakeTasks
) -> None:
    _put(storage, "test-1", SESSION, [0])
    token = recording_token("test-1", test=True)
    body = {"sessions": [{"session_id": SESSION, "chunks": 1}]}
    res = client.post("/api/recordings/test-1/complete", json=body, headers=auth(token))
    assert res.json()["status"] == "test_completed"
    assert tasks.enqueued == []


def test_recorder_page_is_served_with_security_headers(client: TestClient) -> None:
    res = client.get("/recorder/")
    assert res.status_code == 200
    assert "商談の録音" in res.text
    assert "connect-src 'self' https://storage.googleapis.com" in res.headers["Content-Security-Policy"]
    assert res.headers["Referrer-Policy"] == "no-referrer"
    assert client.get("/recorder/app.js").status_code == 200


def test_issue_url_writes_recording_url_to_crm(client: TestClient, crm: FakeCrm) -> None:
    res = client.post(
        "/api/recordings",
        json={"record_id": "5001", "start_at": "2026-10-01T10:00:00+09:00"},
        headers={"X-API-Key": API_KEY},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["crm_updated"] is True
    assert crm.writes_to("5001") == [{F.recording_url: body["recording_url"]}]
    # 有効期限は商談開始から24時間
    assert body["expires_at"].startswith("2026-10-02T01:00:00")


def test_health(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok", "dry_run": False}
