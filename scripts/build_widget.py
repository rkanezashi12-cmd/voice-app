"""PC の CRM 画面のウィジェット（関連リスト）の ZIP を作る（Cloud Shell で実行する想定）。

標準ライブラリだけで動く。CRM には何も書かない（ZIP を作るだけ）。

    cd ~/voice-app && git pull && python3 scripts/build_widget.py
    → dist/meeting-report-widget.zip（商談日報。商談記録の画面。手順は docs/widget.md）
    python3 scripts/build_widget.py --widget needs-analysis
    → dist/needs-analysis-widget.zip（ニーズ分析の結果。ニーズ分析の画面。手順は docs/needs-analysis.md）
      どちらも CRM の 設定 → 開発者スペース → ウィジェット に登録する。
      登録のときのインデックスページは /index.html（Zoho は ZIP の app/ を一番上の階層として置く。2026-10-06 実測）

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
from collections.abc import Callable
from dataclasses import dataclass
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


def read_defaults(path: Path = FIELD_MAP) -> dict[str, dict[str, object]]:
    """app/field_map.py の各クラスの既定値（`name: str = "値"` と、文字の tuple）を読む
    （pydantic が無くても動くように、構文だけを見る）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    classes: dict[str, dict[str, object]] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        values: dict[str, object] = {}
        for item in node.body:
            if not (isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)):
                continue
            value = item.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                values[item.target.id] = value.value
            elif isinstance(value, ast.Tuple) and all(
                isinstance(e, ast.Constant) and isinstance(e.value, str) for e in value.elts
            ):
                values[item.target.id] = [e.value for e in value.elts]  # type: ignore[union-attr]
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


# ニーズ分析ウィジェットが使う項目・選択肢（app/field_map.py の NeedsAnalysisFields・CustomerNeedFields・NeedsValues の名前）
NEEDS_ANALYSIS_FIELDS = [
    "name",
    "period_from",
    "period_to",
    "targets",
    "status",
    "counts",
    "top_needs",
    "matched_needs",
    "product_ideas",
    "error_message",
    "analyzed_at",
]
NEED_FIELDS = [
    "name",
    "detail",
    "need_type",
    "source_type",
    "company",
    "evidence",
    "need_date",
    "extract_key",
]
NEEDS_VALUES = [
    "need_types",
    "source_meeting",
    "source_inquiry",
    "target_both",
    "target_meetings",
    "target_inquiries",
    "status_running",
    "status_done",
    "status_failed",
]


def needs_widget_config(path: Path = FIELD_MAP) -> dict[str, object]:
    defaults = read_defaults(path)
    analysis = defaults.get("NeedsAnalysisFields", {})
    need = defaults.get("CustomerNeedFields", {})
    values = defaults.get("NeedsValues", {})
    meeting = defaults.get("MeetingRecordFields", {})
    inquiry = defaults.get("InquiryFields", {})
    missing = [f"NeedsAnalysisFields.{k}" for k in ["module", *NEEDS_ANALYSIS_FIELDS] if k not in analysis]
    missing += [f"CustomerNeedFields.{k}" for k in ["module", *NEED_FIELDS] if k not in need]
    missing += [f"NeedsValues.{k}" for k in NEEDS_VALUES if k not in values]
    missing += [] if "module" in meeting else ["MeetingRecordFields.module"]
    missing += [] if "module" in inquiry else ["InquiryFields.module"]
    if missing:
        raise BuildError(f"app/field_map.py に次の既定値が見つかりません: {', '.join(missing)}")
    return {
        "analysis_module": analysis["module"],
        "need_module": need["module"],
        "meeting_module": meeting["module"],
        "inquiry_module": inquiry["module"],
        "analysis_fields": {k: analysis[k] for k in NEEDS_ANALYSIS_FIELDS},
        "need_fields": {k: need[k] for k in NEED_FIELDS},
        "values": {k: values[k] for k in NEEDS_VALUES},
    }


@dataclass(frozen=True)
class Widget:
    folder: Path
    output: Path
    app_files: list[str]
    config_var: str
    config: Callable[[], dict[str, object]]
    doc: str


WIDGETS: dict[str, Widget] = {
    "meeting-report": Widget(
        WIDGET, DEFAULT_OUTPUT, APP_FILES, "MEETING_REPORT_CONFIG", widget_config, "docs/widget.md"
    ),
    "needs-analysis": Widget(
        ROOT / "widgets" / "needs-analysis",
        ROOT / "dist" / "needs-analysis-widget.zip",
        ["index.html", "style.css", "analysis.js", "main.js"],
        "NEEDS_ANALYSIS_CONFIG",
        needs_widget_config,
        "docs/needs-analysis.md",
    ),
}


def field_map_js(config: dict[str, object], var: str = "MEETING_REPORT_CONFIG") -> str:
    body = json.dumps(config, ensure_ascii=False, indent=2)
    return (
        f"// scripts/build_widget.py が app/field_map.py から作った（手で直さない）\nwindow.{var} = {body};\n"
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


def build(output: Path | None = None, sdk: bytes | None = None, widget: str = "meeting-report") -> Path:
    """ZIP を作って、その場所を返す。sdk を渡さなければ live.zwidgets.com から取る。"""
    w = WIDGETS[widget]
    output = output or w.output
    config = w.config()
    sdk_data = sdk if sdk is not None else fetch_sdk()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.write(w.folder / "plugin-manifest.json", "plugin-manifest.json")
        # ディレクトリの項目（Zoho は無いと受け付けない）
        directory = zipfile.ZipInfo("app/")
        directory.external_attr = 0o40755 << 16
        z.writestr(directory, b"")
        for name in w.app_files:
            z.write(w.folder / "app" / name, f"app/{name}")
        z.writestr("app/field-map.js", field_map_js(config, w.config_var))
        z.writestr(f"app/{SDK_NAME}", sdk_data)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CRM のウィジェットの ZIP を作る")
    parser.add_argument(
        "--widget",
        choices=sorted(WIDGETS),
        default="meeting-report",
        help="作るウィジェット（既定は商談日報）",
    )
    parser.add_argument(
        "--sdk", type=Path, help="Zoho のウィジェット SDK のファイル（無ければ live.zwidgets.com から取る）"
    )
    parser.add_argument(
        "--output", type=Path, help="作る ZIP の場所（既定は dist/<ウィジェット>-widget.zip）"
    )
    args = parser.parse_args(argv)
    try:
        sdk = check_sdk(args.sdk.read_bytes(), str(args.sdk)) if args.sdk else None
        out = build(args.output, sdk, args.widget)
    except (BuildError, OSError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1
    print(f"作りました: {out}")
    print(
        "パソコンに保存するには: cloudshell download "
        + str(out.relative_to(ROOT) if out.is_relative_to(ROOT) else out)
    )
    print(f"CRM への登録は {WIDGETS[args.widget].doc}（設定 → 開発者スペース → ウィジェット）。")
    print("インデックスページは /index.html（/app/ は付けない）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
