"""稀有度（品质，字段 ``+0x30``）修改：引擎 + 四个页签 + 上限唯一来源。

**覆盖范围**（对应本轮改动）：

* **上限表唯一来源**：``limits.py`` 是等级 / +値 / 稀有度上限与「稀有度 -> 颜色」
  的唯一出处；``records.MAX_ITEM_LEVEL`` / ``equipmentdb.PLUS_CAP_BY_BIG`` /
  ``equipmentdb.RARITY_CAP_BY_BIG`` 都只是它的 re-export（同一个对象）。
* **引擎**：``editor.RarityPlan`` / ``plan_rarity_edit`` / ``apply_rarity_edits`` ——
  四类 x 边界（武器/防具/饰品 4 可写、5 拒绝；魂核 3 可写、4 拒绝）、写 0、
  表外 id 拒绝（fail closed）、相等拒绝、非整数拒绝、字节只动 ``+0x30``/``+0x31``。
* **界面**：饰品 / 魂核 / 武器 / 防具 四个页签各有一行「稀有度」；范围随大类
  （魂核 3 vs 其它 4）；切记录不残留；界面写出的字节与引擎计划逐字节一致。
* **颜色**：``limits.RARITY_COLOR_BY_VALUE`` 对 0..5 完备、未知值返回空串；
  颜色只是显示，**不参与**上限校验（魂核写 4 依旧被拒）。

样本存档一律用 ``tests/support.py`` 的合成存档，物品种类从随包数据里查出来。
"""

from __future__ import annotations

import unittest
from unittest import mock

from nioh3_equipment_affix_editor import editor, equipmentdb, limits, records, ui
from nioh3_equipment_affix_editor.affixdb import AffixDb, ItemDb, load_soul_catalog
from nioh3_equipment_affix_editor.editor import (
    EditorError,
    apply_rarity_edits,
    plan_rarity_edit,
)
from tests import support
from tests.test_ui import TK_AVAILABLE, TK_ERROR, UiTestCase

ITEM_DB = equipmentdb.load_equipment_item_db()
LEGACY = support.legacy_layout()

#: 一个**真实**表外 id：合成记录里常见的 id，不在统一物品总目录里。
OUTSIDE_ID = 0x4001


def item_of(small: str, big: str) -> equipmentdb.EquipmentItem:
    for entry in ITEM_DB.all():
        if (entry.small == small and entry.big == big
                and entry.item_id not in records.SCROLL_TYPES):
            return entry
    raise AssertionError(f"物品总目录里没有 {big}/{small}")


KATANA = item_of("刀", "武器")
CHEST = item_of("身体", "防具")
ACCESSORY = next(e for e in ITEM_DB.all() if e.big == "饰品")
SOUL = next(e for e in ITEM_DB.all() if e.big == "魂核")

MELEE = equipmentdb.load_pool(equipmentdb.POOL_MELEE)
MELEE_AFFIX = next(entry for entry in MELEE.db.all()
                   if not entry.is_fixed and not entry.is_star
                   and MELEE.tags_of(entry.effect_id))
SOUL_DB = AffixDb(load_soul_catalog())
SOUL_AFFIX = next(entry for entry in SOUL_DB.all() if not entry.is_fixed)
#: 模板记录的 metadata 取最普通的一档（0x40）：既不是固定词条也不是 ★。
PLAIN_META = 0x40


def equipment_record(item: equipmentdb.EquipmentItem, **kwargs) -> bytes:
    """一条能当**模板**用的武器记录：至少带一条本池真实词条（否则不算可用样本）。"""
    return support.build_record(
        record_type=item.item_id,
        effects=((MELEE_AFFIX.effect_id, MELEE_AFFIX.value, PLAIN_META),), **kwargs)


def soul_record(item_id: int, **kwargs) -> bytes:
    """一条能被魂核页签认出来的魂核记录（带一条魂核词条作为证据）。"""
    return support.build_record(
        record_type=item_id,
        effects=((SOUL_AFFIX.effect_id, SOUL_AFFIX.value, PLAIN_META),), **kwargs)

#: (名字, 记录 id, 上限) —— 四类各一条，上限取自 limits 而不是写死。
RARITY_CASES = (
    ("武器", KATANA.item_id, limits.rarity_cap("武器")),
    ("防具", CHEST.item_id, limits.rarity_cap("防具")),
    ("饰品", ACCESSORY.item_id, limits.rarity_cap("饰品")),
    ("魂核", SOUL.item_id, limits.rarity_cap("魂核")),
)


