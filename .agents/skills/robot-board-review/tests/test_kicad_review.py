"""Wrapper contract tests using mock KiCad calls, NOT native ERC/DRC tests."""
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SKILL = Path(__file__).resolve().parents[1]
FIXTURES = SKILL / "tests/fixtures"
SPEC = importlib.util.spec_from_file_location("kicad_review", SKILL / "scripts/kicad_review.py")
assert SPEC is not None and SPEC.loader is not None
review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sch = self.root / "root with spaces.kicad_sch"
        self.pcb = self.root / "controller.kicad_pcb"
        # These are mock inputs, deliberately not valid KiCad designs.
        self.sch.write_text("(kicad_sch)", encoding="utf-8")
        self.pcb.write_text("mock pcb", encoding="utf-8")
        self.out = self.root / "reports"
        self.calls = []
        self.native_exit = 0
        self.report_mode = "ok"
        self.refill = True
        self.fail_kind = None
        self.timeout_kind = None
        self.mutate = False
        self.dependency_mutate_path = None

    def fake_run(self, command, **kwargs):
        self.calls.append(command)
        self.assertFalse(kwargs.get("shell", False))
        self.assertGreater(kwargs["timeout"], 0)
        if command[1:] == ["version"]:
            return subprocess.CompletedProcess(command, 0, "10.0.0\n", "")
        if command[-1] == "--help":
            flags = "--format --severity-all --exit-code-violations"
            if self.refill:
                flags += " --refill-zones"
            return subprocess.CompletedProcess(command, 0, flags, "")
        if command[1] == self.timeout_kind:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        report = Path(command[command.index("--output") + 1])
        if self.report_mode == "ok":
            report.write_text(json.dumps({"source": command[-1], "violations": []}), encoding="utf-8")
        elif self.report_mode == "invalid":
            report.write_text("not json", encoding="utf-8")
        elif self.report_mode == "empty":
            report.write_text("{}", encoding="utf-8")
        if self.mutate:
            Path(command[-1]).write_text("changed", encoding="utf-8")
        if self.dependency_mutate_path is not None:
            changed = "(kicad_sch (text \"changed\"))" if (
                self.dependency_mutate_path.suffix == ".kicad_sch"
            ) else "changed"
            self.dependency_mutate_path.write_text(changed, encoding="utf-8")
        code = 3 if command[1] == self.fail_kind else self.native_exit
        return subprocess.CompletedProcess(command, code, "mock stdout", "mock stderr")

    def invoke(self, *args):
        with patch.object(review.shutil, "which", return_value="/mock/kicad-cli"), \
             patch.object(review.subprocess, "run", side_effect=self.fake_run), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return review.main([*args, "--output-dir", str(self.out)])

    def summaries(self):
        return [json.loads(p.read_text()) for p in self.out.glob("*/summary.json")]

    def assert_preflight_failure_reports(self, expected_inputs):
        summaries = self.summaries()
        self.assertEqual(len(summaries), 1)
        summary = summaries[0]
        self.assertEqual(summary["exit_code"], 2)
        self.assertIn("preflight_error", summary)
        self.assertEqual([c["input"] for c in summary["checks"]], expected_inputs)
        for check in summary["checks"]:
            self.assertEqual(check["status"], "NOT_CHECKED")
            self.assertIsNone(check["command"])
            self.assertIn(summary["preflight_error"], check["error"])
            log = Path(check["log"]).read_text(encoding="utf-8")
            self.assertIn("NOT_CHECKED", log)
            self.assertIn(check["input"], log)
            self.assertIn(summary["preflight_error"], log)
        self.assertTrue(next(self.out.glob("*/preflight.log")).is_file())
        self.assertEqual(self.calls, [])

    def test_no_inputs_is_not_success(self):
        with self.assertRaises(SystemExit) as exc:
            self.invoke()
        self.assertEqual(exc.exception.code, 2)
        self.assertEqual(self.calls, [])

    def test_missing_input_is_not_success(self):
        missing = self.root / "missing.kicad_pcb"
        self.assertEqual(self.invoke("--pcb", str(missing)), 2)
        self.assert_preflight_failure_reports([str(missing)])

    def test_wrong_extension_is_not_success(self):
        self.assertEqual(self.invoke("--pcb", str(self.sch)), 2)
        self.assert_preflight_failure_reports([str(self.sch)])

    def test_directory_input_is_not_success(self):
        directory = self.root / "directory.kicad_pcb"
        directory.mkdir()
        self.assertEqual(self.invoke("--pcb", str(directory)), 2)
        self.assert_preflight_failure_reports([str(directory)])

    def test_missing_input_leaves_all_requested_checks_not_checked(self):
        missing = self.root / "missing.kicad_pcb"
        self.assertEqual(self.invoke("--schematic", str(self.sch), "--pcb", str(missing)), 2)
        self.assert_preflight_failure_reports([str(self.sch), str(missing)])

    def test_missing_cli_is_not_success(self):
        with patch.object(review.shutil, "which", return_value=None), redirect_stderr(io.StringIO()):
            self.assertEqual(review.main([
                "--schematic", str(self.sch), "--pcb", str(self.pcb),
                "--output-dir", str(self.out),
            ]), 2)
        summary = self.summaries()[0]
        self.assertEqual(summary["exit_code"], 2)
        self.assertIn("preflight_error", summary)
        self.assertEqual(
            [check["status"] for check in summary["checks"]],
            ["NOT_CHECKED", "NOT_CHECKED"],
        )
        for check in summary["checks"]:
            log = Path(check["log"])
            self.assertTrue(log.is_file())
            self.assertIn("NOT_CHECKED", log.read_text(encoding="utf-8"))
        self.assertTrue((next(self.out.glob("*/preflight.log"))).is_file())

    def test_unsupported_cli_is_not_success(self):
        with patch.object(review.shutil, "which", return_value="/mock/kicad-cli"), \
             patch.object(review.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "9.0.0", "")), \
             redirect_stderr(io.StringIO()):
            self.assertEqual(review.main([
                "--pcb", str(self.pcb), "--output-dir", str(self.out),
            ]), 2)
        summary = self.summaries()[0]
        self.assertEqual(summary["checks"][0]["status"], "NOT_CHECKED")
        self.assertIn("Unsupported KiCad", summary["checks"][0]["error"])

    def test_both_checks_include_warnings_exclusions_and_do_not_save_or_infer_parity(self):
        self.assertEqual(self.invoke("--schematic", str(self.sch), "--pcb", str(self.pcb)), 0)
        commands = [c for c in self.calls if "--output" in c]
        self.assertEqual(len(commands), 2)
        for command in commands:
            self.assertIn("--severity-all", command)
            self.assertIn("--exit-code-violations", command)
            self.assertNotIn("--save-board", command)
            self.assertNotIn("--schematic-parity", command)
        self.assertNotIn("--refill-zones", commands[0])
        self.assertIn("--refill-zones", commands[1])
        self.assertEqual(commands[0][-1], str(self.sch))
        self.assertEqual(self.pcb.read_text(), "mock pcb")
        summary = self.summaries()[0]
        self.assertEqual([c["status"] for c in summary["checks"]], ["PASS", "PASS"])
        self.assertTrue(summary["manual_review_required"])

    def test_native_violation_exit_becomes_lint_failure(self):
        self.native_exit = 5
        self.assertEqual(self.invoke("--pcb", str(self.pcb)), 1)
        self.assertEqual(self.summaries()[0]["checks"][0]["status"], "FAIL")

    def test_tool_error_does_not_prevent_remaining_check(self):
        self.fail_kind = "sch"
        self.assertEqual(self.invoke("--schematic", str(self.sch), "--pcb", str(self.pcb)), 2)
        self.assertEqual([c["status"] for c in self.summaries()[0]["checks"]], ["NOT_CHECKED", "PASS"])

    def test_timeout_is_not_success(self):
        self.timeout_kind = "pcb"
        self.assertEqual(self.invoke("--pcb", str(self.pcb)), 2)

    def test_bad_or_missing_report_is_not_success(self):
        for mode in ("missing", "invalid", "empty"):
            with self.subTest(mode=mode):
                self.report_mode = mode
                self.assertEqual(self.invoke("--pcb", str(self.pcb)), 2)

    def test_new_run_cannot_reuse_stale_report(self):
        self.assertEqual(self.invoke("--pcb", str(self.pcb)), 0)
        self.report_mode = "missing"
        self.assertEqual(self.invoke("--pcb", str(self.pcb)), 2)
        self.assertEqual(len(self.summaries()), 2)

    def test_duplicate_inputs_are_only_checked_once(self):
        self.assertEqual(self.invoke("--pcb", str(self.pcb), "--pcb", str(self.pcb)), 0)
        self.assertEqual(len(self.summaries()[0]["checks"]), 1)

    def test_same_basename_does_not_collide(self):
        other = self.root / "other" / self.pcb.name
        other.parent.mkdir()
        other.write_text("second mock pcb")
        self.assertEqual(self.invoke("--pcb", str(self.pcb), "--pcb", str(other)), 0)
        reports = [c["report"] for c in self.summaries()[0]["checks"]]
        self.assertEqual(len(set(reports)), 2)

    def test_old_cli_requires_explicit_prefilled_confirmation(self):
        self.refill = False
        self.assertEqual(self.invoke("--pcb", str(self.pcb)), 2)
        self.assertEqual(self.invoke("--pcb", str(self.pcb), "--zones-prefilled"), 0)
        success_summary = next(
            summary for summary in self.summaries()
            if summary["checks"][0]["status"] == "PASS"
        )
        check = success_summary["checks"][0]
        self.assertEqual(check["zones"], "prefilled_by_caller")
        self.assertNotIn("--refill-zones", check["command"])

    def test_prefilled_confirmation_is_not_needed_for_schematic(self):
        self.refill = False
        self.assertEqual(self.invoke("--schematic", str(self.sch)), 0)

    def test_invalid_timeout_is_rejected(self):
        with self.assertRaises(SystemExit) as exc:
            self.invoke("--pcb", str(self.pcb), "--timeout", "0")
        self.assertEqual(exc.exception.code, 2)

    def test_input_change_is_not_success(self):
        self.mutate = True
        self.assertEqual(self.invoke("--pcb", str(self.pcb)), 2)

    def test_unrelated_symbol_sheetfile_does_not_prevent_erc(self):
        shutil.copyfile(FIXTURES / "symbol-sheetfile.kicad_sch", self.sch)
        self.assertEqual(self.invoke("--schematic", str(self.sch)), 0)
        check = self.summaries()[0]["checks"][0]
        self.assertEqual(check["status"], "PASS")
        self.assertEqual(check["dependencies"], [str(self.sch)])
        self.assertEqual(len([c for c in self.calls if "--output" in c]), 1)

    def test_real_sheet_hierarchy_ignores_unrelated_and_quoted_properties(self):
        hierarchy = self.root / "hierarchy"
        shutil.copytree(FIXTURES / "hierarchy", hierarchy)
        source = hierarchy / "root.kicad_sch"
        self.assertEqual(self.invoke("--schematic", str(source)), 0)
        expected = {
            str(path.resolve()) for path in hierarchy.rglob("*.kicad_sch")
        }
        check = self.summaries()[0]["checks"][0]
        self.assertEqual(set(check["dependencies"]), expected)
        self.assertEqual(set(check["dependency_sha256"]), expected)
        self.assertEqual(check["status"], "PASS")

    def test_missing_real_child_is_not_checked(self):
        self.sch.write_text(
            '(kicad_sch (sheet (property "Sheetfile" "missing.kicad_sch")))',
            encoding="utf-8",
        )
        self.assertEqual(self.invoke("--schematic", str(self.sch)), 2)
        check = self.summaries()[0]["checks"][0]
        self.assertEqual(check["status"], "NOT_CHECKED")
        self.assertIn("Referenced sheet does not exist", check["error"])
        self.assertFalse(any("--output" in c for c in self.calls))

    def test_invalid_real_child_extension_is_not_checked(self):
        self.sch.write_text(
            '(kicad_sch (sheet (property "Sheetfile" "not-a-schematic.txt")))',
            encoding="utf-8",
        )
        self.assertEqual(self.invoke("--schematic", str(self.sch)), 2)
        self.assertIn("not a KiCad schematic", self.summaries()[0]["checks"][0]["error"])
        self.assertFalse(any("--output" in c for c in self.calls))

    def test_nested_child_change_is_not_success(self):
        hierarchy = self.root / "hierarchy"
        shutil.copytree(FIXTURES / "hierarchy", hierarchy)
        self.dependency_mutate_path = hierarchy / "grandchild.kicad_sch"
        self.assertEqual(self.invoke("--schematic", str(hierarchy / "root.kicad_sch")), 2)
        check = self.summaries()[0]["checks"][0]
        self.assertEqual(check["status"], "NOT_CHECKED")
        self.assertIn("Review dependency changed", check["error"])

    def test_shared_and_cyclic_dependencies_are_only_snapshotted_once(self):
        hierarchy = self.root / "hierarchy"
        shutil.copytree(FIXTURES / "hierarchy", hierarchy)
        (hierarchy / "grandchild.kicad_sch").write_text(
            '(kicad_sch (sheet (property "Sheetfile" "root.kicad_sch")))',
            encoding="utf-8",
        )
        paths = review.dependency_paths((hierarchy / "root.kicad_sch").resolve())
        self.assertEqual(len(paths), 3)
        self.assertEqual(len(set(paths)), 3)

    def test_hierarchical_and_rule_dependencies_are_snapshotted(self):
        child = self.root / "child.kicad_sch"
        project = self.sch.with_suffix(".kicad_pro")
        rules = self.sch.with_suffix(".kicad_dru")
        self.sch.write_text(
            '(kicad_sch (sheet (property "Sheetfile" "child.kicad_sch")))',
            encoding="utf-8",
        )
        child.write_text("(kicad_sch)", encoding="utf-8")
        project.write_text("project", encoding="utf-8")
        rules.write_text("rules", encoding="utf-8")

        self.assertEqual(self.invoke("--schematic", str(self.sch)), 0)
        check = self.summaries()[0]["checks"][0]
        expected = {str(path.resolve()) for path in (self.sch, child, project, rules)}
        self.assertEqual(set(check["dependencies"]), expected)
        self.assertEqual(set(check["dependency_sha256"]), expected)

    def test_change_to_any_hierarchical_dependency_is_not_success(self):
        child = self.root / "child.kicad_sch"
        project = self.sch.with_suffix(".kicad_pro")
        rules = self.sch.with_suffix(".kicad_dru")
        self.sch.write_text(
            '(kicad_sch (sheet (property "Sheetfile" "child.kicad_sch")))',
            encoding="utf-8",
        )
        child.write_text("(kicad_sch)", encoding="utf-8")
        project.write_text("project", encoding="utf-8")
        rules.write_text("rules", encoding="utf-8")

        for dependency in (child, project, rules):
            with self.subTest(dependency=dependency.name):
                self.dependency_mutate_path = dependency
                self.assertEqual(self.invoke("--schematic", str(self.sch)), 2)
                check = self.summaries()[-1]["checks"][0]
                self.assertIn("Review dependency", check["error"])


