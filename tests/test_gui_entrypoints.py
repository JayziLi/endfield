from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

from rhodes_fast import __main__ as cli


class GuiEntrypointTests(unittest.TestCase):
    def _launch(self, flag: str):
        config_path = Path("custom-settings.txt")
        with (
            mock.patch.object(sys, "argv", ["endfield", flag, "--config", str(config_path)]),
            mock.patch.object(cli, "configure_console_output"),
            mock.patch.object(cli, "load_config", return_value=mock.Mock()),
            mock.patch.object(cli, "ensure_default_model", return_value=Path("model.onnx")),
            mock.patch("rhodes_fast.gui_web.app.main") as new_gui,
            mock.patch("rhodes_fast.gui.run_gui") as classic_gui,
        ):
            cli.main()
            return new_gui.call_args, classic_gui.call_args

    def test_gui_flag_opens_new_interface_with_requested_config(self) -> None:
        new_gui, classic_gui = self._launch("--gui")
        self.assertEqual(new_gui.args, (Path("custom-settings.txt"),))
        self.assertIsNone(classic_gui)

    def test_classic_flag_keeps_the_previous_interface_available(self) -> None:
        new_gui, classic_gui = self._launch("--gui-classic")
        self.assertIsNone(new_gui)
        self.assertEqual(classic_gui.args, (Path("custom-settings.txt"),))
        self.assertEqual(classic_gui.kwargs, {"auto_start": False})


if __name__ == "__main__":
    unittest.main()
