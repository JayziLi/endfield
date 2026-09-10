from __future__ import annotations

import ctypes
import unittest
from unittest.mock import Mock, patch

from rhodes_fast.process_priority import keep_running_at_full_speed


def _ok() -> Mock:
    """每个 Win32 调用都返回非零 = 成功。"""
    api = Mock()
    api.GetCurrentProcess.return_value = 1234
    api.SetPriorityClass.return_value = 1
    api.SetProcessInformation.return_value = 1
    return api


class KeepRunningAtFullSpeedTests(unittest.TestCase):
    def test_reports_each_step_it_managed_to_apply(self) -> None:
        notes = keep_running_at_full_speed(kernel32=_ok(), winmm=_ok())
        self.assertEqual(len(notes), 3)

    def test_asks_windows_to_stop_throttling_execution_speed(self) -> None:
        kernel32 = _ok()
        keep_running_at_full_speed(kernel32=kernel32, winmm=_ok())

        state = kernel32.SetProcessInformation.call_args.args[2].contents
        # ControlMask 点名 EXECUTION_SPEED, StateMask 清零 = 别给我降速
        self.assertEqual(state.ControlMask, 1)
        self.assertEqual(state.StateMask, 0)

    def test_declares_the_process_handle_as_pointer_sized(self) -> None:
        # 不声明签名的话, ctypes 默认按 c_int 处理返回值, 64 位下 GetCurrentProcess
        # 的句柄会被截断, 后面每个调用都静默失败。mock 测试照样全绿, 只有真机暴露。
        kernel32 = _ok()
        keep_running_at_full_speed(kernel32=kernel32, winmm=_ok())

        self.assertIs(kernel32.GetCurrentProcess.restype, ctypes.c_void_p)
        self.assertIs(kernel32.SetPriorityClass.argtypes[0], ctypes.c_void_p)
        self.assertIs(kernel32.SetProcessInformation.argtypes[0], ctypes.c_void_p)

    def test_a_refused_call_is_reported_without_stopping_the_others(self) -> None:
        kernel32 = _ok()
        kernel32.SetPriorityClass.return_value = 0
        notes = keep_running_at_full_speed(kernel32=kernel32, winmm=_ok())

        self.assertTrue(any("优先级" in note for note in notes))
        kernel32.SetProcessInformation.assert_called_once()

    def test_an_api_that_raises_never_takes_the_pipeline_down(self) -> None:
        # 这是启动路径上的锦上添花, 任何失败都不该阻止管线跑起来。
        kernel32 = _ok()
        kernel32.SetProcessInformation.side_effect = OSError("nope")

        notes = keep_running_at_full_speed(kernel32=kernel32, winmm=_ok())
        self.assertTrue(notes)

    def test_does_nothing_off_windows(self) -> None:
        with patch("rhodes_fast.process_priority.os.name", "posix"):
            self.assertEqual(keep_running_at_full_speed(), [])


if __name__ == "__main__":
    unittest.main()