def save_with(item_id: int, *, rarity: int = 1, level: int = 150,
              plus: int = 0) -> bytes:
    return support.build_plain_save(records_by_slot={
        3: support.build_record(record_type=item_id, level=level, rarity=rarity,
                                plus=plus)})


def record_at(data: bytes, slot: int) -> bytes:
    base = LEGACY.anchor + slot * records.SCROLL_RECORD_SIZE
    return data[base:base + records.SCROLL_RECORD_SIZE]


def rarity_at(data: bytes, slot: int = 3) -> int:
    return records.record_rarity(record_at(data, slot))


class SingleSourceTests(unittest.TestCase):
    """上限 / 颜色都只有一份来源，re-export 必须是**同一个对象**。"""

    def test_the_level_cap_comes_from_limits(self) -> None:
        self.assertIs(records.MAX_ITEM_LEVEL, limits.LEVEL_CAP)
        self.assertIs(records.MIN_ITEM_LEVEL, limits.MIN_LEVEL)
        self.assertIs(records.MAX_RECORD_PLUS, limits.DEFAULT_PLUS_CAP)
        self.assertIs(equipmentdb.DOCUMENTED_MAX_LEVEL, limits.LEVEL_CAP)
        self.assertIs(equipmentdb.DOCUMENTED_MAX_PLUS, limits.DEFAULT_PLUS_CAP)

    def test_the_two_big_tables_are_the_limits_objects(self) -> None:
        self.assertIs(equipmentdb.PLUS_CAP_BY_BIG, limits.PLUS_CAP_BY_BIG)
        self.assertIs(equipmentdb.RARITY_CAP_BY_BIG, limits.RARITY_CAP_BY_BIG)
        self.assertEqual(limits.RARITY_CAP_BY_BIG,
                         {"武器": 4, "防具": 4, "饰品": 4, "魂核": 3, "绘卷": 4})
        self.assertEqual(limits.PLUS_CAP_BY_BIG,
                         {"武器": 30, "防具": 30, "饰品": 30, "魂核": 15, "绘卷": None})

    def test_the_origin_text_names_the_current_rotation(self) -> None:
        origin = limits.describe_origin()
        self.assertIn("三周目", origin)
        self.assertIn("DLC2", origin)
        self.assertIn("橙色（5）在当前周目不可达", origin)

    def test_the_big_lookup_keeps_the_old_semantics(self) -> None:
        # 既有语义一字未改：表外大类 -> None（调用方退回文档值）；绘卷 -> None（没有 +値）。
        self.assertIsNone(equipmentdb.plus_cap_for_big("表外大类"))
        self.assertIsNone(equipmentdb.plus_cap_for_big("绘卷"))
        self.assertEqual(equipmentdb.plus_cap_for_big("魂核"), 15)
        self.assertIsNone(equipmentdb.rarity_cap_for_big("表外大类"))
        self.assertEqual(equipmentdb.rarity_cap_for_big("魂核"), 3)
        self.assertEqual(equipmentdb.rarity_cap_for_big("绘卷"), 4)


class RarityColorTests(unittest.TestCase):
    """颜色表：0..5 完备、与名表档数一致、未知值 fail-soft、不参与上限。"""

    def test_every_rarity_index_has_a_colour(self) -> None:
        self.assertEqual(len(limits.RARITY_COLOR_BY_VALUE),
                         len(records.RARITY_NAMES))
        for value in range(len(records.RARITY_NAMES)):
            with self.subTest(value=value):
                self.assertTrue(limits.rarity_color_name(value))

    def test_the_mapping_is_the_game_one(self) -> None:
        self.assertEqual(limits.rarity_color_name(0), "白色")
        self.assertEqual(limits.rarity_color_name(1), "黄色")
        self.assertEqual(limits.rarity_color_name(2), "蓝色")
        self.assertEqual(limits.rarity_color_name(3), "紫色")
        self.assertEqual(limits.rarity_color_name(4), "绿色")
        self.assertEqual(limits.rarity_color_name(5), "橙色")

    def test_an_unknown_value_is_an_empty_string_not_an_error(self) -> None:
        for value in (7, -1, 99, None, "4", 3.0, True):
            with self.subTest(value=value):
                self.assertEqual(limits.rarity_color_name(value), "")

    def test_the_ui_helper_and_label_keep_the_existing_name(self) -> None:
        self.assertEqual(ui.rarity_color_of(4), "绿色")
        self.assertEqual(ui.rarity_color_of(7), "")
        # 颜色只是追加，既有游戏稀有度名一个字都不少。
        self.assertEqual(ui.rarity_label(4, "神器"), "神器（绿色）")
        self.assertEqual(ui.rarity_label(3, "特大名器"), "特大名器（紫色）")
        self.assertEqual(ui.rarity_label(9, "未知"), "未知")
        for value, name in enumerate(records.RARITY_NAMES):
            with self.subTest(value=value):
                self.assertIn(name, ui.rarity_label(value, name))


