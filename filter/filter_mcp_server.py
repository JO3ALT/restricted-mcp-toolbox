#!/usr/bin/env python3
"""
Restricted UNIX Filter MCP Server

A small Model Context Protocol server exposing safe-ish CSV/TSV/text
processing tools built from a strict allow-list of UNIX filters.

Design principles:
- No arbitrary shell execution.
- Commands are executed with subprocess argv lists, not shell=True.
- Files may be READ from under WORKDIR or the configured read root
  (the current project directory); WRITES stay under WORKDIR only.
- Pipelines use a JSON specification instead of raw shell syntax.
- Useful for awk/sed/sort/uniq/cut/tr/wc/head/tail/grep style reproducible processing.

Tested conceptually with MCP Python SDK style imports.
Install:
    pip install -r requirements.txt
Run:
    python3 filter_mcp_server.py --workdir ./work
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import resource
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

try:
    from mcp.server.fastmcp import FastMCP
except Exception:  # pragma: no cover - lets local unit tests import without mcp installed
    class FastMCP:  # type: ignore[no-redef]
        def __init__(self, name: str) -> None:
            self.name = name

        def tool(self) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
            def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
                return func
            return decorator

        def run(self) -> None:
            raise SystemExit("This server requires the MCP Python SDK. Install with: pip install mcp")

mcp = FastMCP("restricted-filter-mcp")

WORKDIR = Path.cwd().resolve()
# Read root for input files: defaults to the project directory (parent of the
# server's own directory), i.e. the current directory under which source/data
# files may be read. Writes are never widened to this root.
READ_ROOT = Path(
    os.environ.get("FILTER_MCP_READ_ROOT", Path(__file__).resolve().parents[1])
).expanduser().resolve()
MAX_INPUT_BYTES = 200 * 1024 * 1024
MAX_OUTPUT_BYTES = 50 * 1024 * 1024
DEFAULT_TIMEOUT_SEC = 30
SAFE_PATH = "/usr/bin:/bin:/usr/local/bin"

# Keep this deliberately small. Add tools only after testing their behavior.
ALLOWED_COMMANDS: Dict[str, Dict[str, Any]] = {
    "awk": {"bin": "awk", "allow_scripts": True},
    "sed": {"bin": "sed", "allow_scripts": True},
    "sort": {"bin": "sort"},
    "uniq": {"bin": "uniq"},
    "cut": {"bin": "cut"},
    "tr": {"bin": "tr"},
    "head": {"bin": "head"},
    "tail": {"bin": "tail"},
    "wc": {"bin": "wc"},
    "grep": {"bin": "grep"},
    "cat": {"bin": "cat"},
}

# Arguments that are either dangerous or make behavior hard to sandbox.
FORBIDDEN_ARG_PREFIXES = (
    "--output=",
    "--files0-from=",
    "--reference=",
)
FORBIDDEN_ARGS = {
    "--output",
    "--in-place",
    "-i",              # sed in-place, also grep ignore-case; too ambiguous, so ban globally
    "--include",
    "--exclude",
    "--exclude-dir",
    "--recursive",
    "-R",
    "--dereference-recursive",
    "--null-data",     # avoid NUL pipelines in first version
}

OPTION_ARITY: Dict[str, Dict[str, int]] = {
    "sort": {"-k": 1, "--key": 1, "-t": 1, "--field-separator": 1, "-S": 1, "--buffer-size": 1},
    "uniq": {"-f": 1, "--skip-fields": 1, "-s": 1, "--skip-chars": 1, "-w": 1, "--check-chars": 1},
    "cut": {"-b": 1, "--bytes": 1, "-c": 1, "--characters": 1, "-d": 1, "--delimiter": 1, "-f": 1, "--fields": 1},
    "head": {"-n": 1, "--lines": 1, "-c": 1, "--bytes": 1},
    "tail": {"-n": 1, "--lines": 1, "-c": 1, "--bytes": 1},
}

NO_FILE_OPERAND_COMMANDS = {"sort", "uniq", "cut", "tr", "head", "tail", "wc", "grep", "awk", "sed", "cat"}
AWK_FORBIDDEN_RE = re.compile(r"\b(system|getline|close)\b|[|]|>>|<")
AWK_OUTPUT_REDIRECT_RE = re.compile(r"\bprintf?\b[^;\n{}]*>")
SED_FORBIDDEN_RE = re.compile(r"(^|[;\n])\s*([0-9,$!]+|/[^/\n]*/)?\s*[erw]\b")

# awk -f / sed -f: a script file (under workdir or read root) is read and
# content-validated server-side, then run inline so the child process never
# receives a raw file path. Same forbidden-feature rules as inline programs.
MAX_SCRIPT_BYTES = 256 * 1024
SCRIPT_FILE_SUFFIXES: Dict[str, set] = {"awk": {".awk", ""}, "sed": {".sed", ""}}


def _set_workdir(path: str) -> None:
    global WORKDIR
    WORKDIR = Path(path).expanduser().resolve()
    WORKDIR.mkdir(parents=True, exist_ok=True)


def _set_read_root(path: str) -> None:
    global READ_ROOT
    READ_ROOT = Path(path).expanduser().resolve()


def _is_under(path: Path, root: Path) -> bool:
    return path == root or str(path).startswith(str(root) + os.sep)


def _safe_path(user_path: str, *, must_exist: bool = False) -> Path:
    p = (WORKDIR / user_path).resolve() if not Path(user_path).is_absolute() else Path(user_path).resolve()
    if not _is_under(p, WORKDIR):
        raise ValueError(f"Path escapes workdir: {user_path}")
    if must_exist and not p.exists():
        raise FileNotFoundError(str(p))
    return p


def _safe_read_path(user_path: str, *, must_exist: bool = False) -> Path:
    """Resolve a path for READING under WORKDIR or the configured read root.

    Relative paths are tried under WORKDIR first, then under READ_ROOT, so
    files in the current project directory are reachable without widening
    write access (writes still go through _safe_path / WORKDIR only).
    """
    raw = Path(user_path).expanduser()
    candidates = [raw.resolve()] if raw.is_absolute() else [
        (WORKDIR / raw).resolve(),
        (READ_ROOT / raw).resolve(),
    ]
    allowed = [c for c in candidates if _is_under(c, WORKDIR) or _is_under(c, READ_ROOT)]
    if not allowed:
        raise ValueError(f"Path escapes allowed read roots: {user_path}")
    for c in allowed:
        if not must_exist or c.exists():
            return c
    raise FileNotFoundError(user_path)


def _display_path(path: Path) -> str:
    for root in (WORKDIR, READ_ROOT):
        if _is_under(path, root):
            return str(path.relative_to(root))
    return str(path)


def _resolve_bin(cmd: str) -> str:
    resolved = shutil.which(ALLOWED_COMMANDS[cmd]["bin"], path=SAFE_PATH)
    if resolved is None:
        raise FileNotFoundError(f"Required command not found on safe PATH: {cmd}")
    return resolved


def _check_file_size(path: Path) -> None:
    if path.is_file() and path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError(f"Input too large: {path.name} exceeds {MAX_INPUT_BYTES} bytes")


def _validate_args(args: List[str]) -> None:
    for a in args:
        if a in FORBIDDEN_ARGS or any(a.startswith(prefix) for prefix in FORBIDDEN_ARG_PREFIXES):
            raise ValueError(f"Forbidden argument: {a}")
        if "\x00" in a:
            raise ValueError("NUL byte in argument")
        if Path(a).is_absolute() or ".." in Path(a).parts:
            raise ValueError(f"Path-like argument is not allowed here: {a}")


def _consume_options(args: Sequence[str], arity: Dict[str, int]) -> Tuple[List[str], List[str]]:
    remaining: List[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            remaining.extend(args[i + 1:])
            break
        if not arg.startswith("-") or arg == "-":
            remaining.append(arg)
            i += 1
            continue
        if "=" in arg:
            name = arg.split("=", 1)[0]
            if name in arity:
                i += 1
                continue
        if arg in arity:
            if i + arity[arg] >= len(args):
                raise ValueError(f"Missing value for option: {arg}")
            i += 1 + arity[arg]
            continue
        i += 1
    return list(args), remaining


def _require_no_file_operands(cmd: str, args: List[str], arity: Optional[Dict[str, int]] = None) -> None:
    _, operands = _consume_options(args, arity or {})
    operands = [x for x in operands if x != "-"]
    if operands:
        raise ValueError(f"{cmd} file operands are not allowed; use input_file/input_text and pipelines")


def _validate_awk_args(args: List[str]) -> None:
    program_seen = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "-F":
            i += 2
            continue
        if arg == "-f":
            raise ValueError("awk script files are not allowed")
        if arg.startswith("-F") and arg != "-F":
            i += 1
            continue
        if arg == "-v":
            i += 2
            continue
        if arg.startswith("-"):
            raise ValueError(f"Unsupported awk option: {arg}")
        if program_seen:
            raise ValueError("awk file operands are not allowed")
        if AWK_FORBIDDEN_RE.search(arg) or AWK_OUTPUT_REDIRECT_RE.search(arg):
            raise ValueError("awk program uses forbidden file or command features")
        program_seen = True
        i += 1
    if not program_seen:
        raise ValueError("awk requires an inline program")


def _validate_sed_args(args: List[str]) -> None:
    program_seen = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in {"-n", "-E", "-r", "-u"}:
            i += 1
            continue
        if arg == "-f":
            raise ValueError("sed script files are not allowed")
        if arg in {"-e"}:
            if i + 1 >= len(args):
                raise ValueError("Missing sed expression after -e")
            if SED_FORBIDDEN_RE.search(args[i + 1]):
                raise ValueError("sed expression uses forbidden file or command features")
            program_seen = True
            i += 2
            continue
        if arg.startswith("-"):
            raise ValueError(f"Unsupported sed option: {arg}")
        if program_seen:
            raise ValueError("sed file operands are not allowed")
        if SED_FORBIDDEN_RE.search(arg):
            raise ValueError("sed expression uses forbidden file or command features")
        program_seen = True
        i += 1
    if not program_seen:
        raise ValueError("sed requires an inline expression")


def _validate_grep_args(args: List[str]) -> None:
    pattern_count = 0
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in {"-f", "--file", "-r", "-R", "--recursive", "--dereference-recursive"}:
            raise ValueError(f"Forbidden grep option: {arg}")
        if arg in {"-e", "--regexp"}:
            if i + 1 >= len(args):
                raise ValueError("Missing grep pattern after -e")
            pattern_count += 1
            i += 2
            continue
        if arg.startswith("-") and arg != "-":
            i += 1
            continue
        pattern_count += 1
        if pattern_count > 1:
            raise ValueError("grep file operands are not allowed")
        i += 1
    if pattern_count == 0:
        raise ValueError("grep requires a pattern")


def _validate_tr_args(args: List[str]) -> None:
    _, operands = _consume_options(args, {})
    if not (1 <= len(operands) <= 2):
        raise ValueError("tr requires one or two SET operands")


def _validate_stage_args(cmd: str, args: List[str]) -> None:
    if cmd == "awk":
        _validate_awk_args(args)
    elif cmd == "sed":
        _validate_sed_args(args)
    elif cmd == "grep":
        _validate_grep_args(args)
    elif cmd == "tr":
        _validate_tr_args(args)
    elif cmd in NO_FILE_OPERAND_COMMANDS:
        _require_no_file_operands(cmd, args, OPTION_ARITY.get(cmd))


def _load_script_file(cmd: str, path_str: str) -> str:
    """Read and content-validate an awk/sed script file under a read root.

    Path containment is enforced by _safe_read_path (workdir or read root);
    the same forbidden-feature checks used for inline programs are applied to
    the file content, so a script cannot smuggle in system()/pipes/redirects.
    """
    p = _safe_read_path(path_str, must_exist=True)
    if not p.is_file():
        raise ValueError(f"Not a regular file: {path_str}")
    if p.suffix.lower() not in SCRIPT_FILE_SUFFIXES[cmd]:
        raise ValueError(f"{cmd} script file must end in {sorted(SCRIPT_FILE_SUFFIXES[cmd])}")
    if p.stat().st_size > MAX_SCRIPT_BYTES:
        raise ValueError(f"{cmd} script file too large")
    try:
        content = p.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{cmd} script file must be UTF-8 text") from exc
    if "\x00" in content:
        raise ValueError("script file contains a NUL byte")
    if cmd == "awk":
        if AWK_FORBIDDEN_RE.search(content) or AWK_OUTPUT_REDIRECT_RE.search(content):
            raise ValueError("awk script uses forbidden file or command features")
    else:
        if SED_FORBIDDEN_RE.search(content):
            raise ValueError("sed script uses forbidden file or command features")
    return content


def _extract_script_file(cmd: str, args: List[str]) -> Tuple[List[str], Optional[str]]:
    """Pull a single -f/--file script path out of args, returning (rest, program).

    Recognizes `-f PATH`, `--file PATH`, and `--file=PATH`. The path token is
    resolved/validated by _load_script_file (not _validate_args), so it may be a
    relative path under the read root; everything else stays in `rest`.
    """
    rest: List[str] = []
    script_path: Optional[str] = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-f", "--file"):
            if i + 1 >= len(args):
                raise ValueError(f"Missing script file after {a}")
            if script_path is not None:
                raise ValueError(f"{cmd}: only one script file is allowed")
            script_path = args[i + 1]
            i += 2
            continue
        if a.startswith("--file="):
            if script_path is not None:
                raise ValueError(f"{cmd}: only one script file is allowed")
            script_path = a.split("=", 1)[1]
            i += 1
            continue
        rest.append(a)
        i += 1
    if script_path is None:
        return args, None
    return rest, _load_script_file(cmd, script_path)


def _validate_awk_options_only(args: List[str]) -> None:
    """Validate awk args that accompany a script file: options only, no program."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-F":
            i += 2
            continue
        if a.startswith("-F") and a != "-F":
            i += 1
            continue
        if a == "-v":
            i += 2
            continue
        if a.startswith("-"):
            raise ValueError(f"Unsupported awk option: {a}")
        raise ValueError("awk inline program is not allowed together with a script file")


