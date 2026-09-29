#!/usr/bin/env bash
# CRM の商談記録に対して、録音後の処理（文字起こし・要約）まで動く録音URLを発行する（Cloud Shell で実行する想定）。
# CRM への書き込みはバックエンドの DRY_RUN に従う（DRY_RUN=true の間は書き込まない）。
#
#   cd ~/voice-app && bash scripts/issue_process_url.sh <商談記録の ID>
#
# - バックエンド（Cloud Run）の設定から、バックエンドと同じ署名鍵を読む（通常／リージョン シークレットを取り違えない）
# - 発行した URL をバックエンドに問い合わせ、受け付けられることを確かめてから表示する
# - スマホのカメラで読めるよう QR コードも表示する（表示できないときは URL だけ）
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT="${PROJECT:-voice-ai-510014}"
REGION="asia-northeast1"
SERVICE="meeting-notes"
RECORD_ID="${1:-}"
fail() {
  printf 'エラー: %s\n' "$*" >&2
  exit 1
}
[[ "$RECORD_ID" =~ ^[0-9]+$ ]] || fail "使い方: bash scripts/issue_process_url.sh <商談記録の ID（数字）>"

SVC_JSON="$(gcloud run services describe "$SERVICE" --project="$PROJECT" --region="$REGION" --format=json)"
read -r SERVICE_URL SECRET_REF DIRECT < <(SVC_JSON="$SVC_JSON" python3 -c '
import json, os
svc = json.loads(os.environ["SVC_JSON"])
env = {e.get("name"): e for e in svc["spec"]["template"]["spec"]["containers"][0].get("env", [])}
ref = (env.get("RECORDING_TOKEN_SECRET_REF") or {}).get("value") or "-"
print(svc["status"]["url"], ref, "yes" if "RECORDING_TOKEN_SECRET" in env else "no")
')
[ "$DIRECT" = no ] || fail "Cloud Run に RECORDING_TOKEN_SECRET が直接設定されています。この画面を送ってください"
[[ "$SECRET_REF" == sm:* ]] || fail "Cloud Run の RECORDING_TOKEN_SECRET_REF が sm: 参照ではありません（${SECRET_REF}）"

# sm:projects/<p>/secrets/<name>/versions/<v>  か  sm:projects/<p>/locations/<loc>/secrets/<name>/versions/<v>
REF="${SECRET_REF#sm:}"
NAME="$(sed -n 's#.*/secrets/\([^/]*\)/versions/.*#\1#p' <<<"$REF")"
VERSION="${REF##*/versions/}"
LOCATION="$(sed -n 's#^projects/[^/]*/locations/\([^/]*\)/secrets/.*#\1#p' <<<"$REF")"
LOC_FLAG=()
[ -z "$LOCATION" ] || LOC_FLAG=(--location="$LOCATION")
SECRET="$(gcloud secrets versions access "$VERSION" --secret="$NAME" --project="$PROJECT" "${LOC_FLAG[@]}")"

URL="$(RECORDING_TOKEN_SECRET="$SECRET" python3 scripts/issue_test_url.py --service-url "$SERVICE_URL" \
  --record-id "$RECORD_ID" --process 2>/dev/null)"
unset SECRET

# バックエンドがこの URL を受け付けるか（読み取りだけの問い合わせ）
BODY="$(mktemp)"
CODE="$(curl -sS -o "$BODY" -w '%{http_code}' -H "Authorization: Bearer ${URL#*#}" \
  "${SERVICE_URL}/api/recordings/${RECORD_ID}/session" || true)"
if [ "$CODE" != 200 ]; then
  fail "バックエンドがこの URL を受け付けません（HTTP ${CODE:-000}）: $(cat "$BODY")"
fi
rm -f "$BODY"

show_qr() {
  if command -v qrencode >/dev/null 2>&1; then
    qrencode -t ANSIUTF8 "$1"
    return
  fi
  local venv="${TMPDIR:-/tmp}/meeting-notes-qr"
  if ! "$venv/bin/python" -c 'import qrcode' >/dev/null 2>&1; then
    python3 -m venv "$venv" >/dev/null 2>&1 && "$venv/bin/pip" install -q qrcode >/dev/null 2>&1 || return 1
  fi
  "$venv/bin/python" 2>/dev/null -c '
import sys, qrcode
qr = qrcode.QRCode(border=2)
qr.add_data(sys.argv[1])
qr.make(fit=True)
qr.print_ascii(invert=True)
' "$1"
}

echo "バックエンドで確認済みの録音URL（商談記録 ${RECORD_ID}、24時間有効）。"
echo "スマホのカメラで QR コードを読むか、下の URL を最後の文字まで送って開いてください。"
echo
show_qr "$URL" || echo "（QR コードは表示できませんでした）"
echo
echo "$URL"
