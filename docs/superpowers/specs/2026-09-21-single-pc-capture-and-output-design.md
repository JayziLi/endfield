# 单机模式：本机采集 + 移动输出设计

> 面向实施者：不需要参与过本次讨论，照本文即可动手。

**日期**：2026-09-21
**状态**：已实现（2026-09-21），实测记录见文末

## 1. 背景与目标

现在的链路是**双机**的：主机把画面用 UDP 或 OBS 发过来，副机推理，然后通过 KMBox Net 移动鼠标。

**目标**：让同一台电脑既跑游戏又跑推理。拆成**互相独立的两部分**，任意组合：

| | 现有 | 新增 |
|---|---|---|
| **采集**（画面从哪来） | UDP 视频流 / UDP JPEG / OBS | **本机屏幕**（Windows 自带的 DXGI 桌面复制，或 WGC） |
| **移动**（鼠标移动由谁发） | KMBox | **本机 SendInput** |

**主要适配 KMBox**：单机 + KMBox 是主场景，SendInput 是新增的一个选项。两部分之间没有依赖：单机采集配 KMBox、单机采集配 SendInput、UDP 配 SendInput 都可以。

推理、控制算法、死区/平滑/限幅、预览、轨迹一律不动。

## 2. 现有代码里的接缝

两个口子都已经在，这次只往里各加一个实现：

- **采集**：`pipeline.py` 的 `FrameSource` 协议（`error` / `fps` / `start` / `stop` / `wait_next`），UDP 和 OBS 各是一个实现，由 `create_source(config)` 按 `input.mode` 选。新增的本机采集源是第三个实现，产出同样的 `FrameSnapshot`。
- **移动**：`KmboxController` 对 KMBox 客户端只用四样东西：`move` / `enc_move`（发位移）、`isdown_{left,right,side1,side2}`（触发键）、`monitor_start` + `monitor.add_callback`（监听口读手的移动）、`close`。新增一个接口相同的本机客户端，控制器的算法路径一行不动。

## 3. 采集部分

### 3.1 库的选择：DXcam

同类项目的做法：sunone_aimbot_cpp 默认用 DXGI 桌面复制，另有 WinRT（即 Windows.Graphics.Capture，下称 WGC）作为备选；Python 这边普遍用 DXcam 或它的分支 BetterCam。

选 **DXcam 0.3.x**：

- 同一个库里两个后端都有：`dxcam.create(backend="dxgi" | "winrt")`，都是 Windows 自带的采集接口。
- 官方有 CPython 3.10–3.14 的 wheel，覆盖本项目的 3.11–3.13。
- 自带基准：240fps 的画面平均抓到 239fps（mss 76，D3DShot 118）。
- 支持 `region=(left, top, right, bottom)` 只取一块。

**不装 `dxcam[cv2]` 这个 extra**：本项目依赖的是 `opencv-python-headless`，再装一份 `opencv-python` 会两份 cv2 打架。只装 `dxcam[winrt]`。DXcam 的颜色转换后端（`processor_backend`）用 `"cv2"` 还是 `"numpy"`，在实测阶段（§8）量了再定。

### 3.2 抓哪一块

**所选显示器的正中央**，宽高默认 320×320（和现在 UDP 收到的画面一样大）。按物理像素算：DXcam 报的显示器分辨率就是物理像素，这台机器缩放 150% 也不影响。

```
left = (screen_w - width) // 2
top  = (screen_h - height) // 2
region = (left, top, left + width, top + height)
```

采集区比显示器还大时，启动就报错并说清楚是哪个数不对，不裁剪凑数。

这个做法假设游戏准心在屏幕中央，也就是游戏全屏或无边框铺满那块屏。窗口化且没居中的游戏不在本次范围内。

### 3.3 `DesktopSource`

新文件 `rhodes_fast/desktop_source.py`，实现 `FrameSource`，结构照抄 `UdpSource`：`threading.Condition` 保护最新一帧，一个守护线程负责抓。

