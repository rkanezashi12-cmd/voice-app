"""テスト共通の偽物（外部サービスはすべてここの偽物か respx で置き換える）。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.clients import ClientRegistry
from app.config import Settings
from app.main import create_app
from app.recording_token import issue
from app.runtime import Runtime
from app.services.storage import StoredObject

API_KEY = "test-api-key-0123456789"
TOKEN_SECRET = "test-recording-secret"
SERVICE_URL = "https://meeting-notes.example.run.app"
TASKS_SA = "tasks-invoker@proj.iam.gserviceaccount.com"


class NoNetwork(httpx.AsyncBaseTransport):
    """テストから実際の外部 API（本番 CRM など）に接続させないための通信層。"""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"テストから実ネットワークに接続しようとしました: {request.url.host}")


@dataclass
class FakeStorage:
    objects: dict[str, bytes] = field(default_factory=dict)
    signed: list[tuple[str, str, int]] = field(default_factory=list)
    deleted_prefixes: list[str] = field(default_factory=list)
    dry_run: bool = False

    async def upload_url(self, object_name: str, content_type: str, ttl_seconds: int) -> str:
        self.signed.append((object_name, content_type, ttl_seconds))
        return f"https://storage.googleapis.com/bucket/{object_name}?X-Goog-Signature=fake"

    async def list(self, prefix: str) -> list[StoredObject]:
        return [
            StoredObject(n, len(d), None) for n, d in sorted(self.objects.items()) if n.startswith(prefix)
        ]

    async def download(self, object_name: str, dest: Path) -> None:
        await asyncio.to_thread(dest.write_bytes, self.objects[object_name])

    async def delete_prefix(self, prefix: str) -> int:
        self.deleted_prefixes.append(prefix)
        if self.dry_run:
            return 0
        names = [n for n in self.objects if n.startswith(prefix)]
        for n in names:
            del self.objects[n]
        return len(names)


@dataclass
class FakeTasks:
    enqueued: list[dict[str, Any]] = field(default_factory=list)
    names: set[str] = field(default_factory=set)

    async def enqueue(
        self, path: str, payload: dict[str, Any], *, name: str | None = None, delay_seconds: int = 0
    ) -> bool:
        if name and name in self.names:
            return False
        if name:
            self.names.add(name)
        self.enqueued.append({"path": path, "payload": payload, "name": name, "delay_seconds": delay_seconds})
        return True


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "dry_run": False,
        "service_url": SERVICE_URL,
        "gcp_project_id": "proj",
        "gcs_bucket": "bucket",
        "recording_token_secret": TOKEN_SECRET,
        "tasks_invoker_sa": TASKS_SA,
        "tasks_queue": "meeting-process",
        "gemini_model_transcribe": "gemini-test-audio",
        "gemini_model_text": "gemini-test-text",
    }
    values.update(overrides)
    return Settings(**values)


def base_client_config() -> dict[str, Any]:
    return {
        "clients": {
            "default": {
                "api_key": "env:TEST_API_KEY",
                "own_email_domains": ["own.example.co.jp"],
                "zoho": {
                    "client_id": "env:TEST_ZOHO_CLIENT_ID",
                    "client_secret": "env:TEST_ZOHO_CLIENT_SECRET",
                    "refresh_token": "env:TEST_ZOHO_REFRESH_TOKEN",
                },
                "recall": {
                    "api_key": "env:TEST_RECALL_API_KEY",
                    "webhook_secret": "env:TEST_RECALL_WEBHOOK_SECRET",
                    "bot_name": "議事録ボット（録音中）",
                    "transcript_request": {"provider": {"recallai_async": {"language_code": "ja"}}},
                },
            }
        }
    }


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_API_KEY", API_KEY)
    monkeypatch.setenv("TEST_ZOHO_CLIENT_ID", "zoho-client-id")
    monkeypatch.setenv("TEST_ZOHO_CLIENT_SECRET", "zoho-client-secret")
    monkeypatch.setenv("TEST_ZOHO_REFRESH_TOKEN", "zoho-refresh-token")
    monkeypatch.setenv("TEST_RECALL_API_KEY", "recall-api-key")
    # Svix 形式（whsec_ + base64）
    monkeypatch.setenv("TEST_RECALL_WEBHOOK_SECRET", "whsec_dGVzdC13ZWJob29rLXNlY3JldC0xMjM0NTY=")


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def tasks() -> FakeTasks:
    return FakeTasks()


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def runtime(settings: Settings, storage: FakeStorage, tasks: FakeTasks) -> Runtime:
    return Runtime(
        settings,
        ClientRegistry.from_dict(base_client_config()),
        storage=storage,
        tasks=tasks,
        http=httpx.AsyncClient(transport=NoNetwork()),
    )


@pytest.fixture
def client(runtime: Runtime) -> Iterator[TestClient]:
    with TestClient(create_app(runtime)) as c:
        yield c


def recording_token(record_id: str = "1234567890", *, test: bool = False, ttl: int = 3600) -> str:
    return issue(
        TOKEN_SECRET.encode(),
        client_id="default",
        record_id=record_id,
        expires_at=int(time.time()) + ttl,
        test=test,
    )
