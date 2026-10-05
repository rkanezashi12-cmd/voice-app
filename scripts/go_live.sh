#!/usr/bin/env bash
# 本番運用に切り替える：バックエンドが作る商談記録の名前に【TEST】を付けないようにする（CRM_TEST_RECORDS=false）。
# Cloud Shell で実行する想定。手順は docs/go-live.md（先に CRM の画面でテストの記録を消す）。
#
#   cd ~/voice-app && git pull && bash scripts/go_live.sh          # 本番運用に切り替える
#   cd ~/voice-app && bash scripts/go_live.sh --test               # テスト運用に戻す（【TEST】を付ける）
#
# 変えるのは Cloud Run の環境変数 CRM_TEST_RECORDS だけ（DRY_RUN・クライアント設定などは変えない）。CRM には書き込まない。
set -euo pipefail
cd "$(dirname "$0")/.."
# Ctrl+Z（一時停止）を効かなくする。「元に戻す」のつもりで押すと、スクリプトが止まったままになって分かりにくいため
trap '' TSTP

PROJECT="${PROJECT:-voice-ai-510014}"
REGION="asia-northeast1"
SERVICE="meeting-notes"

fail() {
  printf 'エラー: %s\n' "$*" >&2
  exit 1
}
# yes で進む・no で false を返す。それ以外（空の Enter など）は聞き直す
yes_no() {
  local answer=""
  while true; do
    read -rp "$1" answer || fail "入力が終わりました（中止します）"
    case "$answer" in
      yes) return 0 ;;
      no) return 1 ;;
    esac
  done
}

case "${1:-}" in
  "")
    TARGET=false
    LABEL="本番運用（商談記録の名前に【TEST】を付けない）"
    ;;
  --test)
    TARGET=true
    LABEL="テスト運用（商談記録の名前の先頭に【TEST】を付ける）"
    ;;
  *) fail "使い方: bash scripts/go_live.sh（本番運用にする）／ bash scripts/go_live.sh --test（テスト運用に戻す）" ;;
esac
[ $# -le 1 ] || fail "使い方: bash scripts/go_live.sh [--test]"

gcloud config set project "$PROJECT" >/dev/null
SVC_JSON="$(gcloud run services describe "$SERVICE" --region="$REGION" --format=json 2>/dev/null)" ||
  fail "Cloud Run のサービス ${SERVICE} が見つかりません"
# 1行目：DRY_RUN、2行目：CRM_TEST_RECORDS、3行目：サービス URL（アプリの既定は DRY_RUN=true・CRM_TEST_RECORDS=true）
mapfile -t CURRENT < <(SVC_JSON="$SVC_JSON" python3 -c '
import json, os
svc = json.loads(os.environ["SVC_JSON"])
env = {e.get("name"): e.get("value") for e in svc["spec"]["template"]["spec"]["containers"][0].get("env", [])}
def flag(name):
    return "false" if str(env.get(name, "true")).strip().lower() in ("false", "0", "no", "off") else "true"
print(flag("DRY_RUN"))
print(flag("CRM_TEST_RECORDS"))
print(env.get("SERVICE_URL") or svc.get("status", {}).get("url") or "")
')
DRY_RUN_NOW="${CURRENT[0]:-true}"
TEST_RECORDS_NOW="${CURRENT[1]:-true}"
SERVICE_URL="${CURRENT[2]:-}"

echo "今の設定: DRY_RUN=${DRY_RUN_NOW}（true なら CRM に書き込まない）/ CRM_TEST_RECORDS=${TEST_RECORDS_NOW}（true なら名前に【TEST】）"
if [ "$DRY_RUN_NOW" = true ]; then
  echo "注意: DRY_RUN=true のままです（CRM に書き込みません）。このスクリプトは DRY_RUN を変えません。"
fi
if [ "$TEST_RECORDS_NOW" = "$TARGET" ]; then
  echo "すでに${LABEL}です。何も変えません。"
  exit 0
fi
if [ "$TARGET" = false ]; then
  echo "切り替える前に、CRM の画面でテストの記録（名前が【TEST】で始まる商談記録）を消してください（docs/go-live.md の手順1）。"
fi
yes_no "${LABEL}に切り替えるなら yes、やめるなら no: " || fail "中止しました。何も変えていません。"

# 環境変数を1つ変えて新しい版を出す（コードとほかの設定はそのまま）
gcloud run services update "$SERVICE" --region="$REGION" --quiet --update-env-vars="CRM_TEST_RECORDS=${TARGET}"
[ -z "$SERVICE_URL" ] || curl -fsS "${SERVICE_URL%/}/health"
echo
echo "${LABEL}に切り替えました。"
if [ "$TARGET" = false ]; then
  echo "録音アプリで1件録って、商談記録の名前に【TEST】が付かないことを確かめてください。戻すときは bash scripts/go_live.sh --test。"
fi
