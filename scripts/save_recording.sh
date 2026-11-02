#!/usr/bin/env bash
# 対面録音の音声を、GCS から Cloud Shell に保存する（1本の MP3 にまとめる）。Cloud Shell で実行する想定。
# 音声は「残さない」のが決まり（CLAUDE.md）だが、処理に失敗した録音をユーザーの指示で手元に残すときに使う。
# GCS の音声は1日で自動削除され、処理が成功したときにも消えるので、再処理（reprocess.sh）より先に実行する。
#
#   cd ~/voice-app && bash scripts/save_recording.sh <クライアント（default / monodemo）> <商談記録の ID>
#   → ~/saved-recordings/<クライアント>-<ID>/<ID>.mp3（cloudshell download で PC に保存する）
#
# 読むだけ（GCS の音声は消さない・変えない）。保存したファイルは、PC に移したら Cloud Shell から消す。
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
  fail "使い方: bash scripts/save_recording.sh <クライアント（default / monodemo）> <商談記録の ID（数字）>"

BUCKET="$(gcloud run services describe "$SERVICE" --project="$PROJECT" --region="$REGION" --format=json |
  python3 -c 'import json, sys
env = {e.get("name"): e.get("value") for e in json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0].get("env", [])}
print(env.get("GCS_BUCKET") or "")')"
[ -n "$BUCKET" ] || fail "Cloud Run に GCS_BUCKET がありません"

OUT="${HOME}/saved-recordings/${CLIENT_ID}-${RECORD_ID}"
RAW="${OUT}/raw"
mkdir -p "$RAW"
echo "音声を取ってきます: gs://${BUCKET}/recordings/${CLIENT_ID}/${RECORD_ID}/"
gcloud storage cp -r "gs://${BUCKET}/recordings/${CLIENT_ID}/${RECORD_ID}/*" "$RAW/" ||
  fail "音声が見つかりません（クライアント・ID の違い、または1日を過ぎて自動削除された可能性があります）"
COUNT="$(find "$RAW" -type f | wc -l)"
[ "$COUNT" -gt 0 ] || fail "音声が見つかりません"
echo "取ってきたファイル: ${COUNT} 個"

if ! command -v ffmpeg >/dev/null 2>&1; then
  (cd "$OUT" && zip -qr "${RECORD_ID}-raw.zip" raw)
  echo "ffmpeg が無いので、分割されたままのファイルを ZIP にしました: ${OUT}/${RECORD_ID}-raw.zip"
  echo "PC に保存するには: cloudshell download ${OUT}/${RECORD_ID}-raw.zip"
  exit 0
fi

# 録音の区切り（セッション）ごとに1本にしてから、全体を1本の MP3 にする（録音順＝名前順）
WORK="${OUT}/work"
mkdir -p "$WORK"
: >"${WORK}/all.txt"
n=0
for session in $(find "$RAW" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort); do
  files=$(find "${RAW}/${session}" -type f | sort)
  [ -n "$files" ] || continue
  mp3="${WORK}/s$(printf '%03d' "$n").mp3"
  if [[ "$session" == *-b-* ]]; then
    # 方式 b：先頭のファイルにだけヘッダーがあるので、順に連結して1本に戻す
    first=$(echo "$files" | head -n1)
    joined="${WORK}/joined_${n}.${first##*.}"
    # shellcheck disable=SC2086
    cat $files >"$joined"
    ffmpeg -nostdin -hide_banner -loglevel error -y -i "$joined" -vn -ac 1 -c:a libmp3lame -b:a 64k "$mp3"
  else
    # 方式 a：1分ごとのファイルが単体で再生できる。変換してからつなぐ
    : >"${WORK}/list_${n}.txt"
    i=0
    for f in $files; do
      part="${WORK}/p_${n}_$(printf '%05d' "$i").mp3"
      if ffmpeg -nostdin -hide_banner -loglevel error -y -i "$f" -vn -ac 1 -c:a libmp3lame -b:a 64k "$part"; then
        echo "file '${part}'" >>"${WORK}/list_${n}.txt"
      else
        echo "読めなかった部分を飛ばしました: $(basename "$f")"
      fi
      i=$((i + 1))
    done
    ffmpeg -nostdin -hide_banner -loglevel error -y -f concat -safe 0 -i "${WORK}/list_${n}.txt" -c copy "$mp3"
  fi
  echo "file '${mp3}'" >>"${WORK}/all.txt"
  n=$((n + 1))
done
[ "$n" -gt 0 ] || fail "まとめられる音声がありませんでした"
ffmpeg -nostdin -hide_banner -loglevel error -y -f concat -safe 0 -i "${WORK}/all.txt" -c copy "${OUT}/${RECORD_ID}.mp3"
rm -rf "$WORK"
SECONDS_TOTAL=$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "${OUT}/${RECORD_ID}.mp3" | cut -d. -f1)
echo "1本にまとめました: ${OUT}/${RECORD_ID}.mp3（約$((SECONDS_TOTAL / 60))分）"
echo "PC に保存するには: cloudshell download ${OUT}/${RECORD_ID}.mp3"
echo "PC に移したら、Cloud Shell からは消してください: rm -rf ${OUT}"
