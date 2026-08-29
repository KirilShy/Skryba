"""Picks a Whisper backend and re-exports its interface.

MLX (Apple Silicon GPU) on macOS; faster-whisper (CUDA if a GPU is visible,
otherwise CPU) everywhere else. Both modules expose the same four names, so
callers (jobs.py) never need to know which one is active. Force a specific
backend with the TRANSCRIBE_BACKEND env var — e.g. to test faster-whisper on
a Mac, or to force CPU-only faster-whisper for debugging.
"""
from __future__ import annotations

import sys

from . import config

BACKEND = config.TRANSCRIBE_BACKEND or ("mlx" if sys.platform == "darwin" else "faster-whisper")

if BACKEND == "mlx":
    from .transcribe_mlx import SAMPLE_RATE, detect_language, load_audio, transcribe_window
elif BACKEND == "faster-whisper":
    from .transcribe_fw import SAMPLE_RATE, detect_language, load_audio, transcribe_window
else:
    raise RuntimeError(
        f"Unknown TRANSCRIBE_BACKEND={BACKEND!r}; expected 'mlx' or 'faster-whisper'."
    )

__all__ = ["BACKEND", "SAMPLE_RATE", "detect_language", "load_audio", "transcribe_window"]
