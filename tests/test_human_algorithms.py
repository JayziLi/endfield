"""拟人化运动的三个算法。

现有算法都是「误差 x 动态 kp」的指数衰减接近: 越近越慢, 路径笔直。这三个的目的
是把轨迹变得像人手, 同时不增加死区延迟 —— 所以一律是增量式的, 每帧重新看误差,
绝不预先规划路径。
"""

from __future__ import annotations

import math
import statistics
import unittest

from rhodes_fast.aim_algorithms import Observation, create_algorithm, dynamic_kp
from rhodes_fast.algorithm_library import builtin_names
from rhodes_fast.aim_algorithms.builtin import _landed_command
from rhodes_fast.gui_core.state import algorithm_param_specs

_FF = {"loop_delay_frames": 8.0, "gain": 1.0, "velocity_smoothing": 0.25}


def observe(error_x=0.0, error_y=0.0, *, commands=(), frame_index=0, dt=1 / 241) -> Observation:
    return Observation(
        error_x=error_x,
        error_y=error_y,
        dt=dt,
        frame_index=frame_index,
        recent_commands=tuple(commands),
        kp_min=0.035,
        kp_max=0.125,
        kp_growth=0.047,
    )


def drive(
    algorithm, errors: list[tuple[float, float]], *, feedback: bool = True
) -> list[tuple[float, float]]:
    """喂一串误差, 收集每帧的输出。

    feedback=False 时 recent_commands 固定不变, 不把自己的输出喂回去。要对比两个
    算法的*单帧差值*时必须这样: 否则两边的指令历史会分叉, 前馈的速度估计跟着分叉,
    差值里就混进了基础算法的贡献, 不再是被测那一项。
    """
    out = []
    commands: list[tuple[int, int]] = []
    for index, (ex, ey) in enumerate(errors):
        history = (0, 0) if not feedback else None
        step = algorithm.compute(
            observe(
                ex,
                ey,
                commands=((history,) * 12 if history is not None else tuple(commands)),
                frame_index=index,
            )
        )
        out.append(step)
        commands.append((round(step[0]), round(step[1])))
    return out


def _straight_run(count: int = 40, start: float = 100.0) -> list[tuple[float, float]]:
    """一条笔直靠近的误差序列。每帧误差减一点, 模拟正在拉枪。"""
    return [(start - index * 2.0, 0.0) for index in range(count)]


class WindOffIsUntouchedTests(unittest.TestCase):
    """wind_strength=0 时必须和 feedforward 逐位相同。

    这是这三个算法里最重要的一条护栏。新功能悄悄改掉现有手感是最难查的那种坏法:
    你会以为是自己参数调错了, 而实际上是一个本该关着的功能在偷偷起作用。
    """

    def test_zero_wind_matches_feedforward_bit_for_bit(self) -> None:
        errors = _straight_run()
        plain = drive(create_algorithm("feedforward", _FF), errors)
        windy = drive(
            create_algorithm(
                "feedforward_wind",
                {**_FF, "wind_strength": 0.0, "wind_decay_px": 40.0, "seed": 7.0},
            ),
            errors,
        )
        self.assertEqual(plain, windy)


class WindDeterminismTests(unittest.TestCase):
    def test_the_same_seed_gives_the_same_run(self) -> None:
        # 没有这一条, 后面每个断言都是随机的。
        params = {**_FF, "wind_strength": 0.5, "wind_decay_px": 40.0, "seed": 12345.0}
        errors = _straight_run()
        first = drive(create_algorithm("feedforward_wind", params), errors)
        second = drive(create_algorithm("feedforward_wind", params), errors)
        self.assertEqual(first, second)

    def test_different_seeds_give_different_runs(self) -> None:
        errors = _straight_run()
        base = {**_FF, "wind_strength": 0.5, "wind_decay_px": 40.0}
        first = drive(create_algorithm("feedforward_wind", {**base, "seed": 1.0}), errors)
        second = drive(create_algorithm("feedforward_wind", {**base, "seed": 2.0}), errors)
        self.assertNotEqual(first, second)


