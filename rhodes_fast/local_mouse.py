"""本机鼠标: 用 SendInput 发相对移动, 用 GetAsyncKeyState 读触发键。

接口对齐 KmboxController 用到的那几个 KMBox 客户端方法 (move、isdown_*、close),
控制器的算法路径一行不用改。

SendInput 注入的事件带 LLMHF_INJECTED 标记, 任何装了低级鼠标钩子的程序都看得到。
这是它的固有属性, 界面上照实说; 本模块不做任何绕过。

user32 由构造参数传进来 (生产里是 ctypes.WinDLL("user32")): 单元测试不能真去动
用户的鼠标。
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

INPUT_MOUSE = 0
# 相对移动。带上 MOUSEEVENTF_ABSOLUTE 的话 dx/dy 就成了 0..65535 的屏幕坐标。
MOUSEEVENTF_MOVE = 0x0001
# GetAsyncKeyState 的最高位 = 此刻按着。最低位是「上次查过之后按过」, 不算。
_KEY_DOWN = 0x8000
# 触发键 → 虚拟键码。读的是逻辑按键: 系统里设了左右键互换的话, left 是逻辑左键,
# 这跟游戏看到的一致。
VIRTUAL_KEYS = {"left": 0x01, "right": 0x02, "side1": 0x05, "side2": 0x06}

_ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    # 三个都要声明: union 的大小取最大的那个。只声明 MOUSEINPUT 的话在 32 位上
    # 大小对不上, SendInput 按 cbSize 校验, 对不上直接返回 0。
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _load_user32():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT
    user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
    user32.GetAsyncKeyState.restype = ctypes.c_short
    return user32


class LocalMouseClient:
    def __init__(self, *, user32=None, monitor=None) -> None:
        self._user32 = user32 if user32 is not None else _load_user32()
        # 读手的移动的那个监听 (RawMouseMonitor)。只在关的时候用到。
        self._monitor = monitor

    def move(self, dx: int, dy: int) -> None:
        record = INPUT(type=INPUT_MOUSE, u=_INPUTUNION(mi=MOUSEINPUT(dx, dy, 0, MOUSEEVENTF_MOVE, 0, 0)))
        sent = self._user32.SendInput(1, ctypes.pointer(record), ctypes.sizeof(INPUT))
        if sent != 1:
            # 控制器接住 OSError, 把这一帧记成 (0, 0)。吞掉的话会被记成已经发出,
            # 「扣在途」会减掉一段根本没发生的位移。
            raise OSError(ctypes.get_last_error(), "SendInput 没有发出这次移动")

    def _is_down(self, trigger: str) -> bool:
        return bool(self._user32.GetAsyncKeyState(VIRTUAL_KEYS[trigger]) & _KEY_DOWN)

    def isdown_left(self) -> bool:
        return self._is_down("left")

    def isdown_right(self) -> bool:
        return self._is_down("right")

    def isdown_side1(self) -> bool:
        return self._is_down("side1")

    def isdown_side2(self) -> bool:
        return self._is_down("side2")

    def close(self) -> None:
        monitor, self._monitor = self._monitor, None
        if monitor is not None:
            monitor.stop()


# ---- 手的移动: Raw Input ----
#
# 轨迹要知道手在物理鼠标上移动了多少。KMBox 模式从盒子的监听口读; SendInput
# 模式没有盒子, 改用 Raw Input: 注册一个 message-only 窗口收 WM_INPUT。
# RIDEV_INPUTSINK 让程序不在前台时也收得到 —— 打游戏时前台是游戏。

WM_INPUT = 0x00FF
WM_QUIT = 0x0012
RID_INPUT = 0x10000003
RIM_TYPEMOUSE = 0
RIDEV_REMOVE = 0x00000001
RIDEV_INPUTSINK = 0x00000100
MOUSE_MOVE_ABSOLUTE = 0x0001
_HWND_MESSAGE = -3
_GENERIC_DESKTOP = 0x01
_USAGE_MOUSE = 0x02
_GET_RAW_INPUT_FAILED = 0xFFFFFFFF


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", wintypes.DWORD),
        ("dwSize", wintypes.DWORD),
        ("hDevice", wintypes.HANDLE),
        ("wParam", wintypes.WPARAM),
    ]


class _RAWMOUSEBUTTONS(ctypes.Structure):
    _fields_ = [("usButtonFlags", wintypes.USHORT), ("usButtonData", wintypes.USHORT)]


class _RAWMOUSEBUTTONUNION(ctypes.Union):
    _fields_ = [("ulButtons", wintypes.ULONG), ("buttons", _RAWMOUSEBUTTONS)]


class RAWMOUSE(ctypes.Structure):
    _fields_ = [
        ("usFlags", wintypes.USHORT),
        ("u", _RAWMOUSEBUTTONUNION),
        ("ulRawButtons", wintypes.ULONG),
        ("lLastX", wintypes.LONG),
        ("lLastY", wintypes.LONG),
        ("ulExtraInformation", wintypes.ULONG),
    ]


class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [
        ("MakeCode", wintypes.USHORT),
        ("Flags", wintypes.USHORT),
        ("Reserved", wintypes.USHORT),
        ("VKey", wintypes.USHORT),
        ("Message", wintypes.UINT),
        ("ExtraInformation", wintypes.ULONG),
    ]


class RAWHID(ctypes.Structure):
    _fields_ = [("dwSizeHid", wintypes.DWORD), ("dwCount", wintypes.DWORD), ("bRawData", wintypes.BYTE * 1)]


class _RAWINPUTDATA(ctypes.Union):
    _fields_ = [("mouse", RAWMOUSE), ("keyboard", RAWKEYBOARD), ("hid", RAWHID)]


class RAWINPUT(ctypes.Structure):
    _fields_ = [("header", RAWINPUTHEADER), ("data", _RAWINPUTDATA)]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", wintypes.USHORT),
        ("usUsage", wintypes.USHORT),
        ("dwFlags", wintypes.DWORD),
        ("hwndTarget", wintypes.HWND),
    ]


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        # 直接填 DefWindowProcW 的地址: 这个窗口不处理任何消息, 用不着一个 Python
        # 回调 —— WM_INPUT 在消息循环里就地处理掉了。
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


def _mouse_move(record: RAWINPUT) -> tuple[int, int] | None:
    header = record.header
    if header.dwType != RIM_TYPEMOUSE:
        return None
    # SendInput 注入的原始输入没有设备来源。算进来的话, 程序自己发的移动会被
    # 当成手的移动再算一遍。
    if not header.hDevice:
        return None
    mouse = record.data.mouse
    # 数位板、远程桌面报的是 0..65535 的绝对坐标, 不是位移。
    if mouse.usFlags & MOUSE_MOVE_ABSOLUTE:
        return None
    if mouse.lLastX == 0 and mouse.lLastY == 0:
        return None
    return (mouse.lLastX, mouse.lLastY)


def parse_raw_mouse(buffer: bytes) -> tuple[int, int] | None:
    """GetRawInputData 写出来的一份 RAWINPUT → 手的相对位移, 不算就是 None。"""
    if len(buffer) < ctypes.sizeof(RAWINPUT):
        return None
    return _mouse_move(RAWINPUT.from_buffer_copy(buffer[: ctypes.sizeof(RAWINPUT)]))


def _load_raw_input_api():
    """自己的一份 WinDLL, 不用 ctypes.windll.user32: 在共用的那份上设 argtypes
    会改到同一进程里别的调用方 (pywebview 就在用它)。"""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "RegisterClassW": ((ctypes.POINTER(_WNDCLASSW),), wintypes.ATOM),
        "UnregisterClassW": ((wintypes.LPCWSTR, wintypes.HINSTANCE), wintypes.BOOL),
        "CreateWindowExW": (
            (
                wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
            ),
            wintypes.HWND,
        ),
        "DestroyWindow": ((wintypes.HWND,), wintypes.BOOL),
        "RegisterRawInputDevices": (
            (ctypes.POINTER(RAWINPUTDEVICE), wintypes.UINT, wintypes.UINT), wintypes.BOOL
        ),
        "GetRawInputData": (
            (wintypes.HANDLE, wintypes.UINT, wintypes.LPVOID, ctypes.POINTER(wintypes.UINT), wintypes.UINT),
            wintypes.UINT,
        ),
        "GetMessageW": ((ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT), wintypes.BOOL),
        "DispatchMessageW": ((ctypes.POINTER(wintypes.MSG),), wintypes.LPARAM),
        "DefWindowProcW": ((wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM), wintypes.LPARAM),
        "PostThreadMessageW": ((wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM), wintypes.BOOL),
    }
    for name, (argtypes, restype) in signatures.items():
        function = getattr(user32, name)
        function.argtypes = argtypes
        function.restype = restype
    kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetCurrentThreadId.argtypes = ()
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    return user32, kernel32


class RawMouseMonitor:
    """在自己的线程里收物理鼠标的原始移动, 每收到一次调一次 on_move(dx, dy)。

    on_move 跑在这条线程上; 管线那边是 HandMotion.add, 自己加锁。

    在管线子进程里注册, 不在界面进程里: 一个进程对同一类设备只能有一个接收窗口,
    界面进程里还有 pywebview 的窗口。
    """

    def __init__(self, on_move) -> None:
        self._on_move = on_move
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._ready = threading.Event()
        self._error: str | None = None

    def start(self) -> None:
        """开线程并等它注册完。注册失败抛 OSError —— 调用方决定要不要拦。"""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="raw-mouse", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=2.0):
            self._error = self._error or "Raw Input 线程两秒内没有就绪"
        if self._error is not None:
            self.stop()
            raise OSError(self._error)

    def stop(self) -> None:
        thread, self._thread = self._thread, None
        if thread is None:
            return
        if self._thread_id is not None:
            user32, _ = _load_raw_input_api()
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        thread.join(timeout=2.0)

    def _run(self) -> None:
        try:
            user32, kernel32 = _load_raw_input_api()
        except (AttributeError, OSError) as exc:
            self._fail(f"拿不到 Raw Input 的系统接口：{exc}")
            return
        self._thread_id = kernel32.GetCurrentThreadId()
        instance = kernel32.GetModuleHandleW(None)
        # 类名带上对象 id: 同一进程里停了再开, 上一个类万一没注销掉也不撞名。
        class_name = f"EndfieldRawMouse{id(self):x}"
        window_class = _WNDCLASSW(
            lpfnWndProc=ctypes.cast(user32.DefWindowProcW, ctypes.c_void_p).value,
            hInstance=instance,
            lpszClassName=class_name,
        )
        if not user32.RegisterClassW(ctypes.byref(window_class)):
            self._fail(f"注册窗口类失败 (错误 {ctypes.get_last_error()})")
            return
        window = None
        registered = False
        try:
            window = user32.CreateWindowExW(
                0, class_name, None, 0, 0, 0, 0, 0, wintypes.HWND(_HWND_MESSAGE), None, instance, None
            )
            if not window:
                self._fail(f"建不了接收窗口 (错误 {ctypes.get_last_error()})")
                return
            device = RAWINPUTDEVICE(_GENERIC_DESKTOP, _USAGE_MOUSE, RIDEV_INPUTSINK, window)
            if not user32.RegisterRawInputDevices(ctypes.byref(device), 1, ctypes.sizeof(RAWINPUTDEVICE)):
                self._fail(f"注册 Raw Input 失败 (错误 {ctypes.get_last_error()})")
                return
            registered = True
            self._ready.set()
            self._loop(user32)
        finally:
            if registered:
                removal = RAWINPUTDEVICE(_GENERIC_DESKTOP, _USAGE_MOUSE, RIDEV_REMOVE, None)
                user32.RegisterRawInputDevices(ctypes.byref(removal), 1, ctypes.sizeof(RAWINPUTDEVICE))
            if window:
                user32.DestroyWindow(window)
            user32.UnregisterClassW(class_name, instance)

    def _loop(self, user32) -> None:
        message = wintypes.MSG()
        record = RAWINPUT()
        size = wintypes.UINT()
        header_size = ctypes.sizeof(RAWINPUTHEADER)
        while True:
            result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
            if result == 0 or result == -1:  # WM_QUIT, 或者出错
                return
            if message.message != WM_INPUT:
                user32.DispatchMessageW(ctypes.byref(message))
                continue
            size.value = ctypes.sizeof(record)
            read = user32.GetRawInputData(
                message.lParam, RID_INPUT, ctypes.byref(record), ctypes.byref(size), header_size
            )
            if read != _GET_RAW_INPUT_FAILED:
                moved = _mouse_move(record)
                if moved is not None:
                    self._on_move(*moved)
            # 文档要求 WM_INPUT 交给 DefWindowProc 做清理。直接调它, 不走
            # DispatchMessage —— 那样还要绕一圈窗口过程。
            user32.DefWindowProcW(message.hWnd, message.message, message.wParam, message.lParam)

    def _fail(self, message: str) -> None:
        self._error = message
        self._ready.set()


def check_local_mouse(user32=None) -> None:
    """给「测试输入」用: 确认这两个函数拿得到, 读一次左键。不发任何移动 ——
    测试的时候不能动用户的鼠标。拿不到就抛 OSError。"""
    try:
        user32 = user32 if user32 is not None else _load_user32()
        _send_input, get_key_state = user32.SendInput, user32.GetAsyncKeyState
    except (AttributeError, OSError) as exc:
        raise OSError(f"拿不到 user32 的 SendInput / GetAsyncKeyState：{exc}") from exc
    get_key_state(VIRTUAL_KEYS["left"])
