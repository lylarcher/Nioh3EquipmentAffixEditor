# -*- coding: utf-8 -*-
"""Exit-code helper used by build.ps1 to verify its own failure detection.

A build script is only as trustworthy as its ability to notice that a step
failed.  Windows PowerShell 5.1 reports a *stale or zero* ``$LASTEXITCODE`` for
a child whose output was merged through a pipeline, so ``build.ps1`` asks this
helper to fail on purpose and checks that the failure was seen.

    python tools/check_build_gate.py --exit-code 7 --stderr
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exit-code", type=int, default=0,
                        help="exit code to return (0-255)")
    parser.add_argument("--stderr", action="store_true",
                        help="also write to stderr, like a noisy step does")
    parser.add_argument("--lines", type=int, default=1,
                        help="how many stderr lines to emit")
    args = parser.parse_args(argv)

    print(f"check_build_gate: stdout, will exit {args.exit_code}")
    if args.stderr:
        for index in range(max(args.lines, 1)):
            print(f"check_build_gate: stderr line {index}", file=sys.stderr)
    return max(0, min(255, args.exit_code))


if __name__ == "__main__":
    raise SystemExit(main())
