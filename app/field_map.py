"""CRM の項目マッピング。

CRM の API 名・選択肢の表示値はすべてここに集約し、コードに直書きしない。
下の既定値は仮置き（設計書の論理名から作ったもの）。実物の CRM に合わせてここを直すか、
クライアント設定 JSON の "field_map" で上書きする（例は config/clients.example.json）。

実物との突き合わせは Step 4 の検証スクリプトで行う。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class MeetingRecordFields(_Frozen):
    """カスタムモジュール「商談記録」。"""

    module: str = "MeetingRecords"
    name: str = "Name"  # レコード名（作成時に必須）
    meeting_type: str = "Meeting_Type"
    capture_method: str = "Capture_Method"
    account: str = "Account"  # 取引先ルックアップ
    deal: str = "Deal"  # 商談ルックアップ
    contact_name: str = "Contact_Name"
    owner: str = "Owner"
    start_at: str = "Start_At"
    meeting_url: str = "Meeting_URL"
    recall_id: str = (
        "Recall_ID"  # bot_id または desktop upload id（デスクトップの upsert キー。一意項目にする）
    )
    recording_url: str = "Recording_URL"
    status: str = "Status"
    error_message: str = "Error_Message"
    summary: str = "Summary"
    issues: str = "Issues"
    needs: str = "Needs"
    next_actions: str = "Next_Actions"
    due_date: str = "Due_Date"
    category: str = "Category"
    competitors: str = "Competitors"
    budget: str = "Budget"
    decision_maker: str = "Decision_Maker"
    # 文字起こし全文（複数行・プレーンテキスト（大）を2つ。1つ目に入りきらない分を2つ目へ）
    transcript: str = "Transcript"
    transcript_2: str = "Transcript_2"


class GlossaryFields(_Frozen):
    """カスタムモジュール「用語辞書」。"""

    module: str = "Glossary"
    term: str = "Name"  # 標準のレコード名項目（表示名を「用語」に変える）
    misrecognitions: str = "Misrecognitions"
    term_type: str = "Term_Type"


class StandardFields(_Frozen):
    """標準モジュール（取引先候補の検索に使う）。"""

    contacts_module: str = "Contacts"
    contact_full_name: str = "Full_Name"
    contact_email: str = "Email"
    contact_account: str = "Account_Name"
    account_name: str = "Account_Name"


class StatusValues(_Frozen):
    """状態（選択リスト）の表示値。"""

    reserved: str = "予約済"
    joining: str = "参加中"
    waiting: str = "参加待ち"
    join_failed: str = "参加失敗"
    recording: str = "録音中"
    transcribing: str = "文字起こし中"
    done: str = "完了"
    failed: str = "失敗"
    no_account: str = "取引先未設定"


class MeetingTypeValues(_Frozen):
    in_person: str = "対面"
    online: str = "オンライン"


class CaptureMethodValues(_Frozen):
    desktop: str = "デスクトップ"
    bot: str = "ボット"
    in_person: str = "対面録音"


class TextLimits(_Frozen):
    """項目の最大文字数。超えた分は切り詰めて末尾に「…」を付ける。

    既定は scripts/crm_setup.py で作った項目の実物に合わせてある（2026-09 確認）：
    複数行（大）= 32,000、複数行（小）= 2,000、1行テキスト = 255、名前 = 120（URL は 450 で、切り詰めていない）。
    """

    summary: int = 32000
    issues: int = 32000
    needs: int = 32000
    next_actions: int = 32000
    competitors: int = 32000
    budget: int = 255
    decision_maker: int = 255
    contact_name: int = 255
    error_message: int = 2000
    name: int = 120
    # 文字起こし全文の各項目の上限（合計 2 倍を超えた分は末尾を切る）
    transcript: int = 32000


class FieldMap(_Frozen):
    meeting_record: MeetingRecordFields = MeetingRecordFields()
    glossary: GlossaryFields = GlossaryFields()
    standard: StandardFields = StandardFields()
    status: StatusValues = StatusValues()
    meeting_type: MeetingTypeValues = MeetingTypeValues()
    capture_method: CaptureMethodValues = CaptureMethodValues()
    limits: TextLimits = TextLimits()
    # category の選択肢。空なら自由記述（Gemini の出力をそのまま入れる）。
    # 値を入れると Gemini の出力をこの中に限定し、外れた値は null にする。
    category_choices: tuple[str, ...] = ()


# Recall.ai のボット状態コード → CRM に書く状態（StatusValues の属性名）。None は書かない。
# クレジット節約のため、一時的な状態（参加中など）は書かない。
BOT_EVENT_STATUS: dict[str, str | None] = {
    "joining_call": None,
    "in_waiting_room": "waiting",
    "in_call_not_recording": None,
    "recording_permission_allowed": None,
    "recording_permission_denied": "failed",
    "in_call_recording": "recording",
    "call_ended": None,
    "done": None,
    "fatal": "failed",
}

# 参加できなかったことを示す sub_code に含まれる語（該当すれば「参加失敗」にする）
# Recall.ai の sub_code の正確な一覧は docs/unverified-apis.md の確認項目
JOIN_FAILURE_SUBCODE_HINTS: tuple[str, ...] = (
    "waiting_room",
    "noone_joined",
    "no_one_joined",
    "denied",
    "kicked",
    "not_admitted",
    "meeting_not_started",
    "obf",
    "authorized_user",
    "invalid_meeting",
    "meeting_not_found",
    "password",
)

DEFAULT_FIELD_MAP = FieldMap()


def build_field_map(overrides: dict[str, Any] | None) -> FieldMap:
    """クライアント設定の field_map（部分指定）を既定値に重ねる。"""
    if not overrides:
        return DEFAULT_FIELD_MAP
    merged: dict[str, Any] = DEFAULT_FIELD_MAP.model_dump()
    for key, value in overrides.items():
        if key not in merged:
            raise ValueError(f"field_map に不明なキーがあります: {key}")
        if isinstance(value, dict) and isinstance(merged[key], dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return FieldMap.model_validate(merged)
