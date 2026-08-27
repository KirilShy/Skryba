"""Paths and tunables. Everything is overridable by environment variable."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
JOB_DIR = DATA_DIR / "jobs"

for _d in (UPLOAD_DIR, JOB_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Whisper weights, converted to MLX and hosted by the mlx-community org.
# large-v3-turbo is the sweet spot on an M2: near-large accuracy, ~8x the speed.
WHISPER_MODELS = {
    "turbo": "mlx-community/whisper-large-v3-turbo",
    "large": "mlx-community/whisper-large-v3-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "small": "mlx-community/whisper-small-mlx",
}
DEFAULT_WHISPER = os.environ.get("WHISPER_MODEL", "turbo")

DIARIZATION_MODEL = os.environ.get("DIARIZATION_MODEL", "pyannote/speaker-diarization-3.1")
HF_TOKEN = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5")

# Audio containers we hand straight to ffmpeg.
ALLOWED_SUFFIXES = {
    ".m4a", ".mp3", ".wav", ".aac", ".flac", ".ogg", ".opus",
    ".mp4", ".mov", ".mkv", ".webm", ".m4v", ".aiff", ".wma",
}

# Where "Save to folder" writes the exported transcripts. Sits next to the app
# so the files are easy to find outside the browser.
EXPORT_DIR = Path(os.environ.get("EXPORT_DIR", BASE_DIR.parent / "transcripts"))
EXPORT_DIR.mkdir(parents=True, exist_ok=True)

# Summary provider. "auto" prefers OpenRouter when its key is present, then the
# Anthropic API. Set explicitly to "anthropic" or "openrouter" to force one.
SUMMARY_PROVIDER = os.environ.get("SUMMARY_PROVIDER", "auto").strip().lower()
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-sonnet-5")
