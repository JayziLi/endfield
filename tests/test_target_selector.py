"""目标选择的抗掉检行为。

实测(tools 里的探针): 检测掉帧率 5% 时真实目标切换达到 12.9 次/秒 —— 241fps 下
每 78 毫秒换一个人打。而把切换滞回从 12ms 提到 120ms 只能降到 9.1 次, 说明滞回不是
主要杠杆。

真正的原因是「漏一帧就交班」: 当前目标这一帧没检出时, 关联匹配会直接落到**另一个
目标**身上 —— 在关联半径内就静默接管(连 changed 都不置位), 超出就立刻跳到最近的那个,
完全绕过滞回。
"""

from __future__ import annotations

import unittest

from rhodes_fast.detector import Detection
from rhodes_fast.pipeline import TargetSelector

FRAME = 320
CENTER = FRAME * 0.5


def box(center_x: float, center_y: float, size: float = 20.0) -> Detection:
    half = size * 0.5
    return Detection(center_x - half, center_y - half, center_x + half, center_y + half, 0.9, 0)


class SelectorTestCase(unittest.TestCase):
    def pick(self, selector, detections):
        return selector.select(
            detections,
            frame_width=FRAME,
            frame_height=FRAME,
            target_y_ratio=0.5,
            fov_radius=150.0,
            target_class=0,
        )


class DroppedFrameTests(SelectorTestCase):
    """漏一帧不许把瞄准交给别人。"""

    def setUp(self) -> None:
        self.selector = TargetSelector()
        self.mine = box(CENTER + 20, CENTER)
        self.other = box(CENTER + 60, CENTER)
        self.assertIs(self.pick(self.selector, [self.mine, self.other]), self.mine)

    def test_one_missing_frame_does_not_hand_the_aim_to_the_other_target(self) -> None:
        # 这是整个问题的核心。掉一帧就换人打, 在 241fps 下每 80 毫秒就发生一次。
        self.assertIsNot(self.pick(self.selector, [self.other]), self.other)

    def test_while_the_target_is_missing_it_aims_at_nobody(self) -> None:
        # 宁可这一帧不动鼠标, 也不要瞄到另一个人身上。
        self.assertIsNone(self.pick(self.selector, [self.other]))

    def test_it_picks_the_target_back_up_when_it_reappears(self) -> None:
        for _ in range(5):
            self.pick(self.selector, [self.other])
        back = box(CENTER + 24, CENTER)
        self.assertIs(self.pick(self.selector, [back, self.other]), back)
        self.assertFalse(self.selector.changed)

    def test_an_empty_frame_is_also_treated_as_a_dropout(self) -> None:
        self.assertIsNone(self.pick(self.selector, []))
        back = box(CENTER + 22, CENTER)
        self.assertIs(self.pick(self.selector, [back]), back)
        self.assertFalse(self.selector.changed)

    def test_alternating_hit_and_miss_never_accumulates_into_a_release(self) -> None:
        # 一帧有一帧没的情况下不能慢慢攒够帧数就放手 —— 成功关联一次就该清零。
        for _ in range(40):
            self.pick(self.selector, [self.other])
            back = box(CENTER + 20, CENTER)
            self.assertIs(self.pick(self.selector, [back, self.other]), back)


class ReleaseTests(SelectorTestCase):
    """真的不见了还是得放手, 不能一直空等。"""

    def test_after_enough_missing_frames_it_releases_and_takes_the_best(self) -> None:
        selector = TargetSelector(lost_frames=6)
        mine = box(CENTER + 20, CENTER)
        other = box(CENTER + 60, CENTER)
        self.pick(selector, [mine, other])
        for _ in range(5):
            self.assertIsNone(self.pick(selector, [other]))
        self.assertIs(self.pick(selector, [other]), other)
        self.assertTrue(selector.changed)

    def test_the_release_is_announced_so_the_log_gets_a_new_track_id(self) -> None:
        selector = TargetSelector(lost_frames=2)
        mine = box(CENTER + 20, CENTER)
        other = box(CENTER + 60, CENTER)
        self.pick(selector, [mine, other])
        self.pick(selector, [other])
        self.pick(selector, [other])
        self.assertTrue(selector.changed)


