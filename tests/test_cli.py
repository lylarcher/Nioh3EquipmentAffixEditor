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
from nioh3_accessory_editor.affixdb import AffixDb
from nioh3_accessory_editor.editor import EditorError, SaveDescriptor
from nioh3_accessory_editor.records import EMPTY_EFFECT_ID
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
        for command in ("list", "check", "edit", "backup"):
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
        args = cli.build_parser().parse_args(["list"])
        with mock.patch.object(cli, "default_crypto_tool",
                               side_effect=FileNotFoundError("no exe")):
            crypto = cli._crypto(args)
        self.assertIsNone(crypto.executable)
        self.assertEqual(crypto.backend_name, "python")


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
