"""Tkinter UI tests: widget wiring, edit collection, and write gating.

These build the real window (no main loop) so layout/attribute mistakes surface
as test failures.  They skip themselves when Tk cannot open a display.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nioh3_accessory_editor import ui
from nioh3_accessory_editor.affixdb import AffixDb
from nioh3_accessory_editor.editor import SaveDescriptor
from nioh3_accessory_editor.records import EFFECT_COUNT, EMPTY_EFFECT_ID
from tests import support

try:  # pragma: no cover - environment dependent
    import tkinter

    _probe = tkinter.Tk()
    _probe.destroy()
    TK_AVAILABLE = True
    TK_ERROR = ""
except Exception as error:  # pragma: no cover - environment dependent
    TK_AVAILABLE = False
    TK_ERROR = f"{type(error).__name__}: {error}"


class UiTestCase(unittest.TestCase):
    """Builds one window per test with save discovery stubbed out."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = AffixDb()
        cls.affix = cls.db.all()[0]
        cls.plan = support.build_plain_save(
            records_by_slot={
                3: support.build_record(
                    record_type=0x4001, level=150, rarity=5,
                    effects=((cls.affix.effect_id, 20, 0x40),),
                )
            }
        )

    def setUp(self) -> None:
        if not TK_AVAILABLE:
            self.skipTest(f"Tk unavailable ({TK_ERROR})")
        with mock.patch.object(ui.AccessoryEditorApp, "refresh_saves",
                               lambda self: None):
            self.app = ui.AccessoryEditorApp()
        self.addCleanup(self.app.destroy)
        self._temp = tempfile.TemporaryDirectory(prefix="nioh3-ui-")
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)

    def _select(self) -> None:
        self.app._populate_saves((SaveDescriptor(
            self.root / "SAVEDATA.BIN", 1234, 0, len(self.plan)),))
        self.app._populate_accessories(
            (self.plan, ui.list_accessories(self.plan), True)
        )
        self.app.tree.selection_set("3")
        self.app._on_accessory_selected()


@unittest.skipUnless(TK_AVAILABLE, f"Tk unavailable ({TK_ERROR})")
class ConstructionTests(UiTestCase):
    def test_window_shows_the_disclaimer(self) -> None:
        self.assertIn("仅供测试学习用", self.app.title())
        self.assertIn("不要用于联机影响游戏平衡", ui.DISCLAIMER)

    def test_one_combo_per_effect_slot_with_all_affixes(self) -> None:
        self.assertEqual(len(self.app.slot_combos), EFFECT_COUNT)
        values = self.app.slot_combos[0]["values"]
        self.assertEqual(len(values), len(self.db) + 1)
        self.assertEqual(values[0], "\u0028\u7a7a\u0029")

    def test_defaults_to_dry_run_with_verification(self) -> None:
        self.assertTrue(self.app.dry_run_var.get())
        self.assertTrue(self.app.verify_var.get())

    def test_initial_state_is_empty(self) -> None:
        self.assertIsNone(self.app.decrypted)
        self.assertIsNone(self.app.selected_accessory)
        self.assertEqual(self.app.accessory_views, [])


class SelectionTests(UiTestCase):
    def test_populate_saves_selects_the_first(self) -> None:
        descriptors = (
            SaveDescriptor(self.root / "a", 111, 0, 10),
            SaveDescriptor(self.root / "b", 222, 1, 10),
        )
        self.app._populate_saves(descriptors)
        self.assertEqual(self.app.selected_save, descriptors[0])
        self.assertEqual(len(self.app.save_combo["values"]), 2)

    def test_populate_saves_when_none_found(self) -> None:
        self.app._populate_saves(())
        self.assertIsNone(self.app.selected_save)
        self.assertIn("未发现", self.app.status_var.get())

    def test_populate_accessories_lists_records(self) -> None:
        self.app._populate_accessories((self.plan, ui.list_accessories(self.plan), True))
        self.assertEqual(self.app.tree.get_children(), ("3",))
        self.assertTrue(self.app.checksum_ok)
        self.assertIn("1 条记录", self.app.status_var.get())

    def test_populate_accessories_flags_a_bad_checksum(self) -> None:
        self.app._populate_accessories((self.plan, ui.list_accessories(self.plan), False))
        self.assertFalse(self.app.checksum_ok)
        self.assertIn("校验和不一致", self.app.status_var.get())

    def test_selecting_an_accessory_fills_the_slots(self) -> None:
        self._select()
        self.assertEqual(self.app.selected_accessory, 3)
        self.assertEqual(self.app.slot_combos[0].get(), self.affix.label)
        self.assertIn("数值=20", self.app.slot_labels[0].get())
        self.assertEqual(self.app.slot_combos[1].get(), "\u0028\u7a7a\u0029")

    def test_unknown_affix_is_shown_as_out_of_table(self) -> None:
        plan = support.build_plain_save(
            records_by_slot={0: support.build_record(
                record_type=0x4001, effects=((0xDEADBEEF, 1, 0),))}
        )
        self.app.decrypted = plan
        self.app._populate_accessories((plan, ui.list_accessories(plan), True))
        self.app.tree.selection_set("0")
        self.app._on_accessory_selected()
        self.assertIn("非表内词条", self.app.slot_combos[0].get())


