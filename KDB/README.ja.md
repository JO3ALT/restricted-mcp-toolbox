# Restricted KDB-X/q MCP Server

[English](README.md)

KDB-X/q の小さな式・定義を実行する制限付き MCP サーバーです。
大きめの表形式データや時系列データを、LLM に全量で返さず q 側で処理する用途に向いています。

## 方針

- `shell=True` は使わず、`q` を argv で直接起動
- q プロセスはリクエストごとに起動し、永続状態は `--workdir` 配下の `state/session.q` を再生して表現
- backslash command、`system`、任意のファイル I/O、socket/file handle、`.Q` / `.z` への直接アクセスはユーザーコードでは禁止
- CSV/TSV/TXT 読み込みは専用ツール `load_csv` で許可
- CSV/TSV/TXT 書き出しは専用ツール `save_csv` で許可
- バイナリテーブルの保存・復元は `save_table` / `load_table` で許可
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

- 大きめの CSV/TSV を q table として読み込み、集計する
- 株価・市場データ・センサーデータなどの時系列処理
- 日付フィルタ、銘柄別集計、移動平均、前日比較、シグナル抽出
- `save_table` / `load_table` によるテーブルの再利用

向かない用途:

- 汎用ファイル操作、任意 OS コマンド、ネットワーク接続
- q に不慣れな利用者が長い業務ロジックを保守する用途
- SQL データベースや Python/R の既存分析基盤の全面置き換え

## 典型例

- `load_csv` で日次株価を読み込み、`select by sym` で銘柄別に集計する
- `25 mavg Close` と `75 mavg Close` で移動平均を作り、クロス発生日だけを抽出する
- `Date within 2024.01.01 2024.12.31` のように期間を絞る
- 集計済み結果を `save_csv` で出力する

## 状態管理

`run_q` で `persist_code=true` にすると、コードは `state/session.q` に保存されます。
不要な `show` や古い定義は、`get_state_source` で行番号を確認してから `prune_state` で削除できます。

## 注意

これは安全性を高めた実験用 MCP です。完全なサンドボックスではありません。
KDB-X/q は OS・ファイル・ネットワークへ触れる機能が多いため、未信頼コードを扱う場合は専用ユーザーやコンテナを併用してください。