class HysteresisIsTimeBasedTests(SelectorTestCase):
    """滞回的默认值得按时间算, 不能按帧数拍。

    原来的 switch_frames=3 大概是按 60fps 定的(50 毫秒)。这台机器跑 241fps, 3 帧
    只有 12 毫秒 —— 等于没有滞回。钉的是「至少 50 毫秒」这个要求, 而不是具体数字,
    免得以后有人把它改回 3 还以为没问题。
    """

    FPS = 241.0

    def test_a_switch_needs_at_least_fifty_milliseconds_of_evidence(self) -> None:
        selector = TargetSelector()
        self.assertGreaterEqual(selector.switch_frames / self.FPS, 0.05)

    def test_a_dropout_is_tolerated_for_at_least_fifty_milliseconds(self) -> None:
        selector = TargetSelector()
        self.assertGreaterEqual(selector.lost_frames / self.FPS, 0.05)

    def test_the_association_radius_covers_plausible_per_frame_motion(self) -> None:
        # 实测目标横移 482px/s, 241fps 下每帧 2px。半径要宽裕得多, 但不能宽到把
        # 旁边站着的另一个人也圈进来。
        selector = TargetSelector()
        per_frame = 482.0 / self.FPS
        self.assertGreater(selector.association_radius, per_frame * 5)
        self.assertLess(selector.association_radius, 40.0)


class AmbiguousAssociationTests(SelectorTestCase):
    """挨得近的另一个目标不许被当成「还是原来那个」。

    这条是静默换身份的来源: 原来的关联半径是 min(50, max(12, 检测框对角线)), 对
    36x60 的框就是 50px —— 旁边 25px 站着的另一个人妥妥落在里面, 于是掉检那一帧
    被当成同一个目标接管, 连 changed 都不置位, 日志里看不出任何异常。
    """

    def test_a_neighbour_thirty_pixels_away_is_not_mistaken_for_my_target(self) -> None:
        selector = TargetSelector()
        mine = box(CENTER + 10, CENTER, size=60.0)
        neighbour = box(CENTER + 40, CENTER, size=60.0)
        self.assertIs(self.pick(selector, [mine, neighbour]), mine)
        self.assertIsNone(self.pick(selector, [neighbour]))

    def test_my_own_target_still_associates_across_normal_motion(self) -> None:
        # 241fps 下 482px/s 的目标每帧只走 2px, 关联不该因为收紧而断掉。
        selector = TargetSelector()
        x = CENTER + 40
        current = box(x, CENTER)
        self.assertIs(self.pick(selector, [current]), current)
        for _ in range(30):
            x -= 2.0
            moved = box(x, CENTER)
            self.assertIs(self.pick(selector, [moved]), moved)
            self.assertFalse(selector.changed)


if __name__ == "__main__":
    unittest.main()


