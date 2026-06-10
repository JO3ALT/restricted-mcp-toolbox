# Restricted UNIX Filter MCP Server

[English](README.md)

任意の `bash` を実行しない、制限付きの UNIX フィルタ MCP です。
`awk`, `sed`, `sort`, `uniq`, `cut`, `tr`, `grep`, `head`, `tail`, `wc`, `cat` などを許可リスト方式でパイプライン実行します。

## 方針

- `shell=True` は使わない
- 許可コマンドのみ実行
- 読み込みは作業ディレクトリ (`--workdir`) または読み取りルート (`--read-root`、既定はプロジェクトルート) 配下に限定
- 書き込みは作業ディレクトリ配下のみ (`output_file` / `save_pipeline_script`)
- `sed -i`、再帰 grep、出力先指定系などは禁止
- コマンド引数でファイルを直接渡すことは禁止し、入力は `input_text` / `input_file` に限定
- `awk system()`, `awk getline`, `awk` の外部パイプ・ファイルリダイレクト、`sed e/r/w` は禁止
- `awk -f` / `sed -f` のスクリプトファイルは、読み取りルート配下の `.awk` / `.sed` または拡張子なしファイルに限り許可

## インストール

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## 起動

```sh
python3 filter_mcp_server.py --workdir ./work --read-root ..
```

`--read-root` を省略するとプロジェクトルートが既定値になります。
環境変数 `FILTER_MCP_READ_ROOT` でも指定できます。

## 主なツール

- `list_allowed_commands`
- `list_files`
- `preview_file`
- `csv_summary`
- `run_filter_pipeline`
- `group_by_count`
- `save_pipeline_script`

## 向いている用途 / 向かない用途

向いている用途:

- CSV/TSV やログの先頭確認、行数確認、列抽出、単純な集計
- `sort` / `uniq -c` / `wc` で済む頻度集計や重複確認
- LLM に大きなファイルを直接渡す前の軽量な前処理

向かない用途:

- 複雑なデータ変換、結合、型つき集計
- ファイルシステムを広く探索する処理や再帰 grep
- 外部コマンド、ネットワーク、破壊的操作が必要な処理

## 例

```json
{
  "stages": [
    {"cmd": "awk", "args": ["-F,", "{print $2}"]},
    {"cmd": "sort", "args": []},
    {"cmd": "uniq", "args": ["-c"]},
    {"cmd": "sort", "args": ["-nr"]}
  ],
  "input_file": "data.csv",
  "output_file": "counts.txt"
}
```

これは概ね次の処理に相当します。

```sh
awk -F, '{print $2}' < data.csv | sort | uniq -c | sort -nr > counts.txt
```

## 注意

これは安全性を高めた実験用 MCP です。完全なサンドボックスではありません。
未信頼データを扱う場合は、コンテナ、専用ユーザー、読み取り専用入力ディレクトリを併用してください。
