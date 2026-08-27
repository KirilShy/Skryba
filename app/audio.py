"""ffmpeg/ffprobe helpers."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


class AudioError(RuntimeError):
    pass


def probe(path: Path) -> dict:
    """Return {'duration': float, 'has_audio': bool} for a media file."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise AudioError(f"ffprobe could not read {path.name}: {proc.stderr.strip()[:400]}")
    info = json.loads(proc.stdout or "{}")
    streams = info.get("streams", [])
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    duration = 0.0
    try:
        duration = float(info.get("format", {}).get("duration", 0.0))
    except (TypeError, ValueError):
        pass
    if not duration:
        for s in streams:
            if s.get("codec_type") == "audio" and s.get("duration"):
                try:
                    duration = float(s["duration"])
                except (TypeError, ValueError):
                    pass
                break
    return {"duration": duration, "has_audio": has_audio}


def to_wav16k(src: Path, dst: Path) -> Path:
    """Decode anything to 16 kHz mono PCM — what both Whisper and pyannote want.

    Doing this once up front means the file is only demuxed a single time even
    when we run transcription and diarization over it.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)

    # Clean up the signal before Whisper ever sees it. Meeting recordings from a
    # table mic swing between loud and near-inaudible — one file measured 0.002
    # RMS in places — and Whisper transcribes quiet speech badly or invents
    # filler over it. In order: drop sub-80Hz rumble (HVAC, desk bumps), even
    # out the dynamics so a distant speaker is as loud as a close one, then
    # normalise to broadcast loudness.
    filters = "highpass=f=80,dynaudnorm=f=200:g=15:p=0.9:m=10,loudnorm=I=-16:TP=-1.5:LRA=11"
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-i", str(src),
         "-vn", "-af", filters, "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(dst)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        # Filtering is an enhancement, never a hard requirement — fall back to a
        # plain decode so an odd input still transcribes.
        proc = subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-i", str(src),
             "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)],
            capture_output=True, text=True,
        )
    if proc.returncode != 0 or not dst.exists():
        raise AudioError(f"ffmpeg failed on {src.name}: {proc.stderr.strip()[-600:]}")
    return dst


# Chunking exists so a long transcription can be paused and resumed. Cutting on
# silence keeps Whisper from being handed half a word at a boundary.
CHUNK_TARGET_SECONDS = 120.0
CHUNK_SEARCH_WINDOW = 25.0
MIN_CHUNK_SECONDS = 20.0


def find_silences(path: Path, noise_db: int = -30, min_silence: float = 0.35) -> list[float]:
    """Return midpoints of detected silences, in seconds."""
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-i", str(path),
         "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    # silencedetect reports on stderr, one "silence_start"/"silence_end" per line.
    starts, mids = [], []
    for line in proc.stderr.splitlines():
        if "silence_start:" in line:
            try:
                starts.append(float(line.split("silence_start:")[1].split()[0]))
            except (ValueError, IndexError):
                pass
        elif "silence_end:" in line and starts:
            try:
                end = float(line.split("silence_end:")[1].split()[0])
                mids.append((starts.pop() + end) / 2.0)
            except (ValueError, IndexError):
                pass
    return sorted(mids)


def plan_chunks(path: Path, duration: float,
                target: float = CHUNK_TARGET_SECONDS) -> list[dict]:
    """Split [0, duration] into chunks, snapping cuts to silence where possible."""
    if duration <= target:
        return [{"start": 0.0, "end": duration}]

    try:
        silences = find_silences(path)
    except Exception:
        silences = []  # a failed probe just means we cut on fixed boundaries

    cuts: list[float] = []
    goal = target
    while goal < duration - MIN_CHUNK_SECONDS:
        best, best_dist = None, CHUNK_SEARCH_WINDOW
        for s in silences:
            if cuts and s <= cuts[-1] + MIN_CHUNK_SECONDS:
                continue
            dist = abs(s - goal)
            if dist < best_dist:
                best, best_dist = s, dist
        cut = best if best is not None else goal
        if cuts and cut <= cuts[-1] + MIN_CHUNK_SECONDS:
            cut = cuts[-1] + target  # never emit a degenerate chunk
        if cut >= duration - MIN_CHUNK_SECONDS:
            break
        cuts.append(cut)
        goal = cut + target

    bounds = [0.0, *cuts, duration]
    return [{"start": a, "end": b} for a, b in zip(bounds, bounds[1:]) if b - a > 0.01]


def silence_regions(path: Path, noise_db: int = -35,
                    min_silence: float = 1.0) -> list[tuple[float, float]]:
    """Return [(start, end)] spans of sustained silence."""
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-i", str(path),
         "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    regions, start = [], None
    for line in proc.stderr.splitlines():
        if "silence_start:" in line:
            try:
                start = float(line.split("silence_start:")[1].split()[0])
            except (ValueError, IndexError):
                start = None
        elif "silence_end:" in line and start is not None:
            try:
                regions.append((start, float(line.split("silence_end:")[1].split()[0])))
            except (ValueError, IndexError):
                pass
            start = None
    return regions


def speech_fraction(start: float, end: float,
                    silences: list[tuple[float, float]]) -> float:
    """How much of [start, end] is not silence, as 0..1."""
    span = end - start
    if span <= 0:
        return 0.0
    quiet = sum(
        max(0.0, min(end, s_end) - max(start, s_start))
        for s_start, s_end in silences
    )
    return max(0.0, 1.0 - quiet / span)
