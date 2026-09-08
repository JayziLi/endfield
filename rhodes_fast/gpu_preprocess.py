from __future__ import annotations

import hashlib
import os
from pathlib import Path


_CACHE_VERSION = 1


def prepare_gpu_preprocess_model(model_path: Path, cache_directory: Path) -> Path:
    source = model_path.resolve()
    stat = source.stat()
    cache_key = hashlib.sha256(
        f"{_CACHE_VERSION}\0{source}\0{stat.st_size}\0{stat.st_mtime_ns}".encode("utf-8")
    ).hexdigest()[:16]
    target = cache_directory / f"{source.stem}-{cache_key}.onnx"
    if target.is_file():
        return target

    import onnx

    cache_directory.mkdir(parents=True, exist_ok=True)
    model = onnx.load(str(source))
    add_gpu_preprocess(model)
    onnx.checker.check_model(model)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        onnx.save(model, str(temporary))
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def add_gpu_preprocess(model) -> None:
    from onnx import TensorProto, helper, numpy_helper
    import numpy as np

    graph = model.graph
    if len(graph.input) != 1:
        raise ValueError(f"GPU preprocessing requires one model input, got {len(graph.input)}")
    model_input = graph.input[0]
    tensor_type = model_input.type.tensor_type
    if tensor_type.elem_type != TensorProto.FLOAT:
        raise ValueError("GPU preprocessing requires a float32 model input")
    shape = [
        dimension.dim_value if dimension.HasField("dim_value") else None
        for dimension in tensor_type.shape.dim
    ]
    if len(shape) != 4 or any(not isinstance(value, int) or value <= 0 for value in shape):
        raise ValueError(f"GPU preprocessing requires a static NCHW input, got {shape}")
    batch, channels, height, width = shape
    if batch != 1 or channels != 3:
        raise ValueError(f"GPU preprocessing requires a 1x3xHxW input, got {shape}")

    input_name = model_input.name
    prefix = _unique_prefix(graph, "__endfield_gpu_preprocess")
    normalized_name = f"{prefix}_normalized"
    for node in graph.node:
        for index, name in enumerate(node.input):
            if name == input_name:
                node.input[index] = normalized_name

    original_nodes = list(graph.node)
    del graph.node[:]
    graph.node.extend(
        [
            helper.make_node(
                "Cast",
                [input_name],
                [f"{prefix}_bgr_f32"],
                to=TensorProto.FLOAT,
                name=f"{prefix}_cast",
            ),
            helper.make_node(
                "Gather",
                [f"{prefix}_bgr_f32", f"{prefix}_bgr_indices"],
                [f"{prefix}_rgb_f32"],
                axis=3,
                name=f"{prefix}_bgr_to_rgb",
            ),
            helper.make_node(
                "Transpose",
                [f"{prefix}_rgb_f32"],
                [f"{prefix}_nchw"],
                perm=[0, 3, 1, 2],
                name=f"{prefix}_transpose",
            ),
            helper.make_node(
                "Mul",
                [f"{prefix}_nchw", f"{prefix}_scale"],
                [normalized_name],
                name=f"{prefix}_normalize",
            ),
        ]
    )
    graph.node.extend(original_nodes)
    del graph.input[:]
    graph.input.extend(
        [helper.make_tensor_value_info(input_name, TensorProto.UINT8, [1, height, width, 3])]
    )
    graph.initializer.extend(
        [
            numpy_helper.from_array(
                np.array([2, 1, 0], dtype=np.int64),
                name=f"{prefix}_bgr_indices",
            ),
            numpy_helper.from_array(
                np.array([1.0 / 255.0], dtype=np.float32),
                name=f"{prefix}_scale",
            ),
        ]
    )


def _unique_prefix(graph, base: str) -> str:
    used = {value.name for value in graph.input}
    used.update(value.name for value in graph.output)
    used.update(value.name for value in graph.initializer)
    for node in graph.node:
        used.update(node.input)
        used.update(node.output)
    prefix = base
    while any(name.startswith(prefix) for name in used):
        prefix += "_"
    return prefix
