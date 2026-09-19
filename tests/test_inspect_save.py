"""Tests for tools/inspect_save.py: the read-only deep save inspection.

The tool exists to replace guesswork with evidence when no in-game reference is
available, so these tests check the *evidence*, not just the plumbing:

* the effect-slot base sweep must rank the captured base (0x34) first when the
  records really carry catalog ids there, and must not be fooled when they do
  not (a wrong base reports ~0 hits),
* accessory candidates must require *all* occupied slots to be catalog ids,
* the report must state that nothing was written and the game need not run.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from nioh3_accessory_editor import records
from nioh3_accessory_editor.affixdb import AffixDb
from nioh3_accessory_editor.config import default_config_document
from tests import support

_ROOT = Path(__file__).resolve().parent.parent
_NAME = "inspect_save_tool"
_SPEC = importlib.util.spec_from_file_location(_NAME,
                                              _ROOT / "tools" / "inspect_save.py")
inspect_save = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
# dataclasses looks its class up in sys.modules, so register before executing.
sys.modules[_NAME] = inspect_save
_SPEC.loader.exec_module(inspect_save)


class BaseSweepTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = AffixDb()
        cls.affixes = cls.db.all()
        cls.known = frozenset(entry.effect_id for entry in cls.affixes)

    def _save_with(self, records_by_slot: dict[int, bytes]) -> bytes:
        return support.build_plain_save(records_by_slot=records_by_slot)

    def test_captured_base_wins_when_records_carry_catalog_ids(self) -> None:
        effects = tuple((entry.effect_id, 20 + index, 0x40)
                        for index, entry in enumerate(self.affixes[:4]))
        save = self._save_with({
            1: support.build_record(record_type=0x4001, effects=effects),
            2: support.build_record(record_type=0x4002, effects=effects[:2]),
        })
        layout = records.locate_layout(save)
        scores = inspect_save.effect_slot_base_scores(save, layout, self.known)
        self.assertEqual(scores[0].base, records.EFFECT_START)
        self.assertTrue(scores[0].is_captured_base)
        self.assertEqual(scores[0].hits, 6)
        self.assertEqual(scores[0].occupied, 6)
        self.assertEqual(scores[0].rate, 1.0)
        self.assertEqual(scores[0].slot_indices, (0, 1, 2, 3))
        # A wrong base may still hit by chance, but must not beat this.
        for score in scores[1:]:
            self.assertLessEqual(score.hits, scores[0].hits)

    def test_stride_aliases_are_labelled_not_hidden(self) -> None:
        """Bases 0x18 apart read the same lattice shifted; label them."""
        effects = tuple((entry.effect_id, 20, 0x40)
                        for entry in self.affixes[:7])
        save = self._save_with({
            1: support.build_record(record_type=0x4001, effects=effects),
        })
        layout = records.locate_layout(save)
        scores = inspect_save.effect_slot_base_scores(save, layout, self.known)
        by_base = {score.base: score for score in scores}
        true_base = by_base[records.EFFECT_START]
        alias = by_base[records.EFFECT_START - records.EFFECT_STRIDE]
        self.assertTrue(alias.aliases_captured_base)
        self.assertFalse(true_base.aliases_captured_base)
        # Shifted down by one slot, the alias cannot see the first real slot.
        self.assertEqual(true_base.hits, 7)
        self.assertEqual(alias.hits, 6)
        self.assertLess(alias.hits, true_base.hits)
        self.assertEqual(scores[0].base, records.EFFECT_START)

    def test_a_save_with_random_ids_reports_no_evidence(self) -> None:
        """No catalog ids anywhere: the sweep must not invent a winner."""
        save = self._save_with({
            1: support.build_record(record_type=0x4001,
                                    effects=((0xDEADBEEF, 1, 0), (0x0BADF00D, 2, 0))),
        })
        layout = records.locate_layout(save)
        scores = inspect_save.effect_slot_base_scores(save, layout, self.known)
        self.assertEqual(scores[0].hits, 0)
        self.assertTrue(all(score.hits == 0 for score in scores))

    def test_empty_slots_are_not_counted_as_occupied(self) -> None:
        save = self._save_with({
            1: support.build_record(record_type=0x4001,
                                    effects=((self.affixes[0].effect_id, 5, 0),)),
        })
        layout = records.locate_layout(save)
        scores = inspect_save.effect_slot_base_scores(save, layout, self.known)
        best = next(score for score in scores if score.is_captured_base)
        self.assertEqual(best.occupied, 1)
        self.assertEqual(best.hits, 1)


class ReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = AffixDb()
        cls.affixes = cls.db.all()

    def test_inspect_separates_candidates_from_other_records(self) -> None:
        save = support.build_plain_save(records_by_slot={
            1: support.build_record(
                record_type=0x4001,
                effects=((self.affixes[0].effect_id, 20, 0x40),
                         (self.affixes[1].effect_id, 30, 0x40))),
            2: support.build_record(
                record_type=0x4002,
                effects=((0xDEADBEEF, 20, 0x40),)),
            3: support.build_record(record_type=0x1E82),
        })
        payload = inspect_save.inspect(save, self.db, preview_records=1)
        self.assertEqual(payload["layout"]["item_count"], 2)
        candidates = payload["accessory_candidates"]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["slot_index"], 1)
        self.assertEqual(candidates[0]["catalog_slots"], 2)
        self.assertEqual(candidates[0]["occupied_slots"], 2)
        self.assertEqual(candidates[0]["slots"][0]["name"], self.affixes[0].name)
        self.assertTrue(candidates[0]["slots"][2]["empty"])

        types = {entry["type"]: entry for entry in payload["types"]}
        self.assertEqual(types[0x4001]["count"], 1)
        self.assertEqual(types[0x4001]["slot_indices"], [0, 1])
        self.assertEqual(types[0x4002]["catalog_slots"], 0)

    def test_one_catalog_hit_already_marks_a_record_as_an_accessory(self) -> None:
        """The catalog only holds 饰品词条, so a single hit is the evidence.

        A real v2.21 accessory typically has one occupied slot whose id is *not*
        in the shipped catalog (the catalog is incomplete), so requiring every
        slot to hit would classify the user's own accessories as "other".
        """
        save = support.build_plain_save(records_by_slot={
            1: support.build_record(
                record_type=0x4001,
                effects=((self.affixes[0].effect_id, 20, 0x40),
                         (0xDEADBEEF, 20, 0x40))),
        })
        payload = inspect_save.inspect(save, self.db)
        self.assertEqual(len(payload["accessory_candidates"]), 1)
        self.assertEqual(payload["accessory_candidates"][0]["catalog_slots"], 1)
        self.assertEqual(payload["accessory_candidates"][0]["occupied_slots"], 2)
        self.assertEqual(
            [item["effect_id"] for item in payload["unknown_affix_ids"]],
            [0xDEADBEEF],
        )
        self.assertEqual(payload["unknown_affix_total"], 1)
        text = inspect_save._format_report(payload, self.db, listing=2)
        self.assertIn("词条库没有的 id", text)

    def test_a_record_without_catalog_ids_is_not_an_accessory(self) -> None:
        save = support.build_plain_save(records_by_slot={
            1: support.build_record(record_type=0x4001,
                                    effects=((0xDEADBEEF, 20, 0x40),)),
        })
        payload = inspect_save.inspect(save, self.db)
        self.assertEqual(payload["accessory_candidates"], [])
        self.assertEqual(len(payload["records"]), 1)
        text = inspect_save._format_report(payload, self.db, listing=2)
        self.assertIn("武器/防具/绘卷", text)

    def test_report_without_a_record_table_explains_itself(self) -> None:
        save = support.build_plain_save(pattern_body=False)
        payload = inspect_save.inspect(save, self.db)
        self.assertIsNone(payload["layout"])
        text = inspect_save._format_report(payload, self.db, listing=6)
        self.assertIn("未定位到", text)
        self.assertIn("scan", text)

    def test_report_names_the_trailing_grace_slot_and_flags_the_rest(self) -> None:
        from nioh3_accessory_editor.affixdb import GraceDb

        save = support.build_plain_save(records_by_slot={
            1: support.build_record(
                record_type=0x4001,
                effects=((self.affixes[0].effect_id, 20, 0x40), (0x004FA3, 0, 0))),
            2: support.build_record(
                record_type=0x4001,
                effects=((self.affixes[1].effect_id, 20, 0x40), (0x00FB1D, 0, 0))),
        })
        payload = inspect_save.inspect(save, self.db)
        self.assertEqual(payload["grace_affix_total"], 2)
        text = inspect_save._format_report(payload, self.db, listing=2,
                                           grace_db=GraceDb.best_effort())
        self.assertIn("稻荷神的恩宠", text)
        self.assertIn("名表未收录", text)
        self.assertIn("0x00fb1d", text)

    def test_report_without_a_name_table_still_lists_the_ids(self) -> None:
        save = support.build_plain_save(records_by_slot={
            1: support.build_record(
                record_type=0x4001,
                effects=((self.affixes[0].effect_id, 20, 0x40), (0x004FA3, 0, 0))),
        })
        payload = inspect_save.inspect(save, self.db)
        text = inspect_save._format_report(payload, self.db, listing=2)
        self.assertIn("末位槽", text)
        self.assertIn("0x004fa3", text)
        self.assertIn("名表里没有这个 id", text)

    def test_report_states_the_read_only_guarantee(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nioh3-inspect-") as temp:
            root = Path(temp)
            account = root / "saves" / "76561198000000042" / "SAVEDATA00"
            account.mkdir(parents=True)
            import os

            from nioh3_accessory_editor import savefile

            plain = support.build_plain_save(records_by_slot={
                1: support.build_record(
                    record_type=0x4001,
                    effects=((self.affixes[0].effect_id, 20, 0x40),)),
            })
            staged = root / "staged.bin"
            staged.write_bytes(plain)
            savefile.SaveCrypto(support.EXE_PATH).encrypt(staged,
                                                          account / "SAVEDATA.BIN")
            config = root / "editor.json"
            document = default_config_document()
            document["save_root"] = str(root / "saves")
            document["backup_root"] = str(root / "state")
            config.write_text(json.dumps(document, ensure_ascii=False),
                              encoding="utf-8")
            before = (account / "SAVEDATA.BIN").read_bytes()

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = inspect_save.main(["--config", str(config), "--records", "1"])
            text = out.getvalue()

            self.assertEqual(code, 0, text)
            self.assertIn("只读检查", text)
            self.assertIn("游戏无需运行", text)
            self.assertIn("仅供测试学习用", text)
            self.assertIn("效果槽基准搜索", text)
            self.assertIn("饰品（占用槽命中词条库）", text)
            self.assertEqual((account / "SAVEDATA.BIN").read_bytes(), before)
            self.assertFalse((root / "state").exists(),
                             "a read-only inspection must not create state")

    def test_json_output_is_written(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nioh3-inspect-json-") as temp:
            root = Path(temp)
            save_path = root / "saves" / "76561198000000042" / "SAVEDATA00"
            save_path.mkdir(parents=True)
            from nioh3_accessory_editor import savefile

            plain = support.build_plain_save(records_by_slot={
                1: support.build_record(record_type=0x4001),
            })
            staged = root / "staged.bin"
            staged.write_bytes(plain)
            savefile.SaveCrypto(support.EXE_PATH).encrypt(staged,
                                                          save_path / "SAVEDATA.BIN")
            target = root / "report.json"
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = inspect_save.main([
                    "--save", str(save_path / "SAVEDATA.BIN"),
                    "--json", str(target),
                ])
            self.assertEqual(code, 0, out.getvalue())
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(payload["account_id"], 76561198000000042)
            self.assertEqual(payload["slot_index"], 0)
            self.assertTrue(payload["checksum_consistent"])
            self.assertEqual(payload["layout"]["anchor"],
                             records.LEGACY_GROUP_OFFSET)
            self.assertEqual(len(payload["base_scores"]), 8)


if __name__ == "__main__":
    unittest.main()
