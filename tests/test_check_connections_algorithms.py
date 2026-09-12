from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from rhodes_fast.aim_algorithms import set_installed_algorithms
from rhodes_fast.algorithm_library import install
from rhodes_fast.config import load_config
from rhodes_fast.pipeline import check_connections

_CONFIG = Path(__file__).parents[1] / "config.example.toml"

ALGORITHM = '''
from rhodes_fast.aim_algorithms import Param


class Smoke:
    NAME = "smoke"
    DISPLAY_NAME = "自检用"
    PARAMS = (Param("gain", 1.0, 0.0, 3.0, "整体增益"),)

    def __init__(self, params):
        self.gain = params["gain"]

    def reset(self):
        pass

    def compute(self, observation):
        return observation.error_x * self.gain, observation.error_y * self.gain
'''


class CheckConnectionsAlgorithmTests(unittest.TestCase):
    def tearDown(self) -> None:
        set_installed_algorithms({})

    def _run(self, algorithm: str, algorithms_dir: Path | None) -> str:
        frame = np.zeros((320, 320, 3), dtype=np.uint8)
        source = Mock()
        source.error = None
        source.wait_next.return_value = SimpleNamespace(frame=frame)

        config = load_config(_CONFIG, validate_model=False)
        config = replace(
            config,
            kmbox=replace(config.kmbox, enabled=False),
            aim_profile_1=replace(config.aim_profile_1, algorithm=algorithm),
        )
        printed = io.StringIO()
        with redirect_stdout(printed):
            with patch("rhodes_fast.pipeline.create_source", return_value=source):
                check_connections(config, algorithms_dir=algorithms_dir)
        return printed.getvalue()

    def test_the_self_check_does_not_cry_wolf_about_an_installed_algorithm(self) -> None:
        # --check 不加载算法库的话, 用户只要用了自己装的算法, 自检就会报
        # 「算法 smoke 找不到，已回退到 p」——纯属虚惊, 而自检正是验收清单里的一步。
        with tempfile.TemporaryDirectory() as folder:
            incoming = Path(folder) / "smoke.py"
            incoming.write_text(ALGORITHM, encoding="utf-8")
            library = Path(folder) / "algorithms"
            install(library, incoming)
            output = self._run("smoke", library)
        self.assertNotIn("!!", output)

    def test_an_algorithm_that_really_is_missing_still_gets_reported(self) -> None:
        # 反过来也要成立: 配置指着一个根本不存在的算法, 自检必须响亮地说出来。
        with tempfile.TemporaryDirectory() as folder:
            library = Path(folder) / "algorithms"
            output = self._run("no_such_algorithm", library)
        self.assertIn("!!", output)
        self.assertIn("no_such_algorithm", output)


if __name__ == "__main__":
    unittest.main()
