"""動画の解析の結果を、テキスト（CRM の添付ファイル）と Word（録音アプリからダウンロード）にする。

テキストが正：解析のときに作り、商談記録に .txt で添付する（CRM に残る。GCP には置かない）。
Word はダウンロードのたびに、そのテキストから作る（書き込みは増えない）。テキストの書き方：
- 「■ 見出し」… 章（Word の見出し1）
- 「## 見出し」… 節（見出し2。場面ごとの記録・画面ごとの詳細）、「### 見出し」… 見出し3
- 「・」「- 」… 箇条書き（行頭の空白2つごとに1段下げる）
- 「| a | b |」の行が続くところ … 表（1行目が見出し。「|---|」の行は飛ばす）
- それ以外 … 段落
"""

from __future__ import annotations

import io
import re
from typing import Any

from app.pipeline import formatting as fmt

# 章の見出し（テキストの「■ …」）。要約版に入れる章は SUMMARY_SECTIONS
SECTION_SUMMARY = "要約"
SECTION_FLOW = "業務の流れ"
SECTION_SCREENS = "出てきた画面"
SECTION_FUNCTIONS = "機能・使い方"
SECTION_SCREEN_DETAILS = "画面ごとの詳細"
SECTION_DATA = "扱うデータ"
SECTION_INTEGRATIONS = "ほかのシステム・Excel・紙とのやりとり"
SECTION_ISSUES = "今の課題"
SECTION_REQUIREMENTS = "要件の候補"
SECTION_QUESTIONS = "確かめること"
SECTION_SCENES = "場面ごとの記録"
SUMMARY_SECTIONS = (
    SECTION_SUMMARY,
    SECTION_FLOW,
    SECTION_SCREENS,
    SECTION_ISSUES,
    SECTION_REQUIREMENTS,
    SECTION_QUESTIONS,
)
NONE_TEXT = "（なし）"


def strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v.strip() for v in value if isinstance(v, str) and v.strip()]


def _cell(value: Any) -> str:
    return str(value or "").replace("|", "／").replace("\n", " ").strip()


def _numbered(items: list[str]) -> str:
    return "\n".join(f"・{i}. {item}" for i, item in enumerate(items, 1)) if items else NONE_TEXT


def _screen_details(screens: Any) -> str:
    if not isinstance(screens, list) or not screens:
        return NONE_TEXT
    blocks: list[str] = []
    for screen in screens:
        if not isinstance(screen, dict):
            continue
        lines = [f"## {_cell(screen.get('name')) or '（画面名なし）'}"]
        if screen.get("purpose"):
            lines.append(f"目的：{_cell(screen.get('purpose'))}")
        items = [i for i in screen.get("items") or [] if isinstance(i, dict) and i.get("name")]
        if items:
            lines += ["### 項目", "| 項目 | 種類 | 説明 |", "|---|---|---|"]
            lines += [
                f"| {_cell(i.get('name'))} | {_cell(i.get('kind'))} | {_cell(i.get('note'))} |" for i in items
            ]
        for label, key in (("操作", "operations"), ("ルール・例外", "rules"), ("帳票・出力", "outputs")):
            values = strings(screen.get(key))
            if values:
                lines += [f"### {label}", *(f"・{v}" for v in values)]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) or NONE_TEXT


def report_text(title: str, header: str, report: dict[str, Any], scenes: str) -> str:
    """解析の結果のテキスト全体（添付ファイルの中身）。"""
    sections: list[tuple[str, str]] = [
        (SECTION_SUMMARY, str(report.get("summary") or "").strip() or NONE_TEXT),
        (SECTION_FLOW, _numbered(strings(report.get("business_flow")))),
        (SECTION_SCREENS, fmt.bullets(strings(report.get("screens"))) or NONE_TEXT),
        (SECTION_FUNCTIONS, fmt.bullets(strings(report.get("functions"))) or NONE_TEXT),
        (SECTION_SCREEN_DETAILS, _screen_details(report.get("screen_details"))),
        (SECTION_DATA, fmt.bullets(strings(report.get("data"))) or NONE_TEXT),
        (SECTION_INTEGRATIONS, fmt.bullets(strings(report.get("integrations"))) or NONE_TEXT),
        (SECTION_ISSUES, fmt.bullets(strings(report.get("issues"))) or NONE_TEXT),
        (SECTION_REQUIREMENTS, fmt.bullets(strings(report.get("requirements"))) or NONE_TEXT),
        (SECTION_QUESTIONS, fmt.bullets(strings(report.get("questions"))) or NONE_TEXT),
        (SECTION_SCENES, scenes.strip() or NONE_TEXT),
    ]
    out = [f"動画：{title}", header]
    for name, body in sections:
        out += ["", f"■ {name}", body]
    return "\n".join(out)


