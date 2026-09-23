"""武器/防具目录（P1）：词条表、物品总目录全类别、按类别收集的范围。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from nioh3_accessory_editor import equipmentdb

ROOT = Path(__file__).resolve().parents[1]


class WeaponArmorAffixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.weapon = equipmentdb.load_weapon_db()
        cls.armor = equipmentdb.load_armor_db()

    def test_both_tables_load_and_are_substantial(self) -> None:
        self.assertGreater(len(self.weapon), 400)
        self.assertGreater(len(self.armor), 500)

    def test_ids_are_unique(self) -> None:
        for name, db in (("武器", self.weapon), ("防具", self.armor)):
            with self.subTest(name):
                ids = [entry.effect_id for entry in db.all()]
                self.assertEqual(len(ids), len(set(ids)))

    def test_every_entry_has_a_category_from_the_shipped_table(self) -> None:
        """类别必须在这张 15 类的种类码表里，否则写入时会 fail closed。"""
        from nioh3_accessory_editor import editor

        codes = editor.load_affix_category_codes()
        for name, db in (("武器", self.weapon), ("防具", self.armor)):
            unknown = sorted({entry.category for entry in db.all()
                              if entry.category not in codes})
            with self.subTest(name):
                self.assertEqual(unknown, [])

    def test_star_and_fixed_rows_are_both_present(self) -> None:
        for name, db in (("武器", self.weapon), ("防具", self.armor)):
            with self.subTest(name):
                self.assertTrue(any(e.is_star for e in db.all()), "缺 ★ 词条")
                self.assertTrue(any(e.is_fixed for e in db.all()), "缺同名固定词条")

    def test_no_star_affix_is_marked_fixed(self) -> None:
        """★ 不可能是固定词条（绘卷除外）——这条规则也要覆盖新表。"""
        for name, db in (("武器", self.weapon), ("防具", self.armor)):
            both = [e.label for e in db.all() if e.is_star and e.is_fixed]
            with self.subTest(name):
                self.assertEqual(both, [])

    def test_value_spans_came_from_the_workbook(self) -> None:
        """数值区间是从源表带过来的，不是编的：有相当比例的条目带区间。"""
        for name, db in (("武器", self.weapon), ("防具", self.armor)):
            ranged = sum(1 for entry in db.all() if entry.value_max > entry.value_min
                         or entry.value_min)
            with self.subTest(name):
                self.assertGreater(ranged, len(db.all()) // 2)

    def test_star_rows_carry_their_value_set(self) -> None:
        """★ 词条常常是离散取值集合（数值集合列），要保留下来。"""
        stars = [entry for entry in self.weapon.all() if entry.is_star]
        self.assertTrue(any(entry.values for entry in stars))


class EquipmentItemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = equipmentdb.load_equipment_item_db()
        cls.items = list(cls.db.all())

    def test_the_table_covers_every_big_class(self) -> None:
        self.assertGreater(len(self.items), 2000)
        for big in ("武器", "防具", "饰品", "魂核"):
            with self.subTest(big):
                self.assertIn(big, self.db.big_classes())

    def test_the_type_axis_has_the_expected_values(self) -> None:
        """「类型」= 小类：武器 19 类、防具 5 类（胸甲/腿甲那一条轴）。"""
        weapons = self.db.small_classes("武器")
        armor = self.db.small_classes("防具")
        self.assertEqual(len(weapons), 19)
        self.assertEqual(set(armor),
                         {"头部", "身体", "手臂", "腿部", "足部"})
        for expected in ("刀", "大太刀", "枪", "弓"):
            self.assertIn(expected, weapons)

    def test_the_school_axis_is_available(self) -> None:
        """武士 / 忍者 由中类得出。"""
        schools = set(self.db.schools("武器")) | set(self.db.schools("防具"))
        self.assertIn("武士", schools)
        self.assertIn("忍者", schools)

    def test_two_armours_share_a_type_but_are_different_kinds(self) -> None:
        """用户举的例子：同为武士防具-身体，也不是同一个种类。"""
        bodies = [item for item in self.items
                  if item.big == "防具" and item.small == "身体"
                  and item.school == "武士"]
        self.assertGreater(len({item.item_id for item in bodies}), 1)

    def test_the_override_rows_carry_their_evidence(self) -> None:
        fixed = [item for item in self.items if item.source]
        self.assertTrue(fixed)
        for item in fixed:
            with self.subTest(hex(item.item_id)):
                self.assertIn("实测", item.source)


class EquipmentRangeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = equipmentdb.load_equipment_ranges()
        cls.classes = cls.payload["classes"]

    def test_the_ranges_came_from_a_named_save(self) -> None:
        self.assertEqual(len(self.payload["sha256"]), 64)
        self.assertIn("SAVEDATA", self.payload["source"])

    def test_every_class_has_a_range_and_samples(self) -> None:
        for key, bucket in self.classes.items():
            with self.subTest(key):
                self.assertGreater(bucket["samples"], 0)
                for field in ("level", "plus", "slots"):
                    self.assertEqual(len(bucket[field]), 2)
                    self.assertLessEqual(bucket[field][0], bucket[field][1])

    def test_the_measured_caps_match_what_we_tell_the_user(self) -> None:
        """等级上限 180、+値上限 30（魂核另算，见下一条）。"""
        weapons = {key: value for key, value in self.classes.items()
                   if value["big"] == "武器"}
        armor = {key: value for key, value in self.classes.items()
                 if value["big"] == "防具"}
        for pool in (weapons, armor):
            self.assertTrue(pool)
            self.assertLessEqual(max(v["level"][1] for v in pool.values()), 180)
            self.assertLessEqual(max(v["plus"][1] for v in pool.values()), 30)

    def test_soul_cores_use_a_lower_plus_cap(self) -> None:
        """按类别收集的直接结论：魂核 +値 只到 15，而武器/防具到 30。"""
        souls = {key: value for key, value in self.classes.items()
                 if value["big"] == "魂核"}
        weapons = {key: value for key, value in self.classes.items()
                   if value["big"] == "武器"}
        if not souls:
            self.skipTest("范围表里没有魂核类别")
        self.assertLessEqual(max(v["plus"][1] for v in souls.values()), 15)
        self.assertEqual(max(v["plus"][1] for v in weapons.values()), 30)

    def test_weapons_always_carry_five_slots_and_armor_varies(self) -> None:
        weapons = [v for v in self.classes.values() if v["big"] == "武器"]
        armor = [v for v in self.classes.values() if v["big"] == "防具"]
        self.assertEqual({tuple(v["slots"]) for v in weapons}, {(5, 5)})
        self.assertTrue(any(tuple(v["slots"]) != (5, 5) for v in armor))

    def test_no_unnamed_item_type_was_left(self) -> None:
        self.assertEqual(self.payload["unnamed_types"], {})

    def test_the_shipped_files_are_plain_json(self) -> None:
        for name in ("weapon_affixes.json", "armor_affixes.json",
                     "equipment_items.json", "equipment_ranges.json"):
            with self.subTest(name):
                payload = json.loads((ROOT / "data" / name).read_text(encoding="utf-8"))
                self.assertIn("schema", payload)


if __name__ == "__main__":  # pragma: no cover - manual runs only
    unittest.main()
