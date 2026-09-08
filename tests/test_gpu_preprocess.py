from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import onnx
import numpy as np
import onnxruntime as ort
from onnx import TensorProto, helper

from rhodes_fast.gpu_preprocess import prepare_gpu_preprocess_model


class GpuPreprocessTests(unittest.TestCase):
    def test_wraps_float_nchw_model_with_uint8_nhwc_preprocessing(self) -> None:
        graph = helper.make_graph(
            [helper.make_node("Identity", ["images"], ["output"])],
            "test",
            [helper.make_tensor_value_info("images", TensorProto.FLOAT, [1, 3, 2, 4])],
            [helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, 3, 2, 4])],
        )
        model = helper.make_model(graph)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "model.onnx"
            onnx.save(model, str(source))
            wrapped_path = prepare_gpu_preprocess_model(source, root / "cache")
            wrapped = onnx.load(str(wrapped_path))
            cached_path = prepare_gpu_preprocess_model(source, root / "cache")

        model_input = wrapped.graph.input[0]
        shape = [dimension.dim_value for dimension in model_input.type.tensor_type.shape.dim]
        self.assertEqual(model_input.type.tensor_type.elem_type, TensorProto.UINT8)
        self.assertEqual(shape, [1, 2, 4, 3])
        self.assertEqual([node.op_type for node in wrapped.graph.node[:4]], ["Cast", "Gather", "Transpose", "Mul"])
        self.assertNotEqual(wrapped.graph.node[4].input[0], "images")
        self.assertEqual(cached_path, wrapped_path)

    def test_wrapped_model_matches_bgr_to_rgb_nchw_normalization(self) -> None:
        graph = helper.make_graph(
            [helper.make_node("Identity", ["images"], ["output"])],
            "test",
            [helper.make_tensor_value_info("images", TensorProto.FLOAT, [1, 3, 1, 2])],
            [helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, 3, 1, 2])],
        )
        model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
        model.ir_version = min(model.ir_version, 11)
        pixels = np.array([[[[10, 20, 30], [40, 50, 60]]]], dtype=np.uint8)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "model.onnx"
            onnx.save(model, str(source))
            wrapped_path = prepare_gpu_preprocess_model(source, root / "cache")
            session = ort.InferenceSession(str(wrapped_path), providers=["CPUExecutionProvider"])
            actual = session.run(None, {"images": pixels})[0]

        expected = pixels[..., ::-1].transpose(0, 3, 1, 2).astype(np.float32) / 255.0
        np.testing.assert_allclose(actual, expected, rtol=0.0, atol=1e-7)


if __name__ == "__main__":
    unittest.main()
