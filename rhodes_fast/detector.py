from __future__ import annotations

import os
import site
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


_DLL_DIRECTORY_HANDLES = []
if os.name == "nt":
    for site_packages in site.getsitepackages():
        dll_directories = list((Path(site_packages) / "nvidia").glob("*/bin"))
        dll_directories.append(Path(site_packages) / "tensorrt_libs")
        for dll_directory in dll_directories:
            if dll_directory.is_dir():
                _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(str(dll_directory)))

import onnxruntime as ort

from .config import ModelConfig
from .gpu_preprocess import prepare_gpu_preprocess_model
from .model_inspection import infer_output_layout, inspect_session


@dataclass(frozen=True, slots=True)
class Detection:
    x1: float
    y1: float
    x2: float
    y2: float
    confidence: float
    class_id: int

    @property
    def center_x(self) -> float:
        return (self.x1 + self.x2) * 0.5

    def aim_y(self, ratio: float) -> float:
        return self.y1 + (self.y2 - self.y1) * ratio


class YoloDetector:
    def __init__(self, config: ModelConfig):
        self.config = config
        if hasattr(ort, "preload_dlls"):
            ort.preload_dlls(directory="")
        self.provider = _select_provider(config.provider)
        self.cuda_graph_enabled = False
        self.cuda_graph_fallback_reason: str | None = None
        self.gpu_preprocess_enabled = False
        self.gpu_preprocess_fallback_reason: str | None = None
        self._io_binding = None
        self._input_ortvalue = None
        self._output_ortvalue = None
        self._session_model_path = config.path
        if config.gpu_preprocess and self.provider == "TensorrtExecutionProvider":
            try:
                cache_directory = Path(__file__).resolve().parent.parent / ".cache" / "gpu-preprocess"
                self._session_model_path = prepare_gpu_preprocess_model(config.path, cache_directory)
                self.gpu_preprocess_enabled = True
            except Exception as exc:
                self.gpu_preprocess_fallback_reason = f"{type(exc).__name__}: {exc}"
        elif config.gpu_preprocess:
            self.gpu_preprocess_fallback_reason = "GPU preprocessing requires TensorRTExecutionProvider"
        use_cuda_graph = config.cuda_graph and self.provider == "TensorrtExecutionProvider"
        try:
            self._initialize_session(use_cuda_graph)
        except Exception as exc:
            if not self.gpu_preprocess_enabled:
                raise
            self.gpu_preprocess_enabled = False
            self.gpu_preprocess_fallback_reason = f"{type(exc).__name__}: {exc}"
            self._session_model_path = config.path
            self.cuda_graph_enabled = False
            self._clear_io_binding()
            self._initialize_session(use_cuda_graph)
        self.last_inference_ms = 0.0
        self.last_detection_ms = 0.0
        self.last_preprocess_ms = 0.0
        self.last_postprocess_ms = 0.0

    def _initialize_session(self, use_cuda_graph: bool) -> None:
        if use_cuda_graph:
            try:
                self._create_session(True)
                self._setup_cuda_graph()
            except Exception as exc:
                self._fallback_from_cuda_graph(exc)
        else:
            self._create_session(False)
            if self.config.cuda_graph:
                self.cuda_graph_fallback_reason = "CUDA Graph requires TensorRTExecutionProvider"

    def _create_session(self, use_cuda_graph: bool) -> None:
        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        providers = _provider_chain(self.provider, cuda_graph=use_cuda_graph)
        self.session = ort.InferenceSession(
            str(self._session_model_path),
            sess_options=options,
            providers=providers,
        )
        active_providers = self.session.get_providers()
        if self.provider != "CPUExecutionProvider" and self.provider not in active_providers:
            raise RuntimeError(
                f"{self.provider} failed to load; active providers: {active_providers}. "
                "CUDA inference would otherwise silently fall back to CPU."
            )
        model_input = self.session.get_inputs()[0]
        if len(model_input.shape) != 4:
            raise ValueError(f"Expected a four-dimensional YOLO input, got {model_input.shape}")
        self.input_name = model_input.name
        if self.gpu_preprocess_enabled:
            if model_input.type != "tensor(uint8)" or model_input.shape[3] != 3:
                raise ValueError(f"Expected a uint8 NHWC input, got {model_input.shape} {model_input.type}")
            self.input_dtype = np.uint8
            self.input_height = int(model_input.shape[1])
            self.input_width = int(model_input.shape[2])
        else:
            self.input_dtype = _numpy_tensor_dtype(model_input.type)
            self.input_height = int(model_input.shape[2])
            self.input_width = int(model_input.shape[3])
        try:
            contract = inspect_session(self.session, self.config.output_format)
            self.output_layout = contract.output_layout
            self.output_format = contract.output_format or self.config.output_format
            self.output_index = contract.output_index
        except ValueError:
            self.output_layout = "auto"
            self.output_format = self.config.output_format
            self.output_index = 0
        self.output_name = self.session.get_outputs()[self.output_index].name

    def _setup_cuda_graph(self) -> None:
        model_input = self.session.get_inputs()[0]
        model_output = self.session.get_outputs()[self.output_index]
        input_types = {
            "tensor(float)": np.float32,
            "tensor(float16)": np.float16,
            "tensor(uint8)": np.uint8,
        }
        input_type = input_types.get(model_input.type)
        if input_type is None:
            raise ValueError(f"unsupported CUDA Graph input type: {model_input.type}")
        output_types = {
            "tensor(float)": np.float32,
            "tensor(float16)": np.float16,
        }
        output_type = output_types.get(model_output.type)
        if output_type is None:
            raise ValueError(f"unsupported CUDA Graph output type: {model_output.type}")
        input_shape = _static_tensor_shape(model_input.shape, "input")
        output_shape = _static_tensor_shape(model_output.shape, "output")
        blank = np.zeros(input_shape, dtype=input_type)
        self._input_ortvalue = ort.OrtValue.ortvalue_from_numpy(blank, "cuda", 0)
        self._output_ortvalue = ort.OrtValue.ortvalue_from_shape_and_type(
            output_shape,
            output_type,
            "cuda",
            0,
        )
        self._io_binding = self.session.io_binding()
        self._io_binding.bind_ortvalue_input(self.input_name, self._input_ortvalue)
        self._io_binding.bind_ortvalue_output(self.output_name, self._output_ortvalue)
        self.cuda_graph_enabled = True

    def _fallback_from_cuda_graph(self, exc: Exception) -> None:
        self.cuda_graph_enabled = False
        self.cuda_graph_fallback_reason = f"{type(exc).__name__}: {exc}"
        self._clear_io_binding()
        self._create_session(False)

    def _clear_io_binding(self) -> None:
        self._io_binding = None
        self._input_ortvalue = None
        self._output_ortvalue = None

    def _run_model(self, blob: np.ndarray) -> np.ndarray:
        if not self.cuda_graph_enabled:
            return self.session.run([self.output_name], {self.input_name: blob})[0]
        try:
            self._input_ortvalue.update_inplace(blob)
            self.session.run_with_iobinding(self._io_binding)
            return self._output_ortvalue.numpy()
        except Exception as exc:
            self._fallback_from_cuda_graph(exc)
            return self.session.run([self.output_name], {self.input_name: blob})[0]

    def warmup(self, count: int | None = None) -> None:
        if count is None:
            count = 30 if self.cuda_graph_enabled else 4
        if self.gpu_preprocess_enabled:
            blank = np.zeros((1, self.input_height, self.input_width, 3), dtype=np.uint8)
        else:
            blank = np.zeros((1, 3, self.input_height, self.input_width), dtype=self.input_dtype)
        for _ in range(count):
            self._run_model(blank)
        self._assert_active_provider()

    def detect(self, frame: np.ndarray, target_class: int | None = None) -> list[Detection]:
        detection_started = time.perf_counter()
        frame_h, frame_w = frame.shape[:2]
        if self.gpu_preprocess_enabled:
            model_frame = frame
            if frame_w != self.input_width or frame_h != self.input_height:
                model_frame = cv2.resize(
                    frame,
                    (self.input_width, self.input_height),
                    interpolation=cv2.INTER_LINEAR,
                )
            blob = np.ascontiguousarray(model_frame)[None]
        else:
            blob = _prepare_model_blob(frame, self.input_width, self.input_height, self.input_dtype)
        self.last_preprocess_ms = (time.perf_counter() - detection_started) * 1000.0
        started = time.perf_counter()
        output = self._run_model(blob)
        self._assert_active_provider()
        self.last_inference_ms = (time.perf_counter() - started) * 1000.0
        detections = decode_yolo(
            output,
            frame_width=frame_w,
            frame_height=frame_h,
            input_width=self.input_width,
            input_height=self.input_height,
            output_format=self.output_format,
            output_layout=self.output_layout,
            confidence_threshold=self.config.confidence,
            iou_threshold=self.config.iou,
            target_class=target_class,
        )
        self.last_detection_ms = (time.perf_counter() - detection_started) * 1000.0
        self.last_postprocess_ms = max(
            0.0,
            self.last_detection_ms - self.last_preprocess_ms - self.last_inference_ms,
        )
        return detections

    def _assert_active_provider(self) -> None:
        active = self.session.get_providers()
        if self.provider != "CPUExecutionProvider" and self.provider not in active:
            raise RuntimeError(f"{self.provider} stopped running and fell back to {active}")


