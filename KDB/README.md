# Restricted KDB-X/q MCP Server

[日本語版](README.ja.md)

KDB-X/q の小さな式・定義を実行する制限付き MCP サーバーです。

## 方針

- `shell=True` は使わず、`q` を argv で直接起動
- q プロセスはリクエストごとに起動し、永続状態は `--workdir` 配下の `state/session.q` を再生して表現
- q の backslash command、`system`、任意のファイル I/O、socket/file handle、`.Q` / `.z` への直接アクセスはユーザーコードでは禁止
- CSV/TSV/TXT 読み込みだけは専用ツール `load_csv` で許可。`--workdir` または `--csv-root` 配下のファイルを q の `0:` parser で読み込む
- CSV/TSV 書き出しは専用ツール `save_csv` で許可。q の `0:` text writer をサーバー側でパス検証したうえで実行し、書き込み先は `--workdir` または `--csv-root` 配下の `.csv` / `.tsv` / `.txt` に限定する（ユーザーコードからの `0:` / `` `: `` は引き続き禁止）
- テーブルのバイナリ永続化は専用ツール `save_table` / `load_table` で許可。`set` / `get` はサーバー側でパス検証したうえで実行し、書き込み・読み込み先は `--workdir` 配下に限定する（ユーザーコードからの `set` / `get` / `` `: `` は引き続き禁止）
- 実行時間、入力サイズ、状態サイズ、出力サイズを制限

## インストール

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

KDB-X/q (`q`) が必要です。

## 起動

```sh
python3 kdb_mcp_server.py --workdir ./work --csv-root ..
```

`--csv-root` を省略した場合は、`KDB_MCP_CSV_ROOT`、`CODEX_CWD`、サーバースクリプト位置から推定したプロジェクトルートの順に使います。

## Codex への登録例

```sh
codex mcp add restricted-kdb -- python3 /path/to/project/KDB/kdb_mcp_server.py --workdir /path/to/project/KDB/work --csv-root /path/to/project
```

## 主なツール

- `run_q`
- `load_csv`
- `save_csv`
- `save_table`
- `load_table`
- `get_interpreter_state`
- `get_state_source`
- `prune_state`
- `reset_interpreter_state`
- `list_server_limits`

## 向いている用途 / 向かない用途

向いている用途:

- 大きめの CSV/TSV を q table として読み込み、LLM に全量を返さずに集計する
- 株価・市場データ・センサーデータなどの時系列処理
- 日付フィルタ、銘柄別集計、移動平均、前日比較、シグナル抽出
- `save_table` / `load_table` によるテーブルの再利用と高速復元
- `save_csv` による集計済み結果や小さな抽出結果のファイル出力

向かない用途:

- 汎用ファイル操作、任意 OS コマンド、ネットワーク接続
- q に不慣れな利用者が長い業務ロジックを保守する用途
- SQL データベースや Python/R の既存分析基盤がすでにある処理の全面置き換え

典型例:

- `load_csv` で日次株価を読み込み、`select by sym` で銘柄別に集計する
- `25 mavg Close` と `75 mavg Close` で移動平均を作り、クロス発生日だけを抽出する
- `Date within 2024.01.01 2024.12.31` のように期間を絞り、結果だけ `save_csv` で出力する
- 一度読み込んだ大きなテーブルを `save_table` で保存し、次回以降は `load_table` で復元する

## 実行モデルの改善点

- **複数行ステートメント対応**：q のスクリプト継続規則（行頭が空白の行は前行の続き）をサーバー側で適用してから stdin に渡すため、複数行に分けた `select` / テーブルリテラル / ラムダもそのまま実行できます（裸の式の自動表示も維持）。
- **リプレイ出力の抑制**：各呼び出しで再生される `state/session.q` の出力（過去に永続化した `show` など）は捨て、**そのコード自身の出力だけ**を返します。
- **コンソール非切り捨て**：`-c` で十分大きなコンソールを設定するため、`show` が行数で省略（`..`）されません。
- **状態の衛生管理**：`get_state_source` で `session.q` を行番号つきで確認し、`prune_state` で不要な行（残骸の `show` や上書きされた定義）だけを削除できます（**削除のみ**・再生検証つき・失敗時はロールバック。全消去は従来どおり `reset_interpreter_state`）。

## `run_q` の例

```json
{
  "code": "f:{x+1}\nf 41",
  "persist_code": true
}
```

## `load_csv` の例

`load_csv` は q の `0:` CSV parser を使って、ヘッダー行つき CSV を q table に直接ロードします。
読み込み可能なのは `--workdir` または `--csv-root` 配下の `.csv` / `.tsv` / `.txt` です。
デフォルトではロード式を `state/session.q` に保存するため、後続の `run_q` でテーブルを参照できます。

```json
{
  "table_name": "prices",
  "input_file": "data/toyota_daily.csv",
  "types": "DFFFFFJ",
  "delimiter": ","
}
```

その後:

```json
{
  "code": "select avgClose:avg Close from prices"
}
```

`types` は q の CSV 型指定文字列です。例: `DFFFFFJ` は Date、Open、High、Low、Close、Adj Close、Volume を想定します。

## `save_csv` の例

`save_csv` は `load_csv` の逆操作で、メモリ上の q table を q の `0:` writer で区切りテキストに書き出します。
1 行目はテーブルの列名がヘッダーになります。書き込み先は `--workdir` または `--csv-root` 配下の
`.csv` / `.tsv` / `.txt` に限定され、`delimiter` は 1 文字（CSV は `,`、TSV は `\t`）です。
相対パスは `--workdir` 基準で解決されます（`--csv-root` 配下に書きたい場合は絶対パスを指定）。

```json
{
  "table_name": "sony",
  "dest_file": "out/sony.csv",
  "delimiter": ","
}
```

`0:` text save はサーバー側でのみ発行され、ユーザーコード (`run_q`) からの `0:` / `` `: `` は引き続き拒否されます。
状態 (`state/session.q`) には保存されません（テーブルの中身はその時点のスナップショットとして書き出されます）。

## `save_table` / `load_table` の例

`load_csv` で読み込んだテーブルは、そのままでは `state/session.q` の再生（＝元 CSV の再パース）に依存します。
`save_table` でテーブルをバイナリファイルとして `--workdir` 配下に保存すると、元 CSV に依存せず、再起動後も `load_table` で高速に復元できます。

保存:

```json
{
  "table_name": "sony",
  "dest_file": "tables/sony.kdb"
}
```

復元（`persist_code` 既定 true なので、以後の全セッションで `state/session.q` がバイナリから自動復元）:

```json
{
  "table_name": "sony",
  "src_file": "tables/sony.kdb"
}
```

`set` / `get` はサーバー側でのみ発行され、保存・読み込み先は `--workdir` 配下に限定されます。
ユーザーコード (`run_q`) からの `set` / `get` / `` `: `` は引き続き拒否されます。

## 注意

これは「安全性を高めた実験用 MCP」です。完全なサンドボックスではありません。
KDB-X/q は OS・ファイル・ネットワークへ触れる機能が多いため、未信頼コードを扱う場合は専用ユーザーやコンテナを併用してください。
