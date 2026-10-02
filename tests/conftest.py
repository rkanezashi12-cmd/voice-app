"""テスト共通の偽物。

外部サービス（Zoho CRM・Recall.ai・Gemini・GCS・Cloud Tasks・Secret Manager）はすべてここの偽物か respx で
置き換える。Runtime の HTTP クライアントは NoNetwork で、実際の通信（本番 CRM など）は必ず失敗させる。
偽物への「書き込み」は記録されるだけで、どこにも送られない。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.clients import ClientConfig, ClientRegistry
from app.config import Settings
from app.deps import tasks_oidc
from app.errors import AppError, ExternalServiceError
from app.field_map import FieldMap
from app.main import create_app
from app.recording_token import issue
from app.runtime import Runtime
from app.services.crm import STANDARD_MODULES, CrmWriteForbidden
from app.services.gemini import LlmResult
from app.services.geocoding import Place
from app.services.storage import StoredObject
from app.services.zoho_login import ZohoLoginError, ZohoUser
from app.web_session import AppSession, issue_session

API_KEY = "test-api-key-0123456789"
TOKEN_SECRET = "test-recording-secret"
SERVICE_URL = "https://meeting-notes.example.run.app"
TASKS_SA = "tasks-invoker@proj.iam.gserviceaccount.com"
WEBHOOK_SECRET = "whsec_dGVzdC13ZWJob29rLXNlY3JldC0xMjM0NTY="
SESSION_SECRET = "test-session-secret"
ORG_ID = "4928352000000020005"
APP_USER_ID = "9001"
FM = FieldMap()
F = FM.meeting_record
S = FM.status


class NoNetwork(httpx.AsyncBaseTransport):
    """テストから実際の外部 API（本番 CRM など）に接続させないための通信層。"""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"テストから実ネットワークに接続しようとしました: {request.url.host}")


# ---- GCS / Cloud Tasks ----


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


# ---- Zoho CRM ----


class FakeCrm:
    """CrmService の偽物。書き込み先のガードは本物と同じ規則で確認する。"""

    def __init__(self, fm: FieldMap = FM) -> None:
        self.fm = fm
        self.records: dict[str, dict[str, dict[str, Any]]] = {}
        self.writes: list[tuple[str, str, str, dict[str, Any]]] = []
        self.coql_queries: list[str] = []
        self.coql_rows: list[dict[str, Any]] = []
        self.coql_handler: Callable[[str], list[dict[str, Any]]] | None = None
        self.dry_run = False
        self.fail_writes: AppError | None = None
        # 作成を断る条件（送る中身を見て、断るときはエラーを返す）
        self.reject_create: Callable[[dict[str, Any]], AppError | None] | None = None
        self.writable_modules = frozenset({fm.meeting_record.module, fm.glossary.module})
        # 設定の読み取り（モジュール・項目）と CRM のユーザー
        self.modules: list[dict[str, Any]] = []
        self.settings_fields: dict[str, list[dict[str, Any]]] = {}
        self.settings_reads: list[str] = []
        self.users: dict[str, dict[str, Any]] = {}
        self._seq = 0

    def check_writable(self, module: str) -> None:
        if module not in self.writable_modules or module in STANDARD_MODULES:
            raise CrmWriteForbidden(f"CRM のモジュール {module} への書き込みは許可されていません")

    def add(self, module: str, record_id: str, data: dict[str, Any]) -> None:
        self.records.setdefault(module, {})[record_id] = {"id": record_id, **data}

    def record(self, record_id: str, module: str | None = None) -> dict[str, Any]:
        return self.records[module or self.fm.meeting_record.module][record_id]

    def writes_to(self, record_id: str) -> list[dict[str, Any]]:
        return [data for _, _, rid, data in self.writes if rid == record_id]

    async def get_record(self, module: str, record_id: str) -> dict[str, Any] | None:
        rec = self.records.get(module, {}).get(record_id)
        return dict(rec) if rec else None

    async def list_records(self, module: str, fields: list[str], **_: Any) -> list[dict[str, Any]]:
        return [dict(r) for r in self.records.get(module, {}).values()]

    async def list_modules(self) -> list[dict[str, Any]]:
        self.settings_reads.append("modules")
        return [dict(m) for m in self.modules]

    async def list_fields(self, module: str) -> list[dict[str, Any]]:
        self.settings_reads.append(f"fields:{module}")
        return [dict(f) for f in self.settings_fields.get(module, [])]

    async def get_user(self, user_id: str) -> dict[str, Any] | None:
        user = self.users.get(user_id)
        return dict(user) if user else None

    async def coql(self, query: str) -> list[dict[str, Any]]:
        self.coql_queries.append(query)
        if self.coql_handler is not None:
            return self.coql_handler(query)
        f = self.fm.meeting_record
        if f"from {f.module} " in query and f"where {f.recall_id} = '" in query:
            recall_id = query.split(f"where {f.recall_id} = '", 1)[1].split("'", 1)[0]
            rows = [r for r in self.records.get(f.module, {}).values() if r.get(f.recall_id) == recall_id]
            return [{"id": r["id"], f.status: r.get(f.status), f.account: r.get(f.account)} for r in rows[:1]]
        return list(self.coql_rows)

    async def update_record(self, module: str, record_id: str, data: dict[str, Any]) -> None:
        self.check_writable(module)
        if self.fail_writes:
            raise self.fail_writes
        self.writes.append(("update", module, record_id, dict(data)))
        self.records.setdefault(module, {}).setdefault(record_id, {"id": record_id}).update(data)

    async def create_record(self, module: str, data: dict[str, Any]) -> str:
        self.check_writable(module)
        rejected = self.reject_create(data) if self.reject_create else None
        if rejected:
            raise rejected
        self._seq += 1
        record_id = f"new-{self._seq}"
        self.writes.append(("create", module, record_id, dict(data)))
        self.add(module, record_id, data)
        return record_id

    async def upsert_record(
        self, module: str, data: dict[str, Any], duplicate_check_fields: list[str]
    ) -> tuple[str, str]:
        self.check_writable(module)
        key = duplicate_check_fields[0]
        for rid, rec in self.records.get(module, {}).items():
            if rec.get(key) == data.get(key):
                self.writes.append(("upsert", module, rid, dict(data)))
                rec.update(data)
                return rid, "update"
        self._seq += 1
        record_id = f"new-{self._seq}"
        self.writes.append(("upsert", module, record_id, dict(data)))
        self.add(module, record_id, data)
        return record_id, "insert"


# ---- Recall.ai ----


class FakeRecall:
    def __init__(self) -> None:
        self.bots: dict[str, dict[str, Any]] = {}
        self.recordings: dict[str, dict[str, Any]] = {}
        self.uploads: dict[str, dict[str, Any]] = {}
        self.downloads: dict[str, Any] = {}
        self.created_bots: list[dict[str, Any]] = []
        self.transcript_requests: list[tuple[str, dict[str, Any]]] = []
        self.deleted_bot_media: list[str] = []
        self.deleted_recordings: list[str] = []
        self.fail_create: AppError | None = None
        self.fail_delete: AppError | None = None
        self.dry_run = False

    def add_bot(
        self, bot_id: str, record_id: str, *, recording_id: str | None = "rec-1", transcript: Any = None
    ) -> None:
        recordings = [{"id": recording_id, "started_at": "2026-09-28T01:00:00Z"}] if recording_id else []
        self.bots[bot_id] = {"id": bot_id, "metadata": {"record_id": record_id}, "recordings": recordings}
        if recording_id:
            self.add_recording(recording_id, transcript)

    def add_recording(self, recording_id: str, transcript: Any = None, status: str = "done") -> None:
        shortcuts: dict[str, Any] = {}
        if transcript is not None:
            url = f"https://recall-download.example/{recording_id}.json"
            shortcuts["transcript"] = {"status": {"code": status}, "data": {"download_url": url}}
            self.downloads[url] = transcript
        self.recordings[recording_id] = {"id": recording_id, "media_shortcuts": shortcuts}

    async def create_bot(self, **kwargs: Any) -> dict[str, Any]:
        if self.fail_create:
            raise self.fail_create
        self.created_bots.append(kwargs)
        return {"id": f"bot-{len(self.created_bots)}"}

    async def get_bot(self, bot_id: str) -> dict[str, Any]:
        return self.bots[bot_id]

    async def get_recording(self, recording_id: str) -> dict[str, Any]:
        return self.recordings[recording_id]

    async def create_transcript(self, recording_id: str, request: dict[str, Any]) -> dict[str, Any]:
        self.transcript_requests.append((recording_id, request))
        return {"id": "tr-1"}

    async def download_json(self, url: str) -> Any:
        return self.downloads[url]

    async def delete_bot_media(self, bot_id: str) -> None:
        if self.fail_delete:
            raise self.fail_delete
        self.deleted_bot_media.append(bot_id)

    async def delete_recording(self, recording_id: str) -> None:
        if self.fail_delete:
            raise self.fail_delete
        self.deleted_recordings.append(recording_id)

    async def create_sdk_upload(self, *, recording_config: Any, metadata: dict[str, str]) -> dict[str, Any]:
        upload_id = f"upload-{len(self.uploads) + 1}"
        self.uploads[upload_id] = {
            "id": upload_id,
            "metadata": metadata,
            "recording_config": recording_config,
        }
        return {"id": upload_id, "upload_token": "sdk-upload-token"}

    async def get_sdk_upload(self, upload_id: str) -> dict[str, Any]:
        if upload_id not in self.uploads:
            raise ExternalServiceError("recall", "デスクトップ録音の情報取得に失敗しました", status=404)
        return self.uploads[upload_id]


# ---- Gemini ----


def summary_json(**overrides: Any) -> str:
    data: dict[str, Any] = {
        "summary": "新型治具の見積を依頼された。",
        "issues": ["段取り替えに時間がかかる"],
        "needs": ["納期3週間"],
        "next_actions": [
            {"action": "見積書を送る", "owner": "当社", "due": "2026-10-03"},
            {"action": "図面を共有", "owner": "先方", "due": "2026-10-01"},
        ],
        "category": None,
        "competitors": [],
        "budget": "300万円程度",
        "decision_maker": "工場長",
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


class FakeLlm:
    """task ごとに決まった応答を返す。補正は入力をそのまま返す（replace で置換も可）。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.transcribe_outputs: list[str] = [
            "話者A: 本日はよろしくお願いします。\n話者B: よろしくお願いします。"
        ]
        self.summary_output = summary_json()
        self.replace: dict[str, str] = {}
        self.fail: AppError | None = None

    async def generate(
        self,
        *,
        task: str,
        model: str,
        system_instruction: str,
        parts: list[Any],
        json_schema: dict[str, Any] | None = None,
    ) -> LlmResult:
        self.calls.append({"task": task, "model": model, "parts": parts, "json_schema": json_schema})
        if self.fail:
            raise self.fail
        if task == "transcribe":
            index = sum(1 for c in self.calls if c["task"] == "transcribe") - 1
            return LlmResult(self.transcribe_outputs[min(index, len(self.transcribe_outputs) - 1)])
        if task == "correct":
            text = parts[-1].text
            for a, b in self.replace.items():
                text = text.replace(a, b)
            return LlmResult(text)
        return LlmResult(self.summary_output)