class MotionBudgetTests(SelectorTestCase):
    """关联半径要算上「我们自己动了多少」。

    关联比的是屏幕坐标, 而拉枪时我们自己的位移就让目标在画面上大幅移动: max_step=30
    时每帧可挪 30px, 超过收紧后的 24px 半径。实测 300px 拉枪会在第 8 帧(指令开始落地
    的时刻)断掉关联, 然后空等满 24 帧才放手 —— 准心在拉枪途中冻住 100 毫秒。

    更麻烦的是空等期间在途指令还在陆续落地, 目标继续每帧挪 30px, 半径太紧就永远重新
    关联不上。

    两个要求是真冲突的(实测: 半径 24 时掉检 5% 的切换 0.9 次/秒但拉枪冻 24 帧;
    半径 50 时不冻但切换涨到 3.4 次/秒), 所以半径必须跟着我们自己的速度走: 动得快
    就放宽, 不动就收紧 —— 而不动的时候恰恰就是最怕认错旁边那个人的时候。
    """

    def test_without_any_motion_the_radius_stays_tight(self) -> None:
        selector = TargetSelector()
        mine = box(CENTER + 10, CENTER, size=60.0)
        neighbour = box(CENTER + 40, CENTER, size=60.0)
        self.assertIs(self.pick(selector, [mine, neighbour]), mine)
        self.assertIsNone(self.pick(selector, [neighbour]))

    def test_our_own_motion_widens_the_radius_enough_to_keep_the_target(self) -> None:
        selector = TargetSelector()
        mine = box(CENTER + 100, CENTER)
        self.assertIs(self.pick(selector, [mine]), mine)
        # 我们发了 30px 的指令, 所以目标在画面上往回挪 30px —— 还是同一个人。
        shifted = box(CENTER + 70, CENTER)
        self.assertIs(
            selector.select(
                [shifted],
                frame_width=FRAME,
                frame_height=FRAME,
                target_y_ratio=0.5,
                fov_radius=150.0,
                target_class=0,
                motion_budget=30.0,
            ),
            shifted,
        )
        self.assertFalse(selector.changed)

    def test_without_the_budget_that_same_shift_would_be_lost(self) -> None:
        # 对照: 同样的 30px 位移, 不告诉它我们动了, 就认不出来了。
        selector = TargetSelector()
        mine = box(CENTER + 100, CENTER)
        self.assertIs(self.pick(selector, [mine]), mine)
        self.assertIsNone(self.pick(selector, [box(CENTER + 70, CENTER)]))

    def test_a_big_flick_never_freezes(self) -> None:
        """回归测试: 300px 拉枪全程不许有一帧认不出目标。"""
        selector = TargetSelector()
        lag, max_step = 7, 30.0
        start, cam = 300.0, 0.0
        history = [0.0]
        budget = 0.0
        frozen = 0
        for _ in range(120):
            seen = start - history[max(0, len(history) - 1 - lag)]
            detection = box(CENTER + seen, CENTER)
            target = selector.select(
                [detection],
                frame_width=FRAME,
                frame_height=FRAME,
                target_y_ratio=0.5,
                fov_radius=500.0,
                target_class=0,
                motion_budget=budget,
            )
            if target is None:
                frozen += 1
                step = 0.0
            else:
                error = target.center_x - CENTER
                step = max(-max_step, min(max_step, error * 0.096))
            cam += step
            history.append(cam)
            budget = max(abs(step), budget * 0.9)
        self.assertEqual(frozen, 0)
        self.assertLess(abs(start - cam), 3.0)


class CoastingFlagTests(SelectorTestCase):
    """空等期间要能被管线识别出来。

    管线在没有目标时会每帧 controller.reset(), 而那个 reset 不保留 recent_commands。
    空等期间被清掉的话, 扣在途的算法会以为什么都没发过 —— 而那些位移物理上已经发给
    鼠标了, 目标回来时会当场多走一截。
    """

    def test_it_reports_coasting_while_holding_through_a_dropout(self) -> None:
        selector = TargetSelector()
        mine = box(CENTER + 20, CENTER)
        self.pick(selector, [mine])
        self.assertFalse(selector.coasting)
        self.assertIsNone(self.pick(selector, []))
        self.assertTrue(selector.coasting)

    def test_coasting_ends_when_the_target_comes_back(self) -> None:
        selector = TargetSelector()
        mine = box(CENTER + 20, CENTER)
        self.pick(selector, [mine])
        self.pick(selector, [])
        back = box(CENTER + 21, CENTER)
        self.assertIs(self.pick(selector, [back]), back)
        self.assertFalse(selector.coasting)

    def test_coasting_ends_once_it_gives_up(self) -> None:
        selector = TargetSelector(lost_frames=3)
        mine = box(CENTER + 20, CENTER)
        other = box(CENTER + 60, CENTER)
        self.pick(selector, [mine])
        for _ in range(2):
            self.pick(selector, [other])
            self.assertTrue(selector.coasting)
        self.assertIs(self.pick(selector, [other]), other)
        self.assertFalse(selector.coasting)

    def test_nothing_locked_at_all_is_not_coasting(self) -> None:
        # 从来没锁上过(比如刚开局、画面里没人)不是空等, 管线该照常 reset。
        selector = TargetSelector()
        self.assertIsNone(self.pick(selector, []))
        self.assertFalse(selector.coasting)


