# widgets/（PC の CRM 画面のウィジェット）

| フォルダ | 中身 | 手順 |
|---|---|---|
| `meeting-report/` | 商談日報：商談記録の画面の関連リストに、日報（要約・課題・ニーズ・次のアクションなど）と、話者ごとに色分けした文字起こし全文を出す。全文は言葉で検索できる。読むだけ（CRM には書かない） | docs/widget.md |

- ZIP は `python3 scripts/build_widget.py` で作る（`dist/meeting-report-widget.zip`。項目の API 名は `app/field_map.py` から入れる）
- 画面に触れない処理は `app/report.js`（`tests/js/widget-report.test.mjs`）、画面は `tests/e2e/widget.e2e.mjs`（偽の Zoho SDK）で確かめる
- CRM への登録は API ではできない（画面の操作。docs/widget.md の手順2）
