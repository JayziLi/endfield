# 瞄准算法插件化（核心引擎）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把写死在 `move_toward` 里的控制律换成可插拔算法，并提供 5 个内置算法，使两个控制方案能各用一个算法、在游戏里按不同触发键即可对比手感。

**Architecture:** 新增 `rhodes_fast/aim_algorithms/` 包，定义「误差 → 这一帧想走多少像素」的纯函数契约。`move_toward` 保留全部共用管道（算误差、死区、EMA 平滑、亚像素累加、限幅、发送、异常兜底），只把控制律部分派发给算法对象。算法自声明参数，配置里按方案存算法名与参数字典。

**Tech Stack:** Python 3.11、标准库 `unittest`、`dataclasses`、`configparser`、`tomli_w`、Tkinter。

## Global Constraints

- 设计依据：`docs/superpowers/specs/2026-09-11-aim-algorithm-plugins-design.md`
- 测试命令一律用项目虚拟环境：`.venv/Scripts/python.exe -m unittest ...`
- 回归基线：本计划开始前 `.venv/Scripts/python.exe -m unittest discover -s tests` 为 **137 tests, OK**。每个任务结束都必须仍然全绿。
- 默认算法是 `p`，且 `p` 的逐帧输出必须与重构前完全一致。现有 `tests/test_kmbox_control.py` 就是基准，**不允许为了让重构通过而修改这些既有断言**。
- 算法不做热切换：算法对象在 `KmboxController.__init__` 创建；`kp` 保持现有热更新行为。
- 不影响瞄准回路性能：算法派发只能是一次属性访问加一次方法调用，禁止在 `move_toward` 里做 I/O、日志或异常控制流。
- 中文注释只写「为什么」，不复述代码在做什么。

## 范围

本计划只做核心引擎、5 个内置算法、界面上的算法选择。设计文档里的**调校 JSON 导入导出**和**算法库管理（导入 .py、查看源码、改名、删除）** 留给第二个计划，因为没有算法就没有可分享的东西。

---

### Task 1: 配置承载算法选择

**Files:**
- Modify: `rhodes_fast/config.py`（`AimProfileConfig` 约 70-79 行、`_convert_section`、`_write_config`）
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: 无
- Produces: `AimProfileConfig.algorithm: str`（默认 `"p"`）、`AimProfileConfig.algorithm_params: dict[str, float]`（默认空字典）。后续任务靠这两个字段决定用哪个算法。

- [ ] **Step 1: 写失败测试 —— 算法字段在两种配置格式上都能往返**

加到 `tests/test_config.py` 的 `ConfigTests` 类里：

```python
    def test_algorithm_selection_round_trips_through_both_formats(self) -> None:
        # settings 是 INI, config 是 TOML, 两条存储路径完全不同。
        # 参数字典在 INI 里只能是一行 JSON 字符串, 很容易只修好一边。
        # 用随仓库分发的示例配置, 不要读本机的 settings.txt / config.toml——
        # 那两个文件是个人运行时配置, 没被 git 跟踪, 而且指向本机的模型路径。
        for source in (_TEXT_CONFIG, _TOML_CONFIG):
            with self.subTest(source=source.name):
                original = load_config(source, validate_model=False)
                changed = replace(
                    original,
                    aim_profile_1=replace(
                        original.aim_profile_1,
                        algorithm="inflight_ff",
                        algorithm_params={"loop_delay_frames": 8.0, "gain": 1.0},
                    ),
                )
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / source.name
                    save_config(changed, path)
                    loaded = load_config(path, validate_model=False)
                self.assertEqual(loaded.aim_profile_1.algorithm, "inflight_ff")
                self.assertEqual(
                    loaded.aim_profile_1.algorithm_params,
                    {"loop_delay_frames": 8.0, "gain": 1.0},
                )

    def test_a_profile_without_an_algorithm_defaults_to_p(self) -> None:
        # 升级前写出的配置文件里没有这两个键, 必须当成现状算法而不是报错。
        self.assertEqual(AimProfileConfig().algorithm, "p")
        self.assertEqual(AimProfileConfig().algorithm_params, {})
```

确认 `tests/test_config.py` 顶部已 import `AimProfileConfig`；没有就加进现有的 `from rhodes_fast.config import ...` 一行。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_config -v`
Expected: FAIL —— `TypeError: AimProfileConfig.__init__() got an unexpected keyword argument 'algorithm'`

- [ ] **Step 3: 加字段**

`rhodes_fast/config.py`，`AimProfileConfig` 末尾追加两个字段：

```python
@dataclass(frozen=True, slots=True)
class AimProfileConfig:
    enabled: bool = True
    trigger: str = "right"
    kp_min: float = 0.1
    kp_max: float = 0.164
    kp_growth: float = 0.167
    target_class: int = 0
    target_y_ratio: float = 0.4
    fov_radius: float = 150.0
    algorithm: str = "p"
    algorithm_params: dict[str, float] = field(default_factory=dict)
```

- [ ] **Step 4: 让 INI 路径认识字典**

`rhodes_fast/config.py` 顶部 import 加 `import json`。

`_convert_section` 里，在 `elif expected is float:` 之后、`else:` 之前插入一个分支：

```python
        elif expected is dict or getattr(expected, "__origin__", None) is dict:
            # INI 一行只能是字符串, 所以字典按 JSON 存。TOML 路径不走这里,
            # tomli_w 原生支持嵌套表。
            converted[key] = json.loads(value) if value.strip() else {}
```

`_write_config` 的 INI 分支改成用 `json.dumps` 写字典：

```python
    parser = configparser.ConfigParser(interpolation=None)
    for section, values in raw.items():
        parser[section] = {
            # str(dict) 出来的是 Python repr（单引号）, json.loads 读不回来。
            key: json.dumps(value) if isinstance(value, dict) else str(value)
            for key, value in values.items()
        }
```

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest tests.test_config -v`
Expected: PASS