class WindIsPerpendicularTests(unittest.TestCase):
    """风必须垂直于误差方向。

    不垂直就只是在「快一点 / 慢一点」上加噪声 —— 那不会让路径变弯, 只会让收敛
    变得毛糙, 白掉精度还换不来观感。
    """

    def _contributions(self) -> list[tuple[float, float, float, float]]:
        errors = _straight_run()
        plain = drive(create_algorithm("feedforward", _FF), errors, feedback=False)
        windy = drive(
            create_algorithm(
                "feedforward_wind",
                {**_FF, "wind_strength": 0.6, "wind_decay_px": 40.0, "seed": 99.0},
            ),
            errors,
            feedback=False,
        )
        rows = []
        for (ex, ey), (px, py), (wx, wy) in zip(errors, plain, windy):
            rows.append((ex, ey, wx - px, wy - py))
        return rows

    def test_the_wind_adds_nothing_along_the_error_direction(self) -> None:
        for ex, ey, dx, dy in self._contributions():
            distance = math.hypot(ex, ey)
            if distance < 1e-9:
                continue
            along = (dx * ex + dy * ey) / distance
            self.assertAlmostEqual(along, 0.0, places=9)

    def test_the_wind_actually_does_something_sideways(self) -> None:
        # 上面那条如果实现成「什么都不加」也会过。这条堵住那个洞。
        sideways = [abs(dy) for _ex, _ey, _dx, dy in self._contributions()]
        self.assertGreater(max(sideways), 0.05)


class WindGoesToZeroWhenCloseTests(unittest.TestCase):
    """近了必须归零。用户明确要求跟踪精度不许受影响。"""

    def _deflection(self, error_x: float) -> float:
        errors = [(error_x, 0.0)] * 30
        plain = drive(create_algorithm("feedforward", _FF), errors, feedback=False)
        windy = drive(
            create_algorithm(
                "feedforward_wind",
                {**_FF, "wind_strength": 0.6, "wind_decay_px": 40.0, "seed": 4.0},
            ),
            errors,
            feedback=False,
        )
        return max(abs(w[1] - p[1]) for p, w in zip(plain, windy))

    def test_inside_the_dead_zone_there_is_practically_no_wind(self) -> None:
        self.assertLess(self._deflection(2.0), 0.01)

    def test_far_away_there_is_plenty_of_wind(self) -> None:
        self.assertGreater(self._deflection(150.0), 0.2)

    def test_the_wind_dies_out_while_closing_in_from_far_away(self) -> None:
        """从远处一路拉近, 到了近处风必须已经没了。

        这是「近了归零」真正要保证的场景, 而且是 growth 那一项唯一起作用的地方。
        光看「一直待在 2px」测不出来: 那种情况下 distance < wind_scale, 随机量
        从头到尾没注入过, 风恒为 0 —— 测试会因为别的原因通过, 把 growth 删掉也照过。
        """
        errors = [(150.0 - index * 1.5, 0.0) for index in range(100)]
        plain = drive(create_algorithm("feedforward", _FF), errors, feedback=False)
        windy = drive(
            create_algorithm(
                "feedforward_wind",
                {**_FF, "wind_strength": 0.6, "wind_decay_px": 40.0, "seed": 31.0},
            ),
            errors,
            feedback=False,
        )
        lateral = [abs(w[1] - p[1]) for p, w in zip(plain, windy)]
        far = max(lateral[:40])
        close = max(lateral[-15:])
        self.assertGreater(far, 0.3)
        self.assertLess(close, far * 0.05)

    def test_the_decay_distance_knob_actually_changes_the_falloff(self) -> None:
        # wind_decay_px 是面板上的旋钮。没有这一条, 把它写死成默认值 40 也照过。
        errors = [(20.0, 0.0)] * 40
        plain = drive(create_algorithm("feedforward", _FF), errors, feedback=False)

        def deflection(scale: float) -> float:
            windy = drive(
                create_algorithm(
                    "feedforward_wind",
                    {**_FF, "wind_strength": 0.6, "wind_decay_px": scale, "seed": 5.0},
                ),
                errors,
                feedback=False,
            )
            return max(abs(w[1] - p[1]) for p, w in zip(plain, windy))

        # 20px 处: 归零距离 10 时 growth≈0.86, 归零距离 100 时 growth≈0.18。
        self.assertGreater(deflection(10.0), deflection(100.0) * 3)

    def test_the_deflection_grows_with_distance(self) -> None:
        # 拿三档比单调性, 而不是拿一个倍数。10px 那种低于 wind_decay_px 的距离风
        # 恒为 0, 用它当分母的话断言会变得空洞(任何大于 0 的数都能过)。
        inside = self._deflection(2.0)
        middle = self._deflection(60.0)
        outside = self._deflection(150.0)
        self.assertLess(inside, middle)
        self.assertLess(middle, outside)


