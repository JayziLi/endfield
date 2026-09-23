"""表单在 Python 侧的那份真相。

tkinter 那边真相在 tk 变量里, 这边在 FormBridge 里 —— JS 只是它的一个视图:
控件一动就 set_field 推上来, 要整片换 (载入预设、切算法) 时由 Python 把整份
状态推回去。

反过来 (JS 持有真相, 要用的时候读一遍 DOM) 也能做, 但热推每动一下滑条就要问
一次「现在整张表单是什么」, 保存、另存为、组子进程命令行也都要 —— 那样每一次
都得走一个异步往返, 而 Prompter 那条路还是同步的。

纯 Python: 不碰 webview, 不碰文件系统。
"""

from __future__ import annotations

import types
import typing
from dataclasses import fields, replace
from typing import Any, get_type_hints

from ..gui_core.state import (
    FormState,
    Labels,
    ProfileFormState,
    algorithm_param_specs,
)


class UnknownField(KeyError):
    """路径指向一个不存在的字段。

    静默忽略的症状是「这个控件没用」, 没有任何线索指向拼写错误 —— 而 JS 那边
    的路径是手写的字符串, 打错太容易了。
    """


# get_type_hints 而不是 dataclasses.fields(...).type: state.py 顶上有
# `from __future__ import annotations`, 注解因此是字符串 —— field.type 拿到的是
# 'str' 这个字符串, 不是 str 这个类。用它做 is 比较会全部落空, _coerce 静默
# 返回原值, 于是端口号是个 int 而没人发现。实测过。
_FORM_TYPES = get_type_hints(FormState)
_PROFILE_TYPES = get_type_hints(ProfileFormState)


def _coerce(declared: Any, value: Any) -> Any:
    """按 dataclass 声明的类型转换。

    JS 送上来的只有 number / boolean / string 三种, 而 FormState 里的类型是
    挑过的: udp_port 是 str (留着用户打的原文, 非法输入要走到「设置无法保存」
    那个弹窗), confidence 是 float。不按声明转的话, number 输入框会把端口变成
    int, 那条解析路径就被绕过去了。

    str | None 的字段 (单机模式那几个, None = 旧界面没这个控件) 按 str 转:
    新界面送上来的一定是个值, 不会是 None。
    """
    if isinstance(declared, types.UnionType) or typing.get_origin(declared) is typing.Union:
        concrete = [arg for arg in typing.get_args(declared) if arg is not type(None)]
        if len(concrete) == 1:
            declared = concrete[0]
    if declared is bool:
        return bool(value)
    if declared is float:
        return float(value)
    if declared is int:
        return int(value)
    if declared is str:
        return str(value)
    return value


class FormBridge:
    """当前这张表单。JS 那边的每一次改动都经过 set_field 落到这里。"""

    def __init__(self, state: FormState) -> None:
        self.state = state

    def replace_state(self, state: FormState) -> None:
        """整片换。载入预设走这条路 —— 一个字段一个字段地 set 会让界面闪一串
        中间状态, 而且中途任何一步抛异常就停在半新半旧上。"""
        self.state = state

    def set_field(self, path: str, value: Any) -> None:
        parts = path.split(".")
        if parts[0] == "profiles":
            self._set_profile_field(parts, value)
            return
        if len(parts) != 1 or parts[0] not in _FORM_TYPES:
            raise UnknownField(path)
        name = parts[0]
        self.state = replace(self.state, **{name: _coerce(_FORM_TYPES[name], value)})

    def _set_profile_field(self, parts: list[str], value: Any) -> None:
        path = ".".join(parts)
        if len(parts) < 3 or parts[1] not in ("0", "1"):
            raise UnknownField(path)
        index = int(parts[1])
        profile = self.state.profiles[index]

        if parts[2] == "algorithm_params":
            if len(parts) != 4:
                raise UnknownField(path)
            name = parts[3]
            # 参数名是跟着算法走的。换算法之后 JS 那边残留的旧路径必须报出来,
            # 不然那个值会悄悄加进 algorithm_params, 跟着存进 settings.txt,
            # 而新算法根本不认识它。
            if name not in profile.algorithm_params:
                raise UnknownField(path)
            params = dict(profile.algorithm_params)
            params[name] = float(value)
            updated = replace(profile, algorithm_params=params)
        else:
            if len(parts) != 3 or parts[2] not in _PROFILE_TYPES:
                raise UnknownField(path)
            name = parts[2]
            updated = replace(profile, **{name: _coerce(_PROFILE_TYPES[name], value)})

        profiles = list(self.state.profiles)
        profiles[index] = updated
        # 写回成两元组, 不是 list: FormState.profiles 的注解是个两元组, 而
        # form_state_to_config 按下标读。换成 list 的话注解就是假的。
        self.state = replace(self.state, profiles=(profiles[0], profiles[1]))

    def as_payload(self) -> dict:
        """整份表单, JSON 可序列化。

        不用 dataclasses.asdict: 它会把 profiles 那个元组原样留成 tuple, 而
        json.dumps 虽然接受 tuple, 读回来是 list —— 于是「推过去的」和「读回来
        的」不是同一个东西, 比对时会莫名其妙。这里显式转 list。

        algorithm_params 逐个复制: 直接放引用的话, JS 那边的一次刷新会顺手改掉
        Python 的真相, 而那是最难查的一类。
        """
        payload = {field.name: getattr(self.state, field.name) for field in fields(FormState)}
        payload["profiles"] = [
            {field.name: getattr(profile, field.name) for field in fields(ProfileFormState)}
            for profile in self.state.profiles
        ]
        for profile in payload["profiles"]:
            profile["algorithm_params"] = dict(profile["algorithm_params"])
        return payload


def choice_payload(labels: Labels) -> dict[str, list[str]]:
    """每个下拉框的选项。给的是显示标签, 不是存储值。

    塞存储值进去的话用户看到的是 auto / side1, 而且当前值 (标签) 对不上任何
    一个选项, 每次打开都跳回第一项。
    """
    return {
        name: list(getattr(labels, name))
        for name in (
            "provider", "output_format", "input_mode", "language", "trigger", "algorithm",
            "desktop_backend", "mouse_output",
        )
    }


def param_payload(algorithm_label: str, labels: Labels) -> list[dict]:
    """某个算法的参数契约, 给 02 屏动态生成控件用。

    收的是**显示标签** (表单里存的就是标签), 内部转成标识再查注册表。算法不
    存在时返回空列表 —— settings.txt 指着一个已删掉的算法是真实场景, 抛异常的
    话整个 02 屏画不出来, 用户连改回去的机会都没有。
    """
    name = labels.algorithm.get(algorithm_label)
    if name is None:
        return []
    return [
        {
            "name": spec.name,
            "label": spec.label,
            "default": spec.default,
            "minimum": spec.minimum,
            "maximum": spec.maximum,
            "step": spec.step,
            "slider": spec.slider,
            "advanced": spec.advanced,
        }
        for spec in algorithm_param_specs(name)
    ]
