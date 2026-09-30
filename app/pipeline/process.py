"""共通処理（入口非依存）。/internal/process（Cloud Tasks から OIDC で呼ばれる）の中身。

a. 補正（用語辞書を注入）  b. 要約・構造化（JSON スキーマ固定）
c. CRM の「商談記録」を1回で更新（要約・構造化項目・全文 transcript / transcript_2・状態）
d. 音声を削除（Recall.ai / GCS）  e. 失敗時は状態を「失敗」にしてエラー内容を書く

- 処理済み（完了・取引先未設定で全文あり）のレコードは処理しない（重複通知・再試行の二重処理防止）。
- 途中経過として「文字起こし中」を1回だけ書く（最初の試行のときだけ）。
- 再試行できる失敗（429 / 5xx など）は、最後の試行になるまで CRM に「失敗」を書かずに再試行させる。
- Recall.ai の文字起こしが終わっていなければ、少し後に同じ処理を積み直す（TRANSCRIPT_POLL_*）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from app.client_services import ClientServices
from app.errors import AppError, ExternalServiceError, PermanentError, TranscriptNotReady
from app.logs import log_event
from app.pipeline import formatting as fmt
from app.pipeline.models import MeetingContext
from app.pipeline.records import (
    find_by_recall_id,
    is_finished,
    lookup_id,
    lookup_name,
    meeting_date,
    picklist_value,
)
from app.pipeline.sources import (
    RecallBotSource,
    RecallDesktopSource,
    SourceInfo,
    TranscriptSource,
    WebRecordingSource,
)
from app.services.tasks import task_name

logger = logging.getLogger(__name__)

PROCESS_PATH = "/internal/process"


class ProcessRequest(BaseModel):
    client_id: str
    source: Literal["recall_bot", "recall_desktop", "web_recording"]
    record_id: str | None = None
    bot_id: str | None = None
    sdk_upload_id: str | None = None
    recording_id: str | None = None
    wait_count: int = 0
    force: bool = False


@dataclass(frozen=True)
class ProcessOutcome:
    # done: 完了 / skipped: 処理済み / failed: 失敗を記録 / waiting: 文字起こし待ちで積み直した / retry: 再試行させる
    status: Literal["done", "skipped", "failed", "waiting", "retry"]
    record_id: str | None = None
    message: str | None = None


async def build_source(rt: Any, cs: ClientServices, req: ProcessRequest) -> TranscriptSource:
    if req.source == "web_recording":
        if not req.record_id:
            raise PermanentError("record_id がありません")
        return WebRecordingSource(rt.storage, rt.ai, rt.settings, cs.client_id, req.record_id)
    recall = await cs.recall()
    config = cs.config.need_recall()
    first = req.wait_count == 0
    if req.source == "recall_bot":
        if not req.bot_id:
            raise PermanentError("bot_id がありません")
        return RecallBotSource(recall, config, req.bot_id, request_transcript=first)
    if not req.sdk_upload_id:
        raise PermanentError("sdk_upload_id がありません")
    return RecallDesktopSource(recall, config, req.sdk_upload_id, req.recording_id, request_transcript=first)


class ProcessRun:
    """1回分の処理。途中で分かったレコード ID などを失敗時の書き込みに使う。"""

    def __init__(self, rt: Any, req: ProcessRequest, final_attempt: bool) -> None:
        self.rt = rt
        self.req = req
        self.final_attempt = final_attempt
        self.cs: ClientServices = rt.client_services(req.client_id)
        self.fm = self.cs.field_map
        self.record_id: str | None = req.record_id
        self.record: dict[str, Any] | None = None
        self.info = SourceInfo()
        self.source: TranscriptSource | None = None

    def _log(self, event: str, level: int = logging.INFO, **fields: Any) -> None:
        log_event(
            logger,
            event,
            level,
            client_id=self.req.client_id,
            source=self.req.source,
            record_id=self.record_id,
            **fields,
        )

    async def run(self) -> ProcessOutcome:
        started = time.monotonic()
        try:
            outcome = await self._main()
        except TranscriptNotReady:
            return await self._wait_for_transcript()
        except AppError as exc:
            return await self._failed(exc.message, retryable=exc.retryable, code=exc.code)
        except Exception as exc:  # 想定外は原因不明のため再試行に回し、最後の試行で失敗を記録する
            logger.exception("pipeline.unexpected_error", extra={"fields": {"record_id": self.record_id}})
            return await self._failed(
                f"想定外のエラー（{type(exc).__name__}）", retryable=True, code="unexpected"
            )
        if outcome.status in ("done", "skipped"):
            cleanup = await self._cleanup()
            if cleanup is not None:
                return cleanup
        self._log("pipeline.finished", status=outcome.status, seconds=round(time.monotonic() - started, 1))
        return outcome

    async def _main(self) -> ProcessOutcome:
        f, s = self.fm.meeting_record, self.fm.status
        crm = await self.cs.crm()
        self.source = await build_source(self.rt, self.cs, self.req)
        self.info = await self.source.prepare()
        self.record_id = self.record_id or self.info.record_id

        if self.record_id:
            self.record = await crm.get_record(f.module, self.record_id)
            if self.record is None:
                raise PermanentError(f"商談記録 {self.record_id} が見つかりません")
        elif self.req.source == "recall_desktop" and self.info.recall_id:
            self.record = await find_by_recall_id(crm, self.fm, self.info.recall_id)
            self.record_id = lookup_id(self.record.get("id")) if self.record else None
        else:
            raise PermanentError(
                "処理対象の商談記録が分かりません（ボットの metadata に record_id がありません）"
            )

        if self.record and is_finished(self.record, self.fm) and not self.req.force:
            self._log("pipeline.skipped_finished")
            return ProcessOutcome("skipped", self.record_id)
        if not self.info.has_media:
            raise PermanentError("録音データがありません（会議に参加できなかった可能性があります）")

        if self.record_id and self.req.wait_count == 0:
            progress = {f.status: s.transcribing, f.error_message: None}
            await crm.update_record(f.module, self.record_id, progress)
            if self.record is not None:
                # 手元の状態も合わせる。前の「参加失敗」のままだと、この後の失敗を record_failure が書かなくなる
                self.record = {**self.record, **progress}

        ctx = self._context()
        glossary = await self.rt.glossary.get(self.cs.client_id, crm, self.fm)
        transcript = await self.source.load(ctx, glossary)
        if not transcript.utterances:
            raise PermanentError("文字起こし結果が空でした（音声が無音の可能性があります）")
        self._log("pipeline.transcribed", utterances=len(transcript.utterances), chars=transcript.char_count)

        ai = self.rt.ai
        corrected = await ai.correct(transcript, glossary, ctx)
        summary = await ai.summarize(corrected, ctx, self.fm.category_choices)
        split = fmt.split_transcript(corrected.to_text(), self.fm.limits.transcript)
        if split.truncated:
            self._log(
                "transcript.truncated",
                logging.WARNING,
                total_chars=split.total_chars,
                limit_chars=self.fm.limits.transcript * 2,
            )
        has_account = bool(self.record and lookup_id(self.record.get(f.account)))
        status = s.done if has_account else s.no_account
        fields = fmt.result_fields(self.fm, summary, split, status)

        if self.record_id:
            await crm.update_record(f.module, self.record_id, fields)
        else:
            await self._create_desktop_record(fields, ctx)
        self._log(
            "pipeline.saved",
            status_value=status,
            chars=split.total_chars,
            truncated=split.truncated,
            next_actions=len(summary.next_actions),
        )
        return ProcessOutcome("done", self.record_id)

    def _context(self) -> MeetingContext:
        f = self.fm.meeting_record
        record = self.record or {}
        tz = self.cs.config.timezone
        return MeetingContext(
            client_id=self.cs.client_id,
            record_id=self.record_id,
            meeting_date=meeting_date(record.get(f.start_at), self.info.started_at, tz),
            account_name=lookup_name(record.get(f.account)),
            meeting_type=picklist_value(record.get(f.meeting_type)),
            capture_method=picklist_value(record.get(f.capture_method)),
        )

    async def _create_desktop_record(self, fields: dict[str, Any], ctx: MeetingContext) -> None:
        """デスクトップ方式で取引先が選ばれていない：記録を作って「取引先未設定」にする。"""
        f = self.fm.meeting_record
        crm = await self.cs.crm()
        data = {
            **fields,
            f.name: fmt.record_name(ctx.meeting_date, None, self.fm),
            f.recall_id: self.info.recall_id,
            f.meeting_type: self.fm.meeting_type.online,
            f.capture_method: self.fm.capture_method.desktop,
        }
        if self.info.owner_id:
            data[f.owner] = {"id": self.info.owner_id}
        record_id, action = await crm.upsert_record(f.module, data, [f.recall_id])
        self.record_id = record_id
        if action == "update":
            # 同時に取引先が選ばれていた場合は状態を直す
            record = await crm.get_record(f.module, record_id)
            if record and lookup_id(record.get(f.account)):
                await crm.update_record(f.module, record_id, {f.status: self.fm.status.done})

    async def _wait_for_transcript(self) -> ProcessOutcome:
        settings = self.rt.settings
        if self.req.wait_count >= settings.transcript_poll_max:
            return await self._failed("Recall.ai の文字起こしが時間内に終わりませんでした", retryable=False)
        next_req = self.req.model_copy(update={"wait_count": self.req.wait_count + 1})
        key = self.req.bot_id or self.req.sdk_upload_id or self.req.record_id or ""
        await self.rt.tasks.enqueue(
            PROCESS_PATH,
            next_req.model_dump(),
            name=task_name("proc-wait", self.req.client_id, key, str(next_req.wait_count)),
            delay_seconds=settings.transcript_poll_seconds,
        )
        self._log("pipeline.waiting_transcript", wait_count=next_req.wait_count)
        return ProcessOutcome("waiting", self.record_id)

    async def _failed(self, message: str, *, retryable: bool, code: str | None = None) -> ProcessOutcome:
        if retryable and not self.final_attempt:
            self._log("pipeline.retry", logging.WARNING, error_code=code, error=message)
            return ProcessOutcome("retry", self.record_id, message)
        self._log("pipeline.failed", logging.ERROR, error_code=code, error=message)
        try:
            await self.record_failure(message)
        except AppError as exc:
            self._log("pipeline.failure_not_recorded", logging.ERROR, error=exc.message)
        if self.rt.settings.delete_media_on_failure and self.source is not None:
            await self._cleanup()
        return ProcessOutcome("failed", self.record_id, message)

    async def record_failure(self, message: str) -> None:
        """状態を「失敗」にしてエラー内容を書く。デスクトップ方式でレコードが無ければ作る。"""
        f, s = self.fm.meeting_record, self.fm.status
        crm = await self.cs.crm()
        text = fmt.clip(message, self.fm.limits.error_message)
        recall_id = self.info.recall_id or self.req.sdk_upload_id
        if not self.record_id and self.req.source == "recall_desktop" and recall_id:
            self.record = await find_by_recall_id(crm, self.fm, recall_id)
            self.record_id = lookup_id(self.record.get("id")) if self.record else None
        if self.record and picklist_value(self.record.get(f.status)) == s.join_failed:
            # ボットの状態通知で「参加失敗」を記録済み。上書きしない
            return
        if self.record_id:
            await crm.update_record(f.module, self.record_id, {f.status: s.failed, f.error_message: text})
            return
        if self.req.source == "recall_desktop" and recall_id:
            if not await self._sdk_upload_exists(recall_id):
                # Recall.ai に無いアップロード（ダッシュボードのテスト送信の例など）。記録を作らない
                self._log("recall.unknown_sdk_upload", logging.WARNING, sdk_upload_id=recall_id)
                return
            data = {
                f.name: fmt.record_name(self._context().meeting_date, None, self.fm),
                f.recall_id: recall_id,
                f.meeting_type: self.fm.meeting_type.online,
                f.capture_method: self.fm.capture_method.desktop,
                f.status: s.failed,
                f.error_message: text,
            }
            self.record_id, _ = await crm.upsert_record(f.module, data, [f.recall_id])

    async def _sdk_upload_exists(self, upload_id: str) -> bool:
        """Recall.ai にアップロードがあるか。400・404 のときだけ False（ほかの失敗は、失敗の記録を優先して True）。"""
        if self.info.recall_id:
            return True  # prepare() で取得できている
        try:
            await (await self.cs.recall()).get_sdk_upload(upload_id)
        except ExternalServiceError as exc:
            return exc.status not in (400, 404)
        except AppError:
            return True
        return True

    async def _cleanup(self) -> ProcessOutcome | None:
        """音声を削除する。失敗したら再試行させる（次の試行は処理済みとして削除だけ行う）。"""
        if self.source is None:
            return None
        try:
            await self.source.cleanup()
        except AppError as exc:
            self._log("media.delete_failed", logging.ERROR, error=exc.message)
            if not self.final_attempt:
                return ProcessOutcome("retry", self.record_id, exc.message)
        return None


async def run_process(rt: Any, req: ProcessRequest, *, final_attempt: bool) -> ProcessOutcome:
    return await ProcessRun(rt, req, final_attempt).run()
