"""Save-file tests: discovery, backends, durability, backup, verified write."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nioh3_equipment_affix_editor import savefile
from nioh3_equipment_affix_editor.checksum import patch_user_checksum
from nioh3_equipment_affix_editor.savefile import (
    BACKUP_MANIFEST_SCHEMA,
    BACKUP_SUBDIRECTORY_NAME,
    GAME_PROCESS_NAMES,
    SAVE_SCHEMA_PROFILE,
    GameRunningError,
    SaveCrypto,
    SaveError,
    account_id_from_save_path,
    backup_directory_for,
    capture_quiescent_save_fingerprints,
    capture_related_save_fingerprints,
    create_backup,
    decrypt_save_to_bytes,
    default_crypto_tool,
    discover_save_paths,
    require_game_not_running,
    restore_save_from_bytes,
    running_game_processes,
    save_slot_index_from_path,
    sha256_file,
    write_encrypted_save,
)
from tests import support


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="nioh3-tests-")
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)


class DiscoveryTests(TempDirTestCase):
    def test_discover_without_a_save_root(self) -> None:
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "nope")}):
            self.assertEqual(savefile.save_root_directory(),
                             self.root / "nope" / "KoeiTecmo" / "NIOH3" / "Savedata")
            self.assertEqual(discover_save_paths(), [])

    def test_discover_finds_and_orders_slots(self) -> None:
        save_root = self.root / "KoeiTecmo" / "NIOH3" / "Savedata"
        paths = support.make_fake_save_tree(save_root, account=12345, slots=3)
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}):
            found = discover_save_paths()
        self.assertEqual(found, sorted(paths, key=lambda p: p.parent.name))

    def test_discover_ignores_unrelated_directories(self) -> None:
        save_root = self.root / "KoeiTecmo" / "NIOH3" / "Savedata"
        support.make_fake_save_tree(save_root, account=999, slots=1)
        stray = save_root / "999" / "SAVEDATAXX"
        stray.mkdir(parents=True)
        (stray / "SAVEDATA.BIN").write_bytes(b"x")
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}):
            found = discover_save_paths()
        self.assertEqual(len(found), 1)

    def test_slot_index_from_path(self) -> None:
        path = Path("C:/x/12345/SAVEDATA07/SAVEDATA.BIN")
        self.assertEqual(save_slot_index_from_path(path), 7)

    def test_slot_index_errors(self) -> None:
        with self.assertRaises(SaveError):
            save_slot_index_from_path(Path("C:/x/12345/nope/SAVEDATA.BIN"))

    def test_account_id_from_path(self) -> None:
        path = Path("C:/x/76561198000000000/SAVEDATA00/SAVEDATA.BIN")
        self.assertEqual(account_id_from_save_path(path), 76561198000000000)

    def test_account_id_errors(self) -> None:
        with self.assertRaises(SaveError):
            account_id_from_save_path(Path("C:/only-one/SAVEDATA.BIN"))


class BackendTests(TempDirTestCase):
    def test_default_tool_is_the_packaged_binary(self) -> None:
        tool = default_crypto_tool()
        self.assertTrue(tool.is_file())
        self.assertEqual(tool.name, "Nioh_Savefile_decrypt.exe")
        self.assertEqual(tool, default_crypto_tool(self.root))

    def test_default_tool_error_when_nothing_exists(self) -> None:
        with mock.patch.object(savefile.Path, "is_file", return_value=False):
            with self.assertRaises(FileNotFoundError):
                default_crypto_tool(self.root)

    def test_prefer_python_ignores_an_executable(self) -> None:
        crypto = SaveCrypto(support.EXE_PATH, prefer_python=True)
        self.assertIsNone(crypto.executable)
        self.assertEqual(crypto.backend_name, "python")

    def test_backend_name_reports_the_exe(self) -> None:
        self.assertEqual(SaveCrypto(support.EXE_PATH).backend_name,
                         f"exe:{support.EXE_PATH}")


class TransformGuardTests(TempDirTestCase):
    def test_missing_input(self) -> None:
        crypto = SaveCrypto(prefer_python=True)
        with self.assertRaises(SaveError):
            crypto.transform(self.root / "missing.bin", self.root / "out.bin")

    def test_refuses_in_place_transform(self) -> None:
        source = self.root / "a.bin"
        source.write_bytes(b"x")
        crypto = SaveCrypto(prefer_python=True)
        with self.assertRaises(SaveError):
            crypto.transform(source, source)

    def test_refuses_to_overwrite_output(self) -> None:
        source = self.root / "a.bin"
        output = self.root / "b.bin"
        source.write_bytes(b"x")
        output.write_bytes(b"y")
        crypto = SaveCrypto(prefer_python=True)
        with self.assertRaises(SaveError):
            crypto.transform(source, output)


class FingerprintTests(TempDirTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.plain = support.build_plain_save()

    def _make_save(self) -> Path:
        path = self.root / "12345" / "SAVEDATA00" / "SAVEDATA.BIN"
        path.parent.mkdir(parents=True)
        path.write_bytes(self.plain)
        (path.parent / "BACKUP.BIN").write_bytes(b"game-backup")
        system = path.parent.parent / "SYSTEMSAVEDATA00"
        system.mkdir()
        (system / "SAVEDATA.BIN").write_bytes(b"system")
        return path

    def test_related_roles_are_captured(self) -> None:
        path = self._make_save()
        fingerprints = capture_related_save_fingerprints(path)
        self.assertEqual([f.role for f in fingerprints],
                         ["main_save", "game_backup", "system_save"])
        for fingerprint in fingerprints:
            self.assertTrue(fingerprint.exists)
            self.assertEqual(len(fingerprint.sha256), 64)
            self.assertEqual(
                fingerprint.sha256, sha256_file(fingerprint.path)
            )

    def test_missing_related_files_are_recorded_as_absent(self) -> None:
        path = self._make_save()
        (path.parent / "BACKUP.BIN").unlink()
        roles = {f.role: f for f in capture_related_save_fingerprints(path)}
        self.assertFalse(roles["game_backup"].exists)
        self.assertEqual(roles["game_backup"].sha256, "")

    def test_quiescent_reads_succeed_when_nothing_changes(self) -> None:
        path = self._make_save()
        fingerprints = capture_quiescent_save_fingerprints(path, interval_seconds=0.01)
        self.assertEqual(fingerprints, capture_related_save_fingerprints(path))

    def test_quiescence_detects_a_change(self) -> None:
        path = self._make_save()
        real_sleep = savefile.time.sleep
        calls = {"n": 0}

        def fake_sleep(seconds: float) -> None:
            calls["n"] += 1
            path.write_bytes(self.plain[:1000] + b"changed")
            real_sleep(0)

        with mock.patch.object(savefile.time, "sleep", side_effect=fake_sleep):
            with self.assertRaises(SaveError) as caught:
                capture_quiescent_save_fingerprints(path)
        self.assertIn("SAVE_SYNC_ACTIVE", str(caught.exception))
        self.assertEqual(calls["n"], 1)


class GameProcessTests(unittest.TestCase):
    def test_running_game_processes_returns_a_tuple(self) -> None:
        result = running_game_processes()
        self.assertIsInstance(result, tuple)

    def test_allow_running_short_circuits(self) -> None:
        require_game_not_running(allow_running=True)

    def test_blocks_when_the_game_is_detected(self) -> None:
        with mock.patch.object(savefile, "_windows_process_names",
                               return_value={"Nioh3.exe", "explorer.exe"}):
            self.assertEqual(running_game_processes(), ("Nioh3.exe",))
            with self.assertRaises(GameRunningError):
                require_game_not_running()

    def test_unknown_process_list_does_not_block(self) -> None:
        with mock.patch.object(savefile, "_windows_process_names", return_value=None):
            require_game_not_running()
            self.assertEqual(running_game_processes(), ())

    def test_known_names_are_case_insensitive(self) -> None:
        with mock.patch.object(savefile, "_windows_process_names",
                               return_value={"NIOH3.EXE"}):
            self.assertEqual(running_game_processes(), ("Nioh3.exe",))
        self.assertIn("Nioh3.exe", GAME_PROCESS_NAMES)

    def test_unrelated_processes_do_not_block(self) -> None:
        with mock.patch.object(savefile, "_windows_process_names",
                               return_value={"Nioh2.exe", "steam.exe"}):
            require_game_not_running()
            self.assertEqual(running_game_processes(), ())

    def test_requirement_names_the_safe_states(self) -> None:
        """The notice must tell the user *when* writing is allowed."""
        text = savefile.SAVE_WRITE_REQUIREMENT
        self.assertIn("退出游戏", text)
        self.assertIn("标题界面", text)
        self.assertIn("覆盖", text)

    def test_refusal_message_carries_the_requirement(self) -> None:
        with mock.patch.object(savefile, "_windows_process_names",
                               return_value={"Nioh3.exe"}):
            with self.assertRaises(GameRunningError) as caught:
                require_game_not_running()
        message = str(caught.exception)
        self.assertIn("Nioh3.exe", message)
        self.assertIn("标题界面", message)
        self.assertIn("--force-while-running", message)

    def test_requirement_is_exported(self) -> None:
        self.assertIn("SAVE_WRITE_REQUIREMENT", savefile.__all__)


class DurableWriteTests(TempDirTestCase):
    def test_durable_copy_creates_and_replaces(self) -> None:
        source = self.root / "src.bin"
        target = self.root / "sub" / "dst.bin"
        source.write_bytes(b"first")
        savefile._copy_file_durable(source, target)
        self.assertEqual(target.read_bytes(), b"first")
        source.write_bytes(b"second")
        savefile._copy_file_durable(source, target)
        self.assertEqual(target.read_bytes(), b"second")

    def test_durable_copy_leaves_no_temp_files(self) -> None:
        source = self.root / "src.bin"
        target = self.root / "dst.bin"
        source.write_bytes(b"data")
        savefile._copy_file_durable(source, target)
        leftovers = [p.name for p in self.root.iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_durable_json_write(self) -> None:
        target = self.root / "manifest.json"
        savefile._write_json_durable(target, {"a": 1, "中文": "值"})
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")),
                         {"a": 1, "中文": "值"})

    def test_restore_save_from_bytes(self) -> None:
        target = self.root / "12345" / "SAVEDATA00" / "SAVEDATA.BIN"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"current")
        restore_save_from_bytes(target, b"original")
        self.assertEqual(target.read_bytes(), b"original")


@unittest.skipUnless(support.HAVE_EXE, "reference crypto executable is missing")
class ExeBackendIntegrationTests(TempDirTestCase):
    """Full-size round trips through the reference executable."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.plain = support.build_plain_save()

    def setUp(self) -> None:
        super().setUp()
        self.crypto = SaveCrypto(support.EXE_PATH)

    def test_round_trip_is_lossless_in_the_crypto_region(self) -> None:
        plain_path = self.root / "plain.bin"
        enc_path = self.root / "enc.bin"
        dec_path = self.root / "dec.bin"
        plain_path.write_bytes(self.plain)
        self.crypto.encrypt(plain_path, enc_path)
        self.assertTrue(SaveCrypto.is_encrypted(enc_path.read_bytes()))
        self.crypto.decrypt(enc_path, dec_path)
        self.assertEqual(support.covered(dec_path.read_bytes()),
                         support.covered(self.plain))

    def test_decrypt_save_to_bytes_validates_the_magic(self) -> None:
        plain_path = self.root / "plain.bin"
        enc_path = self.root / "enc.bin"
        plain_path.write_bytes(self.plain)
        self.crypto.encrypt(plain_path, enc_path)
        self.assertEqual(
            support.covered(decrypt_save_to_bytes(enc_path, self.crypto)),
            support.covered(self.plain),
        )

    def test_decrypt_save_to_bytes_rejects_garbage(self) -> None:
        garbage = self.root / "12345" / "SAVEDATA00" / "SAVEDATA.BIN"
        garbage.parent.mkdir(parents=True)
        garbage.write_bytes(os.urandom(len(self.plain)))
        with self.assertRaises(SaveError):
            decrypt_save_to_bytes(garbage, self.crypto)

    def test_exe_output_size_is_validated(self) -> None:
        short = self.root / "short.bin"
        short.write_bytes(b"too small")
        with self.assertRaises(SaveError):
            self.crypto.transform(short, self.root / "out.bin")


