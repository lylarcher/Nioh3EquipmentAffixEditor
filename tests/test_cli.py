"""CLI tests: spec parsing, argument wiring, exit codes, error handling."""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nioh3_accessory_editor import cli
from nioh3_accessory_editor import savefile as savefile_module
from nioh3_accessory_editor.affixdb import AffixDb
from nioh3_accessory_editor.editor import EditorError, SaveDescriptor
from nioh3_accessory_editor.records import EMPTY_EFFECT_ID
from nioh3_accessory_editor.savefile import (
    SAVE_WRITE_REQUIREMENT,
    backup_directory_for,
)
from tests import support


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    """Run the CLI capturing stdout/stderr without touching real saves."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class ParseEditSpecTests(unittest.TestCase):
    def test_minimal_spec(self) -> None:
        self.assertEqual(cli._parse_edit_spec("0:0x0b32"),
                         {"slot_index": 0, "effect_id": 0x0B32})

    def test_full_spec(self) -> None:
        self.assertEqual(
            cli._parse_edit_spec("6:0x0b32:20:0x40"),
            {"slot_index": 6, "effect_id": 0x0B32, "value": 20, "metadata": 0x40},
        )

    def test_decimal_values_are_accepted(self) -> None:
        self.assertEqual(cli._parse_edit_spec("0:123")["effect_id"], 123)
        self.assertEqual(cli._parse_edit_spec("0:-1")["effect_id"], -1)

    def test_too_few_parts(self) -> None:
        for spec in ("0", "", "abc"):
            with self.assertRaises(EditorError):
                cli._parse_edit_spec(spec)

    def test_too_many_parts(self) -> None:
        with self.assertRaises(EditorError):
            cli._parse_edit_spec("0:1:2:3:4")

    def test_non_numeric(self) -> None:
        for spec in ("a:0x1", "0:zz", "0:0x1:zz"):
            with self.assertRaises(EditorError):
                cli._parse_edit_spec(spec)


class ParserTests(unittest.TestCase):
    def test_requires_a_subcommand(self) -> None:
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args([])

    def test_help_exits_zero(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stdout(io.StringIO()):
                cli.main(["--help"])
        self.assertEqual(caught.exception.code, 0)

    def test_subcommand_help(self) -> None:
        for command in ("list", "check", "edit", "backup", "restore"):
            with self.assertRaises(SystemExit) as caught:
                with contextlib.redirect_stdout(io.StringIO()):
                    cli.main([command, "--help"])
            self.assertEqual(caught.exception.code, 0)

    def test_global_options_are_parsed(self) -> None:
        args = cli.build_parser().parse_args(
            ["--python-crypto", "--save-index", "2", "--account", "42", "list"]
        )
        self.assertTrue(args.python_crypto)
        self.assertEqual(args.save_index, 2)
        self.assertEqual(args.account, 42)
        self.assertEqual(args.command, "list")

    def test_edit_requires_an_edit_argument(self) -> None:
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["edit", "--record", "0"])

    def test_edit_defaults(self) -> None:
        args = cli.build_parser().parse_args(["edit", "--edit", "0:1"])
        self.assertEqual(args.record, -1)
        self.assertFalse(args.dry_run)
        self.assertFalse(args.no_verify)
        self.assertFalse(args.force_while_running)

    def test_restore_defaults_to_listing_only(self) -> None:
        """``restore`` with no --from must never write anything."""
        args = cli.build_parser().parse_args(["restore"])
        self.assertIsNone(args.from_)
        self.assertFalse(args.list)
        self.assertFalse(args.dry_run)
        self.assertFalse(args.force_while_running)


class RestoreCommandTests(unittest.TestCase):
    """``restore`` end to end, with backup discovery on a synthetic save."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="nioh3-cli-restore-")
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.save_path = (self.root / "saves" / "76561198000000009"
                          / "SAVEDATA01" / "SAVEDATA.BIN")
        self.save_path.parent.mkdir(parents=True)
        self.save_path.write_bytes(b"RNIOH3" * 4)
        self.state = self.root / "state"
        self.directory = backup_directory_for(self.save_path, self.state)
        self.directory.mkdir(parents=True)
        self.plain = support.build_plain_save()
        for stamp in ("20260101-010101-aaaaaaaa", "20260202-020202-bbbbbbbb"):
            (self.directory / f"SAVEDATA-{stamp}-plain.bin").write_bytes(self.plain)

    def _run(self, argv: list[str]) -> tuple[int, str]:
        buffer = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(buffer), \
                mock.patch.object(cli, "_state_root", return_value=self.state), \
                mock.patch.object(cli, "_crypto", return_value=mock.Mock()), \
                mock.patch.object(cli, "_select_save",
                                  return_value=SaveDescriptor(self.save_path, 9, 1, 8)):
            try:
                code = cli.main(argv)
            except SystemExit as exit_code:  # pragma: no cover - argparse only
                code = int(exit_code.code or 0)
        return code, buffer.getvalue()

    def test_listing_shows_both_backups_and_the_directory(self) -> None:
        code, output = self._run(["restore", "--list"])
        self.assertEqual(code, 0)
        self.assertIn("共 2 个备份", output)
        # Newest first, with a readable time *and* the exact file name (needed to
        # pass --from with a path).
        newest = output.index("2026-02-02 02:02:02")
        older = output.index("2026-01-01 01:01:01")
        self.assertLess(newest, older)
        self.assertIn("SAVEDATA-20260202-020202-bbbbbbbb-plain.bin", output)
        self.assertIn(str(self.directory), output)

    def test_bare_restore_lists_without_writing(self) -> None:
        before = self.save_path.read_bytes()
        code, output = self._run(["restore"])
        self.assertEqual(code, 0)
        self.assertIn("未指定 --from", output)
        self.assertEqual(self.save_path.read_bytes(), before)

    def test_from_index_restores_and_reports_the_backup(self) -> None:
        result = {"dry_run": False, "new_sha256": "ab" * 32,
                  "safety_backup_dir": str(self.directory),
                  "matches_original_save": True}
        with mock.patch.object(cli, "restore_backup",
                               return_value=result) as restore, \
                mock.patch.object(cli, "running_game_processes", return_value=()):
            code, output = self._run(["restore", "--from", "1"])
        self.assertEqual(code, 0)
        entry = restore.call_args.args[1]
        self.assertIn("20260101-010101", entry.plain_path.name)
        self.assertIn("还原后的文件与备份时记录的原文件 SHA-256 完全一致", output)
        # A real restore states the usage notice and the write requirement.
        self.assertIn("仅供测试学习用", output)
        self.assertIn("标题界面", output)
        self.assertIn("还原前的存档已另行备份到", output)

    def test_latest_picks_the_newest_backup(self) -> None:
        with mock.patch.object(cli, "restore_backup",
                               return_value={"dry_run": True}) as restore, \
                mock.patch.object(cli, "running_game_processes", return_value=()):
            self._run(["restore", "--from", "latest", "--dry-run"])
        self.assertIn("20260202-020202", restore.call_args.args[1].plain_path.name)

    def test_out_of_range_index_fails_loudly(self) -> None:
        code, _ = self._run(["restore", "--from", "7"])
        self.assertEqual(code, 1)

    def test_missing_path_fails_loudly(self) -> None:
        code, _ = self._run(["restore", "--from", str(self.root / "nope.bin")])
        self.assertEqual(code, 1)

    def test_explicit_path_outside_the_backup_folder_is_accepted(self) -> None:
        loose = self.root / "loose-plain.bin"
        loose.write_bytes(self.plain)
        with mock.patch.object(cli, "restore_backup",
                               return_value={"dry_run": True}) as restore, \
                mock.patch.object(cli, "running_game_processes", return_value=()):
            code, _ = self._run(["restore", "--from", str(loose), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(restore.call_args.args[1].plain_path, loose)

    def test_no_backups_at_all_reports_where_to_look(self) -> None:
        for path in self.directory.glob("*.bin"):
            path.unlink()
        code, output = self._run(["restore", "--list"])
        self.assertEqual(code, 0)
        self.assertIn("未发现备份", output)
        self.assertIn("backup", output)


class CryptoBackendTests(unittest.TestCase):
    def test_python_backend_selected(self) -> None:
        args = cli.build_parser().parse_args(["--python-crypto", "list"])
        self.assertIsNone(cli._crypto(args).executable)

    def test_exe_backend_selected_by_default(self) -> None:
        args = cli.build_parser().parse_args(["list"])
        crypto = cli._crypto(args)
        if support.HAVE_EXE:
            self.assertIsNotNone(crypto.executable)
        else:  # pragma: no cover - depends on the checkout
            self.assertIsNone(crypto.executable)

    def test_missing_exe_degrades_with_a_hint(self) -> None:
        """No helper anywhere -> pure Python, and the user is told why."""
        absent = Path("C:/nowhere/Nioh_Savefile_decrypt.exe")
        args = cli.build_parser().parse_args(
            ["--config", str(self.config_path()), "list"])
        with mock.patch.object(savefile_module.paths, "default_crypto_exe",
                               lambda: absent):
            crypto = cli._crypto(args)
        self.assertIsNone(crypto.executable)
        self.assertEqual(crypto.backend_name, "python")

    def test_configured_helper_missing_falls_back_to_the_bundled_one(self) -> None:
        args = cli.build_parser().parse_args(["--config", str(self.config_path()),
                                             "list"])
        crypto = cli._crypto(args)
        if support.HAVE_EXE:
            self.assertEqual(crypto.executable,
                             savefile_module.paths.default_crypto_exe())
        else:  # pragma: no cover - depends on the checkout
            self.assertIsNone(crypto.executable)

    def test_config_can_request_the_pure_python_backend(self) -> None:
        target = self.config_path({"crypto_exe": None})
        args = cli.build_parser().parse_args(["--config", str(target), "list"])
        crypto = cli._crypto(args)
        self.assertIsNone(crypto.executable)
        self.assertEqual(crypto.backend_name, "python")

    def config_path(self, overrides: dict | None = None) -> Path:
        """Write a throwaway configuration file for one test."""
        import json
        import tempfile

        from nioh3_accessory_editor.config import CONFIG_SCHEMA

        temp = tempfile.TemporaryDirectory(prefix="nioh3-cli-config-")
        self.addCleanup(temp.cleanup)
        target = Path(temp.name) / "editor.json"
        document = {"schema": CONFIG_SCHEMA, "crypto_exe": "bin/absent-helper.exe"}
        document.update(overrides or {})
        target.write_text(json.dumps(document), encoding="utf-8")
        return target


class CommandErrorTests(unittest.TestCase):
    """Commands must fail closed with rc=1 instead of raising."""

    def test_no_saves_discovered(self) -> None:
        with mock.patch.object(cli, "discover_saves", return_value=()):
            code, _out, err = run_cli(["list"])
        self.assertEqual(code, 1)
        self.assertIn("未发现", err)

    def test_account_filter_matches_nothing(self) -> None:
        saves = (SaveDescriptor(Path("C:/x/1/SAVEDATA00/SAVEDATA.BIN"), 1, 0, 10),)
        with mock.patch.object(cli, "discover_saves", return_value=saves):
            code, _out, err = run_cli(["--account", "999", "list"])
        self.assertEqual(code, 1)
        self.assertIn("未找到账号", err)

    def test_slot_filter_matches_nothing(self) -> None:
        saves = (SaveDescriptor(Path("C:/x/1/SAVEDATA00/SAVEDATA.BIN"), 1, 0, 10),)
        with mock.patch.object(cli, "discover_saves", return_value=saves):
            code, _out, err = run_cli(["--save-index", "5", "list"])
        self.assertEqual(code, 1)
        self.assertIn("未找到栏位", err)

    def test_edit_requires_a_record_index(self) -> None:
        saves = (SaveDescriptor(Path("C:/x/1/SAVEDATA00/SAVEDATA.BIN"), 1, 0, 10),)
        with mock.patch.object(cli, "discover_saves", return_value=saves), \
                mock.patch.object(cli, "open_save", return_value=b"RNNUSR"):
            code, _out, err = run_cli(["edit", "--edit", "0:1"])
        self.assertEqual(code, 1)
        self.assertIn("--record", err)

    def test_bad_edit_spec_reports_an_error(self) -> None:
        saves = (SaveDescriptor(Path("C:/x/1/SAVEDATA00/SAVEDATA.BIN"), 1, 0, 10),)
        with mock.patch.object(cli, "discover_saves", return_value=saves), \
                mock.patch.object(cli, "open_save", return_value=b"RNNUSR"):
            code, _out, err = run_cli(["edit", "--record", "0", "--edit", "oops"])
        self.assertEqual(code, 1)
        self.assertIn("错误", err)

    def test_oserror_is_reported(self) -> None:
        with mock.patch.object(cli, "discover_saves",
                               side_effect=OSError("disk on fire")):
            code, _out, err = run_cli(["list"])
        self.assertEqual(code, 1)
        self.assertIn("系统错误", err)


@unittest.skipUnless(support.HAVE_EXE, "reference crypto executable is missing")
class EndToEndCliTests(unittest.TestCase):
    """Drive real commands against a synthetic save on disk."""

    ACCOUNT = 76561198000000042

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = AffixDb()
        cls.affix = cls.db.all()[0]
        cls.plain = support.build_plain_save(
            records_by_slot={
                3: support.build_record(
                    record_type=0x4001,
                    effects=((cls.affix.effect_id, 20, 0x40),),
                )
            }
        )

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="nioh3-cli-")
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        save_root = self.root / "KoeiTecmo" / "NIOH3" / "Savedata"
        account = save_root / str(self.ACCOUNT) / "SAVEDATA00"
        account.mkdir(parents=True)
        (account / "SYSTEMSAVEDATA00").parent.mkdir(exist_ok=True)

        from nioh3_accessory_editor.savefile import SaveCrypto

        self.crypto = SaveCrypto(support.EXE_PATH)
        staged_plain = self.root / "staged.bin"
        staged_enc = self.root / "staged.enc"
        staged_plain.write_bytes(self.plain)
        self.crypto.encrypt(staged_plain, staged_enc)
        self.save_path = account / "SAVEDATA.BIN"
        self.save_path.write_bytes(staged_enc.read_bytes())

        # CLI commands use the current directory as the backup state root.
        self._previous_cwd = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self._previous_cwd)
        patcher = mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def descriptor(self) -> SaveDescriptor:
        return SaveDescriptor(self.save_path, self.ACCOUNT, 0,
                              self.save_path.stat().st_size)

    def _reload(self):
        from nioh3_accessory_editor.editor import list_accessories

        return list_accessories(cli.open_save(self.descriptor(), self.crypto))

    def test_discovery_finds_the_fake_tree(self) -> None:
        code, out, err = run_cli(["check"])
        self.assertEqual(code, 0, err)
        self.assertIn('"account_id": %d' % self.ACCOUNT, out)
        self.assertIn('"slot_index": 0', out)

    def test_list_prints_records_and_affixes(self) -> None:
        code, out, err = run_cli(["list"])
        self.assertEqual(code, 0, err)
        self.assertIn("记录 #3", out)
        self.assertIn(self.affix.name, out)
        self.assertIn("校验和一致: 是", out)

    def test_check_reports_json(self) -> None:
        code, out, err = run_cli(["check"])
        self.assertEqual(code, 0, err)
        self.assertIn('"accessory_records": 1', out)
        self.assertIn('"magic": "RNNUSR"', out)

    def test_edit_dry_run_writes_nothing(self) -> None:
        before = self.save_path.read_bytes()
        code, out, err = run_cli([
            "edit", "--record", "3", "--edit", f"1:{self.affix.effect_id:#x}:55",
            "--dry-run",
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("仅供测试学习用", out)
        self.assertIn('"dry_run": true', out)
        self.assertEqual(self.save_path.read_bytes(), before)
        self.assertFalse((self.root / "_nioh3_accessory_backup").exists())

    def test_edit_states_the_write_requirement(self) -> None:
        """Both the notice and the current gate state must be visible."""
        code, out, err = run_cli([
            "edit", "--record", "3", "--edit", f"1:{self.affix.effect_id:#x}:55",
            "--dry-run",
        ])
        self.assertEqual(code, 0, err)
        self.assertIn(SAVE_WRITE_REQUIREMENT, out)
        self.assertIn("退出游戏", out)
        self.assertIn("标题界面", out)
        self.assertIn("当前状态：未检测到游戏进程", out)
        self.assertIn("演练模式", out)

    def test_edit_warns_when_the_game_is_running(self) -> None:
        with mock.patch.object(cli, "running_game_processes",
                               return_value=("Nioh3.exe",)):
            code, out, err = run_cli([
                "edit", "--record", "3", "--edit", f"1:{self.affix.effect_id:#x}:55",
                "--dry-run",
            ])
        self.assertEqual(code, 0, err)
        self.assertIn("Nioh3.exe", out)
        self.assertIn("将被拒绝", out)

    def test_write_refuses_while_the_game_runs(self) -> None:
        """The gate inside the writer (not just the notice) must fail closed."""
        from nioh3_accessory_editor import savefile

        before = self.save_path.read_bytes()
        with mock.patch.object(savefile, "running_game_processes",
                               return_value=("Nioh3.exe",)):
            code, _out, err = run_cli([
                "edit", "--record", "3", "--edit", f"1:{self.affix.effect_id:#x}:55",
            ])
        self.assertEqual(code, 1)
        self.assertIn("正在运行", err)
        self.assertIn("标题界面", err)
        self.assertIn("--force-while-running", err)
        self.assertEqual(self.save_path.read_bytes(), before)
        self.assertFalse((self.root / "_nioh3_accessory_backup").exists())

    def test_success_message_warns_about_overwriting(self) -> None:
        code, out, err = run_cli([
            "edit", "--record", "3", "--edit", f"2:{self.affix.effect_id:#x}:66",
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("覆盖本次修改", out)

    def test_edit_writes_and_persists(self) -> None:
        code, out, err = run_cli([
            "edit", "--record", "3", "--edit", f"2:{self.affix.effect_id:#x}:66",
        ])
        self.assertEqual(code, 0, err)
        self.assertIn('"dry_run": false', out)
        view = next(v for v in self._reload() if v.slot_index == 3)
        self.assertEqual(view.effects[2].effect_id, self.affix.effect_id)
        self.assertEqual(view.effects[2].value, 66)
        self.assertTrue(list((self.root / "_nioh3_accessory_backup").rglob("backup-manifest.json")))

    def test_backup_command(self) -> None:
        code, out, err = run_cli(["backup"])
        self.assertEqual(code, 0, err)
        self.assertIn("已备份到", out)
        backups = list(self.root.rglob("backup-manifest.json"))
        self.assertEqual(len(backups), 1)

    def test_clear_slot_via_cli(self) -> None:
        code, out, err = run_cli([
            "edit", "--record", "3", "--edit", f"0:{EMPTY_EFFECT_ID:#x}",
        ])
        self.assertEqual(code, 0, err)
        view = next(v for v in self._reload() if v.slot_index == 3)
        self.assertTrue(view.effects[0].is_empty)

    def test_illegal_affix_is_refused(self) -> None:
        before = self.save_path.read_bytes()
        code, _out, err = run_cli([
            "edit", "--record", "3", "--edit", "0:0xdeadbeef",
        ])
        self.assertEqual(code, 1)
        self.assertIn("不在合法的饰品词条表中", err)
        self.assertEqual(self.save_path.read_bytes(), before)

    def test_missing_record_is_refused(self) -> None:
        before = self.save_path.read_bytes()
        code, _out, err = run_cli([
            "edit", "--record", "7", "--edit", f"0:{self.affix.effect_id:#x}",
        ])
        self.assertEqual(code, 1)
        self.assertIn("不在当前存档", err)
        self.assertEqual(self.save_path.read_bytes(), before)

    def test_save_index_filter_selects_the_only_slot(self) -> None:
        code, out, err = run_cli(["--save-index", "0", "check"])
        self.assertEqual(code, 0, err)
        self.assertIn('"slot_index": 0', out)

    def test_save_index_filter_rejects_a_missing_slot(self) -> None:
        code, _out, err = run_cli(["--save-index", "3", "check"])
        self.assertEqual(code, 1)
        self.assertIn("未找到栏位", err)


if __name__ == "__main__":
    unittest.main()
