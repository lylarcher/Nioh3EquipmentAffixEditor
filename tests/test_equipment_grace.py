"""P5：武器 / 防具上的【恩宠 / 套装】可替换。

**口径**（与饰品的恩宠路径同一套语义，依据来自参考存档实测）：

* 恩宠/套装槽 = 该记录里当前存着恩宠名表 id 的那个槽。实测 497/497 武器、614/614 防具
  各**恰好一个**，都在第 5 槽；它们的种类码 100% 是 ``0x0c00``（=「其他」）。
* **只替换、不凭空新增**：记录里没有恩宠槽就拒绝 —— 否则 metadata 的 byte10
  （实测同一恩宠逐件不同）没有可参照的真实形状，那就是猜。
* 只写 ``effect_id`` 与 ``value``（值取恩宠名表的目录值），**metadata 原样保留**，
  与 :func:`editor.plan_grace_edit` 的做法一致。
* 恩宠槽**从来不算固定词条**：固定位（``0x4000``）不影响它。
* 非恩宠的表外 id 仍然一律拒绝；固定词条仍然不可写；一件仍然只能有一个恩宠/套装。
"""

from __future__ import annotations

import unittest

from nioh3_equipment_affix_editor import editor, equipmentdb, records
from nioh3_equipment_affix_editor.affixdb import GraceDb
from nioh3_equipment_affix_editor.editor import (
    EditorError,
    GraceEditError,
    apply_equipment_edits,
    apply_equipment_grace_edit,
    equipment_grace_availability,
    list_equipment,
    plan_equipment_edits,
    plan_equipment_grace_edit,
)
from tests import support

ITEM_DB = equipmentdb.load_equipment_item_db()
GRACE_DB = GraceDb.best_effort()
MELEE = equipmentdb.load_pool(equipmentdb.POOL_MELEE)
ARMOR = equipmentdb.load_pool(equipmentdb.POOL_ARMOR)

FIXED_BIT = 0x4000
#: 参考存档里恩宠/套装槽实测的 metadata 形状（种类码 = 0x0c00「其他」，byte2 逐件不同）。
GRACE_METADATA = 0x00020C00
#: 套装槽的 metadata 形状（byte9 = 0x4C，即族标签 0x0C | 专属/固定位 0x40）。
SET_METADATA = 0x00024C00

GRACES = sorted(GRACE_DB.all(), key=lambda entry: entry.effect_id)
#: 两条真正的「恩宠」（不含套装）：换恩宠用它们，避免和下面的套装条目撞 id。
BLESSINGS = [entry for entry in GRACES if entry.category in ("恩宠", "上位恩宠")]
GRACE_A, GRACE_B = BLESSINGS[0], BLESSINGS[1]
SET_ENTRY = next(entry for entry in GRACES
                 if entry.category in ("武士套装", "忍者套装")
                 and entry.effect_id not in (GRACE_A.effect_id, GRACE_B.effect_id))


def item_of(small: str, big: str = "武器") -> equipmentdb.EquipmentItem:
    for entry in ITEM_DB.all():
        if (entry.small == small and entry.big == big
                and entry.item_id not in records.SCROLL_TYPES):
            return entry
    raise AssertionError(f"物品总目录里没有 {big}/{small}")


KATANA = item_of("刀")
ARM_PART = item_of("手臂", "防具")


def melee_affix(*, star: bool = False):
    """一条可写的近战词条（标签含「近战」、非固定）。"""
    for entry in sorted(MELEE.db.all(), key=lambda item: item.effect_id):
        if entry.is_fixed or bool(entry.is_star) != star:
            continue
        if "近战" in MELEE.tags_of(entry.effect_id):
            return entry
    raise AssertionError("近战表里找不到可写词条")


AFFIX = melee_affix()
STAR_AFFIX = melee_affix(star=True)


def save_with(record: bytes, slot: int = 0) -> bytes:
    return support.build_plain_save(records_by_slot={slot: record})


