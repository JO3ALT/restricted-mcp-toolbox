import tempfile
import unittest
from pathlib import Path

from prolog_mcp_server import (
    _set_read_root,
    _set_workdir,
    get_interpreter_state,
    reset_interpreter_state,
    run_prolog,
    run_prolog_file,
)


class RestrictedPrologTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.read_tmp = tempfile.TemporaryDirectory()
        _set_workdir(self.tmp.name)
        _set_read_root(self.read_tmp.name)
        self.workdir = Path(self.tmp.name)
        self.read_root = Path(self.read_tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()
        self.read_tmp.cleanup()

    def test_query_returns_solutions(self) -> None:
        result = run_prolog("member(X, [red, blue]).", max_solutions=10)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["solution_count"], 2)
        self.assertEqual(result["solutions"][0]["X"], "red")
        self.assertEqual(result["solutions"][1]["X"], "blue")

    def test_program_can_be_persisted_and_inspected(self) -> None:
        program = "parent(alice, bob).\nancestor(X, Y) :- parent(X, Y).\n"
        result = run_prolog("ancestor(X, bob).", program_text=program, persist_program=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["solutions"], [{"X": "alice"}])

        followup = run_prolog("parent(X, bob).")
        self.assertTrue(followup["ok"], followup)
        self.assertEqual(followup["solutions"], [{"X": "alice"}])

        state = get_interpreter_state()
        self.assertTrue(state["ok"], state)
        self.assertEqual(state["state_clause_count"], 2)
        self.assertIn("parent/2", state["dynamic_predicates"])
        self.assertIn("ancestor/2", state["dynamic_predicates"])

    def test_reset_clears_state(self) -> None:
        run_prolog("true.", program_text="parent(alice, bob).\n", persist_program=True)
        reset = reset_interpreter_state()
        self.assertTrue(reset["ok"], reset)
        state = get_interpreter_state()
        self.assertEqual(state["state_clause_count"], 0)

    def test_directives_are_rejected(self) -> None:
        result = run_prolog("true.", program_text=":- shell(id).\n")
        self.assertFalse(result["ok"], result)
        self.assertIn("directive", result["error"])

    def test_library_import_is_allowed(self) -> None:
        result = run_prolog(
            "items(L), member(X, L).",
            program_text=":- use_module(library(lists)).\nitems([a,b,c]).\n",
            max_solutions=10,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual([s["X"] for s in result["solutions"]], ["a", "b", "c"])

    def test_unsafe_query_is_rejected(self) -> None:
        result = run_prolog("shell(id).")
        self.assertFalse(result["ok"], result)
        self.assertIn("unsafe_process_predicate", result["error"])

    def test_unsafe_rule_body_is_rejected(self) -> None:
        result = run_prolog("true.", program_text="bad :- shell(id).\n")
        self.assertFalse(result["ok"], result)
        self.assertIn("unsafe_process_predicate", result["error"])

    def test_run_prolog_file_loads_source_from_read_root(self) -> None:
        src = self.read_root / "family.pl"
        src.write_text(
            "parent(alice, bob).\nancestor(X, Y) :- parent(X, Y).\n",
            encoding="utf-8",
        )
        result = run_prolog_file(str(src), "ancestor(X, bob).")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["solutions"], [{"X": "alice"}])

    def test_run_prolog_file_can_persist(self) -> None:
        src = self.read_root / "facts.pl"
        src.write_text("color(red).\ncolor(blue).\n", encoding="utf-8")
        result = run_prolog_file(str(src), "color(X).", persist_program=True, max_solutions=10)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["persisted"])
        followup = run_prolog("color(X).", max_solutions=10)
        self.assertEqual([s["X"] for s in followup["solutions"]], ["red", "blue"])

    def test_run_prolog_file_rejects_outside_read_root(self) -> None:
        with self.assertRaises(ValueError):
            run_prolog_file(str(self.read_root / ".." / "escape.pl"), "true.")

    def test_run_prolog_file_rejects_non_prolog_extension(self) -> None:
        bad = self.read_root / "data.txt"
        bad.write_text("color(red).\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            run_prolog_file(str(bad), "true.")

    def test_run_prolog_file_rejects_unsafe_query(self) -> None:
        src = self.read_root / "ok.pl"
        src.write_text("color(red).\n", encoding="utf-8")
        result = run_prolog_file(str(src), "shell(id).")
        self.assertFalse(result["ok"], result)
        self.assertIn("unsafe_process_predicate", result["error"])


if __name__ == "__main__":
    unittest.main()
