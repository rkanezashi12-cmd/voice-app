from __future__ import annotations

import time

import pytest

from app.recording_token import TokenError, compute_expiry, issue, verify


def test_issue_and_verify_roundtrip() -> None:
    token = issue(
        b"k", client_id="default", record_id="5725767000001234567", expires_at=int(time.time()) + 60
    )
    claims = verify(b"k", token)
    assert claims.client_id == "default"
    assert claims.record_id == "5725767000001234567"
    assert claims.test is False


def test_test_flag() -> None:
    token = issue(b"k", client_id="default", record_id="test-1", expires_at=int(time.time()) + 60, test=True)
    assert verify(b"k", token).test is True


def test_rejects_wrong_secret() -> None:
    token = issue(b"k", client_id="default", record_id="r1", expires_at=int(time.time()) + 60)
    with pytest.raises(TokenError):
        verify(b"other", token)


def test_rejects_tampered_payload() -> None:
    token = issue(b"k", client_id="default", record_id="r1", expires_at=int(time.time()) + 60)
    body, sig = token.split(".")
    forged = issue(b"x", client_id="default", record_id="r2", expires_at=int(time.time()) + 60).split(".")[0]
    with pytest.raises(TokenError):
        verify(b"k", f"{forged}.{sig}")
    with pytest.raises(TokenError):
        verify(b"k", body)


def test_rejects_expired() -> None:
    token = issue(b"k", client_id="default", record_id="r1", expires_at=1000)
    with pytest.raises(TokenError, match="有効期限"):
        verify(b"k", token, now=1000)


@pytest.mark.parametrize("record_id", ["", "a/b", "../x", "x" * 65])
def test_rejects_bad_record_id(record_id: str) -> None:
    with pytest.raises(ValueError):
        issue(b"k", client_id="default", record_id=record_id, expires_at=2000)


def test_expiry_uses_later_of_now_and_start() -> None:
    # 前日に作った記録でも、商談開始から24時間は使える
    assert compute_expiry(now=1000, start_at=90000, ttl_hours=24) == 90000 + 86400
    # 開始済み・開始日時なしなら発行から24時間
    assert compute_expiry(now=1000, start_at=500, ttl_hours=24) == 1000 + 86400
    assert compute_expiry(now=1000, start_at=None, ttl_hours=24) == 1000 + 86400


def test_url_fits_crm_url_field() -> None:
    token = issue(b"k" * 32, client_id="default", record_id="5725767000001234567", expires_at=1893456000)
    url = f"https://meeting-notes-abcdefghij-an.a.run.app/recorder/#{token}"
    assert len(url) <= 255