class PipelineWiringTests(unittest.TestCase):
    """管线真的把这两件事接上了吗。

    「加了字段但没人往里填」和「加了标志但没人读」是这类改动最常见的坏法, 而且日志
    和界面看起来完全正常。
    """

    def _drive(self, coast_from: int | None = None):
        from unittest.mock import patch

        from tests.test_pipeline_loop import _Harness

        budgets: list[float] = []
        real_select = TargetSelector.select
        frames = {"n": 0}

        def spy(self, *args, **kwargs):
            budgets.append(kwargs.get("motion_budget", 0.0))
            result = real_select(self, *args, **kwargs)
            frames["n"] += 1
            if coast_from is not None and frames["n"] >= coast_from:
                self.coasting = True
                return None
            return result

        harness = _Harness([1, 2, 3, 4, 5, 6])
        with patch.object(TargetSelector, "select", spy):
            harness.run()
        return budgets, harness.controller

    def test_the_pipeline_feeds_our_own_motion_into_the_association(self) -> None:
        budgets, _ = self._drive()
        # 第一帧还没发过指令, 之后台架里每帧发 (3, 0)。
        self.assertEqual(budgets[0], 0.0)
        self.assertGreater(max(budgets), 0.0)

    def test_the_pipeline_leaves_the_controller_alone_while_coasting(self) -> None:
        # 空等期间 reset 会清掉在途指令窗口, 而那些位移已经发给鼠标了。
        _, controller = self._drive(coast_from=2)
        self.assertFalse(controller.reset.called)

    def test_without_coasting_a_missing_target_still_resets(self) -> None:
        # 对照: 真的没目标(而不是空等)时该照常清干净。
        from unittest.mock import patch

        from tests.test_pipeline_loop import _Harness

        def blind(self, *args, **kwargs):
            self.coasting = False
            return None

        harness = _Harness([1, 2, 3])
        with patch.object(TargetSelector, "select", blind):
            harness.run()
        self.assertTrue(harness.controller.reset.called)


class MotionBudgetDecayTests(unittest.TestCase):
    """自身位移要取衰减峰值, 不能只看当前指令。

    指令绕完整条回路才看得见(实测 7 帧), 拉枪收尾时当前指令已经很小而画面上的目标
    还在按七帧前那个大指令的幅度移动。只看当前值会在收尾那几帧把关联半径收得太紧,
    正好在准心快贴上去的时候丢掉目标。
    """

    def test_a_big_command_raises_the_budget_at_once(self) -> None:
        from rhodes_fast.pipeline import next_motion_budget

        self.assertAlmostEqual(next_motion_budget(0.0, (30, 0)), 30.0)

    def test_the_peak_survives_the_seven_frame_loop_delay(self) -> None:
        from rhodes_fast.pipeline import next_motion_budget

        budget = next_motion_budget(0.0, (30, 0))
        for _ in range(7):
            budget = next_motion_budget(budget, (0, 0))
        # 七帧之后还得剩大半, 否则收尾时半径就塌了。
        self.assertGreater(budget, 30.0 * 0.6)

    def test_it_does_fade_when_we_stop_moving(self) -> None:
        from rhodes_fast.pipeline import next_motion_budget

        budget = next_motion_budget(0.0, (30, 0))
        for _ in range(120):
            budget = next_motion_budget(budget, (0, 0))
        # 长时间不动之后必须收回去, 否则半径永远松着, 又会认错旁边那个人。
        self.assertLess(budget, 1.0)
