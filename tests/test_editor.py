"""Editor service tests: planning, fail-closed validation, commit pipeline."""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nioh3_accessory_editor import editor as editor_module
from nioh3_accessory_editor import records
from nioh3_accessory_editor import savefile as savefile_module
from nioh3_accessory_editor.affixdb import (
    AffixDb,
    AffixError,
    GraceDb,
    ItemDb,
    ItemEntry,
    load_grace_catalog,
    load_soul_catalog,
    load_soul_item_catalog,
)
from nioh3_accessory_editor.editor import (
    CreationError,
    KindSwapError,
    LevelEditError,
    apply_creations,
    apply_kind_swaps,
    apply_level_edits,
    apply_soul_edits,
    find_free_slots,
    identify_soul_cores,
    list_soul_cores,
    plan_creation,
    plan_soul_edits,
    soul_catalog_ids,
    collect_kind_samples,
    plan_kind_swap,
    plan_level_edit,
    resolve_item_id,
    EditorError,
    GraceEditError,
    SaveDescriptor,
    apply_edits,
    apply_grace_edit,
    commit_save,
    discover_saves,
    grace_edit_availability,
    list_accessories,
    open_save,
    plan_edits,
    plan_grace_edit,
    resolve_grace_id,
    save_checksum_is_valid,
)
from nioh3_accessory_editor.records import EFFECT_COUNT, EMPTY_EFFECT_ID, RecordError
from nioh3_accessory_editor.savefile import GameRunningError, SaveCrypto
from tests import support

ITEM_TYPE = 0x4001


class EditorTestCase(unittest.TestCase):
    """Synthetic save with two accessory records in known slots."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = AffixDb()
        # Editable slots must use affixes that are NOT (同名固定): a fixed affix is
        # part of the item kind and the editor refuses to change it.
        cls.free_affixes = [entry for entry in cls.db.all() if not entry.is_fixed]
        #: A 词条 whose 数值区间 spans several values (most 饰品词条 are single
        #: valued, so tests that write a *chosen* value need one of these).
        cls.ranged_affix = next(
            entry for entry in cls.free_affixes
            if entry.has_value_range and entry.value_min != entry.value_max)
        cls.fixed_affixes = [entry for entry in cls.db.all() if entry.is_fixed]
        cls.fixed_affix = cls.fixed_affixes[0]
        cls.affix_a = cls.free_affixes[0]
        cls.affix_b = cls.free_affixes[1]
        cls.record_3 = support.build_record(
            record_type=ITEM_TYPE, level=150, rarity=4,
            effects=((cls.affix_a.effect_id, 20, 0x40),),
        )
        cls.record_11 = support.build_record(
            record_type=ITEM_TYPE, level=160, rarity=5,
            effects=((cls.affix_b.effect_id, 15, 0x00),),
        )
        cls.plain = support.build_plain_save(
            records_by_slot={3: cls.record_3, 11: cls.record_11}
        )

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="nioh3-editor-")
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)


class ReadOnlyOperationTests(EditorTestCase):
    def test_list_accessories(self) -> None:
        views = list_accessories(self.plain)
        self.assertEqual([view.slot_index for view in views], [3, 11])
        first = views[0]
        self.assertEqual(first.level, 150)
        self.assertEqual(first.rarity, 4)
        self.assertEqual(first.rarity_name, "神器")
        self.assertEqual(first.account_id, support.expected_account_id(ITEM_TYPE))
        self.assertEqual(len(first.effects), 7)
        self.assertEqual(len(first.occupied_effects), 1)

    def test_describe_effects(self) -> None:
        view = list_accessories(self.plain)[0]
        lines = view.describe_effects(self.db)
        # One 种类 line plus one line per effect slot.
        self.assertEqual(len(lines), EFFECT_COUNT + 1)
        self.assertIn("种类", lines[0])
        self.assertIn(self.affix_a.name, lines[1])
        self.assertIn("(空)", lines[2])

    def test_describe_unknown_affix(self) -> None:
        record = support.build_record(
            record_type=ITEM_TYPE, effects=((0xDEADBEEF, 1, 0),)
        )
        save = support.build_plain_save(records_by_slot={0: record})
        lines = list_accessories(save)[0].describe_effects(self.db)
        self.assertIn("未知词条", lines[1])


class GraceSlotTests(EditorTestCase):
    """Every real accessory ends with a 恩宠/套装组合 affix outside the table.

    Confirmed in game (a 龙笛 shows 稻荷神的恩宠 as its last 特殊效果): those ids
    are not in the shipped 饰品词条 table, so a trailing out-of-table slot is
    reported as 恩宠/套装 instead of an unexplained unknown id.
    """

    def test_trailing_out_of_table_slot_is_reported_as_grace(self) -> None:
        record = support.build_record(
            record_type=ITEM_TYPE,
            effects=((self.affix_a.effect_id, 20, 0x40),
                     (0x0071F6, 0, 0x5C020C00)),
        )
        save = support.build_plain_save(records_by_slot={3: record})
        view = list_accessories(save, known_ids=frozenset(
            {self.affix_a.effect_id}))[0]
        self.assertEqual(view.grace_slots(self.db), frozenset({1}))
        self.assertEqual(view.slot_role(0, self.db), "饰品词条")
        self.assertEqual(view.slot_role(1, self.db), "恩宠/套装词条")
        lines = view.describe_effects(self.db)
        self.assertIn("恩宠/套装词条", lines[2])
        self.assertIn(f"{0x0071F6:#06x}", lines[2])

    def test_out_of_table_slot_before_a_known_one_is_not_grace(self) -> None:
        record = support.build_record(
            record_type=ITEM_TYPE,
            effects=((0xDEADBEEF, 7, 0x40), (self.affix_a.effect_id, 20, 0x40)),
        )
        save = support.build_plain_save(records_by_slot={3: record})
        view = list_accessories(save)[0]
        self.assertEqual(view.grace_slots(self.db), frozenset())
        self.assertIn("未知词条", view.describe_effects(self.db)[1])

    def test_two_trailing_out_of_table_slots_are_both_grace(self) -> None:
        record = support.build_record(
            record_type=ITEM_TYPE,
            effects=((self.affix_a.effect_id, 20, 0x40),
                     (0x0071F6, 0, 0), (0x004FA3, 0, 0)),
        )
        save = support.build_plain_save(records_by_slot={3: record})
        view = list_accessories(save)[0]
        self.assertEqual(view.grace_slots(self.db), frozenset({1, 2}))

    def test_real_save_shape_reports_one_grace_slot(self) -> None:
        """4 catalog effects + 1 trailing id: the common real-v2.21 layout."""
        record = support.build_record(
            record_type=ITEM_TYPE,
            effects=((self.affix_a.effect_id, 20, 0x40),
                     (self.affix_b.effect_id, 15, 0x40),
                     (self.free_affixes[2].effect_id, 30, 0x40),
                     (self.free_affixes[3].effect_id, 5, 0x40),
                     (0x004FA3, 0, 0x29014C00)),
        )
        save = support.build_plain_save(records_by_slot={3: record})
        view = list_accessories(save, known_ids=frozenset(
            entry.effect_id for entry in self.db.all()))[0]
        self.assertTrue(view.is_accessory)
        self.assertEqual(view.catalog_hits, 4)
        self.assertEqual(view.grace_slots(self.db), frozenset({4}))

    def test_grace_slot_is_named_from_the_grace_table(self) -> None:
        """0x71f6 ends record #3 of the reference save (不动明王的恩宠)."""
        record = support.build_record(
            record_type=ITEM_TYPE,
            effects=((self.affix_a.effect_id, 20, 0x40), (0x0071F6, 0, 0x5C020C00)),
        )
        save = support.build_plain_save(records_by_slot={3: record})
        view = list_accessories(save)[0]
        grace_db = GraceDb.best_effort()
        line = view.describe_effects(self.db, grace_db)[2]
        self.assertIn("不动明王的恩宠", line)
        self.assertIn("上位恩宠", line)
        self.assertIn("0x71f6", line)

    def test_grace_slot_falls_back_when_the_table_is_missing(self) -> None:
        record = support.build_record(
            record_type=ITEM_TYPE,
            effects=((self.affix_a.effect_id, 20, 0x40), (0x0071F6, 0, 0)),
        )
        save = support.build_plain_save(records_by_slot={3: record})
        view = list_accessories(save)[0]
        line = view.describe_effects(self.db)[2]
        self.assertIn("恩宠/套装词条", line)
        self.assertIn("0x71f6", line)

    def test_checksum_is_valid_on_the_fixture(self) -> None:
        self.assertTrue(save_checksum_is_valid(self.plain))


