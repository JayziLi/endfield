from __future__ import annotations

import io
import unittest

from rhodes_fast.console import configure_console_output


class ConsoleOutputTests(unittest.TestCase):
    def test_reconfigures_a_legacy_windows_stream_for_unicode(self) -> None:
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="cp1252")

        configure_console_output(stream)
        stream.write("模型已就绪")
        stream.flush()

        self.assertEqual(stream.encoding.lower().replace("-", ""), "utf8")
        self.assertEqual(buffer.getvalue(), "模型已就绪".encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
