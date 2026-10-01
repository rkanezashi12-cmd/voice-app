"""録音アプリの「Zoho でログイン」（/auth/login・/auth/callback・/auth/logout）。"""

from __future__ import annotations

import time
from collections.abc import Iterator
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.deps import tasks_oidc
from app.main import create_app
from app.runtime import Runtime
from app.services.zoho_login import ZohoLoginError, ZohoUser
from app.web_session import SESSION_COOKIE, STATE_COOKIE, verify_session
from tests.conftest import ORG_ID, SESSION_SECRET, FakeLogin


@pytest.fixture
def https_client(runtime: Runtime) -> Iterator[TestClient]:
    app = create_app(runtime)
    app.dependency_overrides[tasks_oidc] = lambda: None
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        yield c


def set_cookies(res: object) -> dict[str, SimpleCookie]:
    cookies: dict[str, SimpleCookie] = {}
    for header in res.headers.get_list("set-cookie"):  # type: ignore[attr-defined]
        jar = SimpleCookie()
        jar.load(header)
        for name in jar:
            cookies[name] = jar
    return cookies


def start_login(client: TestClient) -> str:
    res = client.get("/auth/login", params={"c": "default"})
    assert res.status_code == 302
    return parse_qs(urlparse(res.headers["location"]).query)["state"][0]


def test_login_redirects_to_zoho_with_state_bound_to_cookie(https_client: TestClient) -> None:
    res = https_client.get("/auth/login", params={"c": "default"})
    assert res.status_code == 302
    assert res.headers["location"].startswith("https://accounts.zoho.com/oauth/v2/auth?state=")
    cookie = set_cookies(res)[STATE_COOKIE][STATE_COOKIE]
    assert cookie["path"] == "/auth"
    assert cookie["httponly"] and cookie["secure"]
    assert cookie["samesite"].lower() == "lax"


def test_login_for_unknown_client_is_404(https_client: TestClient) -> None:
    assert https_client.get("/auth/login", params={"c": "other"}).status_code == 404
    assert https_client.get("/auth/login", params={"c": "Bad_ID"}).status_code == 404


def test_callback_sets_session_cookie_for_crm_user(https_client: TestClient, zoho_login: FakeLogin) -> None:
    state = start_login(https_client)
    res = https_client.get("/auth/callback", params={"code": "auth-code", "state": state})
    assert res.status_code == 302
    assert res.headers["location"] == "/app/?c=default"
    assert zoho_login.codes == ["auth-code"]
    cookies = set_cookies(res)
    morsel = cookies[SESSION_COOKIE][SESSION_COOKIE]
    assert morsel["httponly"] and morsel["secure"]
    session = verify_session(SESSION_SECRET.encode(), morsel.value, now=time.time())
    assert (session.client_id, session.user_id, session.name) == ("default", "9001", "金指 営業")
    assert session.expires_at > time.time() + 11 * 3600, "既定は12時間"


def test_callback_without_state_cookie_is_rejected(https_client: TestClient) -> None:
    state = start_login(https_client)
    https_client.cookies.clear()
    res = https_client.get("/auth/callback", params={"code": "auth-code", "state": state})
    assert res.headers["location"] == "/app/?c=default&login_error=expired"
    assert SESSION_COOKIE not in set_cookies(res)


def test_callback_when_user_cancels(https_client: TestClient, zoho_login: FakeLogin) -> None:
    state = start_login(https_client)
    res = https_client.get("/auth/callback", params={"error": "access_denied", "state": state})
    assert res.headers["location"] == "/app/?c=default&login_error=denied"
    assert zoho_login.codes == []


@pytest.mark.parametrize(
    ("user", "reason"),
    [
        (ZohoUser("9002", "他社", "x@other.example", "active", "999"), "not_crm_user"),
        (ZohoUser("9003", "退職者", "y@example.com", "deactive", ORG_ID), "inactive"),
    ],
)
def test_callback_rejects_other_org_and_inactive_users(
    https_client: TestClient, zoho_login: FakeLogin, user: ZohoUser, reason: str
) -> None:
    zoho_login.user = user
    state = start_login(https_client)
    res = https_client.get("/auth/callback", params={"code": "auth-code", "state": state})
    assert res.headers["location"] == f"/app/?c=default&login_error={reason}"
    assert SESSION_COOKIE not in set_cookies(res)


def test_callback_when_code_exchange_fails(https_client: TestClient, zoho_login: FakeLogin) -> None:
    zoho_login.exchange_error = ZohoLoginError("invalid_code")
    state = start_login(https_client)
    res = https_client.get("/auth/callback", params={"code": "used-code", "state": state})
    assert res.headers["location"] == "/app/?c=default&login_error=failed"


def test_logout_clears_session_cookie(https_client: TestClient) -> None:
    res = https_client.post("/auth/logout")
    assert res.status_code == 204
    morsel = set_cookies(res)[SESSION_COOKIE][SESSION_COOKIE]
    assert morsel.value == ""
    assert morsel["max-age"] == "0"


def test_callback_rejects_account_in_other_data_center(
    https_client: TestClient, zoho_login: FakeLogin
) -> None:
    state = start_login(https_client)
    res = https_client.get(
        "/auth/callback",
        params={
            "code": "auth-code",
            "state": state,
            "location": "eu",
            "accounts-server": "https://accounts.zoho.eu",
        },
    )
    assert res.headers["location"] == "/app/?c=default&login_error=not_crm_user"
    assert zoho_login.codes == [], "別の DC の認可コードは交換しない"


def test_callback_accepts_same_data_center(https_client: TestClient) -> None:
    state = start_login(https_client)
    res = https_client.get(
        "/auth/callback",
        params={
            "code": "auth-code",
            "state": state,
            "location": "us",
            "accounts-server": "https://accounts.zoho.com",
        },
    )
    assert res.headers["location"] == "/app/?c=default"
