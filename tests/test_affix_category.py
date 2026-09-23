"""改写词条时必须一起改写「词条种类」（metadata bits 8..12 = 那个小图标）。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from nioh3_accessory_editor import editor
from nioh3_accessory_editor.affixdb import AffixDb, AffixError, load_affix_category_codes
from nioh3_accessory_editor.editor import EditorError
from tests import support

#: Repository root (this file lives in <root>/tests).
ROOT = Path(__file__).resolve().parents[1]


class CategoryTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.codes = load_affix_category_codes()
        cls.db = support.load_catalog_affixes()

    def test_the_table_is_measured_evidence(self) -> None:
        payload = json.loads((ROOT / "data" / "affix_categories.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(payload["_mask"], 0x1F00)
        self.assertEqual(payload["_evidence"]["conflicts"], 0)
        self.assertGreaterEqual(payload["_evidence"]["accessory_slots"], 800)
        self.assertGreaterEqual(payload["_evidence"]["soul_slots"], 500)

    def test_every_catalog_category_has_a_code(self) -> None:
        """Otherwise affix_metadata() would refuse to write (fail closed)."""
        missing = {entry.category for entry in self.db.all()} - set(self.codes)
        self.assertEqual(missing, set())

    def test_soul_catalog_categories_are_covered_too(self) -> None:
        from nioh3_accessory_editor.affixdb import load_soul_catalog

        soul = AffixDb(load_soul_catalog())
        missing = {entry.category for entry in soul.all()} - set(self.codes)
        self.assertEqual(missing, set())

    def test_a_broken_table_raises(self) -> None:
        bad = ROOT / "tests" / "_tmp_categories.json"
        try:
            bad.write_text('{"categories": {"x": "no"}}', encoding="utf-8")
            with self.assertRaises(AffixError):
                load_affix_category_codes(bad)
        finally:
            bad.unlink(missing_ok=True)


class MetadataRewriteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = support.load_catalog_affixes()
        cls.codes = load_affix_category_codes()

    def _entry(self, category: str):
        return next(entry for entry in self.db.all() if entry.category == category)

    def test_the_category_bits_are_replaced(self) -> None:
        entry = self._entry("恢复体力")
        before = 0x0000_995C  # 掉落 (0x09) 的旧词条
        after = editor.affix_metadata(before, entry, self.codes)
        self.assertEqual(after & editor.CATEGORY_CODE_MASK, self.codes["恢复体力"])
        self.assertEqual(after & ~editor.CATEGORY_CODE_MASK, before & ~editor.CATEGORY_CODE_MASK)

    def test_every_other_bit_is_kept(self) -> None:
        entry = self._entry("灵力")
        before = 0x0026_4C7C  # 固定位 (0x4000) + byte11 的 0x26 + 低位标志
        after = editor.affix_metadata(before, entry, self.codes)
        self.assertEqual(after & 0x4000, 0x4000)          # 固定保留
        # 0x0026_0000 里也含 ★ 位（byte10 bit2 = 0x040000），它随词条走，故排除：
        self.assertEqual(after & 0x0022_0000, 0x0022_0000)  # byte10/11 其余位保留
        self.assertEqual(after & 0x0000_007C, 0x0000_007C)  # byte8 保留
        # ★ 位是随词条走的派生标志位（见 tests/test_star_rule.py），不在这条断言里。
        self.assertEqual(bool(after & editor.STAR_BIT), entry.is_star)

    def test_an_unknown_category_is_refused(self) -> None:
        entry = self._entry("恢复体力")
        with self.assertRaises(EditorError) as caught:
            editor.affix_metadata(0, entry, {"别的类别": 0x0100})
        self.assertIn("词条种类码未知", str(caught.exception))

    def test_an_out_of_table_id_keeps_the_metadata(self) -> None:
        self.assertEqual(editor.affix_metadata(0x1234, None, self.codes), 0x1234)

    def test_the_mapping_matches_the_catalog_categories(self) -> None:
        for entry in self.db.all():
            code = self.codes[entry.category]
            self.assertTrue(0 <= code <= editor.CATEGORY_CODE_MASK)
            self.assertEqual(code & editor.CATEGORY_CODE_MASK, code)


class PlannedEditCarriesTheCodeTests(unittest.TestCase):
    """端到端：计划里的 after 槽位必须带新词条的种类码。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = support.load_catalog_affixes()
        cls.codes = load_affix_category_codes()

    def test_a_planned_edit_rewrites_the_icon_bits(self) -> None:
        target = next(entry for entry in self.db.all()
                      if not entry.is_fixed and entry.has_value_range
                      and entry.category == "恢复体力")
        current = next(entry for entry in self.db.all()
                       if not entry.is_fixed and entry.category == "掉落")
        save = support.build_plain_save(records_by_slot={
            3: support.build_record(record_type=0x4001, level=170, rarity=5,
                                    effects=((current.effect_id, current.value, 0x995C),)),
        })
        edits = [{"record_index": 3, "slot_index": 0,
                  "effect_id": target.effect_id, "value": target.value}]
        plans = editor.plan_edits(save, edits, affix_db=self.db)
        after = plans[0].after[0]
        self.assertEqual(after.effect_id, target.effect_id)
        self.assertEqual(after.metadata & editor.CATEGORY_CODE_MASK,
                         self.codes[target.category])

    def test_the_written_bytes_change_too(self) -> None:
        target = next(entry for entry in self.db.all()
                      if not entry.is_fixed and entry.has_value_range
                      and entry.category == "恢复体力")
        save = support.build_plain_save(records_by_slot={
            3: support.build_record(record_type=0x4001, level=170, rarity=5,
                                    effects=((0x9696, 80, 0x00009F5C),)),
        })
        edits = [{"record_index": 3, "slot_index": 0,
                  "effect_id": target.effect_id, "value": target.value}]
        patched = editor.apply_edits(save, edits, affix_db=self.db)
        self.assertEqual(len(patched), len(save))
        self.assertNotEqual(patched, save)


if __name__ == "__main__":  # pragma: no cover - manual runs only
    unittest.main()