def record_with_grace(item, grace, *, slot: int = 4, metadata: int = GRACE_METADATA,
                      filler=None, level: int = 170, plus: int = 0,
                      extra_slots: dict[int, tuple[int, int, int]] | None = None) -> bytes:
    """一条带恩宠/套装（默认第 5 槽）的合成记录。``filler`` 放在 0 槽。"""
    effects: list[tuple[int, int, int]] = []
    for index in range(max(slot, max((extra_slots or {}), default=-1)) + 1):
        if extra_slots and index in extra_slots:
            effects.append(extra_slots[index])
        elif filler is not None and index == 0:
            effects.append((filler.effect_id, filler.value, GRACE_METADATA))
        else:
            effects.append((0, 0, 0))
    if not extra_slots or slot not in extra_slots:
        effects[slot] = (grace.effect_id, grace.value, metadata)
    return support.build_record(record_type=item.item_id, level=level, rarity=4,
                                plus=plus, effects=tuple(effects))


def slots_of(data: bytes, index: int = 0) -> tuple[records.EffectSlot, ...]:
    record = records.read_item_record(data, index)
    assert record is not None
    return records.read_effect_slots(record.record)


class GraceSwapTests(unittest.TestCase):
    """能换：武器与防具的 恩宠 → 恩宠；套装槽锁死（用户规则）。"""

    def test_a_weapon_swaps_its_grace(self) -> None:
        data = save_with(record_with_grace(KATANA, GRACE_A))
        patched = apply_equipment_grace_edit(data, 0, GRACE_B.effect_id,
                                             grace_db=GRACE_DB)
        slot = slots_of(patched)[4]
        self.assertEqual(slot.effect_id, GRACE_B.effect_id)
        self.assertEqual(slot.value, GRACE_B.value)

    def test_an_armor_swaps_its_grace(self) -> None:
        data = save_with(record_with_grace(ARM_PART, GRACE_A))
        patched = apply_equipment_grace_edit(data, 0, GRACE_B.effect_id,
                                             grace_db=GRACE_DB)
        self.assertEqual(slots_of(patched)[4].effect_id, GRACE_B.effect_id)

    def test_a_set_entry_cannot_be_written(self) -> None:
        """用户规则：恩宠不能换成套装（套装与物品种类强绑定）。

        这条用例原本断言「套装可以写进去」（P5 的口径）。新规则反转了它 ——
        这是本次唯一被反转的既有断言，已在上报里点名。
        """
        data = save_with(record_with_grace(KATANA, GRACE_A))
        with self.assertRaises(GraceEditError) as caught:
            apply_equipment_grace_edit(data, 0, SET_ENTRY.effect_id,
                                       grace_db=GRACE_DB)
        self.assertIn("只能换成另一个恩宠", str(caught.exception))
        self.assertEqual(slots_of(data)[4].effect_id, GRACE_A.effect_id)

    def test_only_the_id_and_value_change(self) -> None:
        """与饰品同口径：metadata 一个字节都不许动。"""
        record = record_with_grace(KATANA, GRACE_A, filler=AFFIX)
        data = save_with(record)
        patched = apply_equipment_grace_edit(data, 0, GRACE_B.effect_id,
                                            grace_db=GRACE_DB)
        offset = list_equipment(data)[0].offset
        before = data[offset:offset + records.SCROLL_RECORD_SIZE]
        after = patched[offset:offset + records.SCROLL_RECORD_SIZE]
        self.assertEqual(len(before), len(after))
        differing = [index for index, (a, b) in enumerate(zip(before, after)) if a != b]
        # 槽内布局: +0x00 effect_id, +0x04 value, +0x08 metadata —— 只有 id 与 value
        # 允许变化，metadata（含种类码、固定位与 ★ 位）必须原样。
        slot_start = records.EFFECT_START + 4 * records.EFFECT_STRIDE
        allowed = set(range(slot_start, slot_start + 8))
        self.assertTrue(set(differing) <= allowed,
                        f"改动越界: {sorted(set(differing) - allowed)}")
        self.assertTrue(differing, "恩宠替换应当至少改动 id/value")

    def test_metadata_is_kept_even_with_the_fixed_bit(self) -> None:
        metadata = GRACE_METADATA | FIXED_BIT
        data = save_with(record_with_grace(KATANA, GRACE_A, metadata=metadata))
        patched = apply_equipment_grace_edit(data, 0, GRACE_B.effect_id,
                                            grace_db=GRACE_DB)
        self.assertEqual(slots_of(patched)[4].metadata, metadata)

    def test_the_plan_keeps_exactly_one_grace(self) -> None:
        """写入的槽就是现有的恩宠槽 ⇒ 不可能造出第二个恩宠。"""
        data = save_with(record_with_grace(KATANA, GRACE_A))
        plan = plan_equipment_grace_edit(data, 0, GRACE_B.effect_id,
                                        grace_db=GRACE_DB)
        graces = [slot for slot in plan.after
                  if not slot.is_empty and GRACE_DB.describe(slot.effect_id)]
        self.assertEqual(len(graces), 1)

    def test_the_view_names_the_grace_from_the_table(self) -> None:
        data = save_with(record_with_grace(KATANA, GRACE_A))
        view = list_equipment(data)[0]
        self.assertEqual(view.grace_slot(GRACE_DB), 4)
        slot = view.slots(grace_db=GRACE_DB)[4]
        self.assertEqual(slot.name, GRACE_DB.describe(GRACE_A.effect_id))
        self.assertEqual(slot.category, "恩宠/套装")
        self.assertFalse(slot.is_fixed)
        self.assertEqual(view.slot_role(4, grace_db=GRACE_DB), "恩宠/套装")

    def test_availability_reports_the_slot_and_the_current_name(self) -> None:
        data = save_with(record_with_grace(KATANA, GRACE_A))
        view = list_equipment(data)[0]
        availability = equipment_grace_availability(view, grace_db=GRACE_DB)
        self.assertTrue(availability.allowed)
        self.assertEqual(availability.slot_index, 4)
        self.assertEqual(availability.current_id, GRACE_A.effect_id)
        self.assertEqual(availability.current_name, GRACE_A.name)
        self.assertIn(GRACE_A.name, availability.describe_current())