class RarityEngineTests(unittest.TestCase):
    """``plan_rarity_edit`` / ``apply_rarity_edits``：四类 x 边界与拒绝路径。"""

    def setUp(self) -> None:
        self.affix_db = AffixDb()

    def _plan(self, data: bytes, rarity: int):
        return plan_rarity_edit(data, 3, rarity, affix_db=self.affix_db,
                                layout=LEGACY)

    def test_the_cap_is_taken_per_big_class(self) -> None:
        for name, item_id, cap in RARITY_CASES:
            with self.subTest(big=name):
                data = save_with(item_id)
                plan = self._plan(data, cap)
                self.assertEqual(plan.new_value, cap)
                self.assertEqual(plan.big, name)
                self.assertEqual(rarity_at(apply_rarity_edits(data, [plan])), cap)

    def test_one_above_the_cap_is_refused_with_the_evidence(self) -> None:
        for name, item_id, cap in RARITY_CASES:
            with self.subTest(big=name):
                data = save_with(item_id)
                with self.assertRaises(EditorError) as caught:
                    self._plan(data, cap + 1)
                message = str(caught.exception)
                self.assertIn(f"稀有度必须在 0..{cap} 之间", message)
                self.assertIn(name, message)
                self.assertIn("三周目", message)  # 依据：上限来历
                self.assertIn("DLC2", message)

    def test_a_soul_core_stops_at_three_even_though_four_has_a_colour(self) -> None:
        """颜色 != 上限：4 有颜色名，魂核照样拒绝。"""
        self.assertEqual(limits.rarity_color_name(4), "绿色")
        data = save_with(SOUL.item_id)
        with self.assertRaises(EditorError) as caught:
            self._plan(data, 4)
        self.assertIn("稀有度必须在 0..3 之间", str(caught.exception))
        self.assertEqual(rarity_at(data), 1)

    def test_writing_zero_is_allowed(self) -> None:
        data = save_with(KATANA.item_id, rarity=3)
        plan = self._plan(data, 0)
        self.assertEqual(plan.old_value, 3)
        self.assertEqual(rarity_at(apply_rarity_edits(data, [plan])), 0)

    def test_the_same_value_is_refused(self) -> None:
        data = save_with(KATANA.item_id, rarity=2)
        with self.assertRaises(EditorError) as caught:
            self._plan(data, 2)
        self.assertIn("已经是 2", str(caught.exception))

    def test_a_value_outside_the_table_is_refused(self) -> None:
        """表外 id：宁可拒绝也不猜一个上限（fail closed）。"""
        data = save_with(OUTSIDE_ID)
        with self.assertRaises(EditorError) as caught:
            self._plan(data, 2)
        message = str(caught.exception)
        self.assertIn("物品总目录", message)
        self.assertIn("无法确定稀有度上限", message)
        self.assertIn(f"{OUTSIDE_ID:#06x}", message)
        self.assertEqual(data, save_with(OUTSIDE_ID), "拒绝时不得改动任何字节")

    def test_a_non_integer_is_refused(self) -> None:
        data = save_with(KATANA.item_id)
        for value in ("2", 2.0, None, True):
            with self.subTest(value=value):
                with self.assertRaises(EditorError) as caught:
                    self._plan(data, value)
                self.assertIn("整数", str(caught.exception))

    def test_the_upper_four_bits_and_the_neighbour_byte_are_preserved(self) -> None:
        """只动 ``+0x30`` 的低 4 位：高 4 位与 ``+0x31`` 原样保留（写非 0 时）。"""
        record = bytearray(support.build_record(record_type=KATANA.item_id, rarity=1))
        record[records.RECORD_RARITY_OFFSET] = 0xA1
        record[records.RECORD_RARITY_HIGH_OFFSET] = 0xB7
        data = support.build_plain_save(records_by_slot={3: bytes(record)})
        plan = self._plan(data, 3)
        out = apply_rarity_edits(data, [plan])
        base = LEGACY.anchor + 3 * records.SCROLL_RECORD_SIZE
        self.assertEqual(out[base + records.RECORD_RARITY_OFFSET], 0xA3)
        self.assertEqual(out[base + records.RECORD_RARITY_HIGH_OFFSET], 0xB7)
        self.assertEqual(rarity_at(out), 3)

    def test_writing_zero_clears_the_fallback_nibble(self) -> None:
        """写 0 必须同时清 ``+0x31`` 的低 4 位，否则读回会退回旧值。"""
        record = bytearray(support.build_record(record_type=KATANA.item_id, rarity=1))
        record[records.RECORD_RARITY_OFFSET] = 0xA0
        record[records.RECORD_RARITY_HIGH_OFFSET] = 0xB4
        self.assertEqual(records.record_rarity(bytes(record)), 4,
                         "0 会退回 +0x31 —— 这正是写 0 要清它的原因")
        data = support.build_plain_save(records_by_slot={3: bytes(record)})
        plan = self._plan(data, 0)
        out = apply_rarity_edits(data, [plan])
        base = LEGACY.anchor + 3 * records.SCROLL_RECORD_SIZE
        self.assertEqual(out[base + records.RECORD_RARITY_HIGH_OFFSET], 0xB0)
        self.assertEqual(rarity_at(out), 0)

    def test_the_plan_and_the_apply_produce_the_same_bytes(self) -> None:
        data = save_with(KATANA.item_id, rarity=1)
        plan = self._plan(data, 4)
        base = LEGACY.anchor + 3 * records.SCROLL_RECORD_SIZE
        expected = (data[:base]
                    + records.patch_record_rarity(record_at(data, 3), 4)
                    + data[base + records.SCROLL_RECORD_SIZE:])
        self.assertEqual(apply_rarity_edits(data, [plan]), expected)
        # 计划本身的自检就是调记录层写入器做的（与等级 / +値 同一风格）。
        self.assertEqual(rarity_at(expected), 4)

    def test_only_the_two_rarity_bytes_differ(self) -> None:
        """与模板逐字节比对：差异集合必须是 ``+0x30``（写 0 时再加 ``+0x31``）。"""
        for new_value in (4, 0):
            with self.subTest(value=new_value):
                record = bytearray(support.build_record(record_type=KATANA.item_id,
                                                        rarity=1))
                record[records.RECORD_RARITY_OFFSET] = 0x51
                record[records.RECORD_RARITY_HIGH_OFFSET] = 0x62
                data = support.build_plain_save(records_by_slot={3: bytes(record)})
                plan = self._plan(data, new_value)
                out = apply_rarity_edits(data, [plan])
                base = LEGACY.anchor + 3 * records.SCROLL_RECORD_SIZE
                diffs = {index - base for index in range(len(data))
                         if data[index] != out[index]}
                expected = {records.RECORD_RARITY_OFFSET}
                if new_value == 0:
                    expected.add(records.RECORD_RARITY_HIGH_OFFSET)
                self.assertEqual(diffs, expected)

    def test_an_empty_plan_list_is_refused(self) -> None:
        with self.assertRaises(EditorError):
            apply_rarity_edits(save_with(KATANA.item_id), [])

    def test_a_bad_record_index_is_refused(self) -> None:
        data = save_with(KATANA.item_id)
        for index in (-1, LEGACY.slot_count, "3"):
            with self.subTest(index=index):
                with self.assertRaises(EditorError):
                    plan_rarity_edit(data, index, 4, affix_db=self.affix_db,
                                     layout=LEGACY)

    def test_an_empty_slot_is_refused(self) -> None:
        data = support.build_plain_save(records_by_slot={})
        with self.assertRaises(EditorError):
            plan_rarity_edit(data, 3, 4, affix_db=self.affix_db, layout=LEGACY)

    def test_a_catalog_without_big_classes_is_refused_not_crashed(self) -> None:
        """传错目录（饰品页签的 ItemDb 没有大类字段）也必须 fail closed，而不是 AttributeError。"""
        data = save_with(ACCESSORY.item_id)
        accessory_db = ItemDb.best_effort()
        self.assertIsNotNone(accessory_db.lookup(ACCESSORY.item_id),
                             "这条记录在饰品目录里查得到（所以拒绝只能来自「没有大类」）")
        for wrong_db in (accessory_db, ItemDb([])):
            with self.subTest(db=type(wrong_db).__name__):
                with self.assertRaises(EditorError) as caught:
                    plan_rarity_edit(data, 3, 2, affix_db=self.affix_db,
                                     layout=LEGACY, item_db=wrong_db)
                self.assertIn("无法确定稀有度上限", str(caught.exception))

    def test_the_equipment_item_db_parameter_wins(self) -> None:
        """``equipment_item_db=`` 是显式口径：给它表外目录同样 fail closed。"""
        data = save_with(KATANA.item_id)
        empty = equipmentdb.EquipmentItemDb([])
        with self.assertRaises(EditorError):
            plan_rarity_edit(data, 3, 4, affix_db=self.affix_db, layout=LEGACY,
                             equipment_item_db=empty)

    def test_the_description_carries_the_colour(self) -> None:
        data = save_with(SOUL.item_id, rarity=1)
        plan = self._plan(data, 3)
        text = plan.describe()
        self.assertIn("稀有度", text)
        self.assertIn("黄色", text)
        self.assertIn("紫色", text)
        self.assertIn("魂核", text)

    def test_class_limits_carry_the_rarity_cap(self) -> None:
        soul = editor.class_limits_for_record(SOUL.item_id)
        self.assertEqual(soul.max_rarity, 3)
        self.assertTrue(soul.rarity_by_big_table)
        self.assertIn("limits.RARITY_CAP_BY_BIG", soul.describe())
        katana = editor.class_limits_for_record(KATANA.item_id)
        self.assertEqual(katana.max_rarity, 4)
        # 既有行为没被改变：饰品不适用按类别取上限，仍然返回 None。
        self.assertIsNone(editor.class_limits_for_record(OUTSIDE_ID))


