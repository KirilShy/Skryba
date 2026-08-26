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
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-i", str(src),
         "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0 or not dst.exists():
        raise AudioError(f"ffmpeg failed on {src.name}: {proc.stderr.strip()[-600:]}")
    return dst
