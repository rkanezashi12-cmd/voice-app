"""本物の CrmService・RecallService を respx で動かし、次を確かめる。

- DRY_RUN=true では CRM への書き込みと Recall.ai の削除を1件も送らない
- ログに文字起こし本文・要約・トークン・API キー・署名付き URL が出ない
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator

import httpx
import pytest
import respx

from app.clients import ClientRegistry
from app.logs import JsonFormatter, setup_logging
from app.pipeline.process import ProcessRequest, run_process
from app.runtime import Runtime
from tests.conftest import FakeLlm, FakeStorage, FakeTasks, base_client_config, make_settings

ZOHO = "https://www.zohoapis.com/crm/v8"
RECALL = "https://ap-northeast-1.recall.ai/api/v1"
DOWNLOAD = "https://recall-dl.example/t.json?X-Amz-Signature=SIGNED-URL-SECRET"
SECRET_WORDS = [
    "機密マーカー",  # 文字起こし本文
    "新型治具の見積",  # 要約
    "ACCESS-TOKEN-SECRET",  # Zoho のアクセストークン
    "zoho-refresh-token",
    "zoho-client-secret",
    "recall-api-key",
    "SIGNED-URL-SECRET",
]


@pytest.fixture
def captured_logs() -> Iterator[io.StringIO]:
    setup_logging("INFO")  # 本番と同じ設定（httpx の URL ログを抑える）
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    yield stream
    root.removeHandler(handler)


async def test_dry_run_pipeline_writes_nothing_and_logs_no_content(
    respx_mock: respx.MockRouter, captured_logs: io.StringIO
) -> None:
    respx_mock.post("https://accounts.zoho.com/oauth/v2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "ACCESS-TOKEN-SECRET", "expires_in": 3600})
    )
    record = {
        "id": "5001",
        "Account": {"id": "a1", "name": "カスタマー株式会社"},
        "Status": "予約済",
        "Start_At": "2026-09-28T10:00:00+09:00",
    }
    respx_mock.get(f"{ZOHO}/MeetingRecords/5001").mock(
        return_value=httpx.Response(200, json={"data": [record]})
    )
    respx_mock.get(url__startswith=f"{ZOHO}/Glossary").mock(return_value=httpx.Response(204))
    respx_mock.get(f"{RECALL}/bot/bot-1/").mock(
        return_value=httpx.Response(
            200, json={"id": "bot-1", "metadata": {"record_id": "5001"}, "recordings": [{"id": "rec-1"}]}
        )
    )
    respx_mock.get(f"{RECALL}/recording/rec-1/").mock(
        return_value=httpx.Response(
            200,
            json={
                "media_shortcuts": {
                    "transcript": {"status": {"code": "done"}, "data": {"download_url": DOWNLOAD}}
                }
            },
        )
    )
    respx_mock.get(url__startswith="https://recall-dl.example/t.json").mock(
        return_value=httpx.Response(
            200,
            json=[{"participant": {"name": "山田"}, "words": [{"text": "機密マーカーについて話します"}]}],
        )
    )

    async with httpx.AsyncClient() as http:
        runtime = Runtime(
            make_settings(dry_run=True),
            ClientRegistry.from_dict(base_client_config()),
            http=http,
            llm=FakeLlm(),
            tasks=FakeTasks(),
            storage=FakeStorage(),
        )
        outcome = await run_process(
            runtime,
            ProcessRequest(client_id="default", source="recall_bot", bot_id="bot-1"),
            final_attempt=False,
        )

    assert outcome.status == "done"
    sent = [(c.request.method, c.request.url.path) for c in respx_mock.calls]
    assert not [s for s in sent if s[0] in ("PUT", "PATCH", "DELETE")], sent
    assert not [s for s in sent if s[1].endswith("/upsert") or "delete_media" in s[1]], sent
    assert [s for s in sent if s[0] == "POST"] == [("POST", "/oauth/v2/token")]

    logs = captured_logs.getvalue()
    assert "dry_run.skip" in logs
    assert "recall.delete_bot_media" in logs
    for word in SECRET_WORDS:
        assert word not in logs, f"ログに {word} が出ている"
