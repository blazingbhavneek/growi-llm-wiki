# Growi LLM Wiki

## 1. 概要

文書を置いたフォルダ（NFS やバインドマウント）を指定すると、AIが Markdown ウィキに書き直して **GROWI** のプロジェクト名付きフォルダ1つに公開する運用ツールです。閲覧と編集はGROWI上で行い、生成したページはこのツールが管理します。

プロジェクト1件 = `configs/` の1ファイルで、操作はすべて `main.py` から行います。設計や内部仕様など開発者向けの内容は [`DEV_README.md`](DEV_README.md) を参照してください。

---

## 2. 出力先

| 場所 | 内容 |
| --- | --- |
| 文書フォルダ（マウント元） | 元の文書そのもの。変更も複製もしません |
| `<data_root>/<name>/` | ローカルの作業データ（変換後のテキスト、生成ページ、公開状態） |
| GROWI `/<name>/` | 公開されたウィキ（番号付きページ、`00-目次`、文書間リンク） |

---

## 3. 前提条件

- Linux と、文書フォルダを読み取り可能にマウント済みの環境（`mount --bind`、NFS、SMB）
- [uv](https://docs.astral.sh/uv/)（Python 3.13 は `uv` が用意します）
- LLM・doc-parser・GROWI の各エンドポイントに届くネットワーク

---

## 4. 初期セットアップ

### 1. uv が使えるか確認

```bash
uv --version
```

入っていない場合はインストールします。

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

### 2. リポジトリを取得

```bash
git clone <repository-url>
cd llm-wiki
```

---

### 3. Python環境をつくる

```bash
uv sync
```

`.venv/` が作成され、依存関係と Python 3.13 が入ります。以降のコマンドはすべてリポジトリ直下で、次の形式で実行します（`uv run python main.py …` でも同じです）。

```bash
.venv/bin/python main.py <command> --project <name>
```

---

### 4. .env はそのままで使えます

LLM・doc-parser・GROWI のURLとトークンは、リポジトリに同梱されている `.env` に設定済みです。**初回は編集しなくて構いません。** 接続先や生成ページの言語を変える必要があるときだけ、「9. 参考：環境変数」を見て `.env` を編集してください。

---

### 5. 接続を確認する

```bash
.venv/bin/python main.py check --project test
```

各接続を個別に確認して、次のように表示されます。

```
chat    200 http://<llm-host>/v1/models
embed   200 http://<embed-host>/v1/models
parser  200 http://<parser-host>/agent/doc-parser/health
growi   200 http://<growi-host>:3000/_api/v3/healthcheck
jev     shisa backend (local start not checked)
data    /path/to/llm-wiki/data/test  mount=/path/to/llm-wiki/data/test/mount ingest=wiki linker=neo
```

この出力は1行が1つの接続に対応します。

- `chat` / `embed` … LLMと埋め込みモデル。`chat` がダメだと生成は一切進みません
- `parser` … 文書変換サービス。`.md` / `.txt` だけの運用なら `skipped` で構いません
- `growi` … 公開先のGROWI
- `jev` … 文書リンクの判定。`skipped` や `backend` 表示は正常で、リンクの付く付かないに影響します
- `data` … 実際に使う作業フォルダと、読みに行くマウントフォルダ。ここで表示された `mount=` が想定したフォルダになっているか確認します

`DOWN` が出た項目は、そのサービスが起動しているか `.env` のURLを確認します。`check` は読むだけでGROWIのページを変更しないので、何度実行しても安全です。

---

## 5. まず動かす：テストプロジェクト

`configs/test.ini` は、そのまま実行できるテスト用プロジェクトです（`--project test`）。

- 文書フォルダ: `data/test/mount/`（試作用のPDFが数件同梱されています。マウント作業は不要）
- 公開先: `configs/test.ini` に設定済みのテスト環境GROWIの `/test`

```bash
.venv/bin/python main.py -v sync --project test
```

`-v` を付けると文書単位の進捗が表示されます。大容量ファイルの変換中に `waiting` が10秒ごとに続くのは、失敗ではなく待っている状態です。テスト規模なら数分で終わります。

`sync` はこのツールの本体で、次の順に処理します。

1. `source_mount` をwalkして、新規・変更された文書だけを集めます（前回と変わらない文書はスキップ）
2. doc-parserで文書をMarkdownに自動変換します（`.md` / `.txt` はそのまま）
3. AIが目次（ドキュメント構成）を計画し、ページ単位でウィキを書き上げます
4. 文書間のリンクを付けて、相互参照できるようにします
5. GROWIへ公開します

処理中の成果物はローカルにも残るので、`data/test/raw/` で変換後テキスト、`data/test/wiki/` で生成ページを確認できます。

出来上がったら GROWI の `http://<growi-host>:3000/test` を開いて、`00-目次` と各ページを確認してください。

同じコマンドをもう一度実行すると、変更がないため数十秒で終了します。

---

### 失敗・中断したら `--continue`

```bash
.venv/bin/python main.py -v sync --project test --continue
```

Ctrl-C、ネットワーク断、LLMや変換サービスの障害、マシンの再起動などで途中までしか進まなかった場合は、同じコマンドに `--continue` を付けるだけです。

- 公開済みの文書は再生成されず、すでに送ったページをもう一度送ることもありません。止まった時点で作りかけだった文書だけが、変換から作り直されます。
- `--continue` はそれまでに払った解析作業（リンク用に抽出したエンティティや Jev の判定結果）を使い回して、未処理の分だけを進めます。迷ったら `-v` を付けて `--continue` で実行し直してください。仕組みは「8. 運用で使うコマンド」の `--continue` に書いています。

---

### テストの片付け

```bash
.venv/bin/python main.py reset --project test
```

このツールが公開した `/test` 配下を削除し、ローカルの公開状態を初期化します。人がGROWIで手動作成したページは削除されません。

---

## 6. 新規プロジェクトのセットアップ

### 1. 文書フォルダをマウント

```bash
sudo mkdir -p /home/seigyo/mnt/newproj
sudo mount --bind /path/to/documents /home/seigyo/mnt/newproj
findmnt --target /home/seigyo/mnt/newproj
```

3つのコマンドの意味は次のとおりです。NFS / SMBマウントでも同じ考え方です。

- `mkdir` … マウント先となる空のフォルダを作ります（以降のINIで `source_mount` に書くパス）
- `mount --bind` … 文書のあるフォルダを、上の空フォルダに接続します
- `findmnt --target` … 接続が効いているかの確認。表示されれば成功です

このツールはマウント先を読むだけで、元ファイルを変更も複製もしません。読み取り専用（`sudo mount -o bind,ro /path/to/documents /home/seigyo/mnt/newproj`）でも動作します。

---

### 2. プロジェクトファイルをつくる

`configs/<名前>.ini` を1つ作ります。**ファイル名がそのまま `--project` の指定名**になります。実際に運用中の `configs/projectF.ini` を例に、1行ずつ読みます。

```ini
[project]
source_mount = /home/seigyo/mnt/projectF
target_name = 中国配自SIM
data_root = data

[settings]
concurrency = 2
```

**`source_mount = /home/seigyo/mnt/projectF`**

変換したい文書フォルダの**絶対パス**です。ここを指定すると、配下をサブフォルダ込みで自動でwalkし、対応形式（`.pdf`、`.docx`、`.pptx`、`.xlsx`、`.md`、`.txt` …）の文書をすべて拾ってきます。フォルダ単位でプロジェクトを分けたいときは、マウントポイントそのものではなく中のフォルダ（例 `/home/seigyo/mnt/projectF/運用マニュアル`）を指定します。このパスより外は一切読まれません。相対パスは使えず、フォルダが存在しない場合は `check` / `sync` の実行時にエラーになります。

**`target_name = 中国配自SIM`**

GROWIにできるフォルダ1つ分の名前、つまり**GROWI利用者に見えるプロジェクト名**です。この例だと `http://<growi-host>:3000/中国配自SIM/` が公開先になり、この下に `00-目次` と番号付きの各ページが並びます。日本語でも英数字でも構いませんが、`/` は使えません。同じ名前がローカル作業フォルダの名前にもなるので、`data/中国配自SIM/` と GROWI `/中国配自SIM/` が対になります。あとで名前を変えると**別プロジェクトとして新規作成**され、古いGROWIフォルダは残ります。古い方は古い設定で `reset` を実行するか、GROWI側を手動で削除してください。

**`data_root = data`**

作業データの置き場所です。`data` はリポジトリ直下の `data/` を指し、絶対パスでも書けます。実際に使われるのは `<data_root>/<target_name>/`（この例では `data/中国配自SIM/`）で、`raw/`（変換後のテキスト）、`wiki/`（生成されたページ）、`metadata/`（どの文書をどこへ公開したかの状態）が入ります。このフォルダを消すと公開状態の情報も失われるため、GROWIのページを消したいときは `rm` ではなく `reset` を使ってください。ディスク使用量は文書量に応じて増えていきます。

**`[settings] concurrency = 2`**

このプロジェクトが同時に投げ続けるLLMリクエストの数です。上げれば速い代わりに、エンドポイントが処理しきれない分は待たされるだけなので、待ち時間が `wiki_request_timeout`（既定300秒）を超えて**タイムアウトし始めます**。**1〜2プロジェクトしか動かさないなら 4〜5**、複数プロジェクトを同時に回すなら、エンドポイントの処理量を超えないよう **2以下**に下げます。この例が `2` なのは、同じマシンで他プロジェクトと同時に来る前提だからです。この行を書かなければ `.env` の `OPENAI_CONCURRENCY`（既定4）が使われます。

`[settings]` は無くても動きますが、`[project]` の3つは必須です。キー名の打ち間違いは実行時に `unknown project setting(s): ...` として通知されます。

---

### 3. このプロジェクトだけ接続先を変える

`.env` の既定値で足りない場合だけ、同じINIに追記します。書かなかった項目は `.env` の値がそのまま使われます。

**`[project] growi_url` / `growi_token`** … このプロジェクトだけ別のGROWI（検証環境など）へ公開する場合に、公開先URLとAPIトークンを指定します。

**`[settings] chat_base_url` / `chat_model` / `chat_api_key`** … LLMのエンドポイントURL、モデル名、APIキー。別のモデルを使わせたいときや、検証用LLMに向けるときに書きます。

**`[settings] parser_base_url`** … doc-parserサービスのURL。`.md` / `.txt` だけのプロジェクトでは不要です。

**`[settings] wiki_request_timeout`** … モデル1回の待ち時間上限（秒）。大きな文書でタイムアウトが続く場合に上げます。

---

### 4. 接続を確認して同期する

```bash
.venv/bin/python main.py check --project newproj
.venv/bin/python main.py -v sync --project newproj
```

1回目は文書量に応じて数時間かかります。中断しても公開済みのページは残り、続きは `--continue` で実行できます。2回目以降の `sync` は新規・変更分だけを処理するため、同期済みの状態なら数十秒で終わります。

---

## 7. テストエンドポイントに向けるには

`configs/test.ini` が実例です。`.env` は本番の接続先にしたまま、このプロジェクトだけ別のサーバーに向けます。

```ini
[project]
source_mount = /home/seigyo/llm-wiki/data/test/mount
target_name = test
data_root = data
growi_url = http://10.160.152.38:3000/     # テスト環境のGROWI
growi_token = <テスト環境のAPIトークン>

[settings]
concurrency = 2
```

- 設定の適用順は `.env` → `configs/<name>.ini` → コマンドライン引数。INIに書いた項目だけが上書きされます。
- GROWIだけ変える場合は `[project]` の2つを、LLMだけ変える場合は `[settings] chat_base_url`（モデルが違うなら `chat_model` も）を書きます。
- 自前の検証用にコピーする場合は `cp configs/test.ini configs/mytest.ini` を作り、`source_mount` と `target_name` を書き換えます。`target_name` を `test` のままにすると、同じGROWIフォルダに書き込むので注意してください。

---

## 8. 運用で使うコマンド

ここまでは `check` と `sync` だけを使いました。定期運用で使うコマンドは次の4つで、すべて `--project <name>` を付けます（`-v` を付けると文書単位の進捗が表示されます）。

<table>
  <thead>
    <tr><th>コマンド</th><th>オプション</th><th>目的</th></tr>
  </thead>
  <tbody>
    <tr>
      <td rowspan="3"><code>sync</code></td>
      <td><em>なし</em></td>
      <td>新規・変更された文書だけを変換 → 生成 → 公開</td>
    </tr>
    <tr>
      <td><code>--continue</code></td>
      <td>途中まで出来た作業を再利用し、未完成分だけやり直す</td>
    </tr>
    <tr>
      <td><code>--force "&lt;path&gt;"</code></td>
      <td>指定した1文書を、変更がなくても再生成する</td>
    </tr>
    <tr>
      <td><code>publish</code></td>
      <td>—</td>
      <td>再生成せず、出来ているページをGROWIへ再送信</td>
    </tr>
    <tr>
      <td><code>pull</code></td>
      <td>—</td>
      <td>GROWI上で人が編集した内容を取り込む</td>
    </tr>
    <tr>
      <td><code>reset</code></td>
      <td>—</td>
      <td>このツールが公開したページを削除する</td>
    </tr>
  </tbody>
</table>

`--continue` と `--force` は `sync` にだけ付くオプションです。他のコマンドにオプションはありません。

---

### sync

```bash
.venv/bin/python main.py -v sync --project newproj
```

マウントを走査して、新規・変更された文書だけを変換 → 生成 → 公開します。同期済みの状態で実行すれば変更がないため数十秒、1回目は文書量に応じて数時間かかります。

#### `--continue`（続きから実行）

```bash
.venv/bin/python main.py -v sync --project newproj --continue
```

`sync` の処理は次の順に進みます。

- 文書ごとに 変換 → ページ生成 → GROWIへ公開 → ローカルで確定。確定した文書は、元ファイルが変わわない限りやり直しません
- 全文書が終わったら、文書間のリンクをまとめて1回付けてから確定し、リンクで変わっただけのページを再公開します
- 最後に索引ページを公開します

例として、5文書を `sync` して3文書まで生成と公開が終わった時点で Ctrl-C や障害で止まった場合を考えます。

- 確定済みの3文書は再生成も再公開もされません。マウントを見て変わっていない文書は対象に入らないため、コストもかかりません
- 作りかけだった4文書目は、変換からページ生成までを作り直します。すでにGROWIへ送ったページはそのままなので、送信だけが足りないなら `publish` でも足ります
- `--continue` は、それまでに払った解析作業を再利用します。リンク処理でページから取り出したエンティティや振る舞い、Jev による役回り判定とリンク候補の判定結果はローカルにキャッシュされているため、解析済みの文書は実行し直さず、未処理の文書だけを進めます。外すとリンク対象の文書をもう一度解析し直すので、その分のLLM呼び出しを取り直すことになります

#### `--force "<path>"`（1文書だけ再生成）

```bash
.venv/bin/python main.py -v sync --project newproj --force "マニュアル/取扱説明書.pdf"
```

パスはマウントからの相対パスで指定します。変更があってもなくても再生成して公開し、他の文書には手をつけません。

---

### publish

```bash
.venv/bin/python main.py publish --project newproj
```

再生成せず、ローカルに出来ているページをGROWIへ送信します。送信中に失敗して残っても、もう一度実行すれば未送信分が送られます。

---

### pull

```bash
.venv/bin/python main.py pull --project newproj
.venv/bin/python main.py human status --project newproj
```

GROWI上で人が編集した内容を取り込み、次の同期で消えないようにします。`human status` は人手編集が残っているページと、競合して保留になっているページを一覧表示します。競合はGROWI側のページを編集して解決してください。

---

### reset

```bash
.venv/bin/python main.py reset --project newproj
```

このツールが公開したGROWIページを削除し、ローカルの公開状態を初期化します。人がGROWIで手動作成したページは削除されません。再度 `sync` を実行すれば作り直せます。

---

## 9. 参考：環境変数

ルートの `.env` に設定します。`.env` は上側が運用で変更する項目、下側が開発者向けフラグで、下側は確認できるまで変更しないでください。設定は `.env` → `configs/<name>.ini` → コマンドライン引数の順で上書きされ、`.env` は全プロジェクト共通、プロジェクトファイルはそのプロジェクト分だけを上書きします。`.env.example` は無いので `.env` を直接編集してください。実接続先やトークンを公開リポジトリへコミットしないよう注意してください。

---

### LLM

| 変数 | 用途 | 設定例 | 変更すると |
| --- | --- | --- | --- |
| `OPENAI_BASE_URL` | LLMエンドポイントのURL | `http://10.160.144.101:51029/v1` | 生成に使うLLMそのものが切り替わります。間違えると `check` の `chat` が `DOWN` になり、生成は1件も進みません |
| `OPENAI_API_KEY` | APIキー。鍵が不要なエンドポイントでは `local` | `local` / `sk-...` | 鍵が合わないと `chat` が401になり、`sync` が1文書目で止まります |
| `OPENAI_MODEL_NAME` | エンドポイント上のモデル名 | `gemma-4-31B` | 別モデルで生成され、ページの書き味と要約の粒度が変わります。存在しない名前だと生成がエラーになります |
| `OPENAI_CONCURRENCY` | 同時実行数（既定 `4`） | `2`（複数同時）/ `4`〜`5`（1〜2件） | 上げるほど速い反面、エンドポイントが捌けない分は待ち→タイムアウトになります。プロジェクト数で割ってください |
| `WIKI_REQUEST_TIMEOUT` | モデル1回の待ち時間上限（秒、既定 `300`） | `900` | 大きい文書でのタイムアウトが減ります。上げすぎると障害の検知が遅れ、止まったまま長く待ちます |

---

### doc-parser（文書変換サービス）

| 変数 | 用途 | 設定例 | 変更すると |
| --- | --- | --- | --- |
| `WIKI_PARSER_BASE_URL` | 変換サービスのURL | `http://10.160.144.101:51027/agent/doc-parser/` | 未設定やURL違いだと `.pdf` / `.docx` / `.xlsx` などの変換が失敗します（`.md` / `.txt` だけのプロジェクトでは未設定で可） |
| `WIKI_PARSER_TIMEOUT` | 1ファイルの変換を待つ上限（秒、既定 `7200`） | `3600` / `14400` | 下げると大容量ファイルが途中で失敗し、上げるとサービス障害時に待ち続ける時間が長くなります |

---

### GROWI

| 変数 | 用途 | 設定例 | 変更すると |
| --- | --- | --- | --- |
| `GROWI_URL` | GROWIのURL | `http://10.160.152.235:3000/` | 公開先GROWIそのものです。プロジェクトINIに `growi_url` を書けば、そのプロジェクトだけ検証GROWIへ出せます |
| `GROWI_TOKEN` | APIトークン | `xEOD0xTA…`（`API Token:` 前置き無しでも可） | ページ作成の権限が無いと `publish` が失敗します。取り違えると別環境のGROWIへ書き込みます |
| `GROWI_MODE` | `attach`（既定）または `own` | `attach` | `attach` はプロジェクトフォルダ外へ書き込みません。`own` はページをこのツールの所有として扱います |
| `GROWI_TIMEOUT` | GROWI APIのタイムアウト（秒、既定 `30`） | `60` | GROWIが遅い環境で上げると、公開時の取りこぼしが減ります |

---

### 生成ページ

| 変数 | 用途 | 設定例 | 変更すると |
| --- | --- | --- | --- |
| `WIKI_OUTPUT_LANGUAGE` | 生成ページの言語（既定 `Japanese (日本語)`） | `"Japanese (日本語)"` / `"English"` | これから生成されるページの言語が変わります。既存ページは `--force` で再生成しない限り変わりません |
| `WIKI_LINKER_ENABLED` | 文書間リンクを付けるか（既定 `true`） | `false` | リンク付与が走らなくなり、その分のLLM呼び出しとかかり時間が減ります |

---

## 10. 対応ファイル形式

- `.md`
- `.txt`
- `.pdf`
- `.docx`
- `.doc`
- `.pptx`
- `.xlsx`
- `.xlsm`
- `.xls`
- `.csv`

`.md` と `.txt` はそのまま使い、それ以外は doc-parser サービスで変換します。Word/Excel 97-2003（`.doc`、`.xls`）はあらかじめ新しい形式へ変換しておいてください。`.xlsm` のマクロは実行せず、ワークシートとマクロ内容（VBA）は別のページとして公開します。

---

## 11. ログと作業ファイル

- `<data_root>/<name>/raw/`: 変換後のテキスト
- `<data_root>/<name>/wiki/`: 生成されたページ
- `<data_root>/<name>/metadata/`: 公開状態、監視キュー
- `logs/<name>/`: 実行ごとに作り直す `live.log` / `debug.log` と、トークン・URLを伏せた `failed_docs.md`

`<data_root>/<name>/` を削除するとローカルの公開状態は無くなりますが、GROWIのページは残ります（削除は `reset` を使ってください）。ログと生成物には社内文書が含まれます。アクセス権と保存期間を運用環境のポリシーに合わせて設定してください。
