# Zoho CRM の設定手順（商談記録・用語辞書）

作業先：**株式会社マルサン木型製作所の本番 Zoho CRM（US DC）**。
作るのは新しいモジュール2つと、その中の項目だけ。**既存のモジュール・項目・レイアウト・関数・ワークフローには触らない。**

> 例外（2026-10-02 ユーザー了承）：商談記録の「先方担当者（連絡先）」（連絡先の複数選択ルックアップ）を作ると、
> Zoho が中間モジュール「商談記録の先方担当者」を作り、**連絡先（Contacts）に逆向きの項目「商談記録」と関連リストを足す**。
> 連絡先のデータは書き換えない（紐づくのは、これから作る商談記録だけ）。

`scripts/crm_setup.py`（Cloud Shell で実行）で自動作成する。スクリプトの約束：

- 書き込む前に接続先の組織を照合し（組織 ID・ドメイン名・会社名）、1つでも違えば何もせずに止まる
- 既定は `plan`（作るものを表示するだけ）。`apply` でも最後に「yes」と入力するまで書き込まない
- 使う書き込みは「作成（POST）」だけ。既存のものの変更・削除はしない。作成済みのものは飛ばす（何度流しても同じ結果）
- 同じ名前の別モジュール・別項目が既にあれば、何もせずに止まる
- 通信の記録は `logs/crm_setup-日時.jsonl` に残る

所要時間：15分ほど。

## 1. Self Client を用意する（ブラウザ）

1. **マルサン木型の CRM の管理者アカウント**で https://api-console.zoho.com を開く
2. 「Add Client」→「Self Client」→「Create」（既にあればそれを使う）
3. 「Client Secret」タブの **Client ID** と **Client Secret** を控える

認可コードはまだ発行しない（10分で失効するので、手順2の途中で発行する）。
このトークンはモジュールと項目を作るためだけのもので、バックエンドの Zoho 接続とは別物。作業が終わったら手順5で無効にする。

## 2. Cloud Shell で接続する

**コマンドは1行ずつ貼って実行する。** 入力を待っている間に次の行まで貼ると、次の行のコマンドが値として読み込まれてしまう。
Client Secret とトークンは画面に出さない（スクリーンショットにも写さない）。

1. 最新にする

   ```bash
   cd ~/voice-app && git pull
   ```

2. Client ID と Secret を入れる。`Client ID:` と出たら ID を貼って Enter、`Client Secret` と出たら Secret を貼って Enter（Secret は表示されない）。
   最後に `OK: ID 1000.… / Secret ○○文字` と出れば成功

   ```bash
   cd ~/voice-app; export ZOHO_DC=com; ZOHO_CLIENT_ID=; ZOHO_CLIENT_SECRET=; until [ -n "$ZOHO_CLIENT_ID" ]; do read -rp "Client ID: " ZOHO_CLIENT_ID || break; done; until [ -n "$ZOHO_CLIENT_SECRET" ]; do read -rsp "Client Secret（表示されません）: " ZOHO_CLIENT_SECRET || break; echo; done; export ZOHO_CLIENT_ID ZOHO_CLIENT_SECRET; echo "OK: ID ${ZOHO_CLIENT_ID:0:5}… / Secret ${#ZOHO_CLIENT_SECRET}文字"
   ```

3. api-console の Self Client の「Generate Code」タブで次を入れて「Create」し、表示された **認可コード** をコピーする（**10分で失効**。すぐに次へ）
   - Scope：`ZohoCRM.org.READ,ZohoCRM.settings.modules.ALL,ZohoCRM.settings.fields.ALL,ZohoCRM.settings.profiles.READ`
   - Time Duration：10 minutes
   - Scope Description：CRM setup
   - 組織（ポータル）を選ぶ画面が出たら、**マルサン木型の本番組織**を選ぶ