def _validate_sed_options_only(args: List[str]) -> None:
    """Validate sed args that accompany a script file: options only, no expression."""
    for a in args:
        if a in {"-n", "-E", "-r", "-u"}:
            continue
        if a == "-e":
            raise ValueError("sed -e expression is not allowed together with a script file")
        if a.startswith("-"):
            raise ValueError(f"Unsupported sed option: {a}")
        raise ValueError("sed inline expression is not allowed together with a script file")


def _build_stage(stage: Dict[str, Any]) -> List[str]:
    cmd = stage.get("cmd")
    if cmd not in ALLOWED_COMMANDS:
        raise ValueError(f"Command not allowed: {cmd}")
    args = stage.get("args", [])
    if not isinstance(args, list) or not all(isinstance(x, str) for x in args):
        raise ValueError("stage.args must be a list of strings")
    if cmd in ("awk", "sed"):
        rest, program = _extract_script_file(cmd, args)
        if program is not None:
            _validate_args(rest)
            if cmd == "awk":
                _validate_awk_options_only(rest)
                return [_resolve_bin(cmd), *rest, program]
            _validate_sed_options_only(rest)
            return [_resolve_bin(cmd), *rest, "-e", program]
    _validate_args(args)
    _validate_stage_args(cmd, args)
    return [_resolve_bin(cmd), *args]


