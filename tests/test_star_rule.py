"""除绘卷外，★ 词条不可能是固定词条；★ 词条可改；★ 位随词条同步。"""

from __future__ import annotations

import unittest

from nioh3_equipment_affix_editor import editor
from nioh3_equipment_affix_editor.affixdb import AffixDb, load_soul_catalog
from tests import support

FIXED_BIT = 0x4000
STAR_BIT = editor.STAR_BIT


def _view_with(effects):
    from nioh3_equipment_affix_editor import records

    save = support.build_plain_save(records_by_slot={
        3: support.build_record(record_type=0x4001, level=170, rarity=5,
                                effects=effects),
    })
    layout = records.locate_layout(save)
    return list(editor.list_accessories(save, layout=layout))[0]


class CatalogGuardTests(unittest.TestCase):
    """随包词条表本身不能出现「★ 且固定」。"""

    def test_no_star_affix_is_marked_fixed(self) -> None:
        for name, db in (("饰品", AffixDb()),
                         ("魂核", AffixDb(load_soul_catalog()))):
            with self.subTest(name):
                both = [entry.label for entry in db.all()
                        if entry.is_star and entry.is_fixed]
                self.assertEqual(both, [], f"{name} 表里出现 ★ 且固定 的词条")

    def test_the_catalogs_do_ship_star_affixes(self) -> None:
        """★ 词条得能在下拉里选到，否则「★ 可改」没有意义。"""
        self.assertTrue(any(e.is_star for e in AffixDb().all()))
        self.assertTrue(any(e.is_star for e in AffixDb(load_soul_catalog()).all()))


class StarIsNeverFixedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = support.load_catalog_affixes()
        cls.star = next(e for e in cls.db.all() if e.is_star)

    def test_a_star_affix_is_editable(self) -> None:
        view = _view_with(((self.star.effect_id, self.star.value, STAR_BIT),))
        self.assertFalse(view.slot_is_fixed(0, self.db))

    def test_a_star_affix_with_the_fixed_bit_is_still_editable(self) -> None:
        """存档里万一给 ★ 槽带了固定位，也按用户规则当可改。"""
        view = _view_with(((self.star.effect_id, self.star.value,
                            STAR_BIT | FIXED_BIT),))
        self.assertFalse(view.slot_is_fixed(0, self.db))

    def test_an_out_of_catalog_star_slot_with_the_fixed_bit_is_editable(self) -> None:
        """表外的 id 也一样：带 ★ 位就不是固定词条。"""
        view = _view_with(((0x75B9, 20, STAR_BIT | FIXED_BIT),))
        self.assertFalse(view.slot_is_fixed(0, self.db))

    def test_a_scroll_record_may_keep_a_fixed_star_affix(self) -> None:
        """绘卷是例外：绘卷记录上「★ 且固定」允许存在。"""
        scroll_type = sorted(editor.SCROLL_RECORD_TYPES)[0]
        from nioh3_equipment_affix_editor import records

        save = support.build_plain_save(records_by_slot={
            3: support.build_record(record_type=scroll_type, level=170, rarity=5,
                                    effects=((self.star.effect_id, self.star.value,
                                              STAR_BIT | FIXED_BIT),)),
        })
        layout = records.locate_layout(save)
        views = [view for view in editor.list_accessories(save, layout=layout)
                 if view.record_type == scroll_type]
        if not views:
            self.skipTest("合成存档里该绘卷类型没被识别出来")
        self.assertTrue(views[0].slot_is_fixed(0, self.db))

    def test_the_category_exception_ignores_a_star_slot(self) -> None:
        """★ 槽不会被当成「同名固定」，所以不能靠它凑出同种类的第二个。"""
        slot = editor.records.EffectSlot(slot_index=0, prefix=0,
                                         effect_id=self.star.effect_id, value=0,
                                         metadata=STAR_BIT | FIXED_BIT,
                                         tail_0=0, tail_1=0)
        self.assertFalse(editor._slot_is_fixed(slot, self.db))


class StarBitWriteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = support.load_catalog_affixes()
        cls.star = next(e for e in cls.db.all() if e.is_star)
        cls.plain = next(e for e in cls.db.all()
                         if not e.is_star and not e.is_fixed and e.has_value_range)

    def test_choosing_a_star_affix_sets_the_bit(self) -> None:
        written = editor.affix_metadata(0x00000000, self.star)
        self.assertTrue(written & STAR_BIT)

    def test_choosing_an_ordinary_affix_clears_the_bit(self) -> None:
        written = editor.affix_metadata(STAR_BIT | 0x0020_0000, self.plain)
        self.assertFalse(written & STAR_BIT)
        self.assertTrue(written & 0x0020_0000, "种类码与 ★ 位以外的位必须保留")

    def test_other_bits_survive_a_star_write(self) -> None:
        written = editor.affix_metadata(FIXED_BIT | 0x120000, self.star)
        self.assertTrue(written & STAR_BIT)
        self.assertTrue(written & FIXED_BIT)
        self.assertTrue(written & 0x120000)

    def test_an_end_to_end_write_moves_the_star_bit(self) -> None:
        """端到端：把普通词条换成 ★ 词条，写完后槽位带 ★ 位。"""
        save = support.build_plain_save(records_by_slot={
            3: support.build_record(record_type=0x4001, level=170, rarity=5,
                                    effects=((self.plain.effect_id, self.plain.value,
                                              0x1000),)),
        })
        edits = [{"record_index": 3, "slot_index": 0,
                  "effect_id": self.star.effect_id, "value": self.star.value}]
        patched = editor.apply_edits(save, edits, affix_db=self.db)
        from nioh3_equipment_affix_editor import records

        layout = records.locate_layout(patched)
        view = next(v for v in editor.list_accessories(patched, layout=layout)
                    if v.slot_index == 3)
        self.assertEqual(view.effects[0].effect_id, self.star.effect_id)
        self.assertTrue(view.effects[0].metadata & STAR_BIT)
        self.assertFalse(view.slot_is_fixed(0, self.db), "★ 槽必须仍然可改")


if __name__ == "__main__":  # pragma: no cover - manual runs only
    unittest.main()
