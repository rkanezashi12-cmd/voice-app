from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from app.field_map import DEFAULT_FIELD_MAP

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "crm_setup.py"
_spec = importlib.util.spec_from_file_location("crm_setup", _PATH)
assert _spec and _spec.loader
cs = importlib.util.module_from_spec(_spec)
sys.modules["crm_setup"] = cs  # dataclass がモジュールを参照するため
_spec.loader.exec_module(cs)


class FakeApi:
    def __init__(self, modules: list[dict[str, Any]], fields: dict[str, list[dict[str, Any]]]) -> None:
        self.modules = modules
        self.fields = fields
        self.calls: list[tuple[str, str, Any]] = []

    def request(self, method: str, path: str, params: dict | None = None, body: Any = None) -> Any:
        self.calls.append((method, path, body))
        if method == "GET" and path == "/crm/v8/settings/modules":
            return {"modules": self.modules}
        if method == "GET" and path == "/crm/v8/settings/fields":
            return {"fields": self.fields.get(params["module"], [])}
        if method == "GET" and path == "/crm/v8/settings/profiles":
            return {"profiles": [{"id": "p1"}, {"id": "p2"}]}
        if method == "POST" and path == "/crm/v8/settings/modules":
            m = body["modules"][0]
            self.modules.append(
                {"api_name": m["api_name"], "generated_type": "custom", "plural_label": m["plural_label"]}
            )
            self.fields[m["api_name"]] = [
                {"api_name": "Name", "field_label": m["display_field"]["field_label"], "data_type": "text"}
            ]
            return {}
        if method == "POST" and path == "/crm/v8/settings/fields":
            self.fields[params["module"]] += [dict(f) for f in body["fields"]]
            return {}
        raise AssertionError(f"想定外の呼び出し {method} {path}")


STANDARD = [{"api_name": "Accounts", "generated_type": "default", "plural_label": "取引先"}]


def test_spec_matches_field_map() -> None:
    """作成する API 名が、バックエンドが読み書きする API 名（field_map）と一致している。"""
    fm = DEFAULT_FIELD_MAP
    by_module = {m.api_name: {f["api_name"] for f in m.fields} | {"Name"} for m in cs.SPEC}
    mr = fm.meeting_record
    expected = {v for k, v in mr.model_dump().items() if k not in ("module", "owner")}
    assert expected <= by_module[mr.module]
    g = fm.glossary
    assert {g.term, g.misrecognitions, g.term_type} <= by_module[g.module]
    status = next(f for m in cs.SPEC for f in m.fields if f["api_name"] == mr.status)
    assert {v["actual_value"] for v in status["pick_list_values"]} == set(fm.status.model_dump().values())


def test_org_guard_stops_on_mismatch() -> None:
    org = {"id": "1", "domain_name": "org1", "company_name": "A"}
    cs.check_org(org, {"id": "1", "domain_name": "org1", "company_name": "A"})
    with pytest.raises(cs.SetupError):
        cs.check_org(org, {"id": "1", "domain_name": "org1", "company_name": "B"})


def test_plan_creates_everything_on_empty_org_and_apply_is_idempotent() -> None:
    api = FakeApi(list(STANDARD), {})
    plan = cs.build_plan(api, cs.SPEC)
    assert [m.api_name for m in plan.create_modules] == ["MeetingRecords", "Glossary"]
    cs.apply(api, plan)
    assert cs.verify(api, cs.SPEC) == []
    posts = [c for c in api.calls if c[0] == "POST" and c[1] == "/crm/v8/settings/fields"]
    assert all(len(c[2]["fields"]) <= cs.FIELDS_PER_REQUEST for c in posts)
    # 2回目は何も作らない
    again = cs.build_plan(api, cs.SPEC)
    assert again.empty


def test_only_post_is_used_for_writes() -> None:
    api = FakeApi(list(STANDARD), {})
    cs.apply(api, cs.build_plan(api, cs.SPEC))
    assert {c[0] for c in api.calls} <= {"GET", "POST"}


def test_adds_only_missing_fields_to_existing_module() -> None:
    fields = {
        "Glossary": [
            {"api_name": "Name", "field_label": "用語", "data_type": "text"},
            {"api_name": "Term_Type", "field_label": "種類", "data_type": "picklist"},
        ]
    }
    api = FakeApi(
        [
            *STANDARD,
            {"api_name": "Glossary", "generated_type": "custom", "plural_label": "用語辞書"},
            {"api_name": "MeetingRecords", "generated_type": "custom", "plural_label": "商談記録"},
        ],
        fields,
    )
    plan = cs.build_plan(api, cs.SPEC)
    assert plan.create_modules == []
    assert [f["api_name"] for f in plan.create_fields["Glossary"]] == ["Misrecognitions"]


def test_stops_when_same_label_module_exists_with_other_api_name() -> None:
    api = FakeApi(
        [*STANDARD, {"api_name": "CustomModule3", "generated_type": "custom", "plural_label": "商談記録"}], {}
    )
    with pytest.raises(cs.SetupError):
        cs.build_plan(api, cs.SPEC)


def test_stops_when_same_label_field_exists_with_other_api_name() -> None:
    fields = {"Glossary": [{"api_name": "field1", "field_label": "誤認識例", "data_type": "textarea"}]}
    api = FakeApi(
        [*STANDARD, {"api_name": "Glossary", "generated_type": "custom", "plural_label": "用語辞書"}], fields
    )
    with pytest.raises(cs.SetupError):
        cs.build_plan(api, cs.SPEC)


def test_lookup_fields_have_display_label() -> None:
    for m in cs.SPEC:
        for f in m.fields:
            if f["data_type"] == "lookup":
                assert f["lookup"]["display_label"]