class GraceEditTests(EditorTestCase):
    """恩宠 -> 恩宠 is allowed; 套装/专属套装 and plain 词条 are refused.

    The gate is the game's own family byte (metadata byte 9): a real v2.21 save
    holds 0x0C in all 196 恩宠 slots and 0x4C in all 16 套装 slots.
    """

    GRACE_A = 0x004FA3  # 稻荷神的恩宠（恩宠）
    GRACE_B = 0x0071F6  # 不动明王的恩宠（上位恩宠）
    SET_ITEM = 0x00A7A1  # 怨恨盖世（忍者套装）— an item-specific 套装 effect

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.grace_db = GraceDb.best_effort()

    def _save_with(self, last_id: int, *, byte9: int = 0x0C,
                   value: int = 0, extra_slots: int = 1) -> bytes:
        effects = [(self.affix_a.effect_id, 20, 0x40)]
        effects += [(self.free_affixes[index + 1].effect_id, 10, 0x40)
                    for index in range(extra_slots)]
        effects.append((last_id, value, 0x5C000000 | (byte9 << 8) | 0x020000))
        return support.build_plain_save(records_by_slot={
            3: support.build_record(record_type=ITEM_TYPE, effects=tuple(effects)),
        })

    def _availability(self, save: bytes):
        view = list_accessories(save)[0]
        return grace_edit_availability(view, grace_db=self.grace_db,
                                      affix_db=self.db)

    def test_a_grace_slot_may_become_another_grace(self) -> None:
        availability = self._availability(self._save_with(self.GRACE_A))
        self.assertTrue(availability.allowed, availability.reason)
        self.assertEqual(availability.current_id, self.GRACE_A)
        self.assertIn("稻荷神的恩宠", availability.current_name)
        self.assertIn("稻荷神的恩宠", availability.describe_current())

    def test_an_upper_grace_is_interchangeable_with_a_normal_one(self) -> None:
        availability = self._availability(self._save_with(self.GRACE_B))
        self.assertTrue(availability.allowed, availability.reason)
        self.assertEqual(availability.kind, "上位恩宠")

    def test_a_set_effect_cannot_be_changed(self) -> None:
        """怨恨盖世 is an item-specific 套装 effect: the user's explicit rule."""
        availability = self._availability(
            self._save_with(self.SET_ITEM, byte9=0x4C))
        self.assertFalse(availability.allowed)
        self.assertIn("套装", availability.reason)
        self.assertIn("怨恨盖世", availability.reason)

    def test_a_plain_affix_in_the_last_slot_cannot_become_a_grace(self) -> None:
        availability = self._availability(self._save_with(self.affix_b.effect_id))
        self.assertFalse(availability.allowed)
        self.assertIn("饰品词条", availability.reason)

    def test_an_unknown_family_byte_is_refused(self) -> None:
        availability = self._availability(self._save_with(self.GRACE_A, byte9=0x99))
        self.assertFalse(availability.allowed)
        self.assertIn("0x99", availability.reason)

    def test_a_grace_id_outside_the_name_table_is_refused(self) -> None:
        # 0x00fb1d occurs in the reference save but is in neither table.
        availability = self._availability(self._save_with(0x00FB1D))
        self.assertFalse(availability.allowed)
        self.assertIn("不在恩宠名表", availability.reason)

    def test_a_record_without_catalog_ids_is_not_changeable(self) -> None:
        save = support.build_plain_save(records_by_slot={
            3: support.build_record(
                record_type=ITEM_TYPE,
                effects=((0xDEADBEEF, 1, 0x40), (self.GRACE_A, 0, 0x00020C00))),
        })
        availability = self._availability(save)
        self.assertFalse(availability.allowed)

    def test_apply_changes_only_the_id_bytes(self) -> None:
        """In memory only the id moves; the checksum is recomputed at commit."""
        save = self._save_with(self.GRACE_A)
        patched = apply_grace_edit(save, 3, self.GRACE_B, affix_db=self.db,
                                   grace_db=self.grace_db)
        self.assertEqual(len(patched), len(save))
        changed = {index for index, (old, new) in enumerate(zip(save, patched))
                   if old != new}
        # The 恩宠 sits in slot 2 of this fixture, and its id lives at slot+0x04.
        # 0x4fa3 -> 0x71f6 differs in the two low bytes only.
        record = list_accessories(save)[0]
        slot_offset = record.offset + 0x34 + 2 * 0x18
        self.assertEqual(changed, {slot_offset + 0x04, slot_offset + 0x05})
        self.assertFalse(save_checksum_is_valid(patched),
                         "写入前校验和应为过期状态，由 commit 重新计算")
        view = list_accessories(patched)[0]
        self.assertEqual(view.effects[2].effect_id, self.GRACE_B)
        self.assertEqual(view.effects[2].value, 0)

    def test_value_and_metadata_of_the_slot_are_preserved(self) -> None:
        save = self._save_with(self.GRACE_A, byte9=0x0C)
        before = list_accessories(save)[0].effects[2]
        patched = apply_grace_edit(save, 3, self.GRACE_B, affix_db=self.db,
                                   grace_db=self.grace_db)
        after = list_accessories(patched)[0].effects[2]
        self.assertEqual(after.metadata, before.metadata)
        self.assertEqual(after.value, before.value)

    def test_set_targets_are_rejected(self) -> None:
        save = self._save_with(self.GRACE_A)
        with self.assertRaises(GraceEditError) as caught:
            apply_grace_edit(save, 3, self.SET_ITEM, affix_db=self.db,
                             grace_db=self.grace_db)
        self.assertIn("不是恩宠", str(caught.exception))

    def test_plan_reports_before_and_after(self) -> None:
        save = self._save_with(self.GRACE_A)
        plan = plan_grace_edit(save, 3, self.GRACE_B, affix_db=self.db,
                               grace_db=self.grace_db)
        self.assertEqual(plan.record_index, 3)
        self.assertEqual(plan.before[2].effect_id, self.GRACE_A)
        self.assertEqual(plan.after[2].effect_id, self.GRACE_B)

    def test_same_grace_is_a_no_op_error(self) -> None:
        save = self._save_with(self.GRACE_A)
        with self.assertRaises(GraceEditError) as caught:
            apply_grace_edit(save, 3, self.GRACE_A, affix_db=self.db,
                             grace_db=self.grace_db)
        self.assertIn("已经是", str(caught.exception))

    def test_unknown_record_is_rejected(self) -> None:
        with self.assertRaises(GraceEditError):
            apply_grace_edit(self.plain, 999, self.GRACE_A, affix_db=self.db,
                             grace_db=self.grace_db)

    def test_resolve_accepts_ids_and_unique_names(self) -> None:
        self.assertEqual(resolve_grace_id(self.grace_db, "0x4fa3"), self.GRACE_A)
        self.assertEqual(resolve_grace_id(self.grace_db, "稻荷神"), self.GRACE_A)
        self.assertEqual(resolve_grace_id(self.grace_db, "稻荷神的恩宠"), self.GRACE_A)
        self.assertEqual(resolve_grace_id(self.grace_db, "不动明王"), self.GRACE_B)

    def test_resolve_rejects_set_names_and_unknown_ids(self) -> None:
        with self.assertRaises(GraceEditError):
            resolve_grace_id(self.grace_db, "怨恨盖世")
        with self.assertRaises(GraceEditError):
            resolve_grace_id(self.grace_db, "0xdeadbeef")
        with self.assertRaises(GraceEditError):
            resolve_grace_id(self.grace_db, "")

    def test_resolve_reports_ambiguous_names(self) -> None:
        with self.assertRaises(GraceEditError) as caught:
            resolve_grace_id(self.grace_db, "恩宠")
        self.assertIn("多个", str(caught.exception))


