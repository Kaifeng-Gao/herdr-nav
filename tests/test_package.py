"""Package metadata tests."""

import importlib.metadata
import unittest

import herdrnav


class PackageTests(unittest.TestCase):
    def test_version_matches_distribution_metadata(self) -> None:
        self.assertEqual(herdrnav.__version__, "0.1.0")
        self.assertEqual(importlib.metadata.version("herdr-nav"), "0.1.0")


if __name__ == "__main__":
    unittest.main()