@unittest.skipUnless(TK_AVAILABLE, f"Tk unavailable ({TK_ERROR})")
class EditCollectionTests(UiTestCase):
    def test_untouched_slots_produce_no_edits(self) -> None:
        self._select()
        self.assertEqual(self.app._current_edits(), ())

    def test_picking_a_new_affix_writes_id_and_value_only(self) -> None:
        self._select()
        other = self.db.all()[1]
        self.app.slot_combos[2].set(other.label)
        edits = self.app._current_edits()
        self.assertEqual(len(edits), 1)
        edit = edits[0]
        self.assertEqual(edit["record_index"], 3)
        self.assertEqual(edit["slot_index"], 2)
        self.assertEqual(edit["effect_id"], other.effect_id)
        self.assertEqual(edit["value"], other.value)
        # metadata is deliberately not touched: its bit layout is unverified.
        self.assertNotIn("metadata", edit)

    def test_metadata_is_preserved_across_an_edit(self) -> None:
        plan = support.build_plain_save(
            records_by_slot={3: support.build_record(
                record_type=0x4001,
                effects=((self.affix.effect_id, 20, 0x3F),),
            )}
        )
        self.app.decrypted = plan
        self.app._populate_accessories((plan, ui.list_accessories(plan), True))
        self.app.tree.selection_set("3")
        self.app._on_accessory_selected()
        other = self.db.all()[1]
        self.app.slot_combos[0].set(other.label)
        self.app.apply_edits_to_selection()
        view = next(v for v in self.app.accessory_views if v.slot_index == 3)
        self.assertEqual(view.effects[0].effect_id, other.effect_id)
        self.assertEqual(view.effects[0].metadata, 0x3F)

    def test_clearing_a_filled_slot_is_an_edit(self) -> None:
        self._select()
        self.app.slot_combos[0].set(ui.EMPTY_LABEL)
        edits = self.app._current_edits()
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["slot_index"], 0)
        self.assertEqual(edits[0]["effect_id"], EMPTY_EFFECT_ID)

    def test_clearing_an_empty_slot_is_not_an_edit(self) -> None:
        self._select()
        self.app.slot_combos[1].set(ui.EMPTY_LABEL)
        self.assertEqual(self.app._current_edits(), ())

    def test_reselecting_the_same_affix_is_not_an_edit(self) -> None:
        self._select()
        self.app.slot_combos[0].set(self.affix.label)
        self.assertEqual(self.app._current_edits(), ())

    def test_no_accessory_selected_yields_no_edits(self) -> None:
        self.assertEqual(self.app._current_edits(), ())

    def test_unparsable_label_is_rejected(self) -> None:
        self._select()
        self.app.slot_combos[0].set("garbage")
        with mock.patch.object(ui.messagebox, "showwarning") as warned:
            self.assertEqual(self.app._current_edits(), ())
        warned.assert_called_once()

    def test_out_of_table_label_is_rejected(self) -> None:
        self._select()
        self.app.slot_combos[0].set("0xdeadbeef (非表内词条)")
        with mock.patch.object(ui.messagebox, "showwarning") as warned:
            self.assertEqual(self.app._current_edits(), ())
        warned.assert_called_once()

    def test_apply_requires_a_loaded_save(self) -> None:
        with mock.patch.object(ui.messagebox, "showwarning") as warned:
            self.app.apply_edits_to_selection()
        warned.assert_called_once()

    def test_apply_without_changes_warns(self) -> None:
        self._select()
        with mock.patch.object(ui.messagebox, "showwarning") as warned:
            self.app.apply_edits_to_selection()
        warned.assert_called_once()
        self.assertIn("没有检测到改动", warned.call_args[0][1])

    def test_apply_updates_memory_and_reselects(self) -> None:
        self._select()
        other = self.db.all()[1]
        self.app.slot_combos[3].set(other.label)
        self.app.apply_edits_to_selection()
        view = next(v for v in self.app.accessory_views if v.slot_index == 3)
        self.assertEqual(view.effects[3].effect_id, other.effect_id)
        self.assertEqual(view.effects[3].value, other.value)
        self.assertEqual(self.app.selected_accessory, 3)
        self.assertIn("尚未写入存档", self.app.status_var.get())

    def test_apply_clear_then_the_slot_reads_empty(self) -> None:
        self._select()
        self.app.slot_combos[0].set(ui.EMPTY_LABEL)
        self.app.apply_edits_to_selection()
        view = next(v for v in self.app.accessory_views if v.slot_index == 3)
        self.assertTrue(view.effects[0].is_empty)
        self.assertEqual(self.app.slot_combos[0].get(), ui.EMPTY_LABEL)