class ItemKindDisplayTests(EditorTestCase):
    """种类: which accessory a record *is* (display only, never written)."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.item_db = ItemDb.best_effort()

    def _view(self, record_type: int = 0x3E3F):
        save = support.build_plain_save(records_by_slot={
            3: support.build_record(
                record_type=record_type,
                effects=((self.affix_a.effect_id, 20, 0x40),),
            ),
        })
        return list_accessories(save)[0]

    def test_names_a_real_item_id(self) -> None:
        self.assertEqual(self._view().describe_item(self.item_db),
                         "种类 0x3e3f 龙笛[武士]")

    def test_says_so_when_the_id_is_not_in_the_table(self) -> None:
        text = self._view(0x1234).describe_item(self.item_db)
        self.assertIn("0x1234", text)
        self.assertIn("不在物品种类表内", text)

    def test_without_the_table_it_says_which_id_it_is(self) -> None:
        text = self._view().describe_item(None)
        self.assertIn("0x3e3f", text)
        self.assertIn("未加载物品种类表", text)

    def test_effects_listing_starts_with_the_kind(self) -> None:
        lines = self._view().describe_effects(self.db, None, self.item_db)
        self.assertIn("龙笛[武士]", lines[0])

    def test_the_kind_is_display_only(self) -> None:
        """Naming an item must not make any 词条 writable."""
        with self.assertRaises(AffixError):
            self.db.require(0x3E3F)
        with self.assertRaises(AffixError):
            plan_edits(self.plain,
                       [{"record_index": 3, "slot_index": 0,
                         "effect_id": 0x3E3F, "value": 1}],
                       affix_db=self.db)


class FixedSlotTests(EditorTestCase):
    """A 同名固定 affix is part of the item kind: never editable manually.

    Evidence: the catalog's ``(同名固定)`` flag and the slot's metadata byte 9 bit
    0x40 agreed on all 795 catalogued slots of the reference save.
    """

    def _save(self, *, fixed_first: bool, bit: bool = True) -> bytes:
        first = (self.fixed_affix.effect_id if fixed_first
                 else self.affix_a.effect_id)
        meta0 = (0x5C000000 | 0x40 << 8) if bit else 0x5C000000
        return support.build_plain_save(records_by_slot={
            3: support.build_record(
                record_type=ITEM_TYPE,
                effects=((first, 20, meta0),
                         (self.affix_b.effect_id, 15, 0x0040)),
            ),
        })

    def test_a_fixed_slot_is_recognised(self) -> None:
        view = list_accessories(self._save(fixed_first=True))[0]
        self.assertTrue(view.slot_is_fixed(0, self.db))
        self.assertFalse(view.slot_is_fixed(1, self.db))
        self.assertEqual(view.fixed_slots(self.db), frozenset({0}))
        self.assertEqual(view.slot_role(0, self.db), "固定词条")
        self.assertEqual(view.slot_role(1, self.db), "饰品词条")

    def test_the_marker_bit_alone_is_not_consulted_outside_the_catalog(self) -> None:
        """恩宠 slots carry byte 9 = 0x4C too, so an uncatalogued id is not fixed."""
        save = support.build_plain_save(records_by_slot={
            3: support.build_record(
                record_type=ITEM_TYPE,
                effects=((self.affix_a.effect_id, 20, 0x0040),
                         (0x004FA3, 0, 0x29014C00)),
            ),
        })
        view = list_accessories(save)[0]
        self.assertFalse(view.slot_is_fixed(0, self.db))
        self.assertFalse(view.slot_is_fixed(1, self.db))

    def test_editing_a_fixed_slot_is_refused(self) -> None:
        save = self._save(fixed_first=True)
        with self.assertRaises(EditorError) as caught:
            plan_edits(save, [{"record_index": 3, "slot_index": 0,
                               "effect_id": self.affix_b.effect_id,
                        "value": self.affix_b.value}],
                       affix_db=self.db)
        self.assertIn("同名固定词条不能修改", str(caught.exception))

    def test_clearing_a_fixed_slot_is_refused_too(self) -> None:
        save = self._save(fixed_first=True)
        with self.assertRaises(EditorError):
            plan_edits(save, [{"record_index": 3, "slot_index": 0,
                               "effect_id": EMPTY_EFFECT_ID}], affix_db=self.db)

    def test_writing_a_fixed_affix_into_a_normal_slot_is_refused(self) -> None:
        with self.assertRaises(EditorError) as caught:
            plan_edits(self.plain,
                       [{"record_index": 3, "slot_index": 1,
                         "effect_id": self.fixed_affix.effect_id,
                        "value": self.fixed_affix.value}],
                       affix_db=self.db)
        self.assertIn("同名固定词条", str(caught.exception))

    def test_normal_slots_still_work(self) -> None:
        save = self._save(fixed_first=True)
        plans = plan_edits(save, [{"record_index": 3, "slot_index": 1,
                                   "effect_id": self.affix_a.effect_id,
                                   "value": self.affix_a.value}], affix_db=self.db)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].after[1].effect_id, self.affix_a.effect_id)

    def test_describe_marks_the_fixed_slot(self) -> None:
        view = list_accessories(self._save(fixed_first=True))[0]
        lines = view.describe_effects(self.db)
        self.assertIn("固定词条，不可修改", lines[1])
        self.assertNotIn("不可修改", lines[2])


class KindSwapTests(EditorTestCase):
    """种类互换: same 中类 only, fixed affix copied from a real sample."""

    # Two synthetic 饰品 kinds of the same 中类 plus one of another 中类.  The fixed
    # affixes are real catalogued ids, because the fixed marker is only trusted
    # for ids that are in the catalog.
    SAMURAI_A = 0x1001
    SAMURAI_B = 0x1002
    NINJA_A = 0x2001
    UNKNOWN = 0x3001

    def setUp(self) -> None:
        super().setUp()
        self.item_db = ItemDb([
            ItemEntry(self.SAMURAI_A, "甲[武士]", "武士饰品"),
            ItemEntry(self.SAMURAI_B, "乙[武士]", "武士饰品"),
            ItemEntry(self.NINJA_A, "丙[忍者]", "忍者饰品"),
        ])

    def _record(self, item_id: int, fixed_id: int, *, value: int = 7,
                meta9: int = 0x4C) -> bytes:
        return support.build_record(
            record_type=item_id,
            effects=((fixed_id, value, 0x5C000000 | meta9 << 8),
                     (self.affix_a.effect_id, 20, 0x0040)),
        )

    @property
    def fixed_a(self) -> int:
        """Two *real* catalogued 同名固定 affixes: a synthetic id would not be
        recognised as fixed, because the marker is only trusted in-catalog."""
        return self.fixed_affixes[0].effect_id

    @property
    def fixed_b(self) -> int:
        return self.fixed_affixes[1].effect_id

    def _save(self, **records: bytes) -> bytes:
        """``_save(**{"3": record})`` — dict-unpacked keys are strings, so cast."""
        return support.build_plain_save(
            records_by_slot={int(slot): record for slot, record in records.items()})

    def test_a_same_category_swap_copies_the_fixed_affix(self) -> None:
        save = self._save(**{"3": self._record(self.SAMURAI_A, self.fixed_a),
                             "11": self._record(self.SAMURAI_B, self.fixed_b,
                                                value=9)})
        plan = plan_kind_swap(save, 3, self.SAMURAI_B, affix_db=self.db,
                              item_db=self.item_db)
        self.assertEqual((plan.old_item_id, plan.new_item_id),
                         (self.SAMURAI_A, self.SAMURAI_B))
        self.assertEqual(plan.fixed_after, ((0, self.fixed_b, 9, 0x4C),))
        patched = apply_kind_swaps(save, [plan])
        view = next(view for view in list_accessories(patched, known_ids=None)
                    if view.slot_index == 3)
        self.assertEqual(view.record_type, self.SAMURAI_B)
        self.assertEqual(view.effects[0].effect_id, self.fixed_b)
        self.assertEqual(view.effects[0].value, 9)
        # The per-instance fields of the slot are left alone: only id/value/byte9
        # are kind properties (byte 10/11 vary per copy of a kind).
        expected = support.build_record(record_type=self.SAMURAI_A,
                                        effects=((self.fixed_a, 7, 0x5C000000 | 0x4C << 8),))
        expected_prefix = records.read_effect_slots(expected)[0].prefix
        self.assertEqual(view.effects[0].prefix, expected_prefix)
        self.assertEqual(view.effects[0].metadata, 0x5C000000 | 0x4C << 8)
        # Normal slots are untouched.
        self.assertEqual(view.effects[1].effect_id, self.affix_a.effect_id)

    def test_the_item_id_is_written_to_both_header_fields(self) -> None:
        save = self._save(**{"3": self._record(self.SAMURAI_A, self.fixed_a),
                             "11": self._record(self.SAMURAI_B, self.fixed_b)})
        plan = plan_kind_swap(save, 3, self.SAMURAI_B, affix_db=self.db,
                              item_db=self.item_db)
        patched = apply_kind_swaps(save, [plan])
        offset = plan.offset
        self.assertEqual(patched[offset:offset + 2],
                         patched[offset + 2:offset + 4])
        self.assertEqual(int.from_bytes(patched[offset:offset + 2], "little"),
                         self.SAMURAI_B)

    def test_only_the_header_and_fixed_slot_change(self) -> None:
        save = self._save(**{"3": self._record(self.SAMURAI_A, self.fixed_a),
                             "11": self._record(self.SAMURAI_B, self.fixed_b,
                                                value=9)})
        plan = plan_kind_swap(save, 3, self.SAMURAI_B, affix_db=self.db,
                              item_db=self.item_db)
        patched = apply_kind_swaps(save, [plan])
        differences = {index - plan.offset for index, (old, new)
                       in enumerate(zip(save, patched)) if old != new}
        allowed = {0x00, 0x01, 0x02, 0x03} | set(
            range(0x34, 0x34 + 0x18))
        self.assertTrue(differences <= allowed, sorted(differences))
        self.assertIn(0x00, differences)

    def test_a_cross_category_swap_is_refused(self) -> None:
        save = self._save(**{"3": self._record(self.SAMURAI_A, self.fixed_a),
                             "11": self._record(self.NINJA_A, self.fixed_b)})
        with self.assertRaises(KindSwapError) as caught:
            plan_kind_swap(save, 3, self.NINJA_A, affix_db=self.db,
                           item_db=self.item_db)
        self.assertIn("只能同分类互换", str(caught.exception))

    def test_a_target_kind_absent_from_the_save_is_refused(self) -> None:
        save = self._save(**{"3": self._record(self.SAMURAI_A, self.fixed_a)})
        with self.assertRaises(KindSwapError) as caught:
            plan_kind_swap(save, 3, self.SAMURAI_B, affix_db=self.db,
                           item_db=self.item_db)
        self.assertIn("没有种类", str(caught.exception))

    def test_an_ambiguous_target_kind_is_refused(self) -> None:
        save = self._save(**{"3": self._record(self.SAMURAI_A, self.fixed_a),
                             "11": self._record(self.SAMURAI_B, self.fixed_b),
                             "12": self._record(self.SAMURAI_B, self.fixed_affixes[2].effect_id)})
        with self.assertRaises(KindSwapError) as caught:
            plan_kind_swap(save, 3, self.SAMURAI_B, affix_db=self.db,
                           item_db=self.item_db)
        self.assertIn("不一致", str(caught.exception))

    def test_a_kind_outside_the_item_table_cannot_be_swapped(self) -> None:
        save = self._save(**{"3": self._record(self.UNKNOWN, self.fixed_a),
                             "11": self._record(self.SAMURAI_B, self.fixed_b)})
        with self.assertRaises(KindSwapError) as caught:
            plan_kind_swap(save, 3, self.SAMURAI_B, affix_db=self.db,
                           item_db=self.item_db)
        self.assertIn("不在物品种类表内", str(caught.exception))

    def test_a_kind_without_a_fixed_affix_swaps_without_touching_slots(self) -> None:
        """A kind whose copies carry no 同名固定 affix: only the header changes."""
        plain = support.build_record(record_type=self.SAMURAI_A,
                                     effects=((self.affix_a.effect_id, 20, 0x40),))
        target = support.build_record(record_type=self.SAMURAI_B,
                                      effects=((self.affix_b.effect_id, 15, 0x40),))
        save = self._save(**{"3": plain, "11": target})
        plan = plan_kind_swap(save, 3, self.SAMURAI_B, affix_db=self.db,
                              item_db=self.item_db)
        self.assertEqual(plan.fixed_after, ())
        patched = apply_kind_swaps(save, [plan])
        offset = plan.offset
        differences = {index - offset for index, (old, new)
                       in enumerate(zip(save, patched)) if old != new}
        # Only the two mirrored id fields may change (0x1001 → 0x1002 touches the
        # low byte of each little-endian copy), and nothing else in the record.
        self.assertTrue(differences <= {0x00, 0x01, 0x02, 0x03}, sorted(differences))
        self.assertEqual(differences, {0x00, 0x02})
    def test_same_kind_is_refused(self) -> None:
        save = self._save(**{"3": self._record(self.SAMURAI_A, self.fixed_a)})
        with self.assertRaises(KindSwapError):
            plan_kind_swap(save, 3, self.SAMURAI_A, affix_db=self.db,
                           item_db=self.item_db)

    def test_samples_report_ambiguity_per_kind(self) -> None:
        save = self._save(**{"3": self._record(self.SAMURAI_A, self.fixed_a),
                             "11": self._record(self.SAMURAI_B, self.fixed_b),
                             "12": self._record(self.SAMURAI_B, self.fixed_affixes[2].effect_id)})
        samples = collect_kind_samples(save, affix_db=self.db)
        self.assertFalse(samples[self.SAMURAI_A].ambiguous)
        self.assertEqual(samples[self.SAMURAI_A].copies, 1)
        self.assertTrue(samples[self.SAMURAI_B].ambiguous)
        self.assertEqual(samples[self.SAMURAI_B].copies, 2)

    def test_resolve_item_id_accepts_names_and_ids(self) -> None:
        self.assertEqual(resolve_item_id(self.item_db, "甲[武士]"),
                         self.SAMURAI_A)
        self.assertEqual(resolve_item_id(self.item_db, "0x1002"),
                         self.SAMURAI_B)
        with self.assertRaises(KindSwapError):
            resolve_item_id(self.item_db, "不存在的饰品")
        with self.assertRaises(KindSwapError):
            resolve_item_id(self.item_db, "0x9999")


class SoulCoreTests(unittest.TestCase):
    """魂核: own affix pool, no 恩宠/套装, 同名固定 and 种类/等级 rules unchanged."""

    #: Two real 魂核 kinds and two real 魂核 affixes from the shipped tables.
    SOUL_A = 0x3da6
    SOUL_B = 0x9443

    @classmethod
    def setUpClass(cls) -> None:
        cls.soul_db = AffixDb(load_soul_catalog())
        cls.soul_item_db = ItemDb(load_soul_item_catalog())
        cls.fixed = [entry for entry in cls.soul_db.all() if entry.is_fixed]
        cls.free = [entry for entry in cls.soul_db.all() if not entry.is_fixed]
        cls.soul_known = soul_catalog_ids(cls.soul_db)

    def _record(self, item_id: int, fixed_id: int, *, level: int = 170,
                value: int = 7) -> bytes:
        return support.build_record(
            record_type=item_id, level=level, rarity=5,
            effects=((fixed_id, value, 0x5C000000 | 0x4C << 8),
                     (self.free[0].effect_id, 15, 0x0040)),
        )

    def _save(self, **records: bytes) -> bytes:
        return support.build_plain_save(
            records_by_slot={int(slot): record for slot, record in records.items()})

    def _layout(self, save: bytes):
        return records.locate_layout(save, known_ids=self.soul_known)

    def _cores(self, save: bytes):
        layout = self._layout(save)
        return layout, list_soul_cores(save, soul_db=self.soul_db,
                                       soul_item_db=self.soul_item_db,
                                       layout=layout, known_ids=self.soul_known)

    def test_a_real_core_is_identified_with_its_fixed_slot(self) -> None:
        save = self._save(**{"3": self._record(self.SOUL_A, self.fixed[0].effect_id)})
        _layout, cores = self._cores(save)
        self.assertEqual([core.slot_index for core in cores], [3])
        self.assertFalse(cores[0].unidentified)
        self.assertEqual(cores[0].describe_item(self.soul_item_db),
                         f"魂核 {self.SOUL_A:#06x} 狱卒鬼（焦热）的魂核")
        self.assertEqual(cores[0].fixed_slots(self.soul_db), frozenset({0}))
        self.assertEqual(cores[0].slot_role(1, self.soul_db), "魂核词条")

    def test_an_id_outside_the_soul_table_is_reported_not_edited(self) -> None:
        """`0xda62` in the real save: core-shaped, but not in the 魂核 sheet."""
        save = self._save(**{"3": self._record(0xDA62, self.fixed[0].effect_id)})
        _layout, cores = self._cores(save)
        self.assertEqual(len(cores), 1)
        self.assertTrue(cores[0].unidentified)
        self.assertIn("不在魂核种类表内", cores[0].unidentified)
        with self.assertRaises(EditorError) as caught:
            plan_soul_edits(save, ({"record_index": 3, "slot_index": 1,
                                    "effect_id": self.free[1].effect_id,
                                    "value": 19, "metadata": 0x40},
                                   ),
                            soul_db=self.soul_db,
                            soul_item_db=self.soul_item_db,
                            known_ids=self.soul_known, layout=self._layout(save))
        self.assertIn("未识别", str(caught.exception))

    def test_a_fixed_slot_cannot_be_edited(self) -> None:
        save = self._save(**{"3": self._record(self.SOUL_A, self.fixed[0].effect_id)})
        with self.assertRaises(EditorError) as caught:
            plan_soul_edits(save, ({"record_index": 3, "slot_index": 0,
                                    "effect_id": self.free[1].effect_id,
                                    "value": 19, "metadata": 0x40},),
                            soul_db=self.soul_db,
                            soul_item_db=self.soul_item_db,
                            known_ids=self.soul_known, layout=self._layout(save))
        self.assertIn("魂核固定词条不能修改", str(caught.exception))

    def test_a_normal_slot_may_become_another_soul_affix(self) -> None:
        save = self._save(**{"3": self._record(self.SOUL_A, self.fixed[0].effect_id)})
        layout = self._layout(save)
        patched = apply_soul_edits(
            save,
            ({"record_index": 3, "slot_index": 1,
              "effect_id": self.free[1].effect_id,
              "value": self.free[1].value, "metadata": 0x40},),
            soul_db=self.soul_db, soul_item_db=self.soul_item_db,
            known_ids=self.soul_known, layout=layout,
        )
        core = list_soul_cores(patched, soul_db=self.soul_db,
                               soul_item_db=self.soul_item_db, layout=layout,
                               known_ids=self.soul_known)[0]
        self.assertEqual(core.effects[1].effect_id, self.free[1].effect_id)
        self.assertEqual(core.effects[0].effect_id, self.fixed[0].effect_id,
                         "固定词条必须原样保留")

    def test_a_grace_id_is_refused_because_a_core_has_none(self) -> None:
        """魂核没有恩宠/套装: the 恩宠 id is simply not in the 魂核 pool."""
        grace_id = next(entry.effect_id for entry in load_grace_catalog()
                        if entry.category in ("恩宠", "上位恩宠"))
        self.assertIsNone(self.soul_db.lookup(grace_id))
        save = self._save(**{"3": self._record(self.SOUL_A, self.fixed[0].effect_id)})
        with self.assertRaises(AffixError):
            plan_soul_edits(save, ({"record_index": 3, "slot_index": 4,
                                    "effect_id": grace_id, "value": 1,
                                    "metadata": 0x0C},),
                            soul_db=self.soul_db,
                            soul_item_db=self.soul_item_db,
                            known_ids=self.soul_known, layout=self._layout(save))

    def test_the_level_cap_applies_to_cores_too(self) -> None:
        save = self._save(**{"3": self._record(self.SOUL_A, self.fixed[0].effect_id)})
        layout = self._layout(save)
        plan = plan_level_edit(save, 3, 180, affix_db=self.soul_db,
                               known_ids=self.soul_known, layout=layout)
        patched = apply_level_edits(save, [plan])
        core = list_soul_cores(patched, soul_db=self.soul_db,
                               soul_item_db=self.soul_item_db, layout=layout,
                               known_ids=self.soul_known)[0]
        self.assertEqual(core.level, 180)
        with self.assertRaises(LevelEditError):
            plan_level_edit(save, 3, 181, affix_db=self.soul_db,
                            known_ids=self.soul_known, layout=layout)

    def test_a_core_swap_copies_the_target_kinds_fixed_affix(self) -> None:
        save = self._save(
            **{"3": self._record(self.SOUL_A, self.fixed[0].effect_id),
               "5": self._record(self.SOUL_B, self.fixed[1].effect_id, value=9)}
        )
        layout = self._layout(save)
        plan = plan_kind_swap(save, 3, self.SOUL_B, affix_db=self.soul_db,
                              item_db=self.soul_item_db, known_ids=self.soul_known,
                              layout=layout)
        patched = apply_kind_swaps(save, [plan])
        core = list_soul_cores(patched, soul_db=self.soul_db,
                               soul_item_db=self.soul_item_db, layout=layout,
                               known_ids=self.soul_known)[0]
        self.assertEqual(core.record_type, self.SOUL_B)
        self.assertEqual(core.effects[0].effect_id, self.fixed[1].effect_id,
                         "固定词条按新种类自动同步")
        self.assertEqual(core.effects[0].value, 9)
        self.assertEqual(core.levels_ok if hasattr(core, "levels_ok") else core.level,
                         core.level)

    def test_an_accessory_id_is_never_treated_as_a_core(self) -> None:
        """The one shared id (0xfb24) is 饰品词条 *and* 魂核词条; the item table decides."""
        shared = next(entry for entry in self.soul_db.all()
                      if self.db_lookup_shared(entry.effect_id))
        self.assertIsNotNone(shared)
        save = self._save(**{"3": support.build_record(
            record_type=0x4987,  # 八尺琼勾玉[武士], an accessory
            effects=((self.free[0].effect_id, 15, 0x40),))})
        _layout, cores = self._cores(save)
        self.assertEqual([core.unidentified for core in cores if core.unidentified],
                         [cores[0].unidentified] if cores else [])
        for core in cores:
            self.assertTrue(core.unidentified, "非魂核种类不得进入魂核列表")

    @staticmethod
    def db_lookup_shared(effect_id: int) -> bool:
        return AffixDb().lookup(effect_id) is not None


class ValueRangeTests(EditorTestCase):
    """词条数值: bounded by the workbook's own 取值集合 span."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.ranged = [entry for entry in cls.free_affixes
                      if entry.has_value_range and entry.value_min != entry.value_max]
        cls.single = [entry for entry in cls.free_affixes
                      if entry.has_value_range and entry.value_min == entry.value_max]

    def _save(self, effect_id: int, value: int) -> bytes:
        return support.build_plain_save(records_by_slot={
            3: support.build_record(record_type=ITEM_TYPE, level=150,
                                    effects=((effect_id, value, 0x0040),))})

    def test_the_catalogue_carries_a_range_for_every_affix(self) -> None:
        self.assertTrue(all(entry.has_value_range for entry in self.db.all()),
                        "原始表给出的区间必须覆盖全部词条")
        self.assertTrue(self.ranged, "应当存在可调数值的词条")

    def test_a_value_inside_the_span_is_written(self) -> None:
        entry = self.ranged[0]
        for wanted in (entry.value_min, entry.value_max):
            save = self._save(entry.effect_id, entry.value_min)
            patched = apply_edits(
                save, ({"record_index": 3, "slot_index": 0,
                        "effect_id": entry.effect_id, "value": wanted,
                        "metadata": 0x40},),
                affix_db=self.db)
            layout = records.locate_layout(patched)
            slots = records.read_effect_slots(
                patched[layout.offset(3):][:records.SCROLL_RECORD_SIZE])
            self.assertEqual(slots[0].value, wanted)

    def test_a_value_outside_the_span_is_refused(self) -> None:
        entry = self.ranged[0]
        save = self._save(entry.effect_id, entry.value_min)
        for bad in (entry.value_min - 1, entry.value_max + 1):
            with self.assertRaises(EditorError) as caught:
                plan_edits(save, ({"record_index": 3, "slot_index": 0,
                                   "effect_id": entry.effect_id, "value": bad,
                                   "metadata": 0x40},), affix_db=self.db)
            self.assertIn("数值必须在", str(caught.exception))

    def test_a_single_valued_affix_keeps_its_value(self) -> None:
        entry = self.single[0]
        save = self._save(entry.effect_id, entry.value_max)
        with self.assertRaises(EditorError):
            plan_edits(save, ({"record_index": 3, "slot_index": 0,
                               "effect_id": entry.effect_id,
                               "value": entry.value_max + 1, "metadata": 0x40},),
                       affix_db=self.db)


