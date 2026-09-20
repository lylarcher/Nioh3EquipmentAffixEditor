"""+値 (record byte ``+0x0A``) and the title-screen write gate.

``+0x0A`` was the last unverified header word.  It is now confirmed: the user's three
龙笛[武士] cards read +13 / +18 / +19, exactly the bytes this tool sees for records
#28 / #3 / #521, so it is a normal, supported edit.  The second half of this module
pins the GUI rule that a running game is still writable **only** after the operator
ticks the "game is on its title screen" box.
"""

from __future__ import annotations

import struct
import unittest
from unittest import mock

from nioh3_accessory_editor import records, ui
from nioh3_accessory_editor.editor import (
    AccessoryView,
    EditorError,
    apply_plus_edits,
    list_accessories,
    plan_plus_edit,
)
from nioh3_accessory_editor.affixdb import AffixDb
from tests import support
from tests.test_ui import TK_AVAILABLE, TK_ERROR, UiTestCase


def _db() -> AffixDb:
    return AffixDb()


def _free_affix():
    db = AffixDb()
    return next(entry for entry in db.all() if not entry.is_fixed)


class PlusValueEditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.affix = _free_affix()
        cls.plus_slot = 3
        cls.plus_value = 17
        cls.plan = support.build_plain_save(records_by_slot={
            cls.plus_slot: support.build_record(
                record_type=0x4001, level=150, rarity=5,
                effects=((cls.affix.effect_id, cls.affix.value, 0x40),),
                plus=cls.plus_value,
            ),
        })

    def test_the_record_fixture_carries_the_plus_value(self) -> None:
        record = records.read_item_record(self.plan, self.plus_slot)
        self.assertEqual(records.read_record_plus(record.record), self.plus_value)

    def test_view_exposes_it_as_plus_value(self) -> None:
        views = {view.slot_index: view for view in list_accessories(self.plan)}
        self.assertEqual(views[self.plus_slot].plus_value, self.plus_value)

    def test_the_deprecated_alias_still_reads_the_same_field(self) -> None:
        record = records.read_item_record(self.plan, self.plus_slot)
        self.assertEqual(records.read_record_plus_candidate(record.record),
                         records.read_record_plus(record.record))
        self.assertEqual(records.RECORD_PLUS_CANDIDATE_MAX, records.MAX_RECORD_PLUS)

    def test_plan_reads_the_old_value_and_the_new_one(self) -> None:
        plan = plan_plus_edit(self.plan, self.plus_slot, 4, affix_db=_db())
        self.assertEqual((plan.old_value, plan.new_value), (self.plus_value, 4))
        self.assertIn("+值", plan.describe())

    def test_only_the_plus_bytes_change(self) -> None:
        plan = plan_plus_edit(self.plan, self.plus_slot, 30, affix_db=_db())
        patched = apply_plus_edits(self.plan, [plan])
        offset = plan.offset + records.RECORD_PLUS_OFFSET
        changed = [index for index in range(len(self.plan))
                   if self.plan[index] != patched[index]]
        self.assertTrue(changed, "应当至少改一个字节")
        self.assertTrue(set(changed) <= {offset, offset + 1},
                        f"改动越界: {changed} 不在 {offset} 附近")
        self.assertEqual(struct.unpack_from("<H", patched, offset)[0], 30)
        # ...and the record still reads back as the same kind with the same affix.
        views = {view.slot_index: view for view in list_accessories(patched)}
        self.assertEqual(views[self.plus_slot].record_type, 0x4001)
        self.assertEqual(views[self.plus_slot].plus_value, 30)

    def test_zero_and_the_observed_maximum_are_both_accepted(self) -> None:
        db = _db()
        for value in (0, records.MAX_RECORD_PLUS):
            with self.subTest(value=value):
                plan = plan_plus_edit(self.plan, self.plus_slot, value, affix_db=db)
                self.assertEqual(plan.new_value, value)

    def test_out_of_range_values_are_refused(self) -> None:
        db = _db()
        for value in (-1, records.MAX_RECORD_PLUS + 1, 999):
            with self.subTest(value=value):
                with self.assertRaises(EditorError) as caught:
                    plan_plus_edit(self.plan, self.plus_slot, value, affix_db=db)
                self.assertIn("+值必须在", str(caught.exception))

    def test_a_non_integer_is_refused(self) -> None:
        with self.assertRaises(EditorError):
            plan_plus_edit(self.plan, self.plus_slot, "7", affix_db=_db())

    def test_setting_the_same_value_is_refused_instead_of_writing(self) -> None:
        with self.assertRaises(EditorError) as caught:
            plan_plus_edit(self.plan, self.plus_slot, self.plus_value,
                           affix_db=_db())
        self.assertIn("已经是", str(caught.exception))

    def test_an_empty_slot_is_refused(self) -> None:
        with self.assertRaises(EditorError):
            plan_plus_edit(self.plan, 0, 5, affix_db=_db())

    def test_the_writer_refuses_out_of_range_even_when_called_directly(self) -> None:
        record = records.read_item_record(self.plan, self.plus_slot).record
        with self.assertRaises(records.RecordError):
            records.patch_record_plus(record, records.MAX_RECORD_PLUS + 1)

    def test_the_patch_only_touches_the_plus_offset(self) -> None:
        record = records.read_item_record(self.plan, self.plus_slot).record
        patched = records.patch_record_plus(record, 1)
        self.assertEqual(len(patched), len(record))
        for index in range(len(record)):
            if index in (records.RECORD_PLUS_OFFSET, records.RECORD_PLUS_OFFSET + 1):
                continue
            self.assertEqual(patched[index], record[index], f"字节 {index:#x} 被改动")


