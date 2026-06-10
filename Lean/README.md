# Lean MCP Server

Lean 4 で命題・証明片をチェックするための MCP サーバーです。

## 方針

- `shell=True` は使わず、`lean` を argv で直接起動
- `lake` や任意 shell は公開しない
- 一時 `.lean` ファイルは `--workdir` 配下の `tmp/` にのみ作成
- `lean --json --trust=0` で検査し、結果を JSON message として返す
- `import` と外部 `LEAN_PATH` を許可し、Mathlib などの既存ライブラリを利用できる
- `check_lean_file` は `--workdir` または読み取りルート（`--read-root`、既定はプロジェクトルート）配下の `.lean` ファイルを読める
- `sorry` / `admit` は禁止
- `axiom`, `constant`, `opaque`, `unsafe`, `extern` は禁止
- `#eval`, `run_cmd`, `initialize`, `macro`, `elab`, `syntax` など実行・拡張系は禁止

## インストール

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Lean 4 (`lean`) が必要です。Mathlib を使う場合は、通常の Lean/Lake 環境と同様に
`LEAN_PATH` などから読み込める状態にしてください。

## 起動

```sh
python3 lean_mcp_server.py --workdir ./work --read-root ..
```

`--read-root` を省略するとプロジェクトルートが既定値になります（環境変数 `LEAN_MCP_READ_ROOT` でも指定可）。

## Codex への登録例

```sh
codex mcp add restricted-lean -- python3 /path/to/project/Lean/lean_mcp_server.py --workdir /path/to/project/Lean/work --read-root /path/to/project
```

## 主なツール

- `check_lean_code`
- `check_lean_file`
- `get_lean_environment`
- `list_server_limits`

## 向いている用途 / 向かない用途

向いている用途:

- 命題、補題、証明断片が Lean 4 で実際に通るか確認する
- 仕様の不変条件や小さな数学的性質を形式的に検査する
- `sorry` / `admit` / `axiom` に頼らない証明確認
- Codex が書いた証明案の構文・型・論理の誤りを早めに検出する用途

向かない用途:

- 任意コード実行、ビルド、テストランナー、`lake` プロジェクト操作
- 実行結果を得るための `#eval` や IO 処理
- 形式化コストに見合わない一回限りの軽い確認

典型例:

- `example (P Q : Prop) : P -> Q -> P := ...` のような証明片を検査する
- 既存 Mathlib 環境がある場合に、補題が現在の import で通るか確認する
- 仕様メモの不変条件を小さな Lean 命題として表し、証明できるか試す

## `check_lean_code` の例

```json
{
  "code": "example (P Q : Prop) : P -> Q -> P := by\n  intro hp _\n  exact hp\n"
}
```

## `check_lean_file` の例

読み取りルートまたは作業ディレクトリ配下の `.lean` ファイルを検査します。

```json
{
  "path": "examples/ok.lean"
}
```

## 注意

これは「安全性を高めた実験用 MCP」です。完全なサンドボックスではありません。
未信頼コードを扱う場合は、専用ユーザーやコンテナを併用してください。
