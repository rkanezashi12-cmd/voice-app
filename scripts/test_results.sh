#!/usr/bin/env bash
# 録音テストの結果（録音ページが送った診断イベント）を Cloud Logging から一覧する。
#
#   bash scripts/test_results.sh                 # 直近 2 日の全テストの結果（完了・中断時の診断まとめ）
#   bash scripts/test_results.sh android-1       # 記録 ID を指定すると、その記録のイベントを時系列で全部出す
set -euo pipefail
PROJECT="${PROJECT:-voice-ai-510014}"
BASE='resource.type="cloud_run_revision" AND jsonPayload.message="recorder.event"'

if [[ $# -eq 0 ]]; then
  gcloud logging read "${BASE} AND jsonPayload.event_type=\"summary\"" --project="$PROJECT" \
    --freshness=2d --limit=50 \
    --format='table(timestamp.date(tz=Asia/Tokyo),jsonPayload.record_id,jsonPayload.d_elapsed_seconds,jsonPayload.d_chunks_uploaded,jsonPayload.d_chunks_pending,jsonPayload.d_interruptions,jsonPayload.d_hidden_count,jsonPayload.d_timer_gap_total_seconds,jsonPayload.d_wake_lock)'
else
  # --order=asc は範囲全体を走査して非常に遅いため、新しい順に取って手元で古い順に並べ替える
  echo "時刻,イベント,理由,非表示ms,停止ms,件数,未送信,状態,エラー"
  gcloud logging read "${BASE} AND jsonPayload.record_id=\"$1\"" --project="$PROJECT" \
    --freshness=2d --limit=500 \
    --format='csv[no-heading](timestamp.date("%Y-%m-%d %H:%M:%S.%f",tz=Asia/Tokyo),jsonPayload.event_type,jsonPayload.d_reason,jsonPayload.d_hidden_ms,jsonPayload.d_gap_ms,jsonPayload.d_missing,jsonPayload.d_pending,jsonPayload.d_status,jsonPayload.d_error)' \
    | sort
fi