- **只留最新一帧**，不排队。这和现有两个来源的原则一致。
- **创建 DXcam 的函数由构造参数传进来**（生产里是 `dxcam.create`）。和 `Popups` 接收 `create_window` 是同一个理由：测试不用装 DXcam，也不用真抓屏。
- **`dxcam` 延迟导入**。没装的时候不能让整个程序起不来：错误写进 `source.error`，内容是「本机屏幕采集需要 dxcam：pip install -e .[local]」。
- **颜色**：`output_color="BGR"`，和 UDP、OBS 两路一致，检测器不用改。
- **抓帧循环**：线程里自己调 `grab(region=..., new_frame_only=True)`，返回 `None`（画面没更新）就歇 0.5 ms 再抓。**不用** DXcam 的 `start()` + `get_latest_frame()`：那一套按固定 `target_fps` 定时取帧，和显示器出帧的时刻不同步。实测 P50 多 2.4 ms 以上，见文末实测记录。
- **时间戳**：DXGI 的 `LastPresentTime` 是 QPC 计数，和 `perf_counter` 同一个时钟。DXcam 只在 `start()` 那套里公开它，`grab` 路径要读内部的 `_duplicator.latest_frame_time`；读不到、为 0、在未来或者早于一秒以前，都当作不可信。
  - 可信时 `first_packet_at` = 出帧时刻，`assembly_ms` = 出帧到开始抓（UDP 那边同一个位置是「等分片到齐」），`decode_ms` = 抓这一下本身。
  - 不可信时 `first_packet_at` = 开始抓的时刻，`assembly_ms` = 0。
  - **只信 DXGI 的**：WGC 的实测大多过不了检查，过了的也有老到 65 ms 的。
- **出错重连**：显示模式切换、锁屏、UAC 弹窗都会让 DXGI 复制失效。线程捕获异常，写进 `error`，等 0.5 秒，然后释放并重建 camera，和 `UdpSource._run` 的做法一样。
- **`label`**：`本机屏幕 DXGI · 显示器 0 · 320x320`（WGC 时写 `WGC`）。

### 3.4 已知限制：自己的窗口会被抓进去

两个后端抓的都是**合成之后的整块屏幕**（DXCam 的 WGC 后端也只接受显示器编号，不按窗口抓）。主窗口、放大预览窗口如果盖在被采集那块屏的正中央，就会被当成游戏画面送去识别。

处理方式：采集设置旁边放一行提示，让用户把这些窗口放到别处或另一块屏上。**不用** `SetWindowDisplayAffinity` 把自己的窗口从采集里排除：那会让这些窗口在所有截图和录屏里都消失，副作用大于好处。

## 4. 移动部分

### 4.1 配置：加一个「输出方式」，`kmbox.enabled` 不动

```toml
[mouse]
output = "kmbox"      # "kmbox" | "sendinput"
```

- 默认 `"kmbox"`。老的 `settings.txt` 和预设里没有这一节，读进来就是现在的行为。
- `kmbox.enabled` **含义不变**：只管 KMBox。选 KMBox 但没勾「启用」，就是现在的「只识别不移动」。
- 选 SendInput 就是启用。它没有「设备连不上」这回事，而且只在按住触发键时才动，用不着单独的开关。

之所以不改成 `output = kmbox | sendinput | none` 三选一：那样要迁移 `kmbox.enabled`，这个字段在 17 个文件里出现了 48 次，包括预设校验和状态灯，风险远大于收益。

### 4.2 `LocalMouseClient`

新文件 `rhodes_fast/local_mouse.py`。接口对齐控制器用到的那几个 KMBox 客户端方法：

| 方法 | 实现 |
|---|---|
| `move(dx, dy)` | `SendInput`，一个 `INPUT_MOUSE`，`dwFlags = MOUSEEVENTF_MOVE`（相对移动）。返回值为 0 时抛 `OSError(ctypes.get_last_error())`：控制器已经在接 `OSError`，会把这一帧记成 `(0, 0)`，而不是记成已经发出 |
| `isdown_left()` 等四个 | `GetAsyncKeyState` 的最高位。`left = VK_LBUTTON (0x01)`、`right = VK_RBUTTON (0x02)`、`side1 = VK_XBUTTON1 (0x05)`、`side2 = VK_XBUTTON2 (0x06)` |
| `close()` | 停掉 §4.4 的 Raw Input 监听 |

