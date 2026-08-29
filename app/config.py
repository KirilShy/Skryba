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
# Same keys, resolved instead to CTranslate2 model names for the faster-whisper
# backend (transcribe_fw.py), which downloads its own converted weights.
WHISPER_MODELS_FASTER = {
    "turbo": "large-v3-turbo",
    "large": "large-v3",
    "medium": "medium",
    "small": "small",
}
DEFAULT_WHISPER = os.environ.get("WHISPER_MODEL", "turbo")

# mlx on Apple Silicon, faster-whisper (CUDA if available, else CPU) elsewhere.
# Override to force one explicitly, e.g. to run faster-whisper on a Mac too.
TRANSCRIBE_BACKEND = os.environ.get("TRANSCRIBE_BACKEND", "").strip().lower()

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

# NOTE: mlx-whisper raises "Beam search decoder is not yet implemented", so
# beam search is unavailable on this backend regardless of what we ask for.
# Decoding stays greedy with Whisper's temperature-fallback schedule.

# Whisper's own hallucination suppression: when a stretch is silent for longer
# than this, discard text generated over it. Requires word timestamps.
HALLUCINATION_SILENCE_SECONDS = float(
    os.environ.get("HALLUCINATION_SILENCE_SECONDS", "2.0")
)

# Seeded into the decoder as context so proper nouns and jargon are recognised
# rather than guessed at phonetically. Override per install.
INITIAL_PROMPT = os.environ.get("INITIAL_PROMPT", "").strip() or None
