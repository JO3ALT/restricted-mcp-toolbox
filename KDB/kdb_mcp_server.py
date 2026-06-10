#!/usr/bin/env python3
"""
Restricted KDB-X/q MCP Server.

The server exposes a small validation-oriented q execution surface. It never
uses shell=True; q is started directly with subprocess argv and receives code
on stdin. Persistent state is represented by replaying WORKDIR/state/session.q
before each query.
"""

from __future__ import annotations

import argparse
import os
import re
import resource
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:
    from mcp.server.fastmcp import FastMCP
except Exception:  # pragma: no cover - lets local tests import without mcp installed
    class FastMCP:  # type: ignore[no-redef]
        def __init__(self, name: str) -> None:
            self.name = name

        def tool(self) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
            def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
                return func
            return decorator

        def run(self) -> None:
            raise SystemExit("This server requires the MCP Python SDK. Install with: pip install mcp")


mcp = FastMCP("restricted-kdb-q-mcp")

WORKDIR = Path.cwd().resolve()
STATE_FILE = WORKDIR / "state" / "session.q"
CODEX_CSV_ROOT = Path(
    os.environ.get(
        "KDB_MCP_CSV_ROOT",
        os.environ.get("CODEX_CWD", str(Path(__file__).resolve().parents[1])),
    )
).expanduser().resolve()
SAFE_PATH = f"{Path.home() / '.kx' / 'bin'}:/usr/bin:/bin:/usr/local/bin"
MAX_CODE_BYTES = 512 * 1024
MAX_STATE_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES = 5 * 1024 * 1024
DEFAULT_TIMEOUT_SEC = 10
# Console size passed to q via -c so show does not truncate tall/wide results.
CONSOLE_ROWS = 1_000_000
CONSOLE_COLS = 2000
# Printed between the replayed state and the user code; everything up to and
# including this marker line is stripped so callers see only their own output.
# Must be printable ASCII -- raw control bytes make q raise a 'char source error.
OUTPUT_SENTINEL = "__KDBMCP_OUTPUT_BEGIN_9c3a1f__"

BANNED_PATTERNS = [
    (re.compile(r"(^|[\s;])system(\s|$)"), "system command is disabled"),
    (re.compile(r"(^|\n)\s*\\"), "q backslash commands are disabled in user code"),
    (re.compile(r"\bhopen\b"), "hopen is disabled"),
    (re.compile(r"\bhclose\b"), "hclose is disabled"),
    (re.compile(r"\bhdel\b"), "hdel is disabled"),
    (re.compile(r"\bget\b"), "get is disabled"),
    (re.compile(r"\bset\b"), "set is disabled"),
    (re.compile(r"\bvalue\b"), "value is disabled"),
    (re.compile(r"\bparse\b"), "parse is disabled"),
    (re.compile(r"\beval\b"), "eval is disabled"),
    (re.compile(r"\bload\b"), "load is disabled"),
    (re.compile(r"\bsave\b"), "save is disabled"),
    (re.compile(r"\bdelete\b"), "delete is disabled"),
    (re.compile(r"\bupsert\b"), "upsert is disabled"),
    (re.compile(r"\binsert\b"), "insert is disabled"),
    (re.compile(r"\b.Q\."), ".Q namespace is disabled in user code"),
    (re.compile(r"\.z\."), ".z namespace is disabled in user code"),
    (re.compile(r"\.Q"), ".Q namespace is disabled in user code"),
    (re.compile(r"\.z"), ".z namespace is disabled in user code"),
    (re.compile(r"[012]:|\b[012]:\s*"), "file/socket handles are disabled"),
    (re.compile(r"`:"), "file symbols are disabled"),
]

Q_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
Q_TYPE_CHARS = set("BGHJIFSECMPDZUVT X")


def _set_workdir(path: str) -> None:
    global WORKDIR, STATE_FILE
    WORKDIR = Path(path).expanduser().resolve()
    STATE_FILE = WORKDIR / "state" / "session.q"
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        STATE_FILE.write_text("", encoding="utf-8")


def _set_codex_csv_root(path: str) -> None:
    global CODEX_CSV_ROOT
    CODEX_CSV_ROOT = Path(path).expanduser().resolve()