- **user32 函数由构造参数传进来**（生产里是 `ctypes.WinDLL("user32", use_last_error=True)`）。测试用假对象，检查打包出去的 `INPUT` 结构和按键映射，不会真去动鼠标。
- **`INPUT` 结构按 64 位布局声明**：`type` 后面是 union，union 里包含 `MOUSEINPUT` / `KEYBDINPUT` / `HARDWAREINPUT`，`cbSize` 传 `ctypes.sizeof(INPUT)`（64 位下是 40）。只声明 `MOUSEINPUT` 的话大小不对，`SendInput` 会直接返回 0。测试里断言这个大小。
- `GetAsyncKeyState` 读的是**逻辑按键**。系统里设了「左右键互换」的话，这里的 left 是逻辑左键，这和游戏看到的一致。

### 4.3 控制器怎么接

`KmboxController` 只改 `connect()` 和发位移那一句。**类名这次不改**：改名会波及所有引用它的地方，和本功能无关。

```python
def connect(self) -> None:
    if self.output == "sendinput":
        self._client = self._open_local_mouse()   # 连同 §4.4 的 Raw Input 监听
        return
    if not self.device_config.enabled:
        return
    ...                                           # 现有 KMBox 路径不变

def _send_method(self):
    if self.output != "sendinput" and self.device_config.encrypted:
        return self._client.enc_move
    return self._client.move
```

`move_toward` 里现在的 `move = self._client.enc_move if self.device_config.encrypted else self._client.move` 改成 `move = self._send_method()`。原因是 `encrypted` 是 KMBox 的配置，选 SendInput 时它不能再去决定调哪个方法。发位移的方法每次现取，不在 `connect` 里定死：现有测试和热路径上都有直接换 `_client` 的用法，定死的话换了客户端还在调旧的。

`output` 从 `config.mouse.output` 通过构造参数传进来。`pipeline.py` 里两处构造 `KmboxController` 的地方（运行和 `--check`）都要传。

### 4.4 手的移动：Raw Input

轨迹功能要知道「手在物理鼠标上移动了多少」。KMBox 模式从监听口读，SendInput 模式没有监听口，改用 Raw Input：

- 在 `local_mouse.py` 里新增 `RawMouseMonitor`。它在自己的线程里建一个 message-only 窗口（父窗口 `HWND_MESSAGE`），用 `RegisterRawInputDevices` 注册鼠标（usage page `0x01`，usage `0x02`，`RIDEV_INPUTSINK`：程序不在前台也能收到），然后跑 `GetMessageW` 循环。
- 收到 `WM_INPUT` 时用 `GetRawInputData` 取数据。只累加相对移动（`usFlags` 的 `MOUSE_MOVE_ABSOLUTE` 位为 0），读 `lLastX` / `lLastY`。
- **跳过程序自己注入的事件**：SendInput 产生的原始输入没有设备来源，`RAWINPUTHEADER.hDevice` 为 0。**这一条必须在实机上确认**（§8）。如果不成立，也不会导致轨迹出错：`trail.py` 的 `hand_includes_commands` 本来就会判断「手的移动里含不含程序指令」，并按判断结果计算。
- 累加进现有的 `HandMotion`。给它加一个 `add(dx, dy)`，现有的 `on_report` 改成调用它。锁和取走的逻辑不变。
- 停止时，向窗口所在线程 `PostThreadMessageW(WM_QUIT)`，注销（`RIDEV_REMOVE`），然后 join 线程。
- Raw Input 在**管线子进程**里注册，不在界面进程里，不会影响 pywebview 窗口收鼠标消息。

### 4.5 必须写进界面的一条：SendInput 能被识别出来

微软文档写明，通过 `SendInput` 注入的事件带 `LLMHF_INJECTED` 标记，任何安装了低级鼠标钩子的程序都能看到。这是 SendInput 的固有属性，所以在界面上**说清楚**：选中 SendInput 时，下面显示一行提示。**不做任何绕过**：不用外设驱动冒充硬件，也不清除注入标记。

## 5. 横幅、连接测试、状态灯

