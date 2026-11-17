"""Offline safety regressions; synthetic market data, UI boundaries and ledgers."""
from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MODULES = (
    "tools.test_broker_guards",
    "tools.test_strategy_integrity",
    "tools.test_runtime_safety",
    "tools.test_execution_reliability",
)


def main():
    loader = unittest.defaultTestLoader
    suite = loader.loadTestsFromNames(MODULES)
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful():
        print(captured.getvalue())
    print("Offline safety regressions: {} run, {} failures, {} errors, {} skipped".format(
        result.testsRun, len(result.failures), len(result.errors), len(result.skipped)))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
