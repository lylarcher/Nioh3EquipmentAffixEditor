"""【仅演练（不写入）】的三处提醒：勾选实时提示、写入确认框、仅演练结果。

用户要求（原话）：「在写入存档之前，若要真正生效请把'仅演练（不写入）'勾选去掉。」
即：勾选状态下必须三处都能看出来——按钮区实时提示、写入确认框顶部、以及执行完的结果。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nioh3_equipment_affix_editor import records, ui
from nioh3_equipment_affix_editor.affixdb import AffixDb
from nioh3_equipment_affix_editor.editor import SaveDescriptor
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


@unittest.skipUnless(TK_AVAILABLE, f"Tk unavailable ({TK_ERROR})")
class DryRunNoticeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = AffixDb()
        cls.affix = next(entry for entry in cls.db.all() if not entry.is_fixed)
        cls.plan = support.build_plain_save(
            records_by_slot={
                3: support.build_record(
                    record_type=0x4001, level=150, rarity=4,
                    effects=((cls.affix.effect_id, 20, 0x40),),
                )
            }
        )

    def setUp(self) -> None:
        support.silence_dialogs(self)
        with mock.patch.object(ui.AccessoryEditorApp, "refresh_saves",
                               lambda self: None):
            self.app = ui.AccessoryEditorApp()
        self.app.withdraw()
        self.addCleanup(self.app.destroy)
        self._temp = tempfile.TemporaryDirectory(prefix="nioh3-dryrun-")
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)

    def _select(self) -> None:
        self.app._populate_saves((SaveDescriptor(
            self.root / "SAVEDATA.BIN", 1234, 0, len(self.plan)),))
        self.app._populate_accessories(
            (self.plan, ui.list_accessories(self.plan), True,
             records.locate_layout(self.plan))
        )

    def _run_worker_synchronously(self):
        def immediate(function, *args):
            try:
                self.app._on_worker_ok(function(*args))
            except Exception as error:  # noqa: BLE001 - mirrors the real worker
                self.app._on_worker_error(error)

        return mock.patch.object(self.app, "_run_worker", side_effect=immediate)

    # ------------------------------------------------------------ 复选框与实时提示
    def test_the_checkbox_uses_the_new_wording(self) -> None:
        texts = [str(child.cget("text")) for child in self.app.controls.winfo_children()]
        self.assertIn("仅演练（不写入）", texts)
        self.assertNotIn("仅演练（不写回）", texts)

    def test_the_hint_is_shown_while_dry_run_is_on(self) -> None:
        """默认就是勾选状态（配置默认仅演练），所以启动时就该有提示。"""
        self.assertTrue(self.app.dry_run_var.get())
        self.assertEqual(self.app.dry_run_hint_var.get(), ui.DRY_RUN_HINT)

    def test_the_hint_follows_the_checkbox_in_real_time(self) -> None:
        self.app.dry_run_var.set(False)
        self.assertEqual(self.app.dry_run_hint_var.get(), "")
        self.app.dry_run_var.set(True)
        self.assertEqual(self.app.dry_run_hint_var.get(), ui.DRY_RUN_HINT)

    def test_the_hint_label_sits_outside_the_controls_row(self) -> None:
        self.assertIsNot(self.app.dry_run_hint_label.master, self.app.controls)
        self.assertEqual(self.app.dry_run_hint_label.pack_info()["side"], "bottom")

    # ------------------------------------------------------------ 写入确认框
    def test_the_confirmation_dialog_warns_while_dry_run_is_on(self) -> None:
        self._select()
        captured: dict[str, str] = {}

        def askyesno(title, message, **kwargs):
            captured["title"] = title
            captured["message"] = message
            return False

        with mock.patch.object(ui.messagebox, "askyesno", side_effect=askyesno):
            self.app.write_save()
        self.assertIn(ui.DRY_RUN_CONFIRM_NOTE, captured["message"])
        # 提醒必须在最上面，不能埋在免责声明下面。
        self.assertTrue(captured["message"].startswith(ui.DRY_RUN_CONFIRM_NOTE))

    def test_the_confirmation_dialog_drops_the_warning_when_not_dry_run(self) -> None:
        self._select()
        self.app.dry_run_var.set(False)
        captured: dict[str, str] = {}

        def askyesno(title, message, **kwargs):
            captured["message"] = message
            return False

        with mock.patch.object(ui.messagebox, "askyesno", side_effect=askyesno):
            self.app.write_save()
        self.assertNotIn("仅演练", captured["message"])

    # ------------------------------------------------------------ 仅演练结果
    def test_the_result_says_nothing_was_written(self) -> None:
        self._select()
        with mock.patch.object(ui.messagebox, "askyesno", return_value=True), \
                mock.patch.object(ui.messagebox, "showinfo") as informed, \
                mock.patch.object(ui, "commit_save", return_value={
                    "dry_run": True, "checksum_before": "0x1", "checksum_after": "0x2",
                }) as committed, self._run_worker_synchronously():
            self.app.write_save()
        self.assertTrue(committed.call_args.kwargs["dry_run"])
        self.assertIn(ui.DRY_RUN_RESULT, informed.call_args[0][1])
        self.assertEqual(self.app.status_var.get(), ui.DRY_RUN_STATUS)

    def test_a_real_write_does_not_claim_dry_run(self) -> None:
        self._select()
        self.app.dry_run_var.set(False)
        with mock.patch.object(ui.messagebox, "askyesno", return_value=True), \
                mock.patch.object(ui.messagebox, "showinfo"), \
                mock.patch.object(ui, "commit_save", return_value={
                    "dry_run": False, "new_sha256": "AB" * 32,
                }) as committed, self._run_worker_synchronously():
            self.app.write_save()
        self.assertFalse(committed.call_args.kwargs["dry_run"])
        self.assertNotIn("未写入", self.app.status_var.get())

    # ------------------------------------------------------------ 旧文案不再出现
    def test_no_source_mentions_the_old_wording(self) -> None:
        package = Path(ui.__file__).resolve().parent
        offenders: list[str] = []
        for path in sorted(package.rglob("*.py")):
            if "不写回" in path.read_text(encoding="utf-8"):
                offenders.append(path.name)
        self.assertEqual(offenders, [])


if __name__ == "__main__":  # pragma: no cover - manual runs only
    unittest.main()