### 5.1 管线横幅

`输入：… | KMBox：host:port` 改为：

```
输入：{source_label} | 移动：KMBox {host}:{port}
输入：{source_label} | 移动：SendInput
```

英文同理：`Input: … | Output: KMBox host:port` / `Output: SendInput`。

`「模型已就绪，正在连接画面输入和 KMBox...」` 改成 `「…和移动输出...」`。状态灯认的是前缀「模型已就绪」，所以这一句改了也不影响状态灯。

### 5.2 连接测试（`--check`）

- 本机屏幕：抓一帧，打印 `本机屏幕 正常：画面=320x320 · 显示器=2560x1440 · 后端=dxgi`（英文 `Desktop OK: frame=320x320 …`），失败打印 `本机屏幕 连接失败：…`。显示器分辨率一起打出来，用户可以据此确认选中的是不是想要的那块屏。
- SendInput：不真发位移（测试时不能动用户的鼠标），只检查 `user32` 里这几个函数能拿到，并读一次按键状态。打印 `SendInput 正常` / `SendInput OK`。
- 如果 Raw Input 能注册成功，一并打印一行；失败只警告，不算测试失败，因为它只影响轨迹。

### 5.3 状态灯（`gui_web/lamps.py`）

- 视频流灯：加上「本机屏幕 正常 / 连接失败」两条规则，写法照抄 UDP、OBS 的。
- 第三盏灯的**标题**按输出方式显示「KMBox」或「SendInput」。内部 id `kmbox` 不改。
- 横幅规则的正则从 `KMBox：` 改认 `移动：`（英文 `Output: `）。
- `lamp_updates` 的 `kmbox_enabled` 参数改名 `output_enabled`，含义是「移动输出是否启用」：`output == "sendinput" or kmbox.enabled`。`Api._kmbox_enabled()` 同步改名为 `_output_enabled()`。
- 加一条 `SendInput 正常` → online「就绪」。

## 6. 配置、预设、界面

### 6.1 配置（`config.py`）

```python
@dataclass(frozen=True, slots=True)
class DesktopConfig:
    backend: str = "dxgi"      # "dxgi" | "winrt"
    monitor: int = 0           # DXCam 的 output_idx
    width: int = 320
    height: int = 320

@dataclass(frozen=True, slots=True)
class MouseConfig:
    output: str = "kmbox"      # "kmbox" | "sendinput"
```

- `AppConfig` 在末尾加上 `desktop: DesktopConfig = field(default_factory=DesktopConfig)` 和 `mouse: MouseConfig = field(default_factory=MouseConfig)`。有默认值，现有测试里直接构造 `AppConfig` 的地方不用改。
- `_read_config` 的 `section_types` 加这两节。
- `input.mode` 的合法值加 `"desktop"`。
- 校验：`backend` 取值、`monitor >= 0`、宽高 > 0、`output` 取值。
- `form_state_to_config` 用的是 `replace(base, …)`，旧界面保存时不认识的字段会原样保留。要加一条测试钉住这一点。

### 6.2 预设（`presets.py`）

- `_INPUT_MODES` 加 `"desktop"`。
- 预设快照里加 `desktop: (backend, monitor, width, height)` 和 `mouse: (output,)`。
- **老预设里没有这两节时按默认值读**，不报错。现有预设文件不能因为这次升级打不开。

### 6.3 界面映射（`gui_core/state.py`）

```python
INPUT_MODES = {..., "本机屏幕": "desktop"}
DESKTOP_BACKENDS = {"DXGI 桌面复制": "dxgi", "WGC（Windows.Graphics.Capture）": "winrt"}
MOUSE_OUTPUTS = {"KMBox": "kmbox", "本机 SendInput": "sendinput"}
```

`Labels` 加上 `desktop_backend` 和 `mouse_output` 两个字段。表单状态加上 `desktop_backend` / `desktop_monitor` / `desktop_width` / `desktop_height` / `mouse_output`，双向转换照抄现有字段的写法。「（推荐）」这个后缀要等实测（§8）定下默认值以后再挂到对应的选项上。

### 6.4 WebView 界面（01 运行设置屏）