4. 認可コードをリフレッシュトークンに交換する。`認可コード:` と出たらコードを貼って Enter（トークンは画面に出さずに環境変数に入る）

   ```bash
   unset ZOHO_REFRESH_TOKEN; ZOHO_CODE=; until [ -n "$ZOHO_CODE" ]; do read -rp "認可コード: " ZOHO_CODE || break; done; eval "$(python3 scripts/crm_setup.py exchange-code "$ZOHO_CODE" | grep '^export ZOHO_REFRESH_TOKEN=')"; echo "トークン: ${#ZOHO_REFRESH_TOKEN}文字（0 なら失敗）"
   ```

   0 より大きい文字数が出れば成功。0 なら、その上に出たエラーを見る
   （`invalid_client`：Client ID / Secret の誤りか、DC が Self Client を作った場所と違う。`invalid_code`：認可コードの期限切れ・使用済み。3 からやり直す）

## 3. 接続先を確かめる（書き込みなし）

```bash
python3 scripts/crm_setup.py show-org
```

会社名・ドメイン名がマルサン木型の本番であることを**目で確かめてから**、表示された `export EXPECTED_...` の3行を実行する。
違う組織が出たら、ここで止めて連絡する（手順1の組織の選択を間違えている）。

## 4. 作るものを確かめて、作成する

```bash
python3 scripts/crm_setup.py plan     # 表示だけ。何も変更しない
python3 scripts/crm_setup.py apply    # 内容をもう一度表示し、yes と入力すると作成する
```

最後に「完了しました。すべての項目が API 名どおりに作成されています。」と、作成した項目の設定（種類・文字数・重複不可など）が出れば終わり。
あとから確かめるときは `python3 scripts/crm_setup.py show-fields`（書き込みなし）。

途中で止まったら、表示された内容と `logs/crm_setup-*.jsonl` の最後の数行を送る（トークンは記録していない）。
原因を直して `apply` をもう一度流せば、足りないものだけを作る。

## 5. 後片付け

- Cloud Shell を閉じる（環境変数に入れた作業用のトークンも消える。画面にもファイルにも残していない）
- **Self Client は削除しない。** バックエンドの接続（docs/zoho-connection.md）も同じ Self Client を使うので、削除するとバックエンドが CRM に接続できなくなる

## 6. 画面で行う仕上げ（任意）

- レイアウトの並べ替え（項目は既定のセクションにまとめて入る）。おすすめは次の4セクション
  - 基本情報：商談形態〜Recall ID
  - 商談の内容：要約〜決裁者
  - 文字起こし：文字起こし全文・文字起こし全文2
  - 処理状況：状態・エラー内容
- 用語辞書に1件登録してみる（例：用語「マルサン木型」、誤認識例「丸三木型、まるさん」、種類「社名」）

## 7. あとから項目を足す（2026-10-02：先方担当者（連絡先））

録音アプリで選んだ担当者を、CRM の連絡先として商談記録に紐づける項目。手順2〜4をもう一度行う
（Self Client はそのまま使う。認可コードを発行し直し、同じスコープでトークンを取り直す）。

1. 手順2（Client ID・Secret を入れる → 認可コード → トークン）と手順3（`show-org` で組織を確かめる）
2. `python3 scripts/crm_setup.py plan` で、次の2行だけが出ることを確かめる（ほかの項目は作成済みなので出ない）

   ```
   項目を作成: MeetingRecords.Customer_Contacts「先方担当者（連絡先）」(multiselectlookup)
     → Zoho が中間モジュール「商談記録の先方担当者」を作り、Contacts に項目「商談記録」と関連リストを足します（Contacts のデータは書き換えません）
   ```

   連絡先に「商談記録」という名前の項目が既にある、同じ名前のモジュールがある、などのときは、何もせずに止まる
3. `python3 scripts/crm_setup.py apply` → yes
4. `python3 scripts/crm_setup.py show-fields` の `MeetingRecords.Customer_Contacts` の行（`複数選択=…`）を送る（実物の形を記録するため）

バックエンドは、項目ができてから10分以内に自動で使い始める（デプロイし直さなくてよい）。それまでは名前だけを「先方担当者」に書く。

---

## 作成される内容

### 商談記録（API 名 MeetingRecords）

