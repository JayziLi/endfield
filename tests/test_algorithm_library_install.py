from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from rhodes_fast.algorithm_library import (
    Candidate,
    DuplicateAlgorithm,
    LibraryError,
    inspect_candidate,
    install,
    load_installed,
    read_registry,
    rename,
)

GOOD = '''
__author__ = "老王"

from rhodes_fast.aim_algorithms import Param


class MyKalman:
    NAME = "my_kalman"
    DISPLAY_NAME = "卡尔曼（老王版）"
    PARAMS = (Param("q", 0.1, 0.0, 1.0, "过程噪声"),)

    def __init__(self, params):
        self.q = params["q"]

    def reset(self):
        pass

    def compute(self, observation):
        return observation.error_x * self.q, observation.error_y * self.q
'''

SIDE_EFFECT = '''
raise RuntimeError("这个文件一跑起来就炸")


class Boom:
    NAME = "boom"
    DISPLAY_NAME = "会炸的"
    PARAMS = ()

    def __init__(self, params):
        pass

    def reset(self):
        pass

    def compute(self, observation):
        return 0.0, 0.0
'''

NO_CLASS = '''
NAME = "not_a_class"
'''

BAD_SIGNATURE = '''
class WrongShape:
    NAME = "wrong_shape"
    DISPLAY_NAME = "签名不对"
    PARAMS = ()

    def __init__(self, params):
        pass

    def reset(self):
        pass

    def compute(self, error_x, error_y):
        return 0.0, 0.0
'''

COLLIDES_WITH_BUILTIN = '''
class Impostor:
    NAME = "p"
    DISPLAY_NAME = "假冒比例控制"
    PARAMS = ()

    def __init__(self, params):
        pass

    def reset(self):
        pass

    def compute(self, observation):
        return 0.0, 0.0
'''


IMPORTS_A_BUILTIN = '''
from rhodes_fast.aim_algorithms.builtin import Proportional


class Mine:
    NAME = "mine"
    DISPLAY_NAME = "我自己的"
    PARAMS = ()

    def __init__(self, params):
        self.fallback = Proportional({})

    def reset(self):
        self.fallback.reset()

    def compute(self, observation):
        return self.fallback.compute(observation)
'''

HUMAN_BUILTIN_COLLISION = GOOD.replace('NAME = "my_kalman"', 'NAME = "windmouse"')
BAD_CONSTRUCTOR = GOOD.replace('def __init__(self, params):', 'def __init__(self):')
BAD_PARAMS = GOOD.replace(
    'PARAMS = (Param("q", 0.1, 0.0, 1.0, "过程噪声"),)',
    'PARAMS = ("not-a-param",)',
)
BAD_RESET = GOOD.replace('def reset(self):', 'def reset(self, required):')
BAD_COMPUTE_EXTRA = GOOD.replace(
    'def compute(self, observation):',
    'def compute(self, observation, required):',
)
MISSING_RESET = GOOD.replace('    def reset(self):\n        pass\n\n', '')
MISSING_COMPUTE = GOOD.replace(
    '    def compute(self, observation):\n'
    '        return observation.error_x * self.q, observation.error_y * self.q\n',
    '',
)


class LibraryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._library = tempfile.TemporaryDirectory()
        self._incoming = tempfile.TemporaryDirectory()
        self.directory = Path(self._library.name)
        self.incoming = Path(self._incoming.name)

    def tearDown(self) -> None:
        self._library.cleanup()
        self._incoming.cleanup()

    def _drop(self, name: str, source: str) -> Path:
        path = self.incoming / name
        path.write_text(source, encoding="utf-8")
        return path


class InspectTests(LibraryTestCase):
    def test_inspect_reports_what_the_confirmation_box_needs(self) -> None:
        candidate = inspect_candidate(self._drop("my_kalman.py", GOOD))
        self.assertIsInstance(candidate, Candidate)
        self.assertEqual(candidate.name, "my_kalman")
        self.assertEqual(candidate.display_name, "卡尔曼（老王版）")
        self.assertEqual(candidate.author, "老王")
        self.assertGreater(candidate.size_bytes, 0)
        self.assertIn("class MyKalman", candidate.source)

    def test_inspect_does_not_execute_the_file(self) -> None:
        # 这个文件在模块层就 raise。能读出信息就证明它没有被执行过。
        candidate = inspect_candidate(self._drop("boom.py", SIDE_EFFECT))
        self.assertEqual(candidate.name, "boom")

    def test_an_unsigned_file_still_inspects(self) -> None:
        source = GOOD.replace('__author__ = "老王"', "")
        self.assertEqual(inspect_candidate(self._drop("a.py", source)).author, "未署名")

    def test_a_file_that_is_not_python_is_rejected_before_anything_else(self) -> None:
        with self.assertRaises(LibraryError):
            inspect_candidate(self._drop("broken.py", "def (((("))

    def test_a_missing_file_is_an_error(self) -> None:
        with self.assertRaises(LibraryError):
            inspect_candidate(self.incoming / "nope.py")


