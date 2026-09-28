"""入口C：デスクトップアプリ（Electron ＋ Recall.ai Desktop Recording SDK。アプリは Phase 2）。

- POST /api/desktop/upload-token … 会議の開始を検知したアプリが呼ぶ。Recall.ai の SDK アップロードを作りトークンを返す
- POST /api/desktop/candidates  … 参加者のメールアドレスから取引先・連絡先の候補を返す（CRM は読み取りのみ）
- POST /api/desktop/link        … 選んだ取引先で「商談記録」を作る／更新する（recall_id = upload id で1件にまとめる）

Phase 1 の認証は X-API-Key（Phase 2 で Zoho ログインに置き換える）。
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator

from app.deps import ApiClientDep, RuntimeDep
from app.logs import log_event
from app.pipeline import formatting as fmt
from app.pipeline.records import find_by_recall_id, lookup_id, meeting_date, picklist_value

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/desktop", tags=["desktop"])

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_DOMAIN_RE = re.compile(r"^[a-z0-9.-]{1,253}\.[a-z]{2,63}$")
# フリーメールは取引先の手がかりにならないので除く
FREE_MAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.co.jp",
        "yahoo.com",
        "ymail.ne.jp",
        "outlook.com",
        "outlook.jp",
        "hotmail.com",
        "hotmail.co.jp",
        "live.jp",
        "live.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "docomo.ne.jp",
        "ezweb.ne.jp",
        "au.com",
        "softbank.ne.jp",
        "i.softbank.jp",
        "nifty.com",
        "biglobe.ne.jp",
        "ocn.ne.jp",
    }
)
MAX_DOMAINS = 10


def _check_id(value: str | None) -> str | None:
    if value is not None and not _ID_RE.match(value):
        raise ValueError("ID の形式が不正です")
    return value


class UploadTokenRequest(BaseModel):
    owner_id: str | None = None

    @field_validator("owner_id")
    @classmethod
    def check_ids(cls, value: str | None) -> str | None:
        return _check_id(value)


class UploadTokenResponse(BaseModel):
    upload_id: str
    upload_token: str
    recall_api_url: str


@router.post("/upload-token", response_model=UploadTokenResponse)
async def upload_token(body: UploadTokenRequest, client: ApiClientDep, rt: RuntimeDep) -> UploadTokenResponse:
    cs = rt.client_services(client.client_id)
    cfg = client.need_recall()
    recall = await cs.recall()
    metadata = {"client_id": client.client_id}
    if body.owner_id:
        metadata["owner_id"] = body.owner_id
    upload = await recall.create_sdk_upload(
        recording_config=cfg.sdk_upload_recording_config or None, metadata=metadata
    )
    return UploadTokenResponse(
        upload_id=str(upload["id"]), upload_token=str(upload["upload_token"]), recall_api_url=cfg.base_url
    )


class CandidatesRequest(BaseModel):
    emails: Annotated[list[Annotated[str, Field(max_length=254)]], Field(max_length=100)]


class ContactCandidate(BaseModel):
    id: str
    name: str | None
    email: str | None


class AccountCandidate(BaseModel):
    id: str
    name: str | None
    contacts: list[ContactCandidate]


class CandidatesResponse(BaseModel):
    domains: list[str]
    accounts: list[AccountCandidate]
    contacts_without_account: list[ContactCandidate]


def candidate_domains(emails: list[str], own_domains: tuple[str, ...]) -> list[str]:
    own = {d.lower() for d in own_domains}
    domains: list[str] = []
    for email in emails:
        _, _, domain = email.strip().lower().rpartition("@")
        if not domain or not _DOMAIN_RE.match(domain) or domain in FREE_MAIL_DOMAINS or domain in own:
            continue
        if domain not in domains:
            domains.append(domain)
    return domains[:MAX_DOMAINS]


@router.post("/candidates", response_model=CandidatesResponse)
async def candidates(body: CandidatesRequest, client: ApiClientDep, rt: RuntimeDep) -> CandidatesResponse:
    cs = rt.client_services(client.client_id)
    std = cs.field_map.standard
    domains = candidate_domains(body.emails, client.own_email_domains)
    if not domains:
        return CandidatesResponse(domains=[], accounts=[], contacts_without_account=[])
    crm = await cs.crm()
    # ドメインは英小文字・数字・ドット・ハイフンだけに制限済み（引用符を含められない）
    conditions = " or ".join(f"{std.contact_email} like '%@{d}'" for d in domains)
    query = (
        f"select id, {std.contact_full_name}, {std.contact_email}, {std.contact_account}, "  # noqa: S608
        f"{std.contact_account}.{std.account_name} from {std.contacts_module} where ({conditions}) limit 200"
    )
    rows = await crm.coql(query)
    accounts: dict[str, AccountCandidate] = {}
    loose: list[ContactCandidate] = []
    for row in rows:
        contact = ContactCandidate(
            id=str(row.get("id")), name=row.get(std.contact_full_name), email=row.get(std.contact_email)
        )
        account_id = lookup_id(row.get(std.contact_account))
        if not account_id:
            loose.append(contact)
            continue
        name = row.get(f"{std.contact_account}.{std.account_name}")
        entry = accounts.setdefault(
            account_id,
            AccountCandidate(id=account_id, name=name if isinstance(name, str) else None, contacts=[]),
        )
        entry.contacts.append(contact)
    log_event(
        logger, "desktop.candidates", client_id=client.client_id, domains=len(domains), contacts=len(rows)
    )
    return CandidatesResponse(
        domains=domains, accounts=list(accounts.values()), contacts_without_account=loose
    )


class LinkRequest(BaseModel):
    upload_id: str
    account_id: str | None = None
    account_name: Annotated[str | None, Field(max_length=200)] = None
    deal_id: str | None = None
    contact_name: Annotated[str | None, Field(max_length=200)] = None
    owner_id: str | None = None
    start_at: datetime | None = None

    @field_validator("upload_id", "account_id", "deal_id", "owner_id")
    @classmethod
    def check_ids(cls, value: str | None) -> str | None:
        return _check_id(value)


class LinkResponse(BaseModel):
    record_id: str
    action: str


@router.post("/link", response_model=LinkResponse)
async def link(body: LinkRequest, client: ApiClientDep, rt: RuntimeDep) -> LinkResponse:
    cs = rt.client_services(client.client_id)
    fm = cs.field_map
    f, s = fm.meeting_record, fm.status
    crm = await cs.crm()
    fields: dict[str, Any] = {}
    if body.account_id:
        fields[f.account] = {"id": body.account_id}
    if body.deal_id:
        fields[f.deal] = {"id": body.deal_id}
    if body.contact_name:
        fields[f.contact_name] = fmt.clip(body.contact_name, fm.limits.contact_name)
    if body.owner_id:
        fields[f.owner] = {"id": body.owner_id}
    if body.start_at:
        fields[f.start_at] = body.start_at.astimezone(UTC).isoformat(timespec="seconds")

    existing = await find_by_recall_id(crm, fm, body.upload_id)
    if existing:
        record_id = str(lookup_id(existing.get("id")))
        if body.account_id and picklist_value(existing.get(f.status)) == s.no_account:
            fields[f.status] = s.done
        await crm.update_record(f.module, record_id, fields)
        action = "update"
    else:
        day = meeting_date(None, body.start_at, client.timezone)
        data = {
            **fields,
            f.name: fmt.record_name(day, body.account_name, fm),
            f.recall_id: body.upload_id,
            f.meeting_type: fm.meeting_type.online,
            f.capture_method: fm.capture_method.desktop,
            f.status: s.transcribing,
        }
        record_id, action = await crm.upsert_record(f.module, data, [f.recall_id])
    log_event(logger, "desktop.linked", client_id=client.client_id, record_id=record_id, action=action)
    return LinkResponse(record_id=record_id, action=action)
