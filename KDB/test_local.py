import tempfile
import unittest
from pathlib import Path

from kdb_mcp_server import (
    _set_codex_csv_root,
    _set_workdir,
    get_interpreter_state,
    get_state_source,
    list_server_limits,
    load_csv,
    load_table,
    prune_state,
    reset_interpreter_state,
    run_q,
    save_csv,
    save_table,
)


class RestrictedKdbTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.codex_tmp = tempfile.TemporaryDirectory()
        _set_workdir(self.tmp.name)
        _set_codex_csv_root(self.codex_tmp.name)
        self.workdir = Path(self.tmp.name)
        self.codex_root = Path(self.codex_tmp.name)

    def tearDown(self) -> None:
        self.codex_tmp.cleanup()
        self.tmp.cleanup()

    def test_run_expression(self) -> None:
        result = run_q("1+2")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stdout"].strip(), "3")

    def test_multiline_statement_runs(self) -> None:
        # Fix 1: a statement split across lines (continuation lines indented)
        # must execute, not fail line-by-line.
        code = "show ([] a:1 2 3;\n   b:10 20 30)\nselect tot:sum a from ([] a:5 6 7)"
        result = run_q(code)
        self.assertTrue(result["ok"], result)
        self.assertIn("18", result["stdout"])  # 5+6+7
        self.assertIn("30", result["stdout"])  # table literal row

    def test_implicit_result_is_still_printed(self) -> None:
        # Fix 1 must not regress REPL-style auto-printing of a bare expression.
        result = run_q("2+3")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stdout"].strip(), "5")

    def test_replay_output_is_suppressed(self) -> None:
        # Fix 3: output from replayed (persisted) state must not leak into a
        # later call's stdout.
        run_q("x:42\nshow `persisted_noise_marker", persist_code=True)
        result = run_q("1+1")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stdout"].strip(), "2")
        self.assertNotIn("persisted_noise_marker", result["stdout"])

    def test_tall_table_not_truncated(self) -> None:
        # Fix 4: a 100-row table must print fully (no console-height truncation).
        result = run_q("show ([] i:til 100)")
        self.assertTrue(result["ok"], result)
        self.assertIn("99", result["stdout"])
        self.assertNotIn("..", result["stdout"])

    def test_get_and_prune_state(self) -> None:
        # Fix 2: inspect persisted lines and delete cruft without a full reset.
        run_q("a:1", persist_code=True)
        run_q("show `junk_line", persist_code=True)
        run_q("b:2", persist_code=True)
        src = get_state_source()
        self.assertEqual(src["line_count"], 3, src)
        self.assertEqual(src["lines"][1]["line"], "show `junk_line", src)

        pruned = prune_state([2])
        self.assertTrue(pruned["ok"], pruned)
        self.assertEqual(pruned["removed_count"], 1, pruned)
        self.assertEqual(pruned["line_count"], 2, pruned)

        followup = run_q("a+b")
        self.assertTrue(followup["ok"], followup)
        self.assertEqual(followup["stdout"].strip(), "3")
        self.assertNotIn("junk_line", followup["stdout"])

    def test_prune_state_rolls_back_when_replay_breaks(self) -> None:
        run_q("t:([]x:1 2 3)", persist_code=True)
        run_q("u:select sx:sum x from t", persist_code=True)
        # Dropping line 1 (defines t) would break line 2 (uses t) -> rollback.
        result = prune_state([1])
        self.assertFalse(result["ok"], result)
        self.assertTrue(result["rolled_back"], result)
        self.assertEqual(get_state_source()["line_count"], 2)
        self.assertTrue(run_q("count t")["ok"])

    def test_prune_state_rejects_bad_line_numbers(self) -> None:
        run_q("a:1", persist_code=True)
        with self.assertRaises(ValueError):
            prune_state([])
        with self.assertRaises(ValueError):
            prune_state([5])

    def test_persisted_state_is_replayed(self) -> None:
        result = run_q("f:{x+1}", persist_code=True)
        self.assertTrue(result["ok"], result)
        followup = run_q("f 41")
        self.assertTrue(followup["ok"], followup)
        self.assertEqual(followup["stdout"].strip(), "42")

    def test_state_summary_and_reset(self) -> None:
        run_q("a:10\nf:{x+a}", persist_code=True)
        state = get_interpreter_state()
        self.assertTrue(state["ok"], state)
        self.assertGreater(state["state_bytes"], 0)
        self.assertIn("a", state["variables_rendered"])
        self.assertIn("f", state["functions_rendered"])
        reset = reset_interpreter_state()
        self.assertTrue(reset["ok"], reset)
        self.assertEqual(reset["state_bytes"], 0)

    def test_dangerous_features_are_rejected(self) -> None:
        for code in [
            "\\ls",
            "system \"ls\"",
            "get `:file",
            "set[`x;1]",
            "value \"1+1\"",
            "hopen `:localhost:5000",
            ".z.p",
            ".Q.s1 1",
            "0: `:file",
        ]:
            with self.subTest(code=code):
                with self.assertRaises(ValueError):
                    run_q(code)

    def test_load_csv_persists_table_for_run_q(self) -> None:
        data_dir = self.workdir / "data"
        data_dir.mkdir()
        csv_path = data_dir / "sample.csv"
        csv_path.write_text(
            "Date,Open,Close\n"
            "2020-01-01,1.5,2.5\n"
            "2020-01-02,3.5,4.5\n",
            encoding="utf-8",
        )

        loaded = load_csv("prices", "data/sample.csv", "DFF")
        self.assertTrue(loaded["ok"], loaded)
        self.assertTrue(loaded["persisted"], loaded)
        self.assertIn("Date", loaded["stdout"])
        self.assertIn("Close", loaded["stdout"])

        queried = run_q("select avgClose:avg Close from prices")
        self.assertTrue(queried["ok"], queried)
        self.assertIn("3.5", queried["stdout"])

    def test_load_csv_rejects_unsafe_inputs(self) -> None:
        csv_path = self.workdir / "sample.csv"
        csv_path.write_text("A\n1\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_csv("bad-name", "sample.csv", "I")
        with self.assertRaises(ValueError):
            load_csv("t", "../sample.csv", "I")
        with self.assertRaises(ValueError):
            load_csv("t", "sample.csv", "I;system")

    def test_load_csv_can_read_under_codex_root(self) -> None:
        data_dir = self.codex_root / "analysis" / "data"
        data_dir.mkdir(parents=True)
        csv_path = data_dir / "outside_workdir.csv"
        csv_path.write_text(
            "Date,Close\n"
            "2024-01-01,10.5\n"
            "2024-01-02,12.5\n",
            encoding="utf-8",
        )

        loaded = load_csv("external_prices", "analysis/data/outside_workdir.csv", "DF")
        self.assertTrue(loaded["ok"], loaded)
        self.assertEqual(loaded["input_file"], "analysis/data/outside_workdir.csv")
        queried = run_q("select maxClose:max Close from external_prices")
        self.assertTrue(queried["ok"], queried)
        self.assertIn("12.5", queried["stdout"])

    def test_save_table_then_load_table_survives_reset(self) -> None:
        run_q("trades:([]sym:`AAA`BBB;px:1.5 2.5)", persist_code=True)

        saved = save_table("trades", "tables/trades.kdb")
        self.assertTrue(saved["ok"], saved)
        self.assertTrue(saved["saved"], saved)
        self.assertGreater(saved["file_bytes"], 0)
        self.assertTrue((self.workdir / "tables" / "trades.kdb").exists())

        # Drop all in-memory/replayed state, then restore purely from binary.
        reset_interpreter_state()
        loaded = load_table("trades", "tables/trades.kdb")
        self.assertTrue(loaded["ok"], loaded)
        self.assertTrue(loaded["persisted"], loaded)
        self.assertIn("sym", loaded["stdout"])

        queried = run_q("select sumPx:sum px from trades")
        self.assertTrue(queried["ok"], queried)
        self.assertIn("4", queried["stdout"])

    def test_save_table_rejects_unsafe_inputs(self) -> None:
        run_q("t:([]x:1 2)", persist_code=True)
        with self.assertRaises(ValueError):
            save_table("bad-name", "tables/t.kdb")
        with self.assertRaises(ValueError):
            save_table("t", "../escape.kdb")
        with self.assertRaises(FileNotFoundError):
            load_table("t", "tables/missing.kdb")

    def test_save_table_unknown_table_is_not_ok(self) -> None:
        result = save_table("nope", "tables/nope.kdb")
        self.assertFalse(result["ok"], result)
        self.assertFalse(result["saved"], result)

    def test_save_csv_writes_header_and_rows(self) -> None:
        run_q("t:([]Date:2020.01.01 2020.01.02;sym:`AAA`BBB;px:1.5 2.5)", persist_code=True)

        saved = save_csv("t", "out/t.csv")
        self.assertTrue(saved["ok"], saved)
        self.assertTrue(saved["saved"], saved)
        self.assertGreater(saved["file_bytes"], 0)
        out = (self.workdir / "out" / "t.csv").read_text(encoding="utf-8")
        self.assertEqual(out.splitlines()[0], "Date,sym,px")
        self.assertIn("2020-01-01,AAA,1.5", out)

    def test_save_csv_under_codex_root_and_tsv(self) -> None:
        run_q("t:([]a:1 2;b:10 20)", persist_code=True)
        dest = self.codex_root / "exports" / "t.tsv"
        saved = save_csv("t", str(dest), delimiter="\t")
        self.assertTrue(saved["ok"], saved)
        self.assertEqual(saved["dest_file"], "exports/t.tsv")
        out = dest.read_text(encoding="utf-8")
        self.assertEqual(out.splitlines()[0], "a\tb")
        self.assertIn("1\t10", out)

    def test_save_csv_rejects_unsafe_inputs(self) -> None:
        run_q("t:([]x:1 2)", persist_code=True)
        with self.assertRaises(ValueError):
            save_csv("bad-name", "out/t.csv")
        with self.assertRaises(ValueError):
            save_csv("t", "../escape.csv")
        with self.assertRaises(ValueError):
            save_csv("t", "out/t.parquet")
        with self.assertRaises(ValueError):
            save_csv("t", "out/t.csv", delimiter=",,")

    def test_save_csv_unknown_table_is_not_ok(self) -> None:
        result = save_csv("nope", "out/nope.csv")
        self.assertFalse(result["ok"], result)
        self.assertFalse(result["saved"], result)

    def test_user_code_still_cannot_text_save(self) -> None:
        with self.assertRaises(ValueError):
            run_q("`:out.csv 0: csv 0: ([]x:1 2)")

    def test_user_code_still_cannot_set_or_get(self) -> None:
        for code in ["get `:file", "set[`x;1]", "`:file set 1"]:
            with self.subTest(code=code):
                with self.assertRaises(ValueError):
                    run_q(code)

    def test_limits(self) -> None:
        limits = list_server_limits()
        self.assertTrue(limits["q"].endswith("/q"), limits)
        self.assertEqual(limits["state_file"], "state/session.q")
        self.assertEqual(limits["codex_csv_root"], str(self.codex_root))
        joined = " ".join(limits["enabled_file_tools"])
        self.assertIn("save_table", joined)
        self.assertIn("load_table", joined)
        self.assertIn("save_csv", joined)


if __name__ == "__main__":
    unittest.main()
