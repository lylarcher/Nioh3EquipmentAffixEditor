"""Parameter-file tests: the config a user edits next to the executable.

``config/editor.json`` is extracted on first run and is meant to be edited by
hand, so the loader has to be strict about nonsense (a typo must be reported, not
silently ignored) while remaining forgiving about the things that legitimately
differ between machines (relative paths, missing optional keys).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nioh3_equipment_affix_editor import config as config_module
from nioh3_equipment_affix_editor.config import (
    CONFIG_SCHEMA,
    ConfigError,
    EditorConfig,
    default_config_document,
    load_config,
    write_default_config,
)


class DefaultDocumentTests(unittest.TestCase):
    def test_document_is_valid_json_and_loads(self) -> None:
        document = default_config_document(version="0.1.0", commit="deadbeef")
        with tempfile.TemporaryDirectory(prefix="nioh3-config-") as temp:
            root = Path(temp)
            target = root / "config" / "editor.json"
            target.parent.mkdir()
            target.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

            loaded = load_config(target)
        self.assertEqual(loaded.source, target)
        self.assertTrue(loaded.default_dry_run)
        self.assertTrue(loaded.default_verify)
        self.assertEqual(loaded.problems, [])

    def test_document_declares_its_own_schema_and_provenance(self) -> None:
        document = default_config_document(version="0.1.0", commit="deadbeef")
        self.assertEqual(document["schema"], CONFIG_SCHEMA)
        self.assertEqual(document["_generated_for"],
                         {"version": "0.1.0", "commit": "deadbeef"})
        self.assertIn("_readme", document)

    def test_every_documented_key_is_a_dataclass_field(self) -> None:
        """The template must not advertise keys the loader would reject."""
        document = default_config_document()
        fields = set(EditorConfig.__dataclass_fields__) | {"schema"}
        unknown = {key for key in document if not key.startswith("_")} - fields
        self.assertEqual(unknown, set())
        # ... and the reverse: no setting is invisible to the user.
        self.assertEqual(fields - set(document) - {"source", "problems"}, set())


class LoadTests(unittest.TestCase):
    def write(self, document) -> Path:
        temp = tempfile.TemporaryDirectory(prefix="nioh3-config-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        target = root / "editor.json"
        if isinstance(document, str):
            target.write_text(document, encoding="utf-8")
        else:
            target.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        self.root = root
        return target

    def test_absent_default_file_uses_built_in_defaults(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nioh3-config-") as temp:
            # No config/editor.json in the application root: all defaults.
            loaded = load_config(root=Path(temp))
        self.assertIsNone(loaded.source)
        self.assertTrue(loaded.default_dry_run)

    def test_explicitly_named_file_must_exist(self) -> None:
        """--config pointing at a typo must fail, not silently use defaults."""
        with self.assertRaises(ConfigError) as caught:
            load_config(Path("does-not-exist.json"))
        self.assertIn("找不到配置文件", str(caught.exception))

    def test_schema_is_required(self) -> None:
        target = self.write({"default_dry_run": True})
        with self.assertRaises(ConfigError) as caught:
            load_config(target)
        self.assertIn("schema", str(caught.exception))

    def test_wrong_schema_is_rejected(self) -> None:
        target = self.write({"schema": "something-else/v9"})
        with self.assertRaises(ConfigError) as caught:
            load_config(target)
        self.assertIn(CONFIG_SCHEMA, str(caught.exception))

    def test_unknown_key_is_reported(self) -> None:
        target = self.write({"schema": CONFIG_SCHEMA, "dry_run": True})
        with self.assertRaises(ConfigError) as caught:
            load_config(target)
        self.assertIn("dry_run", str(caught.exception))

    def test_comment_keys_are_ignored(self) -> None:
        target = self.write({"schema": CONFIG_SCHEMA, "_note": "hello",
                             "default_dry_run": False})
        self.assertFalse(load_config(target).default_dry_run)

    def test_invalid_json_is_reported_with_the_path(self) -> None:
        target = self.write("{not json")
        with self.assertRaises(ConfigError) as caught:
            load_config(target)
        self.assertIn("editor.json", str(caught.exception))

    def test_boolean_must_be_a_boolean(self) -> None:
        target = self.write({"schema": CONFIG_SCHEMA, "default_dry_run": "yes"})
        with self.assertRaises(ConfigError) as caught:
            load_config(target)
        self.assertIn("default_dry_run", str(caught.exception))

    def test_integer_must_be_an_integer(self) -> None:
        target = self.write({"schema": CONFIG_SCHEMA, "save_index": "2"})
        with self.assertRaises(ConfigError) as caught:
            load_config(target)
        self.assertIn("save_index", str(caught.exception))

    def test_relative_paths_resolve_against_the_program_directory(self) -> None:
        """Documented behaviour: relative to the exe, so the folder stays portable."""
        target = self.write({
            "schema": CONFIG_SCHEMA,
            "save_root": "saves",
            "backup_root": "backups",
            "crypto_exe": "bin/custom.exe",
        })
        loaded = load_config(target)
        root = config_module.paths.application_root()
        self.assertEqual(loaded.resolved_backup_root(), root / "backups")
        self.assertEqual(loaded.resolved_crypto_exe(), root / "bin" / "custom.exe")
        self.assertEqual(loaded.resolved_save_root(), root / "saves")

    def test_absolute_paths_are_kept(self) -> None:
        absolute = str(Path("C:/somewhere/tool.exe"))
        target = self.write({"schema": CONFIG_SCHEMA, "crypto_exe": absolute})
        self.assertEqual(load_config(target).resolved_crypto_exe(), Path(absolute))

    def test_null_crypto_exe_means_the_built_in_backend(self) -> None:
        target = self.write({"schema": CONFIG_SCHEMA, "crypto_exe": None})
        loaded = load_config(target)
        self.assertIsNone(loaded.resolved_crypto_exe())
        backend = loaded.crypto_backend()
        self.assertIsNone(backend.executable)
        self.assertTrue(backend.prefer_python)

    def test_null_paths_fall_back_to_the_bundled_defaults(self) -> None:
        target = self.write({"schema": CONFIG_SCHEMA, "backup_root": None,
                             "save_root": None})
        loaded = load_config(target)
        self.assertEqual(loaded.resolved_backup_root(),
                         config_module.paths.default_state_root())
        self.assertIsNone(loaded.resolved_save_root())

    def test_blank_path_is_reported_instead_of_guessed(self) -> None:
        target = self.write({"schema": CONFIG_SCHEMA, "backup_root": ""})
        with self.assertRaises(ConfigError) as caught:
            load_config(target)
        self.assertIn("backup_root", str(caught.exception))

    def test_default_paths_are_used_when_nothing_is_configured(self) -> None:
        loaded = EditorConfig()
        self.assertEqual(loaded.resolved_crypto_exe(),
                         config_module.paths.default_crypto_exe())
        self.assertEqual(loaded.resolved_backup_root(),
                         config_module.paths.default_state_root())
        self.assertIsNone(loaded.resolved_save_root())

    def test_backend_uses_the_bundled_helper_by_default(self) -> None:
        backend = EditorConfig().crypto_backend()
        self.assertEqual(backend.executable, config_module.paths.default_crypto_exe())
        self.assertFalse(backend.prefer_python)
        self.assertEqual(backend.note, "")

    def test_backend_reports_a_missing_configured_helper(self) -> None:
        loaded = EditorConfig(crypto_exe="bin/nope.exe")
        backend = loaded.crypto_backend()
        self.assertEqual(backend.executable, config_module.paths.default_crypto_exe())
        self.assertIn("不存在", backend.note)

    def test_backend_degrades_to_pure_python_when_nothing_is_left(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nioh3-config-") as temp:
            # No bundled helper reachable: pretend the app directory is empty.
            with mock.patch.object(config_module.paths, "application_root",
                                   lambda: Path(temp)), \
                    mock.patch.object(config_module.paths, "default_crypto_exe",
                                      lambda: Path(temp) / "bin" / "absent.exe"):
                backend = EditorConfig().crypto_backend()
        self.assertIsNone(backend.executable)
        self.assertTrue(backend.prefer_python)
        self.assertIn("纯 Python", backend.note)


class OverrideTests(unittest.TestCase):
    def test_overrides_replace_values(self) -> None:
        base = EditorConfig(account=1, save_index=2)
        updated = base.with_overrides(account=7, save_index=5)
        self.assertEqual(updated.account, 7)
        self.assertEqual(updated.save_index, 5)
        self.assertEqual(base.account, 1)

    def test_absent_command_line_option_does_not_clear_the_file_value(self) -> None:
        """argparse defaults are None; that must mean "unset", not "erase"."""
        base = EditorConfig(account=1, save_index=2)
        self.assertEqual(base.with_overrides(account=None, save_index=None), base)

    def test_unknown_overrides_are_ignored(self) -> None:
        base = EditorConfig()
        self.assertEqual(base.with_overrides(nonsense=1), base)

    def test_schema_key_is_not_treated_as_a_field(self) -> None:
        base = EditorConfig()
        self.assertEqual(base.with_overrides(schema="nope"), base)


class RoundTripTests(unittest.TestCase):
    def test_write_then_load(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nioh3-config-") as temp:
            target = Path(temp) / "config" / "editor.json"
            written = write_default_config(target, version="0.1.0",
                                           commit="deadbeef")
            self.assertEqual(written, target)
            self.assertTrue(target.is_file())
            loaded = load_config(target)
            self.assertEqual(loaded.source, target)
            self.assertEqual(loaded.problems, [])

    def test_write_refuses_to_clobber_without_force(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nioh3-config-") as temp:
            target = Path(temp) / "editor.json"
            write_default_config(target)
            target.write_text('{"schema": "user"}', encoding="utf-8")

            with self.assertRaises(ConfigError):
                write_default_config(target)
            self.assertEqual(target.read_text(encoding="utf-8"), '{"schema": "user"}')

            write_default_config(target, overwrite=True)
            self.assertIn(CONFIG_SCHEMA, target.read_text(encoding="utf-8"))

    def test_written_file_is_utf8_without_bom_and_ends_with_newline(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nioh3-config-") as temp:
            target = Path(temp) / "editor.json"
            write_default_config(target, version="0.1.0", commit="deadbeef")
            raw = target.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertTrue(raw.endswith(b"\n"))
        self.assertNotIn(b"\r\n", raw)  # LF everywhere, like the payload copy
        self.assertIn("参数配置".encode("utf-8"), raw)
        json.loads(raw.decode("utf-8"))  # and it is valid JSON


class DescribeTests(unittest.TestCase):
    def test_describe_reports_every_setting(self) -> None:
        lines = EditorConfig().describe()
        text = "\n".join(lines)
        for label in ("配置文件", "存档目录", "账号过滤", "槽位过滤", "备份目录",
                      "加密组件", "GUI 演练", "GUI 校验"):
            self.assertIn(label, text)

    def test_describe_reflects_a_loaded_file(self) -> None:
        loaded = EditorConfig(source=Path("D:/app/config/editor.json"), account=42,
                              save_index=3, prefer_python_crypto=True)
        text = "\n".join(loaded.describe())
        self.assertIn("42", text)
        self.assertIn("3", text)
        self.assertIn("纯 Python", text)

    def test_to_dict_is_json_serialisable(self) -> None:
        payload = EditorConfig(account=5).to_dict()
        self.assertEqual(payload["schema"], CONFIG_SCHEMA)
        self.assertEqual(payload["account"], 5)
        json.dumps(payload)


class CliIntegrationTests(unittest.TestCase):
    def test_config_command_prints_the_effective_settings(self) -> None:
        import contextlib
        import io

        from nioh3_equipment_affix_editor import cli

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(["config"])
        self.assertEqual(code, 0)
        self.assertIn("配置", buffer.getvalue())

    def test_config_command_json_is_parseable(self) -> None:
        import contextlib
        import io

        from nioh3_equipment_affix_editor import cli

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(["config", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["schema"], CONFIG_SCHEMA)

    def test_config_init_writes_the_default_file(self) -> None:
        import contextlib
        import io

        from nioh3_equipment_affix_editor import cli

        with tempfile.TemporaryDirectory(prefix="nioh3-config-") as temp:
            target = Path(temp) / "editor.json"
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cli.main(["config", "--init", str(target)])
            self.assertEqual(code, 0)
            self.assertTrue(target.is_file())
            self.assertIn(CONFIG_SCHEMA, target.read_text(encoding="utf-8"))

    def test_config_init_refuses_to_clobber(self) -> None:
        import contextlib
        import io

        from nioh3_equipment_affix_editor import cli

        with tempfile.TemporaryDirectory(prefix="nioh3-config-") as temp:
            target = Path(temp) / "editor.json"
            target.write_text("{}", encoding="utf-8")
            buffer, errors = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(errors):
                code = cli.main(["config", "--init", str(target)])
            self.assertEqual(code, 1)
            self.assertEqual(target.read_text(encoding="utf-8"), "{}")

    def test_a_broken_config_file_fails_loudly(self) -> None:
        import contextlib
        import io

        from nioh3_equipment_affix_editor import cli

        with tempfile.TemporaryDirectory(prefix="nioh3-config-") as temp:
            target = Path(temp) / "editor.json"
            target.write_text('{"schema": "wrong/v1"}', encoding="utf-8")
            errors = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(errors):
                code = cli.main(["--config", str(target), "config"])
            self.assertEqual(code, 1)
            self.assertIn(CONFIG_SCHEMA, errors.getvalue())

    def test_command_line_filters_win_over_the_file(self) -> None:
        import argparse

        from nioh3_equipment_affix_editor import cli

        with tempfile.TemporaryDirectory(prefix="nioh3-config-") as temp:
            target = Path(temp) / "editor.json"
            target.write_text(json.dumps({
                "schema": CONFIG_SCHEMA, "account": 11, "save_index": 1,
            }), encoding="utf-8")
            args = argparse.Namespace(config=str(target), account=99, save_index=None)
            loaded = cli._config(args)
            # --account wins over the file ...
            self.assertEqual(loaded.account, 99)
            # ... while an omitted --save-index leaves the file's value alone.
            self.assertEqual(loaded.save_index, 1)
            # The file is read once per run.
            self.assertIs(cli._config(args), loaded)


if __name__ == "__main__":
    unittest.main()