class PlusValueGuiTests(UiTestCase):
    def test_the_tree_shows_a_plus_column(self) -> None:
        self.assertIn("plus", self.app.tree["columns"])
        self.assertEqual(self.app.tree.heading("plus")["text"], "+值")

    def test_selecting_a_record_fills_the_plus_box(self) -> None:
        self._select()
        view = next(view for view in self.app.accessory_views if view.slot_index == 3)
        self.assertEqual(self.app.plus_var.get(), str(view.plus_value))

    def test_applying_a_plus_value_edits_memory_only(self) -> None:
        self._select()
        before = self.app.decrypted
        self.app.plus_var.set("5")
        with mock.patch.object(ui.messagebox, "askokcancel", return_value=True):
            self.app.apply_plus_to_selection()
        self.assertIsNot(before, self.app.decrypted)
        views = {view.slot_index: view for view in self.app.accessory_views}
        self.assertEqual(views[3].plus_value, 5)
        row = self.app.tree.item("3")["values"]
        # Tk hands a numeric-looking cell back without a leading plus.
        self.assertEqual(str(row[1]), "5")

    def test_an_out_of_range_value_is_refused_with_a_dialog(self) -> None:
        self._select()
        self.app.plus_var.set(str(records.MAX_RECORD_PLUS + 5))
        with mock.patch.object(ui.messagebox, "askokcancel", return_value=True), \
                mock.patch.object(ui.messagebox, "showerror") as showerror:
            self.app.apply_plus_to_selection()
        showerror.assert_called_once()
        self.assertIn("+值必须在", showerror.call_args.args[1])

    def test_a_non_numeric_value_is_refused_before_any_plan(self) -> None:
        self._select()
        self.app.plus_var.set("abc")
        with mock.patch.object(ui.messagebox, "showwarning") as showwarning:
            self.app.apply_plus_to_selection()
        self.assertIn("必须是整数", showwarning.call_args.args[1])


@unittest.skipUnless(TK_AVAILABLE, f"Tk unavailable ({TK_ERROR})")
class TitleScreenGateTests(UiTestCase):
    """A running game is writable only after the operator ticks the box."""

    def test_the_checkbox_exists_and_starts_unchecked(self) -> None:
        self.assertFalse(self.app.title_screen_var.get())
        self.assertIn("标题界面", self.app.title_screen_check.cget("text"))

    def test_no_game_means_the_gate_is_open_regardless_of_the_box(self) -> None:
        with mock.patch.object(ui, "running_game_processes", return_value=()):
            self.assertTrue(self.app._title_screen_confirmed())
            self.assertFalse(self.app._refuse_running_game("写入存档"))

    def test_a_running_game_without_the_tick_is_refused(self) -> None:
        with mock.patch.object(ui, "running_game_processes",
                               return_value=("nioh3.exe",)), \
                mock.patch.object(ui.messagebox, "showwarning") as showwarning:
            self.assertFalse(self.app._title_screen_confirmed())
            self.assertTrue(self.app._refuse_running_game("写入存档"))
        message = showwarning.call_args.args[1]
        self.assertIn("标题界面", message)
        self.assertIn("勾选", message)

    def test_a_running_game_with_the_tick_is_allowed(self) -> None:
        self.app.title_screen_var.set(True)
        with mock.patch.object(ui, "running_game_processes",
                               return_value=("nioh3.exe",)):
            self.assertTrue(self.app._title_screen_confirmed())
            self.assertFalse(self.app._refuse_running_game("写入存档"))

    def test_write_save_stops_before_the_dialog_when_the_game_runs(self) -> None:
        self._select()
        self.app.selected_save = self.app.saves[0] if self.app.saves else None
        from nioh3_accessory_editor.editor import SaveDescriptor
        self.app.selected_save = SaveDescriptor(self.root / "SAVEDATA.BIN", 1, 0,
                                               len(self.plan))
        with mock.patch.object(ui, "running_game_processes",
                               return_value=("nioh3.exe",)), \
                mock.patch.object(ui.messagebox, "showwarning") as showwarning, \
                mock.patch.object(ui.messagebox, "askyesno") as askyesno, \
                mock.patch.object(self.app, "_run_worker") as runner:
            self.app.write_save()
        showwarning.assert_called_once()
        askyesno.assert_not_called()
        runner.assert_not_called()

    def test_the_tick_is_spent_after_a_successful_write(self) -> None:
        self.app.title_screen_var.set(True)
        self.app._on_worker_ok(("written", {"new_sha256": "x"}))
        self.assertFalse(self.app.title_screen_var.get())

    def test_the_tick_is_spent_after_a_restore(self) -> None:
        self.app.title_screen_var.set(True)
        with mock.patch.object(self.app, "_invalidate_loaded_save"):
            self.app._on_worker_ok(("restored", {"restored_from": "x",
                                                 "safety_backup_dir": "y"}))
        self.assertFalse(self.app.title_screen_var.get())

    def test_the_status_line_mentions_the_checkbox_when_the_game_runs(self) -> None:
        self.app._show_game_status(("nioh3.exe",))
        self.assertIn("勾选", self.app.game_status_var.get())
        self.app._show_game_status(())
        self.assertIn("可以写入存档", self.app.game_status_var.get())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
