from __future__ import annotations

import unittest

import numpy as np

from rhodes_fast.detector import Detection, decode_yolo
from rhodes_fast.pipeline import select_target


class TargetSelectionTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