def fallback_text(title: str, fields: dict[str, str | None]) -> str:
    """添付ファイルが無い記録（添付を使わない会社・添付に失敗した）を、商談記録の項目から組み立てる。"""
    out = [f"動画：{title}", "（商談記録の項目から作りました。場面ごとの記録は 64,000 文字までです）"]
    for name in (SECTION_SUMMARY, SECTION_ISSUES, SECTION_REQUIREMENTS, SECTION_QUESTIONS, SECTION_SCENES):
        out += ["", f"■ {name}", (fields.get(name) or "").strip() or NONE_TEXT]
    return "\n".join(out)


# ---- Word ----

_HEADING = re.compile(r"^(#{2,3})\s+(.*)$")
_BULLET = re.compile(r"^(\s*)(?:・|- |\* )(.*)$")
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _split_sections(text: str) -> tuple[list[str], list[tuple[str, list[str]]]]:
    head: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    for line in text.replace("\r\n", "\n").lstrip("﻿").split("\n"):
        if line.startswith("■ "):
            sections.append((line[2:].strip(), []))
        elif sections:
            sections[-1][1].append(line)
        else:
            head.append(line)
    return head, sections


def _table_rows(lines: list[str]) -> list[list[str]]:
    rows = []
    for line in lines:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        rows.append(cells)
    return rows


def _set_font(doc: Any) -> None:
    from docx.oxml.ns import qn
    from docx.shared import Pt

    for name in ("Normal", "Heading 1", "Heading 2", "Heading 3", "Title", "List Bullet", "List Bullet 2"):
        style = doc.styles[name]
        style.font.name = "Meiryo UI"
        rpr = style.element.get_or_add_rPr()
        fonts = rpr.find(qn("w:rFonts"))
        if fonts is None:
            fonts = rpr.makeelement(qn("w:rFonts"), {})
            rpr.append(fonts)
        fonts.set(qn("w:eastAsia"), "Meiryo UI")
    doc.styles["Normal"].font.size = Pt(10.5)


def _add_lines(doc: Any, lines: list[str]) -> None:
    i = 0
    while i < len(lines):
        line = _BOLD.sub(r"\1", lines[i].rstrip())
        if not line.strip():
            i += 1
            continue
        if line.lstrip().startswith("|"):
            block = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                block.append(_BOLD.sub(r"\1", lines[i]))
                i += 1
            rows = _table_rows(block)
            if rows:
                width = max(len(r) for r in rows)
                table = doc.add_table(rows=len(rows), cols=width)
                table.style = "Table Grid"
                for r, row in enumerate(rows):
                    for c in range(width):
                        cell = table.cell(r, c)
                        cell.text = row[c] if c < len(row) else ""
                        if r == 0:
                            for run in cell.paragraphs[0].runs:
                                run.bold = True
            continue
        heading = _HEADING.match(line)
        bullet = _BULLET.match(line)
        if heading:
            doc.add_heading(heading.group(2).strip(), level=len(heading.group(1)))
        elif bullet:
            level = 2 if len(bullet.group(1)) >= 2 else 1
            doc.add_paragraph(bullet.group(2).strip(), style="List Bullet 2" if level == 2 else "List Bullet")
        else:
            doc.add_paragraph(line.strip())
        i += 1


def build_docx(text: str, *, summary_only: bool = False) -> bytes:
    """解析の結果のテキストから Word（.docx）を作る。summary_only なら要約の章だけ。"""
    from docx import Document

    doc = Document()
    _set_font(doc)
    head, sections = _split_sections(text)
    title = next((h.split("：", 1)[1] for h in head if h.startswith("動画：")), "動画の解析")
    doc.add_heading(f"{title}（{'要約' if summary_only else '詳細'}）", level=0)
    for line in head:
        if line.strip() and not line.startswith("動画："):
            doc.add_paragraph(line.strip())
    for name, lines in sections:
        if summary_only and name not in SUMMARY_SECTIONS:
            continue
        doc.add_heading(name, level=1)
        _add_lines(doc, lines)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()
