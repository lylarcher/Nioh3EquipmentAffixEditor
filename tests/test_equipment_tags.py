"""「装备种类」标签的合并表：``equipmentdb.load_equipment_tags()``。

**覆盖范围**：把词条的 ``equipment_tags`` 字符串按 ``/`` 拆成 token 集合（去空白、
去空 token）；默认读近战武器 / 远程武器 / 防具三张表并合并成一份 id -> 集合，三张表的
全部 id 都能取到；同一个 id 在多张表里冲突时取**并集**；没有标签的词条 id 与表外的 id
取到**空集合**而不是报错；``include=`` 只取某一池、``path=`` 只读一个文件（两者互斥）；
合并表与 ``EquipmentPool``（``load_pool``）用的是同一份单文件解析，不是第二套实现。

预期值全部从随包数据算出来（或用临时文件构造），不写死 id。

仅供测试学习用，不要用于联机影响游戏平衡。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nioh3_accessory_editor import equipmentdb, records
from nioh3_accessory_editor.affixdb import AffixError

#: 三个词条池：近战武器 / 远程武器 / 防具。
POOLS = (equipmentdb.POOL_MELEE, equipmentdb.POOL_RANGED, equipmentdb.POOL_ARMOR)

#: 临时表里"词条在表里、但一个标签都没有"的那条。
MINI_UNTAGGED = 0x0009
#: 临时表里用到的全部 id（``test_only_the_named_file_is_read`` 要避开它们）。
MINI_IDS = (0x0001, 0x0002, 0x0003, 0x0004, MINI_UNTAGGED)


def mini_entry(effect_id: int, name: str, category: str = "掉落") -> dict:
    """一条形状完整的词条（数值域只是样例，本模块只看标签列）。"""
    return {"effect_id": effect_id, "value": 10, "flags": 0, "name": name,
            "category": category, "value_min": 10, "value_max": 20}


#: 一个临时的 P1 形状词条表（字段齐全，所以 ``load_pool`` 也能整表读进来）：
#: 两条带标签的词条、一条空标签、一条非字符串标签、一个坏 id，
#: 外加一条表里根本没有标签的词条。
MINI_TABLE = {
    "schema": equipmentdb.MELEE_WEAPON_CATALOG_SCHEMA,
    "source": "合成样例（仅供测试）",
    "count": 5,
    "affixes": [
        mini_entry(0x0001, "样例甲", "造成伤害"),
        mini_entry(0x0002, "样例乙"),
        mini_entry(0x0003, "样例丙"),
        mini_entry(0x0004, "样例丁"),
        mini_entry(MINI_UNTAGGED, "样例戊（表里没有标签）", "其他"),
    ],
    "equipment_tags": {
        "0x0001": "近战/手臂",
        "0x0002": "  远程 / 弓 /  ",
        "0x0003": "   ",
        "0x0004": 5,
        "0xzzzz": "近战",
    },
}


def write_table(directory: Path, payload: dict, name: str = "mini_affixes.json") -> Path:
    """把一份 P1 形状的词条表写进临时目录，返回路径。"""
    path = directory / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class SplitTests(unittest.TestCase):
    """``/`` 拆分本身：标签是「能出在哪些装备上」，拆成 token 集合。"""

    def test_a_label_splits_into_a_token_set(self) -> None:
        self.assertEqual(equipmentdb.split_equipment_tags("近战/手臂"),
                         ("近战", "手臂"))
        self.assertEqual(set(equipmentdb.split_equipment_tags("近战/手臂")),
                         {"近战", "手臂"})

    def test_whitespace_and_empty_tokens_are_dropped(self) -> None:
        self.assertEqual(equipmentdb.split_equipment_tags(" 近战 / 手臂 / "),
                         ("近战", "手臂"))
        self.assertEqual(equipmentdb.split_equipment_tags("//近战//"), ("近战",))
        for blank in ("", "   ", "//", " / "):
            with self.subTest(blank=blank):
                self.assertEqual(equipmentdb.split_equipment_tags(blank), ())


class MergedTableTests(unittest.TestCase):
    """默认读三张随包表并合并：全部 id 取得到，冲突取并集。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.merged = equipmentdb.load_equipment_tags()
        cls.pools = {key: equipmentdb.load_pool(key) for key in POOLS}
        cls.pool_tags = {key: dict(pool.tags) for key, pool in cls.pools.items()}

    def test_the_default_reads_all_three_tables(self) -> None:
        """默认不是"只读第一张"：三张表的每一条标签都在合并表里。"""
        for key, tags in self.pool_tags.items():
            with self.subTest(pool=key):
                self.assertTrue(tags)
                for effect_id, tokens in tags.items():
                    self.assertLessEqual(set(tokens), set(self.merged[effect_id]))

    def test_every_id_of_the_three_tables_is_present(self) -> None:
        for key, pool in self.pools.items():
            missing = [f"{entry.effect_id:#06x}" for entry in pool.db.all()
                       if entry.effect_id not in self.merged]
            with self.subTest(pool=key):
                self.assertEqual(missing, [], "三张表的全部 id 都要能取到")

    def test_a_row_that_appears_once_keeps_its_own_tags(self) -> None:
        owner: dict[int, list[str]] = {}
        for key, tags in self.pool_tags.items():
            for effect_id in tags:
                owner.setdefault(effect_id, []).append(key)
        unique = [effect_id for effect_id, keys in owner.items() if len(keys) == 1]
        self.assertTrue(unique)
        for effect_id in unique:
            key = owner[effect_id][0]
            with self.subTest(effect_id=f"{effect_id:#06x}", pool=key):
                self.assertEqual(self.merged[effect_id],
                                 frozenset(self.pool_tags[key][effect_id]))

    def test_a_shared_id_gets_the_union_of_both_tables(self) -> None:
        """同一个 id 在两张表里标签不同 -> 取并集（随包数据里真有这种行）。"""
        melee, armor = self.pool_tags[equipmentdb.POOL_MELEE], \
            self.pool_tags[equipmentdb.POOL_ARMOR]
        conflicts = {effect_id: (set(melee[effect_id]), set(armor[effect_id]))
                     for effect_id in set(melee) & set(armor)
                     if melee[effect_id] != armor[effect_id]}
        self.assertTrue(conflicts, "随包数据里应当有标签冲突的 id（本用例的证据）")
        for effect_id, (left, right) in conflicts.items():
            with self.subTest(effect_id=f"{effect_id:#06x}"):
                self.assertNotEqual(left, right)
                self.assertEqual(self.merged[effect_id], frozenset(left | right))

    def test_an_isolated_id_reads_as_an_empty_set_not_an_error(self) -> None:
        """表外的 id 一律空集合：调用方据此 fail closed，而不是抛异常。"""
        unknown = records.EMPTY_EFFECT_ID
        self.assertNotIn(unknown, self.merged)
        self.assertEqual(self.merged[unknown], frozenset())
        self.assertEqual(self.merged.get(unknown), frozenset())

    def test_a_row_without_a_tag_reads_as_an_empty_set(self) -> None:
        """表里那条没有标签的词条也取得到空集合（不报错、也不当成"哪儿都能写"）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = write_table(Path(tmp), MINI_TABLE)
            table = equipmentdb.load_equipment_tags(path=path)
        self.assertFalse(table[0x0009], "没有标签 -> 空集合")
        self.assertEqual(table[0x0009], frozenset())

    def test_the_merged_table_is_still_an_ordinary_dict(self) -> None:
        self.assertIsInstance(self.merged, dict)
        self.assertEqual(self.merged, dict(self.merged))
        self.assertEqual(sorted(self.merged), sorted(self.merged.keys()))
        for effect_id in self.merged:
            self.assertIsInstance(self.merged[effect_id], frozenset)


class PoolSelectionTests(unittest.TestCase):
    """``include=`` 只取某一池；``path=`` 只读一个文件。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.pool_tags = {key: dict(equipmentdb.load_pool(key).tags) for key in POOLS}
        keys = list(cls.pool_tags)
        cls.only_melee = (set(cls.pool_tags[keys[0]])
                          - set(cls.pool_tags[keys[1]]) - set(cls.pool_tags[keys[2]]))

    def test_include_takes_exactly_one_pool(self) -> None:
        table = equipmentdb.load_equipment_tags(include=equipmentdb.POOL_ARMOR)
        self.assertEqual(
            table,
            {effect_id: frozenset(tokens)
             for effect_id, tokens in self.pool_tags[equipmentdb.POOL_ARMOR].items()})
        self.assertTrue(self.only_melee)
        for effect_id in self.only_melee:
            with self.subTest(effect_id=f"{effect_id:#06x}"):
                self.assertEqual(table[effect_id], frozenset(),
                                 "近战独有的词条不该出现在防具池的结果里")

    def test_include_accepts_several_pools(self) -> None:
        selected = (equipmentdb.POOL_MELEE, equipmentdb.POOL_RANGED)
        table = equipmentdb.load_equipment_tags(include=selected)
        self.assertEqual(len(table), len(set(self.pool_tags[selected[0]])
                                        | set(self.pool_tags[selected[1]])))
        for effect_id in self.only_melee:
            with self.subTest(effect_id=f"{effect_id:#06x}"):
                self.assertEqual(table[effect_id],
                                 frozenset(self.pool_tags[selected[0]][effect_id]))

    def test_include_rejects_anything_else(self) -> None:
        for bad in ("nope", "", ("melee", "nope")):
            with self.subTest(include=bad):
                with self.assertRaises(AffixError) as caught:
                    equipmentdb.load_equipment_tags(include=bad)
                self.assertIn("未知的词条池", str(caught.exception))
        with self.assertRaises(AffixError) as caught:
            equipmentdb.load_equipment_tags(include=())
        self.assertIn("至少要指定一个词条池", str(caught.exception))