class WindIsSmoothTests(unittest.TestCase):
    """风要跨帧平滑, 否则就只是每帧独立的抖动, 不是风。

    判据用滞后 1 的自相关: 独立随机是 0, 而 WindMouse 那套 w/sqrt(3)+随机 的
    演化方式理论上是 1/sqrt(3) ≈ 0.577。
    """

    def test_consecutive_wind_values_are_correlated_not_independent(self) -> None:
        errors = [(150.0, 0.0)] * 400
        plain = drive(create_algorithm("feedforward", _FF), errors, feedback=False)
        windy = drive(
            create_algorithm(
                "feedforward_wind",
                {**_FF, "wind_strength": 0.6, "wind_decay_px": 40.0, "seed": 2026.0},
            ),
            errors,
            feedback=False,
        )
        lateral = [w[1] - p[1] for p, w in zip(plain, windy)]
        mean = statistics.fmean(lateral)
        centred = [value - mean for value in lateral]
        variance = sum(value * value for value in centred)
        lag_one = sum(a * b for a, b in zip(centred, centred[1:]))
        self.assertGreater(lag_one / variance, 0.3)


if __name__ == "__main__":
    unittest.main()


def chase(
    algorithm,
    *,
    start: float = 100.0,
    target_speed: float = 0.0,
    lag: int = 0,
    frames: int = 300,
) -> list[float]:
    """闭环追踪, 返回每帧的真实误差。

    lag=0 时没有观测延迟, 用来测控制律本身的基本正确性(收敛、阻尼、归一化重力)。
    带延迟的表现交给 tools/sim_strafe.py —— 那边有噪声、抖动和真实的目标运动。
    """
    target = start
    camera = 0.0
    commands: list[tuple[int, int]] = []
    history: list[float] = [target - camera]
    errors: list[float] = []
    for index in range(frames):
        target += target_speed
        seen = history[max(0, len(history) - 1 - lag)]
        step_x, _ = algorithm.compute(
            observe(seen, 0.0, commands=tuple(commands), frame_index=index)
        )
        camera += step_x
        commands.append((round(step_x), 0))
        history.append(target - camera)
        errors.append(target - camera)
    return errors


_WM = {
    "gravity": 9.0,
    "wind": 0.0,
    "max_step": 15.0,
    "damp_px": 12.0,
    "lead_frames": 0.0,
    "seed": 3.0,
}


class WindMouseConvergesTests(unittest.TestCase):
    """归一化重力 + 恒定速度上限很容易做成永久振荡, 所以基本收敛性得先钉住。"""

    def test_it_settles_into_the_dead_zone_on_a_still_target(self) -> None:
        errors = chase(create_algorithm("windmouse", _WM), frames=200)
        self.assertLess(abs(errors[-1]), 2.0)

    def test_it_does_not_keep_oscillating_after_arriving(self) -> None:
        errors = chase(create_algorithm("windmouse", _WM), frames=300)
        tail = [abs(value) for value in errors[-60:]]
        self.assertLess(max(tail), 3.0)


class WindMouseDeterminismTests(unittest.TestCase):
    def test_with_no_wind_the_trajectory_is_fully_deterministic(self) -> None:
        # 原版在速度截断里也用了一次 random(), 那样 wind=0 也还是随机的, 没法把
        # 「风」的贡献单独隔离出来。这一条钉住「wind 是唯一的随机来源」。
        first = chase(create_algorithm("windmouse", _WM))
        second = chase(create_algorithm("windmouse", _WM))
        self.assertEqual(first, second)

    def test_wind_actually_perturbs_the_path(self) -> None:
        calm = chase(create_algorithm("windmouse", _WM))
        windy = chase(create_algorithm("windmouse", {**_WM, "wind": 3.0}))
        self.assertNotEqual(calm, windy)


class WindMouseGravityIsNormalisedTests(unittest.TestCase):
    """这是它和现有算法的本质区别: 重力大小恒定, 不随距离变。

    写错就退化成比例控制, 而「匀速冲过去再刹一脚」正是要治的机械感 —— 退化了
    就什么都没治到, 只是多了一个名字不同的比例控制。
    """

    def _first_step(self, distance: float) -> float:
        algorithm = create_algorithm("windmouse", _WM)
        step_x, _ = algorithm.compute(observe(distance, 0.0))
        return step_x

    def test_the_first_step_is_the_same_from_far_and_from_near(self) -> None:
        self.assertAlmostEqual(self._first_step(150.0), self._first_step(30.0), places=9)

    def test_proportional_control_would_not_behave_that_way(self) -> None:
        # 对照: 比例控制的第一步和距离成正比, 差出好几倍。
        far, _ = create_algorithm("p", {}).compute(observe(150.0, 0.0))
        near, _ = create_algorithm("p", {}).compute(observe(30.0, 0.0))
        self.assertGreater(far, near * 3)


