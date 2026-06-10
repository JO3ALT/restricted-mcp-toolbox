#!/usr/bin/env python3
"""
SWI-Prolog MCP Server.

This server exposes a small Prolog execution surface without arbitrary shell
execution. Prolog clauses are stored under WORKDIR/state/session.pl, loaded by
reading terms and assertz/1. SWI-Prolog libraries and source files under the
configured Codex workspace can be loaded, while external process predicates are
still rejected before execution.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import shutil
import subprocess
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

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


mcp = FastMCP("restricted-swi-prolog-mcp")

WORKDIR = Path.cwd().resolve()
STATE_FILE = WORKDIR / "state" / "session.pl"
TMPDIR = WORKDIR / "tmp"
CODEX_ROOT = Path(
    os.environ.get(
        "PROLOG_MCP_READ_ROOT",
        os.environ.get("CODEX_MCP_READ_ROOT", Path(__file__).resolve().parents[1]),
    )
).expanduser().resolve()
SAFE_PATH = "/usr/bin:/bin:/usr/local/bin"
MAX_PROGRAM_BYTES = 2 * 1024 * 1024
MAX_QUERY_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 5 * 1024 * 1024
DEFAULT_TIMEOUT_SEC = 10
MAX_SOLUTIONS = 1000


RUNNER_SOURCE = r"""
:- use_module(library(http/json)).
:- use_module(library(solution_sequences)).
:- initialization(main, main).
:- dynamic allowed_root/1.

main :-
    current_prolog_flag(argv, Argv),
    catch(main_(Argv), Error, emit_error(Error)).

main_([Mode, StateFile, ProgramFile, QueryText, MaxSolutionsText, AllowedRoot, WorkRoot]) :-
    assertz(allowed_root(AllowedRoot)),
    assertz(allowed_root(WorkRoot)),
    atom_number(MaxSolutionsText, MaxSolutions),
    load_clause_file(StateFile, StateCount),
    load_clause_file(ProgramFile, ProgramCount),
    (   Mode = inspect
    ->  inspect_state(StateFile, StateCount, ProgramCount, Result)
    ;   Mode = query
    ->  run_query(QueryText, MaxSolutions, StateCount, ProgramCount, Result)
    ;   throw(error(domain_error(mode, Mode), _))
    ),
    json_write_dict(current_output, Result, [width(0)]),
    nl.

emit_error(Error) :-
    term_string(Error, Message, [quoted(true)]),
    json_write_dict(current_output, _{ok:false, error:Message}, [width(0)]),
    nl,
    halt(2).

load_clause_file(File, Count) :-
    (   File == '-'
    ->  Count = 0
    ;   setup_call_cleanup(
            open(File, read, In, [encoding(utf8)]),
            read_and_assert_clauses(In, 0, Count),
            close(In)
        )
    ).

read_and_assert_clauses(In, N0, N) :-
    read_term(In, Term, []),
    (   Term == end_of_file
    ->  N = N0
    ;   load_term(Term, Added),
        N1 is N0 + Added,
        read_and_assert_clauses(In, N1, N)
    ).

load_term((:- Directive), 0) :-
    !,
    run_allowed_directive(Directive).
load_term(Term, 1) :-
    validate_clause(Term),
    assertz(Term).

validate_clause((Head :- Body)) :-
    !,
    callable(Head),
    reject_process_goal(Body).
validate_clause(Fact) :-
    callable(Fact).

run_allowed_directive(use_module(library(Library))) :-
    !,
    use_module(library(Library)).
run_allowed_directive(use_module(Source)) :-
    !,
    allowed_source(Source),
    use_module(Source).
run_allowed_directive(ensure_loaded(Source)) :-
    !,
    allowed_source(Source),
    ensure_loaded(Source).
run_allowed_directive(consult(Source)) :-
    !,
    allowed_source(Source),
    consult(Source).
run_allowed_directive(load_files(Source)) :-
    !,
    allowed_source(Source),
    load_files(Source).
run_allowed_directive(load_files(Source, Options)) :-
    !,
    allowed_source(Source),
    load_files(Source, Options).
run_allowed_directive(Directive) :-
    throw(error(permission_error(load, directive, Directive), _)).

