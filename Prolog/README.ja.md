# SWI-Prolog MCP Server

[English](README.md)

任意の shell 実行を避けつつ、SWI-Prolog の問い合わせを実行する MCP サーバーです。
ルール、制約、依存関係、到達可能性などを Prolog に問い合わせる用途に向いています。

## 方針

- `shell=True` は使わず、`swipl` を argv で直接起動
- 状態ファイルは `--workdir` 配下の `state/session.pl` のみ
- `use_module(library(...))` による SWI-Prolog ライブラリ読み込みを許可
- `consult`、`ensure_loaded`、`load_files` は読み取りルートまたは作業ディレクトリ配下、または `library(...)` に限定
- `run_prolog_file` は `.pl` / `.pro` / `.prolog` のみを対象にする
- 外部プロセス起動につながる `shell`、`process_create` などは拒否

## インストール

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

SWI-Prolog (`swipl`) が必要です。

## 起動

```sh
python3 prolog_mcp_server.py --workdir ./work --read-root ..
```

`--read-root` を省略するとプロジェクトルートが既定値になります。

## 主なツール

- `run_prolog`
- `run_prolog_file`
- `get_interpreter_state`
- `reset_interpreter_state`
- `list_server_limits`

## 向いている用途 / 向かない用途

向いている用途:

- ルール、制約、依存関係、到達可能性の問い合わせ
- 親子関係、継承、権限、ワークフロー遷移などの論理モデル検査
- 小さな事実集合に規則を足して、条件を満たす候補を列挙する処理

向かない用途:

- 表形式データの大規模集計や数値計算
- ファイル操作、外部プロセス、ネットワークを伴う処理
- Prolog の規則として表しにくい通常のアプリケーションロジック全般

## 例

```json
{
  "query": "ancestor(X, bob).",
  "program_text": "parent(alice, bob).\nancestor(X, Y) :- parent(X, Y).\n",
  "persist_program": true,
  "max_solutions": 20
}
```

## 注意

これは安全性を高めた実験用 MCP です。完全なサンドボックスではありません。
未信頼コードを扱う場合は、専用ユーザー、コンテナ、読み取り専用入力ディレクトリを併用してください。