- **画面输入**面板：下拉框多一项「本机屏幕」。选中时显示 `#desktop-panel`，内容是采集方式（下拉）、显示器编号（数字，0 起）、宽、高，以及 §3.4 那行提示。UDP 和 OBS 两块面板照旧按选项显示或隐藏，隐藏用 `[hidden]`，设过 `display` 的 class 要加守卫。
- **KMBox 面板改名「移动输出」**：顶部是「输出方式」下拉框，现有的 KMBox 字段（启用、地址、端口、UUID）收进 `#kmbox-fields`，只在选 KMBox 时显示。选 SendInput 时显示 §4.5 那行提示。
- 运行中不上锁，跟 UDP / OBS 那几项一样：它们都只在启动时读一次（实施时按现有行为走，原稿写的「上锁」与现状不符）。

### 6.5 旧的 tkinter 界面

**不加新控件**，只保证不出错：

- 下拉框里会出现「本机屏幕」，因为 `INPUT_MODES` 是共用的（`test_gui_core_state.py` 有 `assertIs` 钉住两个界面用的是同一份）。选中时把 UDP、OBS 两块面板都藏起来（`gui.py` 里现在按 `obs_websocket` 切面板的那几行），参数使用 `settings.txt` 里的值。
- 保存时 `[desktop]` 和 `[mouse]` 原样保留（见 §6.1）。

## 7. 依赖

```toml
[project.optional-dependencies]
local = [
  "dxcam[winrt]>=0.3,<0.4",
]
```

放在可选依赖里，只用副机的人不需要装。SendInput 和 Raw Input 只用标准库 `ctypes`，不加依赖。

## 8. 实机测量（决定默认值，写回本文）

在这台机器上做（2560×1440，缩放 150%）。脚本放在会话临时目录，结果写回本文的「实测记录」一节。

1. **DXGI 对 WGC**：同样抓中央 320×320，各跑 10 秒，记录帧率、`grab` 耗时的 P50/P95/P99 和 CPU 占用。哪个好就用哪个做默认，并把「（推荐）」挂过去。
2. **轮询 `grab` 对 `start()`**：同上，比较「画面更新 → 拿到数组」的延迟和 CPU 占用（§3.3）。
3. **颜色转换后端**：`cv2` 对 `numpy`，量 `grab` 耗时。
4. **Raw Input 过滤**：`LocalMouseClient.move(100, 0)` 发一次，同时手动推一下鼠标，确认监听只收到手的那一下。如果注入的事件也有设备句柄，就在本文写明，依靠 §4.4 的兜底。
5. **SendInput 发的计数**：打开一个用 Raw Input 的程序（管线自己的 `RawMouseMonitor` 关掉过滤后就是一个），确认 `move(dx, dy)` 到达时就是 `dx, dy`，没有被指针加速改写。
6. **整条管线**：`--pipeline-benchmark N` 在「本机屏幕 + KMBox 禁用」下跑一次，和 UDP 输入的历史数据对比「采集」那一段。

## 9. 测试（单元，TDD）

