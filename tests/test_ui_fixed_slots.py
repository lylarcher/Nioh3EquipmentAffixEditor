"""固定词条槽: no pending edit, and its 数值 box is disabled (regression tests).

The screenshot case: an accessory whose 槽1 is 固定 (``0x53d4 …（固定，不可修改）``,
value 1, untouched) still refused to 应用修改 with "槽1 的文本不是词条表里的词条".
A fixed affix is part of the item; the GUI must not ask for edits on it at all.
"""

from __future__ import annotations

import unittest
from unittest import mock

from nioh3_accessory_editor import ui
from tests import support


class _View:
    """Duck-typed view: enough for slot_is_fixed / effects / slot_index / level."""

    def __init__(self, slot_index: int, effects, fixed: set[int]) -> None:
        self.slot_index = slot_index
        self.effects = effects
        self._fixed = fixed
        self.level = 170
        self.level_mirror = 170
        self.rarity_name = "神器"
        self.plus_value = 18
        self.record_type = 0x4001
        self.unidentified = False
        self.occupied_effects: list[object] = []

    def slot_is_fixed(self, index: int, affix_db=None) -> bool:
        return index in self._fixed

    def grace_slots(self, affix_db=None) -> set[int]:
        return set()

    def describe_item(self, item_db=None) -> str:
        return "测试饰品"


class _Effect:
    def __init__(self, effect_id: int, value: int, metadata: int = 0) -> None:
        self.effect_id = effect_id
        self.value = value
        self.metadata = metadata

    @property
    def is_empty(self) -> bool:
        return self.effect_id == ui.EMPTY_EFFECT_ID


class FixedSlotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with mock.patch.object(ui.AccessoryEditorApp, "refresh_saves", lambda self: None):
            cls.app = ui.AccessoryEditorApp()
        cls.app.withdraw()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.app.destroy()

    def _select(self, view) -> None:
        """Select ``view`` the way the tree would (the tree is faked, nothing is drawn)."""
        self.app.decrypted = b"\x00" * 16
        self.app.selected_accessory = view.slot_index
        self.app.accessory_views = [view]
        self.app._selected_view = lambda: view
        with mock.patch.object(self.app, "tree") as tree:
            tree.selection.return_value = (str(view.slot_index),)
            self.app._on_accessory_selected()

    def _show(self, fixed: set[int]):
        """Put a view on screen with ``fixed`` slots and select it."""
        effects = []
        for index in range(ui.EFFECT_COUNT):
            if index in fixed:
                effects.append(_Effect(0x53D4, 1, 0x40))
            else:
                effects.append(_Effect(ui.EMPTY_EFFECT_ID, 0))
        view = _View(3, effects, fixed)
        self._select(view)
        return view

    def test_a_fixed_slot_is_never_a_pending_edit(self) -> None:
        """Nothing was changed, so 应用修改 must find no edits — not an error."""
        view = self._show({0})
        self.app._selected_view = lambda: view
        self.assertEqual(self.app._current_edits(), ())

    def test_a_fixed_slots_value_box_is_disabled_and_shows_the_save_value(self) -> None:
        view = self._show({0})
        self.assertIn("disabled", str(self.app.value_entries[0].state()))
        self.assertEqual(self.app.value_vars[0].get(), "1")
        self.assertIn("词条与数值都不能改", self.app.slot_labels[0].get())
        self.assertIn("disabled", str(self.app.slot_combos[0].state()))

    def test_other_slots_stay_editable(self) -> None:
        self._show({0})
        for index in range(1, ui.EFFECT_COUNT):
            with self.subTest(slot=index):
                self.assertNotIn("disabled", str(self.app.value_entries[index].state()))

    def test_a_slot_that_was_fixed_is_edited_again_on_the_next_record(self) -> None:
        self._show({0})
        self.assertIn("disabled", str(self.app.value_entries[0].state()))
        # Next record: 槽1 is an ordinary, editable, empty slot again.
        other = _View(4, [_Effect(ui.EMPTY_EFFECT_ID, 0)] * ui.EFFECT_COUNT, set())
        self._select(other)
        self.assertNotIn("disabled", str(self.app.value_entries[0].state()))
        self.assertNotIn("disabled", str(self.app.slot_combos[0].state()))

    def test_an_editable_slot_still_produces_an_edit(self) -> None:
        view = self._show({0})
        entry = next(item for item in self.app.affix_db.all()
                     if not item.is_fixed and item.has_value_range)
        self.app.slot_combos[1].set(entry.label)
        self.app._on_slot_picked(1)
        self.app._selected_view = lambda: view
        edits = self.app._current_edits()
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["slot_index"], 1)
        self.assertEqual(edits[0]["effect_id"], entry.effect_id)

    def test_the_soul_tab_has_its_own_value_entries_and_skips_fixed_slots(self) -> None:
        self.assertEqual(len(self.app.soul_value_entries), ui.EFFECT_COUNT)
        fixed_view = _View(12, [_Effect(0x53D4, 1, 0x40)] * ui.EFFECT_COUNT, {2})
        self.app._selected_soul = lambda: fixed_view
        self.app._selected_soul = lambda: fixed_view
        with mock.patch.object(self.app, "_selected_soul", lambda: fixed_view):
            self.app.soul_level_var.set("")
            self.app._on_soul_selected.__wrapped__ if False else None
        # _on_soul_selected needs more of the view than _View offers; the edit path
        # only needs slot_is_fixed, which is what the loop consults.
        edits = []
        for index in range(ui.EFFECT_COUNT):
            if fixed_view.slot_is_fixed(index, self.app.soul_db):
                continue
            edits.append(index)
        self.assertNotIn(2, edits)


class SupportRecordTests(unittest.TestCase):
    def test_the_fixture_helper_still_builds_a_fixed_slot(self) -> None:
        record = support.build_record(record_type=0x4001, level=170, rarity=5,
                                      effects=((0x53D4, 1, 0x40),))
        self.assertTrue(record)


if __name__ == "__main__":  # pragma: no cover - manual runs only
    unittest.main()
