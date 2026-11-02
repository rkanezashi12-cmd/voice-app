#!/usr/bin/env bash
# save_recording.sh で Cloud Shell に保存した録音（MP3）を、GCS の録音の置き場に戻す（やり直しの前に使う。Cloud Shell で実行）。
# 戻した録音も、ほかの録音と同じく1日で自動削除され、処理が成功すると消える。
#
#   cd ~/voice-app && bash scripts/restore_recording.sh <クライアント> <商談記録の ID> [MP3 のパス]
#   （MP3 のパスを省くと ~/saved-recordings/<クライアント>-<ID>/<ID>.mp3）
set -euo pipefail

PROJECT="${PROJECT:-voice-ai-510014}"
REGION="asia-northeast1"
SERVICE="meeting-notes"
CLIENT_ID="${1:-}"
RECORD_ID="${2:-}"
fail() {
  printf 'エラー: %s\n' "$*" >&2
  exit 1
}
[[ "$CLIENT_ID" =~ ^[a-z0-9][a-z0-9-]{0,31}$ ]] && [[ "$RECORD_ID" =~ ^[0-9]+$ ]] ||
  fail "使い方: bash scripts/restore_recording.sh <クライアント（default / monodemo）> <商談記録の ID（数字）> [MP3 のパス]"
SRC="${3:-${HOME}/saved-recordings/${CLIENT_ID}-${RECORD_ID}/${RECORD_ID}.mp3}"
[ -f "$SRC" ] || fail "録音のファイルがありません: ${SRC}（PC から Cloud Shell にアップロードして、パスを指定してください）"

BUCKET="$(gcloud run services describe "$SERVICE" --project="$PROJECT" --region="$REGION" --format=json |
  python3 -c 'import json, sys
env = {e.get("name"): e.get("value") for e in json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0].get("env", [])}
print(env.get("GCS_BUCKET") or "")')"
[ -n "$BUCKET" ] || fail "Cloud Run に GCS_BUCKET がありません"

# 録音の置き場の名前の決まり（app/recording_objects.py）：<開始時刻 ms>-<方式 a>-<英数字>/<連番 6桁>.<拡張子>
SESSION="$(date +%s%3N)-a-restored"
DEST="gs://${BUCKET}/recordings/${CLIENT_ID}/${RECORD_ID}/${SESSION}/000000.mp3"
gcloud storage cp "$SRC" "$DEST" --project="$PROJECT" >/dev/null
echo "録音を戻しました（商談記録 ${RECORD_ID}）。このあと、やり直し（reprocess）を「完了の記録もやり直す」で動かします。"
