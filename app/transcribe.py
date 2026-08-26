"""Whisper transcription on the Apple Silicon GPU via MLX.

mlx_whisper.transcribe() is a single blocking call with no progress hook, but
with verbose=True it prints one line per decoded segment as it goes. We capture
that stream to drive the progress bar and the live transcript view, while the
*authoritative* segments still come from the returned dict. If mlx_whisper ever
changes its console format we lose the live preview, never the transcript.
"""
from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path
from typing import Callable

from . import config

# e.g. "[00:23.480 --> 00:27.120]  and then we ship it"
_VERBOSE_LINE = re.compile(
    r"^\[(?P<start>[\d:.]+)\s*-->\s*(?P<end>[\d:.]+)\]\s?(?P<text>.*)$"
)


def _parse_clock(value: str) -> float:
    """'00:23.480' or '01:00:23.480' -> seconds."""
    parts = value.split(":")
    try:
        seconds = float(parts[-1])
        if len(parts) >= 2:
            seconds += int(parts[-2]) * 60
        if len(parts) >= 3:
            seconds += int(parts[-3]) * 3600
        return seconds
    except (ValueError, IndexError):
        return 0.0


class _ProgressTap(io.TextIOBase):
    """A stdout stand-in that turns mlx_whisper's verbose lines into callbacks."""

    def __init__(self, duration: float, on_segment: Callable[[dict, float], None]):
        self._buffer = ""
        self._duration = max(duration, 1e-6)
        self._on_segment = on_segment

    def write(self, chunk: str) -> int:
        self._buffer += chunk
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._handle(line)
        return len(chunk)

    def _handle(self, line: str) -> None:
        match = _VERBOSE_LINE.match(line.strip())
        if not match:
            return
        start = _parse_clock(match.group("start"))
        end = _parse_clock(match.group("end"))
        segment = {"start": start, "end": end, "text": match.group("text").strip()}
        progress = min(end / self._duration, 1.0)
        try:
            self._on_segment(segment, progress)
        except Exception:
            # A failing UI callback must never abort a 40-minute transcription.
            pass

    def flush(self) -> None:  # pragma: no cover - required by TextIOBase
        return


def transcribe(
    wav_path: Path,
    duration: float,
    model_key: str = config.DEFAULT_WHISPER,
    language: str | None = None,
    on_segment: Callable[[dict, float], None] | None = None,
) -> dict:
    """Run Whisper and return {'segments': [...], 'language': str, 'text': str}.

    `language=None` lets Whisper auto-detect. Passing an explicit language is
    both faster and more accurate when you already know it.
    """
    import mlx_whisper  # imported lazily: loading MLX costs ~2s

    repo = config.WHISPER_MODELS.get(model_key, model_key)
    tap = _ProgressTap(duration, on_segment) if on_segment else None

    kwargs = {
        "path_or_hf_repo": repo,
        "verbose": bool(on_segment),
        # Whisper's known failure mode is looping on silence; these are the
        # standard guards from the reference implementation.
        "condition_on_previous_text": False,
        "compression_ratio_threshold": 2.4,
        "no_speech_threshold": 0.6,
    }
    if language:
        kwargs["language"] = language

    if tap is not None:
        with contextlib.redirect_stdout(tap):
            result = mlx_whisper.transcribe(str(wav_path), **kwargs)
    else:
        result = mlx_whisper.transcribe(str(wav_path), **kwargs)

    segments = [
        {
            "start": float(s["start"]),
            "end": float(s["end"]),
            "text": (s.get("text") or "").strip(),
            "speaker": None,
        }
        for s in result.get("segments", [])
        if (s.get("text") or "").strip()
    ]
    return {
        "segments": segments,
        "language": result.get("language"),
        "text": (result.get("text") or "").strip(),
    }
