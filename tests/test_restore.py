# -*- coding: utf-8 -*-
"""Backup and restore: where backups live, and putting one back.

The property that matters is stated as a test: a backup is *plaintext*, so
restoring re-encrypts it -- and if that round trip is faithful, the restored file
must be byte-identical to the file that was backed up.  ``create_backup`` records
the original's SHA-256 in its manifest, so the test can check exactly that
instead of trusting the implementation.

The crypto step needs the reference executable (pure Python would take minutes
for a 9.4 MB save), so these tests skip when ``bin/Nioh_Savefile_decrypt.exe`` is
absent.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nioh3_accessory_editor import editor, savefile
from nioh3_accessory_editor.crypto import USER_SAVE_SIZE
from nioh3_accessory_editor.editor import EditorError, SaveDescriptor
from nioh3_accessory_editor.savefile import (
    BACKUP_SUBDIRECTORY_NAME,
    SaveError,
    backup_directory_for,
    create_backup,
    list_backups,
    read_backup_plaintext,
)
from tests import support

ACCOUNT = 76561198000000000
SLOT = 3


class BackupLayoutTests(unittest.TestCase):
    """Pure path/parsing tests: no crypto needed."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="nioh3-backup-")
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.save_path = self.root / str(ACCOUNT) / f"SAVEDATA{SLOT:02d}" / "SAVEDATA.BIN"
        self.save_path.parent.mkdir(parents=True)
        self.save_path.write_bytes(b"x")
        self.state_root = self.root / "state"

    def test_backups_live_under_the_state_root_by_account_and_slot(self) -> None:
        directory = backup_directory_for(self.save_path, self.state_root)
        self.assertEqual(directory.name, f"slot-{SLOT:02d}")
        self.assertEqual(directory.parent.name, f"account-{ACCOUNT}")
        self.assertEqual(directory.parent.parent.name, BACKUP_SUBDIRECTORY_NAME)
        self.assertEqual(directory.parent.parent.parent, self.state_root)

    def test_listing_is_empty_before_the_first_backup(self) -> None:
        self.assertEqual(list_backups(self.save_path, self.state_root), ())

    def test_plain_backup_without_manifest_is_still_listed(self) -> None:
        directory = backup_directory_for(self.save_path, self.state_root)
        directory.mkdir(parents=True)
        (directory / "SAVEDATA-20260101-010203-deadbeef-plain.bin").write_bytes(b"y")
        entries = list_backups(self.save_path, self.state_root)
        self.assertEqual(len(entries), 1)
        # Nothing recorded, so nothing to contradict: usable, but unverified.
        self.assertTrue(entries[0].integrity_ok)
        self.assertEqual(entries[0].when, "2026-01-01 01:02:03")

    def test_corrupt_manifest_does_not_break_the_listing(self) -> None:
        directory = backup_directory_for(self.save_path, self.state_root)
        directory.mkdir(parents=True)
        (directory / "SAVEDATA-20260101-010203-deadbeef-plain.bin").write_bytes(b"y")
        (directory / "backup-manifest.json").write_text("{not json", encoding="utf-8")
        entries = list_backups(self.save_path, self.state_root)
        self.assertEqual(len(entries), 1)

    def test_newest_backup_comes_first(self) -> None:
        directory = backup_directory_for(self.save_path, self.state_root)
        directory.mkdir(parents=True)
        for stamp in ("20260101-010203-aaaaaaaa", "20260202-040506-bbbbbbbb",
                      "20260303-070809-cccccccc"):
            (directory / f"SAVEDATA-{stamp}-plain.bin").write_bytes(b"z")
        entries = list_backups(self.save_path, self.state_root)
        self.assertEqual([entry.created_at for entry in entries],
                         ["20260303-070809-cccccccc", "20260202-040506-bbbbbbbb",
                          "20260101-010203-aaaaaaaa"])

    def test_manifest_hash_mismatch_is_flagged_and_refused(self) -> None:
        directory = backup_directory_for(self.save_path, self.state_root)
        directory.mkdir(parents=True)
        plain = directory / "SAVEDATA-20260101-010203-deadbeef-plain.bin"
        plain.write_bytes(b"tampered")
        (directory / "backup-manifest.json").write_text(json.dumps({
            "backup_manifest_schema": savefile.BACKUP_MANIFEST_SCHEMA,
            "plain_backup": plain.name,
            "plain_backup_sha256": "0" * 64,
            "main_save_sha256": "1" * 64,
            "steam_account_id": ACCOUNT,
            "save_slot_index": SLOT,
            "created_at": "20260101-010203-deadbeef",
        }), encoding="utf-8")
        entries = list_backups(self.save_path, self.state_root)
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0].integrity_ok)
        self.assertIn("校验不符", entries[0].describe())
        with self.assertRaises(SaveError) as caught:
            read_backup_plaintext(entries[0])
        self.assertIn("损坏", str(caught.exception))

    def test_manifest_less_of_a_wrong_size_is_refused(self) -> None:
        entry = savefile.BackupEntry(plain_path=self.save_path, created_at="x",
                                     plain_size=1, plain_sha256="")
        with self.assertRaises(SaveError) as caught:
            read_backup_plaintext(entry)
        self.assertIn("RNNUSR", str(caught.exception))


