"""Editor service tests: planning, fail-closed validation, commit pipeline."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nioh3_accessory_editor import editor as editor_module
from nioh3_accessory_editor import records
from nioh3_accessory_editor import savefile as savefile_module
from nioh3_accessory_editor.affixdb import AffixDb, AffixError, GraceDb, ItemDb
from nioh3_accessory_editor.editor import (
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
        cls.affix_a = cls.db.all()[0]
        cls.affix_b = cls.db.all()[1]
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
                     (self.db.all()[2].effect_id, 30, 0x40),
                     (self.db.all()[3].effect_id, 5, 0x40),
                     (0x004FA3, 0, 0x29014C00)),
        )
        save = support.build_plain_save(records_by_slot={3: record})
        view = list_accessories(save, known_ids=frozenset(
            entry.effect_id for entry in self.db.all()))[0]
        self.assertTrue(view.is_accessory)
        self.assertEqual(view.catalog_hits, 4)
        self.assertEqual(view.grace_slots(self.db), frozenset({4}))

    def test_grace_slot_is_named_from_the_grace_table(self) -> None:
        """0x71f6 ends record #3 of the reporting user's save (不动明王的恩宠)."""
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
        effects += [(self.db.all()[index + 1].effect_id, 10, 0x40)
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
        # 0x00fb1d occurs in the reporting user's save but is in neither table.
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


class PlanTests(EditorTestCase):
    def test_plan_reports_before_and_after(self) -> None:
        plans = plan_edits(
            self.plain,
            [{"record_index": 3, "slot_index": 2,
              "effect_id": self.affix_b.effect_id, "value": 33}],
            affix_db=self.db,
        )
        self.assertEqual(len(plans), 1)
        plan = plans[0]
        self.assertEqual(plan.record_index, 3)
        self.assertTrue(plan.before[2].is_empty)
        self.assertEqual(plan.after[2].effect_id, self.affix_b.effect_id)
        self.assertEqual(plan.after[2].value, 33)

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
              "effect_id": self.affix_a.effect_id, "value": 77}],
            affix_db=self.db,
        )
        self.assertEqual(len(patched), len(self.plain))
        views = {view.slot_index: view for view in list_accessories(patched)}
        self.assertEqual(views[3].effects[3].effect_id, self.affix_a.effect_id)
        self.assertEqual(views[3].effects[3].value, 77)
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
              "effect_id": self.affix_a.effect_id, "value": 42}],
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
        self.assertEqual(views[11].effects[5].value, 42)
        self.assertEqual(views[11].effects[5].effect_id, self.affix_a.effect_id)
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
