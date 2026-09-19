"""Frozen-layout tests: paths, payload extraction and the resulting layout.

The single-file executable cannot be exercised in unit tests, so the parts that
decide *where* files live are made pure enough to test directly: the extraction
pass is a function of (payload zip, target directory), and the path helpers are
a function of the ``sys`` attributes a frozen build sets.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from nioh3_accessory_editor import bootstrap, paths

from tools import make_payload  # noqa: E402 - path set up by tests/__init__


def write_zip(path: Path, files: dict[str, bytes], *, with_index: bool = True,
              version: str = "0.1.0", commit: str = "deadbeef") -> Path:
    index = make_payload.build_index(files, version, commit, "2026-01-01T00:00:00+08:00")
    with zipfile.ZipFile(path, "w") as archive:
        if with_index:
            archive.writestr(bootstrap.PAYLOAD_INDEX_NAME,
                             json.dumps(index, ensure_ascii=False))
        for name, data in files.items():
            archive.writestr(name, data)
    return path


class PathTests(unittest.TestCase):
    def test_source_layout(self) -> None:
        self.assertFalse(paths.is_frozen())
        # A source run reports the checkout root, not the package directory.
        self.assertEqual(paths.application_root().name, "Nioh3AccessoryEditor")
        self.assertTrue((paths.application_root() / "launch_editor.py").is_file())
        self.assertEqual(paths.resource_root(), paths.application_root())
        self.assertIsNone(paths.bundle_root())
        self.assertEqual(paths.default_catalog_path(),
                         paths.application_root() / "data" / "accessory_affixes.json")
        self.assertTrue(paths.default_catalog_path().is_file())
        self.assertEqual(paths.default_crypto_exe(),
                         paths.application_root() / "bin" / "Nioh_Savefile_decrypt.exe")

    def test_frozen_layout_uses_the_executable_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nioh3-frozen-") as temp:
            exe = Path(temp) / "Nioh3AccessoryEditor.exe"
            meipass = Path(temp) / "_MEI12345"
            meipass.mkdir()
            with mock.patch.object(sys, "frozen", True, create=True), \
                    mock.patch.object(sys, "executable", str(exe)), \
                    mock.patch.object(sys, "_MEIPASS", str(meipass), create=True):
                self.assertTrue(paths.is_frozen())
                self.assertEqual(paths.application_root(), Path(temp))
                # Resources come from next to the exe, never from the temp dir.
                self.assertEqual(paths.resource_root(), Path(temp))
                self.assertEqual(paths.bundle_root(), meipass)
                self.assertEqual(paths.default_crypto_exe(),
                                 Path(temp) / "bin" / "Nioh_Savefile_decrypt.exe")
                self.assertEqual(paths.default_config_path(),
                                 Path(temp) / "config" / "editor.json")
                self.assertEqual(paths.default_state_root(), Path(temp))

    def test_source_state_root_is_the_working_directory(self) -> None:
        self.assertEqual(paths.default_state_root(), Path.cwd())


class ExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="nioh3-extract-")
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.target = self.root / "app"
        self.target.mkdir()
        self.files = {
            "data/catalog.json": b'{"a": 1}',
            "config/editor.json": b'{"schema": "x"}',
            "bin/tool.exe": b"MZ fake",
            "third_party/source-data/README.md": b"docs",
        }
        self.payload = write_zip(self.root / "app-payload.zip", self.files)

    def extract(self) -> bootstrap.ExtractionReport:
        return bootstrap.ensure_side_by_side(self.payload, self.target)

    def test_first_run_extracts_everything(self) -> None:
        report = self.extract()
        self.assertTrue(report.writable)
        self.assertTrue(report.first_run)
        self.assertEqual(sorted(report.extracted), sorted(self.files))
        self.assertEqual(report.kept, [])
        for name, data in self.files.items():
            self.assertEqual((self.target / name).read_bytes(), data, name)
        self.assertTrue((self.target / paths.EXTRACTION_MANIFEST_NAME).is_file())

    def test_second_run_changes_nothing(self) -> None:
        self.extract()
        report = self.extract()
        self.assertFalse(report.changed)
        self.assertEqual(sorted(report.unchanged), sorted(self.files))

    def test_user_edit_is_never_overwritten(self) -> None:
        self.extract()
        edited = self.target / "config/editor.json"
        edited.write_bytes(b'{"schema": "user edited"}')

        report = self.extract()
        self.assertIn("config/editor.json", report.kept)
        self.assertEqual(edited.read_bytes(), b'{"schema": "user edited"}')

    def test_unmodified_file_is_refreshed_when_the_payload_changes(self) -> None:
        self.extract()
        updated = dict(self.files)
        updated["data/catalog.json"] = b'{"a": 2}'
        self.payload = write_zip(self.root / "app-payload.zip", updated,
                                 version="0.2.0")

        report = self.extract()
        self.assertIn("data/catalog.json", report.refreshed)
        self.assertEqual((self.target / "data/catalog.json").read_bytes(), b'{"a": 2}')

    def test_new_payload_file_appears(self) -> None:
        self.extract()
        updated = dict(self.files)
        updated["data/new.json"] = b"new"
        self.payload = write_zip(self.root / "app-payload.zip", updated)

        report = self.extract()
        self.assertIn("data/new.json", report.extracted)
        self.assertTrue((self.target / "data/new.json").is_file())

    def test_deleted_file_comes_back(self) -> None:
        self.extract()
        (self.target / "bin/tool.exe").unlink()
        report = self.extract()
        self.assertIn("bin/tool.exe", report.extracted)

    def test_extra_files_are_left_alone(self) -> None:
        self.extract()
        mine = self.target / "my-notes.txt"
        mine.write_text("keep me", encoding="utf-8")
        self.extract()
        self.assertEqual(mine.read_text(encoding="utf-8"), "keep me")

    def test_path_traversal_is_refused(self) -> None:
        payload = write_zip(self.root / "evil.zip",
                            {"../escape.txt": b"nope", "data/ok.txt": b"fine"})
        report = bootstrap.ensure_side_by_side(payload, self.target)
        self.assertIn("../escape.txt", report.failed)
        self.assertFalse((self.root / "escape.txt").exists())
        self.assertTrue((self.target / "data/ok.txt").is_file())

    def test_missing_payload_is_reported_not_raised(self) -> None:
        report = bootstrap.ensure_side_by_side(self.root / "absent.zip", self.target)
        self.assertFalse(report.writable)
        self.assertIn("载荷", report.reason)

    def test_corrupt_payload_is_reported_not_raised(self) -> None:
        broken = self.root / "broken.zip"
        broken.write_bytes(b"not a zip")
        report = bootstrap.ensure_side_by_side(broken, self.target)
        self.assertFalse(report.writable)
        self.assertIn("载荷无法读取", report.reason)

    def test_missing_target_directories_are_created(self) -> None:
        report = bootstrap.ensure_side_by_side(self.payload,
                                               self.root / "deep" / "nested" / "app")
        self.assertTrue(report.writable)
        self.assertEqual(sorted(report.extracted), sorted(self.files))

    def test_prefix_without_index_is_tolerated(self) -> None:
        payload = write_zip(self.root / "noindex.zip", self.files, with_index=False)
        report = bootstrap.ensure_side_by_side(payload, self.target)
        self.assertEqual(sorted(report.extracted), sorted(self.files))

    def test_summary_mentions_counts(self) -> None:
        report = self.extract()
        self.assertIn("解压 4 个文件", report.summary())

    def test_ensure_once_is_a_no_op_for_source_runs(self) -> None:
        bootstrap.reset_for_tests()
        self.addCleanup(bootstrap.reset_for_tests)
        self.assertIsNone(bootstrap.ensure_once())
        self.assertIsNone(bootstrap.last_report())

    def test_ensure_once_extracts_when_asked_directly(self) -> None:
        bootstrap.reset_for_tests()
        self.addCleanup(bootstrap.reset_for_tests)
        report = bootstrap.ensure_once(self.payload, self.target)
        self.assertIsNotNone(report)
        self.assertTrue(report.first_run)
        self.assertIs(bootstrap.last_report(), report)
        self.assertIsNone(bootstrap.ensure_once(self.payload, self.target))


class FindPayloadTests(unittest.TestCase):
    def test_finds_the_payload_next_to_the_executable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nioh3-find-") as temp:
            root = Path(temp)
            payload = root / paths.BUNDLED_PAYLOAD_NAME
            payload.write_bytes(b"zip")
            with mock.patch.object(paths, "application_root", lambda: root), \
                    mock.patch.object(paths, "bundle_root", lambda: None):
                self.assertEqual(bootstrap.find_payload(), payload)

    def test_prefers_the_bundle_directory_when_frozen(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nioh3-find-") as temp:
            root = Path(temp)
            bundle = root / "_MEI1"
            bundle.mkdir()
            embedded = bundle / paths.BUNDLED_PAYLOAD_NAME
            embedded.write_bytes(b"zip")
            with mock.patch.object(paths, "bundle_root", lambda: bundle), \
                    mock.patch.object(paths, "application_root", lambda: root):
                self.assertEqual(bootstrap.find_payload(), embedded)

    def test_none_when_absent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nioh3-find-") as temp:
            with mock.patch.object(paths, "bundle_root", lambda: None), \
                    mock.patch.object(paths, "application_root", lambda: Path(temp)):
                self.assertIsNone(bootstrap.find_payload())


if __name__ == "__main__":
    unittest.main()
