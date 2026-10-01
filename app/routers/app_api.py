"""録音アプリ（/app/）の API。Zoho でログインした営業だけが使える（Cookie。app/deps.py の AppUserDep）。

- GET  /api/app/me                               … ログイン中の人と、GPS の候補を使えるか
- POST /api/app/accounts/search                  … 会社名・住所・担当者名で顧客企業を探す（検索語は本文で受け、URL に残さない）
- POST /api/app/accounts/nearby                  … 現在地（緯度・経度）から近くの顧客企業の候補（位置は本文で受ける）
- GET  /api/app/accounts/{account_id}/contacts   … 顧客企業の担当者（連絡先）
- POST /api/app/visits                           … 商談記録を作り、録音ページの URL を返す
- GET  /api/app/visits                           … 今日の自分の商談記録
- GET  /api/app/visits/{record_id}               … 日報（処理の状態と要約・構造化項目）。自分の記録だけ
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from app import visits
from app.deps import AppUserDep, RuntimeDep
from app.errors import ConfigError
from app.logs import log_event
from app.pipeline.records import lookup_id
from app.recording_token import RECORD_ID_RE, compute_expiry, issue

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/app", tags=["app"])

MAX_CONTACTS = 20


@router.get("/me")
async def me(user: AppUserDep, rt: RuntimeDep) -> dict[str, Any]:
    config = rt.client_services(user.client_id).config
    return {
        "client_id": user.client_id,
        "client_name": config.display_name,
        "user": {"id": user.user_id, "name": user.name, "email": user.email},
        "gps_available": bool(config.app and config.app.maps_api_key),
        "dry_run": rt.settings.dry_run,
    }


class SearchRequest(BaseModel):
    q: Annotated[str, Field(min_length=1, max_length=100)]


@router.post("/accounts/search")
async def search(body: SearchRequest, user: AppUserDep, rt: RuntimeDep) -> dict[str, Any]:
    cs = rt.client_services(user.client_id)
    hits = await visits.search_accounts(await cs.crm(), cs.field_map, body.q)
    log_event(logger, "app.searched", client_id=user.client_id, query_chars=len(body.q), results=len(hits))
    return {"accounts": [h.as_dict() for h in hits]}


class NearbyRequest(BaseModel):
    lat: Annotated[float, Field(ge=-90, le=90)]
    lng: Annotated[float, Field(ge=-180, le=180)]


@router.post("/accounts/nearby")
async def nearby(body: NearbyRequest, user: AppUserDep, rt: RuntimeDep) -> dict[str, Any]:
    cs = rt.client_services(user.client_id)
    geocoder = await cs.geocoder()
    if geocoder is None:
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED, "GPS の候補は設定されていません（地図のキーが未設定）"
        )
    place = await geocoder.reverse(body.lat, body.lng)
    hits = await visits.nearby_accounts(await cs.crm(), cs.field_map, place) if place.city else []
    log_event(logger, "app.nearby", client_id=user.client_id, found_place=bool(place.city), results=len(hits))
    return {"place": place.label, "accounts": [h.as_dict() for h in hits]}


@router.get("/accounts/{account_id}/contacts")
async def contacts(account_id: str, user: AppUserDep, rt: RuntimeDep) -> dict[str, Any]:
    if not visits.ZOHO_ID_RE.match(account_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "顧客企業が見つかりません")
    cs = rt.client_services(user.client_id)
    found = await visits.account_contacts(await cs.crm(), cs.field_map, account_id)
    return {"contacts": found}


class VisitRequest(BaseModel):
    # 既存の顧客企業を選んだときの ID。新規顧客なら空
    account_id: str | None = None
    # 新規顧客の会社名（既存の顧客企業を選んだときは CRM の名前を使う）
    account_name: Annotated[str, Field(max_length=100)] = ""
    new_customer: bool = False
    # 選んだ担当者の名前（CRM の連絡先の名前と、手で足した名前）
    contacts: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=60)]], Field(max_length=MAX_CONTACTS)
    ] = []

    @field_validator("account_id")
    @classmethod
    def check_account_id(cls, value: str | None) -> str | None:
        if value is not None and not visits.ZOHO_ID_RE.match(value):
            raise ValueError("account_id の形式が不正です")
        return value


@router.post("/visits")
async def create_visit(body: VisitRequest, user: AppUserDep, rt: RuntimeDep) -> dict[str, Any]:
    cs = rt.client_services(user.client_id)
    fm = cs.field_map
    crm = await cs.crm()
    account_name = body.account_name.strip()
    if body.new_customer:
        if not account_name:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "新規顧客の会社名を入れてください")
        account_id = None
    else:
        if not body.account_id:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "訪問先を選んでください")
        account = await crm.get_record(fm.standard.accounts_module, body.account_id)
        if account is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "顧客企業が見つかりません")
        account_id = body.account_id
        account_name = str(account.get(fm.standard.account_name) or account_name)
    names = tuple(dict.fromkeys(n.strip() for n in body.contacts if n.strip()))
    tz = ZoneInfo(cs.config.timezone)
    visit = visits.NewVisit(
        owner_id=user.user_id,
        account_id=account_id,
        account_name=account_name,
        new_customer=body.new_customer,
        contacts=names,
    )
    record_id = await crm.create_record(
        fm.meeting_record.module, visits.visit_record(visit, fm, datetime.now(tz))
    )
    if not RECORD_ID_RE.match(record_id):
        raise ConfigError("商談記録の ID を受け取れませんでした")
    expires_at = compute_expiry(now=time.time(), start_at=None, ttl_hours=rt.settings.recording_url_ttl_hours)
    # DRY_RUN では CRM に記録を作らないので、録音ページはテスト（処理しない）で開く
    token = issue(
        await rt.recording_secret(),
        client_id=user.client_id,
        record_id=record_id,
        expires_at=expires_at,
        test=crm.dry_run,
    )
    log_event(
        logger,
        "app.visit_created",
        client_id=user.client_id,
        user_id=user.user_id,
        record_id=record_id,
        new_customer=body.new_customer,
        contacts=len(names),
        dry_run=crm.dry_run,
    )
    return {"record_id": record_id, "recording_url": f"/recorder/#t={token}&app=1", "test": crm.dry_run}


@router.get("/visits")
async def todays_visits(user: AppUserDep, rt: RuntimeDep) -> dict[str, Any]:
    cs = rt.client_services(user.client_id)
    now = datetime.now(ZoneInfo(cs.config.timezone))
    found = await visits.list_visits(await cs.crm(), cs.field_map, user.user_id, now)
    return {"date": now.date().isoformat(), "visits": found}


@router.get("/visits/{record_id}")
async def visit_detail(record_id: str, user: AppUserDep, rt: RuntimeDep) -> dict[str, Any]:
    if not visits.ZOHO_ID_RE.match(record_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "商談記録が見つかりません")
    cs = rt.client_services(user.client_id)
    fm = cs.field_map
    record = await (await cs.crm()).get_record(fm.meeting_record.module, record_id)
    # 自分の記録だけ見せる（ほかの人の記録は「無い」と同じ応答にする）
    if record is None or lookup_id(record.get(fm.meeting_record.owner)) != user.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "商談記録が見つかりません")
    return visits.visit_detail(record, fm)
