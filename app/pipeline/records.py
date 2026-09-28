"""「商談記録」の読み取り・判定の小道具（書き込みは CrmService のガードを通る）。"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.errors import PermanentError
from app.field_map import FieldMap
from app.services.crm import CrmService

_RECALL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


def lookup_id(value: Any) -> str | None:
    if isinstance(value, dict):
        v = value.get("id")
        return str(v) if v else None
    return str(value) if isinstance(value, str | int) and value else None


def lookup_name(value: Any) -> str | None:
    if isinstance(value, dict):
        v = value.get("name")
        return v if isinstance(v, str) and v else None
    return None


def picklist_value(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("display_value") or value.get("actual_value")
    return value if isinstance(value, str) and value else None


def is_finished(record: dict[str, Any], fm: FieldMap) -> bool:
    """処理済み（完了・取引先未設定で全文が入っている）なら True。重複実行を止めるのに使う。"""
    f, s = fm.meeting_record, fm.status
    status = picklist_value(record.get(f.status))
    # COQL の結果には全文を含めない（大きい項目は取らない）ので、そのときは状態だけで判断する
    has_text = bool(record.get(f.transcript)) if f.transcript in record else True
    return status in (s.done, s.no_account) and has_text


async def find_by_recall_id(crm: CrmService, fm: FieldMap, recall_id: str) -> dict[str, Any] | None:
    """デスクトップ方式：recall_id（upload id）で商談記録を探す。"""
    if not _RECALL_ID_RE.match(recall_id):
        raise PermanentError("recall_id の形式が不正です")
    f = fm.meeting_record
    # recall_id は英数字・ハイフン・アンダースコアだけに制限済み（引用符を含められない）
    query = (
        f"select id, {f.status}, {f.account} from {f.module} "  # noqa: S608
        f"where {f.recall_id} = '{recall_id}' limit 1"
    )
    rows = await crm.coql(query)
    return rows[0] if rows else None


def meeting_date(value: Any, fallback: datetime | None, tz: str) -> date:
    zone = ZoneInfo(tz)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(zone).date()
        except ValueError:
            pass
    if fallback is not None:
        return fallback.astimezone(zone).date()
    return datetime.now(zone).date()
