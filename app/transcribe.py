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


SAMPLE_RATE = 16000


def load_audio(wav_path: Path):
    """Decode the whole file to a float32 array once, so windows are cheap slices."""
    from mlx_whisper.audio import load_audio as _load

    return _load(str(wav_path), SAMPLE_RATE)


def transcribe_window(
    samples,
    offset: float,
    model_key: str = config.DEFAULT_WHISPER,
    language: str | None = None,
    on_segment: Callable[[dict, float], None] | None = None,
) -> dict:
    """Transcribe one slice of audio.

    `samples` is the slice; `offset` is where it starts in the full recording,
    used to shift the returned timestamps back into absolute time. Progress
    reported to `on_segment` is a 0..1 fraction *within this window*.
    """
    import mlx_whisper

    repo = config.WHISPER_MODELS.get(model_key, model_key)
    window_seconds = max(len(samples) / SAMPLE_RATE, 1e-6)
    tap = _ProgressTap(window_seconds, on_segment) if on_segment else None

    kwargs = {
        "path_or_hf_repo": repo,
        "verbose": bool(on_segment),
        # Whisper's known failure mode is looping on silence; these are the
        # standard guards. Disabling previous-text conditioning also means
        # window boundaries cost us almost no accuracy.
        "condition_on_previous_text": False,
        "compression_ratio_threshold": 2.4,
        "no_speech_threshold": 0.6,
    }
    if language:
        kwargs["language"] = language

    if tap is not None:
        with contextlib.redirect_stdout(tap):
            result = mlx_whisper.transcribe(samples, **kwargs)
    else:
        result = mlx_whisper.transcribe(samples, **kwargs)

    # Whisper pads its final 30s window with silence and will happily emit
    # segments inside that padding, past the end of the real audio. Over a whole
    # file that just overshoots the ending; across chunks it makes every
    # boundary overlap the next chunk and duplicate the speech there. Drop
    # anything starting past the window and clamp anything spilling over it.
    segments = []
    for seg in result.get("segments", []):
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        start = float(seg["start"])
        end = min(float(seg["end"]), window_seconds)
        if start >= window_seconds - 0.05 or end <= start:
            continue
        segments.append({
            "start": start + offset,
            "end": end + offset,
            "text": text,
            "speaker": None,
        })
    return {"segments": segments, "language": result.get("language")}


def detect_language(samples, model_key: str = config.DEFAULT_WHISPER,
                    probes: int = 5) -> str | None:
    """Guess the language from the loudest windows, by majority vote.

    Detecting from the start of a file is unreliable: recordings often open with
    silence or throat-clearing, and Whisper will confidently label that as any
    language at all. Sampling the parts with the most energy, and taking a vote,
    is far more stable on real meeting audio.
    """
    import numpy as np
    import mlx_whisper

    window = 30 * SAMPLE_RATE
    if len(samples) <= window:
        candidates = [0]
    else:
        # Rank non-overlapping windows by RMS and probe the loudest handful.
        starts = list(range(0, len(samples) - window, window))
        energies = [(float(np.sqrt((samples[s:s + window] ** 2).mean())), s) for s in starts]
        energies.sort(reverse=True)
        candidates = [s for _, s in energies[:probes]]

    repo = config.WHISPER_MODELS.get(model_key, model_key)
    votes: dict[str, int] = {}
    for start in candidates:
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                result = mlx_whisper.transcribe(
                    samples[start:start + window], path_or_hf_repo=repo, verbose=False
                )
        except Exception:
            continue
        lang = result.get("language")
        if lang:
            votes[lang] = votes.get(lang, 0) + 1
    if not votes:
        return None
    return max(votes.items(), key=lambda kv: kv[1])[0]