class GraceFamilyRuleTests(unittest.TestCase):
    """用户规则：套装槽锁死（任何替换都拒绝）、恩宠槽只能换恩宠。"""

    def test_a_set_slot_cannot_be_replaced_on_a_weapon(self) -> None:
        data = save_with(record_with_grace(KATANA, SET_ENTRY,
                                           metadata=SET_METADATA))
        availability = equipment_grace_availability(
            list_equipment(data)[0], grace_db=GRACE_DB)
        self.assertFalse(availability.allowed)
        self.assertIn("套装", availability.reason)
        self.assertIn("不能替换", availability.reason)
        for target in (GRACE_B.effect_id, SET_ENTRY.effect_id):
            with self.subTest(target=f"{target:#06x}"):
                with self.assertRaises(GraceEditError):
                    apply_equipment_grace_edit(data, 0, target, grace_db=GRACE_DB)

    def test_a_set_slot_cannot_be_replaced_on_an_armor(self) -> None:
        data = save_with(record_with_grace(ARM_PART, SET_ENTRY,
                                           metadata=SET_METADATA))
        availability = equipment_grace_availability(
            list_equipment(data)[0], grace_db=GRACE_DB)
        self.assertFalse(availability.allowed)
        self.assertIn("套装", availability.reason)
        with self.assertRaises(GraceEditError):
            apply_equipment_grace_edit(data, 0, GRACE_B.effect_id,
                                       grace_db=GRACE_DB)

    def test_a_blessing_with_the_fixed_bit_is_still_replaceable(self) -> None:
        """固定位不改变族：带 0x4000 的恩宠槽照样能换（族标签看 byte9 低 4 位）。"""
        metadata = GRACE_METADATA | FIXED_BIT
        data = save_with(record_with_grace(KATANA, GRACE_A, metadata=metadata))
        patched = apply_equipment_grace_edit(data, 0, GRACE_B.effect_id,
                                            grace_db=GRACE_DB)
        slot = slots_of(patched)[4]
        self.assertEqual(slot.effect_id, GRACE_B.effect_id)
        self.assertEqual(slot.metadata, metadata)

    def test_an_unknown_family_byte_is_refused(self) -> None:
        """族标签根本不认识的槽一律拒绝（fail closed）。"""
        data = save_with(record_with_grace(KATANA, GRACE_A, metadata=0x00029900))
        availability = equipment_grace_availability(
            list_equipment(data)[0], grace_db=GRACE_DB)
        self.assertFalse(availability.allowed)
        self.assertIn("0x99", availability.reason)

    def test_the_creation_path_is_not_affected(self) -> None:
        """无中生有从模板继承的套装槽不受这条规则影响（只约束「替换」）。"""
        from nioh3_equipment_affix_editor import equipmentdb as _equipmentdb
        item_db = _equipmentdb.load_equipment_item_db()
        # donor 必须自己命中过词条池，才有资格当模板（见 find_equipment_donor）。
        donor = save_with(record_with_grace(KATANA, SET_ENTRY,
                                            metadata=SET_METADATA, filler=AFFIX))
        plan = editor.plan_create_equipment(
            donor, record_type=KATANA.item_id, level=170, plus=0, rarity=4,
            effects=(), slot_index=1, item_db=item_db, grace_db=GRACE_DB)
        self.assertTrue(plan.effects)
        copied = [slot for slot in plan.effects
                  if not slot.is_empty and GRACE_DB.describe(slot.effect_id)]
        self.assertEqual(len(copied), 1)
        self.assertEqual(GRACE_DB.lookup(copied[0].effect_id).category,
                         SET_ENTRY.category)


