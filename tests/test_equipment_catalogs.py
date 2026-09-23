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
        cls.melee = equipmentdb.load_melee_weapon_db()
        cls.ranged = equipmentdb.load_ranged_weapon_db()
        cls.armor = equipmentdb.load_armor_db()
        cls.pools = (("近战武器", cls.melee), ("远程武器", cls.ranged),
                     ("防具", cls.armor))

    def test_both_tables_load_and_are_substantial(self) -> None:
        self.assertGreater(len(self.melee), 300)
        self.assertGreater(len(self.ranged), 80)
        self.assertGreater(len(self.armor), 500)

    def test_ids_are_unique(self) -> None:
        for name, db in self.pools:
            with self.subTest(name):
                ids = [entry.effect_id for entry in db.all()]
                self.assertEqual(len(ids), len(set(ids)))

    def test_every_entry_has_a_category_from_the_shipped_table(self) -> None:
        """类别必须在这张 15 类的种类码表里，否则写入时会 fail closed。"""
        from nioh3_accessory_editor import editor

        codes = editor.load_affix_category_codes()
        for name, db in self.pools:
            unknown = sorted({entry.category for entry in db.all()
                              if entry.category not in codes})
            with self.subTest(name):
                self.assertEqual(unknown, [])

    def test_star_and_fixed_rows_are_both_present(self) -> None:
        for name, db in self.pools:
            with self.subTest(name):
                self.assertTrue(any(e.is_star for e in db.all()), "缺 ★ 词条")
                self.assertTrue(any(e.is_fixed for e in db.all()), "缺同名固定词条")

    def test_no_star_affix_is_marked_fixed(self) -> None:
        """★ 不可能是固定词条（绘卷除外）——这条规则也要覆盖新表。"""
        for name, db in self.pools:
            both = [e.label for e in db.all() if e.is_star and e.is_fixed]
            with self.subTest(name):
                self.assertEqual(both, [])

    def test_value_spans_came_from_the_workbook(self) -> None:
        """数值区间是从源表带过来的，不是编的：有相当比例的条目带区间。"""
        for name, db in self.pools:
            ranged = sum(1 for entry in db.all() if entry.value_max > entry.value_min
                         or entry.value_min)
            with self.subTest(name):
                self.assertGreater(ranged, len(db.all()) // 2)

    def test_star_rows_carry_their_value_set(self) -> None:
        """★ 词条常常是离散取值集合（数值集合列），要保留下来。"""
        stars = [entry for entry in self.melee.all() if entry.is_star]
        self.assertTrue(any(entry.values for entry in stars))


class WeaponPoolSplitTests(unittest.TestCase):
    """【近战】只属于近战武器（不含弓 / 火枪 / 大炮），所以两池必须真的分开。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.melee = equipmentdb.load_melee_weapon_db()
        cls.ranged = equipmentdb.load_ranged_weapon_db()
        cls.items = equipmentdb.load_equipment_item_db()

    @staticmethod
    def _ids(db) -> set[int]:
        return {entry.effect_id for entry in db.all()}

    def test_the_pools_are_not_the_same_union(self) -> None:
        melee, ranged = self._ids(self.melee), self._ids(self.ranged)
        self.assertGreater(len(melee - ranged), 50)
        self.assertGreater(len(ranged - melee), 20)

    def test_one_affix_can_be_legal_on_both_melee_and_ranged(self) -> None:
        """源表里确实有 [近战/远程] 的行，两边都该有。"""
        self.assertTrue(self._ids(self.melee) & self._ids(self.ranged))

    def test_bows_guns_and_cannons_are_ranged_while_melee_types_are_not(self) -> None:
        weapons = [item for item in self.items.all() if item.big == "武器"]
        by_small: dict[str, str] = {}
        for item in weapons:
            by_small.setdefault(item.small, equipmentdb.pool_of_item(item))
        for small in ("弓", "火枪", "大炮"):
            with self.subTest(small):
                self.assertEqual(by_small.get(small), "ranged")
        for small in ("刀", "大太刀", "枪", "双刀", "手甲"):
            with self.subTest(small):
                self.assertEqual(by_small.get(small), "melee")

    def test_the_ranged_class_comes_from_the_item_table(self) -> None:
        """远程武器是物品总目录里的一个中类，不是我们猜的。"""
        categories = {item.category for item in self.items.all() if item.big == "武器"}
        self.assertIn("远程武器", categories)
        self.assertIn("武士武器", categories)
        self.assertIn("忍者武器", categories)

    def test_the_picker_returns_the_ranged_table_for_a_bow(self) -> None:
        bow = next(item for item in self.items.all() if item.small == "弓")
        katana = next(item for item in self.items.all() if item.small == "刀")
        self.assertIs(equipmentdb.weapon_db_for(bow, melee=self.melee,
                                                ranged=self.ranged), self.ranged)
        self.assertIs(equipmentdb.weapon_db_for(katana, melee=self.melee,
                                                ranged=self.ranged), self.melee)

    def test_the_two_files_declare_their_own_schema(self) -> None:
        import json

        melee = json.loads((ROOT / "data" / "melee_weapon_affixes.json")
                           .read_text(encoding="utf-8"))
        ranged = json.loads((ROOT / "data" / "ranged_weapon_affixes.json")
                            .read_text(encoding="utf-8"))
        self.assertEqual(melee["schema"], "nioh3-melee-weapon-affixes/v1")
        self.assertEqual(ranged["schema"], "nioh3-ranged-weapon-affixes/v1")
        self.assertIn("近战", melee["source"])
        self.assertIn("远程", ranged["source"])


class EquipmentTagTests(unittest.TestCase):
    """xlsx 的「装备种类」标签说的是能出在哪些装备上，不是词条自己的种类。

    用户澄清：`近战词条` / 绿色星号词条里的 `[近战]`、`[近战/手臂]` 是**装备**标签；
    词条的「种类（类别）」另有其列（造成伤害 / 掉落 …）。两者必须一直是分开的两件事。
    """

    #: 源表里出现过的装备标签词；它们**永远**不能成为词条的 类别。
    TAG_WORDS = ("近战", "远程", "弓", "火枪", "大炮", "头部", "身体", "手臂",
                 "腿部", "足部", "饰品", "魂核")
    MELEE_TOKENS = {"近战"}
    RANGED_TOKENS = {"远程", "弓", "火枪", "大炮"}
    ARMOR_TOKENS = {"头部", "身体", "手臂", "腿部", "足部"}

    @classmethod
    def setUpClass(cls) -> None:
        cls.files = {
            "melee_weapon_affixes.json": (cls.MELEE_TOKENS,
                                          equipmentdb.load_melee_weapon_db()),
            "ranged_weapon_affixes.json": (cls.RANGED_TOKENS,
                                           equipmentdb.load_ranged_weapon_db()),
            "armor_affixes.json": (cls.ARMOR_TOKENS, equipmentdb.load_armor_db()),
        }

    @staticmethod
    def _payload(name: str) -> dict:
        import json

        return json.loads((ROOT / "data" / name).read_text(encoding="utf-8"))

    def test_every_catalogue_carries_its_equipment_tags(self) -> None:
        for name, (_tokens, db) in self.files.items():
            with self.subTest(name):
                tags = self._payload(name)["equipment_tags"]
                self.assertTrue(tags, "装备种类标签是空的")
                self.assertLessEqual(len(tags), len(db))

    def test_an_equipment_tag_is_never_an_affix_category(self) -> None:
        """这条就是本次澄清的核心：标签 ≠ 词条类别。"""
        categories = {entry.category for _tokens, db in self.files.values()
                      for entry in db.all()}
        self.assertEqual(sorted(categories & set(self.TAG_WORDS)), [])

    def test_star_rows_keep_their_equipment_tag(self) -> None:
        for name, (_tokens, db) in self.files.items():
            tags = self._payload(name)["equipment_tags"]
            stars = [entry for entry in db.all() if entry.is_star]
            with self.subTest(name):
                self.assertTrue(stars)
                missing = [entry.label for entry in stars
                           if f"{entry.effect_id:#06x}" not in tags]
                self.assertEqual(missing, [])

    def test_the_tag_agrees_with_the_pool_it_sits_in(self) -> None:
        """近战池里的标签必须含「近战」，远程池含远程词，防具池含部位。"""
        for name, (tokens, db) in self.files.items():
            tags = self._payload(name)["equipment_tags"]
            known = {entry.effect_id for entry in db.all()}
            for key, tag in tags.items():
                with self.subTest(name=name, key=key):
                    self.assertTrue(set(tag.split("/")) & tokens,
                                    f"{key} 的标签 {tag!r} 与所在池不符")
                    self.assertIn(int(key, 16), known)

    def test_a_melee_tag_never_shows_up_in_the_ranged_table(self) -> None:
        """【近战】不含远程武器：远程表里不能出现只标近战的词条。"""
        ranged_tags = self._payload("ranged_weapon_affixes.json")["equipment_tags"]
        for key, tag in ranged_tags.items():
            with self.subTest(key):
                parts = set(tag.split("/"))
                self.assertFalse(parts and parts <= self.MELEE_TOKENS,
                                 f"{key} 只标了近战却进了远程表")

    def test_armor_rows_keep_their_body_part(self) -> None:
        """防具表用「所属部位」列当标签（[手臂] -> 手臂）。"""
        tags = self._payload("armor_affixes.json")["equipment_tags"]
        parts = {tag.split("/")[0] for tag in tags.values()}
        self.assertTrue(parts & set(self.ARMOR_TOKENS))


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
        for name in ("melee_weapon_affixes.json", "ranged_weapon_affixes.json",
                     "armor_affixes.json", "equipment_items.json",
                     "equipment_ranges.json"):
            with self.subTest(name):
                payload = json.loads((ROOT / "data" / name).read_text(encoding="utf-8"))
                self.assertIn("schema", payload)


if __name__ == "__main__":  # pragma: no cover - manual runs only
    unittest.main()
