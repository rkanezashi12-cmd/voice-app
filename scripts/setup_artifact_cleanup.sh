#!/usr/bin/env bash
# デプロイのたびに Artifact Registry に溜まる「プログラムの古い版」を自動で消す設定（クリーンアップポリシー）を入れる。
# Cloud Shell で一度実行すれば終わり（何度実行しても同じ設定になる）。手順は README の「1-3. デプロイ」。
#
#   cd ~/voice-app && git pull && bash scripts/setup_artifact_cleanup.sh
#
# - 新しいほうから KEEP_VERSIONS 版（既定 5）は必ず残す。今動いている版も含まれ、この範囲なら昔の版に戻せる
# - それより古く、作ってから 7日を過ぎた版を消す。消すのは Artifact Registry が1日に1回ほど自動で行う
# - 無料枠（0.5GB）の中に収めるため。データ（CRM の記録・音声）には関係しない。ソースコードは GitHub にあり、
#   消した版もデプロイし直せば作れる
set -euo pipefail
cd "$(dirname "$0")/.."
# Ctrl+Z（一時停止）を効かなくする。「元に戻す」のつもりで押すと、スクリプトが止まったままになって分かりにくいため
trap '' TSTP

PROJECT="${PROJECT:-voice-ai-510014}"
REGION="asia-northeast1"
# gcloud run deploy --source（scripts/deploy.sh）が作るリポジトリ
REPO="${REPO:-cloud-run-source-deploy}"
KEEP_VERSIONS="${KEEP_VERSIONS:-5}"
DELETE_AFTER_DAYS=7

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

[ $# -eq 0 ] || fail "使い方: bash scripts/setup_artifact_cleanup.sh（引数は要りません）"
[[ "$KEEP_VERSIONS" =~ ^[1-9][0-9]*$ ]] || fail "KEEP_VERSIONS は 1 以上の数にしてください（今の値: ${KEEP_VERSIONS}）"

gcloud config set project "$PROJECT" >/dev/null
SIZE_BYTES="$(gcloud artifacts repositories describe "$REPO" --project="$PROJECT" --location="$REGION" \
  --format='value(sizeBytes)' 2>/dev/null)" ||
  fail "リポジトリ ${REPO}（${REGION}）が見つかりません。先に bash scripts/deploy.sh でデプロイしてください"

if [[ "$SIZE_BYTES" =~ ^[0-9]+$ ]]; then
  echo "リポジトリ: ${REPO}（${REGION}）。今の大きさ: 約 $((SIZE_BYTES / 1024 / 1024)) MB（無料枠は 0.5GB＝約 512 MB）"
else
  echo "リポジトリ: ${REPO}（${REGION}）"
fi
echo "設定する内容: 新しいほうから ${KEEP_VERSIONS} 版は残し、それより古く ${DELETE_AFTER_DAYS} 日を過ぎた版を消す（1日に1回ほど自動で）"
yes_no "設定するなら yes、やめるなら no: " || fail "中止しました。何も変えていません。"

POLICY="$(mktemp)"
trap 'rm -f "$POLICY"' EXIT
# 残す（Keep）と消す（Delete）の両方に当てはまる版は残る（Keep が優先）
cat >"$POLICY" <<JSON
[
  {
    "name": "keep-recent",
    "action": {"type": "Keep"},
    "mostRecentVersions": {"keepCount": ${KEEP_VERSIONS}}
  },
  {
    "name": "delete-old",
    "action": {"type": "Delete"},
    "condition": {"olderThan": "$((DELETE_AFTER_DAYS * 86400))s"}
  }
]
JSON
gcloud artifacts repositories set-cleanup-policies "$REPO" --project="$PROJECT" --location="$REGION" \
  --policy="$POLICY" --no-dry-run >/dev/null
echo "設定しました。今の設定:"
gcloud artifacts repositories list-cleanup-policies "$REPO" --project="$PROJECT" --location="$REGION"
echo
echo "古い版は1日ほどのうちに消えます。大きさは GCP コンソールの「Artifact Registry」→ ${REPO} で見られます。"
