"""動画の解析の結果のテキストと Word（app/videos/report.py）、Word のダウンロード（/api/app/videos/{id}/report.docx）。"""

from __future__ import annotations

import io
import json
import logging

import pytest
from docx import Document
from fastapi.testclient import TestClient

from app.clients import ClientRegistry
from app.field_map import FieldMap
from app.runtime import Runtime
from app.services.gemini import LlmResult
from app.videos import analysis as va
from app.videos.analysis import VideoTask, run_video_analysis
from app.videos.report import build_docx, report_text
from app.web_session import SESSION_COOKIE
from tests.conftest import FakeCrm, FakeLlm, FakeStorage, app_session_cookie, base_client_config

FM = FieldMap()
MR = FM.meeting_record
REPORT = {
    "summary": "受注から出荷指示までを説明している。",
    "business_flow": ["営業が受注入力で受注を登録する", "生産管理が工程を割り当てる"],
    "screens": ["受注入力：受注を登録する"],
    "functions": ["品番で検索する"],
    "screen_details": [
        {
            "name": "受注入力",
            "purpose": "受注を1件ずつ登録する",
            "items": [
                {"name": "受注番号", "kind": "表示", "note": "自動で採番"},
                {"name": "納期", "kind": "入力", "note": "必須。a|b"},
            ],
            "operations": ["品番を入れて検索する", "登録を押す"],
            "rules": ["納期が過ぎた行は赤"],
            "outputs": ["受注一覧（Excel）"],
        }
    ],
    "data": ["品目マスタ：品番・品名"],
    "integrations": ["受注を Excel に書き出して工場に送る"],
    "issues": ["納期を Excel で別に管理している"],
    "requirements": ["納期を一覧で見られること"],
    "questions": ["締め日はいつか"],
}
SCENES = "## 0:00〜1:30 受注入力\n- 項目：受注番号・**納期**\n  - 納期は必須"


def docx_text(data: bytes) -> tuple[list[tuple[str, str]], list[list[list[str]]]]:
    doc = Document(io.BytesIO(data))
    paragraphs = [(p.style.name, p.text) for p in doc.paragraphs if p.text]
    tables = [[[c.text for c in row.cells] for row in t.rows] for t in doc.tables]
    return paragraphs, tables


def test_report_text_and_detailed_docx() -> None:
    text = report_text("01_受注入力", "長さ：25:00　解析日：2026-11-03", REPORT, SCENES)
    assert "■ 業務の流れ\n・1. 営業が受注入力で受注を登録する" in text
    assert "| 納期 | 入力 | 必須。a／b |" in text, "表を壊す | は置き換える"
    paragraphs, tables = docx_text(build_docx(text))
    assert paragraphs[0] == ("Title", "01_受注入力（詳細）")
    headings = [t for s, t in paragraphs if s == "Heading 1"]
    assert headings == [
        "要約", "業務の流れ", "出てきた画面", "機能・使い方", "画面ごとの詳細", "扱うデータ",
        "ほかのシステム・Excel・紙とのやりとり", "今の課題", "要件の候補", "確かめること", "場面ごとの記録",
    ]  # fmt: skip
    assert ("Heading 2", "受注入力") in paragraphs and ("Heading 3", "項目") in paragraphs
    assert tables == [
        [["項目", "種類", "説明"], ["受注番号", "表示", "自動で採番"], ["納期", "入力", "必須。a／b"]]
    ]
    assert ("List Bullet", "納期が過ぎた行は赤") in paragraphs
    assert ("List Bullet", "項目：受注番号・納期") in paragraphs, "**太字** の記号は外す"
    assert ("List Bullet 2", "納期は必須") in paragraphs


def test_summary_docx_has_only_summary_sections() -> None:
    text = report_text("01_受注入力", "長さ：25:00", REPORT, SCENES)
    paragraphs, tables = docx_text(build_docx(text, summary_only=True))
    assert paragraphs[0] == ("Title", "01_受注入力（要約）")
    headings = [t for s, t in paragraphs if s == "Heading 1"]
    assert headings == ["要約", "業務の流れ", "出てきた画面", "今の課題", "要件の候補", "確かめること"]
    assert tables == [] and not any("受注番号" in t for _, t in paragraphs)


# ---- 解析：詳細のまとめが崩れたら、詳細を省いた形でまとめ直す ----


@pytest.fixture
def registry() -> ClientRegistry:
    config = base_client_config()
    config["clients"]["default"]["features"] = {"video_analysis": True, "transcript_attachment": True}
    return ClientRegistry.from_dict(config)


