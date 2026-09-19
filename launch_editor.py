"""Launch the Nioh 3 accessory editor GUI, or the CLI when arguments are given.

    python launch_editor.py                 -> GUI
    python launch_editor.py list            -> CLI: list records/affixes
    python launch_editor.py check           -> CLI: read-only integrity check
    python launch_editor.py edit --help     -> CLI: edit a save
    python launch_editor.py backup          -> CLI: plaintext backup
    python launch_editor.py version         -> CLI: build/version information
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nioh3_accessory_editor import cli, ui

#: Any of these as the first argument means "run the CLI, not the GUI".
#: Kept in sync with the CLI subcommands by tests/test_entrypoints.py.
CLI_COMMANDS = frozenset(
    {"list", "check", "edit", "backup", "version", "-h", "--help", "--version"}
)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in CLI_COMMANDS:
        return cli.main(args)
    return ui.main()


if __name__ == "__main__":
    raise SystemExit(main())
