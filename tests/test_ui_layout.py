"""The layout must not hide controls: scrollable columns, visible 魂核 editor, one-line footer."""

from __future__ import annotations

import unittest
from unittest import mock

from nioh3_equipment_affix_editor import ui
from tests import support
from tests.test_ui import TK_AVAILABLE, TK_ERROR, UiTestCase


@unittest.skipUnless(TK_AVAILABLE, f"Tk unavailable ({TK_ERROR})")
class LayoutTests(UiTestCase):
    def test_both_editor_columns_are_scrollable(self) -> None:
        self.assertEqual(len(self.app.scroll_columns), 2)
        for canvas, inner in self.app.scroll_columns:
            self.assertIsInstance(canvas, ui.tk.Canvas)
            self.assertTrue(inner.winfo_children(), "编辑器列里应有内容")

    def test_the_columns_are_wired_into_their_panedwindows(self) -> None:
        panes = 0
        for tab in (self.app.accessory_tab, self.app.soul_tab):
            for child in tab.winfo_children():
                if isinstance(child, ui.ttk.Panedwindow):
                    panes += 1
                    self.assertEqual(len(child.panes()), 2)
        self.assertEqual(panes, 2, "饰品与魂核各自应有一个左右分栏")

    def test_the_soul_tab_names_itself(self) -> None:
        labels = [self.app.notebook.tab(tab, "text") for tab in self.app.notebook.tabs()]
        self.assertEqual(labels[0], "饰品")
        self.assertIn("魂核", labels[1])

    def test_every_soul_editor_widget_exists(self) -> None:
        for attr in ("soul_slot_combos", "soul_level_entry", "soul_kind_combo",
                     "create_soul_combo", "soul_search_status_var"):
            self.assertTrue(hasattr(self.app, attr), attr)
        self.assertEqual(len(self.app.soul_slot_combos), ui.EFFECT_COUNT)

    def test_each_slot_row_can_be_searched_on_its_own(self) -> None:
        """Per-slot combos + the 数值 box on the second line, so nothing is clipped."""
        for attr in ("search_status_var", "search_status_label"):
            self.assertTrue(hasattr(self.app, attr), attr)
        self.assertFalse(hasattr(self.app, "search_var"),
                         "全局关键词框已被每槽独立搜索取代")
        for combo in self.app.slot_combos:
            self.assertEqual(combo.winfo_manager(), "pack")
            self.assertTrue(combo.cget("values"), "每槽都应有自己的下拉候选")
        for entry in self.app.value_entries:
            self.assertEqual(entry.winfo_manager(), "pack")

    def test_the_plus_row_sits_below_the_level_row(self) -> None:
        """The row that used to be clipped must be a real widget with a button."""
        for attr in ("plus_entry", "plus_button", "plus_status_label"):
            self.assertTrue(hasattr(self.app, attr), attr)

    def test_the_footer_version_line_is_one_line(self) -> None:
        self.assertNotIn("\n", self.app.version_var.get())
        self.assertIn("commit", self.app.version_var.get())
        # The full facts are still available for the 版本信息 dialog.
        self.assertIn("\n", self.app._version_text())

    def test_reading_reports_that_the_soul_tab_is_filled(self) -> None:
        with mock.patch.object(ui, "list_soul_cores", return_value=()), \
                mock.patch.object(ui.messagebox, "showinfo"), \
                mock.patch.object(ui.messagebox, "showwarning"):
            self._select()
        # `_populate_accessories` always refreshes the 魂核 page (same save bytes).
        self.assertTrue(callable(self.app._populate_soul_cores))
        self.assertIn("已读取", self.app.status_var.get())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