class SheetfileParsingTests(unittest.TestCase):
    def test_only_direct_properties_of_top_level_sheets_are_paths(self):
        self.assertEqual(review.schematic_sheetfiles('''(kicad_sch
          (property "Sheetfile" "root-field.kicad_sch")
          (symbol (sheet (property "Sheetfile" "nested-sheet.kicad_sch")))
          (sheet (pin "P" (property "Sheetfile" "pin-field.kicad_sch"))
            (property "Sheetfile" "real.kicad_sch")))'''), ["real.kicad_sch"])

    def test_quoted_paths_decode_escaped_quotes_and_backslashes_once(self):
        self.assertEqual(review.schematic_sheetfiles(
            r'(kicad_sch (sheet (property "Sheetfile" "folder\\child \"A\" (x).kicad_sch")))'
        ), ['folder\\child "A" (x).kicad_sch'])
        self.assertEqual(review.schematic_sheetfiles(
            r'(kicad_sch (sheet (property "Sheetfile" "folder\\\"child.kicad_sch")))'
        ), [r'folder\"child.kicad_sch'])

    def test_quoted_parentheses_and_escaped_backslash_cannot_create_sheets(self):
        self.assertEqual(review.schematic_sheetfiles(
            r'''(kicad_sch
              (text "(sheet (property \"Sheetfile\" \"fake.kicad_sch\"))")
              (text "backslash at end \\")
              (sheet (property "Sheetfile" "real.kicad_sch")))'''
        ), ["real.kicad_sch"])

    def test_malformed_expressions_are_not_silently_accepted(self):
        for text in (
            '', '(kicad_sch', '(kicad_sch))', '(kicad_sch (text "unterminated))',
            '(kicad_sch) (kicad_sch)', 'atom (kicad_sch)', '(kicad_sch ())',
            '(kicad_sch (sheet (property "Sheetfile")))',
            '(kicad_sch (sheet (property "Sheetfile" unquoted)))',
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                review.schematic_sheetfiles(text)

    def test_deep_nesting_does_not_require_python_recursion(self):
        nested = '(effects ' * 1500 + '"(parentheses)"' + ')' * 1500
        self.assertEqual(review.schematic_sheetfiles(
            '(kicad_sch (sheet ' + nested
            + '(property "Sheetfile" "real.kicad_sch")))'
        ), ["real.kicad_sch"])


if __name__ == "__main__":
    unittest.main()