def _limit_child_resources() -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (DEFAULT_TIMEOUT_SEC + 5, DEFAULT_TIMEOUT_SEC + 5))


def _run_pipeline_internal(
    stages: List[Dict[str, Any]],
    input_text: Optional[str] = None,
    input_file: Optional[str] = None,
    output_file: Optional[str] = None,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
) -> Dict[str, Any]:
    if not stages:
        raise ValueError("Pipeline must contain at least one stage")
    if len(stages) > 12:
        raise ValueError("Pipeline too long; maximum is 12 stages")
    if input_text is not None and input_file is not None:
        raise ValueError("Specify only one of input_text or input_file")
    if timeout_sec < 1 or timeout_sec > 300:
        raise ValueError("timeout_sec must be 1..300")

    input_bytes: Optional[bytes]
    if input_file is not None:
        in_path = _safe_read_path(input_file, must_exist=True)
        _check_file_size(in_path)
        input_bytes = in_path.read_bytes()
    elif input_text is not None:
        input_bytes = input_text.encode("utf-8")
        if len(input_bytes) > MAX_INPUT_BYTES:
            raise ValueError("input_text too large")
    else:
        input_bytes = b""

    current = input_bytes
    stderrs: List[bytes] = []
    returncodes: List[int] = []
    start = time.perf_counter()
    for stage in stages:
        argv = _build_stage(stage)
        try:
            completed = subprocess.run(
                argv,
                input=current,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(WORKDIR),
                env={"LC_ALL": "C", "PATH": SAFE_PATH},
                timeout=timeout_sec,
                preexec_fn=_limit_child_resources if hasattr(resource, "setrlimit") else None,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"Pipeline timed out after {timeout_sec} sec") from exc
        current = completed.stdout
        stderrs.append(completed.stderr)
        returncodes.append(completed.returncode)
        if len(current) > MAX_OUTPUT_BYTES:
            raise ValueError(f"Output too large: {len(current)} bytes")

    elapsed = time.perf_counter() - start
    out = current
    if len(out) > MAX_OUTPUT_BYTES:
        raise ValueError(f"Output too large: {len(out)} bytes")

    if output_file:
        out_path = _safe_path(output_file, must_exist=False)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(out)
        output_preview = out[:8192].decode("utf-8", errors="replace")
        output_path = str(out_path.relative_to(WORKDIR))
    else:
        output_preview = out.decode("utf-8", errors="replace")
        output_path = None

    return {
        "ok": all(rc == 0 for rc in returncodes),
        "returncodes": returncodes,
        "stderr": b"\n".join(stderrs).decode("utf-8", errors="replace"),
        "stdout": output_preview,
        "stdout_truncated_to_bytes": 8192 if output_file and len(out) > 8192 else None,
        "output_file": output_path,
        "bytes_out": len(out),
        "elapsed_sec": elapsed,
        "script_equivalent": render_pipeline(stages, input_file=input_file, output_file=output_file),
    }


