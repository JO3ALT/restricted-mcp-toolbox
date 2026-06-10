#!/usr/bin/env python3
"""
Lean MCP Server.

This server checks small Lean 4 proof snippets with the local `lean` binary.
It is validation-only: it does not expose a shell or Lake. Imports and files
under the configured Codex workspace are allowed so library-backed proofs can
be checked, while unchecked assumptions and Lean commands that execute code are
still rejected.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import resource
import shutil
import subprocess
import tempfile
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


mcp = FastMCP("restricted-lean-mcp")

WORKDIR = Path.cwd().resolve()
TMPDIR = WORKDIR / "tmp"
CODEX_ROOT = Path(
    os.environ.get(
        "LEAN_MCP_READ_ROOT",
        os.environ.get("CODEX_MCP_READ_ROOT", Path(__file__).resolve().parents[1]),
    )
).expanduser().resolve()
SAFE_PATH = f"{Path.home() / '.elan' / 'bin'}:/usr/bin:/bin:/usr/local/bin"
MAX_CODE_BYTES = 512 * 1024
MAX_OUTPUT_BYTES = 5 * 1024 * 1024
DEFAULT_TIMEOUT_SEC = 10
LEAN_MEMORY_MB = 512
LEAN_TASK_TIMEOUT = 200000

BANNED_LINE_PATTERNS = [
    (re.compile(r"^\s*#eval\b", re.MULTILINE), "#eval is disabled"),
    (re.compile(r"^\s*#compile\b", re.MULTILINE), "#compile is disabled"),
    (re.compile(r"^\s*#guard_msgs\b", re.MULTILINE), "#guard_msgs is disabled"),
    (re.compile(r"^\s*run_cmd\b", re.MULTILINE), "run_cmd is disabled"),
]

BANNED_TOKEN_PATTERNS = [
    (re.compile(r"\bunsafe\b"), "unsafe declarations are disabled"),
    (re.compile(r"\baxiom\b"), "axiom declarations are disabled"),
    (re.compile(r"\bconstant\b"), "constant declarations are disabled"),
    (re.compile(r"\bopaque\b"), "opaque declarations are disabled"),
    (re.compile(r"\bextern\b"), "extern declarations are disabled"),
    (re.compile(r"\binitialize\b"), "initialize commands are disabled"),
    (re.compile(r"\bbuiltin_initialize\b"), "builtin_initialize commands are disabled"),
    (re.compile(r"\belab\b"), "elab commands are disabled"),
    (re.compile(r"\bmacro\b"), "macro commands are disabled"),
    (re.compile(r"\bsyntax\b"), "syntax extensions are disabled"),
    (re.compile(r"\bsorry\b"), "sorry is disabled"),
    (re.compile(r"\badmit\b"), "admit is disabled"),
]


def _set_workdir(path: str) -> None:
    global WORKDIR, TMPDIR
    WORKDIR = Path(path).expanduser().resolve()
    TMPDIR = WORKDIR / "tmp"
    TMPDIR.mkdir(parents=True, exist_ok=True)


def _set_read_root(path: str) -> None:
    global CODEX_ROOT
    CODEX_ROOT = Path(path).expanduser().resolve()


def _under(root: Path, p: Path) -> bool:
    return p == root or str(p).startswith(str(root) + os.sep)


def _safe_path(user_path: str, *, must_exist: bool = False) -> Path:
    p = (WORKDIR / user_path).resolve() if not Path(user_path).is_absolute() else Path(user_path).resolve()
    if not _under(WORKDIR, p) and not _under(CODEX_ROOT, p):
        raise ValueError(f"Path is outside allowed read roots: {user_path}")
    if must_exist and not p.exists():
        raise FileNotFoundError(str(p))
    return p


def _lean_bin() -> str:
    resolved = shutil.which("lean", path=SAFE_PATH)
    if resolved is None:
        raise FileNotFoundError("lean not found on safe PATH")
    return resolved


def _lean_env() -> Dict[str, str]:
    env = {
        "LC_ALL": "C",
        "PATH": SAFE_PATH,
        "HOME": os.environ.get("HOME", str(WORKDIR)),
        "ELAN_HOME": os.environ.get("ELAN_HOME", str(Path.home() / ".elan")),
        "LAKE_HOME": os.environ.get("LAKE_HOME", str(WORKDIR / ".lake")),
    }
    for name in ("LEAN_PATH", "LEAN_SRC_PATH", "LEAN_SYSROOT"):
        if name in os.environ:
            env[name] = os.environ[name]
    return env


def _check_size(name: str, text: str) -> None:
    if "\x00" in text:
        raise ValueError(f"{name} contains a NUL byte")
    if len(text.encode("utf-8")) > MAX_CODE_BYTES:
        raise ValueError(f"{name} too large")


def _mask_comments_and_strings(code: str) -> str:
    chars = list(code)
    i = 0
    block_depth = 0
    in_string = False
    in_line_comment = False
    while i < len(chars):
        two = "".join(chars[i:i + 2])
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
        if block_depth:
            if two == "/-":
                chars[i] = chars[i + 1] = " "
                block_depth += 1
                i += 2
                continue
            if two == "-/":
                chars[i] = chars[i + 1] = " "
                block_depth -= 1
                i += 2
                continue
            chars[i] = " " if chars[i] != "\n" else "\n"
            i += 1
            continue
        if two == "--":
            chars[i] = chars[i + 1] = " "
            in_line_comment = True
            i += 2
            continue
        if two == "/-":
            chars[i] = chars[i + 1] = " "
            block_depth = 1
            i += 2
            continue
        if chars[i] == '"':
            chars[i] = " "
            in_string = True
        i += 1
    return "".join(chars)


def _validate_code(code: str) -> None:
    _check_size("code", code)
    visible = _mask_comments_and_strings(code)
    for pattern, message in BANNED_LINE_PATTERNS + BANNED_TOKEN_PATTERNS:
        if pattern.search(visible):
            raise ValueError(message)


def _limit_child_resources() -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (DEFAULT_TIMEOUT_SEC + 5, DEFAULT_TIMEOUT_SEC + 5))


def _parse_json_messages(stdout: str) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            messages.append(json.loads(line))
        except json.JSONDecodeError:
            messages.append({"raw": line})
    return messages


def _run_lean_code(code: str, timeout_sec: int = DEFAULT_TIMEOUT_SEC) -> Dict[str, Any]:
    if timeout_sec < 1 or timeout_sec > 60:
        raise ValueError("timeout_sec must be 1..60")
    _validate_code(code)
    TMPDIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".lean", dir=TMPDIR, encoding="utf-8", delete=False) as f:
        f.write(code)
        lean_file = Path(f.name)

    start = time.perf_counter()
    try:
        completed = subprocess.run(
            [
                _lean_bin(),
                "--json",
                "--trust=0",
                "-E",
                "sorry",
                "-M",
                str(LEAN_MEMORY_MB),
                "-T",
                str(LEAN_TASK_TIMEOUT),
                "-j",
                "1",
                str(lean_file),
            ],
            cwd=str(WORKDIR),
            env=_lean_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_sec,
            preexec_fn=_limit_child_resources if hasattr(resource, "setrlimit") else None,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"Lean check timed out after {timeout_sec} sec") from exc
    finally:
        lean_file.unlink(missing_ok=True)

    stdout = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace")
    if len(stdout.encode("utf-8")) > MAX_OUTPUT_BYTES or len(stderr.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise ValueError("Lean output too large")
    messages = _parse_json_messages(stdout)
    if completed.returncode != 0 and not messages and stderr:
        messages.append({"severity": "error", "data": stderr.strip()})
    errors = [m for m in messages if m.get("severity") == "error"]
    warnings = [m for m in messages if m.get("severity") == "warning"]
    return {
        "ok": completed.returncode == 0 and not errors,
        "returncode": completed.returncode,
        "messages": messages,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "stderr": stderr,
        "elapsed_sec": time.perf_counter() - start,
    }


@mcp.tool()
def check_lean_code(code: str, timeout_sec: int = DEFAULT_TIMEOUT_SEC) -> Dict[str, Any]:
    """
    Check a Lean 4 proof snippet with validation.

    The snippet is written to a temporary file under WORKDIR/tmp and checked
    with `lean --json --trust=0`. Imports are allowed. `sorry`, `admit`,
    `axiom`, `constant`, `unsafe`, `#eval`, syntax extensions, and
    initialization commands are rejected before Lean runs.
    """
    return _run_lean_code(code, timeout_sec=timeout_sec)


@mcp.tool()
def check_lean_file(path: str, timeout_sec: int = DEFAULT_TIMEOUT_SEC) -> Dict[str, Any]:
    """Check a Lean file under the workdir or configured Codex read root."""
    p = _safe_path(path, must_exist=True)
    if p.suffix != ".lean":
        raise ValueError("path must point to a .lean file")
    code = p.read_text(encoding="utf-8", errors="replace")
    return _run_lean_code(code, timeout_sec=timeout_sec)


@mcp.tool()
def get_lean_environment() -> Dict[str, Any]:
    """Return Lean executable and version information."""
    lean = _lean_bin()
    completed = subprocess.run(
        [lean, "--version"],
        cwd=str(WORKDIR),
        env=_lean_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    return {
        "ok": completed.returncode == 0,
        "lean": lean,
        "version": completed.stdout.decode("utf-8", errors="replace").strip(),
        "stderr": completed.stderr.decode("utf-8", errors="replace"),
        "workdir": str(WORKDIR),
        "codex_read_root": str(CODEX_ROOT),
        "lean_path": os.environ.get("LEAN_PATH", ""),
    }


@mcp.tool()
def list_server_limits() -> Dict[str, Any]:
    """List the Lean MCP server workdir and execution limits."""
    return {
        "workdir": str(WORKDIR),
        "tmpdir": str(TMPDIR.relative_to(WORKDIR)),
        "max_code_bytes": MAX_CODE_BYTES,
        "max_output_bytes": MAX_OUTPUT_BYTES,
        "default_timeout_sec": DEFAULT_TIMEOUT_SEC,
        "lean_memory_mb": LEAN_MEMORY_MB,
        "lean_task_timeout": LEAN_TASK_TIMEOUT,
        "disabled_features": [
            "#eval",
            "#compile",
            "#guard_msgs",
            "run_cmd",
            "unsafe",
            "axiom",
            "constant",
            "opaque",
            "extern",
            "initialize",
            "elab",
            "macro",
            "syntax",
            "sorry",
            "admit",
        ],
        "allowed_read_roots": [str(WORKDIR), str(CODEX_ROOT)],
        "imports_allowed": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", default=os.environ.get("LEAN_MCP_WORKDIR", os.getcwd()))
    parser.add_argument("--read-root", default=os.environ.get("LEAN_MCP_READ_ROOT", str(CODEX_ROOT)))
    args = parser.parse_args()
    _set_workdir(args.workdir)
    _set_read_root(args.read_root)
    mcp.run()


if __name__ == "__main__":
    main()
