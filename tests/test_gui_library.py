from __future__ import annotations

import pathlib
import tempfile
import unittest

from rhodes_fast.aim_algorithms import set_installed_algorithms
from rhodes_fast.algorithm_library import install, rename, uninstall
from rhodes_fast.gui import RhodesFastGui

ALGORITHM = '''
__author__ = "阿明"

from rhodes_fast.aim_algorithms import Param


class Fresh:
    NAME = "fresh"
    DISPLAY_NAME = "刚导入的"
    PARAMS = (Param("gain", 1.0, 0.0, 3.0, "整体增益"),)

    def __init__(self, params):
        self.gain = params["gain"]

    def reset(self):
        pass

    def compute(self, observation):
        return observation.error_x * self.gain, observation.error_y * self.gain
'''


class JustImportedTests(unittest.TestCase):
    """刚导入、还没重启时, 列表里必须已经看得到它。

    算法要重启才能在控制方案里选, 但那是「能不能用」; 「装没装上」得当场看见,
    否则用户点了导入、提示说成功了, 回头列表里一行没变, 只能以为坏了。
    """

    def setUp(self) -> None:
        self.app = RhodesFastGui(pathlib.Path("settings.example.txt"))
        self.app.root.update_idletasks()
        self._folder = tempfile.TemporaryDirectory()
        incoming = pathlib.Path(self._folder.name) / "fresh.py"
        incoming.write_text(ALGORITHM, encoding="utf-8")
        self.app.algorithms_dir = pathlib.Path(self._folder.name) / "algorithms"
        install(self.app.algorithms_dir, incoming)
        self.app._refresh_library()

    def tearDown(self) -> None:
        self.app.root.destroy()
        self._folder.cleanup()

    def test_a_just_imported_algorithm_shows_up_before_any_restart(self) -> None:
        rows = {row[1]: row for row in self.app._library_rows()}
        self.assertIn("fresh", rows)
        self.assertEqual(rows["fresh"][0], "刚导入的")
        self.assertEqual(rows["fresh"][2], "阿明")
        self.assertEqual(rows["fresh"][3], "fresh.py")
        self.assertEqual(rows["fresh"][5], "已导入")

    def test_the_builtins_are_still_all_there_alongside_it(self) -> None:
        names = [row[1] for row in self.app._library_rows()]
        for builtin in ("p", "pd", "feedforward", "inflight", "inflight_ff"):
            self.assertIn(builtin, names)

    def test_the_tree_shows_it_too(self) -> None:
        self.assertIn("fresh", self.app.library_tree.get_children())

    def test_it_can_be_renamed_and_deleted(self) -> None:
        self.app.library_tree.selection_set("fresh")
        self.app._library_selection_changed()
        self.assertEqual(str(self.app.library_rename_button["state"]), "normal")
        self.assertEqual(str(self.app.library_delete_button["state"]), "normal")


class DropdownFollowsLibraryTests(unittest.TestCase):
    """装完 / 改名 / 删掉之后, 控制方案的下拉框要跟着动。

    下拉框存的是显示名, 而改名改的正是显示名。重载后不按标识把显示名写回去,
    存的那个名字就对不上任何一个选项——之后每次读表单都会静默失败。
    """

    def setUp(self) -> None:
        self.app = RhodesFastGui(pathlib.Path("settings.example.txt"))
        self.app.root.update_idletasks()
        self._folder = tempfile.TemporaryDirectory()
        self.incoming = pathlib.Path(self._folder.name) / "fresh.py"
        self.incoming.write_text(ALGORITHM, encoding="utf-8")
        self.app.algorithms_dir = pathlib.Path(self._folder.name) / "algorithms"

    def tearDown(self) -> None:
        self.app.root.destroy()
        self._folder.cleanup()
        set_installed_algorithms({})

    def _labels(self) -> list[str]:
        return list(self.app.algorithm_combos[0]["values"])

    def test_a_newly_installed_algorithm_appears_in_the_dropdown(self) -> None:
        self.assertNotIn("刚导入的", self._labels())
        install(self.app.algorithms_dir, self.incoming)
        self.app._reload_algorithm_library()
        self.assertIn("刚导入的", self._labels())

    def test_renaming_keeps_the_selection_pointing_at_the_same_algorithm(self) -> None:
        install(self.app.algorithms_dir, self.incoming)
        self.app._reload_algorithm_library()
        self.app.profile_algorithm[1].set("刚导入的")
        rename(self.app.algorithms_dir, "fresh", "换了个名")
        self.app._reload_algorithm_library()
        self.assertEqual(self.app.profile_algorithm[1].get(), "换了个名")
        self.assertEqual(self.app._read_form().aim_profiles[1].algorithm, "fresh")

    def test_deleting_the_algorithm_in_use_falls_back_to_p(self) -> None:
        install(self.app.algorithms_dir, self.incoming)
        self.app._reload_algorithm_library()
        self.app.profile_algorithm[1].set("刚导入的")
        uninstall(self.app.algorithms_dir, "fresh")
        self.app._reload_algorithm_library()
        self.assertEqual(self.app._read_form().aim_profiles[1].algorithm, "p")

    def test_the_other_profile_is_left_where_it_was(self) -> None:
        before = self.app.profile_algorithm[0].get()
        install(self.app.algorithms_dir, self.incoming)
        self.app._reload_algorithm_library()
        self.assertEqual(self.app.profile_algorithm[0].get(), before)


class LibraryRowsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = RhodesFastGui(pathlib.Path("settings.example.txt"))
        self.app.root.update_idletasks()

    def tearDown(self) -> None:
        self.app.root.destroy()

    def test_every_builtin_is_listed_and_marked_as_builtin(self) -> None:
        rows = self.app._library_rows()
        builtin = [row for row in rows if row[5] == "内置"]
        self.assertGreaterEqual(len(builtin), 5)
        self.assertIn("p", [row[1] for row in builtin])

    def test_builtins_have_no_source_file_or_date(self) -> None:
        for row in self.app._library_rows():
            if row[5] == "内置":
                self.assertEqual(row[2], "Endfield")
                self.assertEqual(row[3], "—")
                self.assertEqual(row[4], "—")

    def test_the_tree_shows_exactly_those_rows(self) -> None:
        self.assertEqual(
            len(self.app.library_tree.get_children()), len(self.app._library_rows())
        )

    def test_rename_and_delete_start_disabled_with_nothing_selected(self) -> None:
        self.assertEqual(str(self.app.library_rename_button["state"]), "disabled")
        self.assertEqual(str(self.app.library_delete_button["state"]), "disabled")

    def test_selecting_a_builtin_keeps_rename_and_delete_disabled(self) -> None:
        # 内置算法随程序分发, 改名和删除都必须点不动。
        for item in self.app.library_tree.get_children():
            if self.app.library_tree.item(item, "values")[5] == "内置":
                self.app.library_tree.selection_set(item)
                self.app._library_selection_changed()
                break
        else:
            self.fail("列表里没有内置算法")
        self.assertEqual(str(self.app.library_rename_button["state"]), "disabled")
        self.assertEqual(str(self.app.library_delete_button["state"]), "disabled")


if __name__ == "__main__":
    unittest.main()