class CreationRarityTests(unittest.TestCase):
    """无中生有（武器 / 防具）的稀有度校验改成按大类上限。"""

    def setUp(self) -> None:
        # 模板（同种类样本）放在 #3，新件写进 #0 这个空槽。
        self.data = support.build_plain_save(records_by_slot={
            3: equipment_record(KATANA, level=150, rarity=1)})
        self.layout = records.locate_layout(self.data)
        self.item_db = ITEM_DB
        self.pools = {key: equipmentdb.load_pool(key) for key in
                      (equipmentdb.POOL_MELEE, equipmentdb.POOL_RANGED,
                       equipmentdb.POOL_ARMOR)}

    def _plan(self, *, level: int = 160, **kwargs):
        return editor.plan_create_equipment(
            self.data, record_type=KATANA.item_id, level=level, effects=(),
            item_db=self.item_db, pools=self.pools, layout=self.layout, **kwargs)

    def test_creating_with_the_cap_is_allowed_and_one_above_is_refused(self) -> None:
        cap = limits.rarity_cap("武器")
        made = self._plan(rarity=cap)
        self.assertEqual(made.rarity, cap)
        with self.assertRaises(editor.EquipmentCreationError) as caught:
            self._plan(rarity=cap + 1)
        self.assertIn("稀有度", str(caught.exception))
        self.assertIn(f"0..{cap}", str(caught.exception))

    def test_the_level_and_plus_caps_come_from_limits(self) -> None:
        """等级 / +値 的校验也改走 limits，不再留一份重复数值。"""
        best = limits.plus_cap("武器")
        made = self._plan(level=limits.LEVEL_CAP, plus=best)
        self.assertEqual(made.level, limits.LEVEL_CAP)
        self.assertEqual(made.plus_value, best)
        with self.assertRaises(editor.EquipmentCreationError) as caught:
            self._plan(level=limits.LEVEL_CAP + 1)
        self.assertIn(str(limits.LEVEL_CAP), str(caught.exception))
        with self.assertRaises(editor.EquipmentCreationError) as caught:
            self._plan(plus=best + 1)
        self.assertIn("+値", str(caught.exception))


