# Restricted UNIX Filter MCP Server

[日本語版](README.ja.md)

任意の `bash` を実行しない、制限付きのUNIXフィルタMCPです。
`awk`, `sed`, `sort`, `uniq`, `cut`, `tr`, `grep`, `head`, `tail`, `wc`, `cat` などを許可リスト方式でパイプライン実行します。

## 方針

- `shell=True` は使わない
- 許可コマンドのみ実行
- **読み込み**は作業ディレクトリ (`--workdir`) または読み取りルート (`--read-root`、既定はプロジェクトルート) 配下に限定
- **書き込み**は作業ディレクトリ配下のみ（`output_file` / `save_pipeline_script`）
- `sed -i`、再帰grep、出力先指定系などは禁止
- コマンド引数でファイルを直接渡すことは禁止し、入力は `input_text` / `input_file` に限定
- `awk system()`, `awk getline`, `awk` の外部パイプ・ファイルリダイレクト、`sed e/r/w` は禁止
- `awk -f` / `sed -f` のスクリプトファイルは、読み取りルート配下の `.awk` / `.sed` または拡張子なしファイルに限り許可。サーバー側が中身を読み、インラインプログラムと同じ禁止ルールで再検証してからインライン実行する（子プロセスに生のパスは渡さない）
- 実行結果と等価シェルスクリプトを返す

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

`--read-root` を省略するとプロジェクトルート（このスクリプトの親ディレクトリ）が既定値になります。
環境変数 `FILTER_MCP_READ_ROOT` でも指定できます。

## Claude Desktop 等の設定例

```json
{
  "mcpServers": {
    "restricted-filter": {
      "command": "python3",
      "args": ["/path/to/filter_mcp_server.py", "--workdir", "/path/to/work", "--read-root", "/path/to/project"]
    }
  }
}
```

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
- 任意 shell を許可せずに、定型的な UNIX フィルタだけを使いたい場面

向かない用途:

- 複雑なデータ変換、結合、型つき集計
- ファイルシステムを広く探索する処理や再帰 grep
- 外部コマンド、ネットワーク、破壊的操作が必要な処理

よく使う処理例:

- `csv_summary` でヘッダー、行数、サンプル行を確認する
- `group_by_count` でカテゴリ列の件数を集計する
- `run_filter_pipeline` で `awk | sort | uniq -c | sort -nr` のような小さな集計を行う

## `run_filter_pipeline` の例

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

等価な処理は概ね次です。

```sh
awk -F, '{print $2}' < data.csv | sort | uniq -c | sort -nr > counts.txt
```

## スクリプトファイル (`awk -f` / `sed -f`) の例

読み取りルート配下の `.awk` / `.sed` または拡張子なしファイルを読み込んで実行できます。
指定形式は `-f PATH` / `--file PATH` / `--file=PATH`。

```json
{
  "stages": [
    {"cmd": "awk", "args": ["-F,", "-f", "scripts/agg.awk"]},
    {"cmd": "sed", "args": ["--file=scripts/normalize.sed"]}
  ],
  "input_file": "data.csv"
}
```

スクリプトの中身もインラインプログラムと同じ規則で検証されるため、`system()` /
`getline` / パイプ / リダイレクト（awk）や `e` / `r` / `w`（sed）を含むスクリプトは拒否されます。
インライン awk が `<` を一律禁止している都合上、`$3 < 5` のような数値比較を含むスクリプトも拒否されます。

## 注意

これは「安全性を高めた実験用MCP」です。完全なサンドボックスではありません。
学生・事務用途に配布する場合は、コンテナ、専用ユーザー、読み取り専用入力ディレクトリを併用してください。