def _q_bin() -> str:
    resolved = shutil.which("q", path=SAFE_PATH)
    if resolved is None:
        raise FileNotFoundError("q not found on safe PATH")
    return resolved


def _safe_path(user_path: str, *, must_exist: bool = False) -> Path:
    p = (WORKDIR / user_path).resolve() if not Path(user_path).is_absolute() else Path(user_path).resolve()
    if not str(p).startswith(str(WORKDIR) + os.sep) and p != WORKDIR:
        raise ValueError(f"Path escapes workdir: {user_path}")
    if must_exist and not p.exists():
        raise FileNotFoundError(str(p))
    return p


def _is_under(path: Path, root: Path) -> bool:
    return path == root or str(path).startswith(str(root) + os.sep)


def _safe_csv_path(user_path: str, *, must_exist: bool = False) -> Path:
    raw = Path(user_path).expanduser()
    candidates = [raw.resolve()] if raw.is_absolute() else [
        (WORKDIR / raw).resolve(),
        (CODEX_CSV_ROOT / raw).resolve(),
    ]
    checked_roots = (WORKDIR, CODEX_CSV_ROOT)
    allowed_candidates = []
    for candidate in candidates:
        if not any(_is_under(candidate, root) for root in checked_roots):
            continue
        allowed_candidates.append(candidate)
        if must_exist and not candidate.exists():
            continue
        return candidate
    if not allowed_candidates:
        raise ValueError(f"Path escapes allowed CSV roots: {user_path}")
    if must_exist:
        raise FileNotFoundError(user_path)
    raise ValueError(f"Path escapes allowed CSV roots: {user_path}")


def _display_path(path: Path) -> str:
    for root in (WORKDIR, CODEX_CSV_ROOT):
        if _is_under(path, root):
            return str(path.relative_to(root))
    return str(path)


def _mask_comments_and_strings(code: str) -> str:
    chars = list(code)
    in_string = False
    in_line_comment = False
    i = 0
    while i < len(chars):
        if in_line_comment:
            if chars[i] == "\n":
                in_line_comment = False
            else:
                chars[i] = " "
            i += 1
            continue
        if in_string:
            if chars[i] == "\\":
                chars[i] = " "
                if i + 1 < len(chars):
                    chars[i + 1] = " "
                i += 2
                continue
            if chars[i] == '"':
                in_string = False
            chars[i] = " " if chars[i] != "\n" else "\n"
            i += 1
            continue
        if chars[i] == "/" and (i == 0 or chars[i - 1] == "\n"):
            chars[i] = " "
            in_line_comment = True
            i += 1
            continue
        if chars[i] == '"':
            chars[i] = " "
            in_string = True
        i += 1
    return "".join(chars)


def _join_continuations(code: str) -> str:
    """
    Merge q line-continuations so multi-line statements survive stdin execution.

    q's REPL (which is how code is fed here, to keep implicit result printing)
    evaluates one physical line at a time and does not honour continuation, so a
    statement split across lines fails. q's own script rule is that a line which
    begins with whitespace continues the previous line; we apply exactly that
    rule here, collapsing such lines onto their predecessor before sending the
    code to q. The continuation line's leading whitespace is preserved as token
    separation, matching q's loader.
    """
    out: List[str] = []
    for line in code.split("\n"):
        if out and line[:1] in (" ", "\t"):
            out[-1] = out[-1] + line
        else:
            out.append(line)
    return "\n".join(out)


def _strip_replay_output(stdout: str) -> str:
    """Drop everything up to and including the OUTPUT_SENTINEL marker line."""
    idx = stdout.rfind(OUTPUT_SENTINEL)
    if idx == -1:
        return stdout
    newline = stdout.find("\n", idx)
    return stdout[newline + 1:] if newline != -1 else ""


def _validate_code(name: str, code: str, max_bytes: int = MAX_CODE_BYTES) -> None:
    if "\x00" in code:
        raise ValueError(f"{name} contains a NUL byte")
    if len(code.encode("utf-8")) > max_bytes:
        raise ValueError(f"{name} too large")
    visible = _mask_comments_and_strings(code)
    for pattern, message in BANNED_PATTERNS:
        if pattern.search(visible):
            raise ValueError(message)


def _q_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _validate_q_identifier(name: str) -> None:
    if not Q_IDENTIFIER_RE.fullmatch(name):
        raise ValueError(f"Invalid q identifier: {name}")


