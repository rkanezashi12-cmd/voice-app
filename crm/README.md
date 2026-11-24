# crm/

Zoho CRM のワークフローから Cloud Run を呼ぶ Deluge 関数の保管場所。

- `issue_recording_url.dg`（入口B：録音用URLの発行）。画面での設定手順は [docs/crm-workflow.md](../docs/crm-workflow.md)。
- `create_bot.dg`（入口A：ボットの予約）。画面での設定手順は [docs/recall-bot.md](../docs/recall-bot.md)。
- `create_workdrive_folder.dg`（マルサン木型：商談を作ったら WorkDrive に企業・商談のフォルダを作り、商談に URL を書く。
  バックエンドは使わない）。画面での設定手順は [docs/workdrive-folders.md](../docs/workdrive-folders.md)。
- `create_workdrive_folder_button.dg`（上と同じ処理を、商談の画面のボタン「フォルダを作成」から呼ぶ版。結果を画面に出し、フォルダを開く）。
  2つは同じ処理なので、直すときは両方直す。
- `move_workdrive_folder_button.dg`（商談の画面のボタン「フォルダを移動」。顧客企業を変えた商談の商談フォルダを、今の企業フォルダの下へ移す）。
  企業フォルダを探す・作る部分は上の2つと同じ。
- 画面に貼るときは、1行目の宣言（`void automation.…`）と外側の `{ }` は貼らない（画面が作る）。
- 関数の先頭の設定欄にある API 名（商談記録・取得方法・開始日時）は `app/field_map.py` と合わせる。
- CRM の API 名はバックエンドの `app/field_map.py` に集約している。Deluge 側はレコード ID などの最小限だけを送り、
  CRM への書き戻し（`recall_id`・`recording_url`・状態）はバックエンドが行う。
