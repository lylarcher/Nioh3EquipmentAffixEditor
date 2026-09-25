"""Payload packaging tests: what the single-file exe carries inside itself.

The exe itself is built by PyInstaller (too slow for unit tests), so instead the
two halves of the contract are pinned down here:

* ``tools/make_payload.py`` must produce a deterministic archive containing
  exactly the side-by-side files -- and the configuration template inside it
  must load in the real loader;
* extracting that archive with ``bootstrap`` must yield a working application
  directory (catalogue, crypto helper, configuration) without the sources.
"""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from nioh3_equipment_affix_editor import bootstrap, paths
from nioh3_equipment_affix_editor.affixdb import AffixDb
from nioh3_equipment_affix_editor.config import CONFIG_SCHEMA, load_config

from tools import make_payload

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = make_payload.collect_payload_files(
            version="0.1.0", commit="deadbeef")

    def test_ships_the_editable_resources(self) -> None:
        for expected in ("data/accessory_affixes.json",
                         "data/grace_affixes.json",
                         "data/accessory_items.json",
                         "data/soul_affixes.json",
                         "data/soul_items.json",
                         "config/editor.json",
                         "assets/app.ico",
                         "assets/logo.png",
                         "assets/logo-32.png",
                         "assets/logo-64.png",
                         "bin/Nioh_Savefile_decrypt.exe",
                         "README.md",
                         "CHANGELOG.md",
                         "LICENSE"):
            self.assertIn(expected, self.payload, expected)
        self.assertTrue(any(name.startswith("third_party/source-data/")
                            for name in self.payload))

    def test_ships_the_same_icon_data_as_the_icon_asset(self) -> None:
        on_disk = (PROJECT_ROOT / "assets" / "app.ico").read_bytes()
        self.assertEqual(self.payload["assets/app.ico"], on_disk)

    def test_ships_no_python_sources_or_caches(self) -> None:
        for name, data in self.payload.items():
            self.assertFalse(name.endswith(".py"), name)
            self.assertNotIn("__pycache__", name)
            self.assertFalse(name.endswith(".pyc"), name)
            self.assertTrue(data, f"{name} 是空文件")

    def test_affix_catalogue_in_the_payload_is_the_real_one(self) -> None:
        shipped = self.payload["data/accessory_affixes.json"]
        on_disk = (PROJECT_ROOT / "data" / "accessory_affixes.json").read_bytes()
        self.assertEqual(shipped, on_disk)

    def test_grace_catalogue_in_the_payload_is_the_real_one(self) -> None:
        shipped = self.payload["data/grace_affixes.json"]
        on_disk = (PROJECT_ROOT / "data" / "grace_affixes.json").read_bytes()
        self.assertEqual(shipped, on_disk)

    def test_item_catalogue_in_the_payload_is_the_real_one(self) -> None:
        shipped = self.payload["data/accessory_items.json"]
        on_disk = (PROJECT_ROOT / "data" / "accessory_items.json").read_bytes()
        self.assertEqual(shipped, on_disk)

    def test_missing_directories_are_tolerated(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nioh3-payload-") as temp:
            root = Path(temp)
            (root / "README.md").write_text("hello", encoding="utf-8")
            payload = make_payload.collect_payload_files(root)
        # README + the always-generated configuration template.
        self.assertEqual(sorted(payload), ["README.md", "config/editor.json"])


class IndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = {"data/a.json": b"{}", "config/editor.json": b"{}"}
        self.index = make_payload.build_index(self.payload, "0.1.0", "deadbeef",
                                             "2026-01-01T00:00:00+08:00")

    def test_index_records_size_and_hash(self) -> None:
        entry = self.index["files"]["data/a.json"]
        self.assertEqual(entry["size"], 2)
        self.assertEqual(len(entry["sha256"]), 64)
        self.assertEqual(self.index["schema"], bootstrap.PAYLOAD_SCHEMA)

    def test_archive_round_trip_verifies(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nioh3-payload-") as temp:
            archive = make_payload.write_payload(Path(temp) / "p.zip",
                                                self.payload, self.index)
            self.assertEqual(make_payload.verify_payload(archive), [])

    def test_verification_catches_tampering(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nioh3-payload-") as temp:
            archive = make_payload.write_payload(Path(temp) / "p.zip",
                                                self.payload, self.index)
            with zipfile.ZipFile(archive, "a") as handle:
                handle.writestr("data/a.json", b"{ }")
                handle.writestr("extra.txt", b"surprise")
            problems = make_payload.verify_payload(archive)
        self.assertTrue(any("大小不一致" in problem for problem in problems), problems)
        self.assertTrue(any("extra.txt" in problem for problem in problems), problems)

    def test_verification_catches_a_missing_index(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nioh3-payload-") as temp:
            archive = Path(temp) / "p.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("data/a.json", b"{}")
            problems = make_payload.verify_payload(archive)
        self.assertEqual(len(problems), 1)
        self.assertIn(bootstrap.PAYLOAD_INDEX_NAME, problems[0])

    def test_archive_bytes_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nioh3-payload-") as temp:
            first = make_payload.write_payload(Path(temp) / "a.zip",
                                              self.payload, self.index).read_bytes()
            second = make_payload.write_payload(Path(temp) / "b.zip",
                                               self.payload, self.index).read_bytes()
        self.assertEqual(first, second)


class CliTests(unittest.TestCase):
    def test_main_writes_and_verifies_a_real_payload(self) -> None:
        import contextlib
        import io

        with tempfile.TemporaryDirectory(prefix="nioh3-payload-") as temp:
            target = Path(temp) / "app-payload.zip"
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = make_payload.main([
                    "--output", str(target),
                    "--version", "0.1.0", "--commit", "deadbeef",
                    "--created", "2026-01-01T00:00:00+08:00",
                    "--verify", "--quiet",
                ])
            self.assertEqual(code, 0, buffer.getvalue())
            self.assertTrue(target.is_file())
            self.assertEqual(make_payload.verify_payload(target), [])

    def test_json_output_is_machine_readable(self) -> None:
        import contextlib
        import io

        with tempfile.TemporaryDirectory(prefix="nioh3-payload-") as temp:
            target = Path(temp) / "app-payload.zip"
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                make_payload.main(["--output", str(target), "--json", "--verify"])
            summary = json.loads(buffer.getvalue())
        self.assertGreater(summary["files"], 3)
        self.assertGreater(summary["bytes"], 0)
        self.assertEqual(summary["problems"], [])


class ExtractedApplicationTests(unittest.TestCase):
    """The frozen layout a user ends up with must actually work."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="nioh3-frozen-app-")
        self.addCleanup(self._temp.cleanup)
        self.target = Path(self._temp.name) / "app"
        self.target.mkdir()
        payload = make_payload.collect_payload_files(version="0.1.0",
                                                     commit="deadbeef")
        index = make_payload.build_index(payload, "0.1.0", "deadbeef", "")
        archive = make_payload.write_payload(Path(self._temp.name) / "app-payload.zip",
                                            payload, index)
        self.report = bootstrap.ensure_side_by_side(archive, self.target)

    def test_extraction_succeeded(self) -> None:
        self.assertTrue(self.report.writable)
        self.assertEqual(self.report.failed, [])

    def test_the_shipped_catalogue_loads(self) -> None:
        catalogue = self.target / "data" / "accessory_affixes.json"
        self.assertTrue(catalogue.is_file())
        database = AffixDb.from_file(catalogue)
        self.assertGreater(len(database.all()), 200)
        # And the entries survive the round trip with their categories intact.
        self.assertTrue(database.categories())

    def test_the_shipped_configuration_loads(self) -> None:
        config_file = self.target / "config" / "editor.json"
        loaded = load_config(config_file)
        self.assertEqual(loaded.source, config_file)
        self.assertTrue(loaded.default_dry_run)
        # Nothing is configured, so backups go to the program directory: the
        # extraction target when frozen, the working directory from sources.
        self.assertEqual(loaded.resolved_backup_root(), paths.default_state_root())

    def test_the_shipped_crypto_helper_is_present_and_non_empty(self) -> None:
        helper = self.target / "bin" / "Nioh_Savefile_decrypt.exe"
        self.assertTrue(helper.is_file())
        self.assertGreater(helper.stat().st_size, 10_000)

    def test_resources_are_read_from_next_to_the_executable(self) -> None:
        import sys
        from unittest import mock

        exe = self.target / "Nioh3EquipmentAffixEditor.exe"
        exe.write_bytes(b"MZ")
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", str(exe)), \
                mock.patch.object(sys, "_MEIPASS", str(self.target / "_MEI1"),
                                  create=True):
            self.assertEqual(paths.application_root(), self.target)
            self.assertEqual(paths.default_catalog_path(),
                             self.target / "data" / "accessory_affixes.json")
            self.assertTrue(paths.default_catalog_path().is_file())
            self.assertTrue(paths.default_crypto_exe().is_file())
            self.assertEqual(paths.default_state_root(), self.target)


class SpecContractTests(unittest.TestCase):
    """The PyInstaller spec is what makes the release a single file."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = PROJECT_ROOT / "Nioh3EquipmentAffixEditor.spec"
        cls.source = cls.spec.read_text(encoding="utf-8")
        ast.parse(cls.source)
        cls.calls = [node for node in ast.walk(ast.parse(cls.source))
                     if isinstance(node, ast.Call)]

    def call_names(self) -> set[str]:
        names = set()
        for call in self.calls:
            name = call.func
            if isinstance(name, ast.Name):
                names.add(name.id)
        return names

    def test_builds_one_file(self) -> None:
        """No COLLECT step: everything must go into the single EXE."""
        self.assertIn("Analysis", self.call_names())
        self.assertIn("EXE", self.call_names())
        self.assertNotIn("COLLECT", self.call_names())
        self.assertIn("a.binaries", self.source)
        self.assertIn("a.datas", self.source)

    def test_embeds_the_payload(self) -> None:
        self.assertIn("app-payload.zip", self.source)
        self.assertIn("datas=", self.source)

    def test_uses_the_windowed_subsystem(self) -> None:
        """No console window may be created (the GUI needs none).

        CLI output still works because the entry point borrows the terminal that
        started the process — see console.attach_parent_console().
        """
        self.assertIn("console=False", self.source)
        self.assertNotIn("console=True", self.source)

    def test_includes_the_generated_build_identity(self) -> None:
        self.assertIn("nioh3_equipment_affix_editor._buildinfo", self.source)

    def test_does_not_ship_tests_or_tools(self) -> None:
        for excluded in ("tests", "tools"):
            self.assertIn(f'"{excluded}"', self.source)

    def test_entry_point_exists(self) -> None:
        self.assertTrue((PROJECT_ROOT / "launch_editor.py").is_file())
        self.assertIn("launch_editor.py", self.source)

    def test_uses_no_upx_and_no_icon_argument_guessing(self) -> None:
        self.assertIn("upx=False", self.source)

    def test_embeds_the_application_icon(self) -> None:
        """The exe icon comes from the committed asset, and its absence fails."""
        self.assertIn("ICON = PROJECT_ROOT", self.source)
        self.assertIn('"app.ico"', self.source)
        self.assertIn("icon=str(ICON)", self.source)
        self.assertIn("缺少图标", self.source)


if __name__ == "__main__":
    unittest.main()