class WindMouseDampingTests(unittest.TestCase):
    def test_the_step_shrinks_once_inside_the_damping_distance(self) -> None:
        errors = chase(create_algorithm("windmouse", _WM), frames=200)
        steps = [abs(errors[i] - errors[i + 1]) for i in range(len(errors) - 1)]
        far = max(steps[:10])
        close = max(steps[-30:])
        self.assertGreater(far, close * 3)


class WindMouseLeadTests(unittest.TestCase):
    def test_the_lead_reduces_the_lag_on_a_moving_target(self) -> None:
        without = chase(
            create_algorithm("windmouse", _WM), target_speed=2.0, lag=7, frames=400
        )
        with_lead = chase(
            create_algorithm("windmouse", {**_WM, "lead_frames": 8.0}),
            target_speed=2.0,
            lag=7,
            frames=400,
        )
        settled = slice(-120, None)
        self.assertLess(
            statistics.fmean(abs(v) for v in with_lead[settled]),
            statistics.fmean(abs(v) for v in without[settled]),
        )

    def test_a_detection_jump_is_not_turned_into_false_lead(self) -> None:
        algorithm = create_algorithm("windmouse", {**_WM, "wind": 0.0, "lead_frames": 8.0})
        commands = ((2, 0),) * 12
        for index in range(20):
            algorithm.compute(observe(50.0, 0.0, commands=commands, frame_index=index))

        algorithm.compute(observe(110.0, 0.0, commands=commands, frame_index=20))

        self.assertEqual(algorithm._velocity_x, 0.0)
        self.assertEqual(algorithm._velocity_y, 0.0)


_BEZ = {**_FF, "arc_strength": 0.35, "commit_frames": 0.0, "seed": 8.0}


class BezierOffIsUntouchedTests(unittest.TestCase):
    def test_zero_arc_matches_feedforward_bit_for_bit(self) -> None:
        errors = _straight_run()
        plain = drive(create_algorithm("feedforward", _FF), errors)
        curved = drive(
            create_algorithm("feedforward_bezier", {**_BEZ, "arc_strength": 0.0}), errors
        )
        self.assertEqual(plain, curved)


class BezierDegeneratesToAStraightLineTests(unittest.TestCase):
    """arc_strength=0 时曲线必须真的退化成直线。

    光有「和 feedforward 逐位相同」那一条不够: 它走的是提前返回那条捷径, 根本没经过
    曲线代码。以后有谁觉得那个提前返回冗余(它确实只是优化 —— offset 为 0 时
    aim*t - 0.0 在 IEEE754 下就是 aim*t)把它删掉, 曲线一侧真坏了也没人拦。
    """

    def test_the_bezier_identity_holds_with_no_offset(self) -> None:
        # 整个设计都压在这个恒等式上: 控制点取 aim/3 和 aim*2/3 时 B(t) == t*aim,
        # 所以弧线是纯横向叠加, 沿轴速度曲线和 feedforward 一模一样。
        from rhodes_fast.aim_algorithms.human import _bezier

        for t_value in (0.0, 0.05, 0.25, 0.5, 0.75, 1.0):
            with self.subTest(t=t_value):
                x, y = _bezier(80.0, -30.0, 0.0, t_value)
                self.assertAlmostEqual(x, 80.0 * t_value, places=12)
                self.assertAlmostEqual(y, -30.0 * t_value, places=12)

    def test_no_arc_means_no_sideways_motion_even_through_the_curve(self) -> None:
        # commit_frames>0 绕开了提前返回, 所以这一条真的走曲线那条路。
        steps = drive(
            create_algorithm(
                "feedforward_bezier",
                {**_BEZ, "arc_strength": 0.0, "commit_frames": 10.0},
            ),
            [(120.0, 0.0)] * 20,
            feedback=False,
        )
        for _step_x, step_y in steps:
            self.assertAlmostEqual(step_y, 0.0, places=12)


