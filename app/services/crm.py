"""Zoho CRM v8。

お客様の本番 CRM に接続するため、次を守る。
- 書き込み（作成・更新・upsert）はカスタムモジュール「商談記録」「用語辞書」と、ニーズ分析の「ニーズ」「ニーズ分析」だけ
  （ニーズ分析の2つは、そのモジュールを作ったクライアントでしか使わない。docs/needs-analysis.md）。
  それ以外（取引先・連絡先・商談などの標準モジュール）への書き込みは、送信前にエラーにする。
- 削除の処理は持たない。
- DRY_RUN のときは書き込みを送らず、どの項目を書く予定だったか（項目名だけ）をログに出す。
- 作成するレコードの名前には、テスト中（CRM_TEST_RECORDS=true）は先頭に【TEST】を付ける。
- 更新は "trigger": [] でワークフローを再発火させない。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from app.errors import ConfigError, ExternalServiceError
from app.field_map import FieldMap
from app.logs import log_event
from app.services.http import error_from_response, send
from app.services.zoho_auth import ZohoAuth

logger = logging.getLogger(__name__)

TEST_PREFIX = "【TEST】"
# まとめて作るときの1回の件数（Zoho のレコード API は1回 100件まで）
CREATE_BATCH = 100


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


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
        self.writable_modules = frozenset(
            {
                field_map.meeting_record.module,
                field_map.glossary.module,
                field_map.needs.need.module,
                field_map.needs.analysis.module,
            }
        )
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

    async def list_records_since(
        self,
        module: str,
        fields: list[str],
        since: datetime,
        *,
        per_page: int = 200,
        max_pages: int = 10,
    ) -> tuple[list[dict[str, Any]], bool]:
        """作成日時が since 以降のレコードを、新しい順に読む（作成日時の降順で読み、since より古くなったら止める）。

        (レコード, 上限で読み切れなかったか) を返す。上限は max_pages × per_page 件（page 方式は 2,000 件まで）。
        """
        records: list[dict[str, Any]] = []
        wanted = sorted({*fields, "Created_Time"})
        for page in range(1, max_pages + 1):
            resp = await self._request(
                "GET",
                f"/{module}",
                params={
                    "fields": ",".join(wanted),
                    "per_page": per_page,
                    "page": page,
                    "sort_by": "Created_Time",
                    "sort_order": "desc",
                },
            )
            if resp.status_code == 204:
                return records, False
            if resp.status_code >= 400:
                raise error_from_response("zoho_crm", resp, "レコード一覧の取得に失敗しました")
            body = self._json(resp)
            for row in body.get("data", []):
                if not isinstance(row, dict):
                    continue
                created = _parse_datetime(row.get("Created_Time"))
                if created is not None and created < since:
                    return records, False
                records.append(row)
            if not (body.get("info") or {}).get("more_records"):
                return records, False
        return records, True

    async def get_org(self) -> dict[str, Any]:
        """接続先の組織（org[0]。id / domain_name / company_name など）。"""
        resp = await self._request("GET", "/org")
        if resp.status_code >= 400:
            raise error_from_response("zoho_crm", resp, "組織情報の取得に失敗しました")
        org = self._json(resp).get("org")
        return org[0] if isinstance(org, list) and org and isinstance(org[0], dict) else {}

    async def list_modules(self) -> list[dict[str, Any]]:
        """モジュールの設定（GET /settings/modules。api_name・plural_label・generated_type など）。"""
        resp = await self._request("GET", "/settings/modules")
        if resp.status_code >= 400:
            raise error_from_response("zoho_crm", resp, "モジュールの設定の取得に失敗しました")
        return [m for m in self._json(resp).get("modules", []) if isinstance(m, dict)]

    async def list_fields(self, module: str) -> list[dict[str, Any]]:
        """項目の設定（GET /settings/fields?module=…。api_name・data_type・lookup など）。"""
        resp = await self._request("GET", "/settings/fields", params={"module": module})
        if resp.status_code >= 400:
            raise error_from_response("zoho_crm", resp, "項目の設定の取得に失敗しました")
        return [f for f in self._json(resp).get("fields", []) if isinstance(f, dict)]

    async def get_user(self, user_id: str) -> dict[str, Any] | None:
        """CRM のユーザー（users[0]。id・status など）。いない（削除済み・ID が違う）ときは None。"""
        if not user_id.isdigit():
            return None
        resp = await self._request("GET", f"/users/{user_id}")
        if resp.status_code in (204, 404):
            return None
        if resp.status_code >= 400:
            error = error_from_response("zoho_crm", resp, "ユーザーの取得に失敗しました")
            if resp.status_code == 400 and error.code == "INVALID_DATA":
                return None
            raise error
        users = self._json(resp).get("users")
        return users[0] if isinstance(users, list) and users and isinstance(users[0], dict) else None

    async def coql(self, query: str) -> list[dict[str, Any]]:
        resp = await self._request("POST", "/coql", json={"select_query": query})
        if resp.status_code == 204:
            return []
        if resp.status_code >= 400:
            raise error_from_response("zoho_crm", resp, "COQL の実行に失敗しました")
        return [r for r in self._json(resp).get("data", []) if isinstance(r, dict)]

    # ---- 書き込み（商談記録・用語辞書・ニーズ・ニーズ分析のみ） ----

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

    async def create_records(self, module: str, rows: list[dict[str, Any]]) -> list[tuple[str | None, str]]:
        """まとめて作る（1回 100件まで）。行ごとに (作成した ID か None, "created" / "duplicate" / エラーコード) を返す。

        重複不可の項目が同じ行（DUPLICATE_DATA）は作らずに "duplicate" にする（ニーズの抽出キーで二重に作らないため）。
        """
        self.check_writable(module)
        rows = [self._prefixed(r) for r in rows]
        if self.dry_run:
            for r in rows:
                self._dry_run("crm.create", module, r)
            return [(None, "dry-run") for _ in rows]
        results: list[tuple[str | None, str]] = []
        for i in range(0, len(rows), CREATE_BATCH):
            batch = rows[i : i + CREATE_BATCH]
            resp = await self._request("POST", f"/{module}", json={"data": batch, "trigger": []})
            body = self._json(resp)
            items = body.get("data") if isinstance(body.get("data"), list) else None
            if items is None or len(items) != len(batch):
                raise error_from_response("zoho_crm", resp, "レコードのまとめての作成に失敗しました")
            for item in items:
                item = item if isinstance(item, dict) else {}
                if str(item.get("status", "")).lower() == "success":
                    results.append((str((item.get("details") or {}).get("id", "")) or None, "created"))
                elif item.get("code") == "DUPLICATE_DATA":
                    results.append((None, "duplicate"))
                else:
                    results.append((None, str(item.get("code") or "error")))
        created = sum(1 for _, r in results if r == "created")
        log_event(logger, "crm.created_many", module=module, rows=len(rows), created=created)
        return results

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
