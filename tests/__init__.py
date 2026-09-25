"""Test-suite package for Nioh 3 Equipment Affix Editor.

Run everything with::

    python tools/run_tests.py

Run a single module with::

    python -m unittest tests.test_records -v

Environment flags
-----------------
``NIOH3_PURE_CRYPTO_TESTS=1``
    Also run the full-file pure-Python crypto round trip (~70 s).  The default
    suite uses the bundled reference executable, which performs the same
    transform in ~0.4 s.
"""
