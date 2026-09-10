from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

from rhodes_fast import __version__


class PackageMetadataTests(unittest.TestCase):
    def test_runtime_version_matches_project_metadata(self) -> None:
        pyproject = Path(__file__).parents[1] / "pyproject.toml"
        with pyproject.open("rb") as handle:
            project_version = tomllib.load(handle)["project"]["version"]

        self.assertEqual(__version__, project_version)


if __name__ == "__main__":
    unittest.main()
