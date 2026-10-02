"""PC の CRM 画面の「商談日報」ウィジェット（商談記録の画面の関連リスト）の ZIP を作る（Cloud Shell で実行する想定）。

標準ライブラリだけで動く。CRM には何も書かない（ZIP を作るだけ）。

    cd ~/voice-app && git pull && python3 scripts/build_widget.py
    → dist/meeting-report-widget.zip（CRM の 設定 → 開発者スペース → ウィジェット に登録する。手順は docs/widget.md）

- 項目の API 名・状態の表示値は app/field_map.py の既定値を読んで field-map.js にする（コードに直書きしない）
- Zoho のウィジェット SDK は live.zwidgets.com から取って ZIP に入れる（CDN を読まないので CSP で止まらない）。
  手元のファイルを使うときは --sdk <ファイル>
- ZIP のルートに plugin-manifest.json と app/ を置き、ディレクトリの項目も入れる
  （zoho-crm-build の実測：無いと「Please upload a proper file」で断られる）
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WIDGET = ROOT / "widgets" / "meeting-report"
FIELD_MAP = ROOT / "app" / "field_map.py"
DEFAULT_OUTPUT = ROOT / "dist" / "meeting-report-widget.zip"
SDK_URL = "https://live.zwidgets.com/js-sdk/1.2/ZohoEmbededAppSDK.min.js"
SDK_NAME = "ZohoEmbededAppSDK.min.js"
APP_FILES = ["index.html", "style.css", "report.js", "main.js"]

# ウィジェットが使う商談記録の項目と状態（app/field_map.py の MeetingRecordFields・StatusValues の名前）
WIDGET_FIELDS = [
    "name",
    "status",
    "meeting_type",
    "capture_method",
    "account",
    "owner",
    "contact_name",
    "start_at",
    "category",
    "error_message",
    "transcript",
    "transcript_2",
    "summary",
    "issues",
    "needs",
    "budget",
    "decision_maker",
    "competitors",
    "next_actions",
    "due_date",
]
WIDGET_STATUS = ["done", "no_account", "failed", "join_failed", "transcribing"]


class BuildError(Exception):
    pass


def read_defaults(path: Path = FIELD_MAP) -> dict[str, dict[str, str]]:
    """app/field_map.py の各クラスの既定値（`name: str = "値"`）を読む（pydantic が無くても動くように、構文だけを見る）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    classes: dict[str, dict[str, str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        values: dict[str, str] = {}
        for item in node.body:
            if (
                isinstance(item, ast.AnnAssign)
                and isinstance(item.target, ast.Name)
                and isinstance(item.value, ast.Constant)
                and isinstance(item.value.value, str)
            ):
                values[item.target.id] = item.value.value
        classes[node.name] = values
    return classes


def widget_config(path: Path = FIELD_MAP) -> dict[str, object]:
    defaults = read_defaults(path)
    record = defaults.get("MeetingRecordFields", {})
    status = defaults.get("StatusValues", {})
    standard = defaults.get("StandardFields", {})
    missing = [f"MeetingRecordFields.{k}" for k in ["module", *WIDGET_FIELDS] if k not in record]
    missing += [f"StatusValues.{k}" for k in WIDGET_STATUS if k not in status]
    missing += [] if "accounts_module" in standard else ["StandardFields.accounts_module"]
    if missing:
        raise BuildError(f"app/field_map.py に次の既定値が見つかりません: {', '.join(missing)}")
    return {
        "module": record["module"],
        "accounts_module": standard["accounts_module"],
        "fields": {k: record[k] for k in WIDGET_FIELDS},
        "status": {k: status[k] for k in WIDGET_STATUS},
    }


def field_map_js(config: dict[str, object]) -> str:
    body = json.dumps(config, ensure_ascii=False, indent=2)
    return (
        "// scripts/build_widget.py が app/field_map.py から作った（手で直さない）\n"
        f"window.MEETING_REPORT_CONFIG = {body};\n"
    )


def fetch_sdk(url: str = SDK_URL) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 固定の https の URL
            data = resp.read()
    except OSError as exc:
        raise BuildError(f"Zoho のウィジェット SDK を取得できませんでした（{url}）: {exc}") from exc
    return check_sdk(data, url)


def check_sdk(data: bytes, source: str) -> bytes:
    if len(data) < 1000 or b"ZOHO" not in data:
        raise BuildError(f"Zoho のウィジェット SDK ではないようです（{source}、{len(data)} バイト）")
    return data


def build(output: Path = DEFAULT_OUTPUT, sdk: bytes | None = None) -> Path:
    """ZIP を作って、その場所を返す。sdk を渡さなければ live.zwidgets.com から取る。"""
    sdk_data = sdk if sdk is not None else fetch_sdk()
    config = widget_config()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.write(WIDGET / "plugin-manifest.json", "plugin-manifest.json")
        # ディレクトリの項目（Zoho は無いと受け付けない）
        directory = zipfile.ZipInfo("app/")
        directory.external_attr = 0o40755 << 16
        z.writestr(directory, b"")
        for name in APP_FILES:
            z.write(WIDGET / "app" / name, f"app/{name}")
        z.writestr("app/field-map.js", field_map_js(config))
        z.writestr(f"app/{SDK_NAME}", sdk_data)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="商談日報ウィジェットの ZIP を作る")
    parser.add_argument(
        "--sdk", type=Path, help="Zoho のウィジェット SDK のファイル（無ければ live.zwidgets.com から取る）"
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="作る ZIP の場所")
    args = parser.parse_args(argv)
    try:
        sdk = check_sdk(args.sdk.read_bytes(), str(args.sdk)) if args.sdk else None
        out = build(args.output, sdk)
    except (BuildError, OSError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1
    print(f"作りました: {out}")
    print(
        "パソコンに保存するには: cloudshell download "
        + str(out.relative_to(ROOT) if out.is_relative_to(ROOT) else out)
    )
    print("CRM への登録は docs/widget.md の手順2（設定 → 開発者スペース → ウィジェット）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
