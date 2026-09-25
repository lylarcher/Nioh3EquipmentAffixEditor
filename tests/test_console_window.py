"""没有黑框：窗口子系统 + CLI 只在需要时借用父终端。"""

from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

from nioh3_equipment_affix_editor import console

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "dist" / "Nioh3EquipmentAffixEditor.exe"


class ConsoleHelperTests(unittest.TestCase):
    def test_not_frozen_means_no_op(self) -> None:
        """`python launch_editor.py` already has a console: nothing to attach."""
        self.assertFalse(console.attach_parent_console())

    def test_attach_is_best_effort_when_the_call_fails(self) -> None:
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "stdout", None), \
                mock.patch.object(sys, "stderr", None), \
                mock.patch("ctypes.WinDLL") as windll:
            windll.return_value.AttachConsole.return_value = 0
            self.assertFalse(console.attach_parent_console())

    def test_attach_never_raises(self) -> None:
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "stdout", None), \
                mock.patch.object(sys, "stderr", None), \
                mock.patch("ctypes.WinDLL", side_effect=OSError("boom")):
            self.assertFalse(console.attach_parent_console())

    def test_has_console_reports_the_streams(self) -> None:
        self.assertTrue(console.has_console())
        with mock.patch.object(sys, "stdout", None):
            self.assertFalse(console.has_console())


class EntryPointTests(unittest.TestCase):
    """The GUI path must never borrow a console (killing it must not kill the app)."""

    def setUp(self) -> None:
        import launch_editor

        self.entry = launch_editor

    def test_the_gui_path_never_attaches(self) -> None:
        with mock.patch.object(self.entry.bootstrap, "ensure_once"), \
                mock.patch.object(self.entry.console, "attach_parent_console") as attach, \
                mock.patch("nioh3_equipment_affix_editor.ui.main", return_value=0):
            self.assertEqual(self.entry.main([]), 0)
        attach.assert_not_called()

    def test_the_cli_path_attaches_before_printing(self) -> None:
        with mock.patch.object(self.entry.bootstrap, "ensure_once") as boot, \
                mock.patch.object(self.entry.console, "attach_parent_console") as attach, \
                mock.patch("nioh3_equipment_affix_editor.cli.main", return_value=0):
            self.assertEqual(self.entry.main(["version"]), 0)
        attach.assert_called_once()
        self.assertTrue(boot.called)


class WindowedSubsystemTests(unittest.TestCase):
    """The built exe must be a GUI-subsystem binary (no console window)."""

    def test_spec_is_windowed(self) -> None:
        source = (ROOT / "Nioh3EquipmentAffixEditor.spec").read_text(encoding="utf-8")
        self.assertIn("console=False", source)
        self.assertNotIn("console=True", source)

    @unittest.skipUnless(EXE.is_file(), "dist/Nioh3EquipmentAffixEditor.exe 尚未构建")
    def test_built_exe_is_a_gui_binary(self) -> None:
        data = EXE.read_bytes()[:0x400]
        pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
        # IMAGE_OPTIONAL_HEADER.Subsystem sits 0x5C into the optional header.
        subsystem = struct.unpack_from("<H", data, pe_offset + 0x5C)[0]
        self.assertEqual(subsystem, 2, "exe 仍是 console 子系统，会再出现黑框")


if __name__ == "__main__":  # pragma: no cover - manual runs only
    unittest.main()
