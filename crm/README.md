# crm/

Zoho CRM のワークフローから Cloud Run を呼ぶ Deluge 関数の保管場所。

- `issue_recording_url.dg`（入口B：録音用URLの発行）。画面での設定手順は [docs/crm-workflow.md](../docs/crm-workflow.md)。
- `create_bot.dg`（入口A：ボット予約）は次に追加する。
- 画面に貼るときは、1行目の宣言（`void automation.…`）と外側の `{ }` は貼らない（画面が作る）。
- 関数の先頭の設定欄にある API 名（商談記録・取得方法・開始日時）は `app/field_map.py` と合わせる。
- CRM の API 名はバックエンドの `app/field_map.py` に集約している。Deluge 側はレコード ID などの最小限だけを送り、
  CRM への書き戻し（`recall_id`・`recording_url`・状態）はバックエンドが行う。
