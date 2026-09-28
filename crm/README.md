# crm/

Zoho CRM のワークフローから Cloud Run を呼ぶ Deluge 関数の保管場所。

- Step 4 で `create_bot.dg`（入口A：ボット予約）と `issue_recording_url.dg`（入口B：録音 URL 発行）を追加する。
- CRM の API 名はバックエンドの `app/field_map.py` に集約している。Deluge 側はレコード ID などの最小限だけを送り、
  CRM への書き戻し（`recall_id`・`recording_url`・状態）はバックエンドが行う。
