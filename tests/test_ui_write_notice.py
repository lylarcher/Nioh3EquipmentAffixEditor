"""Tests for how the write requirement is surfaced in the GUI.

The rule itself (the game must be closed, or parked at the title screen) lives
in ``savefile.SAVE_WRITE_REQUIREMENT``; these tests pin the two places the user
actually sees it: the always-visible notice and the confirmation dialog.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nioh3_equipment_affix_editor import ui
from nioh3_equipment_affix_editor.savefile import SAVE_WRITE_REQUIREMENT
from tests import support

try:  # pragma: no cover - environment dependent
    import tkinter

    _probe = tkinter.Tk()
    _probe.destroy()
    TK_AVAILABLE = True
    TK_ERROR = ""
except Exception as error:  # pragma: no cover - environment dependent
    TK_AVAILABLE = False
    TK_ERROR = f"{type(error).__name__}: {error}"


@unittest.skipUnless(TK_AVAILABLE, f"Tk unavailable ({TK_ERROR})")
class WriteRequirementTests(unittest.TestCase):
    def setUp(self) -> None:
        support.silence_dialogs(self)
        with mock.patch.object(ui.AccessoryEditorApp, "refresh_saves",
                               lambda self: None):
            self.app = ui.AccessoryEditorApp()
        self.app.withdraw()
        self.addCleanup(self.app.destroy)
        self._temp = tempfile.TemporaryDirectory(prefix="nioh3-ui-notice-")
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)

    def _load(self) -> None:
        """Populate the window so 写入存档 has something to write."""
        from nioh3_equipment_affix_editor import records
        from nioh3_equipment_affix_editor.editor import SaveDescriptor, list_accessories

        plain = support.build_plain_save(
            records_by_slot={3: support.build_record(record_type=0x4001)}
        )
        self.app._populate_saves((SaveDescriptor(
            self.root / "SAVEDATA.BIN", 1234, 0, support.USER_SAVE_SIZE),))
        self.app._populate_accessories(
            (plain, list_accessories(plain), True, records.locate_layout(plain))
        )

    def test_footer_always_shows_the_requirement(self) -> None:
        text = self.app.write_requirement_var.get()
        self.assertIn("退出游戏", text)
        self.assertIn("标题界面", text)
        self.assertIn("覆盖", text)

    def test_widget_exists_and_is_visible(self) -> None:
        labels = [child for child in self.app.winfo_children()
                  if child.winfo_class() == "TLabel"]
        texts = [child.cget("text") for child in labels]
        self.assertIn(ui.WRITE_REQUIREMENT_SHORT, texts)

    def test_confirmation_dialog_states_the_requirement(self) -> None:
        self._load()
        seen: list[str] = []

        def fake_askyesno(title, message, **_kwargs):
            seen.append(f"{title}\n{message}")
            return False  # decline: nothing is written

        with mock.patch.object(ui, "running_game_processes", return_value=()), \
                mock.patch.object(ui.messagebox, "askyesno", fake_askyesno):
            self.app.write_save()

        self.assertEqual(len(seen), 1, "未弹出确认对话框")
        dialog = seen[0]
        self.assertIn(SAVE_WRITE_REQUIREMENT, dialog)
        self.assertIn("退出游戏", dialog)
        self.assertIn("标题界面", dialog)

    def test_running_game_is_refused_without_the_title_screen_tick(self) -> None:
        self._load()
        warned: list[str] = []

        def fake_showwarning(title, message, **_kwargs):
            warned.append(f"{title}\n{message}")

        with mock.patch.object(ui, "running_game_processes",
                               return_value=("Nioh3.exe",)), \
                mock.patch.object(ui.messagebox, "showwarning", fake_showwarning), \
                mock.patch.object(ui.messagebox, "askyesno") as asked:
            self.app.write_save()

        self.assertEqual(len(warned), 1, "检测到游戏运行时应先拒绝并说明")
        self.assertIn("Nioh3.exe", warned[0])
        self.assertIn("标题界面", warned[0])
        self.assertIn("勾选", warned[0])
        asked.assert_not_called()

    def test_the_ticked_box_keeps_the_write_confirmation_warning(self) -> None:
        self._load()
        self.app.title_screen_var.set(True)
        seen: list[str] = []

        def fake_askyesno(title, message, **_kwargs):
            seen.append(f"{title}\n{message}")
            return False  # decline: nothing is written

        with mock.patch.object(ui, "running_game_processes",
                               return_value=("Nioh3.exe",)), \
                mock.patch.object(ui.messagebox, "askyesno", fake_askyesno):
            self.app.write_save()

        self.assertEqual(len(seen), 1, "勾选确认后仍应弹出写入确认对话框")
        self.assertIn("标题界面", seen[0])
        self.assertIn(SAVE_WRITE_REQUIREMENT, seen[0])

    def test_declining_writes_nothing(self) -> None:
        self._load()
        target = self.root / "SAVEDATA.BIN"
        target.write_bytes(b"untouched")
        with mock.patch.object(ui, "running_game_processes", return_value=()), \
                mock.patch.object(ui.messagebox, "askyesno", lambda *a, **k: False):
            self.app.write_save()
        self.assertEqual(target.read_bytes(), b"untouched")


if __name__ == "__main__":
    unittest.main()
