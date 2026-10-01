"""録音アプリの外部サービス（Zoho のログイン・Google の逆ジオコーディング・CRM の組織）を respx で確かめる。"""

from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from app.errors import ExternalServiceError
from app.services.geocoding import GEOCODE_URL, Geocoder, Place, parse_place
from app.services.zoho_login import LOGIN_SCOPES, ZohoLogin, ZohoLoginError
from tests.test_crm_service import US_API, make_crm, token_route

ACCOUNTS = "https://accounts.zoho.com"
REDIRECT = "https://meeting-notes.example.run.app/auth/callback"


def make_login(http: httpx.AsyncClient) -> ZohoLogin:
    return ZohoLogin(
        http,
        accounts_base=ACCOUNTS,
        api_base="https://www.zohoapis.com",
        client_id="login-client",
        client_secret="login-secret",
        redirect_uri=REDIRECT,
        retry_base_delay=0,
    )


def comp(name: str, *types: str) -> dict[str, object]:
    return {"long_name": name, "short_name": name, "types": [*types, "political"]}


# ---- Zoho のログイン ----


async def test_authorize_url_asks_only_for_user_and_org_read() -> None:
    async with httpx.AsyncClient() as http:
        url = make_login(http).authorize_url("state-1")
    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == f"{ACCOUNTS}/oauth/v2/auth"
    query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert query == {
        "scope": ",".join(LOGIN_SCOPES),
        "client_id": "login-client",
        "response_type": "code",
        "access_type": "online",
        "redirect_uri": REDIRECT,
        "state": "state-1",
    }
    assert LOGIN_SCOPES == ("ZohoCRM.users.READ", "ZohoCRM.org.READ")


async def test_exchange_and_current_user(respx_mock: respx.MockRouter) -> None:
    token = respx_mock.post(f"{ACCOUNTS}/oauth/v2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "user-at", "expires_in": 3600})
    )
    users = respx_mock.get(f"{US_API}/users").mock(
        return_value=httpx.Response(
            200,
            json={
                "users": [
                    {"id": "9001", "full_name": "金指 営業", "email": "s@example.com", "status": "active"}
                ]
            },
        )
    )
    respx_mock.get(f"{US_API}/org").mock(return_value=httpx.Response(200, json={"org": [{"id": "4928"}]}))
    async with httpx.AsyncClient() as http:
        login = make_login(http)
        user = await login.current_user(await login.exchange("code-1"))
    assert (user.user_id, user.name, user.status, user.org_id) == ("9001", "金指 営業", "active", "4928")
    form = parse_qs(token.calls[0].request.content.decode())
    assert form["grant_type"] == ["authorization_code"]
    assert form["code"] == ["code-1"]
    assert form["redirect_uri"] == [REDIRECT]
    request = users.calls[0].request
    assert request.url.params["type"] == "CurrentUser"
    assert request.headers["Authorization"] == "Zoho-oauthtoken user-at"


