from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from rhodes_fast.model_download import ensure_default_model


class ModelDownloadTests(unittest.TestCase):
    def test_downloads_and_verifies_the_default_model(self) -> None:
        payload = b"a tiny model fixture"
        opener = Mock(return_value=io.BytesIO(payload))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "settings.txt"
            config.touch()
            target = ensure_default_model(
                config,
                Path("models/yolov5n.onnx"),
                url="https://example.invalid/yolov5n.onnx",
                expected_sha256=hashlib.sha256(payload).hexdigest(),
                expected_size=len(payload),
                opener=opener,
            )

            self.assertEqual(target.read_bytes(), payload)
            self.assertFalse(target.with_suffix(".onnx.part").exists())
            opener.assert_called_once()

    def test_never_downloads_over_a_custom_model_path(self) -> None:
        opener = Mock(side_effect=AssertionError("download should not run"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = ensure_default_model(root / "settings.txt", Path("models/custom.onnx"), opener=opener)

        self.assertEqual(target, (root / "models/custom.onnx").resolve())
        opener.assert_not_called()

    def test_discards_a_download_with_the_wrong_checksum(self) -> None:
        payload = b"not the expected file"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "models" / "yolov5n.onnx"
            with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                ensure_default_model(
                    root / "settings.txt",
                    Path("models/yolov5n.onnx"),
                    url="https://example.invalid/yolov5n.onnx",
                    expected_sha256="0" * 64,
                    expected_size=len(payload),
                    opener=Mock(return_value=io.BytesIO(payload)),
                )

            self.assertFalse(target.exists())
            self.assertFalse(target.with_suffix(".onnx.part").exists())


if __name__ == "__main__":
    unittest.main()
