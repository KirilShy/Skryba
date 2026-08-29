"""Whisper transcription via faster-whisper (CTranslate2), for hosts without
Apple Silicon. Selected automatically by transcribe.py; uses CUDA when a GPU
is visible, otherwise falls back to CPU int8.

Unlike mlx_whisper, faster-whisper's transcribe() returns the segment list as
a generator, and its TranscriptionInfo (including the detected language) is
already populated before that generator is consumed — so, unlike the MLX
backend, we don't need to scrape console output for progress: we drive it
straight off the generator as segments come out of the model.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from . import config

SAMPLE_RATE = 16000

_model_cache: dict[tuple[str, str, str], object] = {}


def _device_and_compute() -> tuple[str, str]:
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", "float16"
    except Exception:
        pass
    return "cpu", "int8"


def _get_model(repo: str):
    from faster_whisper import WhisperModel

    device, compute_type = _device_and_compute()
    key = (repo, device, compute_type)
    model = _model_cache.get(key)
    if model is None:
        model = WhisperModel(repo, device=device, compute_type=compute_type)
        _model_cache[key] = model
    return model


def load_audio(wav_path: Path):
    """Decode the whole file to a float32 array once, so windows are cheap slices."""
    from faster_whisper.audio import decode_audio

    return decode_audio(str(wav_path), sampling_rate=SAMPLE_RATE)


def transcribe_window(
    samples,
    offset: float,
    model_key: str = config.DEFAULT_WHISPER,
    language: str | None = None,
    on_segment: Callable[[dict, float], None] | None = None,
) -> dict:
    """Transcribe one slice of audio.

    Mirrors transcribe_mlx.transcribe_window: `samples` is the slice, `offset`
    shifts returned timestamps back into absolute time, and `on_segment` gets
    0..1 progress *within this window*.
    """
    repo = config.WHISPER_MODELS_FASTER.get(model_key, model_key)
    model = _get_model(repo)
    window_seconds = max(len(samples) / SAMPLE_RATE, 1e-6)

    kwargs = {
        # Mirrors the MLX backend's guards against Whisper's known failure
        # mode of looping on silence, plus word timestamps so hallucinated
        # text over a silent stretch can be located and dropped.
        "condition_on_previous_text": False,
        "compression_ratio_threshold": 2.4,
        "no_speech_threshold": 0.6,
        "word_timestamps": True,
        "hallucination_silence_threshold": config.HALLUCINATION_SILENCE_SECONDS,
    }
    if config.INITIAL_PROMPT:
        kwargs["initial_prompt"] = config.INITIAL_PROMPT
    if language:
        kwargs["language"] = language

    seg_iter, info = model.transcribe(samples, **kwargs)

    # Same overshoot guard as the MLX backend: Whisper pads its final window
    # with silence and will emit segments inside that padding.
    segments = []
    for seg in seg_iter:
        text = (seg.text or "").strip()
        if not text:
            continue
        start = float(seg.start)
        end = min(float(seg.end), window_seconds)
        if start >= window_seconds - 0.05 or end <= start:
            continue
        segments.append({
            "start": start + offset,
            "end": end + offset,
            "text": text,
            "speaker": None,
        })
        if on_segment is not None:
            progress = min(end / window_seconds, 1.0)
            try:
                on_segment({"start": start, "end": end, "text": text}, progress)
            except Exception:
                # A failing UI callback must never abort a 40-minute transcription.
                pass
    return {"segments": segments, "language": info.language}


def detect_language(samples, model_key: str = config.DEFAULT_WHISPER,
                    probes: int = 5) -> str | None:
    """Guess the language from the loudest windows, by majority vote.

    Same rationale as the MLX backend: a recording's opening seconds are
    often silence or throat-clearing, so probing the loudest windows instead
    is far more stable on real meeting audio.
    """
    import numpy as np

    repo = config.WHISPER_MODELS_FASTER.get(model_key, model_key)
    model = _get_model(repo)

    window = 30 * SAMPLE_RATE
    if len(samples) <= window:
        candidates = [0]
    else:
        starts = list(range(0, len(samples) - window, window))
        energies = [(float(np.sqrt((samples[s:s + window] ** 2).mean())), s) for s in starts]
        energies.sort(reverse=True)
        candidates = [s for _, s in energies[:probes]]

    votes: dict[str, int] = {}
    for start in candidates:
        try:
            _, info = model.transcribe(samples[start:start + window], language=None)
        except Exception:
            continue
        if info.language:
            votes[info.language] = votes.get(info.language, 0) + 1
    if not votes:
        return None
    return max(votes.items(), key=lambda kv: kv[1])[0]
