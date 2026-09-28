"""Zoho CRM v8。

お客様の本番 CRM に接続するため、次を守る。
- 書き込み（作成・更新・upsert）はカスタムモジュール「商談記録」「用語辞書」だけ。
  それ以外（取引先・連絡先・商談などの標準モジュール）への書き込みは、送信前にエラーにする。
- 削除の処理は持たない。
- DRY_RUN のときは書き込みを送らず、どの項目を書く予定だったか（項目名だけ）をログに出す。
- 作成するレコードの名前には、テスト中（CRM_TEST_RECORDS=true）は先頭に【TEST】を付ける。
- 更新は "trigger": [] でワークフローを再発火させない。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.errors import ConfigError, ExternalServiceError
from app.field_map import FieldMap
from app.logs import log_event
from app.services.http import error_from_response, send
from app.services.zoho_auth import ZohoAuth

logger = logging.getLogger(__name__)

TEST_PREFIX = "【TEST】"

# 書き込みを絶対に許さない標準モジュール（field_map の設定ミスでも書き込まないための二重の守り）
STANDARD_MODULES = frozenset(
    {
        "Leads",
        "Contacts",
        "Accounts",
        "Deals",
        "Potentials",
        "Tasks",
        "Events",
        "Calls",
        "Meetings",
        "Products",
        "Quotes",
        "Sales_Orders",
        "Purchase_Orders",
        "Invoices",
        "Vendors",
        "Campaigns",
        "Cases",
        "Solutions",
        "Price_Books",
        "Notes",
        "Attachments",
        "Activities",
        "Visits",
        "Users",
        "Services",
        "Appointments",
    }
)


class CrmWriteForbidden(ConfigError):
    code = "crm_write_forbidden"


class CrmService:
    def __init__(
        self,
        http: httpx.AsyncClient,
        auth: ZohoAuth,
        *,
        api_domain: str,
        field_map: FieldMap,
        dry_run: bool,
        test_records: bool,
        retry_base_delay: float = 1.0,
    ) -> None:
        self._http = http
        self._auth = auth
        self._base = f"{api_domain.rstrip('/')}/crm/v8"
        self._fm = field_map
        self.dry_run = dry_run
        self._test_records = test_records
        self._retry_base_delay = retry_base_delay
        self.writable_modules = frozenset({field_map.meeting_record.module, field_map.glossary.module})
        forbidden = {m for m in self.writable_modules if m.lower() in {s.lower() for s in STANDARD_MODULES}}
        if forbidden:
            raise CrmWriteForbidden(
                f"field_map に標準モジュールが書き込み先として指定されています: {sorted(forbidden)}"
            )

    # ---- 共通 ----

    def check_writable(self, module: str) -> None:
        """書き込み先を「商談記録」「用語辞書」に限定する。"""
        if module not in self.writable_modules or module.lower() in {s.lower() for s in STANDARD_MODULES}:
            log_event(logger, "crm.write_blocked", logging.ERROR, module=module)
            raise CrmWriteForbidden(f"CRM のモジュール {module} への書き込みは許可されていません")

    async def _request(
        self, method: str, path: str, *, json: Any = None, params: dict[str, Any] | None = None
    ) -> httpx.Response:
        for attempt in range(2):
            token = await self._auth.token()
            resp = await send(
                self._http,
                method,
                f"{self._base}{path}",
                service="zoho_crm",
                base_delay=self._retry_base_delay,
                json=json,
                params=params,
                headers={"Authorization": f"Zoho-oauthtoken {token}"},
            )
            if resp.status_code == 401 and attempt == 0:
                # 期限切れ・失効。一度だけ取り直す
                self._auth.invalidate()
                continue
            return resp
        return resp

    @staticmethod
    def _json(resp: httpx.Response) -> dict[str, Any]:
        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            body = resp.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    @staticmethod
    def _first_result(resp: httpx.Response, operation: str) -> dict[str, Any]:
        body = CrmService._json(resp)
        data = body.get("data")
        item = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else None
        if resp.status_code >= 400 or item is None or str(item.get("status", "")).lower() != "success":
            if item is not None:
                details = item.get("details") if isinstance(item.get("details"), dict) else {}
                api_name = details.get("api_name")
                message = str(item.get("message") or operation)
                if api_name:
                    message = f"{message}（項目: {api_name}）"
                raise ExternalServiceError(
                    "zoho_crm",
                    message,
                    status=resp.status_code,
                    code=str(item.get("code") or "error"),
                    retryable=resp.status_code in (429, 500, 502, 503, 504),
                )
            raise error_from_response("zoho_crm", resp, f"{operation}に失敗しました")
        return item

    def _prefixed(self, data: dict[str, Any]) -> dict[str, Any]:
        """テスト中は名前の先頭に【TEST】を付ける（名前の上限を超える分は末尾を切る）。"""
        name_field = self._fm.meeting_record.name
        name = data.get(name_field)
        if self._test_records and isinstance(name, str) and not name.startswith(TEST_PREFIX):
            return {**data, name_field: f"{TEST_PREFIX}{name}"[: self._fm.limits.name]}
        return data

    def _dry_run(self, operation: str, module: str, data: dict[str, Any], **extra: Any) -> None:
        log_event(logger, "dry_run.skip", operation=operation, module=module, fields=sorted(data), **extra)

    # ---- 読み取り ----

    async def get_record(self, module: str, record_id: str) -> dict[str, Any] | None:
        resp = await self._request("GET", f"/{module}/{record_id}")
        if resp.status_code == 204:
            return None
        if resp.status_code >= 400:
            raise error_from_response("zoho_crm", resp, "レコードの取得に失敗しました")
        data = self._json(resp).get("data")
        return data[0] if isinstance(data, list) and data else None

    async def list_records(
        self, module: str, fields: list[str], *, per_page: int = 200, max_pages: int = 5
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            resp = await self._request(
                "GET", f"/{module}", params={"fields": ",".join(fields), "per_page": per_page, "page": page}
            )
            if resp.status_code == 204:
                break
            if resp.status_code >= 400:
                raise error_from_response("zoho_crm", resp, "レコード一覧の取得に失敗しました")
            body = self._json(resp)
            records.extend(r for r in body.get("data", []) if isinstance(r, dict))
            if not (body.get("info") or {}).get("more_records"):
                break
        return records

    async def coql(self, query: str) -> list[dict[str, Any]]:
        resp = await self._request("POST", "/coql", json={"select_query": query})
        if resp.status_code == 204:
            return []
        if resp.status_code >= 400:
            raise error_from_response("zoho_crm", resp, "COQL の実行に失敗しました")
        return [r for r in self._json(resp).get("data", []) if isinstance(r, dict)]

    # ---- 書き込み（商談記録・用語辞書のみ） ----

    async def update_record(self, module: str, record_id: str, data: dict[str, Any]) -> None:
        self.check_writable(module)
        if self.dry_run:
            self._dry_run("crm.update", module, data, record_id=record_id)
            return
        resp = await self._request("PUT", f"/{module}/{record_id}", json={"data": [data], "trigger": []})
        self._first_result(resp, "レコードの更新")
        log_event(logger, "crm.updated", module=module, record_id=record_id, fields=sorted(data))

    async def create_record(self, module: str, data: dict[str, Any]) -> str:
        self.check_writable(module)
        data = self._prefixed(data)
        if self.dry_run:
            self._dry_run("crm.create", module, data)
            return "dry-run"
        resp = await self._request("POST", f"/{module}", json={"data": [data], "trigger": []})
        item = self._first_result(resp, "レコードの作成")
        record_id = str((item.get("details") or {}).get("id", ""))
        log_event(logger, "crm.created", module=module, record_id=record_id, fields=sorted(data))
        return record_id

    async def upsert_record(
        self, module: str, data: dict[str, Any], duplicate_check_fields: list[str]
    ) -> tuple[str, str]:
        """duplicate_check_fields が一致するレコードがあれば更新、無ければ作成。(id, "insert"|"update") を返す。"""
        self.check_writable(module)
        data = self._prefixed(data)
        if self.dry_run:
            self._dry_run("crm.upsert", module, data, duplicate_check_fields=duplicate_check_fields)
            return "dry-run", "insert"
        resp = await self._request(
            "POST",
            f"/{module}/upsert",
            json={"data": [data], "duplicate_check_fields": duplicate_check_fields, "trigger": []},
        )
        item = self._first_result(resp, "レコードの作成・更新")
        record_id = str((item.get("details") or {}).get("id", ""))
        action = str(item.get("action") or "update")
        log_event(
            logger, "crm.upserted", module=module, record_id=record_id, action=action, fields=sorted(data)
        )
        return record_id, action