class BezierStaysClosedLoopTests(unittest.TestCase):
    """commit_frames=0 必须是闭环。这是整个设计的要点。

    浏览器自动化那边的贝塞尔(ghost-cursor 等)是先把 A 到 B 的路径算完再走。对静止
    按钮合理, 对 482px/s 的目标是灾难: 一是开环, 提交 N 帧就是 N 帧不看画面, 正是
    这个项目一直在打的死区延迟; 二是规划时的终点本来就是 7 帧前的位置。
    """

    HELD = 8  # 跳变发生在第 8 帧, 刻意不是 commit_frames 的整数倍, 让它落在计划中段

    def _outputs_after_a_jump(self, commit_frames: float) -> list[tuple[float, float]]:
        # arc_strength 必须设 0: 误差是 (60, 0) 时弧线的横向恰好落在 y 轴上, 留着它
        # 就和「对跳变的响应」混在同一个分量里, 分不出是哪个在动。
        algorithm = create_algorithm(
            "feedforward_bezier",
            {**_BEZ, "arc_strength": 0.0, "commit_frames": commit_frames},
        )
        errors = [(60.0, 0.0)] * self.HELD + [(60.0, 140.0)] * 12
        return drive(algorithm, errors, feedback=False)[self.HELD :]

    def test_a_target_jump_shows_up_in_the_very_next_frame(self) -> None:
        after = self._outputs_after_a_jump(0.0)
        # 目标突然跳到斜上方, 下一帧的 y 分量就该动起来。
        self.assertGreater(abs(after[0][1]), 1.0)

    def test_committing_frames_really_does_go_open_loop(self) -> None:
        # 这个旋钮的意义就是让「预先承诺路径」的代价能被亲手感受到, 而不是只能信我。
        after = self._outputs_after_a_jump(6.0)
        ys = [abs(step[1]) for step in after[:4]]
        self.assertLess(max(ys), 0.5)

    def test_the_committed_plan_eventually_expires_and_looks_again(self) -> None:
        # 计划在第 0、6、12 帧换。跳变在第 8 帧, 所以第 8~11 帧还在旧计划里,
        # 第 12 帧才重新看画面。
        after = self._outputs_after_a_jump(6.0)
        self.assertGreater(abs(after[4][1]), 1.0)


class BezierArcIsOneSidedTests(unittest.TestCase):
    """弧线必须只往一侧鼓。

    ghost-cursor 的经验: 控制点在直线两侧随机取会出现扭曲的 S 形怪曲线。两个控制点
    同号, 横向位移 3t(1-t)o 就恒不换号 —— 鼓出去再回来, 但绝不穿到另一侧。
    """

    def test_the_lateral_drift_never_crosses_to_the_other_side(self) -> None:
        algorithm = create_algorithm(
            "feedforward_bezier", {**_BEZ, "arc_strength": 0.6, "commit_frames": 12.0}
        )
        errors = [(120.0, 0.0)] * 12
        steps = drive(algorithm, errors, feedback=False)
        lateral = 0.0
        seen: list[float] = []
        for _step_x, step_y in steps:
            lateral += step_y
            seen.append(lateral)
        moved = [value for value in seen if abs(value) > 1e-9]
        self.assertTrue(moved, "弧线根本没鼓出去")
        signs = {value > 0 for value in moved}
        self.assertEqual(len(signs), 1)


def closed_flick(params, start=100.0, frames=90) -> list[float]:
    """闭环拉一次枪, 返回每帧累积的横向偏离(相对直线)。"""
    algorithm = create_algorithm("feedforward_bezier", params)
    error_x, error_y = start, 0.0
    lateral: list[float] = []
    commands: list[tuple[int, int]] = []
    for index in range(frames):
        step_x, step_y = algorithm.compute(
            observe(error_x, error_y, commands=tuple(commands), frame_index=index)
        )
        error_x -= step_x
        error_y -= step_y
        lateral.append(-error_y)
        commands.append((round(step_x), round(step_y)))
    return lateral