class NoCryptoRestoreTests(unittest.TestCase):
    """Gate and dry-run behaviour, exercising the real code with a fake crypto."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="nioh3-restore-")
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.state_root = self.root / "state"
        self.plain = support.build_plain_save()
        self.backup_path = self.root / "SAVEDATA-20260101-010203-deadbeef-plain.bin"
        self.backup_path.write_bytes(self.plain)
        self.save_path = Path(tempfile.mkdtemp(prefix="nioh3-save-")) / "SAVEDATA.BIN"
        self.addCleanup(lambda: None)
        self.save_path.write_bytes(b"encrypted")
        self.save = SaveDescriptor(self.save_path, ACCOUNT, SLOT, USER_SAVE_SIZE)
        self.entry = savefile.BackupEntry(
            plain_path=self.backup_path, created_at="20260101-010203-deadbeef",
            plain_size=len(self.plain), plain_sha256="")

    def test_running_game_refuses_the_restore(self) -> None:
        with mock.patch.object(savefile, "running_game_processes",
                               return_value=("Nioh3.exe",)), \
                mock.patch.object(editor, "write_encrypted_save") as writer:
            with self.assertRaises(SaveError) as caught:
                editor.restore_backup(self.save, self.entry, crypto=mock.Mock(),
                                      state_root=self.state_root)
        self.assertIn("Nioh3.exe", str(caught.exception))
        writer.assert_not_called()

    def test_force_while_running_is_recorded(self) -> None:
        with mock.patch.object(savefile, "running_game_processes",
                               return_value=("Nioh3.exe",)):
            report = editor.restore_backup(
                self.save, self.entry, crypto=mock.Mock(),
                state_root=self.state_root, dry_run=True, allow_game_running=True)
        self.assertEqual(report["game_processes_running"], ["Nioh3.exe"])
        self.assertTrue(report["dry_run"])

    def test_dry_run_writes_nothing(self) -> None:
        before = self.save_path.read_bytes()
        with mock.patch.object(savefile, "running_game_processes", return_value=()), \
                mock.patch.object(editor, "write_encrypted_save") as writer, \
                mock.patch.object(editor, "create_backup") as backup:
            report = editor.restore_backup(self.save, self.entry, crypto=mock.Mock(),
                                           state_root=self.state_root, dry_run=True)
        writer.assert_not_called()
        backup.assert_not_called()
        self.assertEqual(self.save_path.read_bytes(), before)
        self.assertNotIn(BACKUP_SUBDIRECTORY_NAME,
                         [path.name for path in self.root.rglob("*")])
        self.assertIsNone(report["new_sha256"])
        self.assertIsNone(report["safety_backup_dir"])

    def test_a_corrupted_backup_is_refused_without_touching_the_save(self) -> None:
        self.backup_path.write_bytes(b"junk")
        before = self.save_path.read_bytes()
        with mock.patch.object(editor, "write_encrypted_save") as writer:
            with self.assertRaises(SaveError):
                editor.restore_backup(self.save, self.entry, crypto=mock.Mock(),
                                      state_root=self.state_root)
        writer.assert_not_called()
        self.assertEqual(self.save_path.read_bytes(), before)


@unittest.skipUnless(support.HAVE_EXE, "reference crypto executable is missing")
class RestoreRoundTripTests(unittest.TestCase):
    """The real thing: encrypt a save, back it up, damage it, restore it."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="nioh3-roundtrip-")
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.state_root = self.root / "state"
        self.crypto = savefile.SaveCrypto(support.EXE_PATH, prefer_python=False)
        self.save_path = (self.root / str(ACCOUNT) / f"SAVEDATA{SLOT:02d}"
                          / "SAVEDATA.BIN")
        self.save_path.parent.mkdir(parents=True)

        self.plain = support.build_plain_save(records_by_slot={
            3: support.build_record(record_type=0x4001, level=150, rarity=5,
                                    effects=()),
        })
        self._encrypt_plain(self.plain)
        self.original_sha = savefile.sha256_file(self.save_path)
        self.save = SaveDescriptor(self.save_path, ACCOUNT, SLOT,
                                   self.save_path.stat().st_size)

    def _encrypt_plain(self, data: bytes) -> None:
        """Write ``data`` to the save file as the game would (encrypted)."""
        staged = self.root / "staged-plain.bin"
        staged.write_bytes(data)
        # The reference tool refuses to overwrite, so clear the target first:
        # this stands in for the game replacing its save file.
        self.save_path.unlink(missing_ok=True)
        self.crypto.encrypt(staged, self.save_path)

    def test_restore_reproduces_the_backed_up_file_exactly(self) -> None:
        create_backup(self.save_path, state_root=self.state_root, crypto=self.crypto)
        entries = list_backups(self.save_path, self.state_root)
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].integrity_ok)
        self.assertEqual(entries[0].original_save_sha256, self.original_sha)

        # Damage the save the way a bad edit would.
        mutated = bytearray(self.plain)
        mutated[0x100:0x104] = b"\xde\xad\xbe\xef"
        self._encrypt_plain(bytes(mutated))
        self.assertNotEqual(savefile.sha256_file(self.save_path), self.original_sha)

        report = editor.restore_backup(self.save, entries[0], crypto=self.crypto,
                                       state_root=self.state_root)
        self.assertTrue(report["verified"])
        self.assertEqual(report["new_sha256"], self.original_sha)
        self.assertIs(report["matches_original_save"], True)
        self.assertEqual(savefile.sha256_file(self.save_path), self.original_sha)
        # ... and the payload really is back (the trailing 8 bytes are excluded
        # from the comparison on purpose: the reference tool zeroes them, so a
        # decrypt/encrypt round trip cannot preserve them -- see
        # write_encrypted_save and tests/test_crypto.py).
        restored = savefile.decrypt_save_to_bytes(self.save_path, self.crypto)
        self.assertEqual(support.covered(restored), support.covered(self.plain))

    def test_restore_keeps_the_on_disk_tail_the_backup_cannot_carry(self) -> None:
        """The reference tool zeroes the tail, so a non-zero tail must survive.

        The backup therefore cannot record it; the restore has to keep what is on
        disk (never write zeros over it) and must say so in the report.
        """
        tail_at_backup = bytes.fromhex("1111222233334444")
        tail_now = bytes.fromhex("99aabbccddeeff00")
        self._set_tail(tail_at_backup)
        create_backup(self.save_path, state_root=self.state_root, crypto=self.crypto)
        entry = list_backups(self.save_path, self.state_root)[0]
        # Proof of the premise: the backup holds zeros, not the real tail.
        self.assertEqual(read_backup_plaintext(entry)[-8:], bytes(8))

        # The game wrote a different tail since, and the payload got damaged.
        self._encrypt_plain(b"RNNUSR" + self.plain[6:])
        self._set_tail(tail_now)

        report = editor.restore_backup(self.save, entry, crypto=self.crypto,
                                       state_root=self.state_root)
        self.assertEqual(self._tail(), tail_now, "不能把尾字节写成 0")
        self.assertEqual(report["tail_source"], "on-disk")
        # The payload is restored ...
        restored = savefile.decrypt_save_to_bytes(self.save_path, self.crypto)
        self.assertEqual(support.covered(restored), support.covered(self.plain))
        # ... but the file is not byte-identical to the backed-up one (that tail
        # is unrecoverable), and the report says exactly that, not something
        # reassuring and untrue.
        self.assertIs(report["matches_original_save"], False)

    def test_restore_writes_a_tail_the_backup_does_carry(self) -> None:
        """A backup with a real tail (pure-Python backend / hand-made) wins."""
        tail = bytes.fromhex("1122334455667788")
        entry = savefile.BackupEntry(
            plain_path=self.root / "handmade-plain.bin",
            created_at="20260101-000000-aaaaaaaa",
            plain_size=len(self.plain),
            plain_sha256="",
        )
        entry.plain_path.write_bytes(self.plain[:-8] + tail)
        report = editor.restore_backup(self.save, entry, crypto=self.crypto,
                                       state_root=self.state_root)
        self.assertEqual(self._tail(), tail)
        self.assertEqual(report["tail_source"], "backup")

    def test_a_backup_from_another_slot_is_reported(self) -> None:
        entry = savefile.BackupEntry(
            plain_path=self.root / "other-slot-plain.bin",
            created_at="20260101-000000-bbbbbbbb",
            plain_size=len(self.plain), plain_sha256="",
            account_id=ACCOUNT, slot_index=SLOT + 1,
        )
        entry.plain_path.write_bytes(self.plain)
        report = editor.restore_backup(self.save, entry, crypto=self.crypto,
                                       state_root=self.state_root, dry_run=True)
        self.assertTrue(report["source_mismatch"])
        self.assertIn(f"栏位 {SLOT + 1:02d}", report["source_mismatch"][0])

    def test_a_backup_from_another_account_is_reported(self) -> None:
        entry = savefile.BackupEntry(
            plain_path=self.root / "other-account-plain.bin",
            created_at="20260101-000000-cccccccc",
            plain_size=len(self.plain), plain_sha256="",
            account_id=ACCOUNT + 1, slot_index=SLOT,
        )
        entry.plain_path.write_bytes(self.plain)
        report = editor.restore_backup(self.save, entry, crypto=self.crypto,
                                       state_root=self.state_root, dry_run=True)
        self.assertEqual(len(report["source_mismatch"]), 1)
        self.assertIn("账号", report["source_mismatch"][0])

    def test_a_matching_backup_reports_no_mismatch(self) -> None:
        entry = savefile.BackupEntry(
            plain_path=self.root / "matching-plain.bin",
            created_at="20260101-000000-dddddddd",
            plain_size=len(self.plain), plain_sha256="",
            account_id=ACCOUNT, slot_index=SLOT,
        )
        entry.plain_path.write_bytes(self.plain)
        report = editor.restore_backup(self.save, entry, crypto=self.crypto,
                                       state_root=self.state_root, dry_run=True)
        self.assertEqual(report["source_mismatch"], [])

    def _tail(self) -> bytes:
        return self.save_path.read_bytes()[-savefile.UNCOVERED_TAIL_BYTES:]

    def _set_tail(self, tail: bytes) -> None:
        """Overwrite the on-disk trailing bytes (outside the crypto scope)."""
        with self.save_path.open("r+b") as handle:
            handle.seek(-len(tail), os.SEEK_END)
            handle.write(tail)

    def test_restore_keeps_a_safety_backup_of_what_it_overwrote(self) -> None:
        create_backup(self.save_path, state_root=self.state_root, crypto=self.crypto)
        first = list_backups(self.save_path, self.state_root)[0]

        mutated = bytearray(self.plain)
        mutated[0x200] ^= 0xFF
        self._encrypt_plain(bytes(mutated))
        damaged_sha = savefile.sha256_file(self.save_path)

        editor.restore_backup(self.save, first, crypto=self.crypto,
                             state_root=self.state_root)
        entries = list_backups(self.save_path, self.state_root)
        self.assertEqual(len(entries), 2, "恢复前应先自动备份当前存档")
        # The newest entry is the safety copy of the damaged state; restoring it
        # puts the damage back, which is what makes a restore reversible.
        safety = entries[0]
        self.assertEqual(safety.original_save_sha256, damaged_sha)
        editor.restore_backup(self.save, safety, crypto=self.crypto,
                             state_root=self.state_root)
        self.assertEqual(savefile.sha256_file(self.save_path), damaged_sha)

    def test_dry_run_then_real_restore(self) -> None:
        create_backup(self.save_path, state_root=self.state_root, crypto=self.crypto)
        entry = list_backups(self.save_path, self.state_root)[0]
        self._encrypt_plain(b"\x00" + self.plain[1:])
        damaged = savefile.sha256_file(self.save_path)

        editor.restore_backup(self.save, entry, crypto=self.crypto,
                              state_root=self.state_root, dry_run=True)
        self.assertEqual(savefile.sha256_file(self.save_path), damaged)

        editor.restore_backup(self.save, entry, crypto=self.crypto,
                              state_root=self.state_root)
        self.assertEqual(savefile.sha256_file(self.save_path), self.original_sha)


if __name__ == "__main__":
    unittest.main()
