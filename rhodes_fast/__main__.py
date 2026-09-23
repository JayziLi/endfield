from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .console import configure_console_output
from .config import default_config, load_config, save_config
from .model_download import ensure_default_model
from .pipeline import (
    benchmark_model,
    check_connections,
    compare_latency_logs,
    run_pipeline,
)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def main() -> None:
    configure_console_output()
    parser = argparse.ArgumentParser(description="Low-latency OBS YOLO inference with KMBox control")
    parser.add_argument("--config", type=Path, default=Path("settings.txt"))
    parser.add_argument("--stop-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--preview-port", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--preview-enable-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--runtime-aim-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--trail-settings-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--autostart", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--benchmark-output", type=Path, help="write the benchmark JSON report to this path")
    parser.add_argument(
        "--latency-log",
        type=Path,
        help="record aim-loop latency samples for every frame to this CSV",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--gui", action="store_true", help="open the graphical control panel")
    mode.add_argument("--gui-classic", action="store_true", help="open the classic control panel")
    mode.add_argument(
        "--download-model",
        action="store_true",
        help="download and verify the default YOLOv5n model, then exit",
    )
    mode.add_argument("--check", action="store_true", help="check OBS and KMBox connections")
    mode.add_argument("--benchmark", type=_positive_int, metavar="N", help="benchmark the model for N iterations")
    mode.add_argument(
        "--analyze",
        type=Path,
        nargs="+",
        metavar="CSV",
        help="compare recorded latency logs and report the loop delay of each",
    )
    mode.add_argument(
        "--pipeline-benchmark",
        type=_positive_int,
        metavar="N",
        help="benchmark the live input-to-target pipeline for N frames",
    )
    args = parser.parse_args()
    if args.gui and args.autostart:
        parser.error("--autostart is supported by --gui-classic only")

    config = None
    try:
        if not args.config.is_file() and args.config == Path("settings.txt"):
            save_config(default_config(), args.config)
            print(f"Created default settings: {args.config.resolve()}")
        bootstrap_config = load_config(args.config, validate_model=False)
        model_path = ensure_default_model(args.config, bootstrap_config.model.path)
        if args.download_model:
            if not model_path.is_file():
                raise FileNotFoundError(
                    f"Custom ONNX model not found: {model_path}. "
                    "Automatic download only applies to models/yolov5n.onnx."
                )
            print(f"Model ready: {model_path}")
            return
        if args.gui:
            from .gui_web.app import main as run_gui

            run_gui(args.config)
            return
        if args.gui_classic:
            from .gui import run_gui

            run_gui(args.config, auto_start=args.autostart)
            return
        config = load_config(args.config)
        if args.check:
            check_connections(
                config, args.stop_file, algorithms_dir=args.config.parent / "algorithms"
            )
        elif args.benchmark is not None:
            benchmark_model(config, args.benchmark, args.stop_file)
        elif args.analyze:
            print(compare_latency_logs(config, args.analyze))
        elif args.pipeline_benchmark is not None:
            from .pipeline_benchmark import run_pipeline_benchmark

            run_pipeline_benchmark(
                config,
                args.pipeline_benchmark,
                stop_file=args.stop_file,
                output_path=args.benchmark_output,
            )
        else:
            run_pipeline(
                config,
                stop_file=args.stop_file,
                preview_port=args.preview_port,
                preview_enable_file=args.preview_enable_file,
                runtime_aim_file=args.runtime_aim_file,
                latency_log=args.latency_log,
                algorithms_dir=args.config.parent / "algorithms",
                trail_settings_file=args.trail_settings_file,
            )
    except Exception as exc:
        if args.gui or args.gui_classic:
            try:
                import tkinter as tk
                from tkinter import messagebox

                root = tk.Tk()
                root.withdraw()
                messagebox.showerror("Endfield 无法启动", str(exc), parent=root)
                root.destroy()
            except Exception:
                pass
        prefix = "错误" if config is None or config.ui.language == "zh" else "Error"
        print(f"{prefix}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
