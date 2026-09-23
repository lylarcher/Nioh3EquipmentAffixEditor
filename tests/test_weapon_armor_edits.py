"""P2：武器 / 防具记录的列出，以及两条新规则。

**覆盖范围**：本模块只测武器 / 防具这两条线 —— `list_equipment()`（含
`list_weapons()` / `list_armor()` 两个包装）与 `EquipmentView` / `EquipmentSlotView`
的槽位明细，以及规则 1（装备种类标签必须匹配）与规则 2（等级 / +値 上限）在这些记录
上的正反例、边界与「历史状态不拦」的口径。其它入口各有自己的测试模块：统一的列举入口
`editor.list_items()` 见 `tests/test_list_items.py`，「装备种类」标签的合并表
`equipmentdb.load_equipment_tags()` 见 `tests/test_equipment_tags.py`，这里不重复测。

**规则 1「装备种类标签必须匹配」**：词条自带的 ``equipment_tags`` 按 ``/`` 拆成
token 集合，与这件装备可接受的 token 集合（大类名 ``武器``/``防具`` + 小类
（武器的具体类型 ``弓``/``火枪``/``大炮``、防具的部位 ``手臂``…）+ 武器的近战/远程
归属）取交集 —— **有交集才允许**，完全没交集就拒绝（fail closed）。判定只作用于
这次真的要写入的 ``effect_id``，存档里本来就有的历史状态不会拦住无关改动。

**规则 2「等级 / +値 上限」**：两条口径都是用户口径，**观测值不等于游戏上限** ——
等级上限一律是文档值 180（``editor.LEVEL_CAP_BY_CLASS = False`` 是默认值，所以观测到
170 / 172 / 174 的 弓 / 大太刀 / 忍者防具足部 也照样能写 180，181 仍被拒）；``+値``
上限按**大类**查固定小表 ``equipmentdb.PLUS_CAP_BY_BIG``（武器 30、防具 30、饰品 30、
魂核 15），完全不看实测值 —— 忍刀这类观测到 25 的武器类别也能写 30。
``data/equipment_ranges.json`` 退到证据的位置（``ClassLimits.describe()`` 仍引用它的
样本数与出处，文件本身没动）。适用大类是 武器 / 防具 / 魂核，**饰品保持原样**（0..30）。

样本存档用 ``tests/support.py`` 的 ``build_plain_save`` / ``build_record`` 合成，
物品种类与词条都从随包的 P1 数据里查出来，不写死 id。
"""

from __future__ import annotations

import unittest
from unittest import mock

from nioh3_accessory_editor import editor, equipmentdb, records
from nioh3_accessory_editor.affixdb import AffixDb
from nioh3_accessory_editor.editor import (
    EditPlan,
    EditorError,
    LevelEditError,
    apply_equipment_edits,
    list_armor,
    list_equipment,
    list_weapons,
    plan_equipment_edits,
    plan_level_edit,
    plan_plus_edit,
)
from nioh3_accessory_editor.records import EffectSlot
from tests import support

ITEM_DB = equipmentdb.load_equipment_item_db()
MELEE = equipmentdb.load_pool(equipmentdb.POOL_MELEE)
RANGED = equipmentdb.load_pool(equipmentdb.POOL_RANGED)
ARMOR = equipmentdb.load_pool(equipmentdb.POOL_ARMOR)
CODES = editor.load_affix_category_codes()

STAR_BIT = editor.STAR_BIT
FIXED_BIT = 0x4000
#: 一件真实饰品（八尺琼勾玉[武士]）：用来证明饰品行为没有被改动。
ACCESSORY_ID = 0x4987


def item_of(small: str, big: str = "武器") -> equipmentdb.EquipmentItem:
    """物品总目录里指定小类的一个具体种类（跳过会被当成绘卷的类型 id）。"""
    for entry in ITEM_DB.all():
        if (entry.small == small and entry.big == big
                and entry.item_id not in records.SCROLL_TYPES):
            return entry
    raise AssertionError(f"物品总目录里没有 {big}/{small}")


def item_of_class_key(big: str, category: str, small: str) -> equipmentdb.EquipmentItem:
    """类别键恰好是 ``大类/中类/小类`` 的一个种类（小类重名时用它点名到具体类别）。

    例如「足部」在 武士防具（观测到 170）与 忍者防具（观测到 174）各有一类、
    「手臂」在两边也各有一类，:func:`item_of` 只能拿到目录里排在前面的那一个。
    """
    for entry in ITEM_DB.all():
        if (entry.big == big and entry.category == category and entry.small == small
                and entry.item_id not in records.SCROLL_TYPES):
            return entry
    raise AssertionError(f"物品总目录里没有 {big}/{category}/{small}")


def affix_tagged(pool: equipmentdb.EquipmentPool, *tokens: str, star: bool | None = None):
    """标签恰好等于 ``tokens`` 的一条词条（可要求 ★ / 非 ★），id 排序保证稳定。"""
    found = [entry for entry in pool.db.all()
             if pool.tags_of(entry.effect_id) == tuple(tokens)
             and (star is None or bool(entry.is_star) == star)]
    if not found:
        raise AssertionError(f"{pool.label} 表里没有标签 {tokens}（star={star}）的词条")
    return sorted(found, key=lambda entry: entry.effect_id)[0]