async def test_exchange_error_with_http_200(respx_mock: respx.MockRouter) -> None:
    """Zoho は認可コードが無効でも HTTP 200 で {"error": ...} を返す。"""
    respx_mock.post(f"{ACCOUNTS}/oauth/v2/token").mock(
        return_value=httpx.Response(200, json={"error": "invalid_code"})
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(ZohoLoginError, match="invalid_code"):
            await make_login(http).exchange("used-code")


async def test_account_without_crm_access_is_not_crm_user(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{US_API}/users").mock(return_value=httpx.Response(401, json={"code": "INVALID_TOKEN"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(ZohoLoginError) as err:
            await make_login(http).current_user("user-at")
    assert err.value.reason == "not_crm_user"


async def test_crm_user_without_api_access_gets_its_own_reason(respx_mock: respx.MockRouter) -> None:
    """プロファイルで API の利用が許可されていない CRM ユーザーは 403（管理者が直せるので理由を分ける）。"""
    respx_mock.get(f"{US_API}/users").mock(return_value=httpx.Response(403, json={"code": "NO_PERMISSION"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(ZohoLoginError) as err:
            await make_login(http).current_user("user-at")
    assert err.value.reason == "no_api_access"


async def test_crm_org_of_backend_connection(respx_mock: respx.MockRouter) -> None:
    token_route(respx_mock)
    respx_mock.get(f"{US_API}/org").mock(
        return_value=httpx.Response(200, json={"org": [{"id": "4928", "domain_name": "org1"}]})
    )
    async with httpx.AsyncClient() as http:
        org = await make_crm(http).get_org()
    assert org["id"] == "4928"


# ---- Google の逆ジオコーディング ----


def test_parse_designated_city_ward_and_town() -> None:
    body = {
        "results": [
            {
                "address_components": [
                    comp("１丁目", "sublocality", "sublocality_level_3"),
                    comp("山下町", "sublocality", "sublocality_level_2"),
                    comp("中区", "sublocality", "sublocality_level_1", "ward"),
                    comp("横浜市", "locality"),
                    comp("神奈川県", "administrative_area_level_1"),
                    comp("日本", "country"),
                ]
            }
        ]
    }
    assert parse_place(body) == Place(prefecture="神奈川県", city="横浜市", ward="中区", town="山下町")


def test_parse_tokyo_special_ward_and_ward_without_ward_type() -> None:
    tokyo = {
        "results": [
            {
                "address_components": [
                    comp("１丁目", "sublocality", "sublocality_level_3"),
                    comp("丸の内", "sublocality", "sublocality_level_2"),
                    comp("千代田区", "locality"),
                    comp("東京都", "administrative_area_level_1"),
                ]
            }
        ]
    }
    assert parse_place(tokyo) == Place(prefecture="東京都", city="千代田区", ward=None, town="丸の内")
    osaka = {
        "results": [
            {"address_components": [comp("plus code", "plus_code")]},
            {
                "address_components": [
                    comp("梅田", "sublocality", "sublocality_level_2"),
                    comp("北区", "sublocality", "sublocality_level_1"),
                    comp("大阪市", "locality"),
                    comp("大阪府", "administrative_area_level_1"),
                ]
            },
        ]
    }
    assert parse_place(osaka) == Place(prefecture="大阪府", city="大阪市", ward="北区", town="梅田")
    assert parse_place({"results": []}) == Place()


async def test_reverse_geocoding_keeps_key_and_position_out_of_logs(
    respx_mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    route = respx_mock.get(GEOCODE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "OK",
                "results": [
                    {
                        "address_components": [
                            comp("横浜市", "locality"),
                            comp("神奈川県", "administrative_area_level_1"),
                        ]
                    }
                ],
            },
        )
    )
    with caplog.at_level(logging.DEBUG):
        async with httpx.AsyncClient() as http:
            place = await Geocoder(http, api_key="secret-maps-key", retry_base_delay=0).reverse(
                35.4437, 139.638
            )
    assert place.city == "横浜市"
    params = route.calls[0].request.url.params
    assert (params["latlng"], params["language"], params["key"]) == (
        "35.443700,139.638000",
        "ja",
        "secret-maps-key",
    )
    logs = "\n".join(f"{r.getMessage()} {getattr(r, 'fields', '')}" for r in caplog.records)
    assert "secret-maps-key" not in logs
    assert "35.4437" not in logs


async def test_reverse_geocoding_errors(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(GEOCODE_URL).mock(
        return_value=httpx.Response(200, json={"status": "REQUEST_DENIED", "error_message": "key"})
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(ExternalServiceError, match="REQUEST_DENIED"):
            await Geocoder(http, api_key="k", retry_base_delay=0).reverse(35.0, 139.0)


async def test_zero_results_is_an_empty_place(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(GEOCODE_URL).mock(
        return_value=httpx.Response(200, json={"status": "ZERO_RESULTS", "results": []})
    )
    async with httpx.AsyncClient() as http:
        assert await Geocoder(http, api_key="k", retry_base_delay=0).reverse(0.0, 0.0) == Place()