class BezierTracesOneArcTests(unittest.TestCase):
    """横向偏离必须走成 3·offset·p(1-p): 鼓出去一次, 到达时回到直线上。

    原来的实现每帧拿 t = kp(一个常数, 约 0.1)去求值一条刚规划的曲线, 所以永远停在
    曲线起点附近那一小段, 从来没沿着它往前走过 —— 弧形退化成一个符号随机的横向偏置,
    实测横向换号 4 次、跟踪时抖动是纯比例控制的 20 倍。用户的原话是「贝塞尔抖得
    尤其厉害」。

    修法是记住**进度**, 而且从观测距离反推(p = 1 - 当前距离/起始距离): 既真的在走
    曲线, 又全程闭环, 还不累积漂移。
    """

    def _lateral(self, strength: float = 0.5) -> list[float]:
        return closed_flick({**_BEZ, "arc_strength": strength})

    def test_the_lateral_drift_never_changes_sign(self) -> None:
        raw = self._lateral()
        peak = max(abs(value) for value in raw)
        # 按峰值的一成过滤: 控制器追那个偏出去的设定点有约 1/kp 帧的滞后, 末尾会轻微
        # 过零。要钉的是「不会鼓向另一侧」, 不是浮点级别的零。
        lateral = [value for value in raw if abs(value) > peak * 0.1]
        self.assertTrue(lateral, "弧线根本没鼓出去")
        flips = sum(1 for a, b in zip(lateral, lateral[1:]) if a * b < 0)
        self.assertEqual(flips, 0)

    def test_it_comes_back_to_the_line_on_arrival(self) -> None:
        lateral = self._lateral()
        peak = max(abs(value) for value in lateral)
        self.assertGreater(peak, 1.0)
        # 到达时横向必须归零 —— 这是真弧线自带的性质, 不需要额外的距离衰减。
        # 回归速度受控制器自身的时间常数(约 1/kp ≈ 8 帧)支配, 所以留到 15%。
        self.assertLess(abs(lateral[-1]), peak * 0.15)

    def test_the_bulge_peaks_in_the_middle_of_the_move(self) -> None:
        lateral = [abs(value) for value in self._lateral()]
        peak_at = lateral.index(max(lateral))
        # 一路单调增(停在起点那一段)或一路单调减都说明没在走曲线。
        self.assertGreater(peak_at, 1)
        self.assertLess(peak_at, len(lateral) - 2)

    def test_the_arc_also_works_on_a_diagonal_flick(self) -> None:
        """斜向拉枪也得鼓成一条弧。

        其余测试都沿 +x 拉, 那是退化情形 —— 弦正好是 (1,0), 于是「弦法向」和「当前
        误差法向」分不出来, 弦的几何根本没被验证。而真实拉枪全是斜的。
        """
        algorithm = create_algorithm(
            "feedforward_bezier", {**_BEZ, "arc_strength": 0.5}
        )
        error_x = error_y = 100.0
        span = math.hypot(error_x, error_y)
        axis = (error_x / span, error_y / span)
        commands: list[tuple[int, int]] = []
        sideways: list[float] = []
        for index in range(90):
            step_x, step_y = algorithm.compute(
                observe(error_x, error_y, commands=tuple(commands), frame_index=index)
            )
            error_x -= step_x
            error_y -= step_y
            commands.append((round(step_x), round(step_y)))
            # 误差里垂直于弦的那一份就是偏离直线路径的量。
            sideways.append(-error_x * axis[1] + error_y * axis[0])
        peak = max(abs(value) for value in sideways)
        self.assertGreater(peak, 1.0)
        # 贝塞尔的定义性质: P0 在弦上。也就是这一次移动的头几帧必须还贴着直线走,
        # 弧是后面才鼓起来的。弦取错(比如沿用上一次的方向)时进度的起点就不是 0,
        # 弧线会一上来就已经偏出去 —— 而它照样单号、照样收口, 只看形状抓不住。
        self.assertAlmostEqual(sideways[0], 0.0, places=9)
        bulged = [value for value in sideways if abs(value) > peak * 0.1]
        flips = sum(1 for a, b in zip(bulged, bulged[1:]) if a * b < 0)
        self.assertEqual(flips, 0)
        self.assertLess(abs(sideways[-1]), peak * 0.15)

    def test_tracking_jitter_is_far_below_the_old_wobble(self) -> None:
        # 跟踪阶段(误差小且不变)横向必须基本不动。原来的实现在这里抖得最凶。
        errors = [(18.0, 0.0)] * 200
        plain = drive(create_algorithm("feedforward", _FF), errors, feedback=False)
        curved = drive(
            create_algorithm("feedforward_bezier", {**_BEZ, "arc_strength": 0.5}),
            errors,
            feedback=False,
        )
        lateral = [c[1] - p[1] for p, c in zip(plain, curved)]
        self.assertLess(statistics.pstdev(lateral), 0.15)


