"""让本进程不被 Windows 的后台节流降速。

窗口失去焦点后 Windows 11 会把进程丢进效率模式（EcoQoS）：挪到能效核、降频。
GPU 上的推理几乎不受影响，但 CPU 那几段会明显变慢——实测解码 +39%、后处理 +67%、
KMBox 发送 +63%，而推理只 +5%，p99 从 6ms 涨到 16ms。对 4ms 一帧的瞄准回路来说，
这既抬高了延迟，也让 20ms 的 KMBox 超时更容易撞上。
"""

from __future__ import annotations

import ctypes
import os

_HIGH_PRIORITY_CLASS = 0x00000080
_PROCESS_POWER_THROTTLING = 4
_PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1
_PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
_TIMER_RESOLUTION_MS = 1
_THREAD_PRIORITY_BELOW_NORMAL = -1


class _PowerThrottlingState(ctypes.Structure):
    _fields_ = [
        ("Version", ctypes.c_uint32),
        ("ControlMask", ctypes.c_uint32),
        ("StateMask", ctypes.c_uint32),
    ]


def keep_running_at_full_speed(*, kernel32=None, winmm=None) -> list[str]:
    """尽力关掉后台节流。返回每一步的结果描述，任何失败都不抛异常。"""
    if os.name != "nt":
        return []
    if kernel32 is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if winmm is None:
        winmm = ctypes.WinDLL("winmm", use_last_error=True)
    _declare_signatures(kernel32)

    notes = [
        _attempt(
            lambda: kernel32.SetPriorityClass(
                kernel32.GetCurrentProcess(), _HIGH_PRIORITY_CLASS
            ),
            "进程优先级已提高",
            "提高进程优先级失败",
        ),
        _attempt(
            lambda: _stop_execution_throttling(kernel32),
            "已退出效率模式（后台也全速运行）",
            "退出效率模式失败",
        ),
        _attempt(
            lambda: winmm.timeBeginPeriod(_TIMER_RESOLUTION_MS) == 0,
            f"系统时钟精度已设为 {_TIMER_RESOLUTION_MS}ms",
            "调整系统时钟精度失败",
        ),
    ]
    return notes


def _declare_signatures(kernel32) -> None:
    """必须声明签名: 不声明的话返回值默认按 c_int 处理, 64 位下 GetCurrentProcess
    的句柄会被截断, 后续调用全部失败——而且是静默失败。"""
    try:
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.SetPriorityClass.restype = ctypes.c_int
        kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.SetProcessInformation.restype = ctypes.c_int
        kernel32.SetProcessInformation.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
    except (AttributeError, TypeError):
        pass


def _stop_execution_throttling(kernel32) -> int:
    state = _PowerThrottlingState(
        Version=_PROCESS_POWER_THROTTLING_CURRENT_VERSION,
        # 点名管「执行速度」这一项, 状态位清零 = 明确要求别降速。
        ControlMask=_PROCESS_POWER_THROTTLING_EXECUTION_SPEED,
        StateMask=0,
    )
    return kernel32.SetProcessInformation(
        kernel32.GetCurrentProcess(),
        _PROCESS_POWER_THROTTLING,
        ctypes.pointer(state),
        ctypes.sizeof(state),
    )


def _attempt(call, success: str, failure: str) -> str:
    """这些都是锦上添花, 系统不给就算了, 绝不能挡住管线启动。"""
    try:
        return success if call() else failure
    except (OSError, AttributeError, ValueError):
        return failure


def run_current_thread_below_normal() -> bool:
    """把调用线程降到「低于普通」优先级。

    预览渲染线程用。整个进程现在跑在 HIGH 优先级上，如果预览线程也跟着 HIGH，
    它就有资格抢瞄准主循环的 CPU——渲染晚 5 毫秒没人看得出来，主循环晚 5 毫秒
    就是准心慢半拍。
    """
    if os.name != "nt":
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # 同样必须声明签名: GetCurrentThread 返回的伪句柄不声明就会被截断。
        kernel32.GetCurrentThread.restype = ctypes.c_void_p
        kernel32.GetCurrentThread.argtypes = []
        kernel32.SetThreadPriority.restype = ctypes.c_int
        kernel32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
        return bool(
            kernel32.SetThreadPriority(
                kernel32.GetCurrentThread(), _THREAD_PRIORITY_BELOW_NORMAL
            )
        )
    except (OSError, AttributeError, ValueError):
        return False