allowed_source(library(_)) :- !.
allowed_source([]) :- !.
allowed_source([H|T]) :-
    !,
    allowed_source(H),
    allowed_source(T).
allowed_source(Source) :-
    (atom(Source) ; string(Source)),
    absolute_file_name(Source, Absolute, [access(read), file_errors(fail)]),
    allowed_root(Root),
    atom_concat(Root, '/', Prefix),
    (Absolute = Root ; sub_atom(Absolute, 0, _, _, Prefix)),
    !.
allowed_source(Source) :-
    throw(error(permission_error(read, source_file, Source), _)).

reject_process_goal(Goal) :-
    var(Goal),
    !.
reject_process_goal(Module:Goal) :-
    atom(Module),
    !,
    reject_process_goal(Goal).
reject_process_goal(Goal) :-
    callable(Goal),
    functor(Goal, Name, Arity),
    (   unsafe_process_predicate(Name, Arity)
    ->  throw(error(permission_error(call, unsafe_process_predicate, Name/Arity), _))
    ;   true
    ),
    Goal =.. [_|Args],
    maplist(reject_process_goal, Args).
reject_process_goal(_).

unsafe_process_predicate(shell, _).
unsafe_process_predicate(process_create, _).
unsafe_process_predicate(process_wait, _).
unsafe_process_predicate(popen, _).
unsafe_process_predicate(exec, _).
unsafe_process_predicate(fork, _).
unsafe_process_predicate(kill, _).
unsafe_process_predicate(halt, _).
unsafe_process_predicate(load_foreign_library, _).
unsafe_process_predicate(open_shared_object, _).
unsafe_process_predicate(call_shared_object_function, _).

run_query(QueryText, MaxSolutions, StateCount, ProgramCount, Result) :-
    term_string(Query, QueryText, [variable_names(VariablePairs)]),
    reject_process_goal(Query),
    get_time(Start),
    with_output_to(
        string(UserOutput),
        findnsols(MaxSolutions, VariablePairs, Query, Solutions)
    ),
    get_time(End),
    Elapsed is End - Start,
    maplist(solution_dict, Solutions, JsonSolutions),
    length(JsonSolutions, Count),
    Result = _{
        ok:true,
        mode:query,
        solutions:JsonSolutions,
        solution_count:Count,
        output:UserOutput,
        state_clause_count:StateCount,
        program_clause_count:ProgramCount,
        elapsed_sec:Elapsed
    }.

solution_dict(VariablePairs, Dict) :-
    maplist(variable_pair, VariablePairs, Pairs),
    dict_pairs(Dict, solution, Pairs).

variable_pair(Name=Value, Name-Rendered) :-
    term_string(Value, Rendered, [quoted(true), numbervars(true)]).

inspect_state(StateFile, StateCount, ProgramCount, Result) :-
    current_prolog_flag(version_data, VersionData),
    term_string(VersionData, Version),
    findall(
        Indicator,
        (
            current_predicate(user:Name/Arity),
            functor(Head, Name, Arity),
            predicate_property(user:Head, dynamic),
            \+ predicate_property(user:Head, imported_from(_)),
            format(atom(Indicator), '~w/~w', [Name, Arity])
        ),
        Indicators0
    ),
    sort(Indicators0, Indicators),
    Result = _{
        ok:true,
        mode:inspect,
        swi_prolog_version:Version,
        state_file:StateFile,
        state_clause_count:StateCount,
        program_clause_count:ProgramCount,
        dynamic_predicates:Indicators
    }.
"""


def _set_workdir(path: str) -> None:
    global WORKDIR, STATE_FILE, TMPDIR
    WORKDIR = Path(path).expanduser().resolve()
    STATE_FILE = WORKDIR / "state" / "session.pl"
    TMPDIR = WORKDIR / "tmp"
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    TMPDIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        STATE_FILE.write_text("", encoding="utf-8")


def _set_read_root(path: str) -> None:
    global CODEX_ROOT
    CODEX_ROOT = Path(path).expanduser().resolve()


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

    Relative paths are tried under WORKDIR first, then under CODEX_ROOT, so
    source files in the current project directory are reachable. The runner's
    allowed_source/1 still independently re-checks the path against the same
    root, so a file is only consulted when it lies under CODEX_ROOT/WORKDIR.
    """
    raw = Path(user_path).expanduser()
    candidates = [raw.resolve()] if raw.is_absolute() else [
        (WORKDIR / raw).resolve(),
        (CODEX_ROOT / raw).resolve(),
    ]
    allowed = [c for c in candidates if _is_under(c, WORKDIR) or _is_under(c, CODEX_ROOT)]
    if not allowed:
        raise ValueError(f"Path escapes allowed read roots: {user_path}")
    for c in allowed:
        if not must_exist or c.exists():
            return c
    raise FileNotFoundError(user_path)


