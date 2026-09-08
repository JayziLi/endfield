# Endfield

Endfield is a Windows application for low-latency YOLO inference from OBS or UDP video sources, with optional KMBox Net relative mouse output.

完整中文说明见 [Endfield 产品文档](docs/product-guide.zh-CN.md)。

## Features

- UDP JPEG datagrams and fragmented JPEG reassembly.
- UDP MPEG-TS/H.264 and OBS WebSocket inputs.
- ONNX Runtime with TensorRT FP16, CUDA, or CPU execution.
- Optional CUDA Graph and GPU preprocessing paths with automatic fallback.
- Automatic YOLO output layout, detection tensor, and class-count inspection.
- YOLOv5, YOLOv8, and end-to-end NMS output support.
- Latest-frame processing to avoid stale-frame queues.
- Target-class filtering, class-aware NMS, configurable FOV, and dynamic proportional control.
- On-demand live preview that stops processing when its tab is not selected.
- Reproducible live pipeline benchmark reports with per-stage latency statistics.

## Pipeline

```text
OBS / UDP sender
  -> UDP receive and JPEG/video decode
  -> latest-frame buffer
  -> GPU preprocessing and TensorRT/CUDA inference
  -> YOLO decode and class-aware NMS
  -> target selection and proportional control
  -> optional KMBox Net relative movement
```

## Requirements

- Windows 11 and Python 3.11 or newer.
- An ONNX YOLO detection model. Model weights are not included.
- An NVIDIA GPU and compatible driver for CUDA or TensorRT acceleration.
- OBS or another compatible sender for live input.
- KMBox Net only when external movement output is needed.

## Setup

```powershell
git clone https://github.com/JayziLi/endfield.git
cd endfield
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

Open the control panel:

```powershell
.\.venv\Scripts\pythonw.exe -m rhodes_fast --gui --config settings.txt
```

You can also double-click `start.bat`. Select your ONNX model and configure the UDP, OBS, and KMBox fields in the GUI before starting inference. The checked-in settings are non-functional examples: KMBox and target control are disabled by default, and no credentials or model files are included.

## Input

The default example listens for UDP JPEG data on `0.0.0.0:4455`. A frame may arrive in one datagram or in consecutive fragments beginning with JPEG marker `FFD8` and ending with `FFD9`.

MPEG-TS/H.264 senders can target a configured host with a URL such as:

```text
udp://192.168.1.100:4455?pkt_size=1316
```

For lowest latency, send frames at the model input resolution and keep the live preview closed when it is not needed.

## Commands

Check input and optional KMBox connectivity:

```powershell
.\.venv\Scripts\python.exe -m rhodes_fast --config settings.txt --check
```

Benchmark model preprocessing, inference, and postprocessing:

```powershell
.\.venv\Scripts\python.exe -m rhodes_fast --config settings.txt --benchmark 200
```

Benchmark the live input-to-target pipeline and save a JSON report under `.cache/benchmarks`:

```powershell
.\.venv\Scripts\python.exe -m rhodes_fast --config settings.txt --pipeline-benchmark 1000
```

The pipeline benchmark disables preview and KMBox output. It reports mean, P50, P95, P99, and maximum latency for UDP assembly, decoding, queueing, preprocessing, inference, YOLO/NMS postprocessing, target selection, and receiver total.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Data and responsible use

Endfield runs locally and does not intentionally upload frames, models, or settings. Network traffic depends on the configured OBS/UDP and KMBox endpoints.

Use this software only on systems and software where you have authorization. Follow applicable platform rules and local laws.
