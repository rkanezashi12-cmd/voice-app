#!/usr/bin/env bash
# save_recording.sh で Cloud Shell に保存した録音（MP3）を、GCS の録音の置き場に戻す（やり直しの前に使う。Cloud Shell で実行）。
# 戻した録音も、ほかの録音と同じく1日で自動削除され、処理が成功すると消える。
#
#   cd ~/voice-app && bash scripts/restore_recording.sh <クライアント> <商談記録の ID> [MP3 のパス]
#   （MP3 のパスを省くと ~/saved-recordings/<クライアント>-<ID>/<ID>.mp3）
#   save_recording.sh が ffmpeg なしで作った ZIP（<ID>-raw.zip）も渡せる。分割された元のファイルを、元の名前のまま戻す
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
  fail "使い方: bash scripts/restore_recording.sh <クライアント（default / monodemo）> <商談記録の ID（数字）> [MP3 または ZIP のパス]"
SRC="${3:-${HOME}/saved-recordings/${CLIENT_ID}-${RECORD_ID}/${RECORD_ID}.mp3}"
[ -f "$SRC" ] || fail "録音のファイルがありません: ${SRC}（PC から Cloud Shell にアップロードして、パスを指定してください）"

BUCKET="$(gcloud run services describe "$SERVICE" --project="$PROJECT" --region="$REGION" --format=json |
  python3 -c 'import json, sys
env = {e.get("name"): e.get("value") for e in json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0].get("env", [])}
print(env.get("GCS_BUCKET") or "")')"
[ -n "$BUCKET" ] || fail "Cloud Run に GCS_BUCKET がありません"

# ZIP（save_recording.sh が作った raw/<録音の区切り>/<連番>.<拡張子>）なら、元の名前のまま戻す
if [[ "$SRC" == *.zip ]]; then
  command -v unzip >/dev/null 2>&1 || fail "unzip がありません"
  WORK="$(mktemp -d)"
  trap 'rm -rf "$WORK"' EXIT
  unzip -q "$SRC" -d "$WORK"
  sessions=0
  files=0
  while IFS= read -r dir; do
    session="$(basename "$dir")"
    # 録音の区切りの名前の決まり（app/recording_objects.py の SESSION_ID_RE）
    [[ "$session" =~ ^[0-9]{13}-[ab]-[a-z0-9]{4,12}$ ]] || continue
    parts=()
    while IFS= read -r f; do
      [[ "$(basename "$f")" =~ ^[0-9]{6}\.[a-z0-9]+$ ]] && parts+=("$f")
    done < <(find "$dir" -maxdepth 1 -type f | sort)
    [ "${#parts[@]}" -gt 0 ] || continue
    # 1つの区切りのファイルをまとめて送る（元の名前のまま）
    gcloud storage cp "${parts[@]}" "gs://${BUCKET}/recordings/${CLIENT_ID}/${RECORD_ID}/${session}/" \
      --project="$PROJECT" >/dev/null 2>&1 || fail "戻せませんでした: ${session}"
    files=$((files + ${#parts[@]}))
    sessions=$((sessions + 1))
  done < <(find "$WORK" -mindepth 1 -type d | sort)
  [ "$files" -gt 0 ] || fail "ZIP の中に録音のファイルがありません（save_recording.sh が作った ZIP か確かめてください）"
  echo "録音を戻しました（商談記録 ${RECORD_ID}。録音の区切り ${sessions} 個・ファイル ${files} 個）。"
  echo "このあと、やり直し（reprocess）を「完了の記録もやり直す」で動かします。"
  exit 0
fi

# 録音の置き場の名前の決まり（app/recording_objects.py）：<開始時刻 ms>-<方式 a>-<英数字>/<連番 6桁>.<拡張子>
SESSION="$(date +%s%3N)-a-restored"
DEST="gs://${BUCKET}/recordings/${CLIENT_ID}/${RECORD_ID}/${SESSION}/000000.mp3"
gcloud storage cp "$SRC" "$DEST" --project="$PROJECT" >/dev/null
echo "録音を戻しました（商談記録 ${RECORD_ID}）。このあと、やり直し（reprocess）を「完了の記録もやり直す」で動かします。"