@unittest.skipUnless(support.HAVE_EXE, "reference crypto executable is missing")
class BackupAndWriteTests(TempDirTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.plain = support.build_plain_save()

    def _make_save(self, data: bytes | None = None) -> tuple[Path, SaveCrypto]:
        crypto = SaveCrypto(support.EXE_PATH)
        plain = self.plain if data is None else data
        directory = self.root / "76561198000000001" / "SAVEDATA02"
        directory.mkdir(parents=True)
        plain_path = self.root / "staged-plain.bin"
        enc_path = self.root / "staged-enc.bin"
        plain_path.write_bytes(plain)
        crypto.encrypt(plain_path, enc_path)
        target = directory / "SAVEDATA.BIN"
        target.write_bytes(enc_path.read_bytes())
        (directory / "BACKUP.BIN").write_bytes(b"game-backup")
        return target, crypto

    def test_backup_contains_a_decrypted_copy_and_manifest(self) -> None:
        target, crypto = self._make_save()
        state_root = self.root / "state"
        backup_dir = create_backup(target, state_root=state_root, crypto=crypto)
        self.assertTrue(backup_dir.is_dir())
        self.assertEqual(
            backup_dir, state_root / BACKUP_SUBDIRECTORY_NAME
            / "account-76561198000000001" / "slot-02"
        )
        manifest = json.loads((backup_dir / "backup-manifest.json").read_text("utf-8"))
        self.assertEqual(manifest["backup_manifest_schema"], BACKUP_MANIFEST_SCHEMA)
        self.assertEqual(manifest["save_schema_profile"], SAVE_SCHEMA_PROFILE)
        self.assertEqual(manifest["steam_account_id"], 76561198000000001)
        self.assertEqual(manifest["save_slot_index"], 2)
        plain_backup = backup_dir / manifest["plain_backup"]
        self.assertTrue(plain_backup.is_file())
        recovered = plain_backup.read_bytes()
        self.assertEqual(recovered[:len(self.plain) - 8], self.plain[:len(self.plain) - 8])
        self.assertEqual(manifest["plain_backup_sha256"], sha256_file(plain_backup))

    def test_two_backups_get_distinct_names(self) -> None:
        target, crypto = self._make_save()
        state_root = self.root / "state"
        first = create_backup(target, state_root=state_root, crypto=crypto)
        second = create_backup(target, state_root=state_root, crypto=crypto)
        names = {p.name for p in first.iterdir() if p.name.endswith(".bin")}
        names |= {p.name for p in second.iterdir() if p.name.endswith(".bin")}
        self.assertEqual(len(names), 2)

    def test_backup_directory_for(self) -> None:
        target = Path("C:/root/42/SAVEDATA03/SAVEDATA.BIN")
        self.assertEqual(
            backup_directory_for(target, Path("C:/state")),
            Path("C:/state") / BACKUP_SUBDIRECTORY_NAME / "account-42" / "slot-03",
        )

    def test_write_encrypted_save_persists_the_patch(self) -> None:
        target, crypto = self._make_save()
        original_tail = target.read_bytes()[-8:]
        patched = bytearray(self.plain)
        patched[0x200] ^= 0x5A
        patch_user_checksum(patched)
        expected_fingerprints = capture_quiescent_save_fingerprints(target)
        digest = write_encrypted_save(
            target, bytes(patched), crypto=crypto,
            expected_fingerprints=expected_fingerprints,
        )
        self.assertEqual(digest, sha256_file(target))
        on_disk = decrypt_save_to_bytes(target, crypto)
        self.assertEqual(support.covered(on_disk), support.covered(bytes(patched)))
        # The uncovered tail must survive byte-for-byte from the original file.
        self.assertEqual(target.read_bytes()[-8:], original_tail)

    def test_write_can_force_the_uncovered_tail(self) -> None:
        """A restore writes the tail explicitly; the crypto step zeroes it."""
        target, crypto = self._make_save()
        wanted = bytes.fromhex("1122334455667788")
        expected = capture_quiescent_save_fingerprints(target)
        write_encrypted_save(target, self.plain, crypto=crypto,
                             expected_fingerprints=expected, tail=wanted)
        self.assertEqual(target.read_bytes()[-8:], wanted)

    def test_write_rejects_a_tail_of_the_wrong_length(self) -> None:
        target, crypto = self._make_save()
        expected = capture_related_save_fingerprints(target)
        with self.assertRaises(SaveError) as caught:
            write_encrypted_save(target, self.plain, crypto=crypto,
                                 expected_fingerprints=expected, tail=b"\x00")
        self.assertIn("尾字节长度", str(caught.exception))

    def test_reference_tool_zeroes_the_uncovered_tail(self) -> None:
        """Pins the measured behaviour the writer compensates for.

        If a future tool preserves the tail instead, this test fails and the
        comments in ``savefile.write_encrypted_save`` / ``editor.restore_backup``
        (and the restore's tail policy) need revisiting.
        """
        crypto = SaveCrypto(support.EXE_PATH)
        source = self.root / "tail-source.bin"
        data = bytearray(self.plain)
        data[-8:] = b"\x11\x22\x33\x44\x55\x66\x77\x88"
        source.write_bytes(bytes(data))
        encrypted = self.root / "tail-encrypted.bin"
        crypto.encrypt(source, encrypted)
        self.assertEqual(encrypted.read_bytes()[-8:], bytes(8),
                         "参考工具已改为保留尾字节——请重新评估恢复时的尾字节策略")
        decrypted = self.root / "tail-decrypted.bin"
        crypto.decrypt(encrypted, decrypted)
        self.assertEqual(decrypted.read_bytes()[-8:], bytes(8))

    def test_write_rejects_a_stale_fingerprint(self) -> None:
        target, crypto = self._make_save()
        expected = capture_quiescent_save_fingerprints(target)
        target.write_bytes(target.read_bytes())  # touch: size/mtime may change
        with mock.patch.object(savefile, "capture_quiescent_save_fingerprints",
                               return_value=expected + (
                                   savefile.SaveFileFingerprint(
                                       "main_save", target, True, 1, "X"),)):
            with self.assertRaises(SaveError) as caught:
                write_encrypted_save(
                    target, self.plain, crypto=crypto,
                    expected_fingerprints=expected,
                )
        self.assertIn("SAVE_SYNC_ACTIVE", str(caught.exception))

    def test_write_rejects_invalid_input(self) -> None:
        target, crypto = self._make_save()
        expected = capture_related_save_fingerprints(target)
        with self.assertRaises(SaveError):
            write_encrypted_save(target, b"not a save", crypto=crypto,
                                 expected_fingerprints=expected)
        with self.assertRaises(SaveError):
            write_encrypted_save(target, bytes(16), crypto=crypto,
                                 expected_fingerprints=expected)

    def test_failed_post_write_verification_rolls_back(self) -> None:
        target, crypto = self._make_save()
        original = target.read_bytes()
        patched = bytearray(self.plain)
        patched[0x300] ^= 0xFF
        patch_user_checksum(patched)
        expected = capture_quiescent_save_fingerprints(target)

        real_verify = savefile._verify_encrypted_file
        calls = {"n": 0}

        def flaky_verify(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] >= 2:  # let the staging check pass, fail the final one
                raise SaveError("写入校验失败：模拟失败")
            return real_verify(*args, **kwargs)

        with mock.patch.object(savefile, "_verify_encrypted_file",
                               side_effect=flaky_verify):
            with self.assertRaises(SaveError):
                write_encrypted_save(
                    target, bytes(patched), crypto=crypto,
                    expected_fingerprints=expected,
                )
        self.assertGreaterEqual(calls["n"], 2)
        self.assertEqual(target.read_bytes(), original)

    def test_write_can_skip_verification(self) -> None:
        target, crypto = self._make_save()
        patched = bytearray(self.plain)
        patched[0x400] ^= 0x11
        patch_user_checksum(patched)
        expected = capture_quiescent_save_fingerprints(target)
        with mock.patch.object(savefile, "_verify_encrypted_file",
                               side_effect=AssertionError("must not verify")):
            write_encrypted_save(
                target, bytes(patched), crypto=crypto,
                expected_fingerprints=expected, verify=False,
            )
        self.assertEqual(
            support.covered(decrypt_save_to_bytes(target, crypto)),
            support.covered(bytes(patched)),
        )


if __name__ == "__main__":
    unittest.main()
