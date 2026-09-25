"""同一物品每个「种类」只能有一个词条（★+同名固定 例外，[其他] 不受限）。"""

from __future__ import annotations

import unittest
from unittest import mock

from nioh3_equipment_affix_editor import editor, ui
from nioh3_equipment_affix_editor.editor import EditorError
from tests import support

CODES = editor.load_affix_category_codes()
OTHER = CODES["其他"]


def _slots(pairs):
    from nioh3_equipment_affix_editor.records import EffectSlot

    return tuple(EffectSlot(slot_index=index, prefix=0, effect_id=effect_id,
                            value=0, metadata=metadata, tail_0=0, tail_1=0)
                 for index, (effect_id, metadata) in enumerate(pairs))


class CategoryRuleTests(unittest.TestCase):
    """直接对 assert_single_affix_per_category 做单元测试。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = support.load_catalog_affixes()

    def _plan(self, before, after):
        from nioh3_equipment_affix_editor.editor import EditPlan

        return EditPlan(record_index=3, offset=0, edits=(),
                        before=_slots(before), after=_slots(after))

    def _affix(self, category: str, *, star: bool = False) -> int:
        for entry in self.db.all():
            if entry.category == category and bool(entry.is_star) == star \
                    and not entry.is_fixed:
                return entry.effect_id
        raise AssertionError(f"目录里没有 {category}（star={star}）的词条")

    def test_two_affixes_of_one_category_are_refused(self) -> None:
        code = CODES["恢复体力"]
        first = self._affix("恢复体力")
        second = next(entry.effect_id for entry in self.db.all()
                      if entry.category == "恢复体力" and entry.effect_id != first)
        plan = self._plan([(first, code)], [(first, code), (second, code)])
        with self.assertRaises(EditorError) as caught:
            editor.assert_single_affix_per_category((plan,), self.db, CODES)
        message = str(caught.exception)
        self.assertIn("恢复体力", message)
        self.assertIn("每个种类只能有一个词条", message)

    def test_different_categories_are_fine(self) -> None:
        plan = self._plan([], [(self._affix("恢复体力"), CODES["恢复体力"]),
                               (self._affix("掉落"), CODES["掉落"])])
        editor.assert_single_affix_per_category((plan,), self.db, CODES)

    def test_other_may_repeat(self) -> None:
        ids = [entry.effect_id for entry in self.db.all()
               if entry.category == "其他" and not entry.is_fixed][:3]
        self.assertGreaterEqual(len(ids), 3)
        plan = self._plan([], [(effect_id, OTHER) for effect_id in ids])
        editor.assert_single_affix_per_category((plan,), self.db, CODES)

    def test_one_star_plus_the_fixed_affix_of_one_category_is_allowed(self) -> None:
        category = "恢复体力"
        star = next(entry.effect_id for entry in self.db.all()
                    if entry.category == category and entry.is_star)
        fixed = next(entry.effect_id for entry in self.db.all()
                     if entry.category == category and entry.is_fixed)
        plan = self._plan([], [(star, CODES[category]), (fixed, CODES[category])])
        editor.assert_single_affix_per_category((plan,), self.db, CODES)

    def test_star_plus_a_normal_affix_of_one_category_is_refused(self) -> None:
        category = "恢复体力"
        star = next(entry.effect_id for entry in self.db.all()
                    if entry.category == category and entry.is_star)
        normal = self._affix(category)
        plan = self._plan([], [(star, CODES[category]), (normal, CODES[category])])
        with self.assertRaises(EditorError):
            editor.assert_single_affix_per_category((plan,), self.db, CODES)

    def test_a_pre_existing_duplicate_is_not_blocked(self) -> None:
        """本来就重复的记录，不因为这个拦住无关的改动。"""
        code = CODES["恢复体力"]
        first = self._affix("恢复体力")
        second = next(entry.effect_id for entry in self.db.all()
                      if entry.category == "恢复体力" and entry.effect_id != first)
        plan = self._plan([(first, code), (second, code)],
                          [(first, code), (second, code)])
        editor.assert_single_affix_per_category((plan,), self.db, CODES)

    def test_empty_slots_never_count(self) -> None:
        plan = self._plan([], [(0, 0), (0, 0), (0, 0)])
        editor.assert_single_affix_per_category((plan,), self.db, CODES)

    def test_the_rule_is_wired_into_plan_edits(self) -> None:
        """端到端：把槽2 改成与槽1 同种类的词条会被 plan_edits 拒绝。"""
        category = "恢复体力"
        first_entry = next(entry for entry in self.db.all()
                           if entry.category == category and not entry.is_fixed
                           and not entry.is_star and entry.has_value_range)
        second_entry = next(entry for entry in self.db.all()
                            if entry.category == category
                            and entry.effect_id != first_entry.effect_id
                            and not entry.is_fixed and entry.has_value_range)
        save = support.build_plain_save(records_by_slot={
            3: support.build_record(record_type=0x4001, level=170, rarity=5,
                                    effects=((first_entry.effect_id, first_entry.value,
                                              CODES[category]),)),
        })
        edits = [{"record_index": 3, "slot_index": 1,
                  "effect_id": second_entry.effect_id, "value": second_entry.value}]
        with self.assertRaises(EditorError) as caught:
            editor.plan_edits(save, edits, affix_db=self.db)
        self.assertIn("每个种类只能有一个词条", str(caught.exception))


class SoulFixedValueBoxTests(unittest.TestCase):
    """魂核页签：固定词条的数值框必须一直是禁用的。"""

    @classmethod
    def setUpClass(cls) -> None:
        with mock.patch.object(ui.AccessoryEditorApp, "refresh_saves", lambda self: None):
            cls.app = ui.AccessoryEditorApp()
        cls.app.withdraw()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.app.destroy()

    def test_a_fixed_soul_slot_keeps_its_value_box_disabled(self) -> None:
        from tests.test_ui_fixed_slots import _Effect, _View

        entry = next(e for e in self.app.soul_db.all() if e.is_fixed)
        effects = [_Effect(entry.effect_id, entry.value, 0x4000)]
        effects += [_Effect(ui.EMPTY_EFFECT_ID, 0)] * (ui.EFFECT_COUNT - 1)
        view = _View(11, effects, {0})
        self.app.decrypted = b"\x00" * 16
        self.app.selected_soul = 11
        self.app.soul_views = [view]
        self.app._selected_soul = lambda: view
        with mock.patch.object(self.app, "soul_tree") as tree, \
                mock.patch.object(ui.messagebox, "showwarning"), \
                mock.patch.object(ui, "collect_kind_samples", return_value={}):
            tree.selection.return_value = ("11",)
            self.app._on_soul_selected()
            self.assertIn("disabled", str(self.app.soul_value_entries[0].state()))
            self.assertEqual(self.app.soul_value_vars[0].get(), str(entry.value))
            # 之前 _set_soul_controls(True) 会把它重新启用，这正是用户看到的问题
            self.app._set_soul_controls(True)
            self.assertIn("disabled", str(self.app.soul_value_entries[0].state()))
            self.assertNotIn("disabled", str(self.app.soul_value_entries[1].state()))


if __name__ == "__main__":  # pragma: no cover - manual runs only
    unittest.main()