def render_pipeline(stages: List[Dict[str, Any]], input_file: Optional[str] = None, output_file: Optional[str] = None) -> str:
    parts = []
    for stage in stages:
        _build_stage(stage)
        cmd = stage["cmd"]
        argv = [ALLOWED_COMMANDS[cmd]["bin"], *stage.get("args", [])]
        parts.append(" ".join(shlex.quote(x) for x in argv))
    if input_file:
        parts[0] += " < " + shlex.quote(input_file)
    s = " | ".join(parts)
    if output_file:
        s += " > " + shlex.quote(output_file)
    return s


@mcp.tool()
def list_allowed_commands() -> Dict[str, Any]:
    """List allowed filter commands and global restrictions."""
    return {
        "workdir": str(WORKDIR),
        "read_root": str(READ_ROOT),
        "allowed_commands": sorted(ALLOWED_COMMANDS.keys()),
        "forbidden_args": sorted(FORBIDDEN_ARGS),
        "max_input_bytes": MAX_INPUT_BYTES,
        "max_output_bytes": MAX_OUTPUT_BYTES,
        "read_roots": [str(WORKDIR), str(READ_ROOT)],
        "write_root": str(WORKDIR),
        "script_files": {
            "awk": "awk -f PATH (.awk/no-suffix under a read root); content is re-validated like an inline program",
            "sed": "sed -f PATH (.sed/no-suffix under a read root); content is re-validated like an inline program",
            "forms": ["-f PATH", "--file PATH", "--file=PATH"],
            "max_script_bytes": MAX_SCRIPT_BYTES,
            "note": "forbidden inside scripts too: system/getline/close, pipes, redirects (awk); e/r/w (sed)",
        },
    }


