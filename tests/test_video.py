"""動画の解析（docs/video-analysis.md）を偽の GCS・偽の CRM・偽の Gemini で確かめる。"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.clients import ClientRegistry
from app.errors import ExternalServiceError
from app.field_map import FieldMap
from app.runtime import Runtime
from app.services.gemini import GeminiClient, LlmResult, TextPart, VideoPart
from app.videos import analysis as va
from app.videos.analysis import (
    ANALYZE_PATH,
    VideoBatchTask,
    VideoTask,
    enqueue_videos,
    run_video_analysis,
    video_title,
    windows,
)
from tests.conftest import FakeCrm, FakeLlm, FakeStorage, FakeTasks, base_client_config, make_settings

FM = FieldMap()
MR = FM.meeting_record
VIDEO = "videos/default/受注入力の説明.mp4"


@pytest.fixture
def registry(request: pytest.FixtureRequest) -> ClientRegistry:
    config = base_client_config()
    config["clients"]["default"]["features"] = getattr(
        request, "param", {"video_analysis": True, "transcript_attachment": True}
    )
    return ClientRegistry.from_dict(config)


@pytest.fixture(autouse=True)
def _duration(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """動画の長さ（既定 1500 秒）。渡された URL を記録する。"""
    urls: list[str] = []

    async def probe(url: str) -> float | None:
        urls.append(url)
        return 1500.0

    monkeypatch.setattr(va, "probe_duration_url", probe)
    return urls


def name_search(crm: FakeCrm) -> None:
    """COQL の「名前が … で終わる」検索を、偽の CRM の記録で答える。"""

    def handler(query: str) -> list[dict[str, Any]]:
        suffix = query.split("like '%", 1)[1].split("'", 1)[0]
        rows = [
            r for r in crm.records.get(MR.module, {}).values() if str(r.get(MR.name, "")).endswith(suffix)
        ]
        return [{"id": r["id"], MR.name: r.get(MR.name), MR.status: r.get(MR.status)} for r in rows[:1]]

    crm.coql_handler = handler


def video_calls(llm: FakeLlm) -> list[dict[str, Any]]:
    return [c for c in llm.calls if c["task"] == "video_describe"]


async def run(runtime: Runtime, *, force: bool = False, final: bool = True, name: str = VIDEO) -> str:
    return await run_video_analysis(
        runtime, VideoTask(client_id="default", object_name=name, force=force), final_attempt=final
    )


# ---- 積む ----


async def test_batch_enqueues_each_video_with_a_delay(
    runtime: Runtime, storage: FakeStorage, tasks: FakeTasks
) -> None:
    storage.objects.update(
        {
            "videos/default/b.mov": b"x",
            "videos/default/a.mp4": b"x",
            "videos/default/memo.txt": b"x",  # 動画でないものは積まない
            "videos/default/empty.mp4": b"",  # 空のファイルも積まない
            "videos/other/c.mp4": b"x",  # ほかのクライアントの動画は積まない
            "recordings/default/1/x/000000.webm": b"x",
        }
    )
    assert await enqueue_videos(runtime, VideoBatchTask(client_id="default")) == 2
    assert [t["payload"]["object_name"] for t in tasks.enqueued] == [
        "videos/default/a.mp4",
        "videos/default/b.mov",
    ]
    assert {t["path"] for t in tasks.enqueued} == {ANALYZE_PATH}
    assert [t["delay_seconds"] for t in tasks.enqueued] == [0, va.STAGGER_SECONDS]
    # 同じ動画を続けて積んでも二重にならない
    assert await enqueue_videos(runtime, VideoBatchTask(client_id="default")) == 0


async def test_batch_limit_takes_the_first_videos(
    runtime: Runtime, storage: FakeStorage, tasks: FakeTasks
) -> None:
    storage.objects.update({"videos/default/a.mp4": b"x", "videos/default/b.mp4": b"x"})
    assert await enqueue_videos(runtime, VideoBatchTask(client_id="default", limit=1)) == 1
    assert tasks.enqueued[0]["payload"]["object_name"] == "videos/default/a.mp4"


@pytest.mark.parametrize("registry", [{}], indirect=True)
async def test_batch_needs_the_feature(runtime: Runtime, storage: FakeStorage, tasks: FakeTasks) -> None:
    storage.objects["videos/default/a.mp4"] = b"x"
    with pytest.raises(Exception, match="動画の解析を使えません"):
        await enqueue_videos(runtime, VideoBatchTask(client_id="default"))
    assert tasks.enqueued == []


# ---- 1本の解析 ----


async def test_analyzes_video_and_creates_record(
    runtime: Runtime, storage: FakeStorage, crm: FakeCrm, llm: FakeLlm, _duration: list[str]
) -> None:
    storage.objects[VIDEO] = b"video"
    name_search(crm)
    assert await run(runtime) == "done"

    # 1500 秒を 600 秒ずつ：0-600・600-1200・1200-1500
    calls = video_calls(llm)
    parts = [c["parts"][0] for c in calls]
    assert [(p.start_seconds, p.end_seconds) for p in parts] == [(0, 600), (600, 1200), (1200, 1500)]
    assert all(isinstance(p, VideoPart) and p.file_uri == f"gs://bucket/{VIDEO}" for p in parts)
    assert parts[0].mime_type == "video/mp4" and parts[0].fps == 1.0
    assert "10:00〜20:00" in calls[1]["parts"][1].text
    # 2つ目からは、前の記録の終わりを手がかりに渡す
    assert "直前の部分の記録" not in calls[0]["parts"][1].text
    assert "直前の部分の記録" in calls[1]["parts"][1].text
    assert _duration == [f"https://storage.googleapis.com/bucket/{VIDEO}?X-Goog-Signature=fake-get"]

    # 商談記録を1回で作る
    creates = [w for w in crm.writes if w[0] == "create"]
    assert len(creates) == 1 and len(crm.writes) == 1
    data = creates[0][3]
    assert data[MR.name] == "動画解析 受注入力の説明"
    assert data[MR.status] == "完了"
    assert data[MR.category] == "動画解析"
    assert data[MR.summary].startswith("受注入力の流れを説明している。")
    assert "【出てきた画面】\n・受注入力：受注を登録する" in data[MR.summary]
    assert data[MR.issues] == "・納期を Excel で別に管理している"
    assert data[MR.needs] == "・納期を一覧で見られること"
    assert data[MR.next_actions] == "・締め日はいつか"
    assert "0:00〜10:00" in data[MR.transcript] and "20:00〜25:00" in data[MR.transcript]

    # 全文の添付と、動画の削除
    [(module, record_id, filename, content)] = crm.attachments
    assert (module, record_id, filename) == (MR.module, "new-1", "動画解析_受注入力の説明.txt")
    text = content.decode("utf-8")
    assert text.startswith("﻿動画：受注入力の説明") and "長さ：25:00" in text
    assert "■ 要件の候補\n・納期を一覧で見られること" in text and "■ 場面ごとの記録" in text
    assert VIDEO not in storage.objects and storage.deleted_prefixes == [VIDEO]


async def test_truncated_window_is_split_in_half(
    runtime: Runtime, storage: FakeStorage, crm: FakeCrm, llm: FakeLlm
) -> None:
    storage.objects[VIDEO] = b"video"
    name_search(crm)
    llm.video_outputs = [LlmResult("途中で切れた", truncated=True), "前半", "後半", "2つ目", "3つ目"]
    assert await run(runtime) == "done"
    spans = [(c["parts"][0].start_seconds, c["parts"][0].end_seconds) for c in video_calls(llm)]
    assert spans[:3] == [(0, 600), (0, 300), (300, 600)]
    text = crm.writes[0][3][MR.transcript]
    assert "途中で切れた" not in text and text.startswith("前半\n\n後半\n\n2つ目")


async def test_truncated_short_window_is_kept_with_a_note(
    runtime: Runtime, storage: FakeStorage, crm: FakeCrm, llm: FakeLlm, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def probe(url: str) -> float | None:
        return 90.0

    monkeypatch.setattr(va, "probe_duration_url", probe)
    storage.objects[VIDEO] = b"video"
    name_search(crm)
    llm.video_outputs = [LlmResult("ここまで", truncated=True)]
    assert await run(runtime) == "done"
    assert len(video_calls(llm)) == 1
    assert "（0:00〜1:30 の終わりの方は、詳しく書ききれませんでした）" in crm.writes[0][3][MR.transcript]


async def test_unknown_length_reads_whole_video(
    runtime: Runtime, storage: FakeStorage, crm: FakeCrm, llm: FakeLlm, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def probe(url: str) -> float | None:
        return None

    monkeypatch.setattr(va, "probe_duration_url", probe)
    storage.objects[VIDEO] = b"video"
    name_search(crm)
    assert await run(runtime) == "done"
    [call] = video_calls(llm)
    assert call["parts"][0].start_seconds is None and "全体" in call["parts"][1].text


async def test_done_video_is_skipped_unless_forced(
    runtime: Runtime, storage: FakeStorage, crm: FakeCrm, llm: FakeLlm
) -> None:
    storage.objects[VIDEO] = b"video"
    crm.add(MR.module, "r1", {MR.name: "【TEST】動画解析 受注入力の説明", MR.status: "完了"})
    name_search(crm)
    assert await run(runtime) == "skipped"
    assert llm.calls == [] and crm.writes == [] and VIDEO in storage.objects

    # やり直すときは同じ記録を更新する（新しく作らない）
    assert await run(runtime, force=True) == "done"
    assert [(w[0], w[2]) for w in crm.writes] == [("update", "r1")]


async def test_failed_record_is_updated_on_the_next_run(
    runtime: Runtime, storage: FakeStorage, crm: FakeCrm
) -> None:
    storage.objects[VIDEO] = b"video"
    crm.add(MR.module, "r1", {MR.name: "動画解析 受注入力の説明", MR.status: "失敗"})
    name_search(crm)
    assert await run(runtime) == "done"
    assert [(w[0], w[2]) for w in crm.writes] == [("update", "r1")]
    assert crm.record("r1")[MR.error_message] is None


async def test_missing_video_records_failure(runtime: Runtime, crm: FakeCrm, llm: FakeLlm) -> None:
    name_search(crm)
    assert await run(runtime) == "failed"
    [(kind, _, _, data)] = crm.writes
    assert kind == "create" and data[MR.status] == "失敗" and "動画が見つかりません" in data[MR.error_message]
    assert llm.calls == []


@pytest.mark.parametrize(
    "name", ["videos/other/a.mp4", "videos/default/../other/a.mp4", "videos/default/a.txt"]
)
async def test_rejects_objects_outside_the_client_folder(
    runtime: Runtime, crm: FakeCrm, llm: FakeLlm, name: str
) -> None:
    name_search(crm)
    assert await run(runtime, name=name) == "failed"
    assert llm.calls == []


async def test_retryable_error_is_retried_before_the_last_attempt(
    runtime: Runtime, storage: FakeStorage, crm: FakeCrm, llm: FakeLlm
) -> None:
    storage.objects[VIDEO] = b"video"
    name_search(crm)
    llm.fail = ExternalServiceError("gemini", "混雑", status=429, retryable=True)
    with pytest.raises(ExternalServiceError):
        await run(runtime, final=False)
    assert crm.writes == [] and VIDEO in storage.objects
    # 最後の試行では「失敗」を書く。動画は残す（やり直せるように）
    assert await run(runtime, final=True) == "failed"
    assert crm.writes[0][3][MR.status] == "失敗"
    assert VIDEO in storage.objects


async def test_dry_run_keeps_video(runtime: Runtime, storage: FakeStorage, crm: FakeCrm) -> None:
    storage.objects[VIDEO] = b"video"
    name_search(crm)
    crm.dry_run = True
    assert await run(runtime) == "done"
    assert VIDEO in storage.objects


@pytest.mark.parametrize("registry", [{"video_analysis": True}], indirect=True)
async def test_no_attachment_without_the_feature(
    runtime: Runtime, storage: FakeStorage, crm: FakeCrm
) -> None:
    storage.objects[VIDEO] = b"video"
    name_search(crm)
    assert await run(runtime) == "done"
    assert crm.attachments == []


async def test_logs_do_not_contain_file_names_or_urls(
    runtime: Runtime, storage: FakeStorage, crm: FakeCrm, caplog: pytest.LogCaptureFixture
) -> None:
    storage.objects[VIDEO] = b"video"
    name_search(crm)
    with caplog.at_level(logging.INFO):
        assert await run(runtime) == "done"
    logged = caplog.text + "".join(str(getattr(r, "fields", "")) for r in caplog.records)
    assert "video.done" in logged
    assert "受注入力" not in logged and "Signature" not in logged and "gs://" not in logged


def test_internal_endpoints(client: TestClient, storage: FakeStorage, tasks: FakeTasks, crm: FakeCrm) -> None:
    storage.objects[VIDEO] = b"video"
    name_search(crm)
    res = client.post("/internal/video-batch", json={"client_id": "default"})
    assert res.json() == {"status": "enqueued", "videos": 1}
    res = client.post(ANALYZE_PATH, json=tasks.enqueued[0]["payload"])
    assert res.json() == {"status": "done"}


# ---- 小道具 ----


def test_windows_merge_a_short_tail() -> None:
    assert windows(1500, 600) == [(0, 600), (600, 1200), (1200, 1500)]
    assert windows(1230, 600) == [(0, 600), (600, 1230)]
    assert windows(300, 600) == [(0, 300)]
    assert windows(None, 600) == [(None, None)]


def test_video_title_removes_characters_that_break_queries() -> None:
    assert video_title("videos/default/受注'入力\\%.MP4") == "受注入力"
    assert video_title("videos/default/'%.mp4") == "動画"


def test_gemini_builds_video_part_and_media_resolution() -> None:
    from google.genai import types

    gemini = GeminiClient(make_settings(gemini_video_media_resolution="low"))
    [content] = gemini.build_contents(
        [VideoPart("gs://b/v.mp4", "video/mp4", 0, 600, 1.0), TextPart("書き出す")]
    )
    part = content.parts[0]
    assert part.file_data.file_uri == "gs://b/v.mp4" and part.file_data.mime_type == "video/mp4"
    assert (part.video_metadata.start_offset, part.video_metadata.end_offset) == ("0s", "600s")
    config = gemini.build_config("指示", None, media_resolution="low")
    assert config.media_resolution == types.MediaResolution.MEDIA_RESOLUTION_LOW
    assert gemini.build_config("指示", None).media_resolution is None


def test_media_resolution_setting_is_checked() -> None:
    with pytest.raises(ValueError):
        make_settings(gemini_video_media_resolution="ultra")
