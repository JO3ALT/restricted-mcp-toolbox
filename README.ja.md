# Restricted MCP Servers

[English](README.md)

Codex などの MCP クライアントから使う、目的別の制限付き MCP サーバー集です。
汎用 shell を公開せず、用途ごとに狭い機能だけを提供する構成です。

## サーバー

- `filter/`: 小さなテキストや CSV の確認に使う制限付き UNIX フィルタパイプライン。
- `KDB/`: 表形式データや時系列分析に使う制限付き KDB-X/q 実行環境。
- `Prolog/`: ルールや論理関係の問い合わせに使う制限付き SWI-Prolog 実行環境。
- `Lean/`: Lean 4 の証明片チェックに使う制限付き実行環境。

各サーバーのディレクトリには、セットアップ、登録例、ツール一覧、用途を説明する README があります。

## 設計方針

- 任意の shell 実行を MCP ツールとして公開しない。
- 各サーバーは明確で狭い機能に絞る。
- 実行時間、入力サイズ、状態サイズ、出力サイズを制限する。
- 読み込みは作業ディレクトリと設定済み読み取りルートに限定する。
- 書き込みは原則として作業ディレクトリに限定する。ただし KDB/q の `save_csv` のように、明示的に文書化した出力ツールは例外とする。
- ソースファイルを読み込む場合も、パス検証をサーバー側で行う。
- 大きな生データを返さず、小さな要約や結果表を返す。

## 基本セットアップ

サーバーごと、またはリポジトリルートで Python 環境を作り、必要な `requirements.txt` をインストールします。

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r filter/requirements.txt
pip install -r KDB/requirements.txt
pip install -r Prolog/requirements.txt
pip install -r Lean/requirements.txt
```

実行時依存はホスト環境側で用意します。

- `filter`: `awk`, `sed`, `sort`, `uniq`, `head`, `tail`, `wc` などの標準 UNIX フィルタコマンド。
- `KDB`: `q` として実行できる KDB-X/q、または `KDB_Q_BIN` で指定した実行ファイル。
- `Prolog`: `swipl` として実行できる SWI-Prolog、または `SWIPL_BIN` で指定した実行ファイル。
- `Lean`: `lean` として実行できる Lean、または `LEAN_BIN` で指定した実行ファイル。

## リポジトリに含めるもの

バージョン管理対象は、サーバーコード、テスト、requirements、ドキュメントを想定しています。
実行時状態、生成物、ローカルデータセット、Python キャッシュ、マシン固有のラッパーはコミットしない方針です。

Codex に守らせたい運用ルールは `AGENTS.md` を参照してください。

## ライセンス

MIT License です。詳しくは [LICENSE](LICENSE) を参照してください。
