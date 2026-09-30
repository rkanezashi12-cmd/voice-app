"""Recall.ai の Webhook 本文の読み取り。

ボットの本文の形は 2026-10-01 に実物で確認した。デスクトップ（sdk_upload）の本文はまだ（docs/unverified-apis.md）。
想定する形を1か所にまとめ、足りない情報は API で取り直せるように ID だけを確実に拾う。

想定する形:
  {"event": "bot.in_waiting_room",
   "data": {"data": {"code": "in_waiting_room", "sub_code": null, "updated_at": "..."},
            "bot": {"id": "...", "metadata": {...}},
            "recording": {"id": "...", "metadata": {...}},
            "sdk_upload": {"id": "...", "metadata": {...}}}}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class RecallEvent:
    event: str
    code: str | None = None
    sub_code: str | None = None
    updated_at: datetime | None = None
    bot_id: str | None = None
    bot_metadata: dict[str, Any] = field(default_factory=dict)
    recording_id: str | None = None
    recording_metadata: dict[str, Any] = field(default_factory=dict)
    sdk_upload_id: str | None = None
    sdk_upload_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return self.event.split(".", 1)[0]

    @property
    def status(self) -> str:
        return self.event.split(".", 1)[1] if "." in self.event else self.event

    def metadata_value(self, key: str) -> str | None:
        for meta in (self.bot_metadata, self.recording_metadata, self.sdk_upload_metadata):
            value = meta.get(key)
            if isinstance(value, str) and value:
                return value
        return None


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_event(payload: dict[str, Any]) -> RecallEvent:
    event = _str(payload.get("event")) or ""
    data = _obj(payload.get("data"))
    status = _obj(data.get("data"))
    bot = _obj(data.get("bot"))
    recording = _obj(data.get("recording"))
    sdk_upload = _obj(data.get("sdk_upload"))
    return RecallEvent(
        event=event,
        code=_str(status.get("code")),
        sub_code=_str(status.get("sub_code")),
        updated_at=_parse_time(status.get("updated_at")),
        bot_id=_str(bot.get("id")),
        bot_metadata=_obj(bot.get("metadata")),
        recording_id=_str(recording.get("id")) or _str(sdk_upload.get("recording_id")),
        recording_metadata=_obj(recording.get("metadata")),
        sdk_upload_id=_str(sdk_upload.get("id")),
        sdk_upload_metadata=_obj(sdk_upload.get("metadata")),
    )


def transcript_shortcut(recording: dict[str, Any]) -> tuple[str | None, str | None]:
    """録音オブジェクトから (文字起こしの状態コード, ダウンロード URL) を取り出す。"""
    transcript = _obj(_obj(recording.get("media_shortcuts")).get("transcript"))
    if not transcript:
        return None, None
    status = _obj(transcript.get("status"))
    code = _str(status.get("code")) or _str(transcript.get("status"))
    url = _str(_obj(transcript.get("data")).get("download_url"))
    return code, url


def recording_id_from_sdk_upload(upload: dict[str, Any]) -> str | None:
    recording = upload.get("recording")
    if isinstance(recording, dict):
        return _str(recording.get("id"))
    return _str(upload.get("recording_id")) or _str(recording)