def _display_path(path: Path) -> str:
    for root in (WORKDIR, CODEX_ROOT):
        if _is_under(path, root):
            return str(path.relative_to(root))
    return str(path)


def _swipl_bin() -> str:
    resolved = shutil.which("swipl", path=SAFE_PATH)
    if resolved is None:
        raise FileNotFoundError("swipl not found on safe PATH")
    return resolved


def _check_text(name: str, text: str, max_bytes: int) -> None:
    if "\x00" in text:
        raise ValueError(f"{name} contains a NUL byte")
    if len(text.encode("utf-8")) > max_bytes:
        raise ValueError(f"{name} too large")


def _limit_child_resources() -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (DEFAULT_TIMEOUT_SEC + 5, DEFAULT_TIMEOUT_SEC + 5))
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))


def _write_temp_file(content: str, suffix: str) -> Path:
    TMPDIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=suffix, dir=TMPDIR, encoding="utf-8", delete=False) as f:
        f.write(content)
        return Path(f.name)


def _run_swipl(
    *,
    mode: str,
    query: str = "true.",
    program_text: str = "",
    max_solutions: int = 20,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
) -> Dict[str, Any]:
    if mode not in {"query", "inspect"}:
        raise ValueError("mode must be query or inspect")
    if max_solutions < 0 or max_solutions > MAX_SOLUTIONS:
        raise ValueError(f"max_solutions must be 0..{MAX_SOLUTIONS}")
    if timeout_sec < 1 or timeout_sec > 60:
        raise ValueError("timeout_sec must be 1..60")
    _check_text("query", query, MAX_QUERY_BYTES)
    _check_text("program_text", program_text, MAX_PROGRAM_BYTES)

    runner = _write_temp_file(RUNNER_SOURCE, ".pl")
    program_file = _write_temp_file(program_text, ".pl") if program_text else None
    start = time.perf_counter()
    try:
        argv = [
            _swipl_bin(),
            "-q",
            "--no-signals",
            "-f",
            "none",
            "-s",
            str(runner),
            "--",
            mode,
            str(STATE_FILE),
            str(program_file) if program_file else "-",
            query,
            str(max_solutions),
            str(CODEX_ROOT),
            str(WORKDIR),
        ]
        completed = subprocess.run(
            argv,
            cwd=str(WORKDIR),
            env={"LC_ALL": "C", "PATH": SAFE_PATH, "HOME": str(WORKDIR)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_sec,
            preexec_fn=_limit_child_resources if hasattr(resource, "setrlimit") else None,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"Prolog execution timed out after {timeout_sec} sec") from exc
    finally:
        runner.unlink(missing_ok=True)
        if program_file is not None:
            program_file.unlink(missing_ok=True)

    if len(completed.stdout) > MAX_OUTPUT_BYTES or len(completed.stderr) > MAX_OUTPUT_BYTES:
        raise ValueError("Prolog output too large")

    stdout = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace")
    try:
        result = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else {}
    except json.JSONDecodeError:
        result = {"ok": False, "error": "Prolog returned non-JSON output", "stdout": stdout}
    if "ok" not in result:
        result = {"ok": False, "error": "Prolog returned no JSON result", "stdout": stdout, **result}
    result["returncode"] = completed.returncode
    result["stderr"] = stderr
    result["elapsed_sec_total"] = time.perf_counter() - start
    return result


@mcp.tool()
def run_prolog(
    query: str,
    program_text: str = "",
    persist_program: bool = False,
    max_solutions: int = 20,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
) -> Dict[str, Any]:
    """
    Run an SWI-Prolog query.

    query examples:
      "member(X, [a,b])."
      "parent(X, bob)."

    program_text may contain facts, rules, SWI-Prolog library imports, and
    source loads under the configured Codex read root. External process
    predicates are rejected. When persist_program is true and validation
    succeeds, program_text is appended to WORKDIR/state/session.pl for later
    queries.
    """
    result = _run_swipl(
        mode="query",
        query=query,
        program_text=program_text,
        max_solutions=max_solutions,
        timeout_sec=timeout_sec,
    )
    if persist_program and program_text and result.get("ok"):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with STATE_FILE.open("a", encoding="utf-8") as f:
            if STATE_FILE.stat().st_size and not program_text.startswith("\n"):
                f.write("\n")
            f.write(program_text)
            if not program_text.endswith("\n"):
                f.write("\n")
    return result


def _prolog_quoted_atom(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


@mcp.tool()
def run_prolog_file(
    file: str,
    query: str = "true.",
    persist_program: bool = False,
    max_solutions: int = 20,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
) -> Dict[str, Any]:
    """
    Consult a Prolog source file and run a query against it.

    file is resolved under the work directory or the configured Codex read root
    (the current project directory) and must be an existing .pl/.pro/.prolog
    file. The server generates a validated `:- ensure_loaded('<abs path>').`
    directive so only files inside the allowed roots are loaded; external
    process predicates remain rejected. query runs after the file is loaded,
    e.g. "ancestor(X, bob).". When persist_program is true and the run
    succeeds, the ensure_loaded directive is appended to
    WORKDIR/state/session.pl so later queries see the loaded clauses.
    """
    path = _safe_read_path(file, must_exist=True)
    if not path.is_file():
        raise ValueError(f"Not a regular file: {file}")
    if path.suffix.lower() not in {".pl", ".pro", ".prolog"}:
        raise ValueError("file must be a .pl, .pro, or .prolog source file")
    program_text = f":- ensure_loaded({_prolog_quoted_atom(str(path))}).\n"
    result = _run_swipl(
        mode="query",
        query=query,
        program_text=program_text,
        max_solutions=max_solutions,
        timeout_sec=timeout_sec,
    )
    if persist_program and result.get("ok"):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with STATE_FILE.open("a", encoding="utf-8") as f:
            if STATE_FILE.stat().st_size:
                f.write("\n")
            f.write(program_text)
    result["file"] = _display_path(path)
    result["persisted"] = bool(persist_program and result.get("ok"))
    return result


@mcp.tool()
def get_interpreter_state() -> Dict[str, Any]:
    """Return SWI-Prolog version and the persisted interpreter state summary."""
    return _run_swipl(mode="inspect", query="true.", max_solutions=0, timeout_sec=DEFAULT_TIMEOUT_SEC)


@mcp.tool()
def reset_interpreter_state() -> Dict[str, Any]:
    """Clear the persisted Prolog clauses under the restricted work directory."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text("", encoding="utf-8")
    return {"ok": True, "state_file": str(STATE_FILE.relative_to(WORKDIR)), "state_clause_count": 0}


@mcp.tool()
def list_server_limits() -> Dict[str, Any]:
    """List the Prolog MCP server workdir and execution limits."""
    return {
        "workdir": str(WORKDIR),
        "state_file": str(STATE_FILE.relative_to(WORKDIR)),
        "max_program_bytes": MAX_PROGRAM_BYTES,
        "max_query_bytes": MAX_QUERY_BYTES,
        "max_output_bytes": MAX_OUTPUT_BYTES,
        "default_timeout_sec": DEFAULT_TIMEOUT_SEC,
        "max_solutions": MAX_SOLUTIONS,
        "swipl": _swipl_bin(),
        "codex_read_root": str(CODEX_ROOT),
        "read_roots": [str(WORKDIR), str(CODEX_ROOT)],
        "libraries_allowed": True,
        "source_file_tool": "run_prolog_file: consult a .pl/.pro/.prolog file under workdir or codex_read_root, then query",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", default=os.environ.get("PROLOG_MCP_WORKDIR", os.getcwd()))
    parser.add_argument("--read-root", default=os.environ.get("PROLOG_MCP_READ_ROOT", str(CODEX_ROOT)))
    args = parser.parse_args()
    _set_workdir(args.workdir)
    _set_read_root(args.read_root)
    mcp.run()


if __name__ == "__main__":
    main()
