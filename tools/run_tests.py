# -*- coding: utf-8 -*-
"""Run the Nioh3AccessoryEditor unit/integration test-suite.

    python tools/run_tests.py               # everything
    python tools/run_tests.py -v            # verbose
    python tools/run_tests.py -p test_records
    python tools/run_tests.py --pure-crypto # include the slow pure-Python tests

Exit code is 0 only when every test passes.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-p", "--pattern", default="test_*.py",
                        help="test module filename pattern")
    parser.add_argument("--pure-crypto", action="store_true",
                        help="run the slow full-file pure-Python crypto tests")
    parser.add_argument("-k", "--keyword", default=None,
                        help="only tests whose id contains this substring")
    args = parser.parse_args()

    if args.pure_crypto:
        os.environ["NIOH3_PURE_CRYPTO_TESTS"] = "1"

    loader = unittest.TestLoader()
    if args.keyword:
        loader.testNamePatterns = [f"*{args.keyword}*"]
    suite = loader.discover(
        start_dir=str(PROJECT_ROOT / "tests"),
        pattern=args.pattern,
        top_level_dir=str(PROJECT_ROOT),
    )
    runner = unittest.TextTestRunner(verbosity=2 if args.verbose else 1)
    started = time.perf_counter()
    result = runner.run(suite)
    elapsed = time.perf_counter() - started
    print(f"\nran {result.testsRun} tests in {elapsed:.1f}s "
          f"({len(result.skipped)} skipped, {len(result.errors)} errors, "
          f"{len(result.failures)} failures)")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
