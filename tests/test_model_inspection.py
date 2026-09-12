from __future__ import annotations

import unittest
from types import SimpleNamespace

from rhodes_fast.model_inspection import infer_contract_from_shape, inspect_session


class ModelInspectionTests(unittest.TestCase):
    def test_detects_selected_yolov5_contract(self) -> None:
        contract = infer_contract_from_shape((1, 6300, 6))
        self.assertEqual(contract.output_format, "yolov5")
        self.assertEqual(contract.output_layout, "candidates_first")

    def test_detects_common_yolov8_contract(self) -> None:
        contract = infer_contract_from_shape((1, 84, 8400))
        self.assertEqual(contract.output_format, "yolov8")
        self.assertEqual(contract.output_layout, "channels_first")
        self.assertEqual(contract.class_count_for("yolov8"), 80)

    def test_counts_classes_for_custom_yolov5_model(self) -> None:
        contract = infer_contract_from_shape((1, 6300, 9), class_count=4)
        self.assertEqual(contract.output_layout, "candidates_first")
        self.assertEqual(contract.class_count_for("yolov5"), 4)

    def test_keeps_ambiguous_six_column_contract_manual(self) -> None:
        contract = infer_contract_from_shape((1, 100, 6))
        self.assertIsNone(contract.output_format)
        self.assertEqual(contract.output_layout, "candidates_first")
        self.assertIsNone(contract.class_count_for("end2end"))

    def test_accepts_dynamic_candidate_dimension(self) -> None:
        contract = infer_contract_from_shape((1, "candidates", 9))
        self.assertEqual(contract.output_layout, "candidates_first")
        self.assertIsNone(contract.class_count_for("yolov5"))

    def test_metadata_resolves_layout_when_candidates_are_the_shorter_axis(self) -> None:
        contract = infer_contract_from_shape((1, 21, 85), class_count=80)
        self.assertEqual(contract.output_format, "yolov5")
        self.assertEqual(contract.output_layout, "candidates_first")
        self.assertEqual(contract.class_count_for("yolov5"), 80)

    def test_multiple_outputs_do_not_force_unknown_model_to_yolov5(self) -> None:
        session = SimpleNamespace(
            get_outputs=lambda: [SimpleNamespace(shape=[1, 100, 6]), SimpleNamespace(shape=[1, 1])],
            get_modelmeta=lambda: SimpleNamespace(custom_metadata_map={}),
        )

        contract = inspect_session(session)

        self.assertIsNone(contract.output_format)

    def test_selects_detection_tensor_when_it_is_not_the_first_output(self) -> None:
        session = SimpleNamespace(
            get_outputs=lambda: [SimpleNamespace(shape=[1, 1]), SimpleNamespace(shape=[1, 84, 8400])],
            get_modelmeta=lambda: SimpleNamespace(custom_metadata_map={}),
        )

        contract = inspect_session(session)

        self.assertEqual(contract.output_index, 1)
        self.assertEqual(contract.output_format, "yolov8")

    def test_end2end_hint_resolves_static_single_detection_layout(self) -> None:
        contract = infer_contract_from_shape((1, 1, 6), output_format_hint="end2end")
        self.assertEqual(contract.output_layout, "candidates_first")

    def test_end2end_hint_prefers_six_feature_detection_output(self) -> None:
        session = SimpleNamespace(
            get_outputs=lambda: [SimpleNamespace(shape=[1, 200, 10]), SimpleNamespace(shape=[1, 5, 6])],
            get_modelmeta=lambda: SimpleNamespace(custom_metadata_map={}),
        )

        contract = inspect_session(session, "end2end")

        self.assertEqual(contract.output_index, 1)
        self.assertEqual(contract.output_layout, "candidates_first")

    def test_ultralytics_end2end_metadata_overrides_stale_format_hint(self) -> None:
        session = SimpleNamespace(
            get_outputs=lambda: [SimpleNamespace(shape=[1, 300, 6])],
            get_modelmeta=lambda: SimpleNamespace(
                custom_metadata_map={
                    "end2end": "True",
                    "names": "{0: 'person', 1: 'bicycle'}",
                }
            ),
        )

        contract = inspect_session(session, "yolov5")

        self.assertEqual(contract.output_format, "end2end")
        self.assertEqual(contract.output_layout, "candidates_first")
        self.assertEqual(contract.class_count, 2)

    def test_ultralytics_segment_end2end_output_keeps_mask_columns(self) -> None:
        session = SimpleNamespace(
            get_outputs=lambda: [
                SimpleNamespace(shape=[1, 300, 38]),
                SimpleNamespace(shape=[1, 32, 80, 80]),
            ],
            get_modelmeta=lambda: SimpleNamespace(
                custom_metadata_map={"end2end": "True", "names": "{0: '0', 1: 'Enemy'}"}
            ),
        )

        contract = inspect_session(session, "yolov5")

        self.assertEqual(contract.output_format, "end2end")
        self.assertEqual(contract.output_shape, (1, 300, 38))
        self.assertEqual(contract.output_index, 0)

    def test_format_minimum_resolves_ultra_small_candidate_axis(self) -> None:
        contract = infer_contract_from_shape((1, 4, 9), output_format_hint="yolov5")
        self.assertEqual(contract.output_layout, "candidates_first")


if __name__ == "__main__":
    unittest.main()
