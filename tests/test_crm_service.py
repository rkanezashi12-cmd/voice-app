"""CrmService（本物）を respx で確かめる。書き込み先のガード・DRY_RUN・【TEST】・DC の切り替え。"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
import respx

from app.clients import ZohoConfig
from app.errors import ExternalServiceError
from app.field_map import FieldMap, build_field_map
from app.services.crm import CrmService, CrmWriteForbidden
from app.services.zoho_auth import ZohoAuth

US_API = "https://www.zohoapis.com/crm/v8"
US_TOKEN = "https://accounts.zoho.com/oauth/v2/token"


def make_crm(
    http: httpx.AsyncClient, *, dry_run: bool = False, test_records: bool = True, dc: str = "us"
) -> CrmService:
    zoho = ZohoConfig(dc=dc, client_id="env:A", client_secret="env:B", refresh_token="env:C")
    auth = ZohoAuth(
        http, accounts_url=zoho.accounts_base, client_id="cid", client_secret="csec", refresh_token="rt",
        retry_base_delay=0,
    )  # fmt: skip
    return CrmService(
        http, auth, api_domain=zoho.api_base, field_map=FieldMap(), dry_run=dry_run, test_records=test_records,
        retry_base_delay=0,
    )  # fmt: skip


def token_route(router: respx.MockRouter, url: str = US_TOKEN) -> respx.Route:
    return router.post(url).mock(
        return_value=httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600, "api_domain": "x"})
    )


def success(record_id: str = "111", action: str | None = None) -> httpx.Response:
    item = {"code": "SUCCESS", "status": "success", "message": "ok", "details": {"id": record_id}}
    if action:
        item["action"] = action
    return httpx.Response(200, json={"data": [item]})


async def test_update_sends_put_without_triggering_workflows(respx_mock: respx.MockRouter) -> None:
    token = token_route(respx_mock)
    put = respx_mock.put(f"{US_API}/MeetingRecords/111").mock(return_value=success())
    async with httpx.AsyncClient() as http:
        crm = make_crm(http)
        await crm.update_record("MeetingRecords", "111", {"Status": "完了"})
        await crm.update_record("MeetingRecords", "111", {"Status": "完了"})
    assert token.call_count == 1, "アクセストークンはキャッシュして使い回す"
    request = put.calls[0].request
    assert request.headers["Authorization"] == "Zoho-oauthtoken at-1"
    assert json.loads(request.content) == {"data": [{"Status": "完了"}], "trigger": []}
    form = dict(x.split("=") for x in token.calls[0].request.content.decode().split("&"))
    assert form["grant_type"] == "refresh_token"


@pytest.mark.parametrize("module", ["Accounts", "Contacts", "Deals", "Leads", "Tasks", "Unknown_Module"])
async def test_writes_to_other_modules_are_blocked(respx_mock: respx.MockRouter, module: str) -> None:
    async with httpx.AsyncClient() as http:
        crm = make_crm(http)
        with pytest.raises(CrmWriteForbidden):
            await crm.update_record(module, "1", {"Name": "x"})
        with pytest.raises(CrmWriteForbidden):
            await crm.create_record(module, {"Name": "x"})
        with pytest.raises(CrmWriteForbidden):
            await crm.upsert_record(module, {"Name": "x"}, ["Name"])
    assert len(respx_mock.calls) == 0, "ガードで止まり、Zoho には何も送らない"


async def test_guard_also_applies_in_dry_run() -> None:
    async with httpx.AsyncClient() as http:
        crm = make_crm(http, dry_run=True)
        with pytest.raises(CrmWriteForbidden):
            await crm.update_record("Accounts", "1", {"Name": "x"})


def test_field_map_cannot_point_writes_at_standard_modules() -> None:
    fm = build_field_map({"meeting_record": {"module": "Deals"}})
    with pytest.raises(CrmWriteForbidden):
        CrmService(None, None, api_domain="https://x", field_map=fm, dry_run=True, test_records=True)  # type: ignore[arg-type]


def test_crm_service_has_no_delete() -> None:
    assert not [name for name in dir(CrmService) if "delete" in name.lower()]


async def test_dry_run_sends_no_writes(
    respx_mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    async with httpx.AsyncClient() as http:
        crm = make_crm(http, dry_run=True)
        with caplog.at_level(logging.INFO):
            await crm.update_record("MeetingRecords", "111", {"Status": "完了", "Summary": "秘密の要約"})
            assert await crm.create_record("MeetingRecords", {"Name": "x"}) == "dry-run"
            assert await crm.upsert_record("Glossary", {"Name": "x"}, ["Name"]) == ("dry-run", "insert")
    assert len(respx_mock.calls) == 0
    skipped = [r for r in caplog.records if r.getMessage() == "dry_run.skip"]
    assert len(skipped) == 3
    assert skipped[0].fields["fields"] == ["Status", "Summary"], "項目名だけを残し、値は出さない"
    assert "秘密の要約" not in caplog.text


async def test_created_records_get_test_prefix(respx_mock: respx.MockRouter) -> None:
    token_route(respx_mock)
    post = respx_mock.post(f"{US_API}/MeetingRecords/upsert").mock(return_value=success("222", "insert"))
    async with httpx.AsyncClient() as http:
        crm = make_crm(http)
        record_id, action = await crm.upsert_record(
            "MeetingRecords", {"Name": "2026-09-28 オンライン商談", "Recall_ID": "u1"}, ["Recall_ID"]
        )
    assert (record_id, action) == ("222", "insert")
    body = json.loads(post.calls[0].request.content)
    assert body["data"][0]["Name"] == "【TEST】2026-09-28 オンライン商談"
    assert body["duplicate_check_fields"] == ["Recall_ID"]
    assert body["trigger"] == []


async def test_no_prefix_when_test_records_disabled(respx_mock: respx.MockRouter) -> None:
    token_route(respx_mock)
    post = respx_mock.post(f"{US_API}/MeetingRecords").mock(return_value=success("333"))
    async with httpx.AsyncClient() as http:
        crm = make_crm(http, test_records=False)
        await crm.create_record("MeetingRecords", {"Name": "本番の記録"})
    assert json.loads(post.calls[0].request.content)["data"][0]["Name"] == "本番の記録"


async def test_refreshes_token_once_on_401(respx_mock: respx.MockRouter) -> None:
    token = token_route(respx_mock)
    get = respx_mock.get(f"{US_API}/MeetingRecords/111").mock(
        side_effect=[
            httpx.Response(401, json={"code": "INVALID_TOKEN"}),
            httpx.Response(200, json={"data": [{"id": "111"}]}),
        ]
    )
    async with httpx.AsyncClient() as http:
        record = await make_crm(http).get_record("MeetingRecords", "111")
    assert record == {"id": "111"}
    assert token.call_count == 2
    assert get.call_count == 2


async def test_error_response_becomes_exception(respx_mock: respx.MockRouter) -> None:
    token_route(respx_mock)
    respx_mock.put(f"{US_API}/MeetingRecords/111").mock(
        return_value=httpx.Response(
            400,
            json={
                "data": [
                    {
                        "code": "INVALID_DATA",
                        "status": "error",
                        "message": "invalid data",
                        "details": {"api_name": "Status"},
                    }
                ]
            },
        )
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(ExternalServiceError) as err:
            await make_crm(http).update_record("MeetingRecords", "111", {"Status": "x"})
    assert err.value.code == "INVALID_DATA"
    assert "Status" in err.value.message
    assert err.value.retryable is False


async def test_retries_server_errors(respx_mock: respx.MockRouter) -> None:
    token_route(respx_mock)
    put = respx_mock.put(f"{US_API}/MeetingRecords/111").mock(side_effect=[httpx.Response(503), success()])
    async with httpx.AsyncClient() as http:
        await make_crm(http).update_record("MeetingRecords", "111", {"Status": "完了"})
    assert put.call_count == 2


async def test_does_not_retry_client_errors(respx_mock: respx.MockRouter) -> None:
    token_route(respx_mock)
    put = respx_mock.put(f"{US_API}/MeetingRecords/111").mock(return_value=httpx.Response(400, json={}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(ExternalServiceError):
            await make_crm(http).update_record("MeetingRecords", "111", {"Status": "完了"})
    assert put.call_count == 1


async def test_jp_data_center_can_be_selected(respx_mock: respx.MockRouter) -> None:
    token = token_route(respx_mock, "https://accounts.zoho.jp/oauth/v2/token")
    get = respx_mock.get("https://www.zohoapis.jp/crm/v8/MeetingRecords/1").mock(
        return_value=httpx.Response(204)
    )
    async with httpx.AsyncClient() as http:
        assert await make_crm(http, dc="jp").get_record("MeetingRecords", "1") is None
    assert token.called and get.called


async def test_coql_and_list(respx_mock: respx.MockRouter) -> None:
    token_route(respx_mock)
    coql = respx_mock.post(f"{US_API}/coql").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "1"}]})
    )
    respx_mock.get(f"{US_API}/Glossary").mock(
        side_effect=[
            httpx.Response(200, json={"data": [{"Term": "A"}], "info": {"more_records": True}}),
            httpx.Response(200, json={"data": [{"Term": "B"}], "info": {"more_records": False}}),
        ]
    )
    async with httpx.AsyncClient() as http:
        crm = make_crm(http)
        assert await crm.coql("select id from MeetingRecords where Recall_ID = 'x' limit 1") == [{"id": "1"}]
        assert [r["Term"] for r in await crm.list_records("Glossary", ["Term"])] == ["A", "B"]
    assert json.loads(coql.calls[0].request.content)["select_query"].startswith("select id")