- [ ] **Step 6: 跑全量确认没破坏别的**

Run: `.venv/Scripts/python.exe -m unittest discover -s tests`
Expected: `Ran 139 tests` `OK`

- [ ] **Step 7: 提交**

```bash
git add rhodes_fast/config.py tests/test_config.py
git commit -m "feat: let an aim profile name its control algorithm

Adds algorithm and algorithm_params to AimProfileConfig. The INI path
stores the parameter dict as one line of JSON because configparser values
are strings; str() on a dict yields a Python repr that json.loads cannot
read back. The TOML path needs no change.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: 插件契约与 `p` 算法（回归关键）

**Files:**
- Create: `rhodes_fast/aim_algorithms/__init__.py`
- Create: `rhodes_fast/aim_algorithms/contract.py`
- Create: `rhodes_fast/aim_algorithms/builtin.py`
- Create: `rhodes_fast/aim_algorithms/registry.py`
- Modify: `rhodes_fast/kmbox_control.py`
- Test: `tests/test_aim_algorithms.py`（新建）、`tests/test_kmbox_control.py`

**Interfaces:**
- Consumes: Task 1 的 `AimProfileConfig.algorithm` / `.algorithm_params`
- Produces:
  - `Param(name: str, default: float, minimum: float, maximum: float, label: str)`
  - `Observation(error_x, error_y, dt, frame_index, recent_commands, kp_min, kp_max, kp_growth)`
  - `dynamic_kp(error_distance: float, kp_min: float, kp_max: float, kp_growth: float) -> float`
  - `create_algorithm(name: str, params: Mapping[str, float]) -> Algorithm`
  - `available_algorithms() -> dict[str, type]`
  - `UnknownAlgorithm(Exception)`
  - 算法对象有 `NAME`、`DISPLAY_NAME`、`PARAMS`、`reset()`、`compute(observation) -> tuple[float, float]`

- [ ] **Step 1: 写失败测试 —— 契约与 `p`**

新建 `tests/test_aim_algorithms.py`：

```python
from __future__ import annotations

import math
import unittest

from rhodes_fast.aim_algorithms import (
    Observation,
    UnknownAlgorithm,
    available_algorithms,
    create_algorithm,
    dynamic_kp,
)


def observe(error_x=0.0, error_y=0.0, *, commands=(), frame_index=0, dt=1 / 240) -> Observation:
    return Observation(
        error_x=error_x,
        error_y=error_y,
        dt=dt,
        frame_index=frame_index,
        recent_commands=tuple(commands),
        kp_min=0.067,
        kp_max=0.143,
        kp_growth=0.031,
    )


class AlgorithmRegistryTests(unittest.TestCase):
    def test_p_is_available_and_is_the_default_name(self) -> None:
        self.assertIn("p", available_algorithms())

    def test_an_unknown_name_is_refused_rather_than_silently_substituted(self) -> None:
        # 静默换成别的算法会让人以为在用自己选的那个, 手感却完全不同。
        with self.assertRaises(UnknownAlgorithm):
            create_algorithm("no_such_algorithm", {})

    def test_missing_parameters_fall_back_to_the_declared_defaults(self) -> None:
        algorithm = create_algorithm("p", {})
        self.assertEqual(algorithm.NAME, "p")


class ProportionalTests(unittest.TestCase):
    def test_output_is_the_error_scaled_by_the_distance_based_gain(self) -> None:
        algorithm = create_algorithm("p", {})
        expected_kp = dynamic_kp(math.hypot(60.0, -20.0), 0.067, 0.143, 0.031)

        raw_x, raw_y = algorithm.compute(observe(60.0, -20.0))

        self.assertAlmostEqual(raw_x, 60.0 * expected_kp)
        self.assertAlmostEqual(raw_y, -20.0 * expected_kp)

    def test_p_holds_no_state_so_reset_changes_nothing(self) -> None:
        algorithm = create_algorithm("p", {})
        before = algorithm.compute(observe(40.0, 0.0))
        algorithm.reset()
        self.assertEqual(algorithm.compute(observe(40.0, 0.0)), before)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_aim_algorithms -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'rhodes_fast.aim_algorithms'`

- [ ] **Step 3: 写契约**

新建 `rhodes_fast/aim_algorithms/contract.py`：

```python
"""控制算法的契约。

算法只回答一个问题: 看到这样的误差, 这一帧想走多少像素。死区、平滑、亚像素累加、
限幅、发送全部由 KmboxController 的共用管道负责——那些容易写错又和控制律无关,
不该让每个算法各写一遍。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence


@dataclass(frozen=True, slots=True)
class Param:
    name: str
    default: float
    minimum: float
    maximum: float
    label: str


@dataclass(frozen=True, slots=True)
class Observation:
    error_x: float
    error_y: float
    dt: float
    frame_index: int
    # 最近发出的移动量, 最新的在末尾。写「扣在途」「前馈」「卡尔曼」都要靠它:
    # 算法结合自己的延迟参数, 既能算出尚未生效的指令总量, 也能算出本帧刚生效的
    # 那一条, 从而把目标自身的位移和我们的位移区分开。
    recent_commands: Sequence[tuple[int, int]]
    kp_min: float
    kp_max: float
    kp_growth: float


class Algorithm(Protocol):
    NAME: str
    DISPLAY_NAME: str
    PARAMS: tuple[Param, ...]

    def reset(self) -> None: ...

    def compute(self, observation: Observation) -> tuple[float, float]: ...


def dynamic_kp(error_distance: float, kp_min: float, kp_max: float, kp_growth: float) -> float:
    span = kp_max - kp_min
    growth = 1.0 - math.exp(-kp_growth * max(0.0, error_distance))
    return kp_min + span * growth


def resolve_params(specs: tuple[Param, ...], values: Mapping[str, float]) -> dict[str, float]:
    return {spec.name: float(values.get(spec.name, spec.default)) for spec in specs}
```

- [ ] **Step 4: 写 `p` 算法**

新建 `rhodes_fast/aim_algorithms/builtin.py`：

```python
"""随程序分发的内置算法。

