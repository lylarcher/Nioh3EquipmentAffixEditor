"""Tests for the build/version identity (commit tail, source, time, language).

These cover both halves of the feature:

* ``nioh3_accessory_editor.version`` -- what the app reports at runtime, from a
  frozen ``_buildinfo`` module when present and from live git otherwise.
* ``tools/make_build_info.py`` -- the generator ``build.ps1`` drives, including
  the guarantee that the generated module reproduces every reported fact.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unicodedata
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from nioh3_accessory_editor import paths, version as version_module
from nioh3_accessory_editor.version import (
    UNKNOWN,
    BuildInfo,
    short_commit,
    version_banner,
    version_info,
    version_lines,
)
from tests import support

make_build_info = support.load_tool_module("make_build_info")


def display_width(text: str) -> int:
    """Independent width helper: CJK/full-width characters occupy 2 columns."""
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1
               for char in text)


def sample_info(**overrides) -> BuildInfo:
    fields = dict(
        version="9.9.9",
        commit="deadbeef",
        commit_full="0" * 32 + "deadbeef",
        branch="main",
        dirty=False,
        built_at="2026-01-02T03:04:05+08:00",
        built_from=r"D:\build\source",
        crypto_exe=r"D:\build\source\bin\Nioh_Savefile_decrypt.exe",
        crypto_exe_sha256="ab" * 32,
        language="CPython 3.10.10 (仅标准库 / stdlib only)",
        python_version="3.10.10",
        source="build",
    )
    fields.update(overrides)
    return BuildInfo(**fields)


class ShortCommitTests(unittest.TestCase):
    def test_short_commit_is_the_tail_not_the_prefix(self) -> None:
        full = "58be86b3164cd181a099c4f5971b563834cca8de"
        self.assertEqual(short_commit(full), "34cca8de")
        self.assertEqual(short_commit(full), full[-8:])
        self.assertNotEqual(short_commit(full), full[:8])

    def test_short_commit_edge_cases(self) -> None:
        self.assertEqual(short_commit("abc"), "abc")
        self.assertEqual(short_commit("12345678"), "12345678")
        self.assertEqual(short_commit(""), UNKNOWN)
        self.assertEqual(short_commit(None), UNKNOWN)
        self.assertEqual(short_commit("  abcdef123456  "), "ef123456")

    def test_alias_matches(self) -> None:
        self.assertIs(version_module.commit_short, version_module.short_commit)


class LiveInfoTests(unittest.TestCase):
    def setUp(self) -> None:
        support.forget_build_info_module()
        version_module.reset_cache()
        self.addCleanup(version_module.reset_cache)
        self.addCleanup(support.forget_build_info_module)

    def test_reports_a_full_set_of_facts(self) -> None:
        info = version_info(refresh=True)
        self.assertTrue(info.version)
        self.assertTrue(info.language.lower().startswith(("cpython", "pypy")))
        self.assertTrue(info.built_from)
        self.assertTrue(info.crypto_exe.endswith("Nioh_Savefile_decrypt.exe"))
        self.assertIn(info.source, {"build", "git", "fallback"})

    def test_commit_is_eight_characters_or_explicitly_unknown(self) -> None:
        info = version_info(refresh=True)
        if info.commit == UNKNOWN:
            self.skipTest("git metadata unavailable in this environment")
        self.assertEqual(len(info.commit), 8)
        self.assertEqual(info.commit, info.commit_full[-8:])

    def test_cache_can_be_reset(self) -> None:
        first = version_info()
        self.assertIs(first, version_info())
        self.assertIsNot(first, version_info(refresh=True))


class FrozenInfoTests(unittest.TestCase):
    """The generated module must win over live git interrogation."""

    GENERATED = """
