from __future__ import annotations

from fastapi.testclient import TestClient

from app.routers.desktop import candidate_domains
from tests.conftest import API_KEY, F, FakeCrm, FakeRecall, S

HEADERS = {"X-API-Key": API_KEY}


def test_upload_token(client: TestClient, recall: FakeRecall) -> None:
    res = client.post("/api/desktop/upload-token", json={"owner_id": "9001"}, headers=HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert body == {
        "upload_id": "upload-1",
        "upload_token": "sdk-upload-token",
        "recall_api_url": "https://ap-northeast-1.recall.ai",
    }
    assert recall.uploads["upload-1"]["metadata"] == {"client_id": "default", "owner_id": "9001"}


def test_candidate_domains_skip_own_and_free_mail() -> None:
    emails = [
        "taro@customer.co.jp",
        "hanako@CUSTOMER.co.jp",
        "me@own.example.co.jp",
        "someone@gmail.com",
        "bad-address",
        "x@partner.com",
    ]
    assert candidate_domains(emails, ("own.example.co.jp",)) == ["customer.co.jp", "partner.com"]


def test_candidates_groups_contacts_by_account(client: TestClient, crm: FakeCrm) -> None:
    crm.coql_rows = [
        {"id": "c1", "Full_Name": "山田 太郎", "Email": "taro@customer.co.jp", "Account_Name": {"id": "a1"},
         "Account_Name.Account_Name": "カスタマー株式会社"},
        {"id": "c2", "Full_Name": "佐藤 花子", "Email": "hanako@customer.co.jp", "Account_Name": {"id": "a1"},
         "Account_Name.Account_Name": "カスタマー株式会社"},
        {"id": "c3", "Full_Name": "個人 様", "Email": "p@partner.com", "Account_Name": None},
    ]  # fmt: skip
    res = client.post(
        "/api/desktop/candidates",
        json={"emails": ["taro@customer.co.jp", "p@partner.com", "me@own.example.co.jp"]},
        headers=HEADERS,
    )
    body = res.json()
    assert body["domains"] == ["customer.co.jp", "partner.com"]
    assert body["accounts"][0]["name"] == "カスタマー株式会社"
    assert [c["id"] for c in body["accounts"][0]["contacts"]] == ["c1", "c2"]
    assert [c["id"] for c in body["contacts_without_account"]] == ["c3"]
    query = crm.coql_queries[0]
    assert "from Contacts where" in query
    assert "Email like '%@customer.co.jp' or Email like '%@partner.com'" in query
    assert crm.writes == [], "候補の検索では CRM に書き込まない"


def test_candidates_without_usable_domains(client: TestClient, crm: FakeCrm) -> None:
    res = client.post("/api/desktop/candidates", json={"emails": ["a@gmail.com"]}, headers=HEADERS)
    assert res.json()["accounts"] == []
    assert crm.coql_queries == []


def test_link_creates_record_when_none_exists(client: TestClient, crm: FakeCrm) -> None:
    res = client.post(
        "/api/desktop/link",
        json={
            "upload_id": "upload-9",
            "account_id": "a1",
            "account_name": "カスタマー株式会社",
            "contact_name": "山田 太郎",
            "start_at": "2026-09-28T10:00:00+09:00",
        },
        headers=HEADERS,
    )
    assert res.status_code == 200
    assert res.json()["action"] == "insert"
    kind, module, _record_id, data = crm.writes[0]
    assert (kind, module) == ("upsert", F.module)
    assert data[F.recall_id] == "upload-9"
    assert data[F.account] == {"id": "a1"}
    assert data[F.status] == S.transcribing
    assert data[F.name] == "2026-09-28 カスタマー株式会社"
    assert data[F.capture_method] == "デスクトップ"


def test_link_updates_existing_record_and_completes_it(client: TestClient, crm: FakeCrm) -> None:
    crm.add(
        F.module, "7001", {F.recall_id: "upload-9", F.status: S.no_account, F.name: "【TEST】2026-09-28 x"}
    )
    res = client.post(
        "/api/desktop/link", json={"upload_id": "upload-9", "account_id": "a1"}, headers=HEADERS
    )
    assert res.json() == {"record_id": "7001", "action": "update"}
    write = crm.writes_to("7001")[0]
    assert write == {F.account: {"id": "a1"}, F.status: S.done}
    assert F.name not in write, "名前は変えない"


def test_link_rejects_injection_in_ids(client: TestClient, crm: FakeCrm) -> None:
    res = client.post("/api/desktop/link", json={"upload_id": "x' or 1=1 --"}, headers=HEADERS)
    assert res.status_code == 422
    assert crm.coql_queries == []