class InstallTests(LibraryTestCase):
    def test_a_good_algorithm_lands_in_the_library_and_the_registry(self) -> None:
        entry = install(
            self.directory,
            self._drop("my_kalman.py", GOOD),
            now=datetime(2026, 9, 11, 20, 0),
        )
        self.assertEqual(entry.name, "my_kalman")
        self.assertEqual(entry.display_name, "卡尔曼（老王版）")
        self.assertEqual(entry.imported_at, "2026-09-11 20:00")
        self.assertTrue((self.directory / entry.source_file).is_file())
        self.assertIn("my_kalman", read_registry(self.directory))

    def test_a_file_that_blows_up_on_import_is_deleted_again(self) -> None:
        with self.assertRaises(LibraryError):
            install(self.directory, self._drop("boom.py", SIDE_EFFECT))
        self.assertEqual(list(self.directory.glob("*.py")), [])
        self.assertEqual(read_registry(self.directory), {})

    def test_a_file_with_no_algorithm_class_is_refused(self) -> None:
        with self.assertRaises(LibraryError):
            install(self.directory, self._drop("nope.py", NO_CLASS))

    def test_a_wrong_compute_signature_is_refused_and_says_so(self) -> None:
        with self.assertRaises(LibraryError) as caught:
            install(self.directory, self._drop("wrong.py", BAD_SIGNATURE))
        self.assertIn("compute", str(caught.exception))
        self.assertEqual(list(self.directory.glob("*.py")), [])

    def test_a_name_that_collides_with_a_builtin_is_refused(self) -> None:
        with self.assertRaises(LibraryError) as caught:
            install(self.directory, self._drop("impostor.py", COLLIDES_WITH_BUILTIN))
        self.assertIn("内置", str(caught.exception))

    def test_a_name_that_collides_with_a_human_builtin_is_refused(self) -> None:
        with self.assertRaises(LibraryError) as caught:
            install(self.directory, self._drop("impostor.py", HUMAN_BUILTIN_COLLISION))
        self.assertIn("内置", str(caught.exception))

    def test_a_constructor_without_params_is_refused(self) -> None:
        with self.assertRaises(LibraryError) as caught:
            install(self.directory, self._drop("bad_constructor.py", BAD_CONSTRUCTOR))
        self.assertIn("__init__", str(caught.exception))

    def test_malformed_param_specs_are_refused(self) -> None:
        with self.assertRaises(LibraryError) as caught:
            install(self.directory, self._drop("bad_params.py", BAD_PARAMS))
        self.assertIn("PARAMS", str(caught.exception))

    def test_a_reset_with_required_arguments_is_refused(self) -> None:
        with self.assertRaises(LibraryError) as caught:
            install(self.directory, self._drop("bad_reset.py", BAD_RESET))
        self.assertIn("reset", str(caught.exception))

    def test_a_compute_with_extra_required_arguments_is_refused(self) -> None:
        with self.assertRaises(LibraryError) as caught:
            install(self.directory, self._drop("bad_compute.py", BAD_COMPUTE_EXTRA))
        self.assertIn("compute", str(caught.exception))

    def test_a_missing_reset_is_refused_and_staging_is_cleaned_up(self) -> None:
        with self.assertRaises(LibraryError) as caught:
            install(self.directory, self._drop("missing_reset.py", MISSING_RESET))
        self.assertIn("reset", str(caught.exception))
        self.assertEqual(list(self.directory.glob("*.py")), [])

    def test_a_missing_compute_is_refused_and_staging_is_cleaned_up(self) -> None:
        with self.assertRaises(LibraryError) as caught:
            install(self.directory, self._drop("missing_compute.py", MISSING_COMPUTE))
        self.assertIn("compute", str(caught.exception))
        self.assertEqual(list(self.directory.glob("*.py")), [])

    def test_a_duplicate_name_is_reported_not_silently_overwritten(self) -> None:
        install(self.directory, self._drop("my_kalman.py", GOOD))
        with self.assertRaises(DuplicateAlgorithm) as caught:
            install(self.directory, self._drop("my_kalman.py", GOOD))
        self.assertEqual(caught.exception.name, "my_kalman")
        self.assertEqual(caught.exception.existing_file, "my_kalman.py")

    def test_an_algorithm_that_reuses_a_builtin_still_installs(self) -> None:
        # 拿内置算法当基线来比较或兜底是很自然的写法。_validate 按定义顺序扫
        # vars(module), 先撞上 import 进来的那个类, 于是这种文件一概装不上——
        # 而且报错倒打作者一耙, 说他源码里的 NAME 和加载出来的对不上。
        entry = install(self.directory, self._drop("mine.py", IMPORTS_A_BUILTIN))
        self.assertEqual(entry.name, "mine")
        self.assertEqual(entry.display_name, "我自己的")

    def test_two_files_whose_names_sanitize_alike_do_not_clobber_each_other(self) -> None:
        # algo.py 和 algo!.py 清洗后都叫 algo.py。第二次安装会静默覆盖第一份
        # 源码, 注册表却留着两行指向同一个文件——第一个算法就这么没了。
        install(self.directory, self._drop("algo.py", GOOD))
        second = GOOD.replace('NAME = "my_kalman"', 'NAME = "other"').replace(
            'DISPLAY_NAME = "卡尔曼（老王版）"', 'DISPLAY_NAME = "另一个"'
        )
        install(self.directory, self._drop("algo!.py", second))
        algorithms, warnings = load_installed(self.directory)
        self.assertEqual(warnings, [])
        self.assertEqual(sorted(algorithms), ["my_kalman", "other"])

    def test_replacing_on_purpose_keeps_the_name_the_user_gave_it(self) -> None:
        install(self.directory, self._drop("my_kalman.py", GOOD))
        rename(self.directory, "my_kalman", "我改过的名字")
        install(self.directory, self._drop("my_kalman.py", GOOD), replace_existing=True)
        self.assertEqual(read_registry(self.directory)["my_kalman"].display_name, "我改过的名字")

    def test_a_broken_replacement_keeps_the_working_algorithm(self) -> None:
        original = install(self.directory, self._drop("my_kalman.py", GOOD))
        broken = GOOD + '\nraise RuntimeError("替换版本加载失败")\n'

        with self.assertRaises(LibraryError):
            install(
                self.directory,
                self._drop("replacement.py", broken),
                replace_existing=True,
            )

        self.assertEqual((self.directory / original.source_file).read_text(encoding="utf-8"), GOOD)
        algorithms, warnings = load_installed(self.directory)
        self.assertEqual(warnings, [])
        self.assertIn("my_kalman", algorithms)


class LoadTests(LibraryTestCase):
    def test_installed_algorithms_load_back_under_their_renamed_display_name(self) -> None:
        install(self.directory, self._drop("my_kalman.py", GOOD))
        rename(self.directory, "my_kalman", "卡尔曼 v2")
        algorithms, warnings = load_installed(self.directory)
        self.assertEqual(warnings, [])
        self.assertIn("my_kalman", algorithms)
        self.assertEqual(algorithms["my_kalman"].DISPLAY_NAME, "卡尔曼 v2")
        # 标识不变: 别人发来的调校认的是 NAME。
        self.assertEqual(algorithms["my_kalman"].NAME, "my_kalman")

    def test_a_registry_row_whose_file_vanished_warns_instead_of_crashing(self) -> None:
        entry = install(self.directory, self._drop("my_kalman.py", GOOD))
        (self.directory / entry.source_file).unlink()
        algorithms, warnings = load_installed(self.directory)
        self.assertEqual(algorithms, {})
        self.assertEqual(len(warnings), 1)
        self.assertIn("卡尔曼", warnings[0])

    def test_an_empty_library_loads_nothing_without_complaining(self) -> None:
        self.assertEqual(load_installed(self.directory), ({}, []))


if __name__ == "__main__":
    unittest.main()