async def test_analysis_falls_back_to_simple_summary(
    runtime: Runtime, storage: FakeStorage, crm: FakeCrm, llm: FakeLlm, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def probe(url: str) -> float | None:
        return 300.0

    monkeypatch.setattr(va, "probe_duration_url", probe)
    crm.coql_handler = lambda q: []
    storage.objects["videos/default/a.mp4"] = b"v"
    outputs = [LlmResult('{"summary": "途中', truncated=True), LlmResult(llm.video_summary)]
    original = llm.generate

    async def generate(**kwargs: object) -> LlmResult:
        if kwargs["task"] == "video_summarize":
            llm.calls.append({"task": "video_summarize", "json_schema": kwargs["json_schema"]})
            return outputs.pop(0)
        return await original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(llm, "generate", generate)
    task = VideoTask(client_id="default", object_name="videos/default/a.mp4")
    assert await run_video_analysis(runtime, task, final_attempt=True) == "done"
    schemas = [c["json_schema"] for c in llm.calls if c["task"] == "video_summarize"]
    assert "screen_details" in schemas[0]["properties"] and "screen_details" not in schemas[1]["properties"]
    assert crm.writes[0][3][MR.status] == "完了"


# ---- ダウンロード ----


@pytest.fixture
def logged_in(client: TestClient) -> TestClient:
    client.cookies.set(SESSION_COOKIE, app_session_cookie())
    return client


def test_download_builds_docx_from_attachment(
    logged_in: TestClient, crm: FakeCrm, caplog: pytest.LogCaptureFixture
) -> None:
    crm.add(
        MR.module,
        "7001",
        {MR.name: "【TEST】動画解析 01_受注入力", MR.category: "動画解析", MR.status: "完了"},
    )
    text = report_text("01_受注入力", "長さ：25:00", REPORT, SCENES)
    crm.attachments.append((MR.module, "7001", "動画解析_01_受注入力.txt", ("﻿" + text).encode()))
    with caplog.at_level(logging.INFO):
        res = logged_in.get("/api/app/videos/7001/report.docx?kind=detail")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert "filename*=UTF-8''%E5%8B%95%E7%94%BB%E8%A7%A3%E6%9E%90_01_" in res.headers["content-disposition"]
    paragraphs, tables = docx_text(res.content)
    assert paragraphs[0] == ("Title", "01_受注入力（詳細）") and tables
    logged = caplog.text + "".join(str(getattr(r, "fields", "")) for r in caplog.records)
    assert "video.report_downloaded" in logged and "受注入力" not in logged
    res = logged_in.get("/api/app/videos/7001/report.docx?kind=summary")
    assert docx_text(res.content)[0][0] == ("Title", "01_受注入力（要約）")


def test_download_without_attachment_uses_record_fields(logged_in: TestClient, crm: FakeCrm) -> None:
    crm.add(
        MR.module,
        "7002",
        {
            MR.name: "動画解析 02_出荷",
            MR.category: "動画解析",
            MR.summary: "出荷の説明",
            MR.needs: "・出荷を一覧で見たい",
            MR.transcript: "## 0:00〜1:00 出荷指示",
        },
    )
    res = logged_in.get("/api/app/videos/7002/report.docx")
    paragraphs, _ = docx_text(res.content)
    assert paragraphs[0] == ("Title", "02_出荷（詳細）")
    assert ("List Bullet", "出荷を一覧で見たい") in paragraphs and (
        "Heading 2",
        "0:00〜1:00 出荷指示",
    ) in paragraphs


@pytest.mark.parametrize(
    ("record_id", "kind"), [("7003", "detail"), ("9999", "detail"), ("abc", "detail"), ("7001", "other")]
)
def test_download_only_video_records(logged_in: TestClient, crm: FakeCrm, record_id: str, kind: str) -> None:
    crm.add(MR.module, "7001", {MR.name: "動画解析 a", MR.category: "動画解析"})
    crm.add(MR.module, "7003", {MR.name: "2026-11-03 訪問", MR.summary: "商談"})  # 動画解析ではない商談記録
    res = logged_in.get(f"/api/app/videos/{record_id}/report.docx?kind={kind}")
    assert res.status_code in (404, 422)


def test_download_needs_login(client: TestClient) -> None:
    assert client.get("/api/app/videos/7001/report.docx").status_code == 401


def test_detail_schema_is_valid_json() -> None:
    from app.pipeline.ai import PromptStore

    schema = PromptStore().video_schema_detail
    assert set(schema["required"]) == set(schema["properties"])
    assert json.dumps(schema)
