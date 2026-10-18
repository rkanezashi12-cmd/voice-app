#!/usr/bin/env bash
# 2つ目以降のクライアントだけ、CRM に書き込むか（dry_run）・商談記録の名前に【TEST】を付けるか（crm_test_records）を切り替える
# （Cloud Shell で実行する想定）。default（マルサン木型）はサービス全体の DRY_RUN・CRM_TEST_RECORDS のままで、このスクリプトでは変えない。
#
#   cd ~/voice-app && git pull && APP_CLIENT=monodemo bash scripts/client_mode.sh            # 今の状態を表示するだけ
#   APP_CLIENT=monodemo bash scripts/client_mode.sh --write      # CRM に書き込む（dry_run=false）。ユーザーの指示があるときだけ
#   APP_CLIENT=monodemo bash scripts/client_mode.sh --dry-run    # CRM に書き込まない（dry_run=true）に戻す
#   APP_CLIENT=monodemo bash scripts/client_mode.sh --live       # 【TEST】を付けない（本番運用。docs/go-live.md）
#   APP_CLIENT=monodemo bash scripts/client_mode.sh --test       # 【TEST】を付ける
#   APP_CLIENT=monodemo bash scripts/client_mode.sh --needs-on   # ニーズ分析を使えるようにする（docs/needs-analysis.md）
#   APP_CLIENT=monodemo bash scripts/client_mode.sh --needs-off  # ニーズ分析を使えなくする
#
# 変えるのは Cloud Run の CLIENTS_CONFIG_JSON のそのクライアントの dry_run / crm_test_records / features.needs_analysis だけ。
# CRM には書き込まない。
set -euo pipefail
cd "$(dirname "$0")/.."
# Ctrl+Z（一時停止）を効かなくする。「元に戻す」のつもりで押すと、スクリプトが止まったままになって分かりにくいため
trap '' TSTP

PROJECT="${PROJECT:-voice-ai-510014}"
REGION="asia-northeast1"
SERVICE="meeting-notes"
APP_CLIENT="${APP_CLIENT:-}"

fail() {
  printf 'エラー: %s\n' "$*" >&2
  exit 1
}
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

USAGE="使い方: APP_CLIENT=<クライアント> bash scripts/client_mode.sh [--write | --dry-run | --live | --test | --needs-on | --needs-off]"
[ -n "$APP_CLIENT" ] || fail "$USAGE"
[ "$APP_CLIENT" != default ] ||
  fail "default（マルサン木型）はサービス全体の設定のままにします（このスクリプトでは変えません。本番運用は scripts/go_live.sh）"
[[ "$APP_CLIENT" =~ ^[a-z0-9][a-z0-9-]{0,31}$ ]] || fail "APP_CLIENT は英小文字・数字・ハイフン（32文字まで）にしてください"
[ $# -le 1 ] || fail "$USAGE"
KEY=""
VALUE=""
case "${1:-}" in
  "") ;;
  --write) KEY=dry_run VALUE=false LABEL="CRM に書き込む（dry_run=false）" ;;
  --dry-run) KEY=dry_run VALUE=true LABEL="CRM に書き込まない（dry_run=true）" ;;
  --live) KEY=crm_test_records VALUE=false LABEL="商談記録の名前に【TEST】を付けない（本番運用）" ;;
  --test) KEY=crm_test_records VALUE=true LABEL="商談記録の名前の先頭に【TEST】を付ける" ;;
  --needs-on) KEY=features.needs_analysis VALUE=true LABEL="ニーズ分析を使う" ;;
  --needs-off) KEY=features.needs_analysis VALUE=false LABEL="ニーズ分析を使わない" ;;
  *) fail "$USAGE" ;;
esac

gcloud config set project "$PROJECT" >/dev/null
SVC_JSON="$(gcloud run services describe "$SERVICE" --region="$REGION" --format=json 2>/dev/null)" ||
  fail "Cloud Run のサービス ${SERVICE} が見つかりません"
# 1行目：環境変数に渡す形（ASCII）、2行目：今の状態の説明、3行目：変えたあとの状態の説明、4行目：変わるなら changed
CONFIG_OUT="$(SVC_JSON="$SVC_JSON" python3 - "$APP_CLIENT" "$KEY" "$VALUE" <<'PY'
import json
import os
import sys

app_client, key, value = sys.argv[1:4]
svc = json.loads(os.environ["SVC_JSON"])
env = {e.get("name"): e.get("value") for e in svc["spec"]["template"]["spec"]["containers"][0].get("env", [])}
clients = json.loads(env.get("CLIENTS_CONFIG_JSON") or "{}")
client = clients.get("clients", {}).get(app_client)
if client is None:
    sys.exit(3)


def service_bool(name: str) -> bool:
    # アプリの既定は DRY_RUN=true・CRM_TEST_RECORDS=true
    return (env.get(name) or "true").strip().lower() in ("1", "true", "yes", "on")


def describe(c: dict) -> str:
    dry = c.get("dry_run")
    test = c.get("crm_test_records")
    dry = service_bool("DRY_RUN") if dry is None else dry
    test = service_bool("CRM_TEST_RECORDS") if test is None else test
    needs = bool((c.get("features") or {}).get("needs_analysis"))
    return (
        f"CRM に{'書き込まない（dry_run）' if dry else '書き込む'}・【TEST】を{'付ける' if test else '付けない'}"
        f"・ニーズ分析を{'使う' if needs else '使わない'}"
    )


before = describe(client)
target, name, current = client, key, client.get(key)
if "." in key:
    # features.needs_analysis のような入れ子の設定（無ければ false）
    head, name = key.split(".", 1)
    current = bool((client.get(head) or {}).get(name))
changed = bool(key) and current is not (value == "true")
if changed:
    if "." in key:
        target = client.setdefault(head, {})
    target[name] = value == "true"
print(json.dumps(clients, separators=(",", ":")))
print(before)
print(describe(client))
print("changed" if changed else "same")
PY
)" || fail "クライアント ${APP_CLIENT} がクライアント設定にありません（先に APP_CLIENT=${APP_CLIENT} bash scripts/setup_client.sh）"
NEW_CLIENTS="$(sed -n 1p <<<"$CONFIG_OUT")"
echo "クライアント ${APP_CLIENT} の今の状態: $(sed -n 2p <<<"$CONFIG_OUT")"
[ -n "$KEY" ] || exit 0
if [ "$(sed -n 4p <<<"$CONFIG_OUT")" != changed ]; then
  echo "すでに「${LABEL}」です。何も変えていません。"
  exit 0
fi
echo "変えたあと: $(sed -n 3p <<<"$CONFIG_OUT")"
yes_no "クライアント ${APP_CLIENT} を「${LABEL}」にします。よろしければ yes、やめるなら no: " ||
  fail "中止しました。何も変えていません。"
# JSON にカンマが含まれるので、区切り文字を @@ に変えて渡す
gcloud run services update "$SERVICE" --region="$REGION" --quiet \
  --update-env-vars="^@@^CLIENTS_CONFIG_JSON=${NEW_CLIENTS}@@CLIENTS_UPDATED_AT=$(date -u +%Y%m%dT%H%M%SZ)"
echo "クライアント ${APP_CLIENT} を「${LABEL}」にしました（ほかのクライアントは変えていません）。"
