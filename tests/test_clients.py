from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.clients import ClientRegistry, SecretResolver
from app.errors import ConfigError
from app.field_map import build_field_map
from tests.conftest import base_client_config


def test_zoho_defaults_to_us_and_can_switch_dc() -> None:
    data = base_client_config()
    reg = ClientRegistry.from_dict(data)
    zoho = reg.get("default").need_zoho()
    assert zoho.accounts_base == "https://accounts.zoho.com"
    assert zoho.api_base == "https://www.zohoapis.com"
    data["clients"]["default"]["zoho"]["dc"] = "jp"
    jp = ClientRegistry.from_dict(data).get("default").need_zoho()
    assert (jp.accounts_base, jp.api_base) == ("https://accounts.zoho.jp", "https://www.zohoapis.jp")
    data["clients"]["default"]["zoho"]["api_domain"] = "https://custom.example/"
    assert ClientRegistry.from_dict(data).get("default").need_zoho().api_base == "https://custom.example"


def test_literal_secrets_are_rejected() -> None:
    data = base_client_config()
    data["clients"]["default"]["zoho"]["refresh_token"] = "1000.abcdef"
    with pytest.raises(ValidationError):
        ClientRegistry.from_dict(data)


def test_unknown_keys_and_bad_ids_are_rejected() -> None:
    data = base_client_config()
    data["clients"]["default"]["workdrive_folder_id"] = "x"
    with pytest.raises(ValidationError):
        ClientRegistry.from_dict(data)
    with pytest.raises(ValidationError):
        ClientRegistry.from_dict({"clients": {"Bad_ID": {}}})
    with pytest.raises(ConfigError):
        ClientRegistry.from_dict({"clients": {}})


def test_minimal_client_for_recorder_test() -> None:
    reg = ClientRegistry.from_dict({"clients": {"default": {"api_key": "env:X"}}})
    with pytest.raises(ConfigError):
        reg.get("default").need_zoho()
    with pytest.raises(ConfigError):
        reg.get("other")


def test_field_map_overrides() -> None:
    fm = build_field_map({"meeting_record": {"module": "Shodan_Kiroku"}, "category_choices": ["新規"]})
    assert fm.meeting_record.module == "Shodan_Kiroku"
    assert fm.meeting_record.summary == "Summary"
    assert fm.category_choices == ("新規",)
    with pytest.raises(ValueError):
        build_field_map({"unknown": {}})
    with pytest.raises(ValidationError):
        build_field_map({"meeting_record": {"typo_field": "x"}})


def test_load_from_json_text() -> None:
    reg = ClientRegistry.load(json_text='{"clients": {"default": {"api_key": "env:X"}}}')
    assert reg.get("default").api_key == "env:X"
    with pytest.raises(ConfigError):
        ClientRegistry.load()


async def test_secret_resolver_env_and_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_SECRET", "v1")
    resolver = SecretResolver()
    assert await resolver.get("env:MY_SECRET") == "v1"
    monkeypatch.setenv("MY_SECRET", "v2")
    assert await resolver.get("env:MY_SECRET") == "v1", "プロセス内でキャッシュする"
    with pytest.raises(ConfigError):
        await resolver.get("env:NOT_SET_ANYWHERE")


async def test_regional_secrets_use_regional_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[Any] = []

    class FakeSecretClient:
        def __init__(self, client_options: Any = None) -> None:
            created.append(client_options)

        async def access_secret_version(self, name: str) -> Any:
            payload = type("P", (), {"data": f"value-of-{name.split('/')[-3]}".encode()})
            return type("R", (), {"payload": payload})

    monkeypatch.setattr("google.cloud.secretmanager.SecretManagerServiceAsyncClient", FakeSecretClient)
    resolver = SecretResolver()
    regional = "sm:projects/p/locations/asia-northeast1/secrets/backend-api-key/versions/latest"
    global_ref = "sm:projects/p/secrets/recall-api-key/versions/latest"
    assert await resolver.get(regional) == "value-of-backend-api-key"
    assert await resolver.get(global_ref) == "value-of-recall-api-key"
    assert created == [{"api_endpoint": "secretmanager.asia-northeast1.rep.googleapis.com"}, None]