class AffixSearchTests(unittest.TestCase):
    """关键词匹配: every match is returned, and no match returns nothing."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = AffixDb()

    def test_a_keyword_matches_many_entries(self) -> None:
        matches = self.db.search("伤害")
        self.assertGreater(len(matches), 1)
        self.assertTrue(any("伤害" in entry.name for entry in matches))

    def test_spaces_inside_the_keyword_are_ignored(self) -> None:
        self.assertEqual(self.db.search("近距离 攻击"), self.db.search("近距离攻击"))
        self.assertTrue(self.db.search("近距离攻击"))

    def test_an_id_matches_its_own_entry(self) -> None:
        entry = self.db.all()[3]
        self.assertIn(entry, self.db.search(f"{entry.effect_id:x}"))

    def test_a_keyword_with_no_match_returns_nothing(self) -> None:
        self.assertEqual(self.db.search("绝不可能存在的词条名字"), ())

    def test_a_blank_keyword_matches_nothing(self) -> None:
        self.assertEqual(self.db.search("   "), ())

    def test_a_limit_caps_the_result(self) -> None:
        self.assertGreater(len(self.db.search("伤害")), 3)
        self.assertEqual(len(self.db.search("伤害", limit=3)), 3)


class CreationTests(EditorTestCase):
    """无中生有: a new item in a free slot, or a refusal with the reason."""

    KIND = ITEM_TYPE

    def _record(self, *, level: int = 150,
                effects: tuple[tuple[int, int, int], ...] | None = None) -> bytes:
        if effects is None:
            effects = ((self.fixed_affix.effect_id, 7, 0x40 << 8),)
        return support.build_record(record_type=self.KIND, level=level,
                                    effects=effects)

    def _db(self, item_id: int | None = None, name: str = "甲[武士]") -> ItemDb:
        return ItemDb([ItemEntry(item_id if item_id is not None else self.KIND,
                                 name, "武士饰品")])

    def test_a_new_item_fills_the_first_free_slot(self) -> None:
        save = support.build_plain_save(records_by_slot={3: self._record()})
        layout = records.locate_layout(save)
        first_free = find_free_slots(save, layout=layout).free_slots[0]
        plan = plan_creation(save, record_type=self.KIND, level=180,
                             effects=({"slot_index": 1,
                                       "effect_id": self.affix_a.effect_id,
                                       "value": self.affix_a.value},),
                             affix_db=self.db, item_db=self._db(), layout=layout)
        self.assertEqual(plan.slot_index, first_free)
        patched = apply_creations(save, [plan])
        views = [view for view in list_accessories(patched, layout=layout,
                                                  known_ids=frozenset())
                 if view.slot_index == plan.slot_index]
        self.assertEqual(len(views), 1)
        self.assertEqual(views[0].record_type, self.KIND)
        self.assertEqual(views[0].level, 180)
        self.assertEqual(views[0].effects[0].effect_id,
                         self.fixed_affix.effect_id, "固定词条按模板带入")
        self.assertEqual(views[0].effects[1].effect_id, self.affix_a.effect_id)

    def test_a_full_bag_is_refused_with_the_users_wording(self) -> None:
        one = support.build_plain_save(records_by_slot={3: self._record()})
        slots = records.locate_layout(one).slot_count
        every = {index: self._record() for index in range(slots)}
        save = support.build_plain_save(records_by_slot=every)
        self.assertTrue(find_free_slots(save).is_full, "该 fixture 必须是满背包")
        with self.assertRaises(CreationError) as caught:
            plan_creation(save, record_type=self.KIND, level=180,
                          affix_db=self.db, item_db=self._db())
        self.assertIn("背包已满", str(caught.exception))
        self.assertIn("清理", str(caught.exception))

    def test_a_kind_without_a_donor_is_refused(self) -> None:
        save = support.build_plain_save(records_by_slot={3: self._record()})
        with self.assertRaises(CreationError) as caught:
            plan_creation(save, record_type=0x2202, level=180, affix_db=self.db,
                          item_db=self._db(0x2202, "乙[武士]"))
        self.assertIn("没有", str(caught.exception))

    def test_a_kind_outside_the_item_table_is_refused(self) -> None:
        save = support.build_plain_save(records_by_slot={3: self._record()})
        with self.assertRaises(CreationError) as caught:
            plan_creation(save, record_type=0x9999, level=180, affix_db=self.db,
                          item_db=self._db())
        self.assertIn("不在", str(caught.exception))

    def test_the_level_cap_applies_to_new_items(self) -> None:
        save = support.build_plain_save(records_by_slot={3: self._record()})
        with self.assertRaises(CreationError):
            plan_creation(save, record_type=self.KIND, level=181, affix_db=self.db,
                          item_db=self._db())

    def test_a_new_item_may_not_gain_a_fixed_affix(self) -> None:
        """要求 (1): 未改种类时，任何情况下都不能再加一个固定词条。"""
        save = support.build_plain_save(records_by_slot={3: self._record()})
        with self.assertRaises(CreationError) as caught:
            plan_creation(save, record_type=self.KIND, level=180,
                          effects=({"slot_index": 1,
                                    "effect_id": self.fixed_affixes[1].effect_id},),
                          affix_db=self.db, item_db=self._db())
        self.assertIn("固定词条", str(caught.exception))

    def test_a_new_items_own_fixed_slot_cannot_be_rewritten(self) -> None:
        save = support.build_plain_save(records_by_slot={3: self._record()})
        with self.assertRaises(CreationError) as caught:
            plan_creation(save, record_type=self.KIND, level=180,
                          effects=({"slot_index": 0,
                                    "effect_id": self.affix_a.effect_id},),
                          affix_db=self.db, item_db=self._db())
        self.assertIn("同名固定词条", str(caught.exception))

    def test_a_created_value_must_stay_inside_the_span(self) -> None:
        save = support.build_plain_save(records_by_slot={3: self._record()})
        entry = next(entry for entry in self.free_affixes
                     if entry.has_value_range and entry.value_min != entry.value_max)
        with self.assertRaises(CreationError) as caught:
            plan_creation(save, record_type=self.KIND, level=180,
                          effects=({"slot_index": 1, "effect_id": entry.effect_id,
                                    "value": entry.value_max + 1},),
                          affix_db=self.db, item_db=self._db())
        self.assertIn("数值必须在", str(caught.exception))

    def test_an_occupied_slot_is_refused_as_a_target(self) -> None:
        save = support.build_plain_save(records_by_slot={3: self._record()})
        with self.assertRaises(CreationError) as caught:
            plan_creation(save, record_type=self.KIND, level=180, affix_db=self.db,
                          item_db=self._db(), slot_index=3)
        self.assertIn("不是空位", str(caught.exception))

    def test_the_free_slot_report_counts_type_zero_slots(self) -> None:
        save = support.build_plain_save(
            records_by_slot={index: self._record() for index in range(5)})
        report = find_free_slots(save)
        self.assertFalse(report.is_full)
        self.assertEqual(report.free_count + 5, report.slot_count)
        self.assertEqual(report.free_slots[0], 5)


class FixedSlotAdditionTests(EditorTestCase):
    """要求 (1): 0→1、1→2、2→3 三种情况都必须拒绝。"""

    def _save(self, *, extra_fixed: bool) -> bytes:
        effects = [(self.fixed_affix.effect_id, 7, 0x40 << 8)]
        if extra_fixed:
            effects.append((self.fixed_affixes[1].effect_id, 9, 0x40 << 8))
        effects.append((self.affix_a.effect_id, 20, 0x40))
        return support.build_plain_save(records_by_slot={
            3: support.build_record(record_type=ITEM_TYPE,
                                    effects=tuple(effects))})

    def test_a_record_with_one_fixed_affix_cannot_gain_a_second(self) -> None:
        save = self._save(extra_fixed=False)
        with self.assertRaises(EditorError) as caught:
            plan_edits(save, ({"record_index": 3, "slot_index": 1,
                               "effect_id": self.fixed_affixes[1].effect_id,
                               "value": self.fixed_affixes[1].value},),
                       affix_db=self.db)
        self.assertIn("固定词条", str(caught.exception))

    def test_a_record_with_two_fixed_affixes_cannot_gain_a_third(self) -> None:
        save = self._save(extra_fixed=True)
        with self.assertRaises(EditorError) as caught:
            plan_edits(save, ({"record_index": 3, "slot_index": 2,
                               "effect_id": self.fixed_affixes[2].effect_id,
                               "value": self.fixed_affixes[2].value},),
                       affix_db=self.db)
        self.assertIn("固定词条", str(caught.exception))

    def test_a_record_with_no_fixed_affix_cannot_gain_one(self) -> None:
        """A kind that has no fixed affix must never acquire one either."""
        save = support.build_plain_save(records_by_slot={
            3: support.build_record(record_type=ITEM_TYPE, level=150,
                                    effects=((self.affix_a.effect_id, 20, 0x40),))})
        for entry in self.fixed_affixes[:3]:
            with self.assertRaises(EditorError):
                plan_edits(save, ({"record_index": 3, "slot_index": 1,
                                   "effect_id": entry.effect_id,
                                   "value": entry.value},), affix_db=self.db)

    def test_a_fixed_affix_can_never_be_edited_in_place(self) -> None:
        save = self._save(extra_fixed=False)
        with self.assertRaises(EditorError) as caught:
            plan_edits(save, ({"record_index": 3, "slot_index": 0,
                               "effect_id": self.affix_a.effect_id,
                               "value": self.affix_a.value},), affix_db=self.db)
        self.assertIn("不能修改", str(caught.exception))


class LevelEditTests(EditorTestCase):
    """等级 edits: ``+0x06`` and its mirror ``+0x08``, capped at 180."""

    def test_plan_and_apply_write_both_level_fields(self) -> None:
        plan = plan_level_edit(self.plain, 3, 180, affix_db=self.db)
        self.assertEqual((plan.old_level, plan.new_level), (150, 180))
        patched = apply_level_edits(self.plain, [plan])
        offset = plan.offset
        record = patched[offset:offset + records.SCROLL_RECORD_SIZE]
        self.assertEqual(records.read_record_level(record), 180)
        self.assertEqual(records.read_record_level_mirror(record), 180)

    def test_only_the_level_bytes_change(self) -> None:
        plan = plan_level_edit(self.plain, 3, 170, affix_db=self.db)
        patched = apply_level_edits(self.plain, [plan])
        differences = {index for index, (old, new)
                       in enumerate(zip(self.plain, patched)) if old != new}
        allowed = {plan.offset + 0x06, plan.offset + 0x07,
                   plan.offset + 0x08, plan.offset + 0x09}
        self.assertTrue(differences <= allowed, sorted(differences))
        self.assertIn(plan.offset + 0x06, differences)
        self.assertIn(plan.offset + 0x08, differences)

    def test_above_the_game_cap_is_refused(self) -> None:
        with self.assertRaises(LevelEditError) as caught:
            plan_level_edit(self.plain, 3, 181, affix_db=self.db)
        self.assertIn("180", str(caught.exception))

    def test_zero_and_negative_levels_are_refused(self) -> None:
        for level in (0, -1):
            with self.assertRaises(LevelEditError):
                plan_level_edit(self.plain, 3, level, affix_db=self.db)

    def test_the_current_level_is_not_a_change(self) -> None:
        with self.assertRaises(LevelEditError):
            plan_level_edit(self.plain, 3, 150, affix_db=self.db)

    def test_a_mismatched_mirror_is_refused(self) -> None:
        """A record whose level fields disagree is not the shape we verified."""
        record = bytearray(self.record_3)
        struct.pack_into("<H", record, 0x08, 151)
        save = support.build_plain_save(records_by_slot={3: bytes(record)})
        # Locate the layout on the *intact* save: the damaged record no longer
        # satisfies the record-header heuristic (type/level must equal their
        # mirrors), so the reader refuses it — the same conclusion either way.
        layout = records.locate_layout(self.plain)
        with self.assertRaises(LevelEditError) as caught:
            plan_level_edit(save, 3, 170, affix_db=self.db, layout=layout)
        self.assertIn("不是物品记录", str(caught.exception))
        # The record-layer gate reports the mismatch itself.
        with self.assertRaises(RecordError) as caught:
            records.patch_record_level(bytes(record), 170)
        self.assertIn("不一致", str(caught.exception))

    def test_a_missing_record_is_refused(self) -> None:
        layout = records.locate_layout(self.plain)
        with self.assertRaises(LevelEditError) as caught:
            plan_level_edit(self.plain, 999, 170, affix_db=self.db,
                            layout=layout)
        self.assertIn("记录索引", str(caught.exception))

    def test_the_view_reports_the_mirror(self) -> None:
        view = list_accessories(self.plain)[0]
        self.assertEqual(view.level, 150)
        self.assertEqual(view.level_mirror, 150)
        self.assertIsInstance(view.plus_candidate, int)


class PlanTests(EditorTestCase):
    def test_plan_reports_before_and_after(self) -> None:
        plans = plan_edits(
            self.plain,
            [{"record_index": 3, "slot_index": 2,
              "effect_id": self.affix_b.effect_id,
                        "value": self.affix_b.value}],
            affix_db=self.db,
        )
        self.assertEqual(len(plans), 1)
        plan = plans[0]
        self.assertEqual(plan.record_index, 3)
        self.assertTrue(plan.before[2].is_empty)
        self.assertEqual(plan.after[2].effect_id, self.affix_b.effect_id)
        self.assertEqual(plan.after[2].value, self.affix_b.value)

    def test_plan_groups_edits_per_record(self) -> None:
        plans = plan_edits(
            self.plain,
            [
                {"record_index": 3, "slot_index": 0, "value": 1},
                {"record_index": 11, "slot_index": 1, "value": 2},
                {"record_index": 3, "slot_index": 4, "value": 3},
            ],
            affix_db=self.db,
        )
        self.assertEqual([plan.record_index for plan in plans], [3, 11])
        self.assertEqual(len(plans[0].edits), 2)

    def test_rejects_empty_edit_list(self) -> None:
        with self.assertRaises(EditorError):
            plan_edits(self.plain, [], affix_db=self.db)

    def test_rejects_an_illegal_affix(self) -> None:
        with self.assertRaises(AffixError):
            plan_edits(
                self.plain,
                [{"record_index": 3, "slot_index": 0, "effect_id": 0xDEADBEEF}],
                affix_db=self.db,
            )

    def test_allows_clearing_a_slot(self) -> None:
        plans = plan_edits(
            self.plain,
            [{"record_index": 3, "slot_index": 0, "effect_id": EMPTY_EFFECT_ID}],
            affix_db=self.db,
        )
        self.assertTrue(plans[0].after[0].is_empty)

    def test_rejects_a_missing_record(self) -> None:
        for index in (0, 4, 399):
            with self.assertRaises(EditorError) as caught:
                plan_edits(
                    self.plain,
                    [{"record_index": index, "slot_index": 0, "value": 1}],
                    affix_db=self.db,
                )
            self.assertIn("不在当前存档", str(caught.exception))

    def test_rejects_an_out_of_range_record_index(self) -> None:
        with self.assertRaises(Exception):
            plan_edits(
                self.plain,
                [{"record_index": 400, "slot_index": 0, "value": 1}],
                affix_db=self.db,
            )

    def test_rejects_missing_keys(self) -> None:
        for edit in (
            {"slot_index": 0, "value": 1},
            {"record_index": 3, "value": 1},
            {"record_index": "3", "slot_index": 0, "value": 1},
            {"record_index": 3, "slot_index": "0", "value": 1},
        ):
            with self.assertRaises(EditorError):
                plan_edits(self.plain, [edit], affix_db=self.db)

    def test_rejects_out_of_range_slot(self) -> None:
        for slot in (-1, 7, 100):
            with self.assertRaises(EditorError):
                plan_edits(
                    self.plain,
                    [{"record_index": 3, "slot_index": slot, "value": 1}],
                    affix_db=self.db,
                )

    def test_rejects_non_integer_effect_id(self) -> None:
        with self.assertRaises(EditorError):
            plan_edits(
                self.plain,
                [{"record_index": 3, "slot_index": 0, "effect_id": "0x1"}],
                affix_db=self.db,
            )

    def test_rejects_an_unknown_field(self) -> None:
        with self.assertRaises(RecordError):
            plan_edits(
                self.plain,
                [{"record_index": 3, "slot_index": 0, "bogus": 1}],
                affix_db=self.db,
            )


class ApplyTests(EditorTestCase):
    def test_apply_writes_the_edit_and_keeps_everything_else(self) -> None:
        patched = apply_edits(
            self.plain,
            [{"record_index": 3, "slot_index": 3,
              "effect_id": self.ranged_affix.effect_id,
                        "value": self.ranged_affix.value_min}],
            affix_db=self.db,
        )
        self.assertEqual(len(patched), len(self.plain))
        views = {view.slot_index: view for view in list_accessories(patched)}
        self.assertEqual(views[3].effects[3].effect_id, self.ranged_affix.effect_id)
        self.assertEqual(views[3].effects[3].value, self.ranged_affix.value_min)
        self.assertEqual(views[11].effects[0].effect_id, self.affix_b.effect_id)

    def test_apply_only_touches_the_target_record(self) -> None:
        patched = apply_edits(
            self.plain,
            [{"record_index": 3, "slot_index": 0, "value": 999}],
            affix_db=self.db,
        )
        layout = records.locate_layout(self.plain)
        target = records.record_offset(3, layout=layout)
        # Exactly one 0xE8-byte record may differ, at the located offset.
        self.assertNotEqual(patched[target:target + 0xE8],
                            self.plain[target:target + 0xE8])
        self.assertEqual(patched[:target], self.plain[:target])
        self.assertEqual(patched[target + 0xE8:], self.plain[target + 0xE8:])

    def test_apply_is_idempotent(self) -> None:
        edits = [{"record_index": 3, "slot_index": 1, "value": 5}]
        once = apply_edits(self.plain, edits, affix_db=self.db)
        twice = apply_edits(once, edits, affix_db=self.db)
        self.assertEqual(once, twice)

    def test_apply_can_clear_and_set_multiple_slots(self) -> None:
        patched = apply_edits(
            self.plain,
            [
                {"record_index": 3, "slot_index": 0, "effect_id": EMPTY_EFFECT_ID},
                {"record_index": 3, "slot_index": 6,
                 "effect_id": self.affix_b.effect_id, "value": 12},
            ],
            affix_db=self.db,
        )
        slots = list_accessories(patched)[0].effects
        self.assertTrue(slots[0].is_empty)
        self.assertEqual(slots[6].effect_id, self.affix_b.effect_id)

    def test_apply_unchecked_write_is_detected(self) -> None:
        """A silently dropped edit must abort instead of reporting success."""
        real_patch = records.patch_effect_slots
        real_plan = editor_module.plan_edits
        state = {"applying": False}

        def plan_then_arm(*args, **kwargs):
            plans = real_plan(*args, **kwargs)
            state["applying"] = True  # every later patch call is the write step
            return plans

        def drop_on_write(record, edits, **kwargs):
            if state["applying"]:
                return record  # simulated write that silently does nothing
            return real_patch(record, edits, **kwargs)

        with mock.patch.object(editor_module, "plan_edits", side_effect=plan_then_arm), \
                mock.patch("nioh3_accessory_editor.records.patch_effect_slots",
                           side_effect=drop_on_write):
            with self.assertRaises(EditorError) as caught:
                apply_edits(
                    self.plain,
                    [{"record_index": 3, "slot_index": 0, "value": 1}],
                    affix_db=self.db,
                )
        self.assertIn("未能正确写入", str(caught.exception))


class DiscoveryTests(EditorTestCase):
    def test_discover_saves_reads_the_fake_tree(self) -> None:
        save_root = self.root / "KoeiTecmo" / "NIOH3" / "Savedata"
        support.make_fake_save_tree(save_root, account=4242, slots=2)
        with mock.patch.dict("os.environ", {"LOCALAPPDATA": str(self.root)}):
            descriptors = discover_saves()
        self.assertEqual([d.slot_index for d in descriptors], [0, 1])
        self.assertEqual({d.account_id for d in descriptors}, {4242})
        self.assertIn("账号 4242", descriptors[0].display)

    def test_discover_saves_without_a_tree(self) -> None:
        with mock.patch.dict("os.environ", {"LOCALAPPDATA": str(self.root / "nope")}):
            self.assertEqual(discover_saves(), ())


@unittest.skipUnless(support.HAVE_EXE, "reference crypto executable is missing")
class CommitTests(EditorTestCase):
    def _make_save(self) -> tuple[SaveDescriptor, SaveCrypto]:
        crypto = SaveCrypto(support.EXE_PATH)
        directory = self.root / "76561198000000009" / "SAVEDATA01"
        directory.mkdir(parents=True)
        plain_path = self.root / "plain.bin"
        enc_path = self.root / "enc.bin"
        plain_path.write_bytes(self.plain)
        crypto.encrypt(plain_path, enc_path)
        target = directory / "SAVEDATA.BIN"
        target.write_bytes(enc_path.read_bytes())
        (directory / "BACKUP.BIN").write_bytes(b"game-backup")
        descriptor = SaveDescriptor(
            path=target, account_id=76561198000000009, slot_index=1,
            size=target.stat().st_size,
        )
        return descriptor, crypto

    def test_open_save_decrypts(self) -> None:
        descriptor, crypto = self._make_save()
        self.assertEqual(support.covered(open_save(descriptor, crypto)),
                         support.covered(self.plain))

    def test_dry_run_writes_nothing(self) -> None:
        descriptor, crypto = self._make_save()
        before = descriptor.path.read_bytes()
        patched = apply_edits(
            self.plain,
            [{"record_index": 3, "slot_index": 0, "value": 123}],
            affix_db=self.db,
        )
        report = commit_save(descriptor, patched, crypto=crypto,
                             state_root=self.root / "state", dry_run=True)
        self.assertTrue(report["dry_run"])
        self.assertIsNone(report["backup_dir"])
        self.assertIsNone(report["new_sha256"])
        self.assertEqual(descriptor.path.read_bytes(), before)
        self.assertFalse((self.root / "state").exists())

    def test_commit_persists_the_edit_and_backs_up(self) -> None:
        descriptor, crypto = self._make_save()
        patched = apply_edits(
            self.plain,
            [{"record_index": 11, "slot_index": 5,
              "effect_id": self.ranged_affix.effect_id,
                        "value": self.ranged_affix.value_min}],
            affix_db=self.db,
        )
        state_root = self.root / "state"
        report = commit_save(descriptor, patched, crypto=crypto,
                             state_root=state_root)
        self.assertFalse(report["dry_run"])
        self.assertTrue(report["verified"])
        self.assertEqual(len(str(report["new_sha256"])), 64)
        self.assertTrue(Path(str(report["backup_dir"])).is_dir())

        reloaded = open_save(descriptor, crypto)
        self.assertTrue(save_checksum_is_valid(reloaded))
        views = {view.slot_index: view for view in list_accessories(reloaded)}
        self.assertEqual(views[11].effects[5].value, self.ranged_affix.value_min)
        self.assertEqual(views[11].effects[5].effect_id, self.ranged_affix.effect_id)
        self.assertEqual(views[3].effects[0].effect_id, self.affix_a.effect_id)

    def test_commit_records_the_checksum_change(self) -> None:
        descriptor, crypto = self._make_save()
        patched = apply_edits(
            self.plain,
            [{"record_index": 3, "slot_index": 0, "value": 5}],
            affix_db=self.db,
        )
        report = commit_save(descriptor, patched, crypto=crypto,
                             state_root=self.root / "state")
        # The input buffer carries edits, so its stored checksum is stale by
        # construction; the report must say so and the recomputed value differ.
        self.assertFalse(report["checksum_was_consistent"])
        self.assertNotEqual(report["checksum_before"], report["checksum_after"])

    def test_commit_reports_a_consistent_input_when_nothing_changed(self) -> None:
        descriptor, crypto = self._make_save()
        report = commit_save(descriptor, self.plain, crypto=crypto,
                             state_root=self.root / "state", dry_run=True)
        self.assertTrue(report["checksum_was_consistent"])
        self.assertEqual(report["checksum_before"], report["checksum_after"])

    def test_commit_blocks_while_the_game_runs(self) -> None:
        # The gate lives in savefile (single implementation, shared with the
        # CLI/GUI notices), so that is the module to patch.
        descriptor, crypto = self._make_save()
        before = descriptor.path.read_bytes()
        with mock.patch.object(savefile_module, "running_game_processes",
                               return_value=("Nioh3.exe",)):
            with self.assertRaises(GameRunningError):
                commit_save(descriptor, self.plain, crypto=crypto,
                            state_root=self.root / "state")
        self.assertEqual(descriptor.path.read_bytes(), before)

    def test_commit_can_override_the_game_gate(self) -> None:
        descriptor, crypto = self._make_save()
        with mock.patch.object(savefile_module, "running_game_processes",
                               return_value=("Nioh3.exe",)):
            report = commit_save(descriptor, self.plain, crypto=crypto,
                                 state_root=self.root / "state",
                                 allow_game_running=True)
        self.assertEqual(report["game_processes_running"], ["Nioh3.exe"])

    def test_commit_rejects_invalid_payloads(self) -> None:
        descriptor, crypto = self._make_save()
        for payload in (b"nope", bytes(16)):
            with self.assertRaises(EditorError):
                commit_save(descriptor, payload, crypto=crypto,
                            state_root=self.root / "state")

    def test_commit_leaves_the_file_untouched_when_verification_fails(self) -> None:
        descriptor, crypto = self._make_save()
        original = descriptor.path.read_bytes()
        with mock.patch("nioh3_accessory_editor.savefile._verify_encrypted_file",
                        side_effect=Exception("boom")):
            with self.assertRaises(Exception):
                commit_save(descriptor, self.plain, crypto=crypto,
                            state_root=self.root / "state")
        self.assertEqual(descriptor.path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
