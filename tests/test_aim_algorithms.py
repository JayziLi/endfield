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


if __name__ == "__main__":
    unittest.main()
