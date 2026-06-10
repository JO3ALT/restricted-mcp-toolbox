import tempfile
import unittest
from pathlib import Path

from filter_mcp_server import (
    _run_pipeline_internal,
    _set_read_root,
    _set_workdir,
    csv_summary,
    group_by_count,
    preview_file,
)


class RestrictedFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.read_tmp = tempfile.TemporaryDirectory()
        _set_workdir(self.tmp.name)
        _set_read_root(self.read_tmp.name)
        self.workdir = Path(self.tmp.name)
        self.read_root = Path(self.read_tmp.name)
        (self.workdir / "sample.csv").write_text(
            "name,team\nalice,red\nbob,blue\ncara,red\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()
        self.read_tmp.cleanup()

    def test_pipeline_counts_csv_field(self) -> None:
        result = _run_pipeline_internal(
            [
                {"cmd": "awk", "args": ["-F,", "NR>1{print $2}"]},
                {"cmd": "sort", "args": []},
                {"cmd": "uniq", "args": ["-c"]},
                {"cmd": "sort", "args": ["-nr"]},
            ],
            input_file="sample.csv",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["stdout"], "      2 red\n      1 blue\n")

    def test_group_by_count_can_write_inside_workdir(self) -> None:
        result = group_by_count("sample.csv", 2, output_file="counts.txt")
        self.assertTrue(result["ok"])
        self.assertEqual(result["output_file"], "counts.txt")
        self.assertIn("2 red", (self.workdir / "counts.txt").read_text(encoding="utf-8"))

    def test_file_tools_reject_path_escape(self) -> None:
        with self.assertRaises(ValueError):
            preview_file("../outside.txt")
        with self.assertRaises(ValueError):
            csv_summary("/etc/passwd")

    def test_commands_cannot_read_file_operands(self) -> None:
        with self.assertRaises(ValueError):
            _run_pipeline_internal([{"cmd": "cat", "args": ["/etc/passwd"]}])
        with self.assertRaises(ValueError):
            _run_pipeline_internal([{"cmd": "grep", "args": ["root", "/etc/passwd"]}])

    def test_dangerous_embedded_features_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _run_pipeline_internal([{"cmd": "awk", "args": ['BEGIN{system("id")}']}])
        with self.assertRaises(ValueError):
            _run_pipeline_internal([{"cmd": "awk", "args": ['{print | "sh"}']}], input_text="x\n")
        with self.assertRaises(ValueError):
            _run_pipeline_internal([{"cmd": "awk", "args": ['BEGIN{print "x" > "out"}']}])
        with self.assertRaises(ValueError):
            _run_pipeline_internal([{"cmd": "sed", "args": ["1e id"]}], input_text="x\n")
        with self.assertRaises(ValueError):
            _run_pipeline_internal([{"cmd": "grep", "args": ["-r", "root"]}])

    def test_output_file_cannot_escape_workdir(self) -> None:
        with self.assertRaises(ValueError):
            _run_pipeline_internal([{"cmd": "wc", "args": ["-l"]}], input_text="x\n", output_file="../out")

    def test_can_read_input_from_read_root(self) -> None:
        (self.read_root / "ext.csv").write_text("name,team\nzed,green\n", encoding="utf-8")
        result = csv_summary(str(self.read_root / "ext.csv"))
        self.assertEqual(result["data_rows"], 1)
        self.assertEqual(result["header"], ["name", "team"])
        pipe = _run_pipeline_internal(
            [{"cmd": "awk", "args": ["-F,", "NR>1{print $2}"]}],
            input_file=str(self.read_root / "ext.csv"),
        )
        self.assertTrue(pipe["ok"])
        self.assertEqual(pipe["stdout"], "green\n")

    def test_writes_stay_in_workdir_not_read_root(self) -> None:
        with self.assertRaises(ValueError):
            _run_pipeline_internal(
                [{"cmd": "wc", "args": ["-l"]}],
                input_text="x\n",
                output_file=str(self.read_root / "out.txt"),
            )

    def test_reads_outside_all_roots_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            csv_summary("/etc/passwd")
        with self.assertRaises(ValueError):
            preview_file(str(self.read_root / ".." / "escape.txt"))

    def test_awk_script_file_from_read_root(self) -> None:
        (self.read_root / "agg.awk").write_text("NR>1{print $2}\n", encoding="utf-8")
        result = _run_pipeline_internal(
            [{"cmd": "awk", "args": ["-F,", "-f", str(self.read_root / "agg.awk")]}],
            input_file="sample.csv",
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stdout"], "red\nblue\nred\n")

    def test_sed_script_file_from_read_root(self) -> None:
        (self.read_root / "up.sed").write_text("s/red/RED/\n", encoding="utf-8")
        result = _run_pipeline_internal(
            [{"cmd": "sed", "args": ["--file=" + str(self.read_root / "up.sed")]}],
            input_text="red\nblue\n",
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stdout"], "RED\nblue\n")

    def test_script_file_with_dangerous_content_is_rejected(self) -> None:
        (self.read_root / "bad.awk").write_text('BEGIN{system("id")}\n', encoding="utf-8")
        with self.assertRaises(ValueError):
            _run_pipeline_internal(
                [{"cmd": "awk", "args": ["-f", str(self.read_root / "bad.awk")]}],
                input_text="x\n",
            )

    def test_script_file_outside_roots_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _run_pipeline_internal(
                [{"cmd": "awk", "args": ["-f", "/etc/passwd"]}],
                input_text="x\n",
            )

    def test_script_file_with_inline_program_is_rejected(self) -> None:
        (self.read_root / "p.awk").write_text("{print}\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            _run_pipeline_internal(
                [{"cmd": "awk", "args": ["-f", str(self.read_root / "p.awk"), "{print $1}"]}],
                input_text="x\n",
            )


if __name__ == "__main__":
    unittest.main()
