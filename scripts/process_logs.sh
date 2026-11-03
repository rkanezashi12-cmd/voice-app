#!/usr/bin/env bash
# 録音後の処理（Cloud Tasks → Gemini → CRM の更新）・動画の解析の進み具合を Cloud Logging から時系列で出す。
# GitHub Actions の ops（command=logs）からも使う（出すのは ID・件数・状態・エラーの種類だけ。リポジトリが公開のため）。
# ログに文字起こしの本文・要約は出ない（件数・文字数・状態・エラーの種類だけ）。
# 項目 = CRM に書いた（DRY_RUN なら書く予定だった）項目、用語数 = 補正に使った用語辞書の件数、
# 削除数 = 消した音声ファイルの数、秒 = 処理全体の所要時間、Recallのイベント・サブコード = Recall.ai からの通知の種類。
# 考える量・入力トークン・出力トークン・考えたトークン = Gemini の呼び出しごとの量（費用の確認用。考えた分も出力として課金される）。
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
  tasks.enqueued glossary.loaded pipeline.transcribed gemini.generated gemini.retry correct.rejected correct.skipped
  summarize.invalid_json dry_run.skip crm.updated gcs.deleted pipeline.finished pipeline.skipped_finished
  pipeline.retry pipeline.failed pipeline.failure_not_recorded pipeline.unexpected_error auth.oidc_rejected
  media.delete_failed
  # 動画の解析（docs/video-analysis.md）
  video.batch_enqueued video.batch_retry video.batch_failed video.started video.split video.done video.skipped
  video.failed video.retry video.attachment_failed video.failure_not_recorded video.unexpected_error
)
FILTER='resource.type="cloud_run_revision" AND (severity>=ERROR'
for e in "${EVENTS[@]}"; do
  FILTER+=" OR jsonPayload.message=\"${e}\""
done
FILTER+=')'

echo "時刻,イベント,記録ID,状態,処理,モデル,文字数,出力文字数,考える量,入力トークン,出力トークン,考えたトークン,項目,用語数,削除数,秒,Recallのイベント,サブコード,エラーコード,エラー,HTTP,動画,動画の数,積んだ数"
# --order=asc は遅いため、新しい順に取って手元で古い順に並べ替える
gcloud logging read "$FILTER" --project="$PROJECT" --freshness="${MINUTES}m" --limit=300 \
  --format='csv[no-heading](timestamp.date("%m-%d %H:%M:%S",tz=Asia/Tokyo),jsonPayload.message,jsonPayload.record_id,jsonPayload.status,jsonPayload.task,jsonPayload.model,jsonPayload.chars,jsonPayload.output_chars,jsonPayload.thinking,jsonPayload.input_tokens,jsonPayload.output_tokens,jsonPayload.thinking_tokens,jsonPayload.fields.join(sep=" "),jsonPayload.terms,jsonPayload.objects,jsonPayload.seconds,jsonPayload.event,jsonPayload.sub_code,jsonPayload.error_code,jsonPayload.error,httpRequest.status,jsonPayload.video,jsonPayload.videos,jsonPayload.enqueued)' |
  sort
