from __future__ import annotations

import unittest

import numpy as np

from rhodes_fast.detector import Detection, decode_yolo
from rhodes_fast.pipeline import TargetSelector, select_target


class TargetSelectionTests(unittest.TestCase):
    def test_small_distance_changes_do_not_switch_the_locked_target(self) -> None:
        selector = TargetSelector()
        selected_centers: list[float] = []

        for frame in range(8):
            if frame % 2 == 0:
                left = Detection(120, 150, 140, 170, 0.9, 0)
                right = Detection(181, 150, 201, 170, 0.9, 0)
            else:
                left = Detection(119, 150, 139, 170, 0.9, 0)
                right = Detection(180, 150, 200, 170, 0.9, 0)
            selected = selector.select(
                [left, right],
                frame_width=320,
                frame_height=320,
                target_y_ratio=0.5,
                fov_radius=100,
                target_class=0,
            )
            self.assertIsNotNone(selected)
            selected_centers.append(selected.center_x)

        self.assertTrue(all(center < 160 for center in selected_centers))

    def test_new_target_must_stay_better_before_lock_switches(self) -> None:
        # 显式给 switch_frames, 这条测的是滞回机制本身而不是默认值取多少。默认值已从
        # 3 帧改到 24 帧: 241fps 下 3 帧只有 12 毫秒, 等于没有滞回。
        selector = TargetSelector(switch_frames=3)
        current = Detection(110, 150, 130, 170, 0.9, 0)
        farther = Detection(230, 150, 250, 170, 0.9, 0)
        selected = selector.select(
            [current, farther],
            frame_width=320,
            frame_height=320,
            target_y_ratio=0.5,
            fov_radius=100,
            target_class=0,
        )
        selected_centers = [selected.center_x]

        better = Detection(160, 150, 180, 170, 0.9, 0)
        for _ in range(3):
            selected = selector.select(
                [current, better],
                frame_width=320,
                frame_height=320,
                target_y_ratio=0.5,
                fov_radius=100,
                target_class=0,
            )
            selected_centers.append(selected.center_x)

        self.assertEqual(selected_centers, [120, 120, 120, 170])

    def test_replacement_after_locked_target_disappears_is_reported_as_changed(self) -> None:
        # 交班的时机改了: 要等 lost_frames 帧确认目标真的不见了, 而不是漏一帧就交。
        # 掉检和真消失在单帧上无法区分, 而漏一帧就交班意味着 241fps 下每 80 毫秒
        # 就可能换一个人打(实测掉检 5% 时 12.9 次/秒)。详见 test_target_selector.py。
        selector = TargetSelector(lost_frames=3)
        locked = Detection(110, 150, 130, 170, 0.9, 0)
        replacement = Detection(150, 150, 170, 170, 0.9, 0)

        def pick(detections):
            return selector.select(
                detections,
                frame_width=320,
                frame_height=320,
                target_y_ratio=0.5,
                fov_radius=100,
                target_class=0,
            )

        pick([locked])
        for _ in range(2):
            # 空等期间宁可不瞄, 也不要瞄到另一个人身上。
            self.assertIsNone(pick([replacement]))

        selected = pick([replacement])

        self.assertEqual(selected, replacement)
        self.assertTrue(selector.changed)

    def test_runtime_target_setting_change_restarts_the_lock(self) -> None:
        selector = TargetSelector()
        first = Detection(140, 120, 180, 200, 0.9, 0)
        selector.select(
            [first],
            frame_width=320,
            frame_height=320,
            target_y_ratio=0.25,
            fov_radius=100,
            target_class=0,
        )
        replacement = Detection(140, 120, 180, 200, 0.9, 1)

        selected = selector.select(
            [replacement],
            frame_width=320,
            frame_height=320,
            target_y_ratio=0.75,
            fov_radius=100,
            target_class=1,
        )

        self.assertEqual(selected, replacement)
        self.assertTrue(selector.changed)

    def test_only_selected_class_can_be_targeted(self) -> None:
        nearest_wrong_class = Detection(150, 150, 170, 170, 0.95, 1)
        farther_selected_class = Detection(180, 150, 200, 170, 0.90, 2)

        selected = select_target(
            [nearest_wrong_class, farther_selected_class],
            frame_width=320,
            frame_height=320,
            target_y_ratio=0.5,
            fov_radius=100,
            target_class=2,
        )

        self.assertEqual(selected, farther_selected_class)

    def test_output_layout_is_inferred_from_each_tensor(self) -> None:
        candidates_first = np.zeros((1, 10, 7), dtype=np.float32)
        candidates_first[0, 0] = [160.0, 160.0, 40.0, 60.0, 0.9, 0.1, 0.8]
        arguments = {
            "frame_width": 320,
            "frame_height": 320,
            "input_width": 320,
            "input_height": 320,
            "output_format": "yolov5",
            "output_layout": "auto",
            "confidence_threshold": 0.25,
            "iou_threshold": 0.5,
        }

        first = decode_yolo(candidates_first, **arguments)
        second = decode_yolo(candidates_first.transpose(0, 2, 1), **arguments)

        self.assertEqual(first, second)
        self.assertEqual(first[0].class_id, 1)

    def test_nms_keeps_overlapping_boxes_from_different_classes(self) -> None:
        output = np.zeros((1, 10, 7), dtype=np.float32)
        output[0, 0] = [160.0, 160.0, 40.0, 60.0, 0.95, 0.9, 0.1]
        output[0, 1] = [160.0, 160.0, 40.0, 60.0, 0.90, 0.1, 0.9]

        detections = decode_yolo(
            output,
            frame_width=320,
            frame_height=320,
            input_width=320,
            input_height=320,
            output_format="yolov5",
            output_layout="auto",
            confidence_threshold=0.25,
            iou_threshold=0.5,
        )

        self.assertEqual({item.class_id for item in detections}, {0, 1})

    def test_target_class_fast_path_matches_full_detection_filter(self) -> None:
        output = np.zeros((1, 12, 8), dtype=np.float32)
        output[0, 0] = [150.0, 150.0, 40.0, 60.0, 0.95, 0.85, 0.10, 0.05]
        output[0, 1] = [152.0, 152.0, 40.0, 60.0, 0.90, 0.80, 0.15, 0.05]
        output[0, 2] = [220.0, 150.0, 30.0, 50.0, 0.92, 0.05, 0.90, 0.05]
        arguments = {
            "frame_width": 320,
            "frame_height": 320,
            "input_width": 320,
            "input_height": 320,
            "output_format": "yolov5",
            "output_layout": "auto",
            "confidence_threshold": 0.25,
            "iou_threshold": 0.5,
        }

        all_detections = decode_yolo(output, **arguments)
        target_only = decode_yolo(output, target_class=1, **arguments)

        self.assertEqual(target_only, [item for item in all_detections if item.class_id == 1])

    def test_end2end_layout_handles_one_or_zero_candidates(self) -> None:
        arguments = {
            "frame_width": 320,
            "frame_height": 320,
            "input_width": 320,
            "input_height": 320,
            "output_format": "end2end",
            "output_layout": "auto",
            "confidence_threshold": 0.25,
            "iou_threshold": 0.5,
        }
        one = np.array([[[10.0, 20.0, 30.0, 40.0, 0.9, 2.0]]], dtype=np.float32)
        empty = np.empty((1, 0, 6), dtype=np.float32)

        self.assertEqual(decode_yolo(one, **arguments)[0].class_id, 2)
        self.assertEqual(decode_yolo(empty, **arguments), [])

    def test_end2end_target_class_fast_path_matches_full_detection_filter(self) -> None:
        output = np.array(
            [[
                [10.0, 20.0, 30.0, 40.0, 0.9, 2.0],
                [50.0, 60.0, 80.0, 100.0, 0.8, 1.0],
            ]],
            dtype=np.float32,
        )
        arguments = {
            "frame_width": 320,
            "frame_height": 320,
            "input_width": 320,
            "input_height": 320,
            "output_format": "end2end",
            "output_layout": "auto",
            "confidence_threshold": 0.25,
            "iou_threshold": 0.5,
        }

        all_detections = decode_yolo(output, **arguments)
        target_only = decode_yolo(output, target_class=1, **arguments)

        self.assertEqual(target_only, [item for item in all_detections if item.class_id == 1])

    def test_end2end_segmentation_output_ignores_mask_coefficients(self) -> None:
        output = np.zeros((1, 2, 38), dtype=np.float32)
        output[0, 0, :6] = [10.0, 20.0, 30.0, 40.0, 0.9, 1.0]
        output[0, 1, :6] = [50.0, 60.0, 80.0, 100.0, 0.8, 0.0]

        detections = decode_yolo(
            output,
            frame_width=320,
            frame_height=320,
            input_width=320,
            input_height=320,
            output_format="end2end",
            output_layout="candidates_first",
            confidence_threshold=0.25,
            iou_threshold=0.5,
            target_class=1,
        )

        self.assertEqual(len(detections), 1)
        self.assertEqual(detections[0].class_id, 1)
        self.assertEqual((detections[0].x1, detections[0].y1), (10.0, 20.0))


if __name__ == "__main__":
    unittest.main()
