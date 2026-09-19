"""Tkinter UI tests: widget wiring, edit collection, and write gating.

These build the real window (no main loop) so layout/attribute mistakes surface
as test failures.  They skip themselves when Tk cannot open a display.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nioh3_accessory_editor import records, ui
from nioh3_accessory_editor.affixdb import AffixDb
from nioh3_accessory_editor.editor import SaveDescriptor
from nioh3_accessory_editor.records import EFFECT_COUNT, EMPTY_EFFECT_ID
from nioh3_accessory_editor import savefile
from nioh3_accessory_editor.version import version_info
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
    """Builds one window per test with save discovery stubbed out.

    ``support.silence_dialogs`` stubs every ``messagebox`` entry point, so a test
    that reaches a dialog it did not expect cannot leave a blocking window on
    screen; tests that assert on a dialog patch it themselves (inner patch wins).
    """

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
        support.silence_dialogs(self)
        with mock.patch.object(ui.AccessoryEditorApp, "refresh_saves",
                               lambda self: None):
            self.app = ui.AccessoryEditorApp()
        # Keep the window off screen: the suite runs a few dozen windows and each
        # one flashing up is pure noise for whoever is using the machine.
        self.app.withdraw()
        self.addCleanup(self.app.destroy)
        self._temp = tempfile.TemporaryDirectory(prefix="nioh3-ui-")
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)

    def _select(self) -> None:
        self.app._populate_saves((SaveDescriptor(
            self.root / "SAVEDATA.BIN", 1234, 0, len(self.plan)),))
        self.app._populate_accessories(
            (self.plan, ui.list_accessories(self.plan), True, records.locate_layout(self.plan))
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

    def test_title_and_footer_report_the_build(self) -> None:
        info = version_info()
        self.assertIn(f"v{info.version}", self.app.title())
        footer = self.app.version_var.get()
        self.assertIn(f"commit {info.commit}", footer)
        self.assertIn(f"来源 {info.built_from}", footer)
        self.assertIn(f"加密组件 {info.crypto_exe}", footer)
        self.assertIn(info.language, footer)
        self.assertIn("构建时间", footer)

    def test_version_button_shows_the_full_banner(self) -> None:
        info = version_info()
        with mock.patch.object(ui.messagebox, "showinfo") as informed:
            self.app.show_version_info()
        informed.assert_called_once()
        title, body = informed.call_args[0]
        self.assertEqual(title, "版本信息")
        for expected in (info.commit, info.built_from, info.crypto_exe,
                         info.language, "完整 commit", "分支"):
            self.assertIn(expected, body)


@unittest.skipUnless(TK_AVAILABLE, f"Tk unavailable ({TK_ERROR})")
class LayoutTests(UiTestCase):
    """The action row must survive long status text and a short window.

    Regression: the status label shared a row with 应用修改/写入存档, so after
    读取饰品 a long message ("已读取 213 件饰品 · 另有 1243 条…") pushed the
    checkboxes and buttons out of the window.
    """

    @staticmethod
    def walk(widget):
        yield widget
        for child in widget.winfo_children():
            yield from LayoutTests.walk(child)

    def test_controls_are_in_their_own_row(self) -> None:
        controls = self.app.controls
        texts = [str(child.cget("text")) for child in controls.winfo_children()]
        self.assertIn("仅演练（不写回）", texts)
        self.assertIn("写入校验", texts)
        self.assertIn("应用修改", texts)
        self.assertIn("写入存档", texts)

    def test_status_lines_are_not_inside_the_controls_row(self) -> None:
        for label in (self.app.status_label, self.app.table_label,
                      self.app.game_status_label):
            self.assertNotIn(label, list(self.walk(self.app.controls)))
            self.assertEqual(label.pack_info()["side"], "bottom")
            self.assertIsNot(label.master, self.app.controls)

    def test_bottom_bars_are_packed_from_the_bottom_edge(self) -> None:
        """Bars packed BOTTOM keep their height when the window shrinks."""
        self.assertEqual(self.app.controls.pack_info()["side"], "bottom")
        self.assertEqual(self.app.status_label.pack_info()["side"], "bottom")
        self.assertEqual(self.app.table_label.pack_info()["side"], "bottom")
        self.assertEqual(self.app.game_status_label.pack_info()["side"], "bottom")
        # The record list is the row that gives up space instead.
        self.assertEqual(self.app.tree.pack_info()["side"], "top")

    def test_a_very_long_status_still_leaves_the_buttons_in_place(self) -> None:
        self.app._status("已读取 213 件饰品 · " + "另有 1243 条武器/防具/绘卷记录未列出 · " * 6)
        self.assertTrue(self.app.write_button.winfo_exists())
        self.assertEqual(self.app.write_button.cget("text"), "写入存档")
        # The button lives in the controls row, never in the status row.
        self.assertIs(self.app.write_button.master, self.app.controls)
        self.assertIsNot(self.app.write_button.master, self.app.status_label.master)

    def test_table_detail_uses_its_own_variable(self) -> None:
        self.app.table_var.set("记录表 0x270066 起 2000 槽（步长 0xf0）")
        self.assertIsNot(self.app.table_label, self.app.status_label)
        # The label really renders the table variable (cget returns its value).
        self.assertIn("步长 0xf0", str(self.app.table_label.cget("text")))


@unittest.skipUnless(TK_AVAILABLE, f"Tk unavailable ({TK_ERROR})")
class GameStatusTests(UiTestCase):
    """The game-process state is shown continuously, since it gates every write."""

    def test_initial_text_is_the_checking_placeholder(self) -> None:
        self.app.game_status_var.set(ui.GAME_STATUS_UNKNOWN)
        self.assertIn("检查中", self.app.game_status_var.get())

    def test_closed_game_reports_writable(self) -> None:
        self.app._show_game_status(())
        self.assertEqual(self.app.game_status_var.get(), ui.GAME_STATUS_CLOSED)
        self.assertIn("可以写入", self.app.game_status_var.get())

    def test_running_game_reports_the_process_names(self) -> None:
        self.app._show_game_status(("Nioh3.exe",))
        text = self.app.game_status_var.get()
        self.assertIn("Nioh3.exe", text)
        self.assertIn("会被拒绝", text)

    def test_status_check_reads_the_process_list_off_the_ui_thread(self) -> None:
        with mock.patch.object(ui, "running_game_processes",
                               return_value=("Nioh3.exe",)) as checker:
            tag, names = self.app._check_game_status()
        self.assertEqual(tag, "game_status")
        self.assertEqual(names, ("Nioh3.exe",))
        checker.assert_called_once()

    def test_the_check_never_overwrites_the_action_status(self) -> None:
        self.app._status("已读取 3 件饰品")
        with mock.patch.object(ui, "running_game_processes", return_value=()):
            self.app._poll_game_status()
        self.assertEqual(self.app.status_var.get(), "已读取 3 件饰品")


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
        # The (long) search location goes to the quieter second line.
        self.assertIn("查找位置", self.app.table_var.get())

    def test_populate_accessories_lists_records(self) -> None:
        self.app._populate_accessories((self.plan, ui.list_accessories(self.plan), True, records.locate_layout(self.plan)))
        self.assertEqual(self.app.tree.get_children(), ("3",))
        self.assertTrue(self.app.checksum_ok)
        self.assertIn("1 件饰品", self.app.status_var.get())

    def test_populate_accessories_flags_a_bad_checksum(self) -> None:
        self.app._populate_accessories((self.plan, ui.list_accessories(self.plan), False, records.locate_layout(self.plan)))
        self.assertFalse(self.app.checksum_ok)
        self.assertIn("校验和不一致", self.app.status_var.get())

    def test_a_save_without_records_reports_the_diagnosis(self) -> None:
        """No silent empty table: the user gets the diagnosis to send back."""
        plain = support.build_plain_save(pattern_body=False)
        with mock.patch.object(ui.messagebox, "showwarning") as warned, \
                mock.patch.object(ui.messagebox, "showinfo"):
            self.app._report_no_layout(
                (plain, True, "未在存档中找到物品记录表",
                 records.layout_diagnosis(plain))
            )
        warned.assert_called_once()
        _title, body = warned.call_args[0]
        self.assertIn("未在存档中找到物品记录表", body)
        self.assertIn("记录表定位: 失败", body)
        self.assertIn("游戏无需运行", body)
        self.assertEqual(self.app.accessory_views, [])
        self.assertEqual(self.app.tree.get_children(), ())
        self.assertIn("未能定位物品记录表", self.app.status_var.get())

    def test_reading_a_save_without_records_uses_the_failure_path(self) -> None:
        """The worker must not raise: it reports a diagnosable failure."""
        plain = support.build_plain_save(pattern_body=False)
        descriptor = SaveDescriptor(self.root / "SAVEDATA.BIN", 1234, 0,
                                    len(plain))
        self.app.crypto = mock.Mock()
        with mock.patch.object(ui, "open_save", return_value=plain), \
                mock.patch.object(ui, "save_checksum_is_valid", return_value=True):
            tag, payload = self.app._load_accessories_worker(descriptor)
        self.assertEqual(tag, "accessories_failed")
        self.assertEqual(payload[0], plain)
        self.assertTrue(payload[1])
        self.assertIn("未在存档中找到物品记录表", payload[2])
        self.assertIsNone(payload[3]["layout"])

    def test_reading_a_save_reports_the_located_table(self) -> None:
        descriptor = SaveDescriptor(self.root / "SAVEDATA.BIN", 1234, 0,
                                    len(self.plan))
        self.app.crypto = mock.Mock()
        with mock.patch.object(ui, "open_save", return_value=self.plan), \
                mock.patch.object(ui, "save_checksum_is_valid", return_value=True):
            tag, payload = self.app._load_accessories_worker(descriptor)
        self.assertEqual(tag, "accessories")
        _data, views, checksum_ok, layout = payload
        self.assertTrue(checksum_ok)
        self.assertEqual([view.slot_index for view in views], [3])
        self.assertEqual(layout.anchor, records.LEGACY_GROUP_OFFSET)
        self.app._populate_accessories(payload)
        self.assertIn("饰品", self.app.status_var.get())
        self.assertIn("记录表", self.app.table_var.get())
        # The type column keeps the raw record type and adds the catalog evidence.
        label = self.app.tree.item("3", "values")[2]
        self.assertIn(f"{0x4001:#06x}", label)
        self.assertIn("词条命中 1", label)
        self.assertEqual([view.kind_name for view in views], ["饰品"])

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
        self.app._populate_accessories((plan, ui.list_accessories(plan), True, records.locate_layout(plan)))
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
        self.app._populate_accessories((plan, ui.list_accessories(plan), True, records.locate_layout(plan)))
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

    # --------------------------------------------------------------- restore

    def _backup_entry(self):
        return savefile.BackupEntry(
            plain_path=self.root / "SAVEDATA-20260101-010101-aaaaaaaa-plain.bin",
            created_at="20260101-010101-aaaaaaaa",
            plain_size=0x9001B0, plain_sha256="", original_save_sha256="",
        )

    def _select_realistic(self) -> None:
        """Select a save whose path carries account/slot, as discovery gives it."""
        self.app._populate_saves((SaveDescriptor(
            self.root / "76561198000000001" / "SAVEDATA02" / "SAVEDATA.BIN",
            76561198000000001, 2, len(self.plan)),))
        self.app._populate_accessories(
            (self.plan, ui.list_accessories(self.plan), True, records.locate_layout(self.plan)))
        self.app.tree.selection_set("3")
        self.app._on_accessory_selected()

    def test_restore_requires_a_selected_save(self) -> None:
        with mock.patch.object(ui.messagebox, "showwarning") as warned, \
                mock.patch.object(ui, "list_backups") as listed:
            self.app.restore_save()
        warned.assert_called_once()
        listed.assert_not_called()

    def test_restore_explains_when_there_is_nothing_to_restore(self) -> None:
        self._select_realistic()
        with mock.patch.object(ui, "list_backups", return_value=()), \
                mock.patch.object(ui.messagebox, "showinfo") as informed, \
                mock.patch.object(ui.messagebox, "askyesno") as asked:
            self.app.restore_save()
        self.assertIn("没有可用备份", informed.call_args[0][0])
        self.assertIn("_nioh3_accessory_backup", informed.call_args[0][1])
        self.assertIn("account-76561198000000001", informed.call_args[0][1])
        self.assertIn("slot-02", informed.call_args[0][1])
        asked.assert_not_called()

    def test_restore_explains_an_unlocatable_save_path(self) -> None:
        """A save copied to an arbitrary folder must not crash the window."""
        self._select()  # <temp>/SAVEDATA.BIN: no account/slot in the path
        with mock.patch.object(ui.messagebox, "showinfo") as informed:
            self.app.restore_save()
        self.assertIn("无法定位备份目录", informed.call_args[0][0])

    def test_restore_cancelled_by_the_picker_writes_nothing(self) -> None:
        self._select()
        with mock.patch.object(ui, "list_backups", return_value=(self._backup_entry(),)), \
                mock.patch.object(self.app, "_choose_backup_dialog", return_value=None), \
                mock.patch.object(ui, "restore_backup") as restored:
            self.app.restore_save()
        restored.assert_not_called()

    def test_restore_asks_for_confirmation_and_passes_the_entry(self) -> None:
        self._select()
        entry = self._backup_entry()
        captured: dict[str, object] = {}

        def fake_restore(save, chosen, **kwargs):
            captured.update(kwargs)
            captured["entry"] = chosen
            return {"dry_run": False, "new_sha256": "CD" * 32,
                    "restored_from": str(chosen.plain_path),
                    "safety_backup_dir": str(self.root / "safety"),
                    "matches_original_save": True}

        with mock.patch.object(ui, "list_backups", return_value=(entry,)), \
                mock.patch.object(self.app, "_choose_backup_dialog", return_value=entry), \
                mock.patch.object(ui, "running_game_processes", return_value=()), \
                mock.patch.object(ui.messagebox, "askyesno", return_value=True) as asked, \
                mock.patch.object(ui.messagebox, "showinfo") as informed, \
                mock.patch.object(ui, "restore_backup", side_effect=fake_restore), \
                self._run_worker_synchronously():
            self.app.restore_save()

        self.assertIs(captured["entry"], entry)
        self.assertEqual(captured["allow_game_running"], True)
        self.assertEqual(captured["state_root"], self.app.state_root)
        confirmation = asked.call_args[0][1]
        self.assertIn("20260101-010101", confirmation)
        self.assertIn("标题界面", confirmation)
        self.assertIn("仅供测试学习用", confirmation)
        self.assertIn("完全一致", informed.call_args[0][1])
        self.assertIn("CD", self.app.status_var.get())

    def test_restore_aborts_when_the_user_declines(self) -> None:
        self._select()
        entry = self._backup_entry()
        with mock.patch.object(ui, "list_backups", return_value=(entry,)), \
                mock.patch.object(self.app, "_choose_backup_dialog", return_value=entry), \
                mock.patch.object(ui.messagebox, "askyesno", return_value=False), \
                mock.patch.object(ui, "restore_backup") as restored:
            self.app.restore_save()
        restored.assert_not_called()

    def test_restore_warns_about_a_running_game(self) -> None:
        self._select()
        entry = self._backup_entry()
        with mock.patch.object(ui, "list_backups", return_value=(entry,)), \
                mock.patch.object(self.app, "_choose_backup_dialog", return_value=entry), \
                mock.patch.object(ui, "running_game_processes",
                                  return_value=("Nioh3.exe",)), \
                mock.patch.object(ui.messagebox, "askyesno", return_value=False) as asked:
            self.app.restore_save()
        self.assertIn("Nioh3.exe", asked.call_args[0][1])

    def test_restore_invalidates_loaded_data(self) -> None:
        """After restoring, the in-memory copy is stale and must not be written."""
        self._select()
        self.assertIsNotNone(self.app.decrypted)
        entry = self._backup_entry()
        with mock.patch.object(ui, "list_backups", return_value=(entry,)), \
                mock.patch.object(self.app, "_choose_backup_dialog", return_value=entry), \
                mock.patch.object(ui, "running_game_processes", return_value=()), \
                mock.patch.object(ui.messagebox, "askyesno", return_value=True), \
                mock.patch.object(ui.messagebox, "showinfo"), \
                mock.patch.object(ui, "restore_backup", return_value={
                    "dry_run": False, "new_sha256": "EF" * 32,
                    "restored_from": "x", "safety_backup_dir": "y",
                    "matches_original_save": False,
                }), self._run_worker_synchronously():
            self.app.restore_save()
        self.assertIsNone(self.app.decrypted)
        self.assertEqual(self.app.tree.get_children(), ())

    def test_restore_dry_run_does_not_invalidate(self) -> None:
        self._select()
        entry = self._backup_entry()
        with mock.patch.object(ui, "list_backups", return_value=(entry,)), \
                mock.patch.object(self.app, "_choose_backup_dialog", return_value=entry), \
                mock.patch.object(ui, "running_game_processes", return_value=()), \
                mock.patch.object(ui.messagebox, "askyesno", return_value=True), \
                mock.patch.object(ui.messagebox, "showinfo"), \
                mock.patch.object(ui, "restore_backup",
                                  return_value={"dry_run": True}), \
                self._run_worker_synchronously():
            self.app.restore_save()
        self.assertIsNotNone(self.app.decrypted)


@unittest.skipUnless(TK_AVAILABLE, f"Tk unavailable ({TK_ERROR})")
class StartupScanTests(unittest.TestCase):
    """The real startup path, with save discovery *not* stubbed out.

    Every other UI test patches ``refresh_saves``, which once hid a live bug
    (``_load_saves`` was a ``@staticmethod`` that still referenced ``self``, so
    the save scan always failed with "name 'self' is not defined" behind a modal
    error box).  These tests therefore build the window exactly as a user does.
    """

    def test_window_opens_and_the_save_scan_does_not_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nioh3-start-") as temp, \
                mock.patch.object(tkinter, "Tk", tkinter.Tk), \
                mock.patch("nioh3_accessory_editor.savefile.save_root_directory",
                           return_value=Path(temp)), \
                mock.patch.object(ui.messagebox, "showerror") as failed:
            app = ui.AccessoryEditorApp()
            self.addCleanup(app.destroy)
            for _ in range(200):  # drain the worker queue
                app.update()
                if app.worker_queue.empty():
                    break
        failed.assert_not_called()
        self.assertEqual(app.saves, [])

    def test_missing_save_root_is_reported_as_a_status_not_a_dialog(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nioh3-start-") as temp, \
                mock.patch("nioh3_accessory_editor.savefile.save_root_directory",
                           return_value=Path(temp) / "absent"), \
                mock.patch.object(ui.AccessoryEditorApp, "refresh_saves",
                                  lambda self: None):
            app = ui.AccessoryEditorApp()
        self.addCleanup(app.destroy)
        with mock.patch.object(ui.messagebox, "showerror") as failed:
            app._populate_saves(())
        failed.assert_not_called()
        # The empty list is actionable: it says where the scan looked (on its own
        # line, so a long path cannot squeeze the action row).
        self.assertIn("未发现存档", app.status_var.get())
        self.assertIn("查找位置", app.table_var.get())


class StaticMethodTests(unittest.TestCase):
    """A ``@staticmethod`` that touches ``self`` is a guaranteed NameError."""

    def test_no_staticmethod_references_self(self) -> None:
        import ast
        from pathlib import Path as _Path

        package = _Path(__file__).resolve().parents[1] / "nioh3_accessory_editor"
        offenders = []
        for path in sorted(package.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef):
                    continue
                if not any("staticmethod" in ast.unparse(d)
                           for d in node.decorator_list):
                    continue
                used = {name.id for name in ast.walk(node)
                        if isinstance(name, ast.Name)}
                if "self" in used:
                    offenders.append(f"{path.name}:{node.lineno}:{node.name}")
        self.assertEqual(offenders, [], "staticmethod 里引用了 self")


if __name__ == "__main__":
    unittest.main()
