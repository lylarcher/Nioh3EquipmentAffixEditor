"""Editor service tests: planning, fail-closed validation, commit pipeline."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nioh3_accessory_editor import editor as editor_module
from nioh3_accessory_editor import records
from nioh3_accessory_editor.affixdb import AffixDb, AffixError
from nioh3_accessory_editor.editor import (
    EditorError,
    SaveDescriptor,
    apply_edits,
    commit_save,
    discover_saves,
    list_accessories,
    open_save,
    plan_edits,
    save_checksum_is_valid,
)
from nioh3_accessory_editor.records import EMPTY_EFFECT_ID, RecordError
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
        self.assertEqual(len(lines), 7)
        self.assertIn(self.affix_a.name, lines[0])
        self.assertIn("(空)", lines[1])

    def test_describe_unknown_affix(self) -> None:
        record = support.build_record(
            record_type=ITEM_TYPE, effects=((0xDEADBEEF, 1, 0),)
        )
        save = support.build_plain_save(records_by_slot={0: record})
        lines = list_accessories(save)[0].describe_effects(self.db)
        self.assertIn("未知词条", lines[0])

    def test_checksum_is_valid_on_the_fixture(self) -> None:
        self.assertTrue(save_checksum_is_valid(self.plain))


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
        other = records.record_offset(11)
        self.assertEqual(
            patched[other:other + 0xE8], self.plain[other:other + 0xE8]
        )

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
        descriptor, crypto = self._make_save()
        before = descriptor.path.read_bytes()
        with mock.patch.object(editor_module, "running_game_processes",
                               return_value=("Nioh3.exe",)):
            with self.assertRaises(GameRunningError):
                commit_save(descriptor, self.plain, crypto=crypto,
                            state_root=self.root / "state")
        self.assertEqual(descriptor.path.read_bytes(), before)

    def test_commit_can_override_the_game_gate(self) -> None:
        descriptor, crypto = self._make_save()
        with mock.patch.object(editor_module, "running_game_processes",
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
