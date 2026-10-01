"""録音アプリの API（/api/app/*）：ログインの確認・検索・GPS の候補・担当者・商談記録の作成と日報。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.deps import APP_REQUEST_HEADER, APP_REQUEST_VALUE
from app.recording_token import verify
from app.runtime import Runtime
from app.services.geocoding import Place
from app.visits import coql_group
from app.web_session import SESSION_COOKIE
from tests.conftest import TOKEN_SECRET, F, FakeCrm, FakeGeocoder, app_session_cookie

WRITE_HEADERS = {APP_REQUEST_HEADER: APP_REQUEST_VALUE}


@pytest.fixture
def logged_in(client: TestClient) -> TestClient:
    client.cookies.set(SESSION_COOKIE, app_session_cookie())
    return client


def account_row(account_id: str, name: str, state: str, city: str, street: str) -> dict[str, Any]:
    return {
        "id": account_id,
        "Account_Name": name,
        "Billing_State": state,
        "Billing_City": city,
        "Billing_Street": street,
    }


def test_me_requires_login(client: TestClient) -> None:
    assert client.get("/api/app/me").status_code == 401
    client.cookies.set(SESSION_COOKIE, app_session_cookie(ttl=-10))
    assert client.get("/api/app/me").status_code == 401, "期限切れの Cookie は使えない"
    client.cookies.set(SESSION_COOKIE, "tampered.value")
    assert client.get("/api/app/me").status_code == 401


def test_me_returns_logged_in_user(logged_in: TestClient) -> None:
    res = logged_in.get("/api/app/me")
    assert res.status_code == 200
    body = res.json()
    assert body["user"] == {"id": "9001", "name": "金指 営業", "email": "sales@example.com"}
    assert body["gps_available"] is True
    assert "geolocation=(self)" in res.headers["permissions-policy"]


def test_writes_need_the_app_header(logged_in: TestClient) -> None:
    """別のサイトのページから Cookie を使って送られる書き込み（CSRF）を拒否する。"""
    res = logged_in.post("/api/app/accounts/search", json={"q": "サンプル"})
    assert res.status_code == 403


def test_search_by_name_address_and_contact(logged_in: TestClient, crm: FakeCrm) -> None:
    def coql(query: str) -> list[dict[str, Any]]:
        if "from Accounts" in query:
            return [account_row("3001", "株式会社サンプル鋳造", "神奈川県", "横浜市中区", "山下町1-2")]
        return [
            {
                "id": "4001",
                "Last_Name": "田中",
                "First_Name": "太郎",
                "Account_Name": {"name": "港南精機", "id": "3002"},
            },
            {
                "id": "4002",
                "Last_Name": "田中",
                "First_Name": "花子",
                "Account_Name": {"name": "株式会社サンプル鋳造", "id": "3001"},
            },
        ]

    crm.coql_handler = coql
    res = logged_in.post("/api/app/accounts/search", json={"q": "サンプル"}, headers=WRITE_HEADERS)
    assert res.status_code == 200
    assert res.json()["accounts"] == [
        {
            "id": "3001",
            "name": "株式会社サンプル鋳造",
            "address": "神奈川県横浜市中区山下町1-2",
            "reason": "会社名",
        },
        {"id": "3002", "name": "港南精機", "address": "", "reason": "担当者（田中 太郎）"},
    ]
    assert crm.writes == [], "検索は読み取りだけ"


def test_search_words_are_cleaned_before_coql(logged_in: TestClient, crm: FakeCrm) -> None:
    crm.coql_handler = lambda query: []
    logged_in.post("/api/app/accounts/search", json={"q": "A'社% \\_x"}, headers=WRITE_HEADERS)
    assert crm.coql_queries, "COQL を実行する"
    for query in crm.coql_queries:
        assert "'%A社 x%'" in query or "'%A社%'" in query
        assert "A'社" not in query and "\\" not in query


def test_nearby_ranks_same_town_then_ward_then_city(
    logged_in: TestClient, crm: FakeCrm, geocoder: FakeGeocoder
) -> None:
    crm.coql_handler = lambda query: [
        account_row("1", "Ｃ市内の会社", "神奈川県", "横浜市西区", "みなとみらい1"),
        account_row("2", "Ｂ区内の会社", "神奈川県", "横浜市中区", "本町3"),
        account_row("3", "Ａ同じ町の会社", "神奈川県", "横浜市中区", "山下町1-2"),
        account_row("4", "別の県の会社", "東京都", "横浜市", "架空"),
    ]
    res = logged_in.post(
        "/api/app/accounts/nearby", json={"lat": 35.4437, "lng": 139.638}, headers=WRITE_HEADERS
    )
    assert res.status_code == 200
    body = res.json()
    assert body["place"] == "神奈川県横浜市中区山下町"
    assert [(a["id"], a["reason"]) for a in body["accounts"]] == [
        ("3", "同じ町（山下町）"),
        ("2", "同じ区（中区）"),
        ("1", "同じ市区町村（横浜市）"),
    ]
    assert geocoder.calls == [(35.4437, 139.638)]
    assert "'%横浜市%'" in crm.coql_queries[0]


def test_nearby_without_city_returns_nothing(
    logged_in: TestClient, crm: FakeCrm, geocoder: FakeGeocoder
) -> None:
    geocoder.place = Place()
    res = logged_in.post("/api/app/accounts/nearby", json={"lat": 0, "lng": 0}, headers=WRITE_HEADERS)
    assert res.json() == {"place": "", "accounts": []}
    assert crm.coql_queries == []


def test_nearby_without_maps_key_is_501(logged_in: TestClient, runtime: Runtime) -> None:
    runtime.client_services("default").geo = None
    res = logged_in.post("/api/app/accounts/nearby", json={"lat": 35.0, "lng": 139.0}, headers=WRITE_HEADERS)
    assert res.status_code == 501


def test_contacts_of_account(logged_in: TestClient, crm: FakeCrm) -> None:
    crm.coql_handler = lambda query: [
        {"id": "4002", "Last_Name": "山本", "First_Name": "次郎", "Department": None, "Title": "工場長"},
        {"id": "4001", "Last_Name": "田中", "First_Name": "太郎", "Department": "製造部", "Title": "課長"},
        {"id": "4003", "Last_Name": "", "First_Name": ""},
    ]
    res = logged_in.get("/api/app/accounts/3001/contacts")
    assert res.json() == {
        "contacts": [
            {"id": "4002", "name": "山本 次郎", "detail": "工場長"},
            {"id": "4001", "name": "田中 太郎", "detail": "製造部・課長"},
        ]
    }
    assert "Account_Name = '3001'" in crm.coql_queries[0]
    assert logged_in.get("/api/app/accounts/x'1/contacts").status_code == 404


def test_create_visit_for_existing_account(logged_in: TestClient, crm: FakeCrm) -> None:
    crm.add("Accounts", "3001", {"Account_Name": "株式会社サンプル鋳造"})
    res = logged_in.post(
        "/api/app/visits",
        json={
            "account_id": "3001",
            "account_name": "書き換えられた名前",
            "contacts": ["田中 太郎", "田中 太郎", "山本 次郎"],
        },
        headers=WRITE_HEADERS,
    )
    assert res.status_code == 200
    body = res.json()
    assert body["record_id"] == "new-1"
    kind, module, _, data = crm.writes[0]
    assert (kind, module) == ("create", F.module)
    assert len(crm.writes) == 1, "商談記録の作成1回だけ"
    assert data[F.owner] == {"id": "9001"}
    assert data[F.account] == {"id": "3001"}
    assert data[F.contact_name] == "田中 太郎、山本 次郎"
    assert data[F.capture_method] == "対面録音"
    assert data[F.meeting_type] == "対面"
    assert data[F.name].endswith("株式会社サンプル鋳造 訪問"), "名前は CRM の顧客企業の名前を使う"
    assert data[F.start_at].endswith("+09:00")
    url = body["recording_url"]
    assert url.startswith("/recorder/#t=") and url.endswith("&app=1")
    claims = verify(TOKEN_SECRET.encode(), url.split("#t=")[1].split("&")[0])
    assert (claims.record_id, claims.client_id, claims.test) == ("new-1", "default", False)


def test_create_visit_for_new_customer_keeps_name_only(logged_in: TestClient, crm: FakeCrm) -> None:
    res = logged_in.post(
        "/api/app/visits",
        json={"account_name": " 有限会社みなと鋳物 ", "new_customer": True, "contacts": ["佐藤 花子"]},
        headers=WRITE_HEADERS,
    )
    assert res.status_code == 200
    _, module, _, data = crm.writes[0]
    assert module == F.module, "新規顧客でも CRM の顧客企業は作らない"
    assert F.account not in data
    today = datetime.now(ZoneInfo("Asia/Tokyo")).date().isoformat()
    assert data[F.name] == f"{today} 有限会社みなと鋳物（新規） 訪問"


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"new_customer": True, "account_name": " "}, 422),
        ({"account_name": "x"}, 422),
        ({"account_id": "9999"}, 404),
        ({"account_id": "1' or '1"}, 422),
    ],
)
def test_create_visit_validation(
    logged_in: TestClient, crm: FakeCrm, body: dict[str, Any], status: int
) -> None:
    assert logged_in.post("/api/app/visits", json=body, headers=WRITE_HEADERS).status_code == status
    assert crm.writes == []


def test_create_visit_in_dry_run_opens_test_recorder(logged_in: TestClient, crm: FakeCrm) -> None:
    crm.dry_run = True
    res = logged_in.post(
        "/api/app/visits", json={"account_name": "新規", "new_customer": True}, headers=WRITE_HEADERS
    )
    assert res.json()["test"] is True
    claims = verify(TOKEN_SECRET.encode(), res.json()["recording_url"].split("#t=")[1].split("&")[0])
    assert claims.test is True, "DRY_RUN では処理しないテストの録音にする"


def test_todays_visits_are_only_mine(logged_in: TestClient, crm: FakeCrm) -> None:
    crm.coql_handler = lambda query: [
        {
            "id": "5001",
            F.name: "2026-10-01 株式会社サンプル鋳造 訪問",
            F.status: "完了",
            F.account: {"name": "株式会社サンプル鋳造", "id": "3001"},
            F.contact_name: "田中 太郎",
            F.start_at: "2026-10-01T10:05:00+09:00",
        },
        {
            "id": "5002",
            F.name: "訪問",
            F.status: None,
            F.account: None,
            F.contact_name: None,
            F.start_at: None,
        },
    ]
    res = logged_in.get("/api/app/visits")
    visits = res.json()["visits"]
    assert [(v["id"], v["state"], v["account"]) for v in visits] == [
        ("5001", "done", "株式会社サンプル鋳造"),
        ("5002", "waiting", ""),
    ]
    query = crm.coql_queries[0]
    assert f"from {F.module} " in query
    assert "Owner = '9001'" in query and " between " in query


def test_visit_detail_for_owner_only(logged_in: TestClient, crm: FakeCrm) -> None:
    crm.add(
        F.module,
        "5001",
        {
            F.owner: {"id": "9001", "name": "金指 営業"},
            F.status: "文字起こし中",
            F.summary: "要約です",
            F.transcript: "全文は返さない",
        },
    )
    crm.add(F.module, "5002", {F.owner: {"id": "9999"}, F.status: "完了"})
    res = logged_in.get("/api/app/visits/5001")
    assert res.status_code == 200
    body = res.json()
    assert (body["state"], body["summary"]) == ("processing", "要約です")
    assert "transcript" not in body
    assert logged_in.get("/api/app/visits/5002").status_code == 404, "ほかの人の記録は見せない"
    assert logged_in.get("/api/app/visits/7777").status_code == 404


def test_coql_conditions_are_grouped_in_pairs() -> None:
    """COQL は3つ以上の条件を2つずつかっこでくくる（((A or B) or (C or D))）。"""
    assert coql_group("or", ["A"]) == "A"
    assert coql_group("or", ["A", "B"]) == "(A or B)"
    assert coql_group("or", ["A", "B", "C"]) == "((A or B) or C)"
    assert coql_group("or", ["A", "B", "C", "D"]) == "((A or B) or (C or D))"
    assert coql_group("and", ["(A or B)", "C"]) == "((A or B) and C)"


def test_search_query_shape(logged_in: TestClient, crm: FakeCrm) -> None:
    crm.coql_handler = lambda query: []
    logged_in.post("/api/app/accounts/search", json={"q": "港南"}, headers=WRITE_HEADERS)
    accounts, contacts = crm.coql_queries
    assert accounts.endswith(
        "from Accounts where ((Account_Name like '%港南%' or Billing_State like '%港南%') "
        "or (Billing_City like '%港南%' or Billing_Street like '%港南%')) limit 30"
    )
    assert contacts.endswith(
        "from Contacts where ((Last_Name like '%港南%' or First_Name like '%港南%') "
        "and Account_Name is not null) limit 30"
    )
