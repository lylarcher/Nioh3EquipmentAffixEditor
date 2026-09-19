"""Allow ``python -m nioh3_accessory_editor`` (GUI) with CLI subcommand support.

Delegates to the same entry logic as ``launch_editor.py`` so both entry points
always agree about which arguments select the CLI.
"""

from __future__ import annotations

import sys

from . import bootstrap

CLI_COMMANDS = frozenset(
    {"list", "check", "edit", "backup", "version", "config",
     "-h", "--help", "--version", "--config", "--python-crypto"}
)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    bootstrap.ensure_once()

    from . import cli, ui  # noqa: PLC0415 - imported after bootstrap

    if args and args[0] in CLI_COMMANDS:
        return cli.main(args)
    return ui.main()


if __name__ == "__main__":
    raise SystemExit(main())
