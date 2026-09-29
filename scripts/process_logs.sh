#!/usr/bin/env bash
# 録音後の処理（Cloud Tasks → Gemini → CRM の更新）の進み具合を Cloud Logging から時系列で出す。
# ログに文字起こしの本文・要約は出ない（件数・文字数・状態・エラーの種類だけ）。
#
#   bash scripts/process_logs.sh          # 直近 60 分
#   bash scripts/process_logs.sh 180      # 直近 180 分
set -euo pipefail
PROJECT="${PROJECT:-voice-ai-510014}"
MINUTES="${1:-60}"
EVENTS=(recording.completed tasks.enqueued glossary.loaded pipeline.transcribed gemini.generated gemini.retry
  dry_run.skip crm.updated pipeline.finished pipeline.skipped_finished pipeline.retry pipeline.failed
  pipeline.failure_not_recorded pipeline.unexpected_error auth.oidc_rejected media.delete_failed)
FILTER='resource.type="cloud_run_revision" AND (severity>=ERROR'
for e in "${EVENTS[@]}"; do
  FILTER+=" OR jsonPayload.message=\"${e}\""
done
FILTER+=')'

echo "時刻,イベント,記録ID,状態,処理,モデル,文字数,出力文字数,書き込む予定の項目,エラーコード,エラー"
# --order=asc は遅いため、新しい順に取って手元で古い順に並べ替える
gcloud logging read "$FILTER" --project="$PROJECT" --freshness="${MINUTES}m" --limit=300 \
  --format='csv[no-heading](timestamp.date("%m-%d %H:%M:%S",tz=Asia/Tokyo),jsonPayload.message,jsonPayload.record_id,jsonPayload.status,jsonPayload.task,jsonPayload.model,jsonPayload.chars,jsonPayload.output_chars,jsonPayload.fields.join(sep=" "),jsonPayload.error_code,jsonPayload.error)' |
  sort