def _select_provider(preference: str) -> str:
    available = set(ort.get_available_providers())
    choices = {
        "cuda": "CUDAExecutionProvider",
        "tensorrt": "TensorrtExecutionProvider",
        "cpu": "CPUExecutionProvider",
    }
    normalized = preference.lower()
    if normalized == "auto":
        for provider in ("TensorrtExecutionProvider", "CUDAExecutionProvider"):
            if provider in available:
                return provider
        return "CPUExecutionProvider"
    provider = choices.get(normalized)
    if provider is None:
        raise ValueError(f"Unknown inference provider: {preference}")
    if provider not in available:
        raise RuntimeError(f"Requested {provider}, available providers: {sorted(available)}")
    return provider


def _numpy_tensor_dtype(tensor_type: str) -> type[np.floating]:
    choices = {
        "tensor(float)": np.float32,
        "tensor(float16)": np.float16,
    }
    dtype = choices.get(tensor_type)
    if dtype is None:
        raise ValueError(f"Unsupported YOLO input type: {tensor_type}")
    return dtype


def _prepare_model_blob(
    frame: np.ndarray,
    input_width: int,
    input_height: int,
    input_dtype: type[np.floating],
) -> np.ndarray:
    blob = cv2.dnn.blobFromImage(
        frame,
        scalefactor=1.0 / 255.0,
        size=(input_width, input_height),
        swapRB=True,
        crop=False,
    )
    return blob.astype(input_dtype, copy=False)