BUILD_SCHEMA = 'nioh3-accessory-editor-build/v1'
VERSION = '1.2.3'
COMMIT = 'cafebabe'
COMMIT_FULL = '11111111111111111111111111111111cafebabe'
BRANCH = 'release'
DIRTY = True
BUILT_AT = '2026-05-06T07:08:09+08:00'
BUILT_FROM = 'D:\\\\ci\\\\checkout'
CRYPTO_EXE = 'D:\\\\ci\\\\checkout\\\\bin\\\\Nioh_Savefile_decrypt.exe'
CRYPTO_EXE_SHA256 = 'cd' * 32
LANGUAGE = 'CPython 3.11.9 (仅标准库 / stdlib only)'
PYTHON_VERSION = '3.11.9'
"""

    def setUp(self) -> None:
        version_module.reset_cache()
        self.addCleanup(version_module.reset_cache)
        self.addCleanup(support.forget_build_info_module)
        module = support.install_build_info_module(self.GENERATED)
        self.module = module

    def test_frozen_module_overrides_live_git(self) -> None:
        info = version_info(refresh=True)
        self.assertEqual(info.source, "build")
        self.assertEqual((info.version, info.commit), ("1.2.3", "cafebabe"))
        self.assertEqual(info.built_at, "2026-05-06T07:08:09+08:00")
        self.assertEqual(info.built_from, r"D:\ci\checkout")
        self.assertTrue(info.dirty)

    def test_banner_shows_all_four_required_facts(self) -> None:
        text = version_banner()
        self.assertIn("cafebabe", text)
        self.assertIn("Nioh_Savefile_decrypt.exe", text)
        self.assertIn("2026-05-06T07:08:09+08:00", text)
        self.assertIn("CPython 3.11.9", text)

    def test_frozen_banner_points_at_run_time_locations(self) -> None:
        """来源 must be where the exe runs from, not where it was built."""
        text = version_banner()
        runtime_root = paths.application_root()
        self.assertIn(str(runtime_root), text)
        self.assertIn(str(paths.default_crypto_exe()), text)
        # The build-time root is never printed: it names the build machine.
        self.assertNotIn(r"D:\ci\checkout", text)
        self.assertNotIn("构建来源", text)
        self.assertNotIn(r"来源      : D:\ci\checkout", text)

    def test_run_time_location_is_where_the_exe_lives(self) -> None:
        """Copying the exe elsewhere must change 来源 without a rebuild."""
        version_info(refresh=True)
        fake = Path(r"D:\copy\elsewhere\Nioh3AccessoryEditor")
        with mock.patch.object(paths, "application_root", lambda: fake), \
                mock.patch.object(
                    paths, "default_crypto_exe",
                    lambda: fake / "bin" / "Nioh_Savefile_decrypt.exe"):
            text = version_banner()
        self.assertIn(str(fake), text)
        self.assertIn(str(fake / "bin" / "Nioh_Savefile_decrypt.exe"), text)
        self.assertNotIn("构建来源", text)


class BannerRenderingTests(unittest.TestCase):
    def test_labels_line_up_in_display_columns(self) -> None:
        lines = version_lines(sample_info())
        body = [line for line in lines[1:] if ":" in line]
        columns = {display_width(line.split(":", 1)[0]) for line in body}
        self.assertEqual(len(columns), 1, columns)
        for line in body:
            self.assertTrue(line.split(":", 1)[0].endswith(" "), line)

    def test_banner_contains_every_required_field(self) -> None:
        info = sample_info()
        text = version_banner(info)
        self.assertIn(f"v{info.version}", text)
        self.assertIn(info.commit, text)
        self.assertIn(str(paths.application_root()), text)
        self.assertIn(str(paths.default_crypto_exe()), text)
        self.assertIn(info.built_at, text)
        self.assertIn(info.language, text)
        self.assertNotIn(info.built_from, text)  # 构建机路径一律不显示

    def test_dirty_worktree_is_called_out(self) -> None:
        self.assertIn("未提交改动", version_banner(sample_info(dirty=True)))
        self.assertNotIn("未提交改动", version_banner(sample_info(dirty=False)))

    def test_unbuilt_checkout_says_so(self) -> None:
        info = sample_info(source="git", built_at=UNKNOWN)
        self.assertIn("未构建", version_banner(info))

    def test_the_build_root_never_produces_a_line(self) -> None:
        """构建目录与运行目录相同、或完全不同，都不该多出任何一行。"""
        same = sample_info(built_from=str(paths.application_root()))
        other = sample_info(built_from=r"D:\somewhere-else")
        self.assertNotIn("构建来源", version_banner(same))
        self.assertNotIn("构建来源", version_banner(other))
        self.assertNotIn(r"D:\somewhere-else", version_banner(other))

    def test_long_paths_are_elided(self) -> None:
        long_root = "D:\\" + "x" * 200
        lines = version_lines(sample_info(), location=long_root,
                              crypto_exe=long_root + "\\bin\\crypto.exe")
        line = next(line for line in lines if line.startswith("来源"))
        self.assertIn("...", line)
        self.assertLessEqual(display_width(line), 90)

    def test_elision_respects_cjk_width(self) -> None:
        lines = version_lines(sample_info(), location="D:\\构建\\" + "宽" * 100)
        line = next(line for line in lines if line.startswith("来源"))
        self.assertIn("...", line)
        self.assertLessEqual(display_width(line), 90)
        self.assertNotIn("\ufffd", line)

    def test_title_is_compact_and_complete(self) -> None:
        title = version_module.version_title(sample_info())
        self.assertIn("v9.9.9", title)
        self.assertIn("deadbeef", title)
        self.assertIn("2026-01-02 03:04:05", title)

    def test_as_dict_is_json_serialisable(self) -> None:
        payload = sample_info().as_dict()
        self.assertEqual(payload["schema"], version_module.BUILD_SCHEMA)
        self.assertEqual(json.loads(json.dumps(payload))["commit"], "deadbeef")


class GeneratorTests(unittest.TestCase):
    """tools/make_build_info.py is what build.ps1 actually calls."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="nioh3-buildinfo-")
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.module_path = self.root / "pkg" / "_buildinfo.py"
        self.out_dir = self.root / "out"

    def run_generator(self, *extra: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = make_build_info.main([
                "--commit", "58be86b3164cd181a099c4f5971b563834cca8de",
                "--branch", "master",
                "--built-at", "2026-01-02T03:04:05+08:00",
                "--crypto-exe", str(support.EXE_PATH),
                "--built-from", str(self.root),
                "--module-path", str(self.module_path),
                "--out-dir", str(self.out_dir),
                "--dirty",
                *extra,
            ])
        return code, buffer.getvalue()

    def test_writes_module_json_and_text(self) -> None:
        code, output = self.run_generator()
        self.assertEqual(code, 0)
        self.assertTrue(self.module_path.is_file())
        self.assertIn("34cca8de", output)

        payload = json.loads((self.out_dir / "BUILD-INFO.json").read_text("utf-8"))
        self.assertEqual(payload["commit"], "34cca8de")  # last 8 of the id
        self.assertEqual(payload["commit_full"],
                         "58be86b3164cd181a099c4f5971b563834cca8de")
        self.assertTrue(payload["worktree_dirty"])
        self.assertEqual(payload["built_at"], "2026-01-02T03:04:05+08:00")
        self.assertEqual(payload["crypto_exe"], str(support.EXE_PATH))
        self.assertEqual(payload["language"], payload["language"].strip())
        self.assertTrue(payload["language"])
        self.assertEqual(payload["info_source"], "build")

        text = (self.out_dir / "BUILD-INFO.txt").read_text("utf-8")
        for expected in ("34cca8de", str(self.root), str(support.EXE_PATH),
                         "2026-01-02T03:04:05+08:00", payload["language"]):
            self.assertIn(expected, text)

    def test_generated_module_reproduces_every_fact(self) -> None:
        self.run_generator()
        module = support.install_build_info_module(
            self.module_path.read_text("utf-8")
        )
        self.assertEqual(module.COMMIT, "34cca8de")
        self.assertEqual(module.COMMIT_FULL,
                         "58be86b3164cd181a099c4f5971b563834cca8de")
        self.assertEqual(len(module.COMMIT), 8)
        self.assertTrue(module.DIRTY)
        self.assertEqual(module.BRANCH, "master")
        self.assertIn("CPython", module.LANGUAGE)
        # Importing it into the package must drive the app's banner.
        version_module.reset_cache()
        self.addCleanup(version_module.reset_cache)
        self.addCleanup(support.forget_build_info_module)
        self.assertEqual(version_info(refresh=True).commit, "34cca8de")

    def test_exe_hash_is_recorded(self) -> None:
        self.run_generator()
        module = support.install_build_info_module(
            self.module_path.read_text("utf-8")
        )
        self.assertEqual(module.CRYPTO_EXE_SHA256,
                         make_build_info.sha256_of(support.EXE_PATH))
        self.assertEqual(len(module.CRYPTO_EXE_SHA256), 64)

    def test_short_commit_must_be_the_id_tail(self) -> None:
        full = "12345678" + "b" * 32
        info = make_build_info.collect(commit=full, crypto_exe=support.EXE_PATH)
        self.assertEqual(info.commit, "bbbbbbbb")
        make_build_info.verify_module(self._write_module(info), info)  # must pass

        # A module that agrees with a *prefix* style short commit must be
        # rejected: the project rule is the last 8 characters of the id.
        import dataclasses

        wrong = dataclasses.replace(info, commit="12345678")
        with self.assertRaises(RuntimeError) as caught:
            make_build_info.verify_module(self._write_module(wrong), wrong)
        self.assertIn("后 8 位", str(caught.exception))

    def test_verify_module_detects_template_drift(self) -> None:
        info = make_build_info.collect(commit="a" * 40, crypto_exe=support.EXE_PATH)
        path = self._write_module(info)
        drifted = path.read_text("utf-8").replace("VERSION = '", "VERSION = 'drift-")
        path.write_text(drifted, encoding="utf-8")
        with self.assertRaises(RuntimeError) as caught:
            make_build_info.verify_module(path, info)
        self.assertIn("VERSION", str(caught.exception))

    def test_missing_git_metadata_is_tolerated(self) -> None:
        info = make_build_info.collect(commit=UNKNOWN, crypto_exe=support.EXE_PATH)
        self.assertEqual(info.commit, UNKNOWN)
        make_build_info.verify_module(self._write_module(info), info)

    def _write_module(self, info: BuildInfo) -> Path:
        path = self.root / f"pkg-{abs(hash(info.commit))}" / "_buildinfo.py"
        make_build_info._write_text(path, make_build_info.render_module(info))
        return path

    def test_missing_exe_is_reported_but_not_fatal(self) -> None:
        missing = self.root / "nope.exe"
        buffer, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(buffer), mock.patch("sys.stderr", errors):
            code = make_build_info.main([
                "--commit", "a" * 40,
                "--crypto-exe", str(missing),
                "--module-path", str(self.module_path),
                "--out-dir", str(self.out_dir),
                "--quiet",
            ])
        self.assertEqual(code, 0)
        self.assertIn("unknown", errors.getvalue())
        payload = json.loads((self.out_dir / "BUILD-INFO.json").read_text("utf-8"))
        self.assertEqual(payload["crypto_exe_sha256"], UNKNOWN)

    def test_collect_falls_back_to_git_when_arguments_missing(self) -> None:
        info = make_build_info.collect(crypto_exe=support.EXE_PATH)
        self.assertTrue(info.version)
        self.assertTrue(info.language)
        if info.commit != UNKNOWN:
            self.assertEqual(len(info.commit), 8)

    def test_timestamp_is_iso8601_with_offset(self) -> None:
        stamp = make_build_info.local_timestamp()
        self.assertIn("T", stamp)
        self.assertRegex(stamp, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")

    def test_language_detection_mentions_stdlib(self) -> None:
        language = make_build_info.detect_language()
        self.assertIn("CPython", language)
        self.assertIn("stdlib", language)

    def test_generated_module_is_importable_by_the_package(self) -> None:
        """The default module path must be inside the package directory."""
        self.assertEqual(make_build_info.DEFAULT_MODULE_PATH.parent,
                         Path(version_module.__file__).resolve().parent)
        self.assertEqual(make_build_info.DEFAULT_MODULE_PATH.name, "_buildinfo.py")


class BannerIsPrintedByTheCliTests(unittest.TestCase):
    def test_version_flag_prints_the_banner_and_exits_zero(self) -> None:
        from nioh3_accessory_editor import cli

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            with self.assertRaises(SystemExit) as caught:
                cli.main(["--version"])
        self.assertEqual(caught.exception.code, 0)
        text = buffer.getvalue()
        info = version_info()
        self.assertIn(info.commit, text)
        self.assertIn(info.crypto_exe, text)
        self.assertIn(info.language, text)
        # The banner must not be re-wrapped by argparse's help formatter.
        self.assertIn("commit    :", text)

    def test_version_subcommand_prints_the_banner(self) -> None:
        from nioh3_accessory_editor import cli

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli.main(["version"])
        self.assertEqual(code, 0)
        self.assertIn("来源", buffer.getvalue())

    def test_version_subcommand_json_is_valid(self) -> None:
        from nioh3_accessory_editor import cli

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli.main(["version", "--json"])
        self.assertEqual(code, 0)
        text = buffer.getvalue()
        payload = json.loads(text[text.index("{"):text.rindex("}") + 1])
        self.assertEqual(payload["component"], "Nioh3AccessoryEditor")
        self.assertEqual(payload["commit"], version_info().commit)


class RoutingTests(unittest.TestCase):
    def test_version_is_routable_from_both_entry_points(self) -> None:
        import launch_editor

        from nioh3_accessory_editor import __main__ as package_main

        self.assertIn("version", launch_editor.CLI_COMMANDS)
        self.assertIn("version", package_main.CLI_COMMANDS)

    def test_cli_subcommands_are_all_routable(self) -> None:
        """Every CLI subcommand must be routed to the CLI, not to the GUI."""
        import launch_editor

        from nioh3_accessory_editor import cli

        parser = cli.build_parser()
        subparsers = next(
            action for action in parser._actions
            if action.dest == "command" and hasattr(action, "choices")
        )
        missing = set(subparsers.choices) - set(launch_editor.CLI_COMMANDS)
        self.assertEqual(missing, set(), f"未路由到 CLI 的子命令: {missing}")


if __name__ == "__main__":
    unittest.main()
