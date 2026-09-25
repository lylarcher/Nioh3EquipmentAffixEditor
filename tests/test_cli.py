"""CLI tests: spec parsing, argument wiring, exit codes, error handling."""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nioh3_equipment_affix_editor import cli, records
from nioh3_equipment_affix_editor import savefile as savefile_module
from nioh3_equipment_affix_editor.affixdb import AffixDb
from nioh3_equipment_affix_editor.editor import EditorError, SaveDescriptor
from nioh3_equipment_affix_editor.records import EMPTY_EFFECT_ID
from nioh3_equipment_affix_editor.savefile import (
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
        for command in ("list", "scan", "check", "edit", "backup", "restore"):
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

    def test_edit_accepts_edit_or_grace_but_parses_neither_as_required(self) -> None:
        """``--edit`` and ``--grace`` are alternatives, so neither is required by
        argparse; the command itself rejects "both missing" and "both given"."""
        args = cli.build_parser().parse_args(["edit", "--record", "0"])
        self.assertIsNone(args.edit)
        self.assertIsNone(args.grace)

    def test_edit_defaults(self) -> None:
        args = cli.build_parser().parse_args(["edit", "--edit", "0:1"])
        self.assertEqual(args.record, -1)
        self.assertFalse(args.dry_run)
        self.assertFalse(args.no_verify)
        self.assertFalse(args.force_while_running)
        self.assertIsNone(args.grace)

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

        from nioh3_equipment_affix_editor.config import CONFIG_SCHEMA

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
        cls.affix = next(entry for entry in cls.db.all() if not entry.is_fixed)
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

        from nioh3_equipment_affix_editor.savefile import SaveCrypto

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
        from nioh3_equipment_affix_editor.editor import list_accessories

        return list_accessories(cli.open_save(self.descriptor(), self.crypto))

    def test_discovery_finds_the_fake_tree(self) -> None:
        code, out, err = run_cli(["check"])
        self.assertEqual(code, 0, err)

    # ------------------------------------------- 筛选 / 关键词 / 新建 (需求 3/4/5)
    def test_search_lists_every_match_without_writing(self) -> None:
        before = self.save_path.read_bytes()
        code, out, err = run_cli(["edit", "--record", "3", "--search", "火抗性"])
        self.assertEqual(code, 0, err)
        self.assertIn("匹配到 2 条", out)
        self.assertIn("数值区间", out)
        self.assertEqual(self.save_path.read_bytes(), before, "搜索不得写存档")

    def test_search_without_matches_says_so(self) -> None:
        code, out, err = run_cli(["edit", "--record", "3", "--search", "绝无此词条"])
        self.assertEqual(code, 0, err)
        self.assertIn("没有匹配结果", out)

    def test_an_ambiguous_keyword_lists_the_candidates(self) -> None:
        code, out, err = run_cli(["edit", "--record", "3", "--edit", "3:伤害",
                                  "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("匹配到", err)
        self.assertIn("请写得更具体或直接用 id", err)

    def test_a_unique_keyword_resolves_and_writes(self) -> None:
        # 「火抗性 +10」 is unique on its own (its (星) twin has a different name).
        code, out, err = run_cli(["edit", "--record", "3", "--edit", "3:火抗性 +10",
                                  "--dry-run"])
        self.assertIn("[3] 火抗性 +10", out + err, f"stdout={out!r} stderr={err!r}")

    def test_list_filters_by_kind_and_grace(self) -> None:
        code, out, err = run_cli(["list", "--kind", "0x4001"])
        self.assertEqual(code, 0, err)
        self.assertIn("筛选（种类含「0x4001」）", out)

    def test_create_refuses_when_the_kind_has_no_template(self) -> None:
        code, out, err = run_cli(["create", "--kind", "0x4001", "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("种类", err)

    def test_list_prints_records_and_affixes(self) -> None:
        code, out, err = run_cli(["list"])
        self.assertEqual(code, 0, err)
        self.assertIn("记录 #3", out)
        self.assertIn(self.affix.name, out)
        self.assertIn("校验和一致: 是", out)

    def test_list_names_the_item_kind(self) -> None:
        """种类 comes from 物品总目录; a real id is named, an unknown one is not."""
        record = support.build_record(
            record_type=0x3E3F,
            effects=((self.affix.effect_id, 20, 0x40),),
        )
        plain = support.build_plain_save(records_by_slot={3: record})
        staged = self.root / "item-plain.bin"
        staged.write_bytes(plain)
        encrypted = self.root / "item-enc.bin"
        self.crypto.encrypt(staged, encrypted)
        self.save_path.write_bytes(encrypted.read_bytes())

        code, out, err = run_cli(["list"])
        self.assertEqual(code, 0, err)
        self.assertIn("种类 0x3e3f 龙笛[武士]", out)

    def test_list_says_so_for_an_unlisted_item_id(self) -> None:
        record = support.build_record(
            record_type=0x1234,
            effects=((self.affix.effect_id, 20, 0x40),),
        )
        plain = support.build_plain_save(records_by_slot={3: record})
        staged = self.root / "item2-plain.bin"
        staged.write_bytes(plain)
        encrypted = self.root / "item2-enc.bin"
        self.crypto.encrypt(staged, encrypted)
        self.save_path.write_bytes(encrypted.read_bytes())

        code, out, err = run_cli(["list"])
        self.assertEqual(code, 0, err)
        self.assertIn("种类 0x1234（不在物品种类表内）", out)

    def test_list_reports_the_located_record_table(self) -> None:
        code, out, err = run_cli(["list"])
        self.assertEqual(code, 0, err)
        self.assertIn("记录表:", out)
        self.assertIn("400 槽", out)
        self.assertIn("对齐", out)

    def test_edit_refuses_a_fixed_slot(self) -> None:
        """同名固定词条不能被改：CLI 也必须拒绝。"""
        fixed = next(entry for entry in self.db.all() if entry.is_fixed)
        record = support.build_record(
            record_type=0x4001,
            effects=((fixed.effect_id, 20, 0x5C000040), (self.affix.effect_id, 15, 0x40)),
        )
        plain = support.build_plain_save(records_by_slot={3: record})
        staged = self.root / "fixed-plain.bin"
        staged.write_bytes(plain)
        encrypted = self.root / "fixed-enc.bin"
        self.crypto.encrypt(staged, encrypted)
        self.save_path.write_bytes(encrypted.read_bytes())

        code, out, err = run_cli([
            "edit", "--record", "3",
            "--edit", f"0:{self.affix.effect_id:#x}:{self.affix.value}",
        ])
        combined = out + err
        self.assertNotEqual(code, 0)
        self.assertIn("同名固定词条不能修改", combined)

    def test_level_edit_dry_run_reports_the_change(self) -> None:
        code, out, err = run_cli([
            "edit", "--record", "3", "--level", "180", "--dry-run",
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("等级 160 → 180", out)
        self.assertIn("不会写入存档", out)

    def test_level_edit_refuses_above_the_cap(self) -> None:
        code, out, err = run_cli([
            "edit", "--record", "3", "--level", "181", "--dry-run",
        ])
        self.assertNotEqual(code, 0)
        self.assertIn("180", out + err)

    def test_edit_requires_exactly_one_mode(self) -> None:
        code, out, err = run_cli([
            "edit", "--record", "3", "--level", "170", "--grace", "稻荷神",
            "--dry-run",
        ])
        self.assertNotEqual(code, 0)
        self.assertIn("选一", out + err)

    def test_list_names_the_trailing_grace_slot(self) -> None:
        """An accessory's last affix is a 恩宠/套装 id from 词条总目录."""
        record = support.build_record(
            record_type=0x4001,
            effects=((self.affix.effect_id, 20, 0x40), (0x004FA3, 0, 0x29014C00)),
        )
        plain = support.build_plain_save(records_by_slot={3: record})
        staged = self.root / "grace-plain.bin"
        staged.write_bytes(plain)
        encrypted = self.root / "grace-enc.bin"
        self.crypto.encrypt(staged, encrypted)
        self.save_path.write_bytes(encrypted.read_bytes())

        code, out, err = run_cli(["list"])
        self.assertEqual(code, 0, err)
        self.assertIn("稻荷神的恩宠", out)
        self.assertIn("末位槽", out)

    def test_list_keeps_the_unknown_wording_without_the_name_table(self) -> None:
        record = support.build_record(
            record_type=0x4001,
            effects=((self.affix.effect_id, 20, 0x40), (0xDEADBEEF, 0, 0)),
        )
        plain = support.build_plain_save(records_by_slot={3: record})
        staged = self.root / "anon-plain.bin"
        staged.write_bytes(plain)
        encrypted = self.root / "anon-enc.bin"
        self.crypto.encrypt(staged, encrypted)
        self.save_path.write_bytes(encrypted.read_bytes())

        code, out, err = run_cli(["list"])
        self.assertEqual(code, 0, err)
        self.assertIn("恩宠/套装词条", out)
        self.assertIn("0xdeadbeef", out)

    # ------------------------------------------------------- 恩宠 (grace) edits

    GRACE_A = 0x004FA3  # 稻荷神的恩宠
    GRACE_B = 0x0071F6  # 不动明王的恩宠
    SET_ITEM = 0x00A7A1  # 怨恨盖世（忍者套装）

    def _stage(self, last_id: int, *, byte9: int = 0x0C, name: str = "grace") -> None:
        record = support.build_record(
            record_type=0x4001,
            effects=((self.affix.effect_id, 20, 0x40),
                     (last_id, 0, 0x5C000000 | (byte9 << 8) | 0x020000)),
        )
        plain = support.build_plain_save(records_by_slot={3: record})
        staged = self.root / f"{name}-plain.bin"
        staged.write_bytes(plain)
        encrypted = self.root / f"{name}-enc.bin"
        self.crypto.encrypt(staged, encrypted)
        self.save_path.write_bytes(encrypted.read_bytes())

    def test_edit_grace_rewrites_the_last_slot_only(self) -> None:
        self._stage(self.GRACE_A)
        before = self.save_path.read_bytes()
        code, out, err = run_cli(["edit", "--record", "3", "--grace", "不动明王",
                                  "--dry-run"])
        self.assertEqual(code, 0, err + out)
        self.assertIn("稻荷神的恩宠", out)
        self.assertIn("不动明王的恩宠", out)
        self.assertIn("演练模式", out)
        self.assertEqual(self.save_path.read_bytes(), before, "演练不得写文件")

    def test_edit_grace_accepts_an_id(self) -> None:
        self._stage(self.GRACE_B)
        code, out, err = run_cli(["edit", "--record", "3", "--grace", "0x4fa3",
                                  "--dry-run"])
        self.assertEqual(code, 0, err + out)
        self.assertIn("稻荷神的恩宠", out)

    def test_edit_grace_refuses_an_item_specific_set_effect(self) -> None:
        """专属套装词条（怨恨盖世）is not a 恩宠, so it must never be replaced."""
        self._stage(self.SET_ITEM, byte9=0x4C, name="set")
        before = self.save_path.read_bytes()
        code, out, err = run_cli(["edit", "--record", "3", "--grace", "稻荷神"])
        self.assertNotEqual(code, 0)
        self.assertIn("套装", err + out)
        self.assertEqual(self.save_path.read_bytes(), before)

    def test_edit_refuses_grace_into_a_plain_affix_slot(self) -> None:
        self._stage(self.affix.effect_id)
        code, out, err = run_cli(["edit", "--record", "3", "--grace", "稻荷神"])
        self.assertNotEqual(code, 0)
        self.assertIn("恩宠只能替换恩宠", err + out)

    def test_edit_needs_exactly_one_of_edit_or_grace(self) -> None:
        code, out, err = run_cli(["edit", "--record", "3"])
        self.assertNotEqual(code, 0)
        self.assertIn("选一", err + out)
        code, out, err = run_cli(["edit", "--record", "3", "--edit", "0:1",
                                  "--grace", "稻荷神"])
        self.assertNotEqual(code, 0)
        self.assertIn("选一", err + out)

    def test_edit_grace_refuses_a_set_target(self) -> None:
        self._stage(self.GRACE_A)
        code, out, err = run_cli(["edit", "--record", "3", "--grace", "怨恨盖世"])
        self.assertNotEqual(code, 0)
        self.assertIn("没有匹配", err + out)

    def test_edit_grace_rejects_a_missing_record(self) -> None:
        self._stage(self.GRACE_A)
        code, out, err = run_cli(["edit", "--record", "7", "--grace", "稻荷神"])
        self.assertNotEqual(code, 0)
        self.assertIn("不在当前存档的饰品记录中", err + out)

    def test_scan_reports_the_diagnosis(self) -> None:
        code, out, err = run_cli(["scan"])
        self.assertEqual(code, 0, err)
        self.assertIn("记录表定位", out)
        self.assertIn("记录头命中", out)
        self.assertIn("证据", out)
        self.assertIn("游戏无需运行", out)
        self.assertIn("0x4001", out)

    def test_scan_json_is_machine_readable(self) -> None:
        import json

        code, out, err = run_cli(["scan", "--json"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["candidate_count"], 1)
        self.assertEqual(payload["layout"]["anchor"],
                         records.LEGACY_GROUP_OFFSET)
        self.assertEqual(payload["layout"]["item_count"], 1)
        self.assertEqual(payload["level_range"], [160, 160])
        self.assertTrue(payload["checksum_consistent"])

    def test_scan_preview_can_be_disabled(self) -> None:
        import json

        code, out, _err = run_cli(["scan", "--json", "--preview", "0"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["effect_preview"], [])

    def test_scan_on_a_save_without_records_explains_itself(self) -> None:
        """A different save layout must be diagnosed, not silently empty."""
        bad = support.build_plain_save(pattern_body=False)
        staged_plain = self.root / "no-records.bin"
        staged_enc = self.root / "no-records.enc"
        staged_plain.write_bytes(bad)
        self.crypto.encrypt(staged_plain, staged_enc)
        self.save_path.write_bytes(staged_enc.read_bytes())

        code, out, err = run_cli(["scan"])
        self.assertEqual(code, 0, err)
        self.assertIn("记录表定位: 失败", out)
        self.assertIn("记录头命中: 0 处", out)

        code, out, err = run_cli(["list"])
        self.assertEqual(code, 1)
        self.assertIn("未找到物品记录表", out)
        self.assertIn("scan", out)

    def test_check_reports_json(self) -> None:
        code, out, err = run_cli(["check"])
        self.assertEqual(code, 0, err)
        self.assertIn('"accessory_records": 1', out)
        self.assertIn('"magic": "RNNUSR"', out)

    def test_edit_dry_run_writes_nothing(self) -> None:
        before = self.save_path.read_bytes()
        code, out, err = run_cli([
            "edit", "--record", "3", "--edit", f"1:{self.affix.effect_id:#x}:{self.affix.value}",
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
            "edit", "--record", "3", "--edit", f"1:{self.affix.effect_id:#x}:{self.affix.value}",
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
                "edit", "--record", "3", "--edit", f"1:{self.affix.effect_id:#x}:{self.affix.value}",
                "--dry-run",
            ])
        self.assertEqual(code, 0, err)
        self.assertIn("Nioh3.exe", out)
        self.assertIn("将被拒绝", out)

    def test_write_refuses_while_the_game_runs(self) -> None:
        """The gate inside the writer (not just the notice) must fail closed."""
        from nioh3_equipment_affix_editor import savefile

        before = self.save_path.read_bytes()
        with mock.patch.object(savefile, "running_game_processes",
                               return_value=("Nioh3.exe",)):
            code, _out, err = run_cli([
                "edit", "--record", "3", "--edit", f"1:{self.affix.effect_id:#x}:{self.affix.value}",
            ])
        self.assertEqual(code, 1)
        self.assertIn("正在运行", err)
        self.assertIn("标题界面", err)
        self.assertIn("--force-while-running", err)
        self.assertEqual(self.save_path.read_bytes(), before)
        self.assertFalse((self.root / "_nioh3_accessory_backup").exists())

    def test_success_message_warns_about_overwriting(self) -> None:
        code, out, err = run_cli([
            "edit", "--record", "3", "--edit", f"2:{self.affix.effect_id:#x}:{self.affix.value}",
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("覆盖本次修改", out)

    def test_edit_writes_and_persists(self) -> None:
        code, out, err = run_cli([
            "edit", "--record", "3", "--edit", f"2:{self.affix.effect_id:#x}:{self.affix.value}",
        ])
        self.assertEqual(code, 0, err)
        self.assertIn('"dry_run": false', out)
        view = next(v for v in self._reload() if v.slot_index == 3)
        self.assertEqual(view.effects[2].effect_id, self.affix.effect_id)
        self.assertEqual(view.effects[2].value, self.affix.value)
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
