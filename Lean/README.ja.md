# Lean MCP Server

[English](README.md)

Lean 4 で命題・証明片をチェックするための MCP サーバーです。
`sorry` や `admit` に頼らず、証明片が本当に Lean で通るか確認する用途に向いています。

## 方針

- `shell=True` は使わず、`lean` を argv で直接起動
- `lake` や任意 shell は公開しない
- 一時 `.lean` ファイルは `--workdir` 配下の `tmp/` にのみ作成
- `lean --json --trust=0` で検査し、結果を JSON message として返す
- `import` と外部 `LEAN_PATH` を許可
- `sorry` / `admit` は禁止
- `axiom`, `constant`, `opaque`, `unsafe`, `extern` は禁止
- `#eval`, `run_cmd`, `initialize`, `macro`, `elab`, `syntax` など実行・拡張系は禁止

## インストール

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Lean 4 (`lean`) が必要です。Mathlib を使う場合は、通常の Lean/Lake 環境と同様に `LEAN_PATH` などから読み込める状態にしてください。

## 起動

```sh
python3 lean_mcp_server.py --workdir ./work --read-root ..
```

`--read-root` を省略するとプロジェクトルートが既定値になります。

## 主なツール

- `check_lean_code`
- `check_lean_file`
- `get_lean_environment`
- `list_server_limits`

## 向いている用途 / 向かない用途

向いている用途:

- 命題、補題、証明断片が Lean 4 で実際に通るか確認する
- 仕様の不変条件や小さな数学的性質を形式的に検査する
- Codex が書いた証明案の構文・型・論理の誤りを早めに検出する

向かない用途:

- 任意コード実行、ビルド、テストランナー、`lake` プロジェクト操作
- 実行結果を得るための `#eval` や IO 処理
- 形式化コストに見合わない一回限りの軽い確認

## 例

```json
{
  "code": "example (P Q : Prop) : P -> Q -> P := by\n  intro hp _\n  exact hp\n"
}
```

## 注意

これは安全性を高めた実験用 MCP です。完全なサンドボックスではありません。
未信頼コードを扱う場合は、専用ユーザーやコンテナを併用してください。