class SingleFileTests(unittest.TestCase):
    """``path=``：只读这一个文件（测试 / 工具指向自己的表时用）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.path = write_table(Path(cls._tmp.name), MINI_TABLE)
        cls.table = equipmentdb.load_equipment_tags(path=cls.path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_tags_are_split_and_trimmed(self) -> None:
        self.assertEqual(self.table[0x0001], frozenset({"近战", "手臂"}))
        self.assertEqual(self.table[0x0002], frozenset({"远程", "弓"}))

    def test_junk_rows_are_skipped_not_invented(self) -> None:
        """空标签、非字符串标签、坏 id 都不进结果；取它们得到空集合。"""
        for effect_id in (0x0003, 0x0004, MINI_UNTAGGED, 0xDEAD):
            with self.subTest(effect_id=f"{effect_id:#06x}"):
                self.assertIsInstance(self.table[effect_id], frozenset)
                self.assertEqual(self.table[effect_id], frozenset())

    def test_only_the_named_file_is_read(self) -> None:
        """不是"读了这个再读三张随包表"：表外的 id 一律空集合。"""
        shipped = equipmentdb.load_equipment_tags()
        sample = next(effect_id for effect_id in shipped
                      if effect_id not in MINI_IDS)
        self.assertEqual(self.table[sample], frozenset())
        self.assertLess(len(self.table), len(shipped))

    def test_path_and_include_are_mutually_exclusive(self) -> None:
        with self.assertRaises(AffixError) as caught:
            equipmentdb.load_equipment_tags(path=self.path,
                                            include=equipmentdb.POOL_ARMOR)
        self.assertIn("只能给一个", str(caught.exception))


class SharedParseTests(unittest.TestCase):
    """两个入口（``EquipmentPool`` 与合并表）的标签来自同一份单文件解析。"""

    def test_one_parser_serves_both_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_table(Path(tmp), MINI_TABLE)
            with mock.patch.object(equipmentdb, "_read_equipment_tags",
                                   wraps=equipmentdb._read_equipment_tags) as reader:
                pool = equipmentdb.load_pool(equipmentdb.POOL_MELEE, path)
                table = equipmentdb.load_equipment_tags(path=path)
        self.assertEqual(reader.call_args_list, [mock.call(path), mock.call(path)])
        self.assertTrue(pool.tags)
        for effect_id, tokens in pool.tags.items():
            with self.subTest(effect_id=f"{effect_id:#06x}"):
                self.assertEqual(table[effect_id], frozenset(tokens))

    def test_the_default_path_hits_the_pool_cache(self) -> None:
        """默认参数直接复用已加载的词条池，不重复读文件。"""
        for key in POOLS:
            equipmentdb.load_pool(key)
        with mock.patch.object(equipmentdb, "_read_equipment_tags",
                               wraps=equipmentdb._read_equipment_tags) as reader:
            equipmentdb.load_equipment_tags()
        self.assertEqual(reader.call_args_list, [])


if __name__ == "__main__":  # pragma: no cover - manual runs only
    unittest.main()
