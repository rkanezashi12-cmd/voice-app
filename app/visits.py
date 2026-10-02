"""録音アプリ（/app/）の訪問先の検索と、商談記録の作成・表示。

- CRM の顧客企業（Accounts）・連絡先（Contacts）は読み取りだけ（COQL）。新規顧客は CRM に作らず、名前だけ商談記録に残す
- 書き込みは「商談記録」の新規作成だけ（"trigger": [] なので CRM のワークフローは動かない）
- 検索語・位置・顧客名はログに出さない（件数・文字数だけ）

COQL の書き方（like・between・Owner の絞り込み）は docs/unverified-apis.md の確認項目。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from app.field_map import FieldMap
from app.pipeline import formatting as fmt
from app.pipeline.records import lookup_id, lookup_name, picklist_value
from app.services.crm import CrmService
from app.services.geocoding import Place

MAX_QUERY_CHARS = 40
SEARCH_LIMIT = 30
NEARBY_QUERY_LIMIT = 200
NEARBY_RESULTS = 20
CONTACTS_LIMIT = 100
VISITS_LIMIT = 50
ZOHO_ID_RE = re.compile(r"^[0-9]{1,30}$")
# COQL の文字列リテラルに入れない文字（引用符・バックスラッシュ・ワイルドカード・制御文字）
_UNSAFE_RE = re.compile(r"['\"\\%_\x00-\x1f\x7f]")
_SPACES_RE = re.compile(r"[\s\u3000]+")


def coql_group(op: str, conditions: list[str]) -> str:
    """COQL の条件をつなぐ。3つ以上のときは2つずつかっこでくくる（((A or B) or (C or D)) の形）。"""
    if len(conditions) == 1:
        return conditions[0]
    mid = (len(conditions) + 1) // 2
    left, right = coql_group(op, conditions[:mid]), coql_group(op, conditions[mid:])
    return f"({left} {op} {right})"


def clean_query(text: str) -> str:
    """検索語を COQL に安全に入れられる形にする（危険な文字は消す。空白は1つにまとめる）。"""
    cleaned = _SPACES_RE.sub(" ", _UNSAFE_RE.sub("", text or "")).strip()
    return cleaned[:MAX_QUERY_CHARS]


def address_of(row: dict[str, Any], fm: FieldMap) -> str:
    s = fm.standard
    parts = [row.get(s.account_state), row.get(s.account_city), row.get(s.account_street)]
    return "".join(str(p).strip() for p in parts if isinstance(p, str) and p.strip())


def contact_display_name(row: dict[str, Any], fm: FieldMap) -> str:
    s = fm.standard
    last = str(row.get(s.contact_last_name) or "").strip()
    first = str(row.get(s.contact_first_name) or "").strip()
    return f"{last} {first}".strip()


@dataclass(frozen=True)
class AccountHit:
    id: str
    name: str
    address: str
    reason: str
    score: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "address": self.address, "reason": self.reason}


def _account_select(fm: FieldMap) -> str:
    s = fm.standard
    return f"select id, {s.account_name}, {s.account_state}, {s.account_city}, {s.account_street} from {s.accounts_module}"


async def search_accounts(crm: CrmService, fm: FieldMap, query: str) -> list[AccountHit]:
    """会社名・住所・担当者名で顧客企業を探す。"""
    q = clean_query(query)
    if not q:
        return []
    s = fm.standard
    like = f"'%{q}%'"
    fields = (s.account_name, s.account_state, s.account_city, s.account_street)
    where = coql_group("or", [f"{field} like {like}" for field in fields])
    rows = await crm.coql(f"{_account_select(fm)} where {where} limit {SEARCH_LIMIT}")
    hits: dict[str, AccountHit] = {}
    for row in rows:
        account_id = str(row.get("id") or "")
        name = str(row.get(s.account_name) or "")
        if not account_id or not name:
            continue
        in_name = q.lower() in name.lower()
        hits[account_id] = AccountHit(account_id, name, address_of(row, fm), "会社名" if in_name else "住所")
    # 担当者名（姓・名のどちらか）で探し、その担当者の会社を足す。空白を含むときは最初の語で探す
    word = q.split(" ")[0]
    names = coql_group(
        "or", [f"{s.contact_last_name} like '%{word}%'", f"{s.contact_first_name} like '%{word}%'"]
    )
    where = coql_group("and", [names, f"{s.contact_account} is not null"])
    contact_rows = await crm.coql(
        f"select id, {s.contact_last_name}, {s.contact_first_name}, {s.contact_account} from {s.contacts_module} "
        f"where {where} limit {SEARCH_LIMIT}"
    )
    for row in contact_rows:
        account_id = lookup_id(row.get(s.contact_account))
        account_name = lookup_name(row.get(s.contact_account))
        if not account_id or not account_name or account_id in hits:
            continue
        hits[account_id] = AccountHit(
            account_id, account_name, "", f"担当者（{contact_display_name(row, fm)}）"
        )
    return list(hits.values())


def score_nearby(address: str, place: Place) -> tuple[int, str]:
    """住所が現在地とどれだけ近いか（同じ町 > 同じ区 > 同じ市区町村）。"""
    if place.town and place.town in address:
        return 3, f"同じ町（{place.town}）"
    if place.ward and place.ward in address:
        return 2, f"同じ区（{place.ward}）"
    return 1, f"同じ市区町村（{place.city}）"


async def nearby_accounts(crm: CrmService, fm: FieldMap, place: Place) -> list[AccountHit]:
    """現在地の市区町村にある顧客企業を、近い順（同じ町・同じ区を先）に並べる。"""
    city = clean_query(place.city or "")
    if not city:
        return []
    s = fm.standard
    like = f"'%{city}%'"
    where = coql_group("or", [f"{s.account_city} like {like}", f"{s.account_street} like {like}"])
    rows = await crm.coql(f"{_account_select(fm)} where {where} limit {NEARBY_QUERY_LIMIT}")
    hits: list[AccountHit] = []
    for row in rows:
        account_id = str(row.get("id") or "")
        name = str(row.get(s.account_name) or "")
        if not account_id or not name:
            continue
        address = address_of(row, fm)
        if place.prefecture and row.get(s.account_state) and place.prefecture not in address:
            continue  # 同じ名前の市が別の都道府県にある（府中市など）
        score, reason = score_nearby(address, place)
        hits.append(AccountHit(account_id, name, address, reason, score))
    hits.sort(key=lambda h: (-h.score, h.name))
    return hits[:NEARBY_RESULTS]


async def account_contacts(crm: CrmService, fm: FieldMap, account_id: str) -> list[dict[str, str]]:
    if not ZOHO_ID_RE.match(account_id):
        return []
    s = fm.standard
    rows = await crm.coql(
        f"select id, {s.contact_last_name}, {s.contact_first_name}, {s.contact_department}, {s.contact_title} "
        f"from {s.contacts_module} where {s.contact_account} = '{account_id}' limit {CONTACTS_LIMIT}"
    )
    contacts = []
    for row in rows:
        name = contact_display_name(row, fm)
        if not row.get("id") or not name:
            continue
        detail = "・".join(
            str(row.get(k)).strip()
            for k in (s.contact_department, s.contact_title)
            if isinstance(row.get(k), str) and str(row.get(k)).strip()
        )
        contacts.append({"id": str(row["id"]), "name": name, "detail": detail})
    contacts.sort(key=lambda c: c["name"])
    return contacts


async def find_contacts_link_field(crm: CrmService, fm: FieldMap) -> str | None:
    """先方担当者（連絡先。複数選択ルックアップ）に書くときに使う、中間モジュールの「連絡先」のルックアップ項目の API 名。

    書く形は {"<複数選択ルックアップ>": [{"<中間モジュールの連絡先のルックアップ>": {"id": …}}]}。
    中間モジュールの名前は Zoho が決めるので、商談記録と連絡先の両方を参照している中間モジュールを探す。
    項目がまだ無い（scripts/crm_setup.py を実行していない）ときは None。
    """
    f, s = fm.meeting_record, fm.standard
    fields = await crm.list_fields(f.module)
    if not any(
        x.get("api_name") == f.contacts_link and x.get("data_type") == "multiselectlookup" for x in fields
    ):
        return None
    for module in await crm.list_modules():
        api_name = module.get("api_name")
        if module.get("generated_type") != "linking" or not isinstance(api_name, str):
            continue
        lookups: dict[str, str] = {}
        for x in await crm.list_fields(api_name):
            target = ((x.get("lookup") or {}).get("module") or {}).get("api_name")
            if (
                x.get("data_type") == "lookup"
                and isinstance(target, str)
                and isinstance(x.get("api_name"), str)
            ):
                lookups[target] = x["api_name"]
        if f.module in lookups and s.contacts_module in lookups:
            return lookups[s.contacts_module]
    return None


@dataclass(frozen=True)
class NewVisit:
    owner_id: str
    account_id: str | None
    account_name: str
    new_customer: bool
    contacts: tuple[str, ...]
    # 選んだ担当者のうち CRM の連絡先の ID（訪問先の連絡先だと確かめたもの）
    contact_ids: tuple[str, ...] = ()


def visit_record(
    visit: NewVisit, fm: FieldMap, now: datetime, *, link_field: str | None = None
) -> dict[str, Any]:
    """作る商談記録の中身。新規顧客は取引先を空のまま名前だけ残す（処理が終わると状態は「取引先未設定」）。

    link_field（中間モジュールの連絡先のルックアップ）が分かっていれば、選んだ連絡先を先方担当者（連絡先）に紐づける。
    """
    f = fm.meeting_record
    label = f"{visit.account_name}{'（新規）' if visit.new_customer else ''} 訪問"
    data: dict[str, Any] = {
        f.name: fmt.record_name(now.date(), label, fm),
        f.owner: {"id": visit.owner_id},
        f.meeting_type: fm.meeting_type.in_person,
        f.capture_method: fm.capture_method.in_person,
        f.start_at: now.replace(microsecond=0).isoformat(),
    }
    if visit.contacts:
        data[f.contact_name] = fmt.clip("、".join(visit.contacts), fm.limits.contact_name)
    if visit.account_id and not visit.new_customer:
        data[f.account] = {"id": visit.account_id}
        if link_field and visit.contact_ids:
            data[f.contacts_link] = [{link_field: {"id": cid}} for cid in visit.contact_ids]
    return data


async def list_visits(crm: CrmService, fm: FieldMap, owner_id: str, day: datetime) -> list[dict[str, Any]]:
    """その日にその人が作った商談記録（新しい順）。"""
    if not ZOHO_ID_RE.match(owner_id):
        return []
    f = fm.meeting_record
    start = datetime.combine(day.date(), time.min, tzinfo=day.tzinfo)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    where = coql_group(
        "and",
        [f"{f.owner} = '{owner_id}'", f"{f.start_at} between '{start.isoformat()}' and '{end.isoformat()}'"],
    )
    rows = await crm.coql(
        f"select id, {f.name}, {f.status}, {f.account}, {f.contact_name}, {f.start_at} from {f.module} "
        f"where {where} order by {f.start_at} desc limit {VISITS_LIMIT}"
    )
    return [visit_summary(row, fm) for row in rows if row.get("id")]


def _status_kind(status: str | None, fm: FieldMap) -> str:
    s = fm.status
    if status in (s.done, s.no_account):
        return "done"
    if status in (s.failed, s.join_failed):
        return "failed"
    if status == s.transcribing:
        return "processing"
    return "waiting"


def visit_summary(record: dict[str, Any], fm: FieldMap) -> dict[str, Any]:
    f = fm.meeting_record
    status = picklist_value(record.get(f.status))
    return {
        "id": str(record.get("id")),
        "name": str(record.get(f.name) or ""),
        "status": status or "",
        "state": _status_kind(status, fm),
        "account": lookup_name(record.get(f.account)) or "",
        "contacts": str(record.get(f.contact_name) or ""),
        "start_at": record.get(f.start_at),
    }


def visit_detail(record: dict[str, Any], fm: FieldMap) -> dict[str, Any]:
    """日報の画面に出す内容（文字起こし全文は出さない。全文は CRM で見る）。"""
    f = fm.meeting_record
    detail = visit_summary(record, fm)
    for key, field in (
        ("summary", f.summary),
        ("issues", f.issues),
        ("needs", f.needs),
        ("next_actions", f.next_actions),
        ("due_date", f.due_date),
        ("budget", f.budget),
        ("decision_maker", f.decision_maker),
        ("competitors", f.competitors),
        ("error_message", f.error_message),
    ):
        value = record.get(field)
        detail[key] = value if isinstance(value, str) else None
    return detail