@mcp.tool()
def list_files(subdir: str = ".") -> Dict[str, Any]:
    """List files under the work directory or the configured read root."""
    root = _safe_read_path(subdir, must_exist=True)
    if not root.is_dir():
        raise ValueError("subdir is not a directory")
    items = []
    for p in sorted(root.iterdir()):
        items.append({"path": _display_path(p), "is_dir": p.is_dir(), "size": p.stat().st_size if p.is_file() else None})
    return {"workdir": str(WORKDIR), "read_root": str(READ_ROOT), "items": items}


@mcp.tool()
def preview_file(path: str, max_bytes: int = 8192) -> Dict[str, Any]:
    """Preview a text file under the work directory or the configured read root."""
    if max_bytes < 1 or max_bytes > 1024 * 1024:
        raise ValueError("max_bytes must be 1..1048576")
    p = _safe_read_path(path, must_exist=True)
    _check_file_size(p)
    data = p.read_bytes()[:max_bytes]
    return {
        "path": _display_path(p),
        "size": p.stat().st_size,
        "preview": data.decode("utf-8", errors="replace"),
        "truncated": p.stat().st_size > max_bytes,
    }


@mcp.tool()
def run_filter_pipeline(
    stages: List[Dict[str, Any]],
    input_text: Optional[str] = None,
    input_file: Optional[str] = None,
    output_file: Optional[str] = None,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
) -> Dict[str, Any]:
    """
    Run a restricted UNIX filter pipeline.

    stages example:
      [
        {"cmd":"awk", "args":["-F,", "{print $2}"]},
        {"cmd":"sort", "args":[]},
        {"cmd":"uniq", "args":["-c"]}
      ]

    awk/sed may load a script file with -f PATH (or --file=PATH), where PATH is
    a .awk/.sed file under the work directory or the configured read root, e.g.
      {"cmd":"awk", "args":["-F,", "-f", "scripts/agg.awk"]}
    The script is read and re-validated server-side (same forbidden features as
    inline programs) and run inline; the child process never receives the path.
    """
    return _run_pipeline_internal(stages, input_text, input_file, output_file, timeout_sec)


