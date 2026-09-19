"""Entry-point routing tests: arguments must pick CLI vs GUI consistently."""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

import launch_editor

from nioh3_accessory_editor import __main__ as package_main
from nioh3_accessory_editor import cli, ui


class LaunchEditorTests(unittest.TestCase):
    def test_cli_commands_route_to_the_cli(self) -> None:
        for argv in (["list"], ["check"], ["edit", "--help"], ["backup"],
                     ["version"], ["version", "--json"],
                     ["-h"], ["--help"], ["--version"]):
            with mock.patch.object(cli, "main", return_value=7) as cli_main, \
                    mock.patch.object(ui, "main") as gui_main:
                self.assertEqual(launch_editor.main(argv), 7)
            cli_main.assert_called_once_with(argv)
            gui_main.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
