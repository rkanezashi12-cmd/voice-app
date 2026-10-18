# widgets/（PC の CRM 画面のウィジェット）

| フォルダ | 中身 | 手順 |
|---|---|---|
| `needs-analysis/` | ニーズ分析の結果：「ニーズ分析」の画面の関連リストに、件数・上位のニーズ・種類・出典・会社のグラフ、応えられるニーズ・新商品開発の候補、ニーズ一覧（絞り込み・元の記録を開く）を出す。読むだけ | docs/needs-analysis.md |
| `meeting-report/` | 商談日報：商談記録の画面の関連リストに、日報（要約・課題・ニーズ・次のアクションなど）と、話者ごとに色分けした文字起こし全文を出す。全文は言葉で検索できる。読むだけ（CRM には書かない） | docs/widget.md |

- ZIP は `python3 scripts/build_widget.py`（`dist/meeting-report-widget.zip`）と `python3 scripts/build_widget.py --widget needs-analysis`
  （`dist/needs-analysis-widget.zip`）で作る。項目の API 名は `app/field_map.py` から入れる
- 画面に触れない処理は `app/report.js`・`app/analysis.js`（`tests/js/widget-report.test.mjs`・`widget-needs.test.mjs`）、
  画面は `tests/e2e/widget.e2e.mjs`・`widget-needs.e2e.mjs`（偽の Zoho SDK）で確かめる
- CRM への登録は API ではできない（画面の操作。docs/widget.md の手順2）
