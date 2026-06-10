# SWI-Prolog MCP Server

[日本語版](README.ja.md)

任意の shell 実行を避けつつ、SWI-Prolog の問い合わせを実行する MCP サーバーです。

## 方針

- `shell=True` は使わず、`swipl` を argv で直接起動
- 状態ファイルは `--workdir` 配下の `state/session.pl` のみ
- Prolog ソースは term として読み込む
- `use_module(library(...))` による SWI-Prolog ライブラリ読み込みを許可
- `consult`、`ensure_loaded`、`load_files` は読み取りルート（`--read-root`、既定はプロジェクトルート）または作業ディレクトリ配下のソース、または `library(...)` 参照に限って許可
- `run_prolog_file` で、読み取りルート／作業ディレクトリ配下の `.pl` / `.pro` / `.prolog` を直接ロードして問い合わせできる（サーバー側でパス検証）
- 外部プロセス起動につながる `shell`、`process_create` などは拒否
- 実行時間、解数、入力サイズ、出力サイズを制限

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

`--read-root` を省略するとプロジェクトルートが既定値になります（環境変数 `PROLOG_MCP_READ_ROOT` でも指定可）。

## Codex への登録例

```sh
codex mcp add restricted-prolog -- python3 /path/to/project/Prolog/prolog_mcp_server.py --workdir /path/to/project/Prolog/work --read-root /path/to/project
```

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
- 小さな事実集合に規則を足して、「条件を満たす候補」を列挙する処理
- Codex が推論過程を Prolog に任せ、結果だけを要約する用途

向かない用途:

- 表形式データの大規模集計や数値計算
- ファイル操作、外部プロセス、ネットワークを伴う処理
- Prolog の規則として表しにくい通常のアプリケーションロジック全般

典型例:

- `parent/2` と `ancestor/2` で関係をたどる
- `depends_on/2` から推移的な依存先を列挙する
- `can_access(User, Resource)` のような権限規則を検査する

## `run_prolog` の例

```json
{
  "query": "ancestor(X, bob).",
  "program_text": "parent(alice, bob).\nancestor(X, Y) :- parent(X, Y).\n",
  "persist_program": true,
  "max_solutions": 20
}
```

## `run_prolog_file` の例

読み取りルート配下の `.pl` ファイルをロードしてから問い合わせます。

```json
{
  "file": "examples/family.pl",
  "query": "ancestor(alice, X).",
  "persist_program": false,
  "max_solutions": 20
}
```

## 注意

これは「安全性を高めた実験用 MCP」です。完全なサンドボックスではありません。
未信頼コードを扱う場合は、専用ユーザー、コンテナ、読み取り専用入力ディレクトリを併用してください。
