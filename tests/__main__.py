"""Run the test suite while rejecting an empty discovery result."""

import sys
import unittest
from pathlib import Path


def main() -> int:
    suite = unittest.defaultTestLoader.discover(Path(__file__).parent)
    if suite.countTestCases() == 0:
        print("No tests were selected.", file=sys.stderr)
        return 1
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