def _provider_chain(provider: str, *, cuda_graph: bool = False) -> list:
    cuda_options = {
        "cudnn_conv_algo_search": "HEURISTIC",
        "cudnn_conv_use_max_workspace": "1",
        "do_copy_in_default_stream": "1",
    }
    if provider == "TensorrtExecutionProvider":
        cache_directory = Path(__file__).resolve().parent.parent / ".cache" / "tensorrt"
        cache_directory.mkdir(parents=True, exist_ok=True)
        tensorrt_options = {
            "trt_fp16_enable": "True",
            "trt_engine_cache_enable": "True",
            "trt_engine_cache_path": str(cache_directory),
            "trt_timing_cache_enable": "True",
            "trt_timing_cache_path": str(cache_directory),
            "trt_cuda_graph_enable": str(cuda_graph),
        }
        return [
            ("TensorrtExecutionProvider", tensorrt_options),
            ("CUDAExecutionProvider", cuda_options),
            "CPUExecutionProvider",
        ]
    if provider == "CUDAExecutionProvider":
        return [("CUDAExecutionProvider", cuda_options), "CPUExecutionProvider"]
    return [provider]


def _static_tensor_shape(shape: list, label: str) -> tuple[int, ...]:
    if any(not isinstance(value, int) or value <= 0 for value in shape):
        raise ValueError(f"CUDA Graph requires a static {label} shape, got {shape}")
    return tuple(shape)