@unittest.skipUnless(TK_AVAILABLE, f"Tk unavailable ({TK_ERROR})")
class RarityUiTests(UiTestCase):
    """四个页签的稀有度入口：控件存在、范围随大类、切记录不残留、字节与引擎一致。"""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.item_db = equipmentdb.load_equipment_item_db()
        cls.affix_db = AffixDb()

    def _load(self, records_by_slot: dict[int, bytes]) -> bytes:
        """把合成记录装进窗口（不带目录证据，四条记录都会列出来）。

        ``known_ids`` 留空 = ``catalog_hits`` 为 ``None``，视图的
        ``is_accessory`` 也是 ``None``（旧口径：保留而不丢弃），所以合成 id 与真实 id
        一样能进饰品列表 —— 稀有度的上限判断走的是**统一物品总目录**，与这里无关。
        """
        data = support.build_plain_save(records_by_slot=records_by_slot)
        layout = records.locate_layout(data)
        self.app._populate_accessories(
            (data, ui.list_accessories(data, layout=layout), True, layout))
        return data

    def _load_souls(self, records_by_slot: dict[int, bytes]) -> bytes:
        data = self._load(records_by_slot)
        self.assertIn("3", self.app.soul_tree.get_children(),
                      "魂核测试夹具必须先被魂核页签认出来")
        return data

    def _equipment_tab(self, big: str) -> ui.EquipmentTab:
        return next(tab for tab in self.app.equipment_tabs if tab.big == big)

    # ---------------------------------------------------------- 控件存在
    def test_all_four_tabs_have_a_rarity_row(self) -> None:
        self.assertTrue(hasattr(self.app, "rarity_entry"))
        self.assertTrue(hasattr(self.app, "rarity_button"))
        self.assertEqual(self.app.rarity_button.cget("text"), "应用稀有度")
        self.assertTrue(hasattr(self.app, "soul_rarity_entry"))
        self.assertEqual(self.app.soul_rarity_button.cget("text"), "应用魂核稀有度")
        for tab in self.app.equipment_tabs:
            with self.subTest(tab=tab.big):
                self.assertTrue(hasattr(tab, "rarity_entry"))
                self.assertEqual(tab.rarity_button.cget("text"), "应用稀有度")

    def test_the_rarity_controls_start_greyed_out(self) -> None:
        self.assertIn("disabled", self.app.rarity_entry.state())
        self.assertIn("disabled", self.app.soul_rarity_entry.state())
        for tab in self.app.equipment_tabs:
            with self.subTest(tab=tab.big):
                self.assertIn("disabled", tab.rarity_entry.state())

    # ------------------------------------------------- 范围随大类（饰品 / 武器 / 防具）
    def test_the_accessory_row_shows_the_cap_of_the_catalogue_entry(self) -> None:
        self._load({3: support.build_record(record_type=ACCESSORY.item_id,
                                            level=150, rarity=2)})
        self.app.tree.selection_set("3")
        self.app._on_accessory_selected()
        self.assertEqual(self.app.rarity_var.get(), "2")
        self.assertNotIn("disabled", self.app.rarity_entry.state())
        self.assertIn("0..4", self.app.rarity_frame.cget("text"))
        self.assertIn("绿色", self.app.rarity_status_var.get())
        self.assertIn("橙色", self.app.rarity_status_var.get())

    def test_an_out_of_catalogue_accessory_greys_the_row_out(self) -> None:
        """合成 id（不在统一目录）走 fail-closed：置灰 + 说明原因，不猜上限。"""
        self._load({3: support.build_record(record_type=OUTSIDE_ID, level=150,
                                            rarity=2)})
        self.app.tree.selection_set("3")
        self.app._on_accessory_selected()
        self.assertEqual(self.app.rarity_var.get(), "")
        self.assertIn("disabled", self.app.rarity_entry.state())
        status = self.app.rarity_status_var.get()
        self.assertIn("物品总目录", status)
        self.assertIn("无法确定稀有度上限", status)

    def test_switching_accessories_resets_the_rarity_box(self) -> None:
        """切记录必须刷新稀有度框的值（这个坑刚修过，别再引入同类问题）。"""
        other = next(e for e in ITEM_DB.all()
                     if e.big == "饰品" and e.item_id != ACCESSORY.item_id)
        self._load({
            3: support.build_record(record_type=ACCESSORY.item_id, level=150, rarity=2),
            4: support.build_record(record_type=other.item_id, level=150, rarity=0),
        })
        self.app.tree.selection_set("3")
        self.app._on_accessory_selected()
        self.assertEqual(self.app.rarity_var.get(), "2")
        self.app.tree.selection_set("4")
        self.app._on_accessory_selected()
        self.assertEqual(self.app.rarity_var.get(), "0")
        self.app.tree.selection_set("3")
        self.app._on_accessory_selected()
        self.assertEqual(self.app.rarity_var.get(), "2")

    def test_decoration_refresh_does_not_leave_the_old_value(self) -> None:
        """先选中一条表内记录，再选中表外记录：框必须清空而不是留着上一条的值。"""
        self._load({
            3: support.build_record(record_type=ACCESSORY.item_id, level=150, rarity=2),
            4: support.build_record(record_type=OUTSIDE_ID, level=150, rarity=4),
        })
        self.app.tree.selection_set("3")
        self.app._on_accessory_selected()
        self.assertEqual(self.app.rarity_var.get(), "2")
        self.app.tree.selection_set("4")
        self.app._on_accessory_selected()
        self.assertEqual(self.app.rarity_var.get(), "")

    # ------------------------------------------------------------ 魂核
    def test_the_soul_row_shows_three_as_the_cap(self) -> None:
        self._load_souls({3: soul_record(SOUL.item_id, level=150, rarity=1)})
        self.app.soul_tree.selection_set("3")
        self.app._on_soul_selected()
        self.assertEqual(self.app.soul_rarity_var.get(), "1")
        self.assertNotIn("disabled", self.app.soul_rarity_entry.state())
        self.assertIn("0..3", self.app.soul_rarity_status_var.get())
        self.assertIn("魂核", self.app.soul_rarity_status_var.get())
        self.assertIn("橙色", self.app.soul_rarity_status_var.get())

    def test_switching_soul_cores_resets_the_rarity_box(self) -> None:
        other = next(e for e in ITEM_DB.all()
                     if e.big == "魂核" and e.item_id != SOUL.item_id)
        self._load_souls({
            3: soul_record(SOUL.item_id, level=150, rarity=3),
            4: soul_record(other.item_id, level=150, rarity=1)})
        self.app.soul_tree.selection_set("3")
        self.app._on_soul_selected()
        self.assertEqual(self.app.soul_rarity_var.get(), "3")
        self.app.soul_tree.selection_set("4")
        self.app._on_soul_selected()
        self.assertEqual(self.app.soul_rarity_var.get(), "1")

    # ------------------------------------------------------- 武器 / 防具
    def test_the_equipment_rows_show_their_own_cap_and_reset_on_switch(self) -> None:
        data = self._load({
            3: support.build_record(record_type=KATANA.item_id, level=150, rarity=4),
            5: support.build_record(record_type=CHEST.item_id, level=150, rarity=2),
        })
        weapon = self._equipment_tab("武器")
        armor = self._equipment_tab("防具")
        weapon.tree.selection_set("3")
        weapon._on_selected()
        self.assertEqual(weapon.rarity_var.get(), "4")
        self.assertIn("0..4", weapon.rarity_frame.cget("text"))
        self.assertNotIn("disabled", weapon.rarity_entry.state())
        armor.tree.selection_set("5")
        armor._on_selected()
        self.assertEqual(armor.rarity_var.get(), "2")
        # 切回原记录：值跟着回来，而不是留着防具那条。
        weapon.tree.selection_set("3")
        weapon._on_selected()
        self.assertEqual(weapon.rarity_var.get(), "4")
        self.assertIsNotNone(data)

    def test_a_tab_resets_the_rarity_box_when_the_selection_is_dropped(self) -> None:
        self._load({3: support.build_record(record_type=KATANA.item_id, level=150,
                                            rarity=4)})
        tab = self._equipment_tab("武器")
        tab.tree.selection_set("3")
        tab._on_selected()
        self.assertEqual(tab.rarity_var.get(), "4")
        tab.populate(())
        self.assertEqual(tab.rarity_var.get(), "")
        self.assertIn("disabled", tab.rarity_entry.state())

    # ------------------------------------------- 界面写入 == 引擎计划（逐字节）
    def test_the_accessory_write_matches_the_engine_plan(self) -> None:
        data = self._load({3: support.build_record(record_type=ACCESSORY.item_id,
                                                   level=150, rarity=1)})
        self.app.tree.selection_set("3")
        self.app._on_accessory_selected()
        self.app.rarity_var.set("4")
        with mock.patch.object(ui.messagebox, "askokcancel", return_value=True), \
                mock.patch.object(ui.messagebox, "showerror") as shown:
            self.app.apply_rarity_to_selection()
        self.assertFalse(shown.called, shown.call_args)
        plan = plan_rarity_edit(data, 3, 4, affix_db=self.app.affix_db,
                                layout=self.app.layout)
        self.assertEqual(self.app.decrypted, apply_rarity_edits(data, [plan]))
        self.assertEqual(self.app.rarity_var.get(), "4")

    def test_the_equipment_write_matches_the_engine_plan(self) -> None:
        data = self._load({3: support.build_record(record_type=KATANA.item_id,
                                                   level=150, rarity=1)})
        tab = self._equipment_tab("武器")
        tab.tree.selection_set("3")
        tab._on_selected()
        tab.rarity_var.set("4")
        with mock.patch.object(ui.messagebox, "askokcancel", return_value=True), \
                mock.patch.object(ui.messagebox, "showerror") as shown:
            tab.apply_rarity()
        self.assertFalse(shown.called, shown.call_args)
        plan = plan_rarity_edit(data, 3, 4, affix_db=self.app.affix_db,
                                layout=self.app.layout)
        self.assertEqual(self.app.decrypted, apply_rarity_edits(data, [plan]))
        self.assertEqual(tab.rarity_var.get(), "4")

    def test_the_soul_write_matches_the_engine_plan(self) -> None:
        data = self._load_souls({3: soul_record(SOUL.item_id, level=150, rarity=1)})
        self.app.soul_tree.selection_set("3")
        self.app._on_soul_selected()
        self.app.soul_rarity_var.set("3")
        with mock.patch.object(ui.messagebox, "askokcancel", return_value=True), \
                mock.patch.object(ui.messagebox, "showerror") as shown:
            self.app.apply_soul_rarity_to_selection()
        self.assertFalse(shown.called, shown.call_args)
        plan = plan_rarity_edit(data, 3, 3, affix_db=self.app.soul_db,
                                known_ids=self.app.soul_known_ids,
                                layout=self.app.soul_layout)
        self.assertEqual(self.app.decrypted, apply_rarity_edits(data, [plan]))
        self.assertEqual(self.app.soul_rarity_var.get(), "3")

    # ------------------------------------------------------------ 拒绝
    def test_a_refusal_shows_the_engine_reason_in_a_dialog(self) -> None:
        self._load_souls({3: soul_record(SOUL.item_id, level=150, rarity=1)})
        self.app.soul_tree.selection_set("3")
        self.app._on_soul_selected()
        self.app.soul_rarity_var.set("4")
        with mock.patch.object(ui.messagebox, "askokcancel", return_value=True), \
                mock.patch.object(ui.messagebox, "showerror") as shown:
            self.app.apply_soul_rarity_to_selection()
        self.assertTrue(shown.called)
        self.assertIn("稀有度必须在 0..3 之间", str(shown.call_args[0]))

    def test_a_non_integer_is_refused_without_touching_the_bytes(self) -> None:
        data = self._load({3: support.build_record(record_type=ACCESSORY.item_id,
                                                   level=150, rarity=1)})
        self.app.tree.selection_set("3")
        self.app._on_accessory_selected()
        self.app.rarity_var.set("四")
        with mock.patch.object(ui.messagebox, "askokcancel") as confirmed, \
                mock.patch.object(ui.messagebox, "showerror") as shown:
            self.app.apply_rarity_to_selection()
        # 非整数在弹确认框**之前**就被拦下：既不确认、也不写字节、也不报错对话框。
        self.assertFalse(confirmed.called)
        self.assertFalse(shown.called)
        self.assertEqual(self.app.decrypted, data)


if __name__ == "__main__":  # pragma: no cover - manual runs only
    unittest.main()
