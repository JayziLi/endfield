from __future__ import annotations

import unittest

import numpy as np

from rhodes_fast.detector import _numpy_tensor_dtype, _prepare_model_blob


class DetectorInputTests(unittest.TestCase):
    def test_maps_onnx_float_types_to_numpy(self) -> None:
        self.assertIs(_numpy_tensor_dtype("tensor(float)"), np.float32)
        self.assertIs(_numpy_tensor_dtype("tensor(float16)"), np.float16)

    def test_rejects_unsupported_input_type(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported YOLO input type"):
            _numpy_tensor_dtype("tensor(double)")

    def test_prepares_float16_input_for_the_default_model(self) -> None:
        frame = np.full((10, 20, 3), (10, 20, 30), dtype=np.uint8)
        blob = _prepare_model_blob(frame, 40, 32, np.float16)

        self.assertEqual(blob.shape, (1, 3, 32, 40))
        self.assertEqual(blob.dtype, np.float16)
        np.testing.assert_allclose(blob[0, :, 0, 0], np.array([30, 20, 10]) / 255.0, rtol=0.002)


if __name__ == "__main__":
    unittest.main()