@unittest.skipUnless(TK_AVAILABLE, f"Tk unavailable ({TK_ERROR})")
class WriteFlowTests(UiTestCase):
    def _run_worker_synchronously(self):
        """Replace the thread dispatch so the test observes results directly."""
        def immediate(function, *args):
            try:
                self.app._on_worker_ok(function(*args))
            except Exception as error:  # noqa: BLE001 - mirrors the real worker
                self.app._on_worker_error(error)

        return mock.patch.object(self.app, "_run_worker", side_effect=immediate)

    def test_write_requires_loaded_data(self) -> None:
        with mock.patch.object(ui.messagebox, "showwarning") as warned:
            self.app.write_save()
        warned.assert_called_once()

    def test_write_asks_for_confirmation_and_passes_flags(self) -> None:
        self._select()
        captured: dict[str, object] = {}

        def fake_commit(save, data, **kwargs):
            captured.update(kwargs)
            captured["data_len"] = len(data)
            return {"dry_run": False, "new_sha256": "AB" * 32}

        with mock.patch.object(ui, "running_game_processes", return_value=()), \
                mock.patch.object(ui.messagebox, "askyesno", return_value=True), \
                mock.patch.object(ui.messagebox, "showinfo") as informed, \
                mock.patch.object(ui, "commit_save", side_effect=fake_commit), \
                self._run_worker_synchronously():
            self.app.write_save()

        self.assertEqual(captured["dry_run"], True)
        self.assertEqual(captured["verify"], True)
        self.assertEqual(captured["allow_game_running"], True)
        self.assertEqual(captured["data_len"], len(self.plan))
        informed.assert_called_once()
        self.assertIn("AB", self.app.status_var.get())

    def test_write_aborts_when_the_user_declines(self) -> None:
        self._select()
        with mock.patch.object(ui.messagebox, "askyesno", return_value=False), \
                mock.patch.object(ui, "commit_save") as committed:
            self.app.write_save()
        committed.assert_not_called()

    def test_running_game_asks_before_writing(self) -> None:
        self._select()
        # First prompt (game running) answered "no" -> nothing is committed.
        with mock.patch.object(ui, "running_game_processes",
                               return_value=("Nioh3.exe",)), \
                mock.patch.object(ui.messagebox, "askyesno", return_value=False) as asked, \
                mock.patch.object(ui, "commit_save") as committed:
            self.app.write_save()
        asked.assert_called_once()
        committed.assert_not_called()

    def test_dry_run_result_shows_a_summary(self) -> None:
        self._select()
        with mock.patch.object(ui.messagebox, "askyesno", return_value=True), \
                mock.patch.object(ui.messagebox, "showinfo") as informed, \
                mock.patch.object(ui, "commit_save", return_value={
                    "dry_run": True, "checksum_before": "0x1", "checksum_after": "0x2",
                }), self._run_worker_synchronously():
            self.app.write_save()
        self.assertIn("演练", informed.call_args[0][0])

    def test_worker_error_is_surfaced(self) -> None:
        self._select()
        with mock.patch.object(ui.messagebox, "askyesno", return_value=True), \
                mock.patch.object(ui.messagebox, "showerror") as failed, \
                mock.patch.object(ui, "commit_save",
                                  side_effect=RuntimeError("boom")), \
                self._run_worker_synchronously():
            self.app.write_save()
        failed.assert_called_once()

    def test_backup_reports_the_directory(self) -> None:
        self._select()
        with mock.patch.object(ui, "create_backup",
                               return_value=self.root / "backup"), \
                mock.patch.object(ui.messagebox, "showinfo") as informed, \
                self._run_worker_synchronously():
            self.app.backup_save()
        self.assertIn("备份", informed.call_args[0][0])

    def test_backup_requires_a_selected_save(self) -> None:
        with mock.patch.object(ui.messagebox, "showwarning") as warned:
            self.app.backup_save()
        warned.assert_called_once()


if __name__ == "__main__":
    unittest.main()
