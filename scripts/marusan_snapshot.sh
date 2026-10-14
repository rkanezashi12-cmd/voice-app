#!/usr/bin/env bash
# マルサン木型（クライアント default）の設定が変わっていないかを確かめる（Cloud Shell で実行する想定。読み取りだけ）。
#
#   cd ~/voice-app && bash scripts/marusan_snapshot.sh save    # 作業の前に1回：今の状態を ~/marusan-snapshot.json に控える
#   cd ~/voice-app && bash scripts/marusan_snapshot.sh check   # 各手順のあと：控えと比べ、違いがあれば項目名を出して止まる
#
# 控えるもの（値そのものは控えない。シークレットは版の番号だけ）:
#   - Cloud Run のクライアント設定（CLIENTS_CONFIG_JSON）の default の中身（シークレットは参照だけなので値は入っていない）
#   - サービス全体の DRY_RUN・CRM_TEST_RECORDS
#   - default が使うシークレットの、有効な最新の版の番号（新しい版が足されたら気づける）
#   - 地図の API キー（app-geocoding で始まるもの）の ID
# Cloud Run・Secret Manager・CRM には何も書き込まない。
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT="${PROJECT:-voice-ai-510014}"
REGION="asia-northeast1"
SERVICE="meeting-notes"
SNAPSHOT="${SNAPSHOT:-$HOME/marusan-snapshot.json}"
SECRETS=(backend-api-key recording-token-secret zoho-client-id zoho-client-secret zoho-refresh-token
  recall-api-key recall-webhook-secret app-login-client-id app-login-client-secret app-session-secret app-maps-api-key)

fail() {
  printf 'エラー: %s\n' "$*" >&2
  exit 1
}
MODE="${1:-}"
[ "$MODE" = save ] || [ "$MODE" = check ] || fail "使い方: bash scripts/marusan_snapshot.sh save（控える）／ check（比べる）"

gcloud config set project "$PROJECT" >/dev/null
if gcloud secrets describe recording-token-secret >/dev/null 2>&1; then
  LOC_FLAG=()
elif gcloud secrets describe recording-token-secret --location="$REGION" >/dev/null 2>&1; then
  LOC_FLAG=(--location="$REGION")
else
  fail "シークレット recording-token-secret が見つかりません"
fi
SVC_JSON="$(gcloud run services describe "$SERVICE" --region="$REGION" --format=json 2>/dev/null)" ||
  fail "Cloud Run のサービス ${SERVICE} が見つかりません"

# シークレットごとに「名前<TAB>有効な最新の版」（無ければ「なし」）
VERSIONS=""
for s in "${SECRETS[@]}"; do
  if gcloud secrets describe "$s" "${LOC_FLAG[@]}" >/dev/null 2>&1; then
    v="$(gcloud secrets versions list "$s" "${LOC_FLAG[@]}" --filter='state=ENABLED' \
      --sort-by=~createTime --limit=1 --format='value(name)')"
    VERSIONS+="${s}"$'\t'"${v##*/}"$'\n'
  else
    VERSIONS+="${s}"$'\t'"なし"$'\n'
  fi
done
MAPS_KEYS="$(gcloud services api-keys list --project="$PROJECT" --format='value(name)' 2>/dev/null |
  sed 's|.*/||' | grep -E '^app-geocoding(-|$)' | sort || true)"

CURRENT="$(SVC_JSON="$SVC_JSON" VERSIONS="$VERSIONS" MAPS_KEYS="$MAPS_KEYS" python3 -c '
import json, os
svc = json.loads(os.environ["SVC_JSON"])
env = {e.get("name"): e.get("value") for e in svc["spec"]["template"]["spec"]["containers"][0].get("env", [])}
clients = json.loads(env.get("CLIENTS_CONFIG_JSON") or "{}").get("clients", {})
versions = dict(line.split("\t", 1) for line in os.environ["VERSIONS"].splitlines() if line)
snapshot = {
    "default_client": clients.get("default"),
    "DRY_RUN": env.get("DRY_RUN"),
    "CRM_TEST_RECORDS": env.get("CRM_TEST_RECORDS"),
    "secret_versions": versions,
    "maps_keys": [k for k in os.environ["MAPS_KEYS"].splitlines() if k],
}
print(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=1))
')"

if [ "$MODE" = save ]; then
  printf '%s\n' "$CURRENT" >"$SNAPSHOT"
  echo "マルサン木型（default）の今の状態を控えました: ${SNAPSHOT}（値そのものは入っていません）"
  exit 0
fi

[ -f "$SNAPSHOT" ] || fail "控えがありません。作業の前に bash scripts/marusan_snapshot.sh save を実行してください"
DIFF="$(SAVED="$(cat "$SNAPSHOT")" CURRENT="$CURRENT" python3 -c '
import json, os
saved, current = json.loads(os.environ["SAVED"]), json.loads(os.environ["CURRENT"])
names = {"default_client": "クライアント設定の default", "DRY_RUN": "DRY_RUN", "CRM_TEST_RECORDS": "CRM_TEST_RECORDS",
         "maps_keys": "地図の API キー"}
for key in sorted(set(saved) | set(current)):
    if key == "secret_versions":
        a, b = saved.get(key, {}), current.get(key, {})
        for s in sorted(set(a) | set(b)):
            if a.get(s) != b.get(s):
                print(f"シークレット {s} の版: {a.get(s)} → {b.get(s)}")
    elif saved.get(key) != current.get(key):
        print(f"{names.get(key, key)} が変わっています")
')"
if [ -n "$DIFF" ]; then
  printf '%s\n' "$DIFF"
  fail "マルサン木型（default）の設定が控えと違います。作業を止めて、この表示を送ってください"
fi
echo "OK：マルサン木型（default）の設定・シークレット・DRY_RUN は、控えと同じです（${SNAPSHOT}）。"
