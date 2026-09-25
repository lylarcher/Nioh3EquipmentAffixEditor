"""无中生有自成一块（与编辑区隔离），且武器 / 防具的无中生有可设稀有度。"""

from __future__ import annotations

import unittest
from unittest import mock

from nioh3_equipment_affix_editor import limits, ui
from tests.test_ui import TK_AVAILABLE, TK_ERROR, UiTestCase


@unittest.skipUnless(TK_AVAILABLE, f"Tk unavailable ({TK_ERROR})")
class CreateBoxIsolationTests(UiTestCase):
    """用户要求：无中生有不要和右侧的词条/等级/+值/稀有度编辑放一起。"""

    def test_the_equipment_create_box_is_a_sibling_of_the_editor_column(self) -> None:
        for tab in self.app.equipment_tabs:
            frame = tab.create_frame
            with self.subTest(tab.big):
                self.assertIs(frame.master, tab,
                              "无中生有应挂在页签本身，而不是右侧编辑列")
                self.assertEqual(frame.pack_info().get("side"), "top")
                for child in tab.winfo_children():
                    if isinstance(child, ui.ttk.Panedwindow):
                        self.assertNotIn(frame, child.winfo_children(),
                                         "无中生有不应落在左右分栏里面")

    def test_a_separator_sits_above_the_equipment_create_box(self) -> None:
        for tab in self.app.equipment_tabs:
            children = list(tab.winfo_children())
            separators = [child for child in children
                          if isinstance(child, ui.ttk.Separator)]
            with self.subTest(tab.big):
                self.assertTrue(separators, "无中生有上方应有分隔线")
                self.assertLess(children.index(separators[0]),
                                children.index(tab.create_frame),
                                "分隔线应在无中生有之上")

    def test_the_accessory_and_soul_boxes_are_isolated_too(self) -> None:
        self.assertIs(self.app.create_frame.master, self.app.accessory_tab)
        soul_frames = [child for child in self.app.soul_tab.winfo_children()
                       if isinstance(child, ui.ttk.LabelFrame)
                       and "无中生有" in str(child.cget("text"))]
        self.assertTrue(soul_frames, "魂核页签应有独立的无中生有框")

    def test_the_box_says_it_is_not_the_apply_button(self) -> None:
        """框内说明要点明【应用修改】只作用于已有记录，避免误触。"""
        for tab in self.app.equipment_tabs:
            with self.subTest(tab.big):
                self.assertIn("应用修改", tab.create_status_var.get())


@unittest.skipUnless(TK_AVAILABLE, f"Tk unavailable ({TK_ERROR})")
class CreateRarityInputTests(UiTestCase):
    """武器 / 防具的无中生有补上稀有度输入，取值受大类上限约束。"""

    def test_the_choices_stop_at_the_class_cap(self) -> None:
        for tab in self.app.equipment_tabs:
            cap = limits.rarity_cap(tab.big)
            values = tuple(str(value) for value in tab.create_rarity_combo.cget("values"))
            with self.subTest(tab.big):
                self.assertEqual(values, ("",) + tuple(str(v) for v in range(cap + 1)))
                self.assertEqual(tab.create_rarity_var.get(), "",
                                 "默认留空 = 沿用模板")

    def _run_create(self, tab, rarity_text: str) -> dict:
        """调 create_item，只到 plan 那一层就被拦下，返回调用参数。"""
        tab.create_rarity_var.set(rarity_text)
        tab.create_kind_combo.set(next(iter(tab.kind_choices)))
        with mock.patch.object(self.app, "decrypted", b"\x00" * 16, create=True), \
                mock.patch.object(ui, "plan_create_equipment",
                                  side_effect=ui.CreationError("测试到此为止")) as plan, \
                mock.patch.object(ui.messagebox, "showwarning"), \
                mock.patch.object(ui.messagebox, "showerror"):
            tab.create_item()
        if not plan.call_args_list:
            self.fail("create_item 没有走到 plan_create_equipment")
        return plan.call_args_list[0].kwargs

    def test_the_chosen_rarity_reaches_the_plan(self) -> None:
        tab = self.app.equipment_tabs[0]
        kwargs = self._run_create(tab, "3")
        self.assertEqual(kwargs.get("rarity"), 3)

    def test_leaving_it_empty_means_follow_the_template(self) -> None:
        tab = self.app.equipment_tabs[0]
        kwargs = self._run_create(tab, "")
        self.assertIsNone(kwargs.get("rarity"))

    def test_the_apply_button_does_not_create_items(self) -> None:
        """【应用修改】与无中生有是两条路：没选记录时它只提示，不会新建。"""
        tab = self.app.equipment_tabs[0]
        with mock.patch.object(ui, "plan_create_equipment") as plan, \
                mock.patch.object(ui.messagebox, "showwarning") as warn:
            tab.apply_all()
        plan.assert_not_called()
        self.assertTrue(warn.called, "没选记录时应给出提示")


if __name__ == "__main__":  # pragma: no cover - manual runs only
    unittest.main()