@mcp.tool()
def group_by_count(input_file: str, field: int, delimiter: str = ",", output_file: Optional[str] = None) -> Dict[str, Any]:
    """Count occurrences of one delimited field, using awk | sort | uniq -c | sort -nr."""
    if field < 1:
        raise ValueError("field is 1-based and must be >= 1")
    if delimiter in ("", "\n", "\x00"):
        raise ValueError("invalid delimiter")
    awk_prog = f"{{print ${field}}}"
    stages = [
        {"cmd": "awk", "args": [f"-F{delimiter}", awk_prog]},
        {"cmd": "sort", "args": []},
        {"cmd": "uniq", "args": ["-c"]},
        {"cmd": "sort", "args": ["-nr"]},
    ]
    return _run_pipeline_internal(stages, input_file=input_file, output_file=output_file)


@mcp.tool()
def csv_summary(input_file: str, delimiter: str = ",", has_header: bool = True, sample_rows: int = 5) -> Dict[str, Any]:
    """Return row count, column count, header, and sample rows for a CSV/TSV-like file."""
    if sample_rows < 0 or sample_rows > 50:
        raise ValueError("sample_rows must be 0..50")
    p = _safe_read_path(input_file, must_exist=True)
    _check_file_size(p)
    with p.open("r", encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.reader(f, delimiter=delimiter)
        rows = []
        row_count = 0
        max_cols = 0
        header = None
        for row in reader:
            row_count += 1
            max_cols = max(max_cols, len(row))
            if row_count == 1 and has_header:
                header = row
                continue
            if len(rows) < sample_rows:
                rows.append(row)
    data_rows = row_count - (1 if has_header and row_count > 0 else 0)
    return {
        "path": _display_path(p),
        "total_lines": row_count,
        "data_rows": data_rows,
        "max_columns": max_cols,
        "header": header,
        "sample_rows": rows,
    }


@mcp.tool()
def save_pipeline_script(stages: List[Dict[str, Any]], script_file: str, input_file: Optional[str] = None, output_file: Optional[str] = None) -> Dict[str, Any]:
    """Save a reproducible shell script for a validated restricted pipeline."""
    script_path = _safe_path(script_file, must_exist=False)
    if script_path.suffix not in {".sh", ""}:
        raise ValueError("script_file should be a .sh file or have no suffix")
    line = render_pipeline(stages, input_file=input_file, output_file=output_file)
    content = "#!/bin/sh\nset -eu\nLC_ALL=C\n" + line + "\n"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(content, encoding="utf-8")
    script_path.chmod(0o755)
    return {"script_file": str(script_path.relative_to(WORKDIR)), "content": content}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", default=os.environ.get("FILTER_MCP_WORKDIR", os.getcwd()))
    parser.add_argument("--read-root", default=os.environ.get("FILTER_MCP_READ_ROOT", str(READ_ROOT)))
    args = parser.parse_args()
    _set_workdir(args.workdir)
    _set_read_root(args.read_root)
    mcp.run()


if __name__ == "__main__":
    main()
