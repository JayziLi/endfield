from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from rhodes_fast.algorithm_library import (
    InstalledAlgorithm,
    LibraryError,
    read_registry,
    rename,
    uninstall,
    write_registry,
)


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._folder = tempfile.TemporaryDirectory()
        self.directory = Path(self._folder.name)

    def tearDown(self) -> None:
        self._folder.cleanup()

    def _seed(self) -> InstalledAlgorithm:
        entry = InstalledAlgorithm(
            name="my_kalman",
            display_name="卡尔曼（老王版）",
            author="老王",
            source_file="my_kalman.py",
            imported_at="2026-09-11 20:00",
        )
        (self.directory / entry.source_file).write_text("# stub\n", encoding="utf-8")
        write_registry(self.directory, {entry.name: entry})
        return entry

    def test_an_empty_directory_reads_as_an_empty_registry(self) -> None:
        self.assertEqual(read_registry(self.directory), {})

    def test_round_trip(self) -> None:
        entry = self._seed()
        self.assertEqual(read_registry(self.directory), {"my_kalman": entry})
        self.assertEqual(read_registry(self.directory)["my_kalman"].author, "老王")

    def test_a_corrupt_registry_reads_as_empty_rather_than_raising(self) -> None:
        # 注册表坏了不该让整个界面起不来。
        (self.directory / "installed.json").write_text("{oops", encoding="utf-8")
        self.assertEqual(read_registry(self.directory), {})

    def test_a_row_missing_fields_is_skipped_not_fatal(self) -> None:
        payload = {"format": 1, "algorithms": [{"name": "broken"}]}
        (self.directory / "installed.json").write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(read_registry(self.directory), {})

    def test_an_old_row_without_author_remains_usable(self) -> None:
        payload = {
            "format": 1,
            "algorithms": [
                {
                    "name": "legacy",
                    "display_name": "旧算法",
                    "source_file": "legacy.py",
                    "imported_at": "",
                }
            ],
        }
        (self.directory / "installed.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        self.assertEqual(read_registry(self.directory)["legacy"].author, "未署名")

    def test_rename_changes_the_display_name_only(self) -> None:
        self._seed()
        rename(self.directory, "my_kalman", "卡尔曼 v2")
        entry = read_registry(self.directory)["my_kalman"]
        self.assertEqual(entry.display_name, "卡尔曼 v2")
        # 标识不能动: 改了 NAME, 别人发来的调校就对不上。
        self.assertEqual(entry.name, "my_kalman")
        self.assertEqual(entry.source_file, "my_kalman.py")

    def test_rename_refuses_a_blank_display_name(self) -> None:
        self._seed()
        with self.assertRaises(LibraryError):
            rename(self.directory, "my_kalman", "   ")

    def test_rename_refuses_a_builtin(self) -> None:
        with self.assertRaises(LibraryError) as caught:
            rename(self.directory, "p", "我的比例控制")
        self.assertIn("内置", str(caught.exception))

    def test_uninstall_removes_both_the_file_and_the_row(self) -> None:
        entry = self._seed()
        uninstall(self.directory, "my_kalman")
        self.assertEqual(read_registry(self.directory), {})
        self.assertFalse((self.directory / entry.source_file).exists())

    def test_uninstall_refuses_a_builtin(self) -> None:
        with self.assertRaises(LibraryError) as caught:
            uninstall(self.directory, "p")
        self.assertIn("内置", str(caught.exception))

    def test_uninstall_survives_a_file_that_is_already_gone(self) -> None:
        entry = self._seed()
        (self.directory / entry.source_file).unlink()
        uninstall(self.directory, "my_kalman")
        self.assertEqual(read_registry(self.directory), {})


if __name__ == "__main__":
    unittest.main()
