"""Build-script contract tests.

``build.ps1`` cannot be unit-tested like Python code, so the parts that matter
are pinned down statically and through the one helper it calls out to.  Three
properties are load-bearing:

* a failing step must abort the build -- verified live by the script itself
  through ``tools/check_build_gate.py``, and here through that helper's own
  contract, plus a regression guard against re-introducing the Windows
  PowerShell 5.1 ``$LASTEXITCODE`` pitfall that made the gate silently pass;
* the unit-test suite must stay opt-in (``-Test``), because running it on every
  packaging build costs minutes and the packaging steps do not depend on it --
  while a build that skipped it must still say so in its version report;
* the file must keep its UTF-8 BOM, otherwise Windows PowerShell reads it as
  GBK and the Chinese messages break the parser.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = PROJECT_ROOT / "build.ps1"
GATE_HELPER = PROJECT_ROOT / "tools" / "check_build_gate.py"


class GateHelperTests(unittest.TestCase):
    """The helper build.ps1 uses to prove it notices failures."""

    def run_helper(self, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(GATE_HELPER), *arguments],
            capture_output=True, text=True, cwd=PROJECT_ROOT, timeout=60,
        )

    def test_reports_the_requested_exit_code(self) -> None:
        for code in (0, 1, 7, 42):
            with self.subTest(code=code):
                self.assertEqual(self.run_helper("--exit-code", str(code)).returncode,
                                 code)

    def test_can_write_to_stderr(self) -> None:
        result = self.run_helper("--exit-code", "3", "--stderr", "--lines", "5")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(len([line for line in result.stderr.splitlines() if line]),
                         5)
        self.assertIn("stdout", result.stdout)

    def test_exit_code_is_clamped_to_a_process_range(self) -> None:
        self.assertEqual(self.run_helper("--exit-code", "300").returncode, 255)
        self.assertEqual(self.run_helper("--exit-code", "-4").returncode, 0)


class ScriptContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = BUILD_SCRIPT.read_bytes()
        cls.source = cls.raw.decode("utf-8-sig")

    def test_keeps_the_utf8_bom(self) -> None:
        """Windows PowerShell 5.1 reads a BOM-less script as GBK: keep the BOM."""
        self.assertTrue(self.raw.startswith(b"\xef\xbb\xbf"),
                        "build.ps1 丢失了 UTF-8 BOM")

    def test_is_valid_utf8_with_chinese_messages(self) -> None:
        self.assertIn("仅供测试学习用", self.source)

    def test_verifies_its_own_failure_detection(self) -> None:
        self.assertIn("Assert-ExitCodeDetection", self.source)
        self.assertIn("check_build_gate.py", self.source)

    def test_does_not_trust_last_exit_code_for_merged_output(self) -> None:
        """Regression guard: the 5.1 pipeline reported 0 for a failing child."""
        self.assertNotIn("2>&1 | ForEach-Object", self.source)
        self.assertIn("Start-Process", self.source)
        self.assertIn("$process.ExitCode", self.source)

    def test_every_python_step_checks_its_exit_code(self) -> None:
        self.assertIn("function Invoke-PythonStep", self.source)
        self.assertIn("$script:LastNativeExitCode -ne 0", self.source)

    def test_single_file_release_pipeline_is_intact(self) -> None:
        for marker in ("app-payload.zip", "PyInstaller", "Nioh3AccessoryEditor.spec",
                       "--distpath", "onefile"):
            self.assertIn(marker, self.source, marker)

    def test_smoke_test_extracts_and_reports_its_commit(self) -> None:
        self.assertIn("解压附属文件", self.source)
        self.assertIn("extracted-manifest.json", self.source)
        self.assertIn("data\\accessory_affixes.json", self.source)

    def test_release_must_not_contain_python_sources(self) -> None:
        self.assertIn("*.py", self.source)
        self.assertIn("发行目录出现 Python 源文件", self.source)

    def test_ships_only_the_executable_in_the_zip(self) -> None:
        self.assertIn("Compress-Archive -LiteralPath $script:ExePath", self.source)

    def test_declares_the_documented_parameters(self) -> None:
        for parameter in ("$Python", "$Configuration", "$OutputDirectory",
                          "$PyInstallerPython", "$Test", "$SkipTests",
                          "$TestPattern", "$SkipZip", "$PureCryptoTests",
                          "$Clean", "$Quiet"):
            self.assertIn(parameter, self.source, parameter)

    def test_unit_tests_are_opt_in(self) -> None:
        """A build must be fast by default: only -Test (or friends) runs them."""
        self.assertIn("$runTests = [bool]($Test -or $TestPattern -or $PureCryptoTests)",
                      self.source)
        self.assertIn("if ($runTests) {", self.source)
        self.assertIn("Invoke-PythonStep -Arguments $testArguments", self.source)
        # The legacy switch must stay accepted, but only as a documented no-op.
        self.assertIn("-SkipTests 现在是空操作", self.source)

    def test_reports_whether_the_tests_ran(self) -> None:
        """A build log must not be mistakable for a verified one."""
        self.assertIn("$script:TestsRan", self.source)
        self.assertIn("单元测试  : 已运行", self.source)
        self.assertIn("单元测试  : 未运行", self.source)

    def test_clean_only_removes_its_own_artifacts(self) -> None:
        self.assertIn("Nioh3AccessoryEditor*", self.source)

    def test_restores_the_callers_console_encoding(self) -> None:
        self.assertIn("$previousConsoleEncoding", self.source)
        self.assertIn("[Console]::OutputEncoding", self.source)

    def test_pip_index_is_overridable(self) -> None:
        """The default PyPI index stalls on some hosts; keep the escape hatch."""
        self.assertIn("NIOH3_PIP_INDEX", self.source)

    def test_no_stale_staging_code_remains(self) -> None:
        for marker in ("Copy-FilteredTree", "$stageItems", "$script:StagePath",
                       "excludeFilePatterns"):
            self.assertNotIn(marker, self.source, marker)

    def test_no_debug_leftovers(self) -> None:
        self.assertNotIn("gate-debug.log", self.source)


class SpecAndPayloadWiringTests(unittest.TestCase):
    def test_build_references_the_real_spec(self) -> None:
        spec = PROJECT_ROOT / "Nioh3AccessoryEditor.spec"
        self.assertTrue(spec.is_file())

    def test_payload_generator_is_referenced(self) -> None:
        self.assertTrue((PROJECT_ROOT / "tools" / "make_payload.py").is_file())
        source = BUILD_SCRIPT.read_text(encoding="utf-8-sig")
        self.assertIn("make_payload.py", source)
        self.assertIn("--verify", source)

    def test_icon_is_verified_before_it_is_packaged(self) -> None:
        """Stale art must fail the build, not silently ship."""
        self.assertTrue((PROJECT_ROOT / "tools" / "make_icon.py").is_file())
        source = BUILD_SCRIPT.read_text(encoding="utf-8-sig")
        self.assertIn("make_icon.py", source)
        self.assertIn("'--check'", source)

    def test_built_exe_icon_is_verified_in_the_pe_resources(self) -> None:
        self.assertTrue((PROJECT_ROOT / "tools" / "check_exe_icon.py").is_file())
        source = BUILD_SCRIPT.read_text(encoding="utf-8-sig")
        self.assertIn("check_exe_icon.py", source)
        self.assertIn("--against", source)
        self.assertIn("--expect", source)
        # All nine sizes must be demanded, 256 px included.
        self.assertIn("'16,20,24,32,40,48,64,128,256'", source)

    def test_smoke_test_checks_the_extracted_assets(self) -> None:
        source = BUILD_SCRIPT.read_text(encoding="utf-8-sig")
        for relative in ("assets\\app.ico", "assets\\logo-32.png"):
            self.assertIn(relative, source, relative)


if __name__ == "__main__":
    unittest.main()
