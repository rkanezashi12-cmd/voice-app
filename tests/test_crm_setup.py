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
    def __init__(
        self,
        modules: list[dict[str, Any]],
        fields: dict[str, list[dict[str, Any]]],
        reject: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.modules = modules
        self.fields = fields
        # api_name → Zoho が返す失敗の行。一部だけ失敗したときの応答（HTTP 207）を再現する
        self.reject = reject or {}
        self.calls: list[tuple[str, str, Any]] = []
        self.field_reads: list[str] = []  # 項目の設定を読んだモジュール

    def request(self, method: str, path: str, params: dict | None = None, body: Any = None) -> Any:
        self.calls.append((method, path, body))
        if method == "GET" and path == "/crm/v8/settings/modules":
            return {"modules": self.modules}
        if method == "GET" and path == "/crm/v8/settings/fields":
            self.field_reads.append(params["module"])
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
            return {"modules": [{"code": "SUCCESS", "status": "success"}]}
        if method == "POST" and path == "/crm/v8/settings/fields":
            rows = []
            for f in body["fields"]:
                if f["api_name"] in self.reject:
                    rows.append(self.reject[f["api_name"]])
                else:
                    self.fields[params["module"]].append(dict(f))
                    rows.append({"code": "SUCCESS", "status": "success", "message": "field created"})
            return {"fields": rows}
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


def _meeting_records_without(api_name: str) -> FakeApi:
    """商談記録に api_name 以外の項目がそろっている組織（2回目以降の apply の想定）。"""
    spec = next(m for m in cs.SPEC if m.api_name == "MeetingRecords")
    fields = {
        "MeetingRecords": [
            {"api_name": "Name", "field_label": spec.display_field_label, "data_type": "text"},
            *(dict(f) for f in spec.fields if f["api_name"] != api_name),
        ],
        "Glossary": [
            {"api_name": "Name", "field_label": "用語", "data_type": "text"},
            *(dict(f) for m in cs.SPEC if m.api_name == "Glossary" for f in m.fields),
        ],
        "Contacts": [{"api_name": "Last_Name", "field_label": "姓", "data_type": "text"}],
    }
    modules = [
        *STANDARD,
        {"api_name": "Contacts", "generated_type": "default", "plural_label": "連絡先"},
        {"api_name": "MeetingRecords", "generated_type": "custom", "plural_label": "商談記録"},
        {"api_name": "Glossary", "generated_type": "custom", "plural_label": "用語辞書"},
    ]
    return FakeApi(modules, fields)


def test_contacts_multiselect_lookup_is_added_and_explained() -> None:
    """先方担当者（連絡先）だけが無い組織では、それだけを作る。連絡先に足されるものを計画に書く（2026-10-02 了承）。"""
    api = _meeting_records_without("Customer_Contacts")
    plan = cs.build_plan(api, cs.SPEC)
    created = plan.create_fields["MeetingRecords"]
    assert [f["api_name"] for f in created] == ["Customer_Contacts"]
    msl = created[0]["multiselectlookup"]
    assert msl["connected_details"]["module"]["api_name"] == "Contacts"
    text = cs.describe(plan)
    assert "Contacts に項目「商談記録」と関連リストを足します" in text
    assert "データは書き換えません" in text
    assert "Contacts" in api.field_reads, "連絡先の項目を読んで、同じ名前が無いか確かめる"
    cs.apply(api, plan)
    assert cs.verify(api, cs.SPEC) == []
    assert {c[0] for c in api.calls} <= {"GET", "POST"}


def test_stops_when_contacts_already_has_the_reverse_field_label() -> None:
    api = _meeting_records_without("Customer_Contacts")
    api.fields["Contacts"].append({"api_name": "Old_Link", "field_label": "商談記録", "data_type": "lookup"})
    with pytest.raises(cs.SetupError, match="Contacts に表示名「商談記録」"):
        cs.build_plan(api, cs.SPEC)


def test_stops_when_linking_module_name_is_taken() -> None:
    api = _meeting_records_without("Customer_Contacts")
    api.modules.append(
        {"api_name": "Link1", "generated_type": "linking", "plural_label": "商談記録の先方担当者"}
    )
    with pytest.raises(cs.SetupError, match="中間モジュール"):
        cs.build_plan(api, cs.SPEC)


def test_lookup_fields_have_display_label() -> None:
    for m in cs.SPEC:
        for f in m.fields:
            if f["data_type"] == "lookup":
                assert f["lookup"]["display_label"]


# 実機で返った応答（unique.case_sensitive に true を送ったとき。同じ回の他の4項目は作成された）
CASE_SENSITIVE_REJECTED = {
    "code": "INVALID_DATA",
    "status": "error",
    "message": "Invalid value.",
    "details": {
        "api_name": "case_sensitive",
        "json_path": "$.fields[4].unique.case_sensitive",
        "supported_values": [False],
    },
}


def test_recall_id_is_unique_without_case_sensitivity() -> None:
    """Zoho が受け付けるのは case_sensitive: false だけ（true は INVALID_DATA）。"""
    recall = next(f for m in cs.SPEC for f in m.fields if f["api_name"] == "Recall_ID")
    assert recall["unique"] == {"case_sensitive": False}


def test_partial_failure_stops_and_names_the_failed_field() -> None:
    """一部だけ失敗した応答を成功扱いにせず、失敗した項目名を出して止まる。再実行で足りない分だけ作る。"""
    api = FakeApi(list(STANDARD), {}, reject={"Recall_ID": CASE_SENSITIVE_REJECTED})
    with pytest.raises(cs.SetupError, match="Recall_ID: INVALID_DATA"):
        cs.apply(api, cs.build_plan(api, cs.SPEC))
    posted = [
        f["api_name"]
        for method, path, body in api.calls
        if method == "POST" and path == "/crm/v8/settings/fields"
        for f in body["fields"]
    ]
    assert posted[-1] == "Recall_ID"  # 失敗した回で止まり、後ろの項目は送らない
    assert "Contact_Name" in {f["api_name"] for f in api.fields["MeetingRecords"]}

    api.reject = {}
    again = cs.build_plan(api, cs.SPEC)
    assert again.create_modules == []
    assert again.create_fields["MeetingRecords"][0]["api_name"] == "Recall_ID"
    assert "Contact_Name" not in {f["api_name"] for f in again.create_fields["MeetingRecords"]}
    cs.apply(api, again)
    assert cs.verify(api, cs.SPEC) == []


def test_verify_reports_type_mismatch() -> None:
    api = FakeApi(list(STANDARD), {})
    cs.apply(api, cs.build_plan(api, cs.SPEC))
    summary = next(f for f in api.fields["MeetingRecords"] if f["api_name"] == "Summary")
    summary["data_type"] = "text"
    assert cs.verify(api, cs.SPEC) == ["MeetingRecords.Summary の種類が text（想定 textarea）"]


def test_field_details_show_actual_settings() -> None:
    api = FakeApi(list(STANDARD), {})
    cs.apply(api, cs.build_plan(api, cs.SPEC))
    lines = cs.field_details(api, cs.SPEC)
    assert len(lines) == sum(len(m.fields) + 1 for m in cs.SPEC)
    assert "MeetingRecords.Name「商談記録名」text" in lines
    assert 'MeetingRecords.Recall_ID「Recall ID」text length=255 unique={"case_sensitive": false}' in lines
    assert (
        'MeetingRecords.Transcript「文字起こし全文」textarea length=32000 textarea={"type": "large"}' in lines
    )
    assert "MeetingRecords.Account「取引先」lookup 参照先=Accounts" in lines
    msl = next(line for line in lines if line.startswith("MeetingRecords.Customer_Contacts"))
    assert msl.startswith(
        "MeetingRecords.Customer_Contacts「先方担当者（連絡先）」multiselectlookup 複数選択="
    )


def test_check_access_reports_missing_scope_without_writing() -> None:
    """権限の確認は読み取りだけで、足りないスコープを名前とエラーコードで返す（本番で出た OAUTH_SCOPE_MISMATCH）。"""
    calls: list[tuple[str, str]] = []

    class ScopeApi:
        def request(self, method: str, path: str, params: dict | None = None, body: Any = None) -> Any:
            calls.append((method, path))
            if path in ("/crm/v8/MeetingRecords/actions/count", "/crm/v8/coql"):
                raise cs.SetupError(
                    f'{method} {path} が失敗しました（HTTP 401）: {{"code": "OAUTH_SCOPE_MISMATCH", "message": "x"}}'
                )
            return {}

    results = cs.check_access(ScopeApi())
    failed = {(scope, label) for ok, scope, label, _ in results if not ok}
    assert failed == {
        ("ZohoCRM.modules.custom.ALL", "商談記録の件数"),
        ("ZohoCRM.coql.READ", "COQL（商談記録の id）"),
    }
    assert {code for ok, _, _, code in results if not ok} == {"OAUTH_SCOPE_MISMATCH"}
    # POST は COQL（読み取り）だけ
    assert {m for m, p in calls if p != "/crm/v8/coql"} == {"GET"}


def test_backend_scopes_are_the_same_everywhere() -> None:
    """CLAUDE.md のスコープ・check-access で試すスコープ・接続スクリプトで発行するスコープが一致している。"""
    root = Path(__file__).resolve().parent.parent
    block = (root / "CLAUDE.md").read_text(encoding="utf-8").split("### OAuth スコープ")[1].split("```")[1]
    documented = {s.strip() for s in block.replace("\n", ",").split(",") if s.strip()}
    assert documented == {scope for scope, *_ in cs.ACCESS_CHECKS}
    script = (root / "scripts" / "setup_zoho_connection.sh").read_text(encoding="utf-8")
    line = next(x for x in script.splitlines() if x.startswith("SCOPES="))
    assert set(line.split('"')[1].split(",")) == documented


def test_diagnose_shows_each_probe_without_record_contents() -> None:
    """切り分け用の診断は、組織・ユーザーと各読み取りの成否を出し、レコードの中身は出さない。"""

    class DiagApi:
        def request(self, method: str, path: str, params: dict | None = None, body: Any = None) -> Any:
            if path == "/crm/v8/org":
                return {"org": [{"company_name": "株式会社テスト", "domain_name": "org1", "id": "9"}]}
            if path == "/crm/v8/users":
                return {
                    "users": [
                        {
                            "full_name": "管理者",
                            "email": "a@example.com",
                            "profile": {"name": "Administrator"},
                        }
                    ]
                }
            if path == "/crm/v8/settings/modules/MeetingRecords":
                return {"modules": [{"api_name": "MeetingRecords", "generated_type": "custom", "id": "5"}]}
            if path.endswith("/actions/count"):
                return {"count": 3}
            if path == "/crm/v8/coql":
                return {"data": [{"id": "123"}]}
            if path == "/crm/v8/MeetingRecords/123" and params is None:
                raise cs.SetupError(
                    f'{method} {path} が失敗しました（HTTP 400）: {{"code": "INVALID_MODULE", "message": "x"}}'
                )
            return {"data": [{"id": "123", "Name": "秘密の商談名"}]}

    lines = cs.diagnose(DiagApi(), "123")
    text = "\n".join(lines)
    assert "株式会社テスト（org1 / 9）" in text
    assert "管理者 / a@example.com / プロファイル Administrator" in text
    assert "3 件" in text and "1 件ヒット" in text
    assert 'NG  レコードの取得（GET /MeetingRecords/123）: HTTP 400 {"code": "INVALID_MODULE"' in text
    assert "OK  レコードの取得・項目指定（GET /MeetingRecords/123?fields=Name）" in text
    assert "秘密の商談名" not in text
    with pytest.raises(cs.SetupError):
        cs.diagnose(DiagApi(), "1 or 1=1")