class GraceRefusalTests(unittest.TestCase):
    """仍然拒绝的东西。"""

    def test_a_grace_cannot_be_written_into_a_normal_slot(self) -> None:
        data = save_with(record_with_grace(KATANA, GRACE_A, filler=AFFIX))
        with self.assertRaises(EditorError) as caught:
            plan_equipment_edits(data, [{"record_index": 0, "slot_index": 0,
                                         "effect_id": GRACE_B.effect_id}],
                                 grace_db=GRACE_DB)
        self.assertIn("恩宠/套装只能写在槽5", str(caught.exception))

    def test_a_record_without_a_grace_slot_is_refused(self) -> None:
        record = support.build_record(record_type=KATANA.item_id, level=170, rarity=4,
                                      effects=((AFFIX.effect_id, AFFIX.value,
                                                GRACE_METADATA),))
        data = save_with(record)
        view = list_equipment(data)[0]
        availability = equipment_grace_availability(view, grace_db=GRACE_DB)
        self.assertFalse(availability.allowed)
        self.assertIn("没有恩宠/套装词条槽", availability.reason)
        with self.assertRaises(GraceEditError):
            plan_equipment_grace_edit(data, 0, GRACE_B.effect_id, grace_db=GRACE_DB)

    def test_the_same_grace_is_refused(self) -> None:
        data = save_with(record_with_grace(KATANA, GRACE_A))
        with self.assertRaises(GraceEditError) as caught:
            plan_equipment_grace_edit(data, 0, GRACE_A.effect_id, grace_db=GRACE_DB)
        self.assertIn("无需修改", str(caught.exception))

    def test_an_id_outside_the_grace_table_is_refused(self) -> None:
        data = save_with(record_with_grace(KATANA, GRACE_A))
        with self.assertRaises(GraceEditError) as caught:
            plan_equipment_grace_edit(data, 0, 0x00FFFF, grace_db=GRACE_DB)
        self.assertIn("不在恩宠名表里", str(caught.exception))

    def test_an_out_of_catalog_id_is_still_refused_by_rule_one(self) -> None:
        """「允许恩宠」绝不能变成「允许任何表外 id」。"""
        data = save_with(record_with_grace(KATANA, GRACE_A, filler=AFFIX))
        with self.assertRaises(EditorError) as caught:
            plan_equipment_edits(data, [{"record_index": 0, "slot_index": 0,
                                         "effect_id": 0x00FFFF}],
                                 grace_db=GRACE_DB)
        self.assertIn("不能写到", str(caught.exception))

    def test_a_fixed_affix_is_still_not_writable(self) -> None:
        fixed = next(entry for entry in MELEE.db.all() if entry.is_fixed)
        record = support.build_record(
            record_type=KATANA.item_id, level=170, rarity=4,
            effects=((fixed.effect_id, fixed.value, GRACE_METADATA),
                     (0, 0, 0), (0, 0, 0), (0, 0, 0),
                     (GRACE_A.effect_id, GRACE_A.value, GRACE_METADATA)),
        )
        data = save_with(record)
        with self.assertRaises(EditorError) as caught:
            plan_equipment_edits(data, [{"record_index": 0, "slot_index": 0,
                                         "effect_id": AFFIX.effect_id}],
                                 grace_db=GRACE_DB)
        self.assertIn("固定词条不能修改", str(caught.exception))

    def test_a_grace_slot_with_the_fixed_bit_is_not_fixed(self) -> None:
        """恩宠槽不受固定位影响：带 0x4000 也照样能换。"""
        data = save_with(record_with_grace(KATANA, GRACE_A,
                                           metadata=GRACE_METADATA | FIXED_BIT))
        view = list_equipment(data)[0]
        self.assertFalse(view.slot_is_fixed(4, grace_db=GRACE_DB))
        self.assertNotIn(4, view.fixed_slots(grace_db=GRACE_DB))
        patched = apply_equipment_grace_edit(data, 0, GRACE_B.effect_id,
                                            grace_db=GRACE_DB)
        self.assertEqual(slots_of(patched)[4].effect_id, GRACE_B.effect_id)

    def test_a_value_that_is_not_the_tables_value_is_refused(self) -> None:
        data = save_with(record_with_grace(KATANA, GRACE_A))
        with self.assertRaises(EditorError) as caught:
            plan_equipment_edits(
                data,
                [{"record_index": 0, "slot_index": 4,
                  "effect_id": GRACE_B.effect_id, "value": GRACE_B.value + 7}],
                grace_db=GRACE_DB,
            )
        self.assertIn("只允许写它自己的目录值", str(caught.exception))

    def test_the_grace_path_needs_the_grace_table(self) -> None:
        """不给恩宠名表时行为与 P2 一致：恩宠 id 仍然写不进去。"""
        data = save_with(record_with_grace(KATANA, GRACE_A))
        with self.assertRaises(EditorError):
            plan_equipment_edits(data, [{"record_index": 0, "slot_index": 4,
                                         "effect_id": GRACE_B.effect_id}])

    def test_a_star_slot_still_travels_with_its_star_bit(self) -> None:
        """顺带钉住：★ 词条仍然可写，且种类码/★ 位照旧跟随词条。"""
        # 槽 1 先放一条可写词条：当前周目不允许往空槽写东西（空槽不可改），
        # 填的这条与 AFFIX / STAR_AFFIX 不同种类，替换它不会引入"同种类两条"。
        codes = editor.load_affix_category_codes()
        filler = next(
            entry for entry in sorted(MELEE.db.all(), key=lambda item: item.effect_id)
            if not entry.is_fixed and not entry.is_star
            and "近战" in MELEE.tags_of(entry.effect_id)
            and entry.category not in (AFFIX.category, STAR_AFFIX.category, "其他"))
        record = support.build_record(
            record_type=KATANA.item_id, level=170, rarity=4,
            effects=((AFFIX.effect_id, AFFIX.value, GRACE_METADATA),
                     (filler.effect_id, filler.value, codes[filler.category]),
                     (0, 0, 0), (0, 0, 0),
                     (GRACE_A.effect_id, GRACE_A.value, GRACE_METADATA)),
        )
        data = save_with(record)
        patched = apply_equipment_edits(
            data, [{"record_index": 0, "slot_index": 1,
                    "effect_id": STAR_AFFIX.effect_id}],
            grace_db=GRACE_DB,
        )
        slot = slots_of(patched)[1]
        self.assertEqual(slot.effect_id, STAR_AFFIX.effect_id)
        self.assertTrue(slot.metadata & editor.STAR_BIT)


if __name__ == "__main__":  # pragma: no cover - manual runs only
    unittest.main()
