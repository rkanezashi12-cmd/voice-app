"""録音アプリから動画を上げて解析する API（/api/app/videos/*。docs/video-analysis.md）。"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.clients import ClientRegistry
from app.deps import APP_REQUEST_HEADER, APP_REQUEST_VALUE
from app.videos.analysis import ANALYZE_PATH
from app.web_session import SESSION_COOKIE
from tests.conftest import FM, FakeCrm, FakeStorage, FakeTasks, app_session_cookie, base_client_config

H = {APP_REQUEST_HEADER: APP_REQUEST_VALUE}
MR = FM.meeting_record


@pytest.fixture
def registry(request: pytest.FixtureRequest) -> ClientRegistry:
    config = base_client_config()
    config["clients"]["default"]["features"] = getattr(request, "param", {"video_analysis": True})
    return ClientRegistry.from_dict(config)


@pytest.fixture
def logged_in(client: TestClient) -> TestClient:
    client.cookies.set(SESSION_COOKIE, app_session_cookie())
    return client


def test_needs_login_and_app_header(client: TestClient, logged_in: TestClient) -> None:
    body = {"filename": "a.mp4", "size": 10}
    assert (
        logged_in.post("/api/app/videos/upload-url", json=body).status_code == 403
    )  # 録音アプリのヘッダーが無い
    logged_in.cookies.clear()
    assert client.post("/api/app/videos/upload-url", json=body, headers=H).status_code == 401


def test_me_tells_whether_videos_are_available(logged_in: TestClient) -> None:
    assert logged_in.get("/api/app/me").json()["video_analysis"] is True


def test_upload_url_signs_a_put_into_the_client_folder(
    logged_in: TestClient, storage: FakeStorage, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        res = logged_in.post(
            "/api/app/videos/upload-url",
            json={"filename": "C:\\動画\\01_受注入力.MP4", "size": 600_000_000},
            headers=H,
        )
    assert res.status_code == 200
    body = res.json()
    assert body["object_name"] == "videos/default/01_受注入力.mp4"
    assert body["content_type"] == "video/mp4"
    assert body["upload_url"].startswith("https://storage.googleapis.com/")
    assert storage.signed == [("videos/default/01_受注入力.mp4", "video/mp4", 6 * 3600)]
    # ファイル名・署名付き URL はログに出さない
    logged = caplog.text + "".join(str(getattr(r, "fields", "")) for r in caplog.records)
    assert "video.upload_url" in logged and "受注入力" not in logged and "Signature" not in logged


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"filename": "memo.txt", "size": 10}, 422),
        ({"filename": ".mp4", "size": 10}, 422),
        ({"filename": "a.mp4", "size": 0}, 422),
        ({"filename": "a.mp4", "size": 6 * 1024**3}, 422),
    ],
)
def test_upload_url_rejects_bad_files(
    logged_in: TestClient, storage: FakeStorage, body: dict, status: int
) -> None:
    assert logged_in.post("/api/app/videos/upload-url", json=body, headers=H).status_code == status
    assert storage.signed == []


@pytest.mark.parametrize("registry", [{}], indirect=True)
def test_feature_must_be_on(logged_in: TestClient, storage: FakeStorage) -> None:
    res = logged_in.post("/api/app/videos/upload-url", json={"filename": "a.mp4", "size": 10}, headers=H)
    assert res.status_code == 403
    assert logged_in.get("/api/app/videos").status_code == 403
    assert logged_in.get("/api/app/me").json()["video_analysis"] is False


def test_analyze_enqueues_uploaded_video_once(
    logged_in: TestClient, storage: FakeStorage, tasks: FakeTasks
) -> None:
    storage.objects["videos/default/01_受注入力.mp4"] = b"video"
    body = {"object_name": "videos/default/01_受注入力.mp4"}
    res = logged_in.post("/api/app/videos/analyze", json=body, headers=H)
    assert res.json() == {"status": "queued"}
    [task] = tasks.enqueued
    assert task["path"] == ANALYZE_PATH
    assert task["payload"] == {
        "client_id": "default",
        "object_name": "videos/default/01_受注入力.mp4",
        "force": False,
    }
    # 続けて押しても二重に積まない
    assert logged_in.post("/api/app/videos/analyze", json=body, headers=H).json() == {
        "status": "already_queued"
    }
    # やり直し（force）は別に積める
    res = logged_in.post("/api/app/videos/analyze", json={**body, "force": True}, headers=H)
    assert res.json() == {"status": "queued"} and tasks.enqueued[-1]["payload"]["force"] is True


@pytest.mark.parametrize(
    "name",
    [
        "videos/other/a.mp4",
        "videos/default/../other/a.mp4",
        "recordings/default/1/x/000000.webm",
        "videos/default/a.txt",
    ],
)
def test_analyze_only_accepts_own_folder(
    logged_in: TestClient, storage: FakeStorage, tasks: FakeTasks, name: str
) -> None:
    storage.objects[name] = b"x"
    res = logged_in.post("/api/app/videos/analyze", json={"object_name": name}, headers=H)
    assert res.status_code == 422 and tasks.enqueued == []


def test_analyze_needs_the_upload_to_be_finished(logged_in: TestClient, tasks: FakeTasks) -> None:
    res = logged_in.post("/api/app/videos/analyze", json={"object_name": "videos/default/a.mp4"}, headers=H)
    assert res.status_code == 404 and tasks.enqueued == []


def test_list_shows_video_records(logged_in: TestClient, crm: FakeCrm) -> None:
    def handler(query: str) -> list[dict[str, Any]]:
        assert f"where {MR.category} = '動画解析'" in query
        return [
            {
                "id": "1",
                MR.name: "【TEST】動画解析 01_受注入力",
                MR.status: "完了",
                "Created_Time": "2026-11-03T20:30:00+09:00",
            },
            {
                "id": "2",
                MR.name: "動画解析 02_出荷",
                MR.status: "失敗",
                MR.error_message: "動画が見つかりません",
            },
        ]

    crm.coql_handler = handler
    body = logged_in.get("/api/app/videos").json()
    assert [v["name"] for v in body["videos"]] == ["01_受注入力", "02_出荷"]
    assert body["videos"][1]["error"] == "動画が見つかりません"
    assert (body["done"], body["failed"]) == ("完了", "失敗")


def test_app_pages_allow_direct_upload_to_gcs(client: TestClient) -> None:
    res = client.get("/app/videos.html")
    assert res.status_code == 200
    assert "connect-src 'self' https://storage.googleapis.com" in res.headers["content-security-policy"]
