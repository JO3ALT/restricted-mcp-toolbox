import tempfile
import unittest
from pathlib import Path

from lean_mcp_server import (
    _set_read_root,
    _set_workdir,
    check_lean_code,
    check_lean_file,
    get_lean_environment,
    list_server_limits,
)


class RestrictedLeanTests(unittest.TestCase):
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

    def test_valid_proof_checks(self) -> None:
        result = check_lean_code(
            "example (P Q : Prop) : P -> Q -> P := by\n"
            "  intro hp _\n"
            "  exact hp\n"
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["error_count"], 0)

    def test_invalid_proof_fails(self) -> None:
        result = check_lean_code("example (P Q : Prop) : P -> Q := by\n  intro hp\n  exact hp\n")
        self.assertFalse(result["ok"], result)
        self.assertGreater(result["error_count"], 0)

    def test_sorry_is_rejected_before_lean_runs(self) -> None:
        with self.assertRaises(ValueError):
            check_lean_code("example : True := by\n  sorry\n")

    def test_import_is_allowed(self) -> None:
        result = check_lean_code("import Init\n\nexample : True := by\n  trivial\n")
        self.assertTrue(result["ok"], result)

    def test_unsafe_features_are_rejected(self) -> None:
        for code in [
            "axiom bad : False\n",
            "constant bad : False\n",
            "unsafe def bad : Nat := 0\n",
            "#eval IO.println \"hi\"\n",
        ]:
            with self.subTest(code=code):
                with self.assertRaises(ValueError):
                    check_lean_code(code)

    def test_check_file_is_limited_to_workdir(self) -> None:
        p = self.workdir / "valid.lean"
        p.write_text("example : True := by\n  trivial\n", encoding="utf-8")
        result = check_lean_file("valid.lean")
        self.assertTrue(result["ok"], result)
        with self.assertRaises(ValueError):
            check_lean_file("../outside.lean")

    def test_check_file_can_read_from_read_root(self) -> None:
        p = self.read_root / "ext.lean"
        p.write_text("example : True := by\n  trivial\n", encoding="utf-8")
        result = check_lean_file(str(p))
        self.assertTrue(result["ok"], result)

    def test_check_file_rejects_outside_read_root(self) -> None:
        with self.assertRaises(ValueError):
            check_lean_file(str(self.read_root / ".." / "escape.lean"))

    def test_environment_and_limits(self) -> None:
        env = get_lean_environment()
        self.assertTrue(env["ok"], env)
        self.assertIn("Lean", env["version"])
        limits = list_server_limits()
        self.assertEqual(limits["tmpdir"], "tmp")
        self.assertTrue(limits["imports_allowed"])


if __name__ == "__main__":
    unittest.main()
