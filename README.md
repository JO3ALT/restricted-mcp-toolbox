# Restricted MCP Servers

[日本語版](README.ja.md)

Purpose-built MCP servers for Codex and other MCP clients. The repository is
organized around narrow tools rather than exposing a general shell.

## Servers

- `filter/`: restricted UNIX filter pipelines for small text and CSV checks.
- `KDB/`: restricted KDB-X/q execution for table and time-series analysis.
- `Prolog/`: restricted SWI-Prolog queries for rules and logical relations.
- `Lean/`: restricted Lean 4 proof checking.

Each server has its own `README.md` with setup, registration examples, tool
lists, and intended use cases.

## Design Principles

- Do not expose arbitrary shell execution as an MCP tool.
- Keep each server focused on a small, explicit capability set.
- Limit execution time, input size, state size, and output size.
- Restrict reads to the server work directory and a configured read root.
- Restrict writes to the server work directory, except for explicitly documented
  export tools such as KDB/q `save_csv`.
- Validate file paths server-side, including when source files are loaded.
- Return compact summaries or result tables instead of large raw datasets.

## Typical Setup

Create a Python environment per server or at the repository root, then install
the relevant requirements file:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r filter/requirements.txt
pip install -r KDB/requirements.txt
pip install -r Prolog/requirements.txt
pip install -r Lean/requirements.txt
```

Runtime dependencies are provided by the host environment:

- `filter`: standard UNIX filter commands such as `awk`, `sed`, `sort`, `uniq`,
  `head`, `tail`, and `wc`.
- `KDB`: KDB-X/q executable available as `q` or via `KDB_Q_BIN`.
- `Prolog`: SWI-Prolog executable available as `swipl` or via
  `SWIPL_BIN`.
- `Lean`: Lean executable available as `lean` or via `LEAN_BIN`.

## Repository Contents

Only server code, tests, requirements, and documentation are intended for
version control. Runtime state, generated outputs, local datasets, Python cache
files, and machine-specific wrappers should stay out of commits.

See `AGENTS.md` for the operating rules intended for Codex.

## License

MIT License. See [LICENSE](LICENSE).
