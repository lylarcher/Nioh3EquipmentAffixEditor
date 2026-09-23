"""P2d：武器 / 防具的「无中生有」引擎入口与 CLI 的 --kind 大类。

**覆盖范围**：`editor.plan_create_equipment()` / `editor.apply_equipment_creations()` /
`editor.find_equipment_donor()`，以及 `cli.cmd_list` 的 `--kind 武器 / 防具 / 魂核` 分支。
饰品与魂核的既有新建入口（`plan_creation`）不在本模块，它有自己的测试。

**模板策略**（本模块重点钉住的东西）：新记录一律从**真实样本**复制整条记录，然后只改
`+0x00`/`+0x02` 种类 id、`+0x04` 数量、`+0x06`/`+0x08` 等级、`+0x0a` +値、`+0x30` 低 4 位
稀有度、以及词条槽；词条 metadata 只重写种类码（`0x1F00`）与 ★ 位（`0x040000`）。
样本优先取**同种类**（它的同名固定词条正好属于这件装备）；没有同种类时才退到**同类型
（小类）**样本，并把"模板自带但不属于新物品"的槽清空、在计划里说明；两者都没有则拒绝。

样本存档用 `tests/support.py` 的合成存档（真实存档只读，永不参与测试）。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import struct
import unittest
from types import SimpleNamespace
from unittest import mock

from nioh3_accessory_editor import cli, editor, equipmentdb, records
from nioh3_accessory_editor.affixdb import AffixDb
from nioh3_accessory_editor.editor import (
    DONOR_SAME_KIND,
    DONOR_SAME_TYPE,
    EquipmentCreationError,
    apply_equipment_creations,
    list_equipment,
    plan_create_equipment,
)
from tests import support

ITEM_DB = equipmentdb.load_equipment_item_db()
MELEE = equipmentdb.load_pool(equipmentdb.POOL_MELEE)
RANGED = equipmentdb.load_pool(equipmentdb.POOL_RANGED)
ARMOR = equipmentdb.load_pool(equipmentdb.POOL_ARMOR)
POOLS = {equipmentdb.POOL_MELEE: MELEE, equipmentdb.POOL_RANGED: RANGED,
         equipmentdb.POOL_ARMOR: ARMOR}
CODES = editor.load_affix_category_codes()
LAYOUT = support.legacy_layout()
FIXED_BIT = 0x4000


def items_of(small: str, big: str = "武器") -> list[equipmentdb.EquipmentItem]:
    return [entry for entry in ITEM_DB.all()
            if entry.small == small and entry.big == big
            and entry.item_id not in records.SCROLL_TYPES]


def item_of(small: str, big: str = "武器") -> equipmentdb.EquipmentItem:
    found = items_of(small, big)
    if not found:
        raise AssertionError(f"物品总目录里没有 {big}/{small}")
    return found[0]


def melee_affix(*, category: str = "", skip: set[int] | None = None) -> int:
    """近战武器表里一条可写词条（非固定；可指定种类）。"""
    for entry in MELEE.db.all():
        if entry.is_fixed or entry.is_star:
            continue
        if skip and entry.effect_id in skip:
            continue
        if not set(MELEE.tags_of(entry.effect_id)) & {"近战"}:
            continue
        if category and entry.category != category:
            continue
        return entry.effect_id
    raise AssertionError("没有可用的近战词条")


def melee_fixed() -> int:
    """一条**标签也允许近战武器**的同名固定词条（否则会先被标签规则拦下）。"""
    for entry in MELEE.db.all():
        if entry.is_fixed and set(MELEE.tags_of(entry.effect_id)) & {"近战"}:
            return entry.effect_id
    raise AssertionError("近战表里没有同名固定词条")


def baseline_affix(pool_key: str, item: equipmentdb.EquipmentItem,
                   avoid_category: str = "") -> int:
    """模板记录里放的那条真实词条（让这件装备被认成"真记录"，证据计数 > 0）。

    它还必须真的能出在这件装备上（标签有交集），否则整条记录会被自己的规则拦下。
    """
    pool = POOLS[pool_key]
    accepted = equipmentdb.acceptable_equipment_tokens(item)
    candidates = [entry for entry in pool.db.all()
                  if not entry.is_fixed and not entry.is_star
                  and (not avoid_category or entry.category != avoid_category)
                  and set(pool.tags_of(entry.effect_id)) & accepted]
    # 「其他」种类在规则里不受"一个种类一个词条"限制，当模板基线最省事。
    for entry in candidates:
        if entry.category == editor.CATEGORY_OTHER:
            return entry.effect_id
    if candidates:
        return candidates[0].effect_id
    raise AssertionError(f"{pool_key} 池里没有 {item.label} 可用的词条")


def ranged_only_affix() -> int:
    """只在远程表里、且标签与近战没有交集的词条。"""
    for entry in RANGED.db.all():
        if entry.is_fixed or entry.is_star:
            continue
        tokens = set(RANGED.tags_of(entry.effect_id))
        if tokens and not tokens & {"近战"} and MELEE.db.lookup(entry.effect_id) is None:
            return entry.effect_id
    raise AssertionError("没有远程专属词条")


def armor_affix(small: str) -> int:
    for entry in ARMOR.db.all():
        if entry.is_fixed or entry.is_star:
            continue
        if small in ARMOR.tags_of(entry.effect_id):
            return entry.effect_id
    raise AssertionError(f"防具表里没有 {small} 的词条")


def donor_record(item: equipmentdb.EquipmentItem, *, level: int = 150, rarity: int = 4,
                 effects: tuple[tuple[int, int, int], ...] = (),
                 tail_0: int = 0x0BADF00D, tail_1: int = 0x0BADBEEF) -> bytes:
    return support.build_record(record_type=item.item_id, level=level, rarity=rarity,
                               effects=effects, tail_0=tail_0, tail_1=tail_1)


def effect_spec(affix_id: int, *, value: int | None = None, db=MELEE.db) -> dict:
    entry = db.lookup(affix_id)
    return {"slot_index": 0, "effect_id": affix_id,
            "value": entry.value if value is None else value}


def plan(save: bytes, item: equipmentdb.EquipmentItem, *, level: int = 160, **kwargs):
    return plan_create_equipment(save, record_type=item.item_id, level=level,
                                 layout=LAYOUT, item_db=ITEM_DB, pools=POOLS,
                                 **kwargs)


class CreationFixture(unittest.TestCase):
    """合成存档的一份基线：一件刀（同种类样本）+ 一件别的刀（同类型样本）。"""

    @classmethod
    def setUpClass(cls) -> None:
        katana = items_of("刀", "武器")
        if len(katana) < 2:
            raise unittest.SkipTest("物品总目录里刀的种类不足两个")
        cls.katana, cls.other_katana = katana[0], katana[1]
        cls.bow = item_of("弓", "武器")
        cls.leg_armor = item_of("腿部", "防具")
        cls.arm_armor = item_of("手臂", "防具")
        cls.affix = melee_affix()
        cls.affix_category = MELEE.db.lookup(cls.affix).category
        cls.other_affix = melee_affix(skip={cls.affix})
        cls.fixed = melee_fixed()

    def save_with(self, **slots: bytes) -> bytes:
        # 关键字参数的名字一定是字符串（**{"0": ...}），这里转回槽位序号。
        return support.build_plain_save(
            records_by_slot={int(key): value for key, value in slots.items()})

    def donor(self, item: equipmentdb.EquipmentItem, effects=(),
              *, level: int = 150, avoid: str = "") -> bytes:
        """模板记录：末尾槽放一条本池的真实词条（证据），efter 其他槽按需再填。

        ``plan_create_equipment`` 只把「自己那一池词条命中过」的记录当作可用样本
        （与饰品的新建口径一致），所以模板必须有至少一条被词条表收录的词条。
        """
        pool_key = equipmentdb.pool_name_for(item)
        affix = baseline_affix(pool_key, item, avoid or self.affix_category)
        entry = POOLS[pool_key].db.lookup(affix)
        record = donor_record(item, level=level)
        record = records.patch_effect_slots(record, [{
            "slot_index": records.EFFECT_COUNT - 1, "effect_id": affix,
            "value": entry.value,
            "metadata": editor.affix_metadata(0, entry, CODES)}])
        if effects:
            record = records.patch_effect_slots(record, [
                {"slot_index": index, "effect_id": effect_id, "value": value,
                 "metadata": metadata}
                for index, (effect_id, value, metadata) in enumerate(effects)])
        return record


class CreationTests(CreationFixture):
    def test_a_new_weapon_lands_in_the_first_free_slot(self) -> None:
        save = self.save_with(**{"0": self.donor(self.katana)})
        made = plan(save, self.katana, effects=[effect_spec(self.affix)])
        self.assertEqual(made.slot_index, 1)
        self.assertEqual(made.record_type, self.katana.item_id)
        self.assertEqual(made.level, 160)
        self.assertEqual(made.donor_slot, 0)
        self.assertEqual(made.donor_reason, DONOR_SAME_KIND)
        self.assertEqual(made.effects[0].effect_id, self.affix)
        self.assertEqual(made.effects[0].value, MELEE.db.lookup(self.affix).value)

    def test_an_explicit_slot_is_honoured(self) -> None:
        save = self.save_with(**{"0": self.donor(self.katana)})
        made = plan(save, self.katana, slot_index=7, effects=[])
        self.assertEqual(made.slot_index, 7)
        self.assertEqual(made.offset, records.record_offset(7, layout=LAYOUT))

    def test_an_occupied_slot_is_refused(self) -> None:
        save = self.save_with(**{"0": self.donor(self.katana)})
        with self.assertRaises(EquipmentCreationError) as caught:
            plan(save, self.katana, slot_index=0, effects=[])
        self.assertIn("不是空位", str(caught.exception))

    def test_plus_and_rarity_are_written(self) -> None:
        save = self.save_with(**{"0": self.donor(self.katana, level=150)})
        made = plan(save, self.katana, plus=30, rarity=5, count=1, effects=[])
        self.assertEqual(made.plus_value, 30)
        self.assertEqual(made.rarity, 5)
        self.assertEqual(made.rarity_name, records.RARITY_NAMES[5])
        self.assertEqual(made.count, 1)
        self.assertEqual(records.read_record_plus(made.record), 30)

    def test_level_can_go_to_the_documented_cap(self) -> None:
        save = self.save_with(**{"0": self.donor(self.katana, level=150)})
        made = plan(save, self.katana, level=180, effects=[])
        self.assertEqual(made.level, 180)
        self.assertEqual(records.read_record_level(made.record), 180)

    def test_level_181_is_refused(self) -> None:
        save = self.save_with(**{"0": self.donor(self.katana)})
        with self.assertRaises(EquipmentCreationError) as caught:
            plan(save, self.katana, level=181, effects=[])
        self.assertIn("等级", str(caught.exception))

    def test_plus_31_is_refused(self) -> None:
        save = self.save_with(**{"0": self.donor(self.katana)})
        with self.assertRaises(EquipmentCreationError) as caught:
            plan(save, self.katana, plus=31, effects=[])
        self.assertIn("+値", str(caught.exception))

    def test_rarity_out_of_range_is_refused(self) -> None:
        save = self.save_with(**{"0": self.donor(self.katana)})
        with self.assertRaises(EquipmentCreationError) as caught:
            plan(save, self.katana, rarity=9, effects=[])
        self.assertIn("稀有度", str(caught.exception))

    def test_an_unknown_item_id_is_refused(self) -> None:
        save = self.save_with(**{"0": self.donor(self.katana)})
        with self.assertRaises(EquipmentCreationError) as caught:
            plan_create_equipment(save, record_type=0xFFF0, level=160, layout=LAYOUT,
                                  item_db=ITEM_DB, pools=POOLS)
        self.assertIn("物品总目录", str(caught.exception))

    def test_an_accessory_is_refused(self) -> None:
        save = self.save_with(**{"0": self.donor(self.katana)})
        with self.assertRaises(EquipmentCreationError) as caught:
            plan_create_equipment(save, record_type=0x4987, level=160, layout=LAYOUT,
                                  item_db=ITEM_DB, pools=POOLS)
        self.assertIn("大类", str(caught.exception))

    def test_an_empty_save_has_no_template(self) -> None:
        save = self.save_with()
        with self.assertRaises(EquipmentCreationError) as caught:
            plan(save, self.katana, effects=[])
        self.assertIn("样本", str(caught.exception))

    def test_the_same_kind_sample_wins_over_a_lower_slot_of_another_kind(self) -> None:
        save = self.save_with(**{"0": self.donor(self.other_katana),
                                 "3": self.donor(self.katana)})
        made = plan(save, self.katana, effects=[])
        self.assertEqual(made.donor_slot, 3)
        self.assertEqual(made.donor_reason, DONOR_SAME_KIND)

    def test_without_a_same_kind_sample_it_falls_back_to_the_same_type(self) -> None:
        save = self.save_with(**{"0": self.donor(self.other_katana)})
        made = plan(save, self.katana, effects=[])
        self.assertEqual(made.donor_slot, 0)
        self.assertEqual(made.donor_reason, DONOR_SAME_TYPE)
        self.assertIn("同类型", made.describe())

    def test_the_fallback_clears_the_other_kinds_fixed_affix(self) -> None:
        donor = self.donor(self.other_katana, effects=(
            (self.fixed, MELEE.db.lookup(self.fixed).value, FIXED_BIT),))
        save = self.save_with(**{"0": donor})
        made = plan(save, self.katana, effects=[])
        self.assertIn(0, made.cleared_slots)
        self.assertTrue(made.effects[0].is_empty)

    def test_the_same_kind_fixed_affix_is_carried_over(self) -> None:
        donor = self.donor(self.katana, effects=(
            (self.fixed, MELEE.db.lookup(self.fixed).value, FIXED_BIT),))
        save = self.save_with(**{"0": donor})
        made = plan(save, self.katana, effects=[])
        self.assertEqual(made.cleared_slots, ())
        self.assertEqual(made.effects[0].effect_id, self.fixed)

    def test_a_fixed_slot_of_the_same_kind_cannot_be_rewritten(self) -> None:
        donor = self.donor(self.katana, effects=(
            (self.fixed, MELEE.db.lookup(self.fixed).value, FIXED_BIT),))
        save = self.save_with(**{"0": donor})
        with self.assertRaises(EquipmentCreationError) as caught:
            plan(save, self.katana, effects=[effect_spec(self.affix)])
        self.assertIn("同名固定", str(caught.exception))

    def test_a_fixed_affix_cannot_be_added_by_hand(self) -> None:
        save = self.save_with(**{"0": self.donor(self.katana)})
        with self.assertRaises(EquipmentCreationError) as caught:
            plan(save, self.katana, effects=[effect_spec(self.fixed)])
        self.assertIn("同名固定", str(caught.exception))

    def test_an_affix_of_the_wrong_equipment_tag_is_refused(self) -> None:
        save = self.save_with(**{"0": self.donor(self.katana)})
        with self.assertRaises(editor.EditorError) as caught:
            plan(save, self.katana,
                 effects=[effect_spec(ranged_only_affix(), db=RANGED.db)])
        # 远程专属词条连近战表都不在，所以拒绝理由先说"不在这一池词条表里"。
        self.assertIn("不能写到", str(caught.exception))

    def test_two_affixes_of_one_category_are_refused(self) -> None:
        entry = MELEE.db.lookup(self.affix)
        twin = melee_affix(category=entry.category, skip={self.affix})
        save = self.save_with(**{"0": self.donor(self.katana)})
        with self.assertRaises(editor.EditorError) as caught:
            plan(save, self.katana, effects=[
                {"slot_index": 0, "effect_id": self.affix},
                {"slot_index": 1, "effect_id": twin},
            ])
        self.assertIn("种类", str(caught.exception))

    def test_two_graces_are_refused(self) -> None:
        table = support.load_grace_table()
        entries = table.all() if hasattr(table, "all") else list(table)
        ids = [entry.effect_id for entry in entries
               if not entry.is_fixed and not entry.is_star][:2]
        if len(ids) < 2:
            self.skipTest("恩宠/套装表里没有两条可写词条")
        save = self.save_with(**{"0": self.donor(self.katana)})
        with self.assertRaises(editor.EditorError) as caught:
            plan(save, self.katana, grace_db=editor.GraceDb.best_effort(),
                 effects=[{"slot_index": 0, "effect_id": ids[0]},
                          {"slot_index": 1, "effect_id": ids[1]}])
        # 恩宠词条在源表里没有「种类」码，两条会先撞上"一个种类一个词条"，
        # 所以这里只要求"两条恩宠被拒"这件事本身成立。
        message = str(caught.exception)
        self.assertTrue("恩宠" in message or "只能有一个词条" in message, message)

    def test_armor_uses_the_armor_pool(self) -> None:
        affix = armor_affix(self.leg_armor.small)
        save = self.save_with(**{"0": self.donor(
            self.leg_armor, avoid=ARMOR.db.lookup(affix).category)})
        made = plan(save, self.leg_armor, effects=[effect_spec(affix, db=ARMOR.db)])
        self.assertEqual(made.effects[0].effect_id, affix)
        self.assertEqual(made.big, "防具")

    def test_an_armor_affix_for_another_body_part_is_refused(self) -> None:
        other_part = "手臂" if self.leg_armor.small != "手臂" else "腿部"
        affix = armor_affix(other_part)
        save = self.save_with(**{"0": self.donor(self.leg_armor)})
        with self.assertRaises(editor.EditorError) as caught:
            plan(save, self.leg_armor, effects=[effect_spec(affix, db=ARMOR.db)])
        self.assertIn("装备种类", str(caught.exception))

    def test_only_the_understood_fields_differ_from_the_template(self) -> None:
        """逐字节：改动只允许出现在种类 id / 数量 / 等级 / +値 / 稀有度 / 词条槽。"""
        donor = self.donor(self.katana, level=150, effects=(
            (self.other_affix, MELEE.db.lookup(self.other_affix).value,
             editor.affix_metadata(0, MELEE.db.lookup(self.other_affix), CODES)),))
        save = self.save_with(**{"0": donor})
        made = plan(save, self.katana, level=175, plus=12, rarity=3, count=1,
                    effects=[effect_spec(self.affix)])
        slot_span = len(made.record) - records.EFFECT_START
        allowed = set(range(0x00, 0x04)) | {0x04, 0x05, 0x06, 0x07, 0x08, 0x09,
                                            0x0A, 0x0B, 0x30}
        allowed |= set(range(records.EFFECT_START, len(made.record)))
        differences = {index for index in range(len(made.record))
                       if made.record[index] != donor[index]}
        self.assertTrue(differences <= allowed,
                        f"改到了不该改的字节: {sorted(differences - allowed)}")
        self.assertIn(0x30, differences)
        self.assertGreater(slot_span, 0)

    def test_metadata_only_rewrites_the_category_and_star_bits(self) -> None:
        save = self.save_with(**{"0": self.donor(self.katana)})
        made = plan(save, self.katana, effects=[effect_spec(self.affix)])
        metadata = made.effects[0].metadata
        self.assertEqual(metadata & ~editor.CATEGORY_CODE_MASK & ~editor.STAR_BIT, 0)
        self.assertEqual(metadata & editor.CATEGORY_CODE_MASK,
                         CODES[MELEE.db.lookup(self.affix).category])

    def test_clearing_a_slot_writes_an_empty_effect(self) -> None:
        donor = self.donor(self.katana, effects=(
            (self.other_affix, MELEE.db.lookup(self.other_affix).value,
             editor.affix_metadata(0, MELEE.db.lookup(self.other_affix), CODES)),))
        save = self.save_with(**{"0": donor})
        made = plan(save, self.katana, effects=[
            {"slot_index": records.EFFECT_COUNT - 1,
             "effect_id": records.EMPTY_EFFECT_ID}])
        self.assertTrue(made.effects[records.EFFECT_COUNT - 1].is_empty)

    def test_the_disclaimer_free_describe_is_readable(self) -> None:
        save = self.save_with(**{"0": self.donor(self.katana)})
        made = plan(save, self.katana, effects=[effect_spec(self.affix)])
        text = made.describe()
        self.assertIn("新建", text)
        self.assertIn("模板", text)
        self.assertIn(self.katana.name, text)


class ApplyTests(CreationFixture):
    def test_the_written_record_is_readable_by_list_equipment(self) -> None:
        save = self.save_with(**{"0": self.donor(self.katana, level=150)})
        made = plan(save, self.katana, level=170, plus=9,
                    effects=[effect_spec(self.affix)])
        patched = apply_equipment_creations(save, [made])
        self.assertEqual(patched[made.offset:made.offset + len(made.record)],
                         made.record)
        views = {view.slot_index: view for view in list_equipment(patched, big="武器",
                                                                 layout=LAYOUT)}
        self.assertIn(made.slot_index, views)
        view = views[made.slot_index]
        self.assertEqual(view.item.item_id, self.katana.item_id)
        self.assertEqual(view.level, 170)
        self.assertEqual(view.plus_value, 9)
        self.assertEqual(view.occupied_effects[0].effect_id, self.affix)

    def test_the_untouched_part_of_the_save_is_identical(self) -> None:
        save = self.save_with(**{"0": self.donor(self.katana)})
        made = plan(save, self.katana, effects=[effect_spec(self.affix)])
        patched = apply_equipment_creations(save, [made])
        start, end = made.offset, made.offset + len(made.record)
        self.assertEqual(patched[:start], save[:start])
        self.assertEqual(patched[end:], save[end:])
        self.assertEqual(len(patched), len(save))

    def test_the_same_slot_cannot_be_created_twice(self) -> None:
        save = self.save_with(**{"0": self.donor(self.katana)})
        made = plan(save, self.katana, slot_index=2, effects=[])
        patched = apply_equipment_creations(save, [made])
        with self.assertRaises(editor.CreationError) as caught:
            apply_equipment_creations(patched, [made])
        self.assertIn("已被占用", str(caught.exception))

    def test_two_plans_in_one_call(self) -> None:
        save = self.save_with(**{"0": self.donor(self.katana)})
        first = plan(save, self.katana, slot_index=1, effects=[
            effect_spec(self.affix)])
        second = plan(save, self.katana, slot_index=2, effects=[
            effect_spec(self.other_affix)])
        patched = apply_equipment_creations(save, [first, second])
        views = list_equipment(patched, big="武器", layout=LAYOUT)
        self.assertEqual(len(views), 3)


class CliKindTests(CreationFixture):
    """`list --kind <大类>`：给未给时行为不变。"""

    def run_list(self, data: bytes, *argv: str) -> tuple[int, str]:
        args = cli.build_parser().parse_args(["list", *argv])
        save = SimpleNamespace(display="合成存档", path="SYNTHETIC")
        buffer = io.StringIO()
        with mock.patch.object(cli, "_crypto", return_value=object()), \
                mock.patch.object(cli, "_select_save", return_value=save), \
                mock.patch.object(cli, "open_save", return_value=data):
            with contextlib.redirect_stdout(buffer):
                code = cli.cmd_list(args)
        return code, buffer.getvalue()

    def test_kind_weapon_lists_the_item_name_level_and_disclaimer(self) -> None:
        save = self.save_with(**{"0": self.donor(self.katana, level=155,
                                                effects=((self.affix, 10,
                                                          editor.affix_metadata(
                                                              0, MELEE.db.lookup(
                                                                  self.affix),
                                                              CODES)),))})
        code, output = self.run_list(save, "--kind", "武器")
        self.assertEqual(code, 0)
        self.assertIn("仅供测试学习用", output)
        self.assertIn(self.katana.name, output)
        self.assertIn("Lv155", output)
        self.assertIn("装备种类", output)

    def test_kind_armor_works_too(self) -> None:
        save = self.save_with(**{"0": self.donor(self.leg_armor)})
        code, output = self.run_list(save, "--kind", "防具")
        self.assertEqual(code, 0)
        self.assertIn(self.leg_armor.name, output)
        self.assertIn("仅供测试学习用", output)

    def test_a_non_big_kind_keeps_the_old_name_filter(self) -> None:
        """非大类取值仍然是原来的"按物品名称/id 过滤"，且只作用于饰品。"""
        save = self.save_with(**{"0": self.donor(self.katana)})
        code, output = self.run_list(save, "--kind", "弓")
        self.assertEqual(code, 0)
        # 走的是原来的饰品分之路：输出里有饰品口径的行，而不是大类的槽明细。
        self.assertIn("含饰品词条", output)
        self.assertNotIn("装备种类", output)

    def test_kind_jewelry_lists_accessories_without_name_filtering(self) -> None:
        save = self.save_with(**{"0": self.donor(self.katana)})
        code, output = self.run_list(save, "--kind", "饰品")
        self.assertEqual(code, 0)
        self.assertIn("仅供测试学习用", output)
        self.assertIn("含饰品词条", output)


if __name__ == "__main__":  # pragma: no cover - manual runs only
    unittest.main()