它们和用户自己写的算法走完全相同的契约, 不开任何后门。如果某个内置算法需要
特殊通道, 说明契约设计得不对。
"""

from __future__ import annotations

import math
from typing import Mapping

from .contract import Observation, Param, dynamic_kp


class Proportional:
    """现状: 误差乘以随距离变化的比例增益。"""

    NAME = "p"
    DISPLAY_NAME = "比例控制（现状）"
    PARAMS: tuple[Param, ...] = ()

    def __init__(self, params: Mapping[str, float]) -> None:
        del params

    def reset(self) -> None:
        return None

    def compute(self, observation: Observation) -> tuple[float, float]:
        distance = math.hypot(observation.error_x, observation.error_y)
        kp = dynamic_kp(distance, observation.kp_min, observation.kp_max, observation.kp_growth)
        return observation.error_x * kp, observation.error_y * kp


BUILTIN_ALGORITHMS: tuple[type, ...] = (Proportional,)
```

- [ ] **Step 5: 写注册表与包出口**

新建 `rhodes_fast/aim_algorithms/registry.py`：

```python
from __future__ import annotations

from typing import Mapping

from .builtin import BUILTIN_ALGORITHMS
from .contract import Algorithm, resolve_params


class UnknownAlgorithm(LookupError):
    def __init__(self, name: str) -> None:
        super().__init__(f"未知的控制算法：{name}")
        self.name = name


def available_algorithms() -> dict[str, type]:
    return {algorithm.NAME: algorithm for algorithm in BUILTIN_ALGORITHMS}


def create_algorithm(name: str, params: Mapping[str, float]) -> Algorithm:
    algorithms = available_algorithms()
    if name not in algorithms:
        raise UnknownAlgorithm(name)
    factory = algorithms[name]
    return factory(resolve_params(factory.PARAMS, params))
```

新建 `rhodes_fast/aim_algorithms/__init__.py`：

```python
from .contract import Algorithm, Observation, Param, dynamic_kp, resolve_params
from .registry import UnknownAlgorithm, available_algorithms, create_algorithm

__all__ = [
    "Algorithm",
    "Observation",
    "Param",
    "UnknownAlgorithm",
    "available_algorithms",
    "create_algorithm",
    "dynamic_kp",
    "resolve_params",
]
```

- [ ] **Step 6: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest tests.test_aim_algorithms -v`
Expected: PASS（5 tests）

- [ ] **Step 7: 写失败测试 —— 控制器派发给算法，且运行时设置不会冲掉算法**

加到 `tests/test_kmbox_control.py` 的 `KmboxControllerTests` 类里：

```python
    def test_an_unknown_algorithm_falls_back_loudly_instead_of_refusing_to_start(self) -> None:
        # 启动失败会让人在游戏里才发现自瞄整个不工作, 比回退更糟; 但回退必须留痕。
        controller = KmboxController(
            KmboxConfig(uuid="00000000"),
            AimConfig(),
            profiles=_profiles(AimProfileConfig(algorithm="no_such_algorithm")),
        )
        self.assertEqual(controller.algorithm_name(0), "p")
        self.assertTrue(controller.algorithm_warnings)

    def test_a_runtime_settings_write_keeps_the_configured_algorithm(self) -> None:
        # 运行时 JSON 只带 kp 那几项。重建 profile 时若不显式继承, 算法会被默默
        # 重置成 p——用户在界面上拖一下 kp 滑块, 算法就换了。
        with tempfile.TemporaryDirectory() as directory:
            runtime_file = Path(directory) / "aim.json"
            runtime_file.write_text(
                json.dumps(
                    {
                        "profiles": [
                            {
                                "enabled": True,
                                "trigger": "right",
                                "kp_min": 0.2,
                                "kp_max": 0.2,
                                "kp_growth": 0.0,
                            },
                            {
                                "enabled": False,
                                "trigger": "left",
                                "kp_min": 0.1,
                                "kp_max": 0.1,
                                "kp_growth": 0.0,
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            controller = KmboxController(
                KmboxConfig(uuid="00000000"),
                AimConfig(),
                runtime_file,
                profiles=_profiles(
                    AimProfileConfig(algorithm="p", algorithm_params={"kd": 0.5})
                ),
            )
            controller.refresh_runtime_settings(force=True)

        self.assertEqual(controller._profiles[0].algorithm, "p")
        self.assertEqual(controller._profiles[0].algorithm_params, {"kd": 0.5})
```

- [ ] **Step 8: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_kmbox_control -v`
Expected: 两个新测试都红。

- `test_an_unknown_algorithm_falls_back_loudly...` → `AttributeError: 'KmboxController' object has no attribute 'algorithm_name'`
- `test_a_runtime_settings_write_keeps_the_configured_algorithm` → `AssertionError: {} != {'kd': 0.5}`

第二条的形态值得留意：它**不报错**，只是把 `algorithm_params` 静默冲成空字典。
这正是这个 bug 危险的地方——界面上拖一下 kp 滑块，算法参数就没了，没有任何提示。

- [ ] **Step 9: 改造控制器**

`rhodes_fast/kmbox_control.py`：

import 区域改为：

```python
from collections import deque

from kmbox_universal import KMBoxClient, KMBoxError

# dynamic_kp 起别名: 本模块要对外导出一个同名但签名不同的包装函数（传 profile 对象）,
# 既有测试依赖那个形式。
from .aim_algorithms import Observation, UnknownAlgorithm, create_algorithm, dynamic_kp as _kp
from .config import AimConfig, AimProfileConfig, KmboxConfig
from .detector import Detection

# 在途指令窗口。算法最多回看自己的延迟参数那么多帧, 64 足够覆盖合理范围。
_COMMAND_HISTORY = 64
```

删掉文件末尾的 `dynamic_kp` 与 `_dynamic_kp` 两个函数定义——`dynamic_kp` 现在从 `.aim_algorithms` 导入并由本模块再导出，既有测试 `from rhodes_fast.kmbox_control import dynamic_kp` 仍然可用。但既有测试调用的是 `dynamic_kp(distance, config)` 这种「传 profile」的形式，所以保留一个同名包装：

```python
def dynamic_kp(error_distance: float, config: AimProfileConfig) -> float:
    return _kp(error_distance, config.kp_min, config.kp_max, config.kp_growth)
```

（上面的 import 已经用了 `dynamic_kp as _kp` 这个别名。）

`_AimMotionState` 增加历史与计时：

```python
class _AimMotionState:
    def __init__(self) -> None:
        self.recent_commands: deque[tuple[int, int]] = deque(maxlen=_COMMAND_HISTORY)
        self.reset()

    def reset(self) -> None:
        self.smooth_x = 0.0
        self.smooth_y = 0.0
        self.residual_x = 0.0
        self.residual_y = 0.0
        self.frame_index = 0
        self.last_frame_at = 0.0
        self.recent_commands.clear()
```

`__init__` 末尾（`self.trigger_errors = 0` 之后）建算法对象：

```python
        self.algorithm_warnings: list[str] = []
        self._algorithms = [self._build_algorithm(profile) for profile in self._profiles]

    def _build_algorithm(self, profile: AimProfileConfig):
        try:
            return create_algorithm(profile.algorithm, profile.algorithm_params)
        except UnknownAlgorithm:
            self.algorithm_warnings.append(
                f"算法 {profile.algorithm} 找不到，已回退到 p"
            )
            return create_algorithm("p", {})

    def algorithm_name(self, index: int) -> str:
        return self._algorithms[index].NAME
```

重置要连算法一起：

```python
    def _reset_profile(self, index: int) -> None:
        self._motion_states[index].reset()
        self._algorithms[index].reset()

    def reset(self) -> None:
        for index in range(len(self._motion_states)):
            self._reset_profile(index)
```

把原来三处 `self._motion_states[...].reset()` / `state.reset()` 全换成 `self._reset_profile(index)`：
`refresh_runtime_settings` 的循环里、`_set_active_profile` 的两处、以及 `move_toward` 死区分支（那里用 `self._reset_profile(profile_index)`）。

`refresh_runtime_settings` 里构造 profile 的那段（约 146-160 行）补上两个字段，从 `fallback` 继承：

```python
                        fov_radius=float(raw.get("fov_radius", values.get("fov_radius", fallback.fov_radius))),
                        algorithm=fallback.algorithm,
                        algorithm_params=dict(fallback.algorithm_params),
```

`move_toward` 中间那段换成派发：

```python
        if error_distance <= self.aim_config.deadzone:
            self._reset_profile(profile_index)
            return (0, 0)
        now = time.perf_counter()
        observation = Observation(
            error_x=error_x,
            error_y=error_y,
            dt=now - state.last_frame_at if state.last_frame_at else 0.0,
            frame_index=state.frame_index,
            recent_commands=state.recent_commands,
            kp_min=profile.kp_min,
            kp_max=profile.kp_max,
            kp_growth=profile.kp_growth,
        )
        state.last_frame_at = now
        state.frame_index += 1
        raw_x, raw_y = self._algorithms[profile_index].compute(observation)
```

`move_toward` 结尾要把实际发出的移动量记进历史。指令没发出去就记 `(0, 0)`——记成发了会让「扣在途」减掉一段根本没发生的位移：

```python
        dx, state.residual_x = _axis_step(state.smooth_x, state.residual_x, self.aim_config.max_step)
        dy, state.residual_y = _axis_step(state.smooth_y, state.residual_y, self.aim_config.max_step)
        if dx == 0 and dy == 0:
            state.recent_commands.append((0, 0))
            return (0, 0)
        move = self._client.enc_move if self.device_config.encrypted else self._client.move
        started = time.perf_counter()
        try:
            move(dx, dy)
        except (KMBoxError, OSError):
            state.recent_commands.append((0, 0))
            return (0, 0)
        finally:
            self.last_send_ms = (time.perf_counter() - started) * 1000.0
        state.recent_commands.append((dx, dy))
        return (dx, dy)
```

- [ ] **Step 10: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest tests.test_kmbox_control -v`
Expected: PASS。**既有断言一个都不能改**——它们证明 `p` 的逐帧输出与重构前一致。

- [ ] **Step 11: 跑全量**

Run: `.venv/Scripts/python.exe -m unittest discover -s tests`
Expected: `Ran 146 tests` `OK`

- [ ] **Step 12: 提交**

```bash
git add rhodes_fast/aim_algorithms tests/test_aim_algorithms.py rhodes_fast/kmbox_control.py tests/test_kmbox_control.py
git commit -m "refactor: dispatch the control law through a pluggable algorithm

move_toward keeps every shared stage - error, deadzone, smoothing,
sub-pixel accumulation, clamping, sending, error handling - and hands only
the control law to an algorithm object. The p algorithm reproduces the
previous behaviour frame for frame, which the untouched existing tests
prove.

An unknown algorithm falls back to p with a recorded warning rather than
refusing to start: a startup failure would be discovered mid-game as the
aim simply not working. A runtime settings write now carries the
configured algorithm forward, which it would otherwise reset to the
default every time a kp slider moved.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: `pd` 算法

**Files:**
- Modify: `rhodes_fast/aim_algorithms/builtin.py`
- Test: `tests/test_aim_algorithms.py`

**Interfaces:**
- Consumes: Task 2 的 `Param`、`Observation`、`dynamic_kp`、`BUILTIN_ALGORITHMS`
- Produces: 算法名 `"pd"`，参数 `kd`

- [ ] **Step 1: 写失败测试**

```python
class ProportionalDerivativeTests(unittest.TestCase):
    def test_the_first_frame_has_no_previous_error_so_it_matches_plain_p(self) -> None:
        derivative = create_algorithm("pd", {"kd": 0.6})
        proportional = create_algorithm("p", {})
        self.assertEqual(derivative.compute(observe(50.0, 0.0)), proportional.compute(observe(50.0, 0.0)))

    def test_a_shrinking_error_produces_a_braking_term(self) -> None:
        # 误差在缩小时微分项符号与比例项相反, 起刹车作用。
        algorithm = create_algorithm("pd", {"kd": 0.6})
        algorithm.compute(observe(100.0, 0.0))
        raw_x, _ = algorithm.compute(observe(80.0, 0.0))

        expected_kp = dynamic_kp(80.0, 0.067, 0.143, 0.031)
        self.assertAlmostEqual(raw_x, 80.0 * expected_kp + 0.6 * (80.0 - 100.0))

    def test_reset_forgets_the_previous_error(self) -> None:
        algorithm = create_algorithm("pd", {"kd": 0.6})
        algorithm.compute(observe(100.0, 0.0))
        algorithm.reset()
        self.assertEqual(
            algorithm.compute(observe(80.0, 0.0)),
            create_algorithm("p", {}).compute(observe(80.0, 0.0)),
        )
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_aim_algorithms -v`
Expected: FAIL —— `UnknownAlgorithm: 未知的控制算法：pd`

- [ ] **Step 3: 实现**

`rhodes_fast/aim_algorithms/builtin.py` 追加：

```python
class ProportionalDerivative:
    """比例加微分。治超调, 对匀速移动目标的稳态落后没有作用——那时误差恒定,
    微分项为零。"""

    NAME = "pd"
    DISPLAY_NAME = "比例 + 微分"
    PARAMS: tuple[Param, ...] = (Param("kd", 0.3, 0.0, 2.0, "微分系数"),)

    def __init__(self, params: Mapping[str, float]) -> None:
        self._kd = params["kd"]
        self.reset()

    def reset(self) -> None:
        self._previous: tuple[float, float] | None = None

    def compute(self, observation: Observation) -> tuple[float, float]:
        distance = math.hypot(observation.error_x, observation.error_y)
        kp = dynamic_kp(distance, observation.kp_min, observation.kp_max, observation.kp_growth)
        raw_x = observation.error_x * kp
        raw_y = observation.error_y * kp
        if self._previous is not None:
            raw_x += self._kd * (observation.error_x - self._previous[0])
            raw_y += self._kd * (observation.error_y - self._previous[1])
        self._previous = (observation.error_x, observation.error_y)
        return raw_x, raw_y
```

并把它加进 `BUILTIN_ALGORITHMS`：

```python
BUILTIN_ALGORITHMS: tuple[type, ...] = (Proportional, ProportionalDerivative)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest tests.test_aim_algorithms -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add rhodes_fast/aim_algorithms/builtin.py tests/test_aim_algorithms.py
git commit -m "feat: add the proportional-derivative algorithm

Kept as a built-in even though simulation says it is the weaker answer -
it does nothing for tracking lag, and lowering kp beats it on overshoot.
Trying it is more convincing than being told.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: `feedforward` 算法

**Files:**
- Modify: `rhodes_fast/aim_algorithms/builtin.py`
- Test: `tests/test_aim_algorithms.py`

**Interfaces:**
- Consumes: Task 2 的契约、`Observation.recent_commands`
- Produces: 算法名 `"feedforward"`，参数 `loop_delay_frames`、`gain`、`velocity_smoothing`；模块级辅助函数 `_landed_command(recent_commands, lag) -> tuple[float, float]`（Task 5 复用）

- [ ] **Step 1: 写失败测试**

```python
class FeedforwardTests(unittest.TestCase):
    def test_a_still_target_gets_no_lead(self) -> None:
        algorithm = create_algorithm(
            "feedforward",
            {"loop_delay_frames": 4, "gain": 1.0, "velocity_smoothing": 1.0},
        )
        algorithm.compute(observe(50.0, 0.0, commands=[(0, 0)] * 8))
        raw_x, _ = algorithm.compute(observe(50.0, 0.0, commands=[(0, 0)] * 8))

        self.assertAlmostEqual(raw_x, 50.0 * dynamic_kp(50.0, 0.067, 0.143, 0.031))

    def test_our_own_landed_movement_is_not_mistaken_for_target_motion(self) -> None:
        # 误差因为我们自己的移动生效而变小时, 目标其实没动, 不该产生前馈。
        algorithm = create_algorithm(
            "feedforward",
            {"loop_delay_frames": 4, "gain": 1.0, "velocity_smoothing": 1.0},
        )
        history = [(5, 0), (0, 0), (0, 0), (0, 0)]
        algorithm.compute(observe(50.0, 0.0, commands=history))
        raw_x, _ = algorithm.compute(observe(45.0, 0.0, commands=history))

        self.assertAlmostEqual(raw_x, 45.0 * dynamic_kp(45.0, 0.067, 0.143, 0.031))

    def test_a_moving_target_is_aimed_ahead_of_where_it_was_seen(self) -> None:
        algorithm = create_algorithm(
            "feedforward",
            {"loop_delay_frames": 4, "gain": 1.0, "velocity_smoothing": 1.0},
        )
        history = [(0, 0)] * 8
        algorithm.compute(observe(50.0, 0.0, commands=history))
        raw_x, _ = algorithm.compute(observe(53.0, 0.0, commands=history))

        aim = 53.0 + 3.0 * 4
        self.assertAlmostEqual(raw_x, aim * dynamic_kp(abs(aim), 0.067, 0.143, 0.031))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_aim_algorithms -v`
Expected: FAIL —— `UnknownAlgorithm: 未知的控制算法：feedforward`

- [ ] **Step 3: 实现**

`rhodes_fast/aim_algorithms/builtin.py` 追加：

```python
def _landed_command(recent_commands, lag: int) -> tuple[float, float]:
    """本帧刚在画面里生效的那一条指令。

    recent_commands 最新的在末尾, 对应上一帧。第 k-lag 帧发出的指令在第 k 帧生效,
    所以往回数第 lag 个就是它。
    """
    commands = list(recent_commands)
    if len(commands) < lag:
        return 0.0, 0.0
    x, y = commands[-lag]
    return float(x), float(y)


class Feedforward:
    """按目标速度提前量瞄准。

    目标位移要绕完整条回路才出现在画面里, 所以看到的永远是它几帧前的位置。
    把「目标速度 x 延迟帧数」加到误差上, 瞄的就是它现在大概在的地方。
    """

    NAME = "feedforward"
    DISPLAY_NAME = "速度前馈"
    PARAMS: tuple[Param, ...] = (
        Param("loop_delay_frames", 8.0, 1.0, 30.0, "回路延迟（帧）"),
        Param("gain", 1.0, 0.0, 2.0, "前馈强度"),
        Param("velocity_smoothing", 0.25, 0.01, 1.0, "速度平滑"),
    )

    def __init__(self, params: Mapping[str, float]) -> None:
        self._lag = max(1, int(round(params["loop_delay_frames"])))
        self._gain = params["gain"]
        self._alpha = params["velocity_smoothing"]
        self.reset()

    def reset(self) -> None:
        self._previous: tuple[float, float] | None = None
        self._velocity_x = 0.0
        self._velocity_y = 0.0

    def _track(self, observation: Observation) -> None:
        landed_x, landed_y = _landed_command(observation.recent_commands, self._lag)
        if self._previous is not None:
            # 误差的变化 = 目标动了多少 - 我们动了多少。把刚生效的那条指令加回去,
            # 剩下的才是目标自己的位移。
            delta_x = (observation.error_x - self._previous[0]) + landed_x
            delta_y = (observation.error_y - self._previous[1]) + landed_y
            self._velocity_x += self._alpha * (delta_x - self._velocity_x)
            self._velocity_y += self._alpha * (delta_y - self._velocity_y)
        self._previous = (observation.error_x, observation.error_y)

    def _lead(self) -> tuple[float, float]:
        return (
            self._gain * self._velocity_x * self._lag,
            self._gain * self._velocity_y * self._lag,
        )

    def compute(self, observation: Observation) -> tuple[float, float]:
        self._track(observation)
        lead_x, lead_y = self._lead()
        aim_x = observation.error_x + lead_x
        aim_y = observation.error_y + lead_y
        distance = math.hypot(aim_x, aim_y)
        kp = dynamic_kp(distance, observation.kp_min, observation.kp_max, observation.kp_growth)
        return aim_x * kp, aim_y * kp
```

更新注册清单：

```python
BUILTIN_ALGORITHMS: tuple[type, ...] = (Proportional, ProportionalDerivative, Feedforward)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest tests.test_aim_algorithms -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add rhodes_fast/aim_algorithms/builtin.py tests/test_aim_algorithms.py
git commit -m "feat: add velocity feedforward

Separating the target's motion from our own is the whole trick: the error
shrinking because a command of ours just landed is not the target moving,
so the command that became visible this frame is added back before the
velocity estimate is updated.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: `inflight` 与 `inflight_ff` 算法

**Files:**
- Modify: `rhodes_fast/aim_algorithms/builtin.py`
- Test: `tests/test_aim_algorithms.py`

**Interfaces:**
- Consumes: Task 4 的 `Feedforward._track` / `_lead` / `_landed_command`
- Produces: 算法名 `"inflight"`、`"inflight_ff"`；模块级 `_in_flight(recent_commands, lag) -> tuple[float, float]`

- [ ] **Step 1: 写失败测试**

```python
class InFlightTests(unittest.TestCase):
    def test_commands_that_have_not_landed_are_subtracted_from_the_error(self) -> None:
        # 不扣掉在途指令, 控制器会为已经发出的位移重复下单, 于是必然冲过头。
        algorithm = create_algorithm("inflight", {"loop_delay_frames": 4})
        commands = [(9, 0), (2, 0), (3, 0), (4, 0)]
        raw_x, _ = algorithm.compute(observe(100.0, 0.0, commands=commands))

        remaining = 100.0 - (2 + 3 + 4)
        self.assertAlmostEqual(raw_x, remaining * dynamic_kp(remaining, 0.067, 0.143, 0.031))

    def test_an_empty_history_leaves_the_error_untouched(self) -> None:
        algorithm = create_algorithm("inflight", {"loop_delay_frames": 4})
        raw_x, _ = algorithm.compute(observe(100.0, 0.0))
        self.assertAlmostEqual(raw_x, 100.0 * dynamic_kp(100.0, 0.067, 0.143, 0.031))


class InFlightFeedforwardTests(unittest.TestCase):
    def test_it_both_subtracts_in_flight_movement_and_leads_a_moving_target(self) -> None:
        algorithm = create_algorithm(
            "inflight_ff",
            {"loop_delay_frames": 4, "gain": 1.0, "velocity_smoothing": 1.0},
        )
        commands = [(0, 0)] * 8
        algorithm.compute(observe(50.0, 0.0, commands=commands))
        moving = [(0, 0), (2, 0), (2, 0), (2, 0)]
        raw_x, _ = algorithm.compute(observe(53.0, 0.0, commands=moving))

        aim = 53.0 - (2 + 2 + 2) + 3.0 * 4
        self.assertAlmostEqual(raw_x, aim * dynamic_kp(abs(aim), 0.067, 0.143, 0.031))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_aim_algorithms -v`
Expected: FAIL —— `UnknownAlgorithm: 未知的控制算法：inflight`

- [ ] **Step 3: 实现**

`rhodes_fast/aim_algorithms/builtin.py` 追加：

```python
def _in_flight(recent_commands, lag: int) -> tuple[float, float]:
    """已经发出但还没在画面里生效的位移总量。

    第 m 帧发出的指令在第 m+lag 帧才可见, 所以最近 lag-1 帧发出的都还在途中。
    """
    if lag <= 1:
        return 0.0, 0.0
    window = list(recent_commands)[-(lag - 1):]
    return float(sum(x for x, _ in window)), float(sum(y for _, y in window))


class InFlight:
    """扣掉在途指令。

    控制器看到的误差还没反映出最近几帧已经发出的移动, 不扣掉就会为同一段位移
    重复下单。注意它假设目标不动——目标在动时在途位移补不上那段距离, 跟踪反而
    比纯比例更差, 所以实用时应与前馈配对（见 inflight_ff）。
    """

    NAME = "inflight"
    DISPLAY_NAME = "扣除在途指令"
    PARAMS: tuple[Param, ...] = (
        Param("loop_delay_frames", 8.0, 1.0, 30.0, "回路延迟（帧）"),
    )

    def __init__(self, params: Mapping[str, float]) -> None:
        self._lag = max(1, int(round(params["loop_delay_frames"])))

    def reset(self) -> None:
        return None

    def compute(self, observation: Observation) -> tuple[float, float]:
        flight_x, flight_y = _in_flight(observation.recent_commands, self._lag)
        work_x = observation.error_x - flight_x
        work_y = observation.error_y - flight_y
        distance = math.hypot(work_x, work_y)
        kp = dynamic_kp(distance, observation.kp_min, observation.kp_max, observation.kp_growth)
        return work_x * kp, work_y * kp


class InFlightFeedforward(Feedforward):
    """两者合用: 在途量补我们自己的位移, 前馈补目标的位移。

    它们补的是同一段死区时间的不同部分, 必须成对使用。
    """

    NAME = "inflight_ff"
    DISPLAY_NAME = "扣在途 + 速度前馈"

    def compute(self, observation: Observation) -> tuple[float, float]:
        self._track(observation)
        lead_x, lead_y = self._lead()
        flight_x, flight_y = _in_flight(observation.recent_commands, self._lag)
        aim_x = observation.error_x - flight_x + lead_x
        aim_y = observation.error_y - flight_y + lead_y
        distance = math.hypot(aim_x, aim_y)
        kp = dynamic_kp(distance, observation.kp_min, observation.kp_max, observation.kp_growth)
        return aim_x * kp, aim_y * kp
```

更新注册清单：

```python
BUILTIN_ALGORITHMS: tuple[type, ...] = (
    Proportional,
    ProportionalDerivative,
    Feedforward,
    InFlight,
    InFlightFeedforward,
)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest tests.test_aim_algorithms -v`
Expected: PASS

- [ ] **Step 5: 跑全量**

Run: `.venv/Scripts/python.exe -m unittest discover -s tests`
Expected: `Ran 155 tests` `OK`

- [ ] **Step 6: 提交**

```bash
git add rhodes_fast/aim_algorithms/builtin.py tests/test_aim_algorithms.py
git commit -m "feat: add in-flight compensation, alone and paired with feedforward

Subtracting commands that have not yet become visible stops the controller
re-ordering movement it already sent, which is what makes the overshoot.
On its own it assumes a stationary target and tracks worse than plain
proportional; paired with feedforward it covers both halves of the dead
time.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: 界面上选择算法与参数

**Files:**
- Modify: `rhodes_fast/gui.py`
- Test: `tests/test_gui_algorithm.py`（新建）

**Interfaces:**
- Consumes: Task 2 的 `available_algorithms()`、Task 1 的 `AimProfileConfig.algorithm` / `.algorithm_params`
- Produces: 无（终端任务）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_gui_algorithm.py`：

```python
from __future__ import annotations

import unittest

from rhodes_fast.aim_algorithms import available_algorithms
from rhodes_fast.gui import algorithm_choices, algorithm_param_specs


class GuiAlgorithmTests(unittest.TestCase):
    def test_every_algorithm_is_offered_with_a_human_readable_label(self) -> None:
        choices = algorithm_choices()
        self.assertEqual(set(choices.values()), set(available_algorithms()))
        for label in choices:
            self.assertTrue(label.strip())

    def test_p_declares_no_parameters_so_no_fields_are_drawn(self) -> None:
        self.assertEqual(algorithm_param_specs("p"), ())

    def test_inflight_ff_declares_the_three_fields_it_needs(self) -> None:
        names = [spec.name for spec in algorithm_param_specs("inflight_ff")]
        self.assertEqual(names, ["loop_delay_frames", "gain", "velocity_smoothing"])

    def test_an_unknown_algorithm_declares_nothing_rather_than_raising(self) -> None:
        # 配置里指着一个已删掉的算法时, 界面仍要能画出来。
        self.assertEqual(algorithm_param_specs("no_such_algorithm"), ())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_gui_algorithm -v`
Expected: FAIL —— `ImportError: cannot import name 'algorithm_choices' from 'rhodes_fast.gui'`

- [ ] **Step 3: 加两个纯函数**

`rhodes_fast/gui.py`，在已有的模块级常量附近（`PREVIEW_POLL_MS` 那一带）加：

```python
from .aim_algorithms import Param, available_algorithms


def algorithm_choices() -> dict[str, str]:
    """界面显示名 -> 算法标识。和 TRIGGERS 那几个映射同一个写法。"""
    return {
        algorithm.DISPLAY_NAME: name
        for name, algorithm in sorted(available_algorithms().items())
    }


def algorithm_param_specs(name: str) -> tuple[Param, ...]:
    algorithm = available_algorithms().get(name)
    # 配置里指着一个已删掉的算法时界面仍要画得出来, 所以不抛异常。
    return algorithm.PARAMS if algorithm is not None else ()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest tests.test_gui_algorithm -v`
Expected: PASS

- [ ] **Step 5: 在方案面板里加算法下拉框与参数区**

`rhodes_fast/gui.py` 的 `_create_variables`，在 `self.profile_kp_growth_text` 那一组之后加：

```python
        self.profile_algorithm = [
            tk.StringVar(value=_display_value(algorithm_choices(), profile.algorithm))
            for profile in cfg.aim_profiles
        ]
        self.profile_algorithm_params: list[dict[str, tk.DoubleVar]] = [
            {
                spec.name: tk.DoubleVar(
                    value=profile.algorithm_params.get(spec.name, spec.default)
                )
                for spec in algorithm_param_specs(profile.algorithm)
            }
            for profile in cfg.aim_profiles
        ]
```

在方案面板构建处（`panel` 那个循环里，触发方式那一行之后）插入下拉框与参数容器：

```python
            algorithm_combo = self._combo_row(
                panel, 2, "控制算法", self.profile_algorithm[index], algorithm_choices()
            )
            algorithm_combo.bind(
                "<<ComboboxSelected>>",
                lambda _event, profile=index: self._algorithm_changed(profile),
            )
            self.algorithm_combos.append(algorithm_combo)
            params_frame = ttk.Frame(panel)
            params_frame.grid(row=3, column=0, columnspan=2, sticky="ew")
            params_frame.columnconfigure(1, weight=1)
            self.algorithm_param_frames.append(params_frame)
            self._rebuild_algorithm_params(index)
```

面板原有的网格行是：0 启用此方案、1 触发方式、2 目标标签、3 框内位置、4 视野半径、
5 P 最小值、6 P 最大值、7 P 增长斜率。算法下拉框占 2、参数区占 3，所以后面五项各加 2：

| 控件 | 原 row | 新 row |
|---|---|---|
| 目标标签（`ttk.Label` 与 `target_class_combo` 两处） | 2 | 4 |
| 框内位置（顶部 0%） | 3 | 5 |
| 视野半径 | 4 | 6 |
| P 最小值 | 5 | 7 |
| P 最大值 | 6 | 8 |
| P 增长斜率 | 7 | 9 |

循环开始前初始化两个列表：

```python
        self.algorithm_combos: list[ttk.Combobox] = []
        self.algorithm_param_frames: list[ttk.Frame] = []
```

新增两个方法：

```python
    def _rebuild_algorithm_params(self, profile: int) -> None:
        frame = self.algorithm_param_frames[profile]
        for child in frame.winfo_children():
            child.destroy()
        name = algorithm_choices()[self.profile_algorithm[profile].get()]
        variables: dict[str, tk.DoubleVar] = {}
        for row, spec in enumerate(algorithm_param_specs(name)):
            previous = self.profile_algorithm_params[profile].get(spec.name)
            variable = tk.DoubleVar(
                value=previous.get() if previous is not None else spec.default
            )
            variables[spec.name] = variable
            ttk.Label(frame, text=spec.label).grid(
                row=row, column=0, sticky="w", padx=(0, 12), pady=4
            )
            ttk.Spinbox(
                frame,
                textvariable=variable,
                from_=spec.minimum,
                to=spec.maximum,
                increment=0.01,
                width=10,
            ).grid(row=row, column=1, sticky="w", pady=4)
        self.profile_algorithm_params[profile] = variables

    def _algorithm_changed(self, profile: int) -> None:
        self._rebuild_algorithm_params(profile)
        if self.process is not None:
            self._append_log("控制算法要停止后重新启动才会生效。")
```

- [ ] **Step 6: 保存配置时带上算法**

`rhodes_fast/gui.py` 里两处 `profile_1 = replace(...)` / `profile_2 = replace(...)`（约 563-584 行），各加两行：

```python
            algorithm=algorithm_choices()[self.profile_algorithm[0].get()],
            algorithm_params={
                name: variable.get()
                for name, variable in self.profile_algorithm_params[0].items()
            },
```

profile_2 同理，下标换成 `[1]`。

- [ ] **Step 7: 运行中禁用算法下拉框**

找到 `_set_running`，在它设置其他控件状态的地方加上：

```python
        for combo in self.algorithm_combos:
            combo.configure(state="disabled" if running else "readonly")
```

- [ ] **Step 8: 跑全量**

Run: `.venv/Scripts/python.exe -m unittest discover -s tests`
Expected: `Ran 159 tests` `OK`

- [ ] **Step 9: 手动验界面能起来**

Run: `.venv/Scripts/python.exe -m rhodes_fast --gui --config settings.txt`
Expected: 界面正常打开；「识别与控制」页每个控制方案里出现「控制算法」下拉框；选 `扣在途 + 速度前馈` 后下方出现三个参数框；选 `比例控制（现状）` 后参数框消失。关掉窗口。

- [ ] **Step 10: 提交**

```bash
git add rhodes_fast/gui.py tests/test_gui_algorithm.py
git commit -m "feat: choose the control algorithm per profile in the panel

Parameter fields are generated from what the algorithm declares rather
than hardcoded, so an algorithm added later needs no GUI change. The
dropdown is disabled while running because algorithms are constructed at
pipeline startup.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## 验收

全部任务完成后：

1. `.venv/Scripts/python.exe -m unittest discover -s tests` → `Ran 159 tests` `OK`
2. `.venv/Scripts/python.exe -m rhodes_fast --config settings.txt --check` → UDP 与 KMBox 均正常（管线或界面在跑时会占端口，需先停止）
3. 把两个控制方案设成不同算法（例如方案 1 用 `p`、方案 2 用 `inflight_ff`，后者 kp_max 调到 0.35），启动后进游戏，按两个不同触发键对同一个目标拉枪，对比手感。

## 下一个计划

设计文档剩余部分：调校 JSON 导入导出、算法库管理（导入 .py、查看源码、改名、删除、`installed.json` 注册表）、导入时对比 `measured_loop_ms`。
