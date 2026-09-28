"""入口ごとの「文字起こしを得る」部品。

共通処理（process.py）は TranscriptSource だけを使う。入口D（Recall.ai Mobile Recording SDK。未提供）を
足すときは、ここにソースを1つ追加し、process.build_source() に登録する。
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from app import recording_objects as ro
from app.clients import RecallConfig
from app.config import Settings
from app.errors import PermanentError, TranscriptNotReady
from app.logs import log_event
from app.pipeline.ai import MeetingAI
from app.pipeline.models import MeetingContext, Transcript, Utterance, join_words
from app.services import audio
from app.services.recall import RecallService
from app.services.recall_events import recording_id_from_sdk_upload, transcript_shortcut

logger = logging.getLogger(__name__)

# Recall.ai の文字起こしの状態コード
_DONE = {"done", "complete", "completed"}
_FAILED = {"failed", "error", "fatal"}


@dataclass
class SourceInfo:
    record_id: str | None = None
    recall_id: str | None = None
    started_at: datetime | None = None
    owner_id: str | None = None
    # 録音があるか（ボットが会議に入れなかった場合は False）
    has_media: bool = True


class TranscriptSource(Protocol):
    kind: str

    async def prepare(self) -> SourceInfo: ...

    async def load(self, ctx: MeetingContext, glossary: str) -> Transcript: ...

    async def cleanup(self) -> None: ...


def transcript_from_recall(data: Any) -> Transcript:
    """Recall.ai の文字起こし JSON（[{participant: {name}, words: [{text}]}]）を発言の列にする。"""
    if isinstance(data, dict):
        data = data.get("transcript") or data.get("data") or []
    utterances: list[Utterance] = []
    for seg in data if isinstance(data, list) else []:
        if not isinstance(seg, dict):
            continue
        participant = seg.get("participant") if isinstance(seg.get("participant"), dict) else {}
        speaker = participant.get("name") or seg.get("speaker") or "不明"
        words = seg.get("words") if isinstance(seg.get("words"), list) else []
        text = join_words([w.get("text", "") for w in words if isinstance(w, dict)])
        if text:
            utterances.append(Utterance(str(speaker).strip() or "不明", text))
    return Transcript(utterances).merged()


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class _RecallSource:
    """ボット・デスクトップ共通：録音の文字起こしを待って取得する。"""

    kind = "recall"

    def __init__(
        self, recall: RecallService, config: RecallConfig, *, request_transcript: bool = True
    ) -> None:
        self.recall = recall
        self.config = config
        self.recording_id: str | None = None
        # 会議後の文字起こしを依頼するのは最初の試行だけ（完了待ちの再確認では依頼し直さない）
        self.request_transcript = request_transcript

    async def load(self, ctx: MeetingContext, glossary: str) -> Transcript:
        if not self.recording_id:
            raise PermanentError("録音データがありません（会議に参加できなかった可能性があります）")
        recording = await self.recall.get_recording(self.recording_id)
        code, url = transcript_shortcut(recording)
        if code is None:
            if self.config.transcription_mode == "async" and self.request_transcript:
                # 会議後の文字起こしをまだ依頼していない
                await self.recall.create_transcript(self.recording_id, self.config.transcript_request)
            raise TranscriptNotReady("文字起こしの完了待ち")
        if code in _FAILED:
            raise PermanentError(f"Recall.ai の文字起こしに失敗しました（{code}）")
        if code not in _DONE or not url:
            raise TranscriptNotReady("文字起こしの完了待ち")
        transcript = transcript_from_recall(await self.recall.download_json(url))
        log_event(
            logger,
            "recall.transcript_loaded",
            recording_id=self.recording_id,
            utterances=len(transcript.utterances),
            chars=transcript.char_count,
        )
        return transcript


class RecallBotSource(_RecallSource):
    kind = "recall_bot"

    def __init__(
        self, recall: RecallService, config: RecallConfig, bot_id: str, *, request_transcript: bool = True
    ) -> None:
        super().__init__(recall, config, request_transcript=request_transcript)
        self.bot_id = bot_id

    async def prepare(self) -> SourceInfo:
        bot = await self.recall.get_bot(self.bot_id)
        metadata = bot.get("metadata") if isinstance(bot.get("metadata"), dict) else {}
        recordings = [r for r in bot.get("recordings") or [] if isinstance(r, dict)]
        if recordings:
            self.recording_id = recordings[0].get("id")
        started = _parse_dt(recordings[0].get("started_at")) if recordings else None
        return SourceInfo(
            record_id=metadata.get("record_id"),
            recall_id=self.bot_id,
            started_at=started or _parse_dt(bot.get("join_at")),
            has_media=bool(self.recording_id),
        )

    async def cleanup(self) -> None:
        await self.recall.delete_bot_media(self.bot_id)


class RecallDesktopSource(_RecallSource):
    kind = "recall_desktop"

    def __init__(
        self,
        recall: RecallService,
        config: RecallConfig,
        upload_id: str,
        recording_id: str | None,
        *,
        request_transcript: bool = True,
    ) -> None:
        super().__init__(recall, config, request_transcript=request_transcript)
        self.upload_id = upload_id
        self.recording_id = recording_id
        self._metadata: dict[str, Any] = {}

    async def prepare(self) -> SourceInfo:
        upload = await self.recall.get_sdk_upload(self.upload_id)
        self._metadata = upload.get("metadata") if isinstance(upload.get("metadata"), dict) else {}
        self.recording_id = self.recording_id or recording_id_from_sdk_upload(upload)
        return SourceInfo(
            record_id=None,
            recall_id=self.upload_id,
            started_at=_parse_dt(upload.get("created_at")),
            owner_id=self._metadata.get("owner_id"),
        )

    async def cleanup(self) -> None:
        if self.recording_id:
            await self.recall.delete_recording(self.recording_id)


class WebRecordingSource:
    """入口B：GCS の分割録音を結合・分割し、Gemini で文字起こしする（話者A/B）。"""

    kind = "web_recording"

    def __init__(
        self, storage: Any, ai: MeetingAI, settings: Settings, client_id: str, record_id: str
    ) -> None:
        self.storage = storage
        self.ai = ai
        self.settings = settings
        self.client_id = client_id
        self.record_id = record_id
        self.prefix = ro.prefix(client_id, record_id)

    async def prepare(self) -> SourceInfo:
        return SourceInfo(record_id=self.record_id)

    async def load(self, ctx: MeetingContext, glossary: str) -> Transcript:
        objects = await self.storage.list(self.prefix)
        chunks = [c for c in (ro.parse_object(o.name, o.size) for o in objects) if c is not None]
        sessions = ro.group_sessions(chunks)
        if not sessions:
            raise PermanentError("録音データが見つかりません（1日を過ぎて自動削除された可能性があります）")

        async def download(chunk: ro.Chunk, dest: Path) -> None:
            await self.storage.download(chunk.name, dest)

        with tempfile.TemporaryDirectory(prefix="rec-") as tmp:
            segments = await audio.prepare_segments(
                sessions,
                download,
                Path(tmp),
                segment_seconds=self.settings.audio_segment_seconds,
                window_seconds=self.settings.audio_split_window_seconds,
                bitrate=self.settings.audio_bitrate,
            )
        return await self.ai.transcribe(segments, ctx, glossary)

    async def cleanup(self) -> None:
        await self.storage.delete_prefix(self.prefix)
