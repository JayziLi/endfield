from __future__ import annotations

from typing import Mapping

from .builtin import BUILTIN_ALGORITHMS
from .contract import Algorithm, resolve_params
from .human import HUMAN_ALGORITHMS


class UnknownAlgorithm(LookupError):
    def __init__(self, name: str) -> None:
        super().__init__(f"未知的控制算法：{name}")
        self.name = name


# 用户自己装的算法。进程级的一份, 由界面和管线在启动时各填一次。
_INSTALLED: dict[str, type] = {}


def set_installed_algorithms(mapping: Mapping[str, type]) -> None:
    _INSTALLED.clear()
    _INSTALLED.update(mapping)


def available_algorithms() -> dict[str, type]:
    merged = dict(_INSTALLED)
    # 内置最后写入: 安装时已经挡掉了和内置重名的 NAME, 这里再兜一层, 内置永不被顶掉。
    # human 那一份单独一个元组是为了避开循环导入: human.py 要用 builtin 的
    # Feedforward 当基类, 所以 builtin.py 不能反过来引用它。
    merged.update(
        {
            algorithm.NAME: algorithm
            for algorithm in (*BUILTIN_ALGORITHMS, *HUMAN_ALGORITHMS)
        }
    )
    return merged


def create_algorithm(name: str, params: Mapping[str, float]) -> Algorithm:
    algorithms = available_algorithms()
    if name not in algorithms:
        raise UnknownAlgorithm(name)
    factory = algorithms[name]
    return factory(resolve_params(factory.PARAMS, params))
