"""Zoho CRM に「商談記録」「用語辞書」モジュールと項目を API で作る（Cloud Shell で実行する想定）。

標準ライブラリだけで動く。既定は dry-run（何を作るかを表示するだけで、書き込まない）。

安全のための約束:
- 書き込む前に必ず GET /crm/v8/org で組織を照合する（id・domain_name・company_name の3点。1つでも違えば中止）
- 既存のモジュール・項目は一切変更しない。無いものを POST で追加するだけ（PATCH / PUT / DELETE は使わない）
- 作成してよいモジュールは SPEC に書いた2つだけ。同じ表示名の別モジュールがあれば中止
- 接続先の既定値は持たない。環境変数がそろっていなければエラーで止まる
- 4xx はリトライしない。429 / 5xx は指数バックオフで最大3回。すべての通信を logs/ に JSON Lines で残す

使い方（詳しくは docs/crm-setup.md）:
    python3 scripts/crm_setup.py exchange-code <認可コード>   # 初回だけ。リフレッシュトークンを得る
    python3 scripts/crm_setup.py show-org                     # 接続先の組織を表示する（書き込みなし）
    python3 scripts/crm_setup.py plan                         # 作るものを表示する（書き込みなし）
    python3 scripts/crm_setup.py apply                        # 実際に作る

必要な環境変数:
    ZOHO_DC               com / jp / eu / in / com.au / ca（マルサン木型は com）
    ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET   Self Client の値
    ZOHO_REFRESH_TOKEN    exchange-code で得た値（exchange-code 以外で必要）
    EXPECTED_ORG_ID / EXPECTED_ORG_DOMAIN / EXPECTED_COMPANY_NAME   show-org で確かめた値（plan / apply で必要）
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

SCOPES = ",".join(
    [
        "ZohoCRM.org.READ",
        "ZohoCRM.settings.modules.ALL",
        "ZohoCRM.settings.fields.ALL",
        "ZohoCRM.settings.profiles.READ",
    ]
)
DATA_CENTERS = {"com", "jp", "eu", "in", "com.au", "ca"}
FIELDS_PER_REQUEST = 5  # Zoho の上限（1リクエスト5項目まで）
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


# ---- 作るもの（app/field_map.py の API 名と一致させる） ----


def text(label: str, api: str, length: int = 255, **extra: Any) -> dict[str, Any]:
    return {"field_label": label, "api_name": api, "data_type": "text", "length": length, **extra}


def textarea(label: str, api: str, size: str) -> dict[str, Any]:
    length = 32000 if size == "large" else 2000
    return {
        "field_label": label,
        "api_name": api,
        "data_type": "textarea",
        "length": length,
        "textarea": {"type": size},
    }


def picklist(label: str, api: str, values: list[str]) -> dict[str, Any]:
    return {
        "field_label": label,
        "api_name": api,
        "data_type": "picklist",
        "pick_list_values": [{"display_value": v, "actual_value": v} for v in values],
    }


def lookup(label: str, api: str, module: str, related_label: str) -> dict[str, Any]:
    # display_label が無いと MANDATORY_NOT_FOUND で5件まとめて 400 になる
    return {
        "field_label": label,
        "api_name": api,
        "data_type": "lookup",
        "lookup": {"module": {"api_name": module}, "display_label": related_label},
    }


def simple(label: str, api: str, data_type: str) -> dict[str, Any]:
    return {"field_label": label, "api_name": api, "data_type": data_type}


@dataclass(frozen=True)
class ModuleSpec:
    api_name: str
    label: str
    display_field_label: str
    fields: list[dict[str, Any]] = field(default_factory=list)


SPEC: list[ModuleSpec] = [
    ModuleSpec(
        api_name="MeetingRecords",
        label="商談記録",
        display_field_label="商談記録名",
        fields=[
            picklist("商談形態", "Meeting_Type", ["対面", "オンライン"]),
            picklist("取得方法", "Capture_Method", ["デスクトップ", "ボット", "対面録音"]),
            picklist(
                "状態",
                "Status",
                [
                    "予約済",
                    "参加待ち",
                    "参加中",
                    "参加失敗",
                    "録音中",
                    "文字起こし中",
                    "完了",
                    "失敗",
                    "取引先未設定",
                ],
            ),
            lookup("取引先", "Account", "Accounts", "商談記録"),
            lookup("商談", "Deal", "Deals", "商談記録"),
            text("先方担当者", "Contact_Name"),
            simple("開始日時", "Start_At", "datetime"),
            simple("会議URL", "Meeting_URL", "website"),
            simple("録音用URL", "Recording_URL", "website"),
            # デスクトップ方式の upsert キー。重複を許さない
            text("Recall ID", "Recall_ID", unique={"case_sensitive": True}),
            textarea("エラー内容", "Error_Message", "small"),
            textarea("要約", "Summary", "large"),
            textarea("課題", "Issues", "large"),
            textarea("ニーズ", "Needs", "large"),
            textarea("次のアクション", "Next_Actions", "large"),
            simple("次回期限", "Due_Date", "date"),
            text("分類", "Category"),
            textarea("競合", "Competitors", "large"),
            text("予算", "Budget"),
            text("決裁者", "Decision_Maker"),
            textarea("文字起こし全文", "Transcript", "large"),
            textarea("文字起こし全文2", "Transcript_2", "large"),
        ],
    ),
    ModuleSpec(
        api_name="Glossary",
        label="用語辞書",
        display_field_label="用語",
        fields=[
            textarea("誤認識例", "Misrecognitions", "small"),
            picklist("種類", "Term_Type", ["社名", "製品名", "人名", "専門用語", "その他"]),
        ],
    ),
]


# ---- 通信 ----


class SetupError(Exception):
    pass


class Logger:
    def __init__(self) -> None:
        LOG_DIR.mkdir(exist_ok=True)
        self.path = LOG_DIR / f"crm_setup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"

    def write(self, entry: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": time.time(), **entry}, ensure_ascii=False) + "\n")


class Zoho:
    """アクセストークンはプロセス内でキャッシュする（連続取得すると Zoho に拒否される）。"""

    def __init__(self, dc: str, client_id: str, client_secret: str, refresh_token: str, log: Logger) -> None:
        self.accounts = f"https://accounts.zoho.{dc}"
        self.api = f"https://www.zohoapis.{dc}"
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.log = log
        self._token: str | None = None
        self._expires_at = 0.0

    def _http(self, req: urllib.request.Request) -> tuple[int, Any]:
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read()
                    return resp.status, json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                raw = e.read()
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = raw.decode("utf-8", "replace")[:500]
                if (e.code == 429 or e.code >= 500) and attempt < 3:
                    time.sleep(2**attempt)
                    continue
                return e.code, body
            except urllib.error.URLError:
                if attempt < 3:
                    time.sleep(2**attempt)
                    continue
                raise
        raise SetupError("通信に失敗しました")

    def token(self) -> str:
        if self._token and time.time() < self._expires_at:
            return self._token
        data = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
        ).encode()
        status, body = self._http(urllib.request.Request(f"{self.accounts}/oauth/v2/token", data=data))
        self.log.write({"op": "token", "status": status})
        if status != 200 or not isinstance(body, dict) or "access_token" not in body:
            error = body.get("error") if isinstance(body, dict) else None
            raise SetupError(f"アクセストークンを取得できません（HTTP {status} {error}）")
        self._token = body["access_token"]
        self._expires_at = time.time() + int(body.get("expires_in", 3600)) - 300
        return self._token

    def request(self, method: str, path: str, params: dict[str, str] | None = None, body: Any = None) -> Any:
        url = f"{self.api}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Zoho-oauthtoken {self.token()}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        status, resp = self._http(req)
        self.log.write(
            {
                "op": "api",
                "method": method,
                "path": path,
                "params": params,
                "request": body,
                "status": status,
                "response": resp,
            }
        )
        if status == 204:
            return None
        if status >= 400:
            raise SetupError(
                f"{method} {path} が失敗しました（HTTP {status}）: {json.dumps(resp, ensure_ascii=False)[:800]}"
            )
        return resp


# ---- 計画 ----


@dataclass
class Plan:
    create_modules: list[ModuleSpec] = field(default_factory=list)
    create_fields: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.create_modules and not any(self.create_fields.values())


def check_org(org: dict[str, Any], expected: dict[str, str]) -> None:
    for key, want in expected.items():
        got = str(org.get(key, ""))
        if got != want:
            raise SetupError(
                f"接続先の組織が違います: {key} = {got!r}（期待値 {want!r}）。何も変更せずに中止します。"
            )


def build_plan(api: Any, spec: list[ModuleSpec]) -> Plan:
    plan = Plan()
    modules = (api.request("GET", "/crm/v8/settings/modules") or {}).get("modules", [])
    by_api = {m.get("api_name"): m for m in modules}
    for ms in spec:
        existing = by_api.get(ms.api_name)
        same_label = [
            m.get("api_name")
            for m in modules
            if m.get("api_name") != ms.api_name
            and ms.label in (m.get("plural_label"), m.get("singular_label"))
        ]
        if same_label:
            raise SetupError(
                f"「{ms.label}」という名前の別のモジュール（{same_label}）が既にあります。中止します。"
            )
        if existing is None:
            plan.create_modules.append(ms)
            plan.create_fields[ms.api_name] = list(ms.fields)
            continue
        if existing.get("generated_type") != "custom":
            raise SetupError(f"{ms.api_name} はカスタムモジュールではありません。中止します。")
        current = (api.request("GET", "/crm/v8/settings/fields", {"module": ms.api_name}) or {}).get(
            "fields", []
        )
        by_field = {f.get("api_name"): f for f in current}
        labels = {f.get("field_label"): f.get("api_name") for f in current}
        missing = []
        for fs in ms.fields:
            found = by_field.get(fs["api_name"])
            if found is None:
                other = labels.get(fs["field_label"])
                if other:
                    raise SetupError(
                        f"{ms.api_name} に表示名「{fs['field_label']}」の項目が別の API 名（{other}）で既にあります。中止します。"
                    )
                missing.append(fs)
            elif found.get("data_type") != fs["data_type"]:
                plan.warnings.append(
                    f"{ms.api_name}.{fs['api_name']} の種類が {found.get('data_type')}（想定 {fs['data_type']}）。変更はしません。"
                )
        plan.create_fields[ms.api_name] = missing
    return plan


def describe(plan: Plan) -> str:
    lines = []
    for ms in plan.create_modules:
        lines.append(f"モジュールを作成: {ms.label}（{ms.api_name}）表示名の項目「{ms.display_field_label}」")
    for module, fields in plan.create_fields.items():
        for f in fields:
            lines.append(f"項目を作成: {module}.{f['api_name']}「{f['field_label']}」({f['data_type']})")
    lines += [f"注意: {w}" for w in plan.warnings]
    if plan.empty:
        lines.append("作成するものはありません（すべて作成済み）。")
    return "\n".join(lines)


def apply(api: Any, plan: Plan) -> None:
    if plan.create_modules:
        profiles = (api.request("GET", "/crm/v8/settings/profiles") or {}).get("profiles", [])
        if not profiles:
            raise SetupError("プロファイルを取得できません")
        for ms in plan.create_modules:
            api.request(
                "POST",
                "/crm/v8/settings/modules",
                body={
                    "modules": [
                        {
                            "plural_label": ms.label,
                            "singular_label": ms.label,
                            "api_name": ms.api_name,
                            "profiles": [{"id": p["id"]} for p in profiles],
                            "display_field": {"field_label": ms.display_field_label, "data_type": "text"},
                        }
                    ]
                },
            )
            print(f"作成しました: モジュール {ms.api_name}")
    for module, fields in plan.create_fields.items():
        for i in range(0, len(fields), FIELDS_PER_REQUEST):
            batch = fields[i : i + FIELDS_PER_REQUEST]
            try:
                api.request("POST", "/crm/v8/settings/fields", {"module": module}, {"fields": batch})
            except SetupError:
                # 400 でも一部は作成されていることがある。現物を確かめてから止める
                current = (api.request("GET", "/crm/v8/settings/fields", {"module": module}) or {}).get(
                    "fields", []
                )
                made = {f.get("api_name") for f in current}
                print("作成済みになった項目:", [f["api_name"] for f in batch if f["api_name"] in made])
                raise
            print(f"作成しました: {module} の項目 {[f['api_name'] for f in batch]}")


def verify(api: Any, spec: list[ModuleSpec]) -> list[str]:
    """作成後の突き合わせ。問題の一覧を返す（空なら OK）。"""
    problems = []
    for ms in spec:
        current = (api.request("GET", "/crm/v8/settings/fields", {"module": ms.api_name}) or {}).get(
            "fields", []
        )
        by_field = {f.get("api_name"): f for f in current}
        for fs in ms.fields:
            if fs["api_name"] not in by_field:
                problems.append(f"{ms.api_name}.{fs['api_name']} がありません")
        name = by_field.get("Name")
        if name is None or name.get("field_label") != ms.display_field_label:
            actual = [f.get("api_name") for f in current if f.get("field_label") == ms.display_field_label]
            problems.append(
                f"{ms.api_name} の表示名の項目「{ms.display_field_label}」の API 名が Name ではありません（実際: {actual}）"
            )
    return problems


# ---- 入口 ----


def env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SetupError(f"環境変数 {name} を設定してください（docs/crm-setup.md）")
    return value


def dc() -> str:
    value = env("ZOHO_DC")
    if value not in DATA_CENTERS:
        raise SetupError(f"ZOHO_DC は {sorted(DATA_CENTERS)} のどれかにしてください")
    return value


def exchange_code(code: str) -> None:
    data = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": env("ZOHO_CLIENT_ID"),
            "client_secret": env("ZOHO_CLIENT_SECRET"),
            "code": code,
        }
    ).encode()
    req = urllib.request.Request(f"https://accounts.zoho.{dc()}/oauth/v2/token", data=data)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise SetupError(f"認可コードの交換に失敗しました（HTTP {e.code}）") from e
    if "refresh_token" not in body:
        raise SetupError(
            f"リフレッシュトークンが返りませんでした: {body.get('error')}（認可コードは10分で失効します）"
        )
    print("次の行を Cloud Shell に貼ってください（画面の外には保存しないでください）:")
    print(f"export ZOHO_REFRESH_TOKEN='{body['refresh_token']}'")


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in {"exchange-code", "show-org", "plan", "apply"}:
        print(__doc__)
        return 2
    command = argv[1]
    try:
        if command == "exchange-code":
            if len(argv) != 3:
                raise SetupError("使い方: exchange-code <認可コード>")
            exchange_code(argv[2])
            return 0
        log = Logger()
        api = Zoho(dc(), env("ZOHO_CLIENT_ID"), env("ZOHO_CLIENT_SECRET"), env("ZOHO_REFRESH_TOKEN"), log)
        org = (api.request("GET", "/crm/v8/org") or {}).get("org", [{}])[0]
        if command == "show-org":
            for key in ("company_name", "domain_name", "id", "time_zone", "currency", "type"):
                print(f"{key}: {org.get(key)}")
            print("\n合っていれば、次の3行を実行してから plan に進んでください:")
            print(f"export EXPECTED_ORG_ID='{org.get('id')}'")
            print(f"export EXPECTED_ORG_DOMAIN='{org.get('domain_name')}'")
            print(f"export EXPECTED_COMPANY_NAME='{org.get('company_name')}'")
            return 0
        check_org(
            org,
            {
                "id": env("EXPECTED_ORG_ID"),
                "domain_name": env("EXPECTED_ORG_DOMAIN"),
                "company_name": env("EXPECTED_COMPANY_NAME"),
            },
        )
        print(f"接続先: {org.get('company_name')}（{org.get('domain_name')} / {org.get('id')}）\n")
        plan = build_plan(api, SPEC)
        print(describe(plan))
        if command == "plan":
            print("\n（dry-run です。何も変更していません。作成するには apply を実行してください）")
            return 0
        if plan.empty:
            return 0
        answer = input(f"\n上の内容を「{org.get('company_name')}」に作成します。よろしければ yes と入力: ")
        if answer.strip() != "yes":
            print("中止しました。何も変更していません。")
            return 1
        apply(api, plan)
        problems = verify(api, SPEC)
        if problems:
            print("\n確認で見つかった問題:")
            print("\n".join(f"- {p}" for p in problems))
            return 1
        print("\n完了しました。すべての項目が API 名どおりに作成されています。")
        print(f"通信の記録: {log.path}")
        return 0
    except SetupError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