class BezierArcStrengthTests(unittest.TestCase):
    def test_a_stronger_arc_bulges_further(self) -> None:
        # 必须用真的收敛过程量。把误差按住不动的话准心根本没有「进度」, 也就没有弧线
        # —— 那是新设计的正确行为, 不是缺陷。
        def bulge(strength: float) -> float:
            lateral = closed_flick({**_BEZ, "arc_strength": strength})
            return max(abs(value) for value in lateral)

        self.assertGreater(bulge(0.6), bulge(0.15) * 2)

    def test_the_same_seed_gives_the_same_arc(self) -> None:
        errors = _straight_run()
        first = drive(create_algorithm("feedforward_bezier", _BEZ), errors)
        second = drive(create_algorithm("feedforward_bezier", _BEZ), errors)
        self.assertEqual(first, second)


class VelocityOutlierTests(unittest.TestCase):
    """误差突然跳一大截时不许当成「目标在飞」。

    目标换人(上报的切换, 以及位置歧义导致的静默换身份)会让误差瞬间跳几十像素。前馈
    把这个跳变乘上延迟帧数, 一次切换就变成一记暴力甩枪 —— 跳 60px 时前馈量 120px,
    单帧走 17px 而正确值约 6px。

    修在速度估计器里而不是让管线去通知: 静默换身份根本不置 changed, 通知不到; 而
    检测抖动、漏检回来的第一帧也会造成同样的跳变。一个机制覆盖全部来源。
    """

    def _lead_after_jump(self, jump: float) -> float:
        algorithm = create_algorithm("feedforward", _FF)
        commands = ((2, 0),) * 12
        for index in range(20):
            algorithm.compute(observe(50.0, 0.0, commands=commands, frame_index=index))
        step_x, _ = algorithm.compute(
            observe(50.0 + jump, 0.0, commands=commands, frame_index=20)
        )
        return step_x

    def test_a_plausible_frame_to_frame_move_still_feeds_the_lead(self) -> None:
        # 实测目标横移 482px/s, 241fps 下每帧 2px。这种量必须照常进速度估计。
        quiet = self._lead_after_jump(0.0)
        moving = self._lead_after_jump(3.0)
        self.assertGreater(moving, quiet)

    def test_a_sixty_pixel_jump_does_not_turn_into_a_violent_flick(self) -> None:
        jumped = self._lead_after_jump(60.0)
        # 没有前馈量时这一帧应该约等于 110 x kp。允许一点余量, 但绝不能是 3 倍。
        straight = 110.0 * dynamic_kp(110.0, 0.035, 0.125, 0.047)
        self.assertLess(jumped, straight * 1.3)

    def test_the_lead_comes_back_after_the_jump_settles(self) -> None:
        # 跳变之后要能重新建立速度估计, 不能永久失效。
        algorithm = create_algorithm("feedforward", _FF)
        commands = ((2, 0),) * 12
        algorithm.compute(observe(50.0, 0.0, commands=commands))
        algorithm.compute(observe(130.0, 0.0, commands=commands))  # 跳变
        error = 130.0
        for index in range(12):
            error += 3.0
            step_x, _ = algorithm.compute(
                observe(error, 0.0, commands=commands, frame_index=index)
            )
        plain_kp = dynamic_kp(error, 0.035, 0.125, 0.047)
        self.assertGreater(step_x, error * plain_kp * 1.05)


