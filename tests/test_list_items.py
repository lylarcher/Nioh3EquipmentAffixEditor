"""统一列举入口 ``editor.list_items()``：一个 ``kind`` 转发到各自的列举函数。

**覆盖范围**：四个 ``kind``（武器 / 防具 / 饰品 / 魂核）都能列出，结果与直接调用
``list_equipment(big=...)`` / ``list_weapons`` / ``list_armor`` / ``list_accessories`` /
``list_soul_cores`` 逐项一致；``list_items(data, kind="饰品")`` 与
``list_accessories(data)`` 完全一致（它是**薄封装**，被转发对象的语义不变）；
``layout`` / ``known_ids`` / ``item_db`` / ``soul_db`` / ``soul_item_db`` 按名字原样
转发（缺省的仍传 ``None``，交给被转发的函数用自己的默认值）；未知或空的 ``kind``
明确报错，不会退化成"列出全部"。各 ``kind`` 自己的识别规则由
``tests/test_weapon_armor_edits.py``、``tests/test_editor.py`` 覆盖，这里不重复测。

样本存档用 ``tests/support.py`` 的 ``build_plain_save`` / ``build_record`` 合成，
物品种类与词条都从随包数据里查出来，不写死 id。

仅供测试学习用，不要用于联机影响游戏平衡。
"""

from __future__ import annotations

import unittest
from unittest import mock

from nioh3_accessory_editor import editor, equipmentdb, records
from nioh3_accessory_editor.affixdb import (
    AffixDb,
    ItemDb,
    load_soul_catalog,
    load_soul_item_catalog,
)
from nioh3_accessory_editor.editor import (
    EditorError,
    list_accessories,
    list_armor,
    list_equipment,
    list_items,
    list_soul_cores,
    list_weapons,
)
from tests import support

ITEM_DB = equipmentdb.load_equipment_item_db()
SOUL_DB = AffixDb(load_soul_catalog())
SOUL_ITEM_DB = ItemDb(load_soul_item_catalog())


def item_of(big: str, small: str = "") -> equipmentdb.EquipmentItem:
    """物品总目录里指定大类的第一个种类（跳过会被当成绘卷的类型 id）。"""
    for entry in ITEM_DB.all():
        if entry.big == big and (not small or entry.small == small) \
                and entry.item_id not in records.SCROLL_TYPES:
            return entry
    raise AssertionError(f"物品总目录里没有 {big}/{small or '（任意小类）'}")


def soul_item_of() -> int:
    """魂核种类表里的一个 id（魂核的记录种类来自这张表，不是物品总目录）。"""
    for entry in SOUL_ITEM_DB.all():
        if entry.item_id not in records.SCROLL_TYPES:
            return entry.item_id
    raise AssertionError("魂核种类表是空的")


#: 一块合成存档：槽 3 刀、槽 4 弓、槽 5 手臂甲、槽 6 饰品、槽 7 魂核。
#: 「饰品」那条是 ``list_accessories`` 的原始口径（整张记录数组都列出来），
#: 另外三个 ``kind`` 各自只列自己能证明的那几条。
WEAPON_SLOTS = (3, 4)
ARMOR_SLOTS = (5,)
ACCESSORY_SLOTS = (3, 4, 5, 6, 7)
SOUL_SLOTS = (7,)


class ListItemsTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.katana = item_of("武器", "刀")
        cls.bow = item_of("武器", "弓")
        cls.arm_piece = item_of("防具", "手臂")
        cls.accessory = item_of("饰品")
        cls.soul_item = soul_item_of()
        cls.soul_affix = next(entry for entry in SOUL_DB.all() if not entry.is_fixed)
        cls.accessory_db = AffixDb()
        cls.accessory_known = editor.accessory_catalog_ids(cls.accessory_db)
        cls.save = support.build_plain_save(records_by_slot={
            3: support.build_record(record_type=cls.katana.item_id, level=170),
            4: support.build_record(record_type=cls.bow.item_id, level=160, plus=7),
            5: support.build_record(record_type=cls.arm_piece.item_id, level=150),
            6: support.build_record(record_type=cls.accessory.item_id, level=170,
                                    rarity=5),
            7: support.build_record(
                record_type=cls.soul_item, level=170, rarity=5,
                effects=((cls.soul_affix.effect_id, cls.soul_affix.value, 0x0040),)),
        })
        cls.layout = records.locate_layout(cls.save)

    def slots(self, kind: str, **kwargs) -> list[int]:
        return [view.slot_index
                for view in list_items(self.save, kind=kind, layout=self.layout,
                                       **kwargs)]


