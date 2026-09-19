"""Entry-point routing tests: arguments must pick CLI vs GUI consistently."""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

import launch_editor

from nioh3_accessory_editor import __main__ as package_main
from nioh3_accessory_editor import bootstrap, cli, ui


def subcommand_names() -> set[str]:
    """Every subcommand the CLI parser actually defines."""
    import argparse

    parser = cli.build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    raise AssertionError("CLI parser defines no subcommands")


class LaunchEditorTests(unittest.TestCase):
    def test_cli_commands_route_to_the_cli(self) -> None:
        for argv in (["list"], ["check"], ["edit", "--help"], ["backup"],
                     ["version"], ["version", "--json"],
                     ["config"], ["config", "--json"], ["config", "--init"],
                     ["--config", "custom.json", "check"], ["--python-crypto", "list"],
                     ["-h"], ["--help"], ["--version"]):
            with mock.patch.object(cli, "main", return_value=7) as cli_main, \
                    mock.patch.object(ui, "main") as gui_main:
                self.assertEqual(launch_editor.main(argv), 7)
            cli_main.assert_called_once_with(argv)
            gui_main.assert_not_called()

    def test_every_subcommand_is_routable(self) -> None:
        """CLI_COMMANDS must keep up with the parser, or a command opens the GUI."""
        self.assertTrue(subcommand_names() <= launch_editor.CLI_COMMANDS,
                        sorted(subcommand_names() - launch_editor.CLI_COMMANDS))

    def test_no_arguments_starts_the_gui(self) -> None:
        with mock.patch.object(ui, "main", return_value=3) as gui_main, \
                mock.patch.object(cli, "main") as cli_main:
            self.assertEqual(launch_editor.main([]), 3)
        gui_main.assert_called_once_with()
        cli_main.assert_not_called()

    def test_unknown_first_argument_starts_the_gui(self) -> None:
        with mock.patch.object(ui, "main", return_value=0) as gui_main:
            launch_editor.main(["--something-else"])
        gui_main.assert_called_once_with()

    def test_help_reaches_the_cli_and_exits_zero(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                launch_editor.main(["--help"])
        self.assertEqual(caught.exception.code, 0)

    def test_side_by_side_files_are_unpacked_before_anything_else(self) -> None:
        with mock.patch.object(bootstrap, "ensure_once") as ensure, \
                mock.patch.object(cli, "main", return_value=0):
            launch_editor.main(["list"])
        ensure.assert_called_once_with()

    def test_unpacking_happens_for_the_gui_too(self) -> None:
        with mock.patch.object(bootstrap, "ensure_once") as ensure, \
                mock.patch.object(ui, "main", return_value=0):
            launch_editor.main([])
        ensure.assert_called_once_with()


class PackageMainTests(unittest.TestCase):
    def test_routing_matches_launch_editor(self) -> None:
        with mock.patch.object(cli, "main", return_value=5) as cli_main:
            self.assertEqual(package_main.main(["check"]), 5)
        cli_main.assert_called_once_with(["check"])

        with mock.patch.object(ui, "main", return_value=0) as gui_main:
            package_main.main([])
        gui_main.assert_called_once_with()

    def test_cli_command_sets_are_identical(self) -> None:
        self.assertEqual(launch_editor.CLI_COMMANDS, package_main.CLI_COMMANDS)

    def test_package_main_unpacks_side_by_side_files(self) -> None:
        with mock.patch.object(bootstrap, "ensure_once") as ensure, \
                mock.patch.object(cli, "main", return_value=0):
            package_main.main(["version"])
        ensure.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
