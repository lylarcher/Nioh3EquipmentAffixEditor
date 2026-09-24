"""一件物品最多一个恩宠/套装词条（引擎 + GUI）。"""

from __future__ import annotations

import unittest
from unittest import mock

from nioh3_accessory_editor import editor, ui
from nioh3_accessory_editor.editor import EditorError
from tests import support

GRACE_DB = support.load_grace_table()
GRACES = list(GRACE_DB.all())


def _effects(*ids: int):
    return tuple((effect_id, 0, 0) for effect_id in ids)


class SingleGraceRuleTests(unittest.TestCase):
    """The engine refuses a plan that would leave two 恩宠/套装 affixes."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = support.load_catalog_affixes()

    def _plan(self, effects, edits):
        save = support.build_plain_save(
            records_by_slot={3: support.build_record(record_type=0x4001, level=170,
                                                     rarity=5, effects=effects)})
        return save, edits

    @staticmethod
    def _slots(ids):
        from nioh3_accessory_editor.records import EffectSlot

        return tuple(EffectSlot(slot_index=index, prefix=0, effect_id=effect_id,
                                value=0, metadata=0, tail_0=0, tail_1=0)
                     for index, effect_id in enumerate(ids))

    def _plan_with(self, before_ids, after_ids):
        from nioh3_accessory_editor.editor import EditPlan

        return EditPlan(record_index=3, offset=0, edits=(),
                        before=self._slots(before_ids), after=self._slots(after_ids))

    def test_a_second_grace_is_refused(self) -> None:
        first, second = GRACES[0].effect_id, GRACES[1].effect_id
        plan = self._plan_with([first], [first, second])
        with self.assertRaises(EditorError) as caught:
            editor.assert_single_grace((plan,), GRACE_DB)
        message = str(caught.exception)
        self.assertIn("只能有一个恩宠/套装词条", message)
        self.assertIn("记录 #3", message)

    def test_one_grace_is_fine(self) -> None:
        first = GRACES[0].effect_id
        editor.assert_single_grace((self._plan_with([], [first]),), GRACE_DB)

    def test_a_plan_without_graces_is_fine(self) -> None:
        editor.assert_single_grace((self._plan_with([0x1234], [0x1234, 0x5678]),),
                                   GRACE_DB)

    def test_a_real_slot_edit_cannot_introduce_a_grace_at_all(self) -> None:
        """The catalog gate already refuses a grace id in a slot edit."""
        grace = GRACES[0]
        save = support.build_plain_save(
            records_by_slot={3: support.build_record(record_type=0x4001, level=170,
                                                     rarity=5)})
        edits = [{"record_index": 3, "slot_index": 1,
                  "effect_id": grace.effect_id, "value": 0}]
        with self.assertRaises(Exception) as caught:
            editor.plan_edits(save, edits, affix_db=self.db, grace_db=GRACE_DB)
        self.assertIn("不在合法的饰品词条表中", str(caught.exception))

    def test_a_record_that_already_has_two_is_not_blocked_from_another_edit(self) -> None:
        """Only *newly introduced* pairs are refused."""
        first, second = GRACES[0], GRACES[1]
        normal = next(entry for entry in self.db.all()
                      if not entry.is_fixed and entry.has_value_range
                      and not GRACE_DB.describe(entry.effect_id))
        other = next(entry for entry in self.db.all()
                     if not entry.is_fixed and entry.has_value_range
                     and entry.effect_id != normal.effect_id
                     and not GRACE_DB.describe(entry.effect_id))
        effects = ((first.effect_id, 0, 0), (second.effect_id, 0, 0),
                   (normal.effect_id, normal.value, 0),
                   (0, 0, 0), (0, 0, 0), (0, 0, 0), (0, 0, 0))
        save, _ = self._plan(effects, None)
        edits = [{"record_index": 3, "slot_index": 2, "effect_id": other.effect_id,
                  "value": other.value}]
        plans = editor.plan_edits(save, edits, affix_db=self.db, grace_db=GRACE_DB)
        self.assertEqual(len(plans), 1)

    def test_a_single_grace_stays_allowed(self) -> None:
        first = GRACES[0]
        normal = next(entry for entry in self.db.all()
                      if not entry.is_fixed and entry.has_value_range
                      and not GRACE_DB.describe(entry.effect_id))
        other = next(entry for entry in self.db.all()
                     if not entry.is_fixed and entry.has_value_range
                     and entry.effect_id != normal.effect_id)
        effects = ((first.effect_id, 0, 0), (normal.effect_id, normal.value, 0),
                   (0, 0, 0), (0, 0, 0), (0, 0, 0), (0, 0, 0), (0, 0, 0))
        save, _ = self._plan(effects, None)
        edits = [{"record_index": 3, "slot_index": 1, "effect_id": other.effect_id,
                  "value": other.value}]
        plans = editor.plan_edits(save, edits, affix_db=self.db, grace_db=GRACE_DB)
        self.assertEqual(len(plans), 1)


class UnknownNonGraceSlotTests(unittest.TestCase):
    """0x30fe（表外、不在恩宠表里）应可在槽内直接改，而不是被当成恩宠槽禁用。"""

    @classmethod
    def setUpClass(cls) -> None:
        with mock.patch.object(ui.AccessoryEditorApp, "refresh_saves", lambda self: None):
            cls.app = ui.AccessoryEditorApp()
        cls.app.withdraw()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.app.destroy()

    def test_an_unknown_non_grace_slot_is_editable(self) -> None:
        from tests.test_ui_fixed_slots import _Effect, _View

        effects = [_Effect(0x9696, 80, 0), _Effect(0x30FE, 2, 0)]
        effects += [_Effect(ui.EMPTY_EFFECT_ID, 0)] * (ui.EFFECT_COUNT - 2)
        view = _View(9, effects, set(), grace={1})
        self.app.decrypted = b"\x00" * 16
        self.app.selected_accessory = 9
        self.app.accessory_views = [view]
        self.app._selected_view = lambda: view
        with mock.patch.object(self.app, "tree") as tree, \
                mock.patch.object(ui.messagebox, "showwarning"):
            tree.selection.return_value = ("9",)
            self.app._on_accessory_selected()
        # 0x30fe is not in 词条总目录, so it is *not* treated as a grace slot...
        self.assertEqual(self.app._grace_slot_indexes(view), frozenset())
        self.assertNotIn("disabled", str(self.app.slot_combos[1].state()))
        self.assertNotIn("disabled", str(self.app.value_entries[1].state()))

    def test_a_real_grace_slot_is_still_disabled(self) -> None:
        from tests.test_ui_fixed_slots import _Effect, _View

        grace = GRACES[0]
        effects = [_Effect(0x9696, 80, 0), _Effect(grace.effect_id, 0, 0)]
        effects += [_Effect(ui.EMPTY_EFFECT_ID, 0)] * (ui.EFFECT_COUNT - 2)
        view = _View(9, effects, set(), grace={1})
        self.app.decrypted = b"\x00" * 16
        self.app.selected_accessory = 9
        self.app.accessory_views = [view]
        self.app._selected_view = lambda: view
        with mock.patch.object(self.app, "tree") as tree, \
                mock.patch.object(ui.messagebox, "showwarning"):
            tree.selection.return_value = ("9",)
            self.app._on_accessory_selected()
        self.assertEqual(self.app._grace_slot_indexes(view), frozenset({1}))
        self.assertIn("disabled", str(self.app.slot_combos[1].state()))
        shown = self.app.slot_combos[1].get()
        if grace.category in ("武士套装", "忍者套装"):
            # 用户规则：套装槽锁死，文案要说明"不可替换"，不是"用恩宠栏替换"。
            self.assertIn("不可替换", shown)
            self.assertNotIn("固定，不可修改", shown)
        else:
            self.assertIn("用下方【恩宠】栏替换", shown)


if __name__ == "__main__":  # pragma: no cover - manual runs only
    unittest.main()
