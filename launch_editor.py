"""Launch the Nioh 3 accessory editor GUI, or the CLI when arguments are given.

    Nioh3AccessoryEditor.exe               -> GUI
    Nioh3AccessoryEditor.exe list          -> CLI: list records/affixes
    Nioh3AccessoryEditor.exe check         -> CLI: read-only integrity check
    Nioh3AccessoryEditor.exe edit --help   -> CLI: edit a save
    Nioh3AccessoryEditor.exe backup        -> CLI: plaintext backup
    Nioh3AccessoryEditor.exe config        -> CLI: show/create the config file
    Nioh3AccessoryEditor.exe version       -> CLI: build/version information

Also runnable from a checkout: ``python launch_editor.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from nioh3_accessory_editor import bootstrap

#: Any of these as the first argument means "run the CLI, not the GUI".
#: Kept in sync with the CLI subcommands by tests/test_entrypoints.py.
CLI_COMMANDS = frozenset(
    {"list", "check", "edit", "backup", "restore", "version", "config",
     "-h", "--help", "--version", "--config", "--python-crypto"}
)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    # A frozen build carries its side-by-side files inside the executable;
    # unpack them next to it before importing anything that reads them.
    bootstrap.ensure_once()

    from nioh3_accessory_editor import cli  # noqa: PLC0415 - after bootstrap

    if args and args[0] in CLI_COMMANDS:
        return cli.main(args)

    from nioh3_accessory_editor import ui  # noqa: PLC0415 - GUI is optional

    return ui.main()


if __name__ == "__main__":
    raise SystemExit(main())
