from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ModelContract:
    output_shape: tuple[int | str | None, ...]
    output_format: str | None
    output_layout: str
    class_count: int | None = None
    output_index: int = 0

    @property
    def feature_count(self) -> int:
        value = self.output_shape[1] if self.output_layout == "channels_first" else self.output_shape[2]
        if not isinstance(value, int):
            raise ValueError(f"YOLO feature dimension must be static, got {self.output_shape}")
        return value

    def class_count_for(self, output_format: str) -> int | None:
        if self.class_count is not None:
            return self.class_count
        if self.output_format is None or output_format != self.output_format:
            return None
        if output_format == "end2end":
            return None
        offset = 5 if output_format == "yolov5" else 4
        count = self.feature_count - offset
        return count if count > 0 else None


def inspect_model(path: Path, output_format_hint: str | None = None) -> ModelContract:
    import onnxruntime as ort

    session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    return inspect_session(session, output_format_hint)


def inspect_session(session, output_format_hint: str | None = None) -> ModelContract:
    outputs = session.get_outputs()
    if not outputs:
        raise ValueError("The model does not expose a YOLO output")
    metadata = session.get_modelmeta().custom_metadata_map
    class_count = _class_count_from_metadata(metadata)
    metadata_end2end = _metadata_flag(metadata.get("end2end"))
    effective_format_hint = "end2end" if metadata_end2end else output_format_hint
    contracts: list[ModelContract] = []
    for index, output in enumerate(outputs):
        if len(output.shape) != 3:
            continue
        try:
            contract = infer_contract_from_shape(
                tuple(output.shape),
                class_count=class_count,
                output_format_hint=effective_format_hint,
            )
        except ValueError:
            continue
        output_format = contract.output_format
        if metadata_end2end and contract.feature_count >= 6:
            output_format = "end2end"
        contracts.append(
            ModelContract(
                contract.output_shape,
                output_format,
                contract.output_layout,
                contract.class_count,
                index,
            )
        )
    if not contracts:
        shapes = [output.shape for output in outputs]
        raise ValueError(f"No supported 3D YOLO output found among {shapes}")
    return max(contracts, key=lambda contract: _contract_score(contract, effective_format_hint))


def infer_contract_from_shape(
    shape: tuple[int | str | None, ...],
    *,
    class_count: int | None = None,
    output_format_hint: str | None = None,
) -> ModelContract:
    if len(shape) != 3 or (isinstance(shape[0], int) and shape[0] != 1):
        raise ValueError(f"Expected output shape [1,N,C] or [1,C,N], got {shape}")
    first, second = shape[1:]
    metadata_matches: list[tuple[str, str]] = []
    if class_count is not None:
        for layout, features in (("channels_first", first), ("candidates_first", second)):
            if features == class_count + 5:
                metadata_matches.append(("yolov5", layout))
            if features == class_count + 4:
                metadata_matches.append(("yolov8", layout))
    if len(metadata_matches) == 1:
        output_format, output_layout = metadata_matches[0]
        candidates = second if output_layout == "channels_first" else first
        if not (class_count <= 2 and _small_six_feature_output(shape, output_layout, candidates)):
            return ModelContract(shape, output_format, output_layout, class_count)

    output_layout = infer_output_layout((first, second), output_format_hint)
    features = first if output_layout == "channels_first" else second
    candidates = second if output_layout == "channels_first" else first
    if not isinstance(features, int):
        raise ValueError(f"YOLO feature dimension must be static, got {shape}")

    output_format: str | None = None
    if features == 6:
        output_format = "yolov5" if isinstance(candidates, int) and candidates > 1_000 else None
    elif features in {5, 84}:
        output_format = "yolov8"
    elif features == 85:
        output_format = "yolov5"
    return ModelContract(shape, output_format, output_layout, class_count)


def _contract_score(contract: ModelContract, output_format_hint: str | None) -> tuple[int, int, int]:
    hint_match = 0
    if output_format_hint == "end2end":
        hint_match = int(contract.feature_count == 6)
    elif output_format_hint is not None:
        hint_match = int(contract.output_format == output_format_hint)
    known = int(contract.class_count is not None) + int(contract.output_format is not None)
    candidate_dimension = contract.output_shape[2] if contract.output_layout == "channels_first" else contract.output_shape[1]
    candidates = candidate_dimension if isinstance(candidate_dimension, int) else 0
    return hint_match, known, candidates


def _small_six_feature_output(shape, output_layout: str, candidates) -> bool:
    features = shape[1] if output_layout == "channels_first" else shape[2]
    return features == 6 and isinstance(candidates, int) and candidates <= 1_000


def _class_count_from_metadata(metadata: dict[str, str]) -> int | None:
    raw_names = metadata.get("names")
    if not raw_names:
        return None
    try:
        names = ast.literal_eval(raw_names)
    except (SyntaxError, ValueError):
        return None
    if isinstance(names, (dict, list, tuple)) and len(names) > 0:
        return len(names)
    return None


def _metadata_flag(value: str | None) -> bool:
    return value is not None and value.strip().casefold() in {"1", "true", "yes", "on"}


def infer_output_layout(
    shape: tuple[int | str | None, int | str | None], output_format: str | None = None
) -> str:
    first, second = shape
    minimum_features = {"yolov5": 6, "yolov8": 5}.get(output_format)
    if minimum_features is not None:
        if isinstance(first, int) and first < minimum_features and isinstance(second, int) and second >= minimum_features:
            return "candidates_first"
        if isinstance(second, int) and second < minimum_features and isinstance(first, int) and first >= minimum_features:
            return "channels_first"
    if output_format == "end2end":
        if second == 6:
            return "candidates_first"
        if first == 6:
            return "channels_first"
    known_feature_counts = {6, 84, 85}
    if second in known_feature_counts and first not in known_feature_counts:
        return "candidates_first"
    if first in known_feature_counts and second not in known_feature_counts:
        return "channels_first"
    if isinstance(first, int) and not isinstance(second, int):
        return "channels_first"
    if not isinstance(first, int) and isinstance(second, int):
        return "candidates_first"
    if not isinstance(first, int) or not isinstance(second, int):
        raise ValueError(f"Cannot infer YOLO output layout from dynamic shape {shape}")
    if first == 0:
        return "candidates_first"
    if second == 0:
        return "channels_first"
    if first == second:
        raise ValueError(f"Cannot infer YOLO output layout from square shape {shape}")
    return "channels_first" if first < second else "candidates_first"