def _validate_csv_schema(types: str, delimiter: str) -> None:
    if not types:
        raise ValueError("types must not be empty")
    if any(ch not in Q_TYPE_CHARS for ch in types):
        raise ValueError("types contains unsupported q type characters")
    if len(delimiter) != 1:
        raise ValueError("delimiter must be exactly one character")


def _limit_child_resources() -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (DEFAULT_TIMEOUT_SEC + 5, DEFAULT_TIMEOUT_SEC + 5))


def _run_q_script(script: str, timeout_sec: int = DEFAULT_TIMEOUT_SEC) -> Dict[str, Any]:
    if timeout_sec < 1 or timeout_sec > 60:
        raise ValueError("timeout_sec must be 1..60")
    start = time.perf_counter()
    try:
        completed = subprocess.run(
            [_q_bin(), "-q", "-c", str(CONSOLE_ROWS), str(CONSOLE_COLS)],
            input=script.encode("utf-8"),
            cwd=str(WORKDIR),
            env={"LC_ALL": "C", "PATH": SAFE_PATH, "HOME": str(WORKDIR)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_sec,
            preexec_fn=_limit_child_resources if hasattr(resource, "setrlimit") else None,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"q execution timed out after {timeout_sec} sec") from exc

    if len(completed.stdout) > MAX_OUTPUT_BYTES or len(completed.stderr) > MAX_OUTPUT_BYTES:
        raise ValueError("q output too large")
    stdout = _strip_replay_output(completed.stdout.decode("utf-8", errors="replace"))
    stderr = completed.stderr.decode("utf-8", errors="replace")
    return {
        "ok": completed.returncode == 0 and not stderr.strip().startswith("'"),
        "returncode": completed.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "elapsed_sec": time.perf_counter() - start,
    }


def _state_text() -> str:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        return ""
    if STATE_FILE.stat().st_size > MAX_STATE_BYTES:
        raise ValueError("state file too large")
    return STATE_FILE.read_text(encoding="utf-8", errors="replace")


def _script_for(code: str) -> str:
    # State and code are continuation-joined independently so a leading-whitespace
    # first line of code can never merge into the sentinel statement. The sentinel
    # is printed after the (silent-by-intent) state replay so callers see only the
    # output of their own code.
    state = _join_continuations(_state_text())
    body = _join_continuations(code)
    return f"{state}\n-1 {_q_string(OUTPUT_SENTINEL)};\n{body}\n\\\\\n"


def _csv_load_code(table_name: str, input_file: str, types: str, delimiter: str) -> str:
    _validate_q_identifier(table_name)
    _validate_csv_schema(types, delimiter)
    path = _safe_csv_path(input_file, must_exist=True)
    if not path.is_file():
        raise ValueError(f"Not a regular file: {input_file}")
    if path.suffix.lower() not in {".csv", ".tsv", ".txt"}:
        raise ValueError("Only .csv, .tsv, and .txt files are supported")
    return f"{table_name}:({_q_string(types)};enlist {_q_string(delimiter)}) 0: `$ {_q_string(':' + str(path))};"


def _save_table_code(table_name: str, dest_path: Path) -> str:
    _validate_q_identifier(table_name)
    return f"(`$ {_q_string(':' + str(dest_path))}) set {table_name};"


def _load_table_code(table_name: str, src_path: Path) -> str:
    _validate_q_identifier(table_name)
    return f"{table_name}: get `$ {_q_string(':' + str(src_path))};"


def _save_csv_code(table_name: str, dest_path: Path, delimiter: str) -> str:
    _validate_q_identifier(table_name)
    return (
        f"(`$ {_q_string(':' + str(dest_path))}) 0: "
        f"({_q_string(delimiter)} 0: {table_name});"
    )


@mcp.tool()
def run_q(
    code: str,
    persist_code: bool = False,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
) -> Dict[str, Any]:
    """
    Run sandbox-checked KDB-X/q code.

    The persisted interpreter state is replayed before code. If persist_code is
    true and validation/execution succeeds, code is appended to
    WORKDIR/state/session.q for later runs.
    """
    _validate_code("code", code)
    result = _run_q_script(_script_for(code), timeout_sec=timeout_sec)
    if persist_code and result.get("ok"):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        current = _state_text()
        joined = _join_continuations(code)
        appended = joined if joined.endswith("\n") else joined + "\n"
        if len((current + appended).encode("utf-8")) > MAX_STATE_BYTES:
            raise ValueError("persisted state would become too large")
        with STATE_FILE.open("a", encoding="utf-8") as f:
            if current and not current.endswith("\n"):
                f.write("\n")
            f.write(appended)
    return result


@mcp.tool()
def load_csv(
    table_name: str,
    input_file: str,
    types: str,
    delimiter: str = ",",
    persist_code: bool = True,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
) -> Dict[str, Any]:
    """
    Load a delimited text file into a q table using q's 0: CSV parser.

    input_file must resolve under WORKDIR or the configured Codex CSV root and
    must be a regular .csv, .tsv, or .txt file. The first row is treated as the
    CSV header by q. types is the q type string passed to 0:, for example
    "DFFFFFJ" for Date plus five floats and a long volume column. When
    persist_code is true, the generated load expression is stored in
    WORKDIR/state/session.q so later run_q calls can query the loaded table.
    """
    load_code = _csv_load_code(table_name, input_file, types, delimiter)
    inspect_code = f"{load_code}\nshow count {table_name};\nshow cols {table_name};\nshow meta {table_name};"
    result = _run_q_script(_script_for(inspect_code), timeout_sec=timeout_sec)
    if persist_code and result.get("ok"):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        current = _state_text()
        appended = load_code + "\n"
        if len((current + appended).encode("utf-8")) > MAX_STATE_BYTES:
            raise ValueError("persisted state would become too large")
        with STATE_FILE.open("a", encoding="utf-8") as f:
            if current and not current.endswith("\n"):
                f.write("\n")
            f.write(appended)
    result.update(
        {
            "table_name": table_name,
            "input_file": _display_path(_safe_csv_path(input_file, must_exist=True)),
            "types": types,
            "delimiter": delimiter,
            "persisted": bool(persist_code and result.get("ok")),
        }
    )
    return result


@mcp.tool()
def save_table(
    table_name: str,
    dest_file: str,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
) -> Dict[str, Any]:
    """
    Persist an in-memory q table to a binary file under WORKDIR using q's set.

    The named table must already exist in the replayed interpreter state (for
    example after load_csv or a persisted run_q). dest_file is resolved under
    WORKDIR only and is written as a single binary file via `:path set table,
    so it survives restarts and no longer depends on the source CSV. q's set is
    issued server-side with a validated path; user code remains barred from
    set/file-symbols. Reload the saved table later with load_table.
    """
    _validate_q_identifier(table_name)
    dest = _safe_path(dest_file, must_exist=False)
    if str(dest) == str(WORKDIR) or _is_under(STATE_FILE, dest) or dest == STATE_FILE:
        raise ValueError("dest_file must be a regular file path inside workdir")
    dest.parent.mkdir(parents=True, exist_ok=True)
    save_code = _save_table_code(table_name, dest)
    guard = f'if[not `{table_name} in key `.;\'"unknown table: {table_name}"];'
    result = _run_q_script(_script_for(guard + "\n" + save_code), timeout_sec=timeout_sec)
    result.update(
        {
            "table_name": table_name,
            "dest_file": _display_path(dest),
            "saved": bool(result.get("ok")),
            "file_bytes": dest.stat().st_size if result.get("ok") and dest.exists() else 0,
        }
    )
    return result


@mcp.tool()
def save_csv(
    table_name: str,
    dest_file: str,
    delimiter: str = ",",
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
) -> Dict[str, Any]:
    """
    Export an in-memory q table to a delimited text file using q's 0: writer.

    The named table must already exist in the replayed interpreter state (for
    example after load_csv, save/load_table, or a persisted run_q). The header
    row is the table's column names. dest_file is resolved under WORKDIR or the
    configured Codex CSV root and must end in .csv, .tsv, or .txt; delimiter is a
    single character ("," for CSV, "\\t" for TSV). q's 0: text save is issued
    server-side with a validated path, so user code remains barred from 0:/file
    symbols. This is the inverse of load_csv and is not persisted to session.q.
    """
    _validate_q_identifier(table_name)
    if len(delimiter) != 1:
        raise ValueError("delimiter must be exactly one character")
    dest = _safe_csv_path(dest_file, must_exist=False)
    if dest.suffix.lower() not in {".csv", ".tsv", ".txt"}:
        raise ValueError("Only .csv, .tsv, and .txt files are supported")
    if dest == STATE_FILE or dest == WORKDIR:
        raise ValueError("dest_file must be a regular output file")
    dest.parent.mkdir(parents=True, exist_ok=True)
    save_code = _save_csv_code(table_name, dest, delimiter)
    guard = f'if[not `{table_name} in key `.;\'"unknown table: {table_name}"];'
    tguard = f'if[not 98h=type {table_name};\'"not a table: {table_name}"];'
    result = _run_q_script(_script_for(guard + "\n" + tguard + "\n" + save_code), timeout_sec=timeout_sec)
    result.update(
        {
            "table_name": table_name,
            "dest_file": _display_path(dest),
            "delimiter": delimiter,
            "saved": bool(result.get("ok")),
            "file_bytes": dest.stat().st_size if result.get("ok") and dest.exists() else 0,
        }
    )
    return result


@mcp.tool()
def load_table(
    table_name: str,
    src_file: str,
    persist_code: bool = True,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
) -> Dict[str, Any]:
    """
    Reload a table previously written by save_table from its binary file.

    src_file is resolved under WORKDIR and must exist. The table is read back
    with q's get and bound to table_name. When persist_code is true the reload
    expression is appended to WORKDIR/state/session.q, so the table is restored
    from binary in every later session without re-parsing the original CSV.
    """
    _validate_q_identifier(table_name)
    src = _safe_path(src_file, must_exist=True)
    if not src.is_file():
        raise ValueError(f"Not a regular file: {src_file}")
    load_code = _load_table_code(table_name, src)
    inspect_code = f"{load_code}\nshow count {table_name};\nshow meta {table_name};"
    result = _run_q_script(_script_for(inspect_code), timeout_sec=timeout_sec)
    if persist_code and result.get("ok"):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        current = _state_text()
        appended = load_code + "\n"
        if len((current + appended).encode("utf-8")) > MAX_STATE_BYTES:
            raise ValueError("persisted state would become too large")
        with STATE_FILE.open("a", encoding="utf-8") as f:
            if current and not current.endswith("\n"):
                f.write("\n")
            f.write(appended)
    result.update(
        {
            "table_name": table_name,
            "src_file": _display_path(src),
            "persisted": bool(persist_code and result.get("ok")),
        }
    )
    return result


@mcp.tool()
def get_interpreter_state() -> Dict[str, Any]:
    """Return q version banner, persisted state size, variables, and functions."""
    script = f"{_join_continuations(_state_text())}\n.system.vars:.Q.s1 value \"\\\\v\"\n.system.funcs:.Q.s1 value \"\\\\f\"\n.system.vars\n.system.funcs\n\\\\\n"
    result = _run_q_script(script, timeout_sec=DEFAULT_TIMEOUT_SEC)
    lines = [line for line in result["stdout"].splitlines() if line.strip()]
    result.update(
        {
            "state_file": str(STATE_FILE.relative_to(WORKDIR)),
            "state_bytes": STATE_FILE.stat().st_size if STATE_FILE.exists() else 0,
            "variables_rendered": lines[-2] if len(lines) >= 2 else "",
            "functions_rendered": lines[-1] if len(lines) >= 1 else "",
        }
    )
    return result


@mcp.tool()
def reset_interpreter_state() -> Dict[str, Any]:
    """Clear the persisted q code under the restricted work directory."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text("", encoding="utf-8")
    return {"ok": True, "state_file": str(STATE_FILE.relative_to(WORKDIR)), "state_bytes": 0}


@mcp.tool()
def get_state_source() -> Dict[str, Any]:
    """
    Return the persisted session.q as numbered lines for state hygiene.

    run_q persists code in continuation-joined, one-statement-per-line form, so
    each numbered line is one replayed statement. Use the line numbers with
    prune_state to drop stray show/echo lines or superseded definitions without
    a full reset_interpreter_state.
    """
    text = _state_text()
    raw = text.splitlines()
    return {
        "ok": True,
        "state_file": str(STATE_FILE.relative_to(WORKDIR)),
        "state_bytes": STATE_FILE.stat().st_size if STATE_FILE.exists() else 0,
        "line_count": len(raw),
        "lines": [{"n": i, "line": ln} for i, ln in enumerate(raw, start=1)],
        "source": text,
    }


@mcp.tool()
def prune_state(line_numbers: List[int], timeout_sec: int = DEFAULT_TIMEOUT_SEC) -> Dict[str, Any]:
    """
    Delete persisted session.q lines by 1-based line number (subtractive only).

    This cleans accumulated cruft (stray show/echo output, superseded
    definitions) without dropping everything via reset_interpreter_state. Only
    deletion is allowed -- lines cannot be added or rewritten -- so no
    unvalidated code can enter the replayed state. The surviving lines are
    replayed first; if they no longer load cleanly the change is rolled back and
    nothing is written. Inspect line numbers first with get_state_source.
    """
    if not line_numbers:
        raise ValueError("line_numbers must not be empty")
    raw = _state_text().splitlines()
    drop = set(line_numbers)
    if any((n < 1 or n > len(raw)) for n in drop):
        raise ValueError(f"line_numbers out of range 1..{len(raw)}")
    kept = [ln for i, ln in enumerate(raw, start=1) if i not in drop]
    new_text = ("\n".join(kept) + "\n") if kept else ""
    probe = _run_q_script(
        f"{_join_continuations(new_text)}\n-1 {_q_string(OUTPUT_SENTINEL)};\n\\\\\n",
        timeout_sec=timeout_sec,
    )
    if not probe.get("ok"):
        return {
            "ok": False,
            "rolled_back": True,
            "removed_count": 0,
            "line_count": len(raw),
            "error": "pruned state failed to replay; no changes written",
            "stderr": probe.get("stderr", ""),
        }
    STATE_FILE.write_text(new_text, encoding="utf-8")
    return {
        "ok": True,
        "rolled_back": False,
        "removed_count": len(raw) - len(kept),
        "line_count": len(kept),
        "state_bytes": STATE_FILE.stat().st_size if STATE_FILE.exists() else 0,
    }


@mcp.tool()
def list_server_limits() -> Dict[str, Any]:
    """List q MCP server workdir, executable, and execution limits."""
    return {
        "workdir": str(WORKDIR),
        "codex_csv_root": str(CODEX_CSV_ROOT),
        "state_file": str(STATE_FILE.relative_to(WORKDIR)),
        "q": _q_bin(),
        "max_code_bytes": MAX_CODE_BYTES,
        "max_state_bytes": MAX_STATE_BYTES,
        "max_output_bytes": MAX_OUTPUT_BYTES,
        "default_timeout_sec": DEFAULT_TIMEOUT_SEC,
        "console_rows": CONSOLE_ROWS,
        "console_cols": CONSOLE_COLS,
        "execution_notes": [
            "multi-line statements supported (q whitespace continuation is honoured)",
            "bare expressions auto-print their result (REPL semantics)",
            "replayed-state output is suppressed; only your code's output is returned",
            f"show is not truncated up to {CONSOLE_ROWS} rows x {CONSOLE_COLS} cols",
            "state hygiene: get_state_source to inspect, prune_state to delete lines",
        ],
        "disabled_features": [
            "system",
            "backslash commands",
            "hopen/hclose/hdel",
            "get/set/load/save in user code",
            "value/parse/eval",
            "insert/upsert/delete",
            ".Q and .z in user code",
            "file/socket handles in user code",
            "file symbols in user code",
        ],
        "enabled_file_tools": [
            "load_csv: q 0: CSV parser for .csv/.tsv/.txt files under workdir or codex_csv_root",
            "save_table: q set to a binary file under workdir (server-side path validation)",
            "load_table: q get from a binary file under workdir (server-side path validation)",
            "save_csv: q 0: text writer to a .csv/.tsv/.txt file under workdir or codex_csv_root (server-side path validation)",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", default=os.environ.get("KDB_MCP_WORKDIR", os.getcwd()))
    parser.add_argument("--csv-root", default=os.environ.get("KDB_MCP_CSV_ROOT", str(CODEX_CSV_ROOT)))
    args = parser.parse_args()
    _set_workdir(args.workdir)
    _set_codex_csv_root(args.csv_root)
    mcp.run()


if __name__ == "__main__":
    main()