| 测试 | 钉住什么 |
|---|---|
| 采集区是显示器正中央（2560×1440 → `(1120, 560, 1440, 880)`） | 算错了准心就不在画面中心，所有瞄准都偏 |
| 采集区比显示器大时报错，不裁剪 | 悄悄裁成别的尺寸的话，模型输入尺寸就不对 |
| `grab` 返回 `None` 不发布新帧 | 否则同一帧会被当成新帧推理两次，算法按两帧的 dt 算 |
| 没装 dxcam 时写进 `error`，不抛 | 否则整个管线起不来，而错误信息只是 ImportError |
| camera 抛异常后会重建 | 锁屏或 UAC 之后画面永久停住 |
| `stop()` 会 join 线程并释放 camera | DXGI 复制是独占资源，不释放的话下次启动拿不到 |
| `INPUT` 结构体大小（64 位）= 40 | 大小错了 `SendInput` 直接返回 0，什么都不发 |
| `move(3, -2)` 打包出 `MOUSEEVENTF_MOVE`、`dx=3`、`dy=-2` | 基本正确性 |
| `SendInput` 返回 0 → 抛 `OSError` → 控制器把这一帧记成 `(0, 0)` | 记成已发出的话，「扣在途」会减掉一段根本没发生的位移 |
| 四个触发键的 VK 映射 | 侧键映射反了，两套方案会互换 |
| 选 SendInput 时不管 `kmbox.encrypted`，调用的是 `move` | `encrypted` 默认是 true，接错了会调一个不存在的方法 |
| `kmbox.enabled = false` + SendInput → 照样连接并移动 | 两个设置互不干扰 |
| Raw Input 数据解析：相对移动累加，绝对移动忽略，`hDevice == 0` 忽略 | 解析写成纯函数，吃字节、吐 `(dx, dy)` 或 `None` |
| 配置：老文件没有 `[desktop]` / `[mouse]` 照常读；新字段能往返保存 | 升级后打不开 `settings.txt` 是最糟的情况 |
| 预设：老预设照常读；新字段能往返 | 同上 |
| 旧界面保存不丢 `[desktop]` / `[mouse]` | §6.5 |
| 状态灯：本机屏幕的成功与失败、`移动：` 横幅、`SendInput 正常`、`output_enabled` | §5.3 |

## 10. 已定的决策

1. 采集和移动是两个独立设置，可以任意组合。
2. 主要适配 KMBox，`kmbox.enabled` 的含义不变；SendInput 是新增的选项。
3. 采集库用 DXcam，两个后端都接；默认用哪个由实测决定。
4. 只抓所选显示器正中央的 `width × height`。
5. SendInput 模式下，用 Raw Input 提供手的移动，轨迹功能完整可用。
6. `KmboxController` 不改名。
7. 旧 tkinter 界面只做到不出错。

## 11. 明确不做

- **任何绕过检测的手段**：不用外设驱动冒充硬件输入，不清除注入标记，不把自己的窗口从截图和录屏里隐藏。
- **按窗口采集**、窗口化游戏的准心定位。
- **GPU 零拷贝**（D3D11 纹理直接交给 CUDA）。先量，等「采集」那一段成为瓶颈再说。
- **推理和游戏抢显卡的缓解**。这是单机模式最大的代价，也是项目原先做成双机的原因。这次先把它量出来（§8 第 6 项），不在本次解决。
- tkinter 界面的新控件。

## 12. 风险

- **和游戏抢显卡**：推理和游戏在同一张卡上，两边都会变慢，需要实测。
- **独占全屏**：部分游戏在独占全屏下 DXGI 复制会拿到黑屏或频繁失效。建议使用无边框窗口。实测时如果遇到，写进 README。
- **混合显卡笔记本**：进程跑在不直连显示器的那张显卡上时，DXGI 复制可能创建失败。DXCam 的 `device_idx` 可以选显卡，如果遇到再把它加到设置里。

## 实测记录（2026-09-21）

机器：RTX 4060 Laptop，2560×1440 @ 144 Hz，缩放 150%。画面源是屏幕正中央一个每帧重绘的窗口（另一个进程）。每项 6–8 秒，「出帧→就绪」= DXGI 报的出帧时间到拿到 BGR 数组。

### 实施中发现的 DXcam 缺陷

`StageSurface.release` 和 `DXGIDuplicator.release` 先手动 `Release()` 再丢引用，comtypes 回收指针时又 `Release()` 一次，同一个 COM 对象被释放两次，进程直接 access violation。按区域抓的第一次 `grab` 就会触发（暂存面按区域尺寸重建），`camera.release()` 也会。两个后端都受影响，上游 main 分支还没修。`desktop_source.py` 给 0.3.x 打了补丁（只丢引用，不手动 Release），打补丁后反复 create / grab / release 都不再崩。

### §8 第 1–3 项：后端、抓帧方式、颜色转换