class KindForwardingTests(ListItemsTestCase):
    """四个 kind 与各自的既有入口逐项一致。"""

    def test_every_kind_lists_the_records_it_owns(self) -> None:
        self.assertEqual(self.slots("武器"), list(WEAPON_SLOTS))
        self.assertEqual(self.slots("防具"), list(ARMOR_SLOTS))
        self.assertEqual(self.slots("魂核"), list(SOUL_SLOTS))
        # 饰品是整张记录数组（list_accessories 的既有口径，这次没有收紧它）。
        self.assertEqual(self.slots("饰品"), list(ACCESSORY_SLOTS))

    def test_the_accessory_kind_is_identical_to_list_accessories(self) -> None:
        """带参数的既有调用一字不差地传下去：结果必须完全相同。"""
        self.assertEqual(list_items(self.save, kind="饰品"),
                         list_accessories(self.save))
        self.assertEqual(
            list_items(self.save, kind="饰品", layout=self.layout,
                       known_ids=self.accessory_known),
            list_accessories(self.save, layout=self.layout,
                             known_ids=self.accessory_known))

    def test_the_weapon_and_armor_kinds_match_their_own_entries(self) -> None:
        self.assertEqual(
            list_items(self.save, kind="武器", layout=self.layout),
            list_equipment(self.save, big="武器", layout=self.layout))
        self.assertEqual(
            list_items(self.save, kind="武器", layout=self.layout),
            list_weapons(self.save, layout=self.layout))
        self.assertEqual(
            list_items(self.save, kind="防具", layout=self.layout),
            list_equipment(self.save, big="防具", layout=self.layout))
        self.assertEqual(
            list_items(self.save, kind="防具", layout=self.layout),
            list_armor(self.save, layout=self.layout))

    def test_the_soul_kind_matches_list_soul_cores(self) -> None:
        self.assertEqual(
            list_items(self.save, kind="魂核", layout=self.layout),
            list_soul_cores(self.save, soul_db=SOUL_DB, soul_item_db=SOUL_ITEM_DB,
                            layout=self.layout))
        self.assertEqual(
            list_items(self.save, kind="魂核", layout=self.layout, soul_db=SOUL_DB,
                       soul_item_db=SOUL_ITEM_DB),
            list_soul_cores(self.save, soul_db=SOUL_DB, soul_item_db=SOUL_ITEM_DB,
                            layout=self.layout))

    def test_the_default_soul_tables_are_the_shipped_ones(self) -> None:
        """不注入就是随包的那两张表：与显式传入它们结果一致。"""
        self.assertEqual(
            list_items(self.save, kind="魂核", layout=self.layout),
            list_items(self.save, kind="魂核", layout=self.layout, soul_db=SOUL_DB,
                       soul_item_db=SOUL_ITEM_DB))


class ArgumentForwardingTests(ListItemsTestCase):
    """转发的是"按名字对齐的参数"，不补也不改：缺省仍是 ``None``。"""

    def test_the_dispatch_passes_exactly_the_named_arguments(self) -> None:
        marker = object()
        with mock.patch.object(editor, "list_accessories",
                               return_value=marker) as called:
            self.assertIs(list_items(self.save, kind="饰品", layout=self.layout,
                                     known_ids=self.accessory_known), marker)
        called.assert_called_once_with(self.save, layout=self.layout,
                                       known_ids=self.accessory_known)

        with mock.patch.object(editor, "list_equipment",
                               return_value=marker) as called:
            self.assertIs(list_items(self.save, kind="防具", layout=self.layout),
                          marker)
        called.assert_called_once_with(self.save, big="防具", item_db=None,
                                       layout=self.layout, known_ids=None)

    def test_the_soul_dispatch_fills_in_the_shipped_tables(self) -> None:
        marker = object()
        with mock.patch.object(editor, "list_soul_cores",
                               return_value=marker) as called:
            self.assertIs(list_items(self.save, kind="魂核", layout=self.layout),
                          marker)
        self.assertEqual(called.call_args.args, (self.save,))
        kwargs = called.call_args.kwargs
        self.assertIs(kwargs["layout"], self.layout)
        self.assertIsNone(kwargs["known_ids"])
        self.assertEqual(len(kwargs["soul_db"]), len(SOUL_DB))
        self.assertEqual(len(kwargs["soul_item_db"]), len(SOUL_ITEM_DB))
        self.assertIsNotNone(
            kwargs["soul_db"].lookup(self.soul_affix.effect_id),
            "默认魂核词条表必须是随包的那张：它认识存档里这条魂核词条")

    def test_an_injected_item_db_reaches_the_equipment_path(self) -> None:
        """注入的空物品总目录 -> 武器 / 防具都列不出来（证明参数真的转发了）。"""
        empty = equipmentdb.EquipmentItemDb()
        self.assertEqual(
            list_items(self.save, kind="武器", layout=self.layout, item_db=empty),
            ())
        self.assertEqual(
            list_items(self.save, kind="防具", layout=self.layout, item_db=empty),
            ())
        self.assertTrue(self.slots("武器"), "不注入时仍然列得出来")

    def test_layout_and_known_ids_are_forwarded(self) -> None:
        """自己定位的记录表与显式传入的是同一块：结果一致（不是各列一遍）。"""
        self.assertEqual(
            list_items(self.save, kind="武器",
                       known_ids=frozenset({self.katana.item_id})),
            list_items(self.save, kind="武器", layout=self.layout,
                       known_ids=frozenset({self.katana.item_id})))
        self.assertEqual(
            list_items(self.save, kind="魂核", layout=self.layout,
                       known_ids=editor.soul_catalog_ids(SOUL_DB)),
            list_items(self.save, kind="魂核"))


class RejectedKindTests(ListItemsTestCase):
    """白名单之外的 ``kind`` 一律报错：不猜、也不退化成"列出全部"。"""

    def test_an_unknown_or_empty_kind_is_refused(self) -> None:
        for kind in ("", "绘卷", "装备", "武器 ", "soul"):
            with self.subTest(kind=kind):
                with self.assertRaises(EditorError) as caught:
                    list_items(self.save, kind=kind, layout=self.layout)
                message = str(caught.exception)
                for expected in editor.ITEM_LIST_KINDS:
                    self.assertIn(expected, message)

    def test_the_supported_kinds_are_the_four_documented_ones(self) -> None:
        self.assertEqual(editor.ITEM_LIST_KINDS, ("武器", "防具", "饰品", "魂核"))


if __name__ == "__main__":  # pragma: no cover - manual runs only
    unittest.main()
