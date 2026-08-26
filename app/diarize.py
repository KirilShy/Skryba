"""Speaker diarization with pyannote, and merging its turns onto Whisper segments.

pyannote answers "who spoke when" but not "what did they say"; Whisper answers
the reverse. Neither aligns to the other, so we assign each Whisper segment the
speaker whose turns overlap it most in time.

Optional dependency: the app runs fine without pyannote installed, it just
can't label speakers.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from . import config

_pipeline = None
_pipeline_lock = threading.Lock()


class DiarizationUnavailable(RuntimeError):
    """Raised when pyannote or its weights aren't usable — always recoverable."""


def is_available() -> bool:
    try:
        import pyannote.audio  # noqa: F401
        import torch  # noqa: F401
    except ImportError:
        return False
    return bool(config.HF_TOKEN)


def _load_pipeline():
    """Load the pipeline once and keep it warm; it costs ~10s and ~1GB."""
    global _pipeline
    with _pipeline_lock:
        if _pipeline is not None:
            return _pipeline
        try:
            import torch
            from pyannote.audio import Pipeline
        except ImportError as exc:
            raise DiarizationUnavailable(
                "pyannote.audio is not installed. Run: uv pip install -e '.[diarize]'"
            ) from exc

        if not config.HF_TOKEN:
            raise DiarizationUnavailable(
                "No HF_TOKEN set. Create a free token at huggingface.co/settings/tokens "
                "and accept the terms for pyannote/speaker-diarization-3.1."
            )

        # pyannote 4.x renamed this argument from `use_auth_token` to `token`.
        # Pick whichever the installed version actually accepts.
        import inspect
        params = inspect.signature(Pipeline.from_pretrained).parameters
        auth_kw = "token" if "token" in params else "use_auth_token"
        pipeline = Pipeline.from_pretrained(
            config.DIARIZATION_MODEL, **{auth_kw: config.HF_TOKEN}
        )
        if pipeline is None:
            raise DiarizationUnavailable(
                "Hugging Face returned no pipeline. The usual cause is not having "
                "accepted the model terms for pyannote/speaker-diarization-3.1 "
                "and pyannote/segmentation-3.0 while signed in."
            )
        # MPS accelerates the segmentation model; some pyannote ops still fall
        # back to CPU, which torch handles transparently.
        if torch.backends.mps.is_available():
            try:
                pipeline.to(torch.device("mps"))
            except Exception:
                pass  # CPU is slower but always correct
        _pipeline = pipeline
        return _pipeline


def diarize(
    wav_path: Path,
    num_speakers: int | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> list[dict]:
    """Return [{'start', 'end', 'speaker'}] sorted by start time."""
    pipeline = _load_pipeline()

    hook = None
    if on_progress:
        try:
            from pyannote.audio.pipelines.utils.hook import ProgressHook

            class _Hook(ProgressHook):
                def __call__(self, step_name, step_artifact, file=None,
                             total=None, completed=None):
                    if total:
                        on_progress(min((completed or 0) / total, 1.0))

            hook = _Hook()
        except Exception:
            hook = None

    kwargs = {}
    if num_speakers:
        kwargs["num_speakers"] = int(num_speakers)

    if hook is not None:
        with hook as h:
            annotation = pipeline(str(wav_path), hook=h, **kwargs)
    else:
        annotation = pipeline(str(wav_path), **kwargs)

    turns = [
        {"start": float(turn.start), "end": float(turn.end), "speaker": str(label)}
        for turn, _, label in annotation.itertracks(yield_label=True)
    ]
    turns.sort(key=lambda t: t["start"])
    return turns


def assign_speakers(segments: list[dict], turns: list[dict]) -> list[dict]:
    """Label each Whisper segment with the speaker it overlaps most."""
    if not turns:
        return segments

    for seg in segments:
        best_label, best_overlap = None, 0.0
        for turn in turns:
            if turn["start"] >= seg["end"]:
                break  # turns are sorted; nothing later can overlap
            overlap = min(seg["end"], turn["end"]) - max(seg["start"], turn["start"])
            if overlap > best_overlap:
                best_overlap, best_label = overlap, turn["speaker"]
        seg["speaker"] = best_label
    return segments


def prettify_labels(segments: list[dict]) -> list[str]:
    """Rename SPEAKER_00/01/... to 'Speaker 1/2/...' in order of first appearance.

    pyannote's numbering is arbitrary; ordering by who talks first reads better.
    Returns the ordered list of display names.
    """
    mapping: dict[str, str] = {}
    for seg in segments:
        raw = seg.get("speaker")
        if raw and raw not in mapping:
            mapping[raw] = f"Speaker {len(mapping) + 1}"
    for seg in segments:
        raw = seg.get("speaker")
        if raw:
            seg["speaker"] = mapping[raw]
    return list(mapping.values())