| 方式 | 帧率 | 出帧→就绪 P50 / P95 / P99 | CPU（一个核 = 100%） |
|---|---|---|---|
| **DXGI，轮询 grab（采用）** | 132 | **1.62 / 3.32 / 5.37 ms** | 66% |
| WGC，轮询 grab | 137 | 量不出（时间戳不可信） | 42% |
| DXGI，numpy 颜色转换 | 135 | 1.81 / 3.52 / 4.74 ms | 861% |
| DXGI，`start(144)` + `get_latest_frame` | 78 | 7.86 / 9.69 / 10.96 ms | 43% |
| DXGI，`start(240)` + `get_latest_frame` | 130 | 4.00 / 6.87 / 9.43 ms | 72% |

结论：
- **默认 DXGI**，界面上挂「（推荐）」。WGC 的出帧时间只有 27/135 帧过得了合理性检查，过了的里面还有一帧老到 65 ms，所以 WGC 路径不用它的时间戳，延迟也就量不出来。
- **用轮询 grab，不用 `start()`**：`start()` 按固定帧率定时取，跟出帧不同步，P50 多 2.4 ms 以上。
- **颜色转换用 cv2**：numpy 后端的 CPU 高得不能用。

### 降 CPU 的几次尝试（都没采用）

| 尝试 | CPU | 出帧→就绪 P50 | 结论 |
|---|---|---|---|
| 轮询间隔 0.5 → 1 / 2 ms | 71% → 68% / 60% | 1.77 → 2.68 / 3.12 ms | 省得少，延迟涨得多 |
| 按帧间隔预测，睡到 0.5 / 0.7 / 0.85 个间隔再轮询 | 75% / 52% / 47% | 2.23 / 1.59 / 1.87 ms | 跑与跑之间的波动跟收益一样大，不值得多一套逻辑 |
| `AcquireNextFrame` 带 20 ms 超时阻塞等帧（要再补一个 DXcam 内部方法） | 64% → 35% | 2.04 → **5.56 ms** | CPU 减半，但延迟翻倍多：阻塞等待不是帧一到就返回 |
| OpenCV 线程数默认（20）对 1，交替各三轮 | 66.9% 对 64.2% | 1.64 对 1.88 ms | 没有可分辨的差别，不改（它是进程级的全局设置） |

代价就摆在这里：**DXGI 轮询采集约占一个核的六成**。空轮询在 DXcam 里是一次 COM 调用加一次 Python 异常，每帧大约 5 次；轮询换成阻塞等帧，延迟又变差。延迟是硬指标，所以保留轮询。

### §8 第 4–5 项：Raw Input 与 SendInput

用 `LocalMouseClient.move` 发 ±5 各三次，Raw Input 收到 6 条，`hDevice` 全部为空，**一条都没有算成手的移动**；计数原样是 ±5，没有被指针加速改写。物理鼠标那一侧要人去推鼠标，留给用户验收。

### §8 第 6 项：整条管线

`--pipeline-benchmark 1000`，本机屏幕（DXGI）+ TensorRT FP16 + `ow2_v8s_320.onnx`：

| 阶段 | P50 | P95 | P99 |
|---|---|---|---|
| **total**（出帧 → 算出目标） | **3.43** | 4.79 | 7.03 |
| assembly（出帧 → 开始抓） | 0.70 | 1.51 | 2.70 |
| decode（抓这一下） | 0.51 | 0.73 | 1.03 |
| inference | 1.86 | 2.35 | 3.09 |

同一台机器上次 UDP JPEG 的记录是 total P50 2.40 ms、inference 1.58 ms。但 UDP 的 total 从「第一个包到达」算起，不含主机那边的采集、编码和网络；本机屏幕的 total 从画面出现在屏幕上算起，是完整的一段。所以单机模式的端到端延迟并不比双机长。inference 多 0.28 ms 是因为显卡跟桌面合成共用；真跑游戏时会更明显，这正是 §12 第一条风险，要在用户的游戏里实测。

### 另外记下的限制

- 游戏以管理员身份运行时，本程序也要以管理员身份运行，否则 SendInput 会被 UIPI 静默拦下：返回值和 GetLastError 都不会报错（微软文档原话）。已写进 README。
- 旧 tkinter 界面载入带非默认 `[desktop]` / `[mouse]` 的预设时，这两节不会跟着换（界面上没这些控件），预设名后面的 `*` 会一直亮着。只在旧界面里出现，新界面没有这个问题。

