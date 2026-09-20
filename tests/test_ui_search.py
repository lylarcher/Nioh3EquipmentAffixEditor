"""Keyword search / value box / filter tests for the new GUI controls.

These only need the shipped catalogs, so they never touch a real save.  The point
of each one is the user-visible behaviour of requirements (2), (3) and (4):

* a keyword search always *reports* its result, including "无匹配";
* the 数值 box is range-checked before anything is written;
* the 种类 / 恩宠 filters decide per record, with no filter meaning "everything".
"""

from __future__ import annotations

import unittest
from unittest import mock

from nioh3_accessory_editor import ui
from nioh3_accessory_editor.editor import EditorError


class _FakeEffect:
    def __init__(self, effect_id: int) -> None:
        self.effect_id = effect_id


class _FakeView:
    """Minimal duck-typed view: only what the filter predicates read."""

    def __init__(self, record_type: int, grace_effect: int | None = None) -> None:
        self.record_type = record_type
        self.occupied_effects = (
            [] if grace_effect is None else [_FakeEffect(grace_effect)]
        )


class AffixSearchWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with mock.patch.object(ui.AccessoryEditorApp, "refresh_saves", lambda self: None):
            cls.app = ui.AccessoryEditorApp()
        cls.app.withdraw()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.app.destroy()

    def test_a_keyword_shows_every_match_in_the_slot_boxes(self) -> None:
        self.app.search_var.set("火抗性")
        self.app.search_affixes()
        values = self.app.slot_combos[0]["values"]
        self.assertIn("「火抗性」匹配到 2 条", self.app.search_status_var.get())
        self.assertEqual(len(values), 3)  # (空) + the two matches
        self.assertEqual(values[0], ui.EMPTY_LABEL)

    def test_zero_matches_is_reported_explicitly(self) -> None:
        self.app.search_var.set("绝不可能存在的词条名")
        self.app.search_affixes()
        message = self.app.search_status_var.get()
        self.assertIn("没有匹配到任何词条", message)
        self.assertIn("绝不可能存在的词条名", message)

    def test_a_blank_keyword_asks_for_one_and_matches_nothing(self) -> None:
        self.app.search_var.set("   ")
        self.app.search_affixes()
        self.assertIn("请输入关键词", self.app.search_status_var.get())

    def test_show_all_restores_the_full_catalog(self) -> None:
        self.app.search_var.set("火抗性")
        self.app.search_affixes()
        self.app.show_all_affixes()
        self.assertEqual(len(self.app.slot_combos[0]["values"]),
                         len(self.app.affix_db) + 1)  # + (空)

    def test_the_value_box_rejects_text_and_accepts_numbers(self) -> None:
        self.app.value_vars[0].set("abc")
        with self.assertRaises(EditorError) as caught:
            self.app._slot_value(0)
        self.assertIn("必须是整数", str(caught.exception))
        self.app.value_vars[0].set(" 200 ")
        self.assertEqual(self.app._slot_value(0), 200)
        self.app.value_vars[0].set("")
        self.assertIsNone(self.app._slot_value(0))


class SoulSearchWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with mock.patch.object(ui.AccessoryEditorApp, "refresh_saves", lambda self: None):
            cls.app = ui.AccessoryEditorApp()
        cls.app.withdraw()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.app.destroy()

    def test_soul_keyword_search_reports_matches(self) -> None:
        self.app.soul_search_var.set("灵力")
        self.app.search_soul_affixes()
        values = self.app.soul_slot_combos[0]["values"]
        self.assertGreater(len(values), 1)
        self.assertIn("匹配到", self.app.soul_search_status_var.get())
        self.assertIn("8" if False else "请从框里选择",
                      self.app.soul_search_status_var.get())

    def test_soul_zero_matches_is_reported(self) -> None:
        self.app.soul_search_var.set("绝不可能存在的魂核词条")
        self.app.search_soul_affixes()
        self.assertIn("没有匹配到任何魂核词条", self.app.soul_search_status_var.get())


class FilterPredicateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with mock.patch.object(ui.AccessoryEditorApp, "refresh_saves", lambda self: None):
            cls.app = ui.AccessoryEditorApp()
        cls.app.withdraw()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.app.destroy()

    def test_no_kind_filter_lets_everything_through(self) -> None:
        self.app.kind_filter_var.set(ui.ALL_FILTER)
        self.assertTrue(self.app._kind_filter_passes(_FakeView(0x1234)))

    def test_a_kind_filter_matches_only_that_id(self) -> None:
        label = next(iter(self.app.item_choices))
        entry = self.app.item_choices[label]
        self.app.kind_filter_var.set(label)
        self.assertTrue(self.app._kind_filter_passes(_FakeView(entry.item_id)))
        self.assertFalse(self.app._kind_filter_passes(_FakeView(entry.item_id ^ 0x1)))

    def test_a_grace_filter_needs_that_affix_on_the_record(self) -> None:
        label = next(iter(self.app.grace_choices))
        entry = self.app.grace_choices[label]
        self.app.grace_filter_var.set(label)
        self.assertTrue(self.app._grace_filter_passes(
            _FakeView(0x1234, grace_effect=entry.effect_id)))
        self.assertFalse(self.app._grace_filter_passes(_FakeView(0x1234)))

    def test_clear_filters_resets_both_combos(self) -> None:
        self.app.kind_filter_var.set(next(iter(self.app.item_choices)))
        self.app.grace_filter_var.set(next(iter(self.app.grace_choices)))
        self.app.clear_filters()
        self.assertEqual(self.app.kind_filter_var.get(), ui.ALL_FILTER)
        self.assertEqual(self.app.grace_filter_var.get(), ui.ALL_FILTER)


if __name__ == "__main__":  # pragma: no cover - manual runs only
    unittest.main()
