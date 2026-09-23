"""同名固定判定的口径：目录标记 或 存档 metadata 的固定位（0x4000）。"""

from __future__ import annotations

import unittest

from nioh3_accessory_editor import editor
from nioh3_accessory_editor.affixdb import AffixDb, GraceDb
from nioh3_accessory_editor.editor import EditorError, SaveDescriptor
from tests import support

FIXED_BIT = 0x4000


def _view_with(effects):
    """真实 AccessoryView：造一个合成存档并列出记录（slot_is_fixed 是它的方法）。"""
    from nioh3_accessory_editor import records

    save = support.build_plain_save(records_by_slot={
        3: support.build_record(record_type=0x4001, level=170, rarity=5,
                                effects=effects),
    })
    layout = records.locate_layout(save)
    return list(editor.list_accessories(save, layout=layout))[0]


class MetaFixedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = support.load_catalog_affixes()
        cls.grace = GraceDb.best_effort()
        cls.grace_id = next(entry.effect_id for entry in cls.grace.all())

    def _view(self, effect, **kwargs):
        return _View([effect], **kwargs)

    def test_a_catalogued_fixed_affix_is_fixed(self) -> None:
        entry = next(e for e in self.db.all() if e.is_fixed)
        view = _view_with(((entry.effect_id, entry.value, 0),))
        self.assertTrue(view.slot_is_fixed(0, self.db, grace_db=self.grace))

    def test_an_out_of_catalog_id_with_the_fixed_bit_is_fixed(self) -> None:
        """xlsx 里没有、但 meta 带「固定」的词条（用户实测的规则）。"""
        view = _view_with(((0x75B9, 20, 0x27085164),))
        self.assertTrue(view.slot_is_fixed(0, self.db, grace_db=self.grace))

    def test_an_out_of_catalog_id_without_the_bit_is_editable(self) -> None:
        view = _view_with(((0x75B9, 20, 0x27080164),))
        self.assertFalse(view.slot_is_fixed(0, self.db, grace_db=self.grace))

    def test_a_grace_slot_is_not_fixed(self) -> None:
        """恩宠/套装槽同样带这个位，但它由【恩宠】栏负责。"""
        view = _view_with(((0x4FA3, 6, 0x00024C00),))
        self.assertTrue(self.grace.describe(0x4FA3), "0x4fa3 应在恩宠表里")
        self.assertFalse(view.slot_is_fixed(0, self.db, grace_db=self.grace))

    def test_an_empty_slot_is_never_fixed(self) -> None:
        view = _view_with(((0, 0, FIXED_BIT),))
        self.assertFalse(view.slot_is_fixed(0, self.db, grace_db=self.grace))

    def test_an_edit_to_a_meta_fixed_slot_is_refused(self) -> None:
        """端到端：表外但 meta 固定的槽不能再改。"""
        other = next(e for e in self.db.all() if not e.is_fixed and e.has_value_range)
        save = support.build_plain_save(records_by_slot={
            3: support.build_record(record_type=0x4001, level=170, rarity=5,
                                    effects=((0x75B9, 20, 0x27085164),)),
        })
        edits = [{"record_index": 3, "slot_index": 0,
                  "effect_id": other.effect_id, "value": other.value}]
        with self.assertRaises(EditorError) as caught:
            editor.plan_edits(save, edits, affix_db=self.db, grace_db=self.grace)
        self.assertIn("固定词条不能修改", str(caught.exception))

    def test_the_category_exception_uses_the_same_looking_glass(self) -> None:
        """★ + meta 固定的同种类词条 → 允许（例外按同一个口径）。"""
        codes = editor.load_affix_category_codes()
        category = "恢复体力"
        star = next(e for e in self.db.all() if e.category == category and e.is_star)
        plan = editor.EditPlan(
            record_index=3, offset=0, edits=(),
            before=(),
            after=_slots([(star.effect_id, codes[category]),
                          (0x75B9, codes[category] | FIXED_BIT)]))
        editor.assert_single_affix_per_category((plan,), self.db, codes)

    def test_two_star_affixes_of_different_categories_are_allowed(self) -> None:
        """用户确认：一个物品可以有多个星号词条，只要种类不同。"""
        codes = editor.load_affix_category_codes()
        by_category: dict[str, int] = {}
        for entry in self.db.all():
            if entry.is_star and entry.category not in by_category:
                by_category[entry.category] = entry.effect_id
        picked = list(by_category.items())[:2]
        self.assertEqual(len(picked), 2)
        plan = editor.EditPlan(
            record_index=3, offset=0, edits=(), before=(),
            after=_slots([(effect_id, codes[category])
                          for category, effect_id in picked]))
        editor.assert_single_affix_per_category((plan,), self.db, codes)

    def test_two_star_affixes_of_one_category_are_refused(self) -> None:
        codes = editor.load_affix_category_codes()
        entry = next(e for e in self.db.all() if e.is_star)
        same = next(e for e in self.db.all()
                    if e.is_star and e.category == entry.category
                    and e.effect_id != entry.effect_id)
        plan = editor.EditPlan(
            record_index=3, offset=0, edits=(), before=(),
            after=_slots([(entry.effect_id, codes[entry.category]),
                          (same.effect_id, codes[same.category])]))
        with self.assertRaises(EditorError):
            editor.assert_single_affix_per_category((plan,), self.db, codes)


def _slots(pairs):
    from nioh3_accessory_editor.records import EffectSlot

    return tuple(EffectSlot(slot_index=index, prefix=0, effect_id=effect_id,
                            value=0, metadata=metadata, tail_0=0, tail_1=0)
                 for index, (effect_id, metadata) in enumerate(pairs))


if __name__ == "__main__":  # pragma: no cover - manual runs only
    unittest.main()
