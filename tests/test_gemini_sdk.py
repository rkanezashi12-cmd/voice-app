"""インストールされた google-genai SDK に対して、呼び出し方（型・引数名）が合っているかを確かめる。

実際の Vertex AI には接続しない（クライアントは偽物）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from google.genai import errors, types

from app.config import Settings
from app.errors import ExternalServiceError
from app.services.gemini import AudioPart, GeminiClient, TextPart
from tests.conftest import make_settings


class FakeModels:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def fake_client(responses: list[Any]) -> tuple[Any, FakeModels]:
    models = FakeModels(responses)
    return SimpleNamespace(aio=SimpleNamespace(models=models)), models


def response(text: str, finish: str = "STOP") -> Any:
    return SimpleNamespace(
        text=text,
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name=finish))],
        usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=5),
    )


def test_global_location_is_rejected() -> None:
    with pytest.raises(ValueError):
        Settings(gemini_location="global")


def test_client_uses_vertex_regional_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class Client:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("google.genai.Client", Client)
    GeminiClient(make_settings())._get_client()
    assert captured == {"vertexai": True, "project": "proj", "location": "asia-northeast1"}


def test_config_and_contents_match_the_installed_sdk() -> None:
    client = GeminiClient(make_settings())
    config = client.build_config("指示", {"type": "object", "properties": {}})
    assert isinstance(config, types.GenerateContentConfig)
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == {"type": "object", "properties": {}}
    assert config.temperature == 0.0
    contents = client.build_contents([TextPart("情報"), AudioPart(b"\xff\xfb", "audio/mpeg")])
    assert isinstance(contents[0], types.Content)
    assert contents[0].parts[0].text == "情報"
    assert contents[0].parts[1].inline_data.mime_type == "audio/mpeg"


async def test_generate_reports_truncation_and_usage() -> None:
    fake, models = fake_client([response("話者A: こんにちは", finish="MAX_TOKENS")])
    result = await GeminiClient(make_settings(), client=fake).generate(
        task="transcribe", model="m", system_instruction="s", parts=[TextPart("x")]
    )
    assert result.text == "話者A: こんにちは"
    assert result.truncated is True
    assert (result.input_tokens, result.output_tokens) == (10, 5)
    assert models.calls[0]["model"] == "m"


def _api_error(code: int) -> Exception:
    return errors.APIError(code, {"error": {"code": code, "message": "x", "status": "X"}})


async def test_retries_rate_limits() -> None:
    fake, models = fake_client([_api_error(429), response("ok")])
    result = await GeminiClient(make_settings(), client=fake, retry_base_delay=0).generate(
        task="summarize", model="m", system_instruction="s", parts=[TextPart("x")]
    )
    assert result.text == "ok"
    assert len(models.calls) == 2


async def test_client_errors_are_not_retried() -> None:
    fake, models = fake_client([_api_error(400)])
    with pytest.raises(ExternalServiceError) as err:
        await GeminiClient(make_settings(), client=fake, retry_base_delay=0).generate(
            task="summarize", model="m", system_instruction="s", parts=[TextPart("x")]
        )
    assert err.value.retryable is False
    assert len(models.calls) == 1
