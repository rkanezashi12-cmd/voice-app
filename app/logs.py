"""構造化ログ（Cloud Logging の JSON 形式）。

ルール：文字起こし本文・要約・API キー・トークン・署名付き URL をログに出さない。
log_event() に渡すフィールドは ID・件数・文字数・所要時間などに限る。
念のため、名前が秘密情報や本文を示すキーは値を伏せる。
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_trace: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace", default=None)

# 値を伏せるキー（完全一致）
_REDACT_EXACT = {
    "authorization",
    "api_key",
    "x-api-key",
    "token",
    "access_token",
    "refresh_token",
    "upload_token",
    "client_secret",
    "secret",
    "password",
    "signature",
    "url",
    "signed_url",
    "download_url",
    "recording_url",
    "transcript",
    "text",
    "summary",
    "content",
    "body",
    "prompt",
}
# 値を伏せるキー（接尾辞）
_REDACT_SUFFIXES = ("_token", "_secret", "_key", "_text", "_url", "_body", "_content")

REDACTED = "[REDACTED]"


def _should_redact(key: str) -> bool:
    k = key.lower()
    return k in _REDACT_EXACT or k.endswith(_REDACT_SUFFIXES)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: (REDACTED if _should_redact(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "time": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            for key, value in redact(fields).items():
                if key not in payload:
                    payload[key] = value
        trace = _trace.get()
        if trace:
            payload["logging.googleapis.com/trace"] = trace
        if record.exc_info:
            # 例外の種類と発生箇所だけを残す（例外メッセージは本文を含まない設計だが、長さは制限する）
            payload["exception"] = self.formatException(record.exc_info)[-4000:]
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    # 二重登録を避ける（他のハンドラー、たとえばテストのログ取得は残す）
    root.handlers[:] = [h for h in root.handlers if not isinstance(h.formatter, JsonFormatter)]
    root.addHandler(handler)
    root.setLevel(level.upper())
    # httpx は INFO でリクエスト URL（署名付き URL を含む）を出すので抑える
    for name in ("httpx", "httpcore", "google", "urllib3", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.WARNING)


def set_trace(header_value: str | None, project_id: str | None) -> None:
    """X-Cloud-Trace-Context ヘッダーからトレース ID を取り出してログに紐づける。"""
    if not header_value or not project_id:
        _trace.set(None)
        return
    trace_id = header_value.split("/", 1)[0].strip()
    _trace.set(f"projects/{project_id}/traces/{trace_id}" if trace_id else None)


def log_event(logger: logging.Logger, message: str, level: int = logging.INFO, /, **fields: Any) -> None:
    """構造化ログを1行出す。fields には ID・件数などだけを渡す（"event" などの名前も使える）。"""
    logger.log(level, message, extra={"fields": fields})
