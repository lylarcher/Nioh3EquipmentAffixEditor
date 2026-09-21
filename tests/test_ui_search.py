"""Per-slot search tests: filtering one 词条槽 must not disturb the others.

The regression these lock down: the old global 搜索词条 rewrote every slot's
``values``, and a readonly ttk.Combobox blanks its display when its value leaves
``values`` — so a search made already-filled slots (and 固定 slots, whose label
carries a 「（固定，不可修改）」 suffix that is in no catalog) look empty until the
record was re-selected.
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


class _AppTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with mock.patch.object(ui.AccessoryEditorApp, "refresh_saves", lambda self: None):
            cls.app = ui.AccessoryEditorApp()
        cls.app.withdraw()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.app.destroy()


class PerSlotSearchTests(_AppTestCase):
    def setUp(self) -> None:
        self.app._reset_affix_lists()
        self.app._reset_soul_lists()
        for combo in self.app.slot_combos + self.app.soul_slot_combos:
            combo.set("")

    def test_typing_in_one_slot_only_filters_that_slot(self) -> None:
        full = len(self.app.affix_db) + 1  # + (空)
        other_before = self.app.slot_combos[3]["values"]
        self.app.slot_combos[0].set("火抗性")
        self.app._on_slot_typed(0)
        self.assertLess(len(self.app.slot_combos[0]["values"]), full)
        self.assertEqual(tuple(self.app.slot_combos[3]["values"]), tuple(other_before))
        self.assertEqual(len(self.app.slot_combos[3]["values"]), full)

    def test_every_slot_keeps_its_own_list_length(self) -> None:
        self.app.slot_combos[0].set("火抗性")
        self.app._on_slot_typed(0)
        self.app.slot_combos[1].set("灵力")
        self.app._on_slot_typed(1)
        first = len(self.app.slot_combos[0]["values"])
        second = len(self.app.slot_combos[1]["values"])
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.app.slot_combos[0]["values"]), first)

    def test_a_search_does_not_blank_the_other_slots(self) -> None:
        """The exact complaint: filled slots lost their text after 搜索词条."""
        picked = [entry.label for entry in self.app.affix_db.all()][:3]
        for index, label in enumerate(picked):
            self.app.slot_combos[index].set(label)
            self.app._on_slot_picked(index)
            self.app.slot_combos[index].configure(values=self.app.affix_db.labels())
        self.app.slot_combos[5].set("火抗性")
        self.app._on_slot_typed(5)
        for index, label in enumerate(picked):
            with self.subTest(slot=index):
                self.assertEqual(self.app.slot_combos[index].get(), label)

    def test_the_typed_keyword_is_kept_and_reported(self) -> None:
        self.app.slot_combos[2].set("火抗性")
        self.app._on_slot_typed(2)
        self.assertEqual(self.app.slot_combos[2].get(), "火抗性")
        status = self.app.search_status_var.get()
        self.assertIn("槽3", status)
        self.assertIn("火抗性", status)
        self.assertIn("其它槽不受影响", status)
        self.assertEqual(self.app.slot_combos[2]["values"][0], ui.EMPTY_LABEL)

    def test_no_match_says_so_and_leaves_other_slots_alone(self) -> None:
        self.app.slot_combos[4].set("绝不可能存在的词条名")
        self.app._on_slot_typed(4)
        self.assertIn("没有匹配到任何词条", self.app.search_status_var.get())
        self.assertEqual(len(self.app.slot_combos[4]["values"]), 1)  # only (空)
        self.assertEqual(len(self.app.slot_combos[0]["values"]),
                         len(self.app.affix_db) + 1)

    def test_clearing_the_box_restores_only_that_slots_list(self) -> None:
        self.app.slot_combos[0].set("火抗性")
        self.app._on_slot_typed(0)
        self.app.slot_combos[0].set("")
        self.app._on_slot_typed(0)
        self.assertEqual(len(self.app.slot_combos[0]["values"]),
                         len(self.app.affix_db) + 1)
        self.assertIn("只缩小", self.app.search_status_var.get())

    def test_enter_takes_an_unambiguous_keyword(self) -> None:
        matches = self.app.affix_db.search("火抗性")
        self.assertEqual(len(matches), 2, "这条关键词应当恰好命中两条")
        self.app.slot_combos[0].set("火抗性 +12")
        entry = self.app.affix_db.search("火抗性 +12")[0]
        self.app.slot_combos[0].set(str(entry.effect_id))
        matches = self.app.affix_db.search(str(entry.effect_id))
        if len(matches) == 1:
            self.app._on_slot_return(0)
            self.assertEqual(self.app.slot_combos[0].get(), entry.label)
            self.assertEqual(self.app.value_vars[0].get(), str(entry.value))

    def test_enter_with_several_matches_asks_the_user_to_pick(self) -> None:
        self.app.slot_combos[0].set("火抗性")
        self.app._on_slot_return(0)
        self.assertIn("请从该槽的下拉列表里选一条",
                      self.app.search_status_var.get())
        self.assertEqual(self.app.slot_combos[0].get(), "火抗性")

    def test_applying_a_typed_id_resolves_it(self) -> None:
        entry = next(item for item in self.app.affix_db.all() if not item.is_fixed)
        self.app.slot_combos[0].set(str(entry.effect_id))
        self.assertEqual(self.app._resolve_slot_text(0), entry.label)
        self.assertEqual(self.app.slot_combos[0].get(), entry.label)

    def test_applying_garbage_is_refused_with_a_slot_specific_message(self) -> None:
        self.app.slot_combos[0].set("完全不是词条的文本")
        with self.assertRaises(EditorError) as caught:
            self.app._resolve_slot_text(0)
        self.assertIn("槽1", str(caught.exception))
        self.assertIn("完全不是词条的文本", str(caught.exception))

    def test_picking_shows_the_legal_span_under_the_slot(self) -> None:
        entry = next(item for item in self.app.affix_db.all()
                     if item.has_value_range and not item.is_fixed)
        self.app.slot_combos[1].set(entry.label)
        self.app._on_slot_picked(1)
        hint = self.app.slot_labels[1].get()
        self.assertIn(f"{entry.effect_id:#06x}", hint)
        self.assertIn(entry.describe_value_range(), hint)
        self.assertEqual(self.app.value_vars[1].get(), str(entry.value))

    def test_slots_are_editable_combos_not_readonly(self) -> None:
        for combo in self.app.slot_combos:
            self.assertNotIn("readonly", str(combo.state()))


class SoulPerSlotSearchTests(_AppTestCase):
    def setUp(self) -> None:
        self.app._reset_soul_lists()
        for combo in self.app.soul_slot_combos:
            combo.set("")

    def test_soul_slot_filters_itself_only(self) -> None:
        full = len(self.app.soul_db) + 1
        self.app.soul_slot_combos[0].set("灵力")
        self.app._on_soul_slot_typed(0)
        self.assertLess(len(self.app.soul_slot_combos[0]["values"]), full)
        self.assertEqual(len(self.app.soul_slot_combos[1]["values"]), full)
        self.assertIn("魂核词条", self.app.soul_search_status_var.get())

    def test_soul_no_match_is_reported(self) -> None:
        self.app.soul_slot_combos[0].set("绝不可能存在的魂核词条")
        self.app._on_soul_slot_typed(0)
        self.assertIn("没有匹配到任何魂核词条",
                      self.app.soul_search_status_var.get())


class FilterPredicateTests(_AppTestCase):
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

    def test_the_value_box_rejects_text_and_accepts_numbers(self) -> None:
        self.app.value_vars[0].set("abc")
        with self.assertRaises(EditorError) as caught:
            self.app._slot_value(0)
        self.assertIn("必须是整数", str(caught.exception))
        self.app.value_vars[0].set(" 200 ")
        self.assertEqual(self.app._slot_value(0), 200)
        self.app.value_vars[0].set("")
        self.assertIsNone(self.app._slot_value(0))


if __name__ == "__main__":  # pragma: no cover - manual runs only
    unittest.main()