def decode_yolo(
    output: np.ndarray,
    *,
    frame_width: int,
    frame_height: int,
    input_width: int,
    input_height: int,
    output_format: str,
    output_layout: str,
    confidence_threshold: float,
    iou_threshold: float,
    target_class: int | None = None,
) -> list[Detection]:
    rows = np.asarray(output)
    if rows.ndim == 3 and rows.shape[0] == 1:
        rows = rows[0]
    if rows.ndim != 2:
        raise ValueError(f"Unsupported YOLO output shape: {np.asarray(output).shape}")
    actual_layout = (
        infer_output_layout((int(rows.shape[0]), int(rows.shape[1])), output_format)
        if output_layout == "auto"
        else output_layout
    )
    if actual_layout == "channels_first":
        rows = rows.T

    scale_x = frame_width / input_width
    scale_y = frame_height / input_height
    if output_format == "end2end":
        if rows.shape[1] != 6:
            raise ValueError(f"End-to-end YOLO output must have 6 columns, got {rows.shape}")
        scores = rows[:, 4]
        keep = scores >= confidence_threshold
        if target_class is not None:
            keep &= rows[:, 5].astype(np.int32) == target_class
        selected = rows[keep]
        scores = scores[keep]
        classes = selected[:, 5].astype(np.int32)
        boxes = np.column_stack((
            selected[:, 0] * scale_x,
            selected[:, 1] * scale_y,
            (selected[:, 2] - selected[:, 0]) * scale_x,
            (selected[:, 3] - selected[:, 1]) * scale_y,
        ))
    else:
        class_offset = 5 if output_format == "yolov5" else 4
        class_values = rows[:, class_offset:]
        if class_values.shape[1] == 0:
            raise ValueError(f"YOLO output has no class scores: {rows.shape}")
        classes = np.argmax(class_values, axis=1).astype(np.int32)
        row_indices = np.arange(rows.shape[0])
        scores = class_values[row_indices, classes]
        if output_format == "yolov5":
            scores = scores * rows[:, 4]
        keep = scores >= confidence_threshold
        if target_class is not None:
            keep &= classes == target_class
        selected = rows[keep, :4]
        scores = scores[keep]
        classes = classes[keep]
        boxes = np.column_stack((
            (selected[:, 0] - selected[:, 2] * 0.5) * scale_x,
            (selected[:, 1] - selected[:, 3] * 0.5) * scale_y,
            selected[:, 2] * scale_x,
            selected[:, 3] * scale_y,
        ))

    if boxes.shape[0] == 0:
        return []
    indices = _class_aware_nms(boxes, scores, classes, confidence_threshold, iou_threshold)
    if len(indices) == 0:
        return []
    detections: list[Detection] = []
    for index in np.asarray(indices).reshape(-1):
        x, y, width, height = boxes[int(index)]
        detections.append(Detection(
            float(x),
            float(y),
            float(x + width),
            float(y + height),
            float(scores[int(index)]),
            int(classes[int(index)]),
        ))
    return detections


def _class_aware_nms(
    boxes: np.ndarray,
    scores: np.ndarray,
    classes: np.ndarray,
    confidence_threshold: float,
    iou_threshold: float,
) -> list[int]:
    kept: list[int] = []
    for class_id in np.unique(classes):
        class_indices = np.flatnonzero(classes == class_id)
        local_indices = cv2.dnn.NMSBoxes(
            boxes[class_indices].tolist(),
            scores[class_indices].tolist(),
            confidence_threshold,
            iou_threshold,
        )
        kept.extend(int(class_indices[int(index)]) for index in np.asarray(local_indices).reshape(-1))
    return kept