class ZeroIsReachableTest(unittest.TestCase):
    """每个旋钮都要能拧到 0。

    拧不到的那几个原来是这样卡住的: loop_delay_frames 最小 1, velocity_smoothing
    最小 0.01, wind_decay_px 最小 5, gravity / max_step / damp_px 各有自己的下限。
    界面上的滑条按 Param 的 minimum 画, 用户拖到头也到不了 0, 手打一个 0 会被夹回去。

    0 在这几个上都是有意义的设置 (不要前馈、不要末端阻尼、风不归零……), 而且都不会
    把算法弄崩 —— 下面那条测试就是钉这个的。
    """

    ZERO_PARAMS = {
        "feedforward": ("loop_delay_frames", "velocity_smoothing"),
        "inflight": ("loop_delay_frames",),
        "inflight_ff": ("loop_delay_frames", "velocity_smoothing"),
        "feedforward_wind": ("loop_delay_frames", "velocity_smoothing", "wind_decay_px"),
        "feedforward_bezier": ("loop_delay_frames", "velocity_smoothing"),
        "windmouse": ("gravity", "max_step", "damp_px"),
    }

    def test_every_listed_parameter_can_be_set_to_zero(self) -> None:
        for name, params in self.ZERO_PARAMS.items():
            specs = {spec.name: spec for spec in algorithm_param_specs(name)}
            for param in params:
                with self.subTest(algorithm=name, param=param):
                    self.assertEqual(specs[param].minimum, 0.0)

    def test_no_builtin_parameter_is_fenced_off_from_zero(self) -> None:
        """反过来再扫一遍。将来加参数时顺手写个非零下限的话, 这条会红 ——
        而症状本来只是「这个旋钮拧不到底」, 没人会为它提 bug。

        真的不能是 0 的参数就加进 allowed, 连同理由。
        """
        # 今天一个都没有。有除零风险的 —— 比如 examples/kalman_projectile.py 的
        # camera_scale, 它在算式里当除数 —— 是用户自己装的算法, 不归这里管, 也
        # 不该由这条测试去改别人文件里的下限。
        allowed: dict[tuple[str, str], str] = {}
        for name in builtin_names():
            for spec in algorithm_param_specs(name):
                if spec.minimum <= 0.0:
                    continue
                with self.subTest(algorithm=name, param=spec.name):
                    self.assertIn((name, spec.name), allowed)

    def test_every_algorithm_survives_all_of_its_minimums(self) -> None:
        """「能填 0」不能变成「填 0 就崩」。

        每个算法按它自己的最小值整套构造一遍, 跑一段真实的拉枪: 不许抛异常,
        输出必须是有限数。除零和 NaN 都在这里拦 —— NaN 尤其阴, 它会一路流进
        亚像素累加器, 之后这一局再也动不了, 而日志里一个字都没有。
        """
        errors = [(120.0 - index * 4.0, 40.0 - index * 1.5) for index in range(30)]
        for name in builtin_names():
            specs = algorithm_param_specs(name)
            params = {spec.name: spec.minimum for spec in specs}
            with self.subTest(algorithm=name, params=params):
                steps = drive(create_algorithm(name, params), errors)
                for step_x, step_y in steps:
                    self.assertTrue(math.isfinite(step_x) and math.isfinite(step_y), params)

    def test_every_algorithm_survives_all_of_its_maximums(self) -> None:
        """顺手把另一头也扫了。上限这边原来没人验过, 而用户拖滑条是两头都拖的。"""
        errors = [(120.0 - index * 4.0, 40.0 - index * 1.5) for index in range(30)]
        for name in builtin_names():
            specs = algorithm_param_specs(name)
            params = {spec.name: spec.maximum for spec in specs}
            with self.subTest(algorithm=name, params=params):
                steps = drive(create_algorithm(name, params), errors)
                for step_x, step_y in steps:
                    self.assertTrue(math.isfinite(step_x) and math.isfinite(step_y), params)


class ZeroLoopDelayTest(unittest.TestCase):
    """回路延迟填 0 = 不要前馈。"""

    def test_a_zero_lag_reads_no_landed_command(self) -> None:
        """_landed_command 往回数第 lag 条。lag=0 时 commands[-0] 就是 commands[0],
        也就是**最老**的那一条 —— Python 的 -0 等于 0。

        原来的 max(1, ...) 把这个下标问题挡住了, 代价是 0 根本填不进来。现在
        0 能填了, 这一支就得自己站得住: 没有延迟就没有「刚落地」的指令。
        """
        history = ((99, 99), (7, 7), (1, 1))
        self.assertEqual(_landed_command(history, 0), (0.0, 0.0))
        self.assertEqual(_landed_command(history, 1), (1.0, 1.0))

    def test_zero_lag_turns_feedforward_into_plain_proportional(self) -> None:
        """前馈量是 速度 x 延迟帧数, 延迟为 0 就没有前馈 —— 输出应该跟比例控制
        逐位相同。差一点点的话, 说明 0 那一档其实被悄悄夹成了 1。"""
        errors = [(100.0 - index * 3.0, 20.0) for index in range(20)]
        lead = drive(create_algorithm("feedforward", dict(_FF, loop_delay_frames=0.0)), errors)
        plain = drive(create_algorithm("p", {}), errors)
        self.assertEqual(lead, plain)

    def test_zero_lag_still_leaves_the_wind(self) -> None:
        """「不要前馈, 只要风」是一种真实的配法 —— 别的算法给不了。整条被夹成
        比例控制的话, 这个组合就没了。"""
        errors = [(100.0 - index * 3.0, 20.0) for index in range(20)]
        params = dict(_FF, loop_delay_frames=0.0, wind_strength=0.8, wind_decay_px=40.0, seed=7.0)
        windy = drive(create_algorithm("feedforward_wind", params), errors)
        plain = drive(create_algorithm("p", {}), errors)
        self.assertNotEqual(windy, plain)