| 設定 | 値 |
|---|---|
| モジュール名（単数・複数） | 商談記録 |
| API 名 | **MeetingRecords**（アンダースコア不可） |
| 権限（プロファイル） | すべてのプロファイル（スクリプトが自動で付ける） |

名前の項目 **「商談記録名」**（API 名 `Name`、120 文字まで）と **「商談記録の担当者」**（`Owner`）はモジュールと一緒に作られる。

#### 項目

| # | 表示名 | 種類 | API 名 | 設定 |
|---|---|---|---|---|
| 1 | 商談形態 | 選択リスト | `Meeting_Type` | 選択肢：対面 / オンライン |
| 2 | 取得方法 | 選択リスト | `Capture_Method` | 選択肢：デスクトップ / ボット / 対面録音 |
| 3 | 状態 | 選択リスト | `Status` | 選択肢（この順）：予約済 / 参加待ち / 参加中 / 参加失敗 / 録音中 / 文字起こし中 / 完了 / 失敗 / 取引先未設定 |
| 4 | 取引先 | ルックアップ | `Account` | 関連付けるモジュール：取引先 |
| 5 | 商談 | ルックアップ | `Deal` | 関連付けるモジュール：商談 |
| 6 | 先方担当者 | 1行 | `Contact_Name` | 文字数 255 |
| 7 | 開始日時 | 日付/時刻 | `Start_At` | |
| 8 | 会議URL | URL | `Meeting_URL` | 450 文字まで（Zoho の既定） |
| 9 | 録音用URL | URL | `Recording_URL` | バックエンドが書き込む。450 文字まで |
| 10 | Recall ID | 1行 | `Recall_ID` | 文字数 255。**「重複する値を許可しない」に✓**（大文字・小文字は区別しない。Zoho は区別する設定を API で受け付けない） |
| 11 | エラー内容 | 複数行（小） | `Error_Message` | |
| 12 | 要約 | 複数行（大） | `Summary` | |
| 13 | 課題 | 複数行（大） | `Issues` | |
| 14 | ニーズ | 複数行（大） | `Needs` | |
| 15 | 次のアクション | 複数行（大） | `Next_Actions` | |
| 16 | 次回期限 | 日付 | `Due_Date` | |
| 17 | 分類 | 1行 | `Category` | 文字数 255（選択肢が決まったら選択リストに変える） |
| 18 | 競合 | 複数行（大） | `Competitors` | |
| 19 | 予算 | 1行 | `Budget` | 文字数 255 |
| 20 | 決裁者 | 1行 | `Decision_Maker` | 文字数 255 |
| 21 | 文字起こし全文 | 複数行（大） | `Transcript` | |
| 22 | 文字起こし全文2 | 複数行（大） | `Transcript_2` | 1つ目に入りきらない分（32,000文字超）が入る |
| 23 | 先方担当者（連絡先） | 複数選択ルックアップ | `Customer_Contacts` | 関連付けるモジュール：連絡先。中間モジュール「商談記録の先方担当者」ができ、連絡先に項目「商談記録」と関連リストが足される（2026-10-02 追加。手順7） |

- 「複数行（大）」は最大 32,000 文字。
- どれも必須項目にはしない（バックエンドが後から書き込むため）。

### 用語辞書（API 名 Glossary）

文字起こしの補正に使う（社名・製品名など、聞き間違えやすい言葉を登録する）。

| 設定 | 値 |
|---|---|
| モジュール名（単数・複数） | 用語辞書 |
| API 名 | **Glossary** |
| 権限（プロファイル） | 商談記録と同じ |

名前の項目は表示名 **「用語」**（API 名 `Name`）でモジュールと一緒に作られる。

| # | 表示名 | 種類 | API 名 | 設定 |
|---|---|---|---|---|
| 1 | 誤認識例 | 複数行（小） | `Misrecognitions` | 聞き間違いの例を「、」区切りで入れる（例：丸三木型、まるさん） |
| 2 | 種類 | 選択リスト | `Term_Type` | 選択肢：社名 / 製品名 / 人名 / 専門用語 / その他 |