def two_affixes_of_one_category(pool: equipmentdb.EquipmentPool):
    """同一「种类」的两条可写词条（``其他`` 豁免，所以跳过它）。"""
    seen: dict[str, object] = {}
    for entry in pool.db.all():
        if entry.is_fixed or entry.category == "其他" or not pool.tags_of(entry.effect_id):
            continue
        first = seen.get(entry.category)
        if first is not None:
            return first, entry
        seen[entry.category] = entry
    raise AssertionError(f"{pool.label} 表里找不到同种类的两条词条")


KATANA = item_of("刀")
BOW = item_of("弓")
GUN = item_of("火枪")
GREAT_KATANA = item_of("大太刀")
NINJA_BLADE = item_of("忍刀")
ARM_PART = item_of("手臂", "防具")
LEG_PART = item_of("腿部", "防具")
SOUL_CORE = item_of("魂核", "魂核")
#: 观测上限低于 30 的类别（忍刀 +値 观测 25、忍者防具/手臂 观测 23）：用来证明
#: 「观测值不是上限」—— 它们照样能写 30。
NINJA_ARMOR_ARM = item_of_class_key("防具", "忍者防具", "手臂")
#: 观测等级低于 180 的类别（忍者防具/足部 观测 174，武士防具/足部 观测 170）。
NINJA_ARMOR_FOOT = item_of_class_key("防具", "忍者防具", "足部")
#: 观测等级低于 180 的三个类别：弓 观测 170、大太刀 观测 172、忍者防具/足部 观测 174。
LOW_OBSERVED_LEVEL_ITEMS = (BOW, GREAT_KATANA, NINJA_ARMOR_FOOT)


def save_with(slots: dict[int, bytes]) -> bytes:
    """一块只放指定记录槽的合成存档（槽号 -> 记录字节）。"""
    return support.build_plain_save(records_by_slot=slots)


def record(item: equipmentdb.EquipmentItem, *, level: int = 170, rarity: int = 4,
           plus: int = 0, effects=()) -> bytes:
    return support.build_record(record_type=item.item_id, level=level, rarity=rarity,
                                plus=plus, effects=effects)


def effect(entry, *, metadata: int = 0) -> tuple[int, int, int]:
    return (entry.effect_id, entry.value, metadata)


