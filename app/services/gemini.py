"""Vertex AI の Gemini（google-genai SDK、リージョナルエンドポイント）。

- location は GEMINI_LOCATION（既定 asia-northeast1）。global は設定の段階で拒否している。
- モデル名は環境変数（GEMINI_MODEL_TRANSCRIBE / GEMINI_MODEL_TEXT）。
- 考える量（thinking_level）は処理ごとに環境変数（GEMINI_THINKING_TRANSCRIBE など。既定 low）。
  考えた分のトークン（thoughts_token_count）も出力として課金されるので、ログに出す。
- 共通処理からは LlmClient（generate だけ）として使う。テストでは偽物に差し替える。
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

from app.config import Settings
from app.errors import ExternalServiceError
from app.logs import log_event

logger = logging.getLogger(__name__)

RETRYABLE_CODES = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 4
# thinking_level を受け付けないモデル（Gemini 2.5 までは thinking_budget の方式）。これらには考える量を指定しない
_NO_THINKING_LEVEL = re.compile(r"^gemini-[12][.-]")


@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class AudioPart:
    data: bytes
    mime_type: str


Part = TextPart | AudioPart


@dataclass(frozen=True)
class LlmResult:
    text: str
    truncated: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None
    thinking_tokens: int | None = None


class LlmClient(Protocol):
    async def generate(
        self,
        *,
        task: str,
        model: str,
        system_instruction: str,
        parts: list[Part],
        json_schema: dict[str, Any] | None = None,
    ) -> LlmResult: ...


class GeminiClient:
    def __init__(self, settings: Settings, *, client: Any = None, retry_base_delay: float = 2.0) -> None:
        self._settings = settings
        self._client = client
        self._retry_base_delay = retry_base_delay

    def _get_client(self) -> Any:
        if self._client is None:
            from google import genai

            self._client = genai.Client(
                vertexai=True,
                project=self._settings.need("gcp_project_id"),
                location=self._settings.gemini_location,
            )
        return self._client

    def build_config(
        self, system_instruction: str, json_schema: dict[str, Any] | None, thinking_level: str = ""
    ) -> Any:
        from google.genai import types

        kwargs: dict[str, Any] = {
            "system_instruction": system_instruction,
            "temperature": self._settings.gemini_temperature,
            "max_output_tokens": self._settings.gemini_max_output_tokens,
        }
        if json_schema is not None:
            kwargs["response_mime_type"] = "application/json"
            kwargs["response_json_schema"] = json_schema
        if thinking_level:
            # Gemini 3 系は thinking_level（数値の thinking_budget は使えない）
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=thinking_level.upper())
        return types.GenerateContentConfig(**kwargs)

    @staticmethod
    def build_contents(parts: list[Part]) -> list[Any]:
        from google.genai import types

        converted = [
            types.Part.from_text(text=p.text)
            if isinstance(p, TextPart)
            else types.Part.from_bytes(data=p.data, mime_type=p.mime_type)
            for p in parts
        ]
        return [types.Content(role="user", parts=converted)]

    async def generate(
        self,
        *,
        task: str,
        model: str,
        system_instruction: str,
        parts: list[Part],
        json_schema: dict[str, Any] | None = None,
    ) -> LlmResult:
        from google.genai import errors

        client = self._get_client()
        thinking_level = "" if _NO_THINKING_LEVEL.match(model) else self._settings.thinking_level(task)
        config = self.build_config(system_instruction, json_schema, thinking_level)
        contents = self.build_contents(parts)
        for attempt in range(MAX_ATTEMPTS):
            try:
                resp = await client.aio.models.generate_content(model=model, contents=contents, config=config)
                break
            except errors.APIError as exc:
                code = getattr(exc, "code", None)
                retryable = code in RETRYABLE_CODES
                if retryable and attempt < MAX_ATTEMPTS - 1:
                    log_event(
                        logger, "gemini.retry", logging.WARNING, task=task, attempt=attempt + 1, status=code
                    )
                    await asyncio.sleep(self._retry_base_delay * (2**attempt))
                    continue
                raise ExternalServiceError(
                    "gemini", f"{task} に失敗しました（{code}）", status=code, retryable=retryable
                ) from exc
        return self._result(task, model, resp, thinking_level)

    @staticmethod
    def _result(task: str, model: str, resp: Any, thinking_level: str = "") -> LlmResult:
        text = getattr(resp, "text", None) or ""
        candidates = getattr(resp, "candidates", None) or []
        finish = getattr(candidates[0], "finish_reason", None) if candidates else None
        finish_name = getattr(finish, "name", None) or str(finish or "")
        usage = getattr(resp, "usage_metadata", None)
        result = LlmResult(
            text=text,
            truncated=finish_name.endswith("MAX_TOKENS"),
            input_tokens=getattr(usage, "prompt_token_count", None),
            output_tokens=getattr(usage, "candidates_token_count", None),
            thinking_tokens=getattr(usage, "thoughts_token_count", None),
        )
        log_event(
            logger,
            "gemini.generated",
            task=task,
            model=model,
            thinking=thinking_level or "default",
            finish_reason=finish_name,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            thinking_tokens=result.thinking_tokens,
            output_chars=len(text),
        )
        return result
