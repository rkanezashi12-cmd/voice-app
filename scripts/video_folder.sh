#!/usr/bin/env bash
# 動画を置く場所（GCS の videos/<クライアント>/）の、Cloud Console の画面の URL を出す（Cloud Shell で実行。読むだけ）。
# その画面の「ファイルをアップロード」で動画を置く。置いた動画は1日で自動で消える（バケットのライフサイクル）ので、
# 置いた日のうちに解析する（docs/video-analysis.md）。
#
#   cd ~/voice-app && bash scripts/video_folder.sh monodemo
set -euo pipefail

# 接続先の GCP（PROJECT・REGION・SERVICE・RUN_SA・TASKS_SA）は deploy/ops.env から読む
# shellcheck source=scripts/lib/env.sh
source "$(dirname "$0")/lib/env.sh"
CLIENT_ID="${1:-}"
fail() {
  printf 'エラー: %s\n' "$*" >&2
  exit 1
}
[[ "$CLIENT_ID" =~ ^[a-z0-9][a-z0-9-]{0,31}$ ]] || fail "使い方: bash scripts/video_folder.sh <クライアント（例 monodemo）>"

BUCKET="$(gcloud run services describe "$SERVICE" --project="$PROJECT" --region="$REGION" --format=json |
  python3 -c 'import json, sys
env = {e.get("name"): e.get("value") for e in json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0].get("env", [])}
print(env.get("GCS_BUCKET") or "")')"
[ -n "$BUCKET" ] || fail "Cloud Run に GCS_BUCKET がありません"
# フォルダが無いと画面が開けないので、空の目印を置く（動画として扱われない）
printf '' | gcloud storage cp - "gs://${BUCKET}/videos/${CLIENT_ID}/.keep" --project="$PROJECT" >/dev/null 2>&1 || true
echo "次の URL を開き、「ファイルをアップロード」で動画を選んでください（mp4・mov など）:"
echo "  https://console.cloud.google.com/storage/browser/${BUCKET}/videos/${CLIENT_ID}?project=${PROJECT}"
echo "置いた動画は1日で自動で消えます。置いたら、その日のうちに解析を頼んでください。"
