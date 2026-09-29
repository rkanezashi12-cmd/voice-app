#!/usr/bin/env bash
# バックエンド（Cloud Run）が使っている Zoho の接続情報のままで、CRM に読み取りの問い合わせをして結果を出す
# （Cloud Shell で実行する想定）。バックエンドだけが CRM の読み取りに失敗するときの切り分けに使う。
# 表示するのは組織・ユーザー・各問い合わせの成否とエラーコードだけ。レコードの中身とトークンは出さない。
#
#   cd ~/voice-app && bash scripts/check_backend_zoho.sh <商談記録の ID>
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT="${PROJECT:-voice-ai-510014}"
REGION="asia-northeast1"
SERVICE="meeting-notes"
APP_CLIENT="${APP_CLIENT:-default}"
RECORD_ID="${1:-}"
fail() {
  printf 'エラー: %s\n' "$*" >&2
  exit 1
}
[[ "$RECORD_ID" =~ ^[0-9]+$ ]] || fail "使い方: bash scripts/check_backend_zoho.sh <商談記録の ID（数字）>"

# sm:projects/<p>/secrets/<name>/versions/<v>  か  sm:projects/<p>/locations/<loc>/secrets/<name>/versions/<v>
access_ref() {
  local ref="${1#sm:}" name version location
  name="$(sed -n 's#.*/secrets/\([^/]*\)/versions/.*#\1#p' <<<"$ref")"
  version="${ref##*/versions/}"
  location="$(sed -n 's#^projects/[^/]*/locations/\([^/]*\)/secrets/.*#\1#p' <<<"$ref")"
  if [ -n "$location" ]; then
    gcloud secrets versions access "$version" --secret="$name" --project="$PROJECT" --location="$location"
  else
    gcloud secrets versions access "$version" --secret="$name" --project="$PROJECT"
  fi
}

SVC_JSON="$(gcloud run services describe "$SERVICE" --project="$PROJECT" --region="$REGION" --format=json)"
read -r DC ID_REF SECRET_REF TOKEN_REF < <(SVC_JSON="$SVC_JSON" python3 -c '
import json, os, sys
svc = json.loads(os.environ["SVC_JSON"])
env = {e.get("name"): e.get("value") for e in svc["spec"]["template"]["spec"]["containers"][0].get("env", [])}
zoho = json.loads(env.get("CLIENTS_CONFIG_JSON") or "{}").get("clients", {}).get(sys.argv[1], {}).get("zoho") or {}
print(zoho.get("dc", "us"), zoho.get("client_id", "-"), zoho.get("client_secret", "-"), zoho.get("refresh_token", "-"))
' "$APP_CLIENT")
[[ "$ID_REF" == sm:* && "$SECRET_REF" == sm:* && "$TOKEN_REF" == sm:* ]] ||
  fail "Cloud Run のクライアント設定（${APP_CLIENT}）に zoho の sm: 参照がありません"
case "$DC" in
  us) export ZOHO_DC=com ;;
  au) export ZOHO_DC=com.au ;;
  *) export ZOHO_DC="$DC" ;;
esac
ZOHO_CLIENT_ID="$(access_ref "$ID_REF")"
ZOHO_CLIENT_SECRET="$(access_ref "$SECRET_REF")"
ZOHO_REFRESH_TOKEN="$(access_ref "$TOKEN_REF")"
export ZOHO_CLIENT_ID ZOHO_CLIENT_SECRET ZOHO_REFRESH_TOKEN

echo "バックエンドと同じ接続情報（DC ${DC}）で、読み取りだけを試します:"
python3 scripts/crm_setup.py diagnose "$RECORD_ID"