class ListingTestCase(unittest.TestCase):
    """一块合成存档：刀 / 弓 / 手臂甲 / 腿部甲 / 一件饰品 / 一把火枪。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.melee_affix = affix_tagged(MELEE, "近战", star=False)
        cls.bow_affix = affix_tagged(RANGED, "弓")
        cls.arm_affix = affix_tagged(ARMOR, "手臂")
        cls.fixed_melee = next(entry for entry in MELEE.db.all() if entry.is_fixed)
        cls.save = support.build_plain_save(records_by_slot={
            3: record(KATANA, level=170, effects=(effect(cls.melee_affix, metadata=0x1000),)),
            4: record(BOW, level=160, plus=7,
                      effects=(effect(cls.bow_affix, metadata=STAR_BIT),)),
            5: record(ARM_PART, level=150, rarity=5, plus=11),
            6: record(LEG_PART, level=150),
            7: record(GUN, level=170),
            8: support.build_record(record_type=ACCESSORY_ID, level=170, rarity=5),
        })
        cls.layout = records.locate_layout(cls.save)

    def views(self, **kwargs) -> dict[int, editor.EquipmentView]:
        return {view.slot_index: view
                for view in list_equipment(self.save, layout=self.layout, **kwargs)}


class EquipmentListingTests(ListingTestCase):
    def test_lists_only_weapons_and_armor(self) -> None:
        views = self.views()
        self.assertEqual(sorted(views), [3, 4, 5, 6, 7])
        for view in views.values():
            self.assertIn(view.big, ("武器", "防具"))

    def test_every_listed_kind_is_in_the_item_catalog(self) -> None:
        for view in self.views().values():
            with self.subTest(slot=view.slot_index):
                entry = ITEM_DB.lookup(view.record_type)
                self.assertIsNotNone(entry, "列出的种类必须能在物品总目录里查到")
                self.assertEqual(entry.item_id, view.item.item_id)
                self.assertEqual(view.item_label, entry.label)

    def test_an_accessory_is_never_listed_here(self) -> None:
        self.assertNotIn(ACCESSORY_ID,
                         {view.record_type for view in self.views().values()})

    def test_an_unknown_kind_is_never_listed_here(self) -> None:
        save = save_with({3: support.build_record(record_type=0x4001, level=170)})
        self.assertEqual(list_equipment(save, layout=records.locate_layout(save)), ())

    def test_big_filter_and_the_two_wrappers(self) -> None:
        self.assertEqual(sorted(self.views(big="武器")), [3, 4, 7])
        self.assertEqual(sorted(self.views(big="防具")), [5, 6])
        self.assertEqual(
            [view.slot_index for view in list_weapons(self.save, layout=self.layout)],
            [3, 4, 7])
        self.assertEqual(
            [view.slot_index for view in list_armor(self.save, layout=self.layout)],
            [5, 6])

    def test_a_view_carries_level_plus_rarity_and_pool(self) -> None:
        views = self.views()
        self.assertEqual((views[3].level, views[3].plus_value, views[3].pool),
                         (170, 0, equipmentdb.POOL_MELEE))
        self.assertEqual((views[4].level, views[4].plus_value, views[4].pool),
                         (160, 7, equipmentdb.POOL_RANGED))
        self.assertEqual((views[5].plus_value, views[5].pool),
                         (11, equipmentdb.POOL_ARMOR))
        self.assertEqual(views[4].rarity_name, records.RARITY_NAMES[4])
        self.assertEqual(views[3].school, "武士")
        self.assertEqual(views[4].item_name, BOW.name)

    def test_acceptable_tokens_follow_the_item(self) -> None:
        views = self.views()
        self.assertEqual(views[3].acceptable_tokens, frozenset({"武器", "刀", "近战"}))
        self.assertEqual(views[4].acceptable_tokens, frozenset({"武器", "弓", "远程"}))
        self.assertEqual(views[5].acceptable_tokens, frozenset({"防具", "手臂"}))

    def test_catalog_hits_count_this_items_own_pool(self) -> None:
        views = self.views()
        self.assertEqual(views[3].catalog_hits, 1)
        self.assertEqual(views[4].catalog_hits, 1)
        self.assertEqual(views[7].catalog_hits, 0)  # 空武器：没有本池词条

    def test_slot_details_give_id_name_fixed_star_category_and_value(self) -> None:
        view = self.views()[3]
        slots = view.slots()
        self.assertEqual(len(slots), records.EFFECT_COUNT)
        first = slots[0]
        self.assertEqual(first.effect_id, self.melee_affix.effect_id)
        self.assertEqual(first.name, self.melee_affix.name)
        self.assertEqual(first.category, self.melee_affix.category)
        self.assertEqual(first.value, self.melee_affix.value)
        self.assertFalse(first.is_fixed)
        self.assertFalse(first.is_star)
        self.assertEqual(first.equipment_tags, ("近战",))
        self.assertTrue(first.is_occupied)
        empty = slots[1]
        self.assertTrue(empty.is_empty)
        self.assertFalse(empty.is_occupied)
        self.assertEqual(empty.name, "(空)")

    def test_a_star_slot_is_marked_as_star(self) -> None:
        slot = self.views()[4].slots()[0]
        self.assertTrue(slot.is_star)
        self.assertEqual(slot.equipment_tags, ("弓",))
        self.assertFalse(slot.is_fixed)

    def test_describe_effects_has_a_header_and_one_line_per_slot(self) -> None:
        lines = self.views()[3].describe_effects()
        self.assertEqual(len(lines), records.EFFECT_COUNT + 1)
        self.assertIn("种类", lines[0])
        self.assertIn("等级 170", lines[0])
        self.assertIn(self.melee_affix.name, lines[1])
        self.assertIn("(空)", lines[2])

    def test_describe_item_uses_the_catalog_class_key(self) -> None:
        self.assertEqual(self.views()[3].describe_item(),
                         f"种类 {KATANA.label}（武器/武士武器/刀）")

    def test_slot_role_names_the_pool(self) -> None:
        view = self.views()[3]
        self.assertEqual(view.slot_role(0), "近战武器词条")
        self.assertEqual(view.slot_role(1), "空")
        self.assertEqual(self.views()[4].slot_role(0), "远程武器词条")

    def test_an_empty_record_is_still_listed_with_its_kind(self) -> None:
        """空武器（没有任何词条）也要列出来：种类 id 本身就是证据。"""
        views = self.views()
        self.assertEqual(views[7].occupied_effects, ())
        self.assertEqual(views[7].item_label, GUN.label)

    def test_the_views_are_ordered_by_record_number(self) -> None:
        self.assertEqual([view.slot_index
                          for view in list_equipment(self.save, layout=self.layout)],
                         sorted(self.views()))


class FixedSlotTests(ListingTestCase):
    """固定词条不可写：目录「同名固定」与存档固定位 ``0x4000`` 两种来源。"""

    def test_a_catalog_fixed_affix_is_fixed_and_never_editable(self) -> None:
        save = save_with({3: record(
            KATANA, effects=(effect(self.fixed_melee, metadata=0x40),))})
        layout = records.locate_layout(save)
        view = list_weapons(save, layout=layout)[0]
        self.assertTrue(view.slot_is_fixed(0))
        self.assertEqual(view.slot_role(0), "固定词条")
        self.assertEqual(view.fixed_slots(), frozenset({0}))
        with self.assertRaises(EditorError) as caught:
            plan_equipment_edits(
                save, [{"record_index": 3, "slot_index": 0,
                        "effect_id": self.melee_affix.effect_id,
                        "value": self.melee_affix.value}], layout=layout)
        self.assertIn("固定词条不能修改", str(caught.exception))

    def test_a_save_side_fixed_bit_is_fixed_even_outside_the_catalog(self) -> None:
        save = save_with({3: record(
            KATANA, effects=((0x75B9, 20, FIXED_BIT),))})
        layout = records.locate_layout(save)
        view = list_weapons(save, layout=layout)[0]
        self.assertTrue(view.slot_is_fixed(0))
        with self.assertRaises(EditorError) as caught:
            plan_equipment_edits(
                save, [{"record_index": 3, "slot_index": 0,
                        "effect_id": self.melee_affix.effect_id,
                        "value": self.melee_affix.value}], layout=layout)
        self.assertIn("固定词条不能修改", str(caught.exception))

    def test_a_star_slot_with_the_fixed_bit_is_still_editable(self) -> None:
        """★ 不是固定词条（绘卷例外不适用于武器/防具），带固定位也按可改写。"""
        star = affix_tagged(MELEE, "近战", star=True)
        save = save_with({3: record(
            KATANA, effects=(effect(star, metadata=STAR_BIT | FIXED_BIT),))})
        layout = records.locate_layout(save)
        view = list_weapons(save, layout=layout)[0]
        self.assertTrue(view.slot_is_star(0))
        self.assertFalse(view.slot_is_fixed(0))
        self.assertFalse(editor._slot_is_fixed(view.effects[0], MELEE.db))
        plan = plan_equipment_edits(
            save, [{"record_index": 3, "slot_index": 0,
                    "effect_id": self.melee_affix.effect_id,
                    "value": self.melee_affix.value}], layout=layout)
        self.assertEqual(plan[0].after[0].effect_id, self.melee_affix.effect_id)


class EquipmentTagRuleTests(ListingTestCase):
    """规则 1：装备种类标签 token 必须有交集。"""

    def _plan(self, save, layout, record_index, slot_index, entry, *, value=None):
        return plan_equipment_edits(
            save, [{"record_index": record_index, "slot_index": slot_index,
                    "effect_id": entry.effect_id,
                    "value": entry.value if value is None else value}],
            layout=layout)

    def test_a_melee_affix_goes_onto_a_katana(self) -> None:
        target = next(entry for entry in MELEE.db.all()
                      if not entry.is_fixed and "近战" in MELEE.tags_of(entry.effect_id))
        patched = apply_equipment_edits(
            self.save, [{"record_index": 3, "slot_index": 1,
                         "effect_id": target.effect_id, "value": target.value}],
            layout=self.layout)
        view = next(view for view in list_weapons(patched, layout=self.layout)
                    if view.slot_index == 3)
        self.assertEqual(view.effects[1].effect_id, target.effect_id)
        # 除了目标槽所在记录，别的字节一个都不许变。
        changed = {index for index, (old, new) in enumerate(zip(self.save, patched))
                   if old != new}
        allowed = {view.offset + records.EFFECT_START + 1 * records.EFFECT_STRIDE + step
                   for step in range(records.EFFECT_STRIDE)}
        self.assertTrue(changed and changed <= allowed, sorted(changed))

    def test_a_melee_only_affix_is_refused_on_a_bow(self) -> None:
        with self.assertRaises(EditorError) as caught:
            self._plan(self.save, self.layout, 4, 1, self.melee_affix)
        message = str(caught.exception)
        self.assertIn(BOW.label, message)
        self.assertIn("远程武器", message)

    def test_a_bow_affix_goes_onto_a_bow_but_not_onto_a_gun(self) -> None:
        plan = self._plan(self.save, self.layout, 4, 1, self.bow_affix)
        self.assertEqual(plan[0].after[1].effect_id, self.bow_affix.effect_id)
        with self.assertRaises(EditorError) as caught:
            self._plan(self.save, self.layout, 7, 1, self.bow_affix)
        message = str(caught.exception)
        self.assertIn(GUN.label, message)
        self.assertIn("弓", message)
        self.assertIn("没有交集", message)
        self.assertIn("火枪", message)

    def test_an_arm_affix_is_refused_on_a_leg_piece(self) -> None:
        plan = self._plan(self.save, self.layout, 5, 1, self.arm_affix)
        self.assertEqual(plan[0].after[1].effect_id, self.arm_affix.effect_id)
        with self.assertRaises(EditorError) as caught:
            self._plan(self.save, self.layout, 6, 1, self.arm_affix)
        self.assertIn("没有交集", str(caught.exception))
        self.assertIn("腿部", str(caught.exception))

    def test_a_shared_tag_is_allowed_on_both_a_katana_and_an_arm_piece(self) -> None:
        """标签有交集就允许：``近战/手臂`` 对刀（近战）与手臂甲（手臂）都成立。"""
        shared = affix_tagged(MELEE, "近战", "手臂")
        self.assertTrue(editor.equipment_affix_allowed(MELEE, KATANA, shared.effect_id))
        self.assertTrue(editor.equipment_affix_allowed(ARMOR, ARM_PART, shared.effect_id))
        for record_index in (3, 5):
            with self.subTest(record_index=record_index):
                plan = self._plan(self.save, self.layout, record_index, 1, shared)
                self.assertEqual(plan[0].after[1].effect_id, shared.effect_id)

    def test_the_predicate_is_a_plain_token_intersection(self) -> None:
        self.assertTrue(editor.equipment_affix_allowed(
            MELEE, KATANA, records.EMPTY_EFFECT_ID), "清空槽不引入词条，一律放行")
        self.assertFalse(editor.equipment_affix_allowed(RANGED, BOW,
                                                        self.melee_affix.effect_id))
        self.assertFalse(editor.equipment_affix_allowed(RANGED, GUN,
                                                        self.bow_affix.effect_id))
        self.assertFalse(editor.equipment_affix_allowed(MELEE, KATANA, 0xDEADBEEF))

    def test_a_missing_tag_is_refused_not_ignored(self) -> None:
        """标签缺失时 fail closed：不能把"没标签"当成"哪儿都能写"。"""
        blind = equipmentdb.EquipmentPool(
            key=MELEE.key, db=MELEE.db, tags={}, path=MELEE.path)
        with self.assertRaises(EditorError) as caught:
            plan_equipment_edits(
                self.save, [{"record_index": 3, "slot_index": 1,
                             "effect_id": self.melee_affix.effect_id,
                             "value": self.melee_affix.value}],
                layout=self.layout, pools={MELEE.key: blind})
        self.assertIn("没有「装备种类」标签", str(caught.exception))

    def test_only_the_edited_slot_is_judged(self) -> None:
        """只在新引入违规时拒绝：历史状态里的错配不拦住别的改动。"""
        stray = affix_tagged(MELEE, "近战")  # 只标近战，本来不该出现在弓上
        save = save_with({4: record(
            BOW, effects=(effect(stray, metadata=0x1000),))})
        layout = records.locate_layout(save)
        # ① 改另一个槽：允许（错配是历史状态，不是这次引入的）
        plan = self._plan(save, layout, 4, 1, self.bow_affix)
        self.assertEqual(plan[0].after[1].effect_id, self.bow_affix.effect_id)
        # ② 只改历史槽的数值（没有新 effect_id）：也允许
        plan = plan_equipment_edits(
            save, [{"record_index": 4, "slot_index": 0, "value": 21}], layout=layout)
        self.assertEqual(plan[0].after[0].value, 21)
        # ③ 往第三个槽再写一个近战词条：这才是新引入的违规，拒绝
        with self.assertRaises(EditorError):
            self._plan(save, layout, 4, 2, stray)
        # ④ 清空历史槽：允许
        plan = plan_equipment_edits(
            save, [{"record_index": 4, "slot_index": 0,
                    "effect_id": records.EMPTY_EFFECT_ID}], layout=layout)
        self.assertTrue(plan[0].after[0].is_empty)

    def test_an_edit_on_a_non_weapon_record_is_refused(self) -> None:
        with self.assertRaises(EditorError) as caught:
            self._plan(self.save, self.layout, 8, 1, self.melee_affix)
        self.assertIn("不在当前存档的武器/防具记录中", str(caught.exception))

    def test_a_missing_record_is_refused(self) -> None:
        with self.assertRaises(EditorError) as caught:
            self._plan(self.save, self.layout, 99, 1, self.melee_affix)
        self.assertIn("不在当前存档的武器/防具记录中", str(caught.exception))

    def test_an_empty_edit_list_is_refused(self) -> None:
        with self.assertRaises(EditorError):
            plan_equipment_edits(self.save, [], layout=self.layout)


class ExistingRulesOnEquipmentTests(ListingTestCase):
    """既有规则在武器/防具上同样生效。"""

    def test_two_affixes_of_one_category_are_refused(self) -> None:
        first, second = two_affixes_of_one_category(MELEE)
        category = first.category
        save = save_with({3: record(KATANA, effects=(
            effect(first, metadata=CODES[category]),))})
        layout = records.locate_layout(save)
        with self.assertRaises(EditorError) as caught:
            plan_equipment_edits(
                save, [{"record_index": 3, "slot_index": 1,
                        "effect_id": second.effect_id, "value": second.value}],
                layout=layout)
        message = str(caught.exception)
        self.assertIn("每个种类只能有一个词条", message)
        self.assertIn(category, message)

    def test_a_pre_existing_duplicate_is_not_blocked(self) -> None:
        first, second = two_affixes_of_one_category(MELEE)
        code = CODES[first.category]
        save = save_with({3: record(KATANA, effects=(
            effect(first, metadata=code), effect(second, metadata=code)))})
        layout = records.locate_layout(save)
        plan = plan_equipment_edits(
            save, [{"record_index": 3, "slot_index": 2, "value": 12}], layout=layout)
        self.assertEqual(plan[0].after[2].value, 12)

    def test_a_new_category_duplicate_is_refused_end_to_end(self) -> None:
        first, second = two_affixes_of_one_category(MELEE)
        save = save_with({3: record(KATANA, effects=(
            effect(first, metadata=CODES[first.category]),))})
        layout = records.locate_layout(save)
        with self.assertRaises(EditorError) as caught:
            apply_equipment_edits(
                save, [{"record_index": 3, "slot_index": 1,
                        "effect_id": second.effect_id, "value": second.value}],
                layout=layout)
        self.assertIn("每个种类只能有一个词条", str(caught.exception))
        # 被拒绝之后原存档照样可用：换一个不引入重复的写法就能写成功。
        patched = apply_equipment_edits(
            save, [{"record_index": 3, "slot_index": 1, "value": 12}], layout=layout)
        self.assertNotEqual(patched, save)

    def test_the_category_code_travels_with_the_new_affix(self) -> None:
        target = next(entry for entry in MELEE.db.all()
                      if not entry.is_fixed and not entry.is_star)
        patched = apply_equipment_edits(
            self.save, [{"record_index": 3, "slot_index": 1,
                         "effect_id": target.effect_id, "value": target.value},
                        {"record_index": 3, "slot_index": 2,
                         "effect_id": records.EMPTY_EFFECT_ID}],
            layout=self.layout)
        view = next(view for view in list_weapons(patched, layout=self.layout)
                    if view.slot_index == 3)
        written = view.effects[1]
        self.assertEqual(written.metadata & editor.CATEGORY_CODE_MASK,
                         CODES[target.category])
        self.assertEqual(bool(written.metadata & STAR_BIT), bool(target.is_star))

    def test_one_grace_per_record_applies_to_equipment_plans(self) -> None:
        """一件一个恩宠/套装：规则是 db 无关的，武器/防具的计划同样被拒。"""
        grace_db = support.load_grace_table()
        graces = [entry.effect_id for entry in grace_db.grace_entries()[:2]]
        self.assertEqual(len(graces), 2)

        def slots(pairs):
            return tuple(EffectSlot(slot_index=index, prefix=0, effect_id=effect_id,
                                    value=0, metadata=0, tail_0=0, tail_1=0)
                         for index, effect_id in enumerate(pairs))

        plan = EditPlan(record_index=3, offset=0, edits=(),
                        before=slots([graces[0]]), after=slots(graces))
        with self.assertRaises(EditorError) as caught:
            editor.assert_single_grace((plan,), grace_db)
        self.assertIn("只能有一个恩宠", str(caught.exception))
        allowed = EditPlan(record_index=3, offset=0, edits=(),
                           before=slots([]), after=slots([graces[0]]))
        editor.assert_single_grace((allowed,), grace_db)

    def test_a_grace_id_is_only_accepted_on_the_records_own_grace_slot(self) -> None:
        """P5 反转的旧口径：恩宠/套装 id 现在能写进武器/防具 —— 但只能写在**该记录已有
        的那个恩宠槽**上（正例见 ``tests/test_equipment_grace.py``）。

        这条记录没有恩宠槽，所以「凭空给一个槽安上恩宠」仍然被拒：拒绝原因从
        「不在词条表」变成「没有恩宠/套装词条槽」，fail closed 没有放松。
        """
        grace_db = support.load_grace_table()
        grace_id = grace_db.grace_entries()[0].effect_id
        with self.assertRaises(EditorError) as caught:
            plan_equipment_edits(
                self.save, [{"record_index": 3, "slot_index": 1,
                             "effect_id": grace_id, "value": 0}],
                layout=self.layout, grace_db=grace_db)
        self.assertIn("没有恩宠/套装词条槽", str(caught.exception))
        # 不给恩宠名表时口径与 P2 完全一致（表外 id 一律拒绝），老调用方不受影响。
        with self.assertRaises(EditorError) as caught:
            plan_equipment_edits(
                self.save, [{"record_index": 3, "slot_index": 1,
                             "effect_id": grace_id, "value": 0}],
                layout=self.layout)
        self.assertIn("不能写到", str(caught.exception))
        self.assertIn("词条表", str(caught.exception))

    def test_the_grace_rule_is_wired_into_the_equipment_path(self) -> None:
        """端到端路径确实接了恩宠规则：用一个哨兵证明接线（而不是靠写不进的 id）。"""
        grace_db = support.load_grace_table()
        sentinel = EditorError("恩宠规则被调用了（测试哨兵）")
        with mock.patch.object(editor, "assert_single_grace",
                               side_effect=sentinel) as wired:
            with self.assertRaises(EditorError) as caught:
                plan_equipment_edits(
                    self.save, [{"record_index": 3, "slot_index": 1,
                                 "effect_id": self.melee_affix.effect_id,
                                 "value": self.melee_affix.value}],
                    layout=self.layout, grace_db=grace_db)
        self.assertIs(caught.exception, sentinel)
        self.assertEqual(wired.call_count, 1)
        self.assertIs(wired.call_args.args[1], grace_db)
        # 不给 grace_db 时这条规则不参与（与饰品路径同一个口径）。
        plan = plan_equipment_edits(
            self.save, [{"record_index": 3, "slot_index": 1,
                         "effect_id": self.melee_affix.effect_id,
                         "value": self.melee_affix.value}], layout=self.layout)
        self.assertEqual(plan[0].after[1].effect_id, self.melee_affix.effect_id)


class ClassCapTests(unittest.TestCase):
    """规则 2：+値 上限按大类固定表、等级默认 180，饰品保持原样。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.affix_db = AffixDb()

    def _save(self, item, *, level: int = 160, plus: int = 0):
        save = save_with({3: record(item, level=level, plus=plus)})
        return save, records.locate_layout(save)

    def test_weapon_and_armor_plus_30_is_allowed_and_31_refused(self) -> None:
        for item in (KATANA, ARM_PART, LEG_PART):
            with self.subTest(item=item.name):
                self.assertEqual(equipmentdb.plus_cap_for_big(item.big), 30)
                limits = editor.class_limits_for_record(item.item_id)
                self.assertEqual(limits.max_plus, 30)
                self.assertTrue(limits.plus_by_big_table)
                save, layout = self._save(item)
                plan = plan_plus_edit(save, 3, 30, affix_db=self.affix_db, layout=layout)
                self.assertEqual(plan.new_value, 30)
                with self.assertRaises(EditorError) as caught:
                    plan_plus_edit(save, 3, 31, affix_db=self.affix_db, layout=layout)
                self.assertIn("+值必须在", str(caught.exception))
                self.assertIn("30", str(caught.exception))

    def test_a_class_observed_below_30_can_still_be_written_to_30(self) -> None:
        """观测值不是上限：忍刀观测到 25、忍者防具/手臂 23，照样能写 30（31 才拒）。"""
        for item in (NINJA_BLADE, NINJA_ARMOR_ARM):
            with self.subTest(item=item.name):
                self.assertLess(equipmentdb.equipment_caps(item)[1], 30,
                                "这个类别的观测值本来就低于 30（这正是本条要证明的）")
                limits = editor.class_limits_for_record(item.item_id)
                self.assertEqual(limits.max_plus, 30)
                self.assertTrue(limits.plus_by_big_table)
                save, layout = self._save(item)
                plan = plan_plus_edit(save, 3, 30, affix_db=self.affix_db, layout=layout)
                self.assertEqual(plan.new_value, 30)
                with self.assertRaises(EditorError) as caught:
                    plan_plus_edit(save, 3, 31, affix_db=self.affix_db, layout=layout)
                self.assertIn("+值必须在", str(caught.exception))
                self.assertIn("30", str(caught.exception))

    def test_the_plus_gate_itself_stops_at_the_big_table_value(self) -> None:
        """把扁平上限临时抬高，类别闸门本身就露出来了：忍刀封在 30（不是观测的 25）。

        否则武器 / 防具的 31 总是被扁平的 0..30 先拒掉，看不出类别闸门还在不在。
        """
        save, layout = self._save(NINJA_BLADE)
        with mock.patch.object(records, "MAX_RECORD_PLUS", 40):
            plan = plan_plus_edit(save, 3, 30, affix_db=self.affix_db, layout=layout)
            self.assertEqual(plan.new_value, 30)
            with self.assertRaises(EditorError) as caught:
                plan_plus_edit(save, 3, 31, affix_db=self.affix_db, layout=layout)
        message = str(caught.exception)
        self.assertIn("+值必须在 0..30 之间", message)
        self.assertIn("超过该大类的 +値 上限 30", message)
        self.assertIn(equipmentdb.equipment_class_key(NINJA_BLADE), message)

    def test_a_soul_core_plus_15_is_allowed_and_16_refused(self) -> None:
        self.assertEqual(equipmentdb.plus_cap_for_big("魂核"), 15)
        limits = editor.class_limits_for_record(SOUL_CORE.item_id)
        self.assertEqual(limits.max_plus, 15)
        self.assertTrue(limits.plus_by_big_table)
        save, layout = self._save(SOUL_CORE)
        plan = plan_plus_edit(save, 3, 15, affix_db=self.affix_db, layout=layout)
        self.assertEqual(plan.new_value, 15)
        with self.assertRaises(EditorError) as caught:
            plan_plus_edit(save, 3, 16, affix_db=self.affix_db, layout=layout)
        message = str(caught.exception)
        self.assertIn("+值必须在 0..15 之间", message)
        self.assertIn("魂核", message)
        self.assertIn("超过该大类的 +値 上限", message)
        # 拒绝信息里的依据仍然指向那份实测范围表（它只是证据，不再是上限）。
        self.assertIn("equipment_ranges.json", message)

    def test_an_accessory_keeps_the_flat_cap(self) -> None:
        save, layout = self._save(ITEM_DB.lookup(ACCESSORY_ID))
        self.assertIsNone(editor.class_limits_for_record(ACCESSORY_ID),
                          "饰品不适用按类别取上限，行为必须保持原样")
        plan = plan_plus_edit(save, 3, 30, affix_db=self.affix_db, layout=layout)
        self.assertEqual(plan.new_value, 30)
        with self.assertRaises(EditorError):
            plan_plus_edit(save, 3, 31, affix_db=self.affix_db, layout=layout)

    def test_an_unknown_kind_has_no_class_limits(self) -> None:
        self.assertIsNone(editor.class_limits_for_record(0x4001))
        save, layout = self._save(ITEM_DB.lookup(ACCESSORY_ID))
        plan = plan_plus_edit(save, 3, 30, affix_db=self.affix_db, layout=layout)
        self.assertEqual(plan.new_value, 30)
        self.assertIsNone(editor.class_limits_for_record(0x4001, item_db=ITEM_DB))

    def test_the_level_cap_is_180_even_where_the_observation_is_lower(self) -> None:
        """默认口径：等级上限就是文档值 180（弓 / 大太刀 / 忍者防具足部 可 180、181 拒）。"""
        for item in LOW_OBSERVED_LEVEL_ITEMS:
            with self.subTest(item=item.name):
                self.assertLess(equipmentdb.equipment_caps(item)[0],
                                records.MAX_ITEM_LEVEL,
                                "这个类别的观测等级本来就低于 180")
                limits = editor.class_limits_for_record(item.item_id)
                self.assertEqual(limits.max_level, records.MAX_ITEM_LEVEL)
                self.assertFalse(limits.level_by_class)
                save, layout = self._save(item)
                plan = plan_level_edit(save, 3, records.MAX_ITEM_LEVEL,
                                       affix_db=self.affix_db, layout=layout)
                self.assertEqual(plan.new_level, records.MAX_ITEM_LEVEL)
                with self.assertRaises(LevelEditError) as caught:
                    plan_level_edit(save, 3, records.MAX_ITEM_LEVEL + 1,
                                    affix_db=self.affix_db, layout=layout)
                self.assertIn(str(records.MAX_ITEM_LEVEL), str(caught.exception))

    def test_turning_the_flag_on_uses_the_observed_level_cap(self) -> None:
        """只有显式把 LEVEL_CAP_BY_CLASS 设为 True，等级才按类别观测值封顶。"""
        for item in LOW_OBSERVED_LEVEL_ITEMS:
            observed = equipmentdb.equipment_caps(item)[0]
            with self.subTest(item=item.name):
                self.assertLess(observed, records.MAX_ITEM_LEVEL)
                with mock.patch.object(editor, "LEVEL_CAP_BY_CLASS", True):
                    limits = editor.class_limits_for_record(item.item_id)
                    self.assertEqual(limits.max_level, observed)
                    self.assertTrue(limits.level_by_class)
                    save, layout = self._save(item)
                    plan = plan_level_edit(save, 3, observed, affix_db=self.affix_db,
                                           layout=layout)
                    self.assertEqual(plan.new_level, observed)
                    with self.assertRaises(LevelEditError) as caught:
                        plan_level_edit(save, 3, observed + 1, affix_db=self.affix_db,
                                        layout=layout)
                message = str(caught.exception)
                self.assertIn(str(observed), message)
                self.assertIn("超过该类别的实测上限", message)

    def test_a_view_reports_the_caps_of_its_own_class(self) -> None:
        save, layout = self._save(KATANA)
        view = list_weapons(save, layout=layout)[0]
        limits = view.limits()
        self.assertEqual((limits.max_level, limits.max_plus), (180, 30))
        self.assertEqual(limits.key, equipmentdb.equipment_class_key(KATANA))

    def test_a_soul_core_is_not_in_the_equipment_list(self) -> None:
        """魂核有自己的入口（+値 上限也不同），不混进武器/防具列表。"""
        save = save_with({3: record(SOUL_CORE)})
        layout = records.locate_layout(save)
        self.assertEqual(list_equipment(save, layout=layout), ())
        self.assertEqual(editor.class_limits_for_record(SOUL_CORE.item_id).max_plus, 15)

    def test_the_global_level_cap_still_applies(self) -> None:
        save, layout = self._save(KATANA, level=170)
        with self.assertRaises(LevelEditError) as caught:
            plan_level_edit(save, 3, records.MAX_ITEM_LEVEL + 1,
                            affix_db=self.affix_db, layout=layout)
        self.assertIn(str(records.MAX_ITEM_LEVEL), str(caught.exception))

    def test_the_observed_ranges_stay_evidence_only(self) -> None:
        """观测值低的两类都不再被当成上限：等级 180、+値 30，观测值只作证据。"""
        for item in (NINJA_BLADE, NINJA_ARMOR_FOOT):
            with self.subTest(item=item.name):
                observed_level, observed_plus = equipmentdb.equipment_caps(item)
                limits = editor.class_limits_for_record(item.item_id)
                self.assertGreaterEqual(limits.max_level, observed_level)
                self.assertGreaterEqual(limits.max_plus, observed_plus)
                self.assertEqual(limits.max_level, records.MAX_ITEM_LEVEL)
                self.assertEqual(limits.max_plus, records.MAX_RECORD_PLUS)

    def test_the_limits_say_where_the_number_came_from(self) -> None:
        limits = editor.class_limits_for_record(NINJA_BLADE.item_id)
        self.assertTrue(limits.measured)
        self.assertGreater(limits.samples, 0)
        self.assertEqual(limits.key, equipmentdb.equipment_class_key(NINJA_BLADE))
        text = limits.describe()
        # 证据：那张实测范围表（样本数 + 出处）还在拒绝信息里。
        self.assertIn("equipment_ranges.json", text)
        # 上限来源：等级 = 文档值 180，+値 = 大类固定表。
        self.assertIn("文档值", text)
        self.assertIn("equipmentdb.PLUS_CAP_BY_BIG", text)
        self.assertFalse(limits.level_by_class)
        self.assertTrue(limits.plus_by_big_table)

    def test_a_class_missing_from_the_range_table_falls_back_to_the_docs(self) -> None:
        """类别不在实测范围表里就退回文档值 180 / 30，绝不猜；+値 仍按大类固定表。"""
        empty = {"classes": {}}
        limits = editor.class_limits_for_record(KATANA.item_id, ranges=empty)
        self.assertFalse(limits.measured)
        self.assertEqual((limits.max_level, limits.max_plus),
                         (records.MAX_ITEM_LEVEL, records.MAX_RECORD_PLUS))
        self.assertFalse(limits.level_by_class)
        self.assertTrue(limits.plus_by_big_table)
        save, layout = self._save(KATANA)
        plan = plan_plus_edit(save, 3, 30, affix_db=self.affix_db, layout=layout,
                              ranges=empty)
        self.assertEqual(plan.new_value, 30)
        # 魂核的 15 来自大类固定表，与那张范围表在不在无关。
        soul = editor.class_limits_for_record(SOUL_CORE.item_id, ranges=empty)
        self.assertFalse(soul.measured)
        self.assertEqual((soul.max_level, soul.max_plus),
                         (records.MAX_ITEM_LEVEL, 15))


if __name__ == "__main__":  # pragma: no cover - manual runs only
    unittest.main()
