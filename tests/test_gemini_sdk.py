"""インストールされた google-genai SDK に対して、呼び出し方（型・引数名）が合っているかを確かめる。

実際の Vertex AI には接続しない（クライアントは偽物）。
"""

from __future__ import annotations

import logging
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
        usage_metadata=SimpleNamespace(
            prompt_token_count=10, candidates_token_count=5, thoughts_token_count=7
        ),
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
    assert (result.input_tokens, result.output_tokens, result.thinking_tokens) == (10, 5, 7), (
        "考えた分のトークンも出力として課金されるので受け取る"
    )
    assert models.calls[0]["model"] == "m"


def test_thinking_level_matches_the_installed_sdk() -> None:
    client = GeminiClient(make_settings())
    config = client.build_config("指示", None, "low")
    assert config.thinking_config.thinking_level == types.ThinkingLevel.LOW
    assert client.build_config("指示", None).thinking_config is None, "空ならモデルの既定のまま"


async def test_each_task_uses_its_thinking_level(caplog: pytest.LogCaptureFixture) -> None:
    settings = make_settings(gemini_thinking_summarize="medium")
    assert (settings.gemini_thinking_transcribe, settings.gemini_thinking_correct) == ("low", "low"), (
        "既定は low（考えた分も出力として課金されるので、深い推論の要らない処理は抑える）"
    )
    fake, models = fake_client([response("a"), response("b"), response("c")])
    client = GeminiClient(settings, client=fake)
    with caplog.at_level(logging.INFO):
        for task in ["transcribe", "correct", "summarize"]:
            await client.generate(task=task, model="m", system_instruction="s", parts=[TextPart("x")])
    levels = [c["config"].thinking_config.thinking_level for c in models.calls]
    assert levels == [types.ThinkingLevel.LOW, types.ThinkingLevel.LOW, types.ThinkingLevel.MEDIUM]
    logged = [r.fields for r in caplog.records if r.getMessage() == "gemini.generated"]
    assert [(f["thinking"], f["thinking_tokens"]) for f in logged] == [
        ("low", 7),
        ("low", 7),
        ("medium", 7),
    ], "費用を確かめられるように、考える量と考えた分のトークン数をログに出す"


async def test_older_models_get_no_thinking_level() -> None:
    fake, models = fake_client([response("a"), response("b")])
    client = GeminiClient(make_settings(), client=fake)
    await client.generate(
        task="correct", model="gemini-2.5-flash", system_instruction="s", parts=[TextPart("x")]
    )
    await client.generate(
        task="correct", model="gemini-3.5-flash", system_instruction="s", parts=[TextPart("x")]
    )
    assert models.calls[0]["config"].thinking_config is None, "2.5 までは thinking_level を受け付けない"
    assert models.calls[1]["config"].thinking_config.thinking_level == types.ThinkingLevel.LOW


def test_thinking_level_settings_are_checked() -> None:
    assert make_settings(gemini_thinking_correct=" LOW ").gemini_thinking_correct == "low"
    assert make_settings(gemini_thinking_correct="").thinking_level("correct") == ""
    with pytest.raises(ValueError):
        make_settings(gemini_thinking_correct="max")


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
