#!/usr/bin/env bash
# Gemini の使用量（トークン）と、処理した件数を集計して、費用の目安を出す（読むだけ）。
# 出すのは件数・トークン数・金額だけ（商談の中身・会社名は出さない。GitHub Actions のログは公開されるため）。
#
#   bash scripts/usage_report.sh [時間（既定 24）]
#
# 単価は 2026-10 の請求の実績（100万トークンあたり 入力 約260円・出力 約1,550円。考えた分は出力として課金）。
set -euo pipefail
PROJECT="${PROJECT:-voice-ai-510014}"
HOURS="${1:-24}"
[[ "$HOURS" =~ ^[0-9]{1,4}$ ]] || { echo "使い方: bash scripts/usage_report.sh [時間]" >&2; exit 1; }

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
gcloud logging read "resource.type=\"cloud_run_revision\" AND (jsonPayload.message=\"gemini.generated\" OR jsonPayload.message=\"pipeline.saved\" OR jsonPayload.message=\"needs.done\" OR jsonPayload.message=\"correct.skipped\" OR jsonPayload.message=\"transcribe.split\" OR jsonPayload.message=\"transcribe.truncated_kept\" OR jsonPayload.message=\"video.done\" OR jsonPayload.message=\"video.failed\" OR jsonPayload.message=\"video.split\")" \
  --project="$PROJECT" --freshness="${HOURS}h" --limit=5000 --format=json >"$TMP"
python3 - "$TMP" "$HOURS" <<'PY'
import json
import sys
from collections import defaultdict

rows = json.load(open(sys.argv[1], encoding="utf-8"))
hours = sys.argv[2]
tasks = defaultdict(lambda: [0, 0, 0, 0])
events = defaultdict(int)
saved = defaultdict(int)
for r in rows:
    p = r.get("jsonPayload") or {}
    msg = p.get("message")
    if msg == "gemini.generated":
        t = tasks[p.get("task") or "?"]
        t[0] += 1
        t[1] += int(p.get("input_tokens") or 0)
        t[2] += int(p.get("output_tokens") or 0)
        t[3] += int(p.get("thinking_tokens") or 0)
    elif msg == "pipeline.saved":
        saved[p.get("client_id") or "?"] += 1
    elif msg:
        events[msg] += 1
IN, OUT = 260 / 1e6, 1550 / 1e6
print(f"直近 {hours} 時間")
print("処理 | 回数 | 入力 | 出力 | 考えた分 | 金額（円）")
total = 0.0
for name, (n, i, o, th) in sorted(tasks.items()):
    yen = i * IN + (o + th) * OUT
    total += yen
    print(f"{name} | {n} | {i:,} | {o:,} | {th:,} | {yen:.1f}")
print(f"Gemini の合計：約 {total:.0f} 円（目安）")
print("商談記録の更新：" + ("、".join(f"{c} {n}件" for c, n in sorted(saved.items())) or "なし"))
for name, n in sorted(events.items()):
    print(f"{name}: {n}件")
PY
