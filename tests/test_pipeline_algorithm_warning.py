from __future__ import annotations

import unittest

from dataclasses import replace

from rhodes_fast.config import AppConfig, UiConfig, default_config
from rhodes_fast.pipeline import _algorithm_warning_text


class AlgorithmWarningTextTests(unittest.TestCase):
    def _config(self, language: str) -> AppConfig:
        # AppConfig 的 7 个字段是必填的, 只能从 default_config() 改一处出来。
        return replace(default_config(), ui=UiConfig(language=language))

    def test_no_warnings_produces_nothing_to_print(self) -> None:
        self.assertEqual(_algorithm_warning_text(self._config("zh"), []), "")

    def test_each_warning_gets_its_own_marked_line(self) -> None:
        text = _algorithm_warning_text(self._config("zh"), ["算法 a 找不到", "算法 b 找不到"])
        lines = text.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("算法 a 找不到", lines[0])
        self.assertIn("算法 b 找不到", lines[1])
        # 必须显眼: 这行混在一堆启动信息里, 不标出来等于没打印。
        for line in lines:
            self.assertTrue(line.startswith("!!"), line)

    def test_english_console_still_gets_the_algorithm_name(self) -> None:
        text = _algorithm_warning_text(self._config("en"), ["算法 my_kalman 找不到，已回退到 p"])
        self.assertIn("my_kalman", text)


if __name__ == "__main__":
    unittest.main()