# ---- クライアント単位のサービス ----


class FakeLogin:
    """ZohoLogin の偽物。exchange / current_user の結果をテストごとに変える。"""

    def __init__(self) -> None:
        self.user = ZohoUser(
            user_id=APP_USER_ID, name="金指 営業", email="sales@example.com", status="active", org_id=ORG_ID
        )
        self.exchange_error: ZohoLoginError | None = None
        self.codes: list[str] = []

    def authorize_url(self, state: str) -> str:
        return f"https://accounts.zoho.com/oauth/v2/auth?state={state}"

    async def exchange(self, code: str) -> str:
        self.codes.append(code)
        if self.exchange_error:
            raise self.exchange_error
        return "user-access-token"

    async def current_user(self, access_token: str) -> ZohoUser:
        assert access_token == "user-access-token"
        return self.user


class FakeGeocoder:
    def __init__(self) -> None:
        self.place = Place(prefecture="神奈川県", city="横浜市", ward="中区", town="山下町")
        self.calls: list[tuple[float, float]] = []

    async def reverse(self, lat: float, lng: float) -> Place:
        self.calls.append((lat, lng))
        return self.place


class FakeClientServices:
    def __init__(
        self,
        config: ClientConfig,
        crm: FakeCrm,
        recall: FakeRecall,
        *,
        login: FakeLogin | None = None,
        geocoder: FakeGeocoder | None = None,
    ) -> None:
        self.config = config
        self.field_map = config.field_map
        self._crm = crm
        self._recall = recall
        self.login = login or FakeLogin()
        self.geo = geocoder
        # 先方担当者（連絡先）に書くときの中間モジュールのルックアップ項目（None は項目がまだ無い）
        self.contacts_link: str | None = None
        # CRM のユーザーを確かめ直した結果（例外を入れると Zoho に届かない）
        self.user_active: bool | AppError = True
        self.user_checks: list[str] = []

    async def session_secret(self) -> bytes:
        return SESSION_SECRET.encode()

    async def zoho_login(self) -> FakeLogin:
        return self.login

    async def geocoder(self) -> FakeGeocoder | None:
        return self.geo

    async def org_id(self) -> str:
        return ORG_ID

    async def contacts_link_field(self) -> str | None:
        return self.contacts_link

    async def crm_user_active(self, user_id: str) -> bool:
        self.user_checks.append(user_id)
        if isinstance(self.user_active, AppError):
            raise self.user_active
        return self.user_active

    @property
    def client_id(self) -> str:
        return self.config.client_id

    async def crm(self) -> FakeCrm:
        return self._crm

    async def recall(self) -> FakeRecall:
        return self._recall


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
                "app": {
                    "login_client_id": "env:TEST_LOGIN_CLIENT_ID",
                    "login_client_secret": "env:TEST_LOGIN_CLIENT_SECRET",
                    "session_secret": "env:TEST_SESSION_SECRET",
                    "maps_api_key": "env:TEST_MAPS_API_KEY",
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
    monkeypatch.setenv("TEST_RECALL_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("TEST_LOGIN_CLIENT_ID", "login-client-id")
    monkeypatch.setenv("TEST_LOGIN_CLIENT_SECRET", "login-client-secret")
    monkeypatch.setenv("TEST_SESSION_SECRET", SESSION_SECRET)
    monkeypatch.setenv("TEST_MAPS_API_KEY", "maps-api-key")


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def tasks() -> FakeTasks:
    return FakeTasks()


@pytest.fixture
def crm() -> FakeCrm:
    return FakeCrm()


@pytest.fixture
def recall() -> FakeRecall:
    return FakeRecall()


@pytest.fixture
def llm() -> FakeLlm:
    return FakeLlm()


@pytest.fixture
def zoho_login() -> FakeLogin:
    return FakeLogin()


@pytest.fixture
def geocoder() -> FakeGeocoder:
    return FakeGeocoder()


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def registry() -> ClientRegistry:
    return ClientRegistry.from_dict(base_client_config())


@pytest.fixture
def runtime(
    settings: Settings,
    registry: ClientRegistry,
    storage: FakeStorage,
    tasks: FakeTasks,
    crm: FakeCrm,
    recall: FakeRecall,
    llm: FakeLlm,
    zoho_login: FakeLogin,
    geocoder: FakeGeocoder,
) -> Runtime:
    services = FakeClientServices(registry.get("default"), crm, recall, login=zoho_login, geocoder=geocoder)
    return Runtime(
        settings,
        registry,
        storage=storage,
        tasks=tasks,
        llm=llm,
        http=httpx.AsyncClient(transport=NoNetwork()),
        client_services={"default": services},
    )


@pytest.fixture
def client(runtime: Runtime) -> Iterator[TestClient]:
    app = create_app(runtime)
    # /internal/* の OIDC 検証は test_internal_auth.py で個別に確かめる
    app.dependency_overrides[tasks_oidc] = lambda: None
    with TestClient(app) as c:
        yield c


def recording_token(record_id: str = "1234567890", *, test: bool = False, ttl: int = 3600) -> str:
    return issue(
        TOKEN_SECRET.encode(),
        client_id="default",
        record_id=record_id,
        expires_at=int(time.time()) + ttl,
        test=test,
    )


def app_session_cookie(
    user_id: str = APP_USER_ID, *, ttl: int = 3600, client_id: str = "default", checked_ago: int = 0
) -> str:
    """録音アプリにログインした状態の Cookie の値（checked_ago 秒前に CRM の有効なユーザーと確かめた）。"""
    now = int(time.time())
    return issue_session(
        SESSION_SECRET.encode(),
        AppSession(
            client_id=client_id,
            user_id=user_id,
            name="金指 営業",
            email="sales@example.com",
            expires_at=now + ttl,
            checked_at=now - checked_ago,
        ),
    )
