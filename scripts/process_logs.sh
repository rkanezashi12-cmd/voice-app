#!/usr/bin/env bash
# 録音後の処理（Cloud Tasks → Gemini → CRM の更新）の進み具合を Cloud Logging から時系列で出す。
# ログに文字起こしの本文・要約は出ない（件数・文字数・状態・エラーの種類だけ）。
# 項目 = CRM に書いた（DRY_RUN なら書く予定だった）項目、用語数 = 補正に使った用語辞書の件数、
# 削除数 = 消した音声ファイルの数、秒 = 処理全体の所要時間、Recallのイベント・サブコード = Recall.ai からの通知の種類。
#
#   bash scripts/process_logs.sh          # 直近 60 分
#   bash scripts/process_logs.sh 180      # 直近 180 分
set -euo pipefail
PROJECT="${PROJECT:-voice-ai-510014}"
MINUTES="${1:-60}"
EVENTS=(
  # 入口B：対面録音
  recording.already_processed recording.record_not_found recording.record_check_failed recording.completed
  # 入口A：ボット（予約・Recall.ai からの通知・文字起こしの取得・録画の削除）
  bot.reserved recall.bot_created recall.bot_507_retry webhook.received webhook.ignored recall.event_parsed
  recall.status_written recall.status_stale recall.status_no_record recall.event_ignored recall_event.failed
  recall.transcript_requested pipeline.waiting_transcript recall.transcript_loaded recall.media_deleted
  recall.recording_deleted recall.unknown_sdk_upload
  # 共通処理
  tasks.enqueued glossary.loaded pipeline.transcribed gemini.generated gemini.retry correct.rejected
  summarize.invalid_json dry_run.skip crm.updated gcs.deleted pipeline.finished pipeline.skipped_finished
  pipeline.retry pipeline.failed pipeline.failure_not_recorded pipeline.unexpected_error auth.oidc_rejected
  media.delete_failed
)
FILTER='resource.type="cloud_run_revision" AND (severity>=ERROR'
for e in "${EVENTS[@]}"; do
  FILTER+=" OR jsonPayload.message=\"${e}\""
done
FILTER+=')'

echo "時刻,イベント,記録ID,状態,処理,モデル,文字数,出力文字数,項目,用語数,削除数,秒,Recallのイベント,サブコード,エラーコード,エラー"
# --order=asc は遅いため、新しい順に取って手元で古い順に並べ替える
gcloud logging read "$FILTER" --project="$PROJECT" --freshness="${MINUTES}m" --limit=300 \
  --format='csv[no-heading](timestamp.date("%m-%d %H:%M:%S",tz=Asia/Tokyo),jsonPayload.message,jsonPayload.record_id,jsonPayload.status,jsonPayload.task,jsonPayload.model,jsonPayload.chars,jsonPayload.output_chars,jsonPayload.fields.join(sep=" "),jsonPayload.terms,jsonPayload.objects,jsonPayload.seconds,jsonPayload.event,jsonPayload.sub_code,jsonPayload.error_code,jsonPayload.error)' |
  sort
