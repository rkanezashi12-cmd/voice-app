"""ニーズ分析（docs/needs-analysis.md）を偽の CRM・偽の Gemini で確かめる。"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.clients import ClientRegistry
from app.errors import ExternalServiceError
from app.field_map import FieldMap
from app.needs.analysis import ANALYZE_PATH, NeedsAnalysisTask, run_needs_analysis
from app.runtime import Runtime
from app.services.crm import CrmWriteForbidden
from tests.conftest import API_KEY, FakeCrm, FakeLlm, FakeTasks, base_client_config

FM = FieldMap()
N = FM.needs
A = N.analysis
NEED = N.need
Q = N.inquiry
T = N.own_technology
MR = FM.meeting_record
ANALYSIS_ID = "9000000000000000001"


@pytest.fixture
def registry(request: pytest.FixtureRequest) -> ClientRegistry:
    config = base_client_config()
    config["clients"]["default"]["features"] = {"needs_analysis": getattr(request, "param", True)}
    return ClientRegistry.from_dict(config)


def seed(crm: FakeCrm, **analysis: Any) -> None:
    crm.add(
        A.module,
        ANALYSIS_ID,
        {A.name: "10月", A.period_from: "2026-10-01", A.period_to: "2026-10-31", **analysis},
    )
    # 商談記録：期間内で完了・期間内で処理中（使わない）・期間外（使わない）
    crm.add(MR.module, "m1", {
        "Created_Time": "2026-10-10T10:00:00+09:00", MR.status: "完了", MR.account: {"id": "a1", "name": "株式会社イシダ"},
        MR.summary: "グミの計量の相談", MR.needs: "・流動性の悪いグミの計量",
    })  # fmt: skip
    crm.add(
        MR.module,
        "m2",
        {"Created_Time": "2026-10-11T10:00:00+09:00", MR.status: "録音中", MR.summary: "途中"},
    )
    crm.add(
        MR.module, "m3", {"Created_Time": "2026-09-01T10:00:00+09:00", MR.status: "完了", MR.summary: "前月"}
    )
    # 問合せ：本文あり2件（うち1件は抜き出し済み）・短い本文（使わない）・本文なし（使わない）
    crm.add(Q.module, "q1", {"Created_Time": "2026-10-05T09:00:00+09:00", Q.request: "食品用シリコンで3Dプリントできますか。",
                             Q.item: "技術相談", Q.company: "みらいフーズ"})  # fmt: skip
    crm.add(Q.module, "q2", {"Created_Time": "2026-10-06T09:00:00+09:00", Q.request: "観光船の定期メンテナンスの見積をお願いします。",
                             Q.company: "ワールド"})  # fmt: skip
    crm.add(Q.module, "q3", {"Created_Time": "2026-10-07T09:00:00+09:00", Q.request: "テストです"})
    crm.add(Q.module, "q4", {"Created_Time": "2026-10-08T09:00:00+09:00", Q.request: None})
    crm.add(NEED.module, "n-old", {
        "Created_Time": "2026-10-06T12:00:00+09:00", NEED.name: "【TEST】船の保守", NEED.detail: "定期点検",
        NEED.need_type: "サービス", NEED.source_type: "WEB問合せ", NEED.company: "ワールド",
        NEED.need_date: "2026-10-06", NEED.extract_key: "inquiry:q2:1",
    })  # fmt: skip
    crm.add(T.module, "t1", {T.name: "高精度計量", T.summary: "多点計量", T.strengths: "粘着物に強い"})


def analysis_writes(crm: FakeCrm) -> list[dict[str, Any]]:
    return crm.writes_to(ANALYSIS_ID)


async def run(rt: Runtime, *, final: bool = True) -> str:
    return await run_needs_analysis(
        rt, NeedsAnalysisTask(client_id="default", record_id=ANALYSIS_ID), final_attempt=final
    )


async def test_extracts_new_sources_and_writes_report(runtime: Runtime, crm: FakeCrm, llm: FakeLlm) -> None:
    seed(crm)
    assert await run(runtime) == "done"
    extract = [c for c in llm.calls if c["task"] == "needs_extract"]
    assert len(extract) == 1
    text = extract[0]["parts"][0].text
    assert "# 元: meeting:m1" in text and "# 元: inquiry:q1" in text
    assert "inquiry:q2" not in text, "抜き出し済みの元は抜き出し直さない"
    assert "m2" not in text and "m3" not in text and "q3" not in text and "q4" not in text
    assert "流動性の悪いグミの計量" in text, "商談記録は要約・ニーズを使う"

    created = [d for op, mod, _, d in crm.writes if op == "create" and mod == NEED.module]
    assert {d[NEED.extract_key] for d in created} == {"meeting:m1:1", "inquiry:q1:1"}
    q1 = next(d for d in created if d[NEED.extract_key] == "inquiry:q1:1")
    assert q1[NEED.source_type] == "WEB問合せ" and q1[NEED.company] == "みらいフーズ"
    assert q1[NEED.need_date] == "2026-10-05" and q1[NEED.analysis] == {"id": ANALYSIS_ID}
    assert q1[NEED.source_url] == "https://crm.zoho.com/crm/demo2/tab/CustomModule3/q1"

    first, final = analysis_writes(crm)[0], analysis_writes(crm)[-1]
    assert first[A.status] == "実行中"
    assert final[A.status] == "完了" and final[A.error_message] is None
    assert "ニーズ 3件" in final[A.counts] and "新しく抜き出した 2件" in final[A.counts]
    assert "1. 計量の精度（3件・3社）" in final[A.top_needs]
    # ウィジェット（widgets/needs-analysis/app/analysis.js の THEME_HEAD）が読む形
    assert re.match(r"^\s*(\d+)\.\s*(.+?)（(\d+)件・(\d+)社）\s*$", final[A.top_needs].split("\n")[0])
    assert "■ 計量の精度 × 高精度計量" in final[A.matched_needs]
    assert "求める声：3件・3社" in final[A.product_ideas]
    analyze = next(c for c in llm.calls if c["task"] == "needs_analyze")
    assert "高精度計量" in analyze["parts"][1].text, "自社技術を渡す"
    assert "【TEST】" not in analyze["parts"][0].text, "【TEST】は分析に渡さない"


@pytest.fixture(autouse=True)
def modules(crm: FakeCrm) -> None:
    crm.modules = [
        {"api_name": "Inquiry", "module_name": "CustomModule3"},
        {"api_name": "MeetingRecords", "module_name": "CustomModule13"},
    ]


async def test_second_run_does_not_extract_again(runtime: Runtime, crm: FakeCrm, llm: FakeLlm) -> None:
    seed(crm)
    await run(runtime)
    llm.calls.clear()
    assert await run(runtime) == "done"
    assert not [c for c in llm.calls if c["task"] == "needs_extract"]
    assert [c for c in llm.calls if c["task"] == "needs_analyze"]


async def test_targets_and_period_limit_sources(runtime: Runtime, crm: FakeCrm, llm: FakeLlm) -> None:
    seed(crm, **{A.targets: "WEB問合せだけ", A.period_to: "2026-10-05"})
    await run(runtime)
    text = next(c for c in llm.calls if c["task"] == "needs_extract")["parts"][0].text
    assert "inquiry:q1" in text and "meeting:" not in text
    final = analysis_writes(crm)[-1]
    assert "商談 0件・WEB問合せ 1件 → ニーズ 1件" in final[A.counts], (
        "期間外の抜き出し済みのニーズ（10/6）は数えない"
    )


async def test_no_needs_in_period(runtime: Runtime, crm: FakeCrm, llm: FakeLlm) -> None:
    crm.add(A.module, ANALYSIS_ID, {A.period_from: "2025-01-01", A.period_to: "2025-01-31"})
    assert await run(runtime) == "done"
    assert analysis_writes(crm)[-1][A.top_needs] == "期間内にニーズが見つかりませんでした。"
    assert not [c for c in llm.calls if c["task"] == "needs_analyze"]


async def test_failure_is_recorded_on_last_attempt(runtime: Runtime, crm: FakeCrm, llm: FakeLlm) -> None:
    seed(crm)
    llm.fail = ExternalServiceError("gemini", "混雑", retryable=True)
    with pytest.raises(ExternalServiceError):
        await run(runtime, final=False)
    assert analysis_writes(crm)[-1][A.status] == "実行中", "再試行の前は失敗を書かない"
    assert await run(runtime, final=True) == "failed"
    last = analysis_writes(crm)[-1]
    assert last[A.status] == "失敗" and "混雑" in last[A.error_message]


async def test_bad_period_is_recorded(runtime: Runtime, crm: FakeCrm) -> None:
    crm.add(A.module, ANALYSIS_ID, {A.period_from: "2026-11-01", A.period_to: "2026-10-01"})
    assert await run(runtime, final=False) == "failed", "直らない失敗はすぐ記録する"
    assert "開始" in analysis_writes(crm)[-1][A.error_message]


async def test_invalid_analysis_json_is_retried_then_fails(
    runtime: Runtime, crm: FakeCrm, llm: FakeLlm
) -> None:
    seed(crm)
    llm.analyze_output = "{壊れた"
    assert await run(runtime) == "failed"
    assert len([c for c in llm.calls if c["task"] == "needs_analyze"]) == 2


async def test_unknown_need_ids_are_ignored(runtime: Runtime, crm: FakeCrm, llm: FakeLlm) -> None:
    seed(crm)
    llm.analyze_output = json.dumps(
        {
            "themes": [{"theme": "X", "description": "d", "need_ids": ["n-old", "作り話"]}],
            "matched": [],
            "product_ideas": [],
        }
    )
    await run(runtime)
    final = analysis_writes(crm)[-1]
    assert "1. X（1件・1社）" in final[A.top_needs], "入力に無い ID は数えない"
    assert final[A.matched_needs] == "自社技術で応えられるニーズは見つかりませんでした。"


def test_api_queues_analysis(client: TestClient, tasks: FakeTasks) -> None:
    headers = {"X-API-Key": API_KEY}
    for _ in range(2):
        res = client.post("/api/needs/analyze", json={"record_id": ANALYSIS_ID}, headers=headers)
        assert res.status_code == 200
    assert len(tasks.enqueued) == 1, "続けて押されたボタンは1回にまとめる"
    assert tasks.enqueued[0]["path"] == ANALYZE_PATH
    assert tasks.enqueued[0]["payload"] == {"client_id": "default", "record_id": ANALYSIS_ID}
    assert client.post("/api/needs/analyze", json={"record_id": ANALYSIS_ID}).status_code == 401
    assert client.post("/api/needs/analyze", json={"record_id": "x'or"}, headers=headers).status_code == 422


def test_internal_endpoint_runs_analysis(client: TestClient, crm: FakeCrm) -> None:
    seed(crm)
    res = client.post(ANALYZE_PATH, json={"client_id": "default", "record_id": ANALYSIS_ID})
    assert res.json() == {"status": "done", "record_id": ANALYSIS_ID}


def test_writes_go_only_to_needs_modules() -> None:
    crm = FakeCrm()
    for module in (NEED.module, A.module):
        crm.check_writable(module)
    for module in (T.module, Q.module, "Accounts"):
        with pytest.raises(CrmWriteForbidden):
            crm.check_writable(module)


@pytest.mark.parametrize("registry", [False], indirect=True)
def test_api_is_off_unless_the_client_enables_it(client: TestClient, tasks: FakeTasks, crm: FakeCrm) -> None:
    seed(crm)
    res = client.post("/api/needs/analyze", json={"record_id": ANALYSIS_ID}, headers={"X-API-Key": API_KEY})
    assert res.status_code == 404 and not tasks.enqueued
    assert (
        client.post(ANALYZE_PATH, json={"client_id": "default", "record_id": ANALYSIS_ID}).json()["status"]
        == "failed"
    )
    assert not crm.writes, "使わないクライアントの CRM には何も書かない"


async def test_logs_hold_no_inquiry_or_need_text(
    runtime: Runtime, crm: FakeCrm, caplog: pytest.LogCaptureFixture
) -> None:
    seed(crm)
    with caplog.at_level("DEBUG"):
        await run(runtime)
    logged = caplog.text + " ".join(str(r.__dict__) for r in caplog.records)
    for secret in (
        "食品用シリコン",
        "流動性の悪いグミ",
        "グミの計量の相談",
        "みらいフーズ",
        "高精度計量",
        "計量の精度",
    ):
        assert secret not in logged


async def test_top_needs_are_ranked_by_count_and_examples_span_companies(
    runtime: Runtime, crm: FakeCrm, llm: FakeLlm
) -> None:
    crm.add(A.module, ANALYSIS_ID, {A.period_from: "2026-10-01", A.period_to: "2026-10-31"})
    for i, company in enumerate(["A社", "A社", "B社", "C社"]):
        crm.add(NEED.module, f"n{i}", {
            "Created_Time": "2026-10-06T12:00:00+09:00", NEED.name: f"声{i}", NEED.company: company, NEED.source_type: "WEB問合せ",
            NEED.need_date: "2026-10-06", NEED.extract_key: f"inquiry:x{i}:1",
        })  # fmt: skip
    llm.analyze_output = json.dumps(
        {
            "themes": [
                {"theme": "少ない", "description": "d", "need_ids": ["n3"]},
                {"theme": "多い", "description": "d", "need_ids": ["n0", "n1", "n2"]},
            ],
            "matched": [],
            "product_ideas": [],
        }
    )
    await run(runtime)
    top = analysis_writes(crm)[-1][A.top_needs]
    assert top.index("1. 多い（3件・2社）") < top.index("2. 少ない（1件・1社）"), "件数の多い順"
    assert "例：A社「声0」、B社「声2」、A社「声1」" in top, "別の会社の例を先に"
