"""Job queue, pipeline, and persistence.

Transcription is GPU-bound and MLX has one GPU to give, so jobs run through a
single worker thread. Queuing three files works fine — they just run in order
instead of thrashing.

Every state change is written to disk and pushed to any connected browser, so
a refresh (or a restart) never loses a finished transcript.
"""
from __future__ import annotations

import json
import queue
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import audio, config, diarize, formats, summarize, transcribe

# Pipeline stages, in order, with the share of the progress bar each one owns.
STAGE_WEIGHTS = {"prepare": 0.03, "transcribe": 0.62, "diarize": 0.30, "summarize": 0.05}


@dataclass
class Job:
    id: str
    filename: str
    source_path: str
    status: str = "queued"          # queued | running | done | error | canceled
    stage: str = "queued"           # prepare | transcribe | diarize | summarize | done
    progress: float = 0.0           # 0..1 across the whole pipeline
    message: str = ""
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    options: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    chunks: list = field(default_factory=list)   # [{"start","end"}] of the audio
    next_chunk: int = 0                          # first chunk not yet transcribed
    control: str = "run"                         # run | pause | cancel
    segments: list = field(default_factory=list)
    summary: dict | None = None

    def public(self, include_segments: bool = True) -> dict:
        data = asdict(self)
        data.pop("source_path", None)
        if not include_segments:
            data.pop("segments", None)
        return data


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._subscribers: dict[str, list[queue.Queue]] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue[str] = queue.Queue()
        self._worker = threading.Thread(target=self._run_worker, daemon=True)
        self._worker.start()
        self._load_from_disk()

    # ---------- persistence ----------

    def _path(self, job_id: str) -> Path:
        return config.JOB_DIR / f"{job_id}.json"

    def _persist(self, job: Job) -> None:
        tmp = self._path(job.id).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(job), ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._path(job.id))  # atomic: a crash mid-write can't corrupt a job

    def _load_from_disk(self) -> None:
        for path in sorted(config.JOB_DIR.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                job = Job(**data)
                # A job cut short mid-flight is resumable: its finished chunks
                # are already on disk, so park it as paused rather than failed.
                if job.status in ("running", "queued"):
                    if job.chunks and job.next_chunk > 0:
                        job.status = "paused"
                        job.control = "pause"
                        job.message = "Paused — the server restarted."
                    else:
                        job.status = "error"
                        job.error = "Interrupted before any audio was transcribed."
                        job.stage = "done"
                self._jobs[job.id] = job
            except Exception:
                continue

    # ---------- events ----------

    def subscribe(self, job_id: str) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subscribers.setdefault(job_id, []).append(q)
        return q

    def unsubscribe(self, job_id: str, q: queue.Queue) -> None:
        with self._lock:
            subs = self._subscribers.get(job_id, [])
            if q in subs:
                subs.remove(q)

    def _emit(self, job_id: str, event: dict) -> None:
        with self._lock:
            subs = list(self._subscribers.get(job_id, []))
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass  # a browser that stopped reading must not block the pipeline

    # ---------- public API ----------

    def create(self, filename: str, source_path: Path, options: dict) -> Job:
        job = Job(
            id=uuid.uuid4().hex[:12],
            filename=filename,
            source_path=str(source_path),
            options=options,
        )
        with self._lock:
            self._jobs[job.id] = job
        self._persist(job)
        self._queue.put(job.id)
        return job

    def persist(self, job: Job) -> None:
        """Public hook for callers that mutate a finished job (e.g. late summary)."""
        self._persist(job)

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[dict]:
        jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        return [j.public(include_segments=False) for j in jobs]

    def pause(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job.status not in ("running", "queued"):
            return False
        job.control = "pause"
        self._update(job, message="Finishing the current chunk…")
        return True

    def resume(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job.status != "paused":
            return False
        job.control = "run"
        self._update(job, status="queued", message="Resuming…")
        self._queue.put(job.id)
        return True

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job.status not in ("running", "queued", "paused"):
            return False
        job.control = "cancel"
        if job.status == "paused":
            # Nothing is executing, so retire it here rather than in the worker.
            self._update(job, status="canceled", stage="done",
                         message="Canceled", finished_at=time.time())
        else:
            self._update(job, message="Cancelling…")
        return True

    def retry(self, job_id: str) -> bool:
        """Re-queue a failed or canceled job, keeping any chunks it finished."""
        job = self._jobs.get(job_id)
        if not job or job.status not in ("error", "canceled"):
            return False
        if not Path(job.source_path).exists():
            return False
        job.control = "run"
        job.error = None
        job.finished_at = None
        if not job.chunks or job.next_chunk == 0:
            job.segments = []
            job.next_chunk = 0
            job.progress = 0.0
        self._update(job, status="queued", stage="queued", message="Queued…")
        self._queue.put(job.id)
        return True

    def delete(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if not job:
            return False
        self._path(job_id).unlink(missing_ok=True)
        for path in (Path(job.source_path), config.UPLOAD_DIR / f"{job_id}.wav"):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return True

    # ---------- worker ----------

    def _update(self, job: Job, *, persist: bool = True, **fields) -> None:
        for key, value in fields.items():
            setattr(job, key, value)
        if persist:
            self._persist(job)
        self._emit(job.id, {"type": "state", "job": job.public(include_segments=False)})

    def _stage_progress(self, job: Job, stage: str, fraction: float) -> None:
        """Map progress within a stage onto the overall bar.

        Weights are normalised over the stages this job actually runs, so a
        transcribe-only job still sweeps the full bar instead of stopping at
        65% and then jumping to done.
        """
        active = {n: w for n, w in STAGE_WEIGHTS.items() if self._stage_runs(job, n)}
        total = sum(active.values()) or 1.0
        base = 0.0
        for name, weight in active.items():
            if name == stage:
                break
            base += weight
        span = active.get(stage, 0.0)
        done = base + span * max(0.0, min(fraction, 1.0))
        job.progress = min(done / total, 0.999)
        # Progress ticks are frequent; don't hit the disk for every one.
        self._emit(job.id, {"type": "progress", "progress": job.progress, "stage": stage})

    def _stage_runs(self, job: Job, stage: str) -> bool:
        if stage == "diarize":
            return bool(job.options.get("diarize"))
        if stage == "summarize":
            return bool(job.options.get("summarize"))
        return True

    def _run_worker(self) -> None:
        while True:
            job_id = self._queue.get()
            job = self._jobs.get(job_id)
            if job is None or job.status == "canceled":
                continue
            try:
                self._process(job)
            except self._Paused:
                done = job.next_chunk
                total = len(job.chunks) or 1
                self._update(job, status="paused",
                             message=f"Paused after chunk {done} of {total}")
                continue
            except self._Canceled:
                self._update(job, status="canceled", stage="done",
                             message="Canceled", finished_at=time.time())
                continue
            except Exception as exc:
                traceback.print_exc()
                self._update(
                    job,
                    status="error",
                    stage="done",
                    error=str(exc) or exc.__class__.__name__,
                    finished_at=time.time(),
                )

    class _Paused(Exception):
        """Raised to unwind out of the pipeline when the user hits pause."""

    def _check_control(self, job: Job) -> None:
        if job.control == "pause":
            raise self._Paused()
        if job.control == "cancel":
            raise self._Canceled()

    class _Canceled(Exception):
        """Raised to unwind out of the pipeline when the user cancels."""

    def _process(self, job: Job) -> None:
        source = Path(job.source_path)
        resuming = job.next_chunk > 0

        self._update(job, status="running",
                     stage="transcribe" if resuming else "prepare",
                     started_at=job.started_at or time.time(),
                     message="Resuming…" if resuming else "Decoding audio…")

        wav_path = config.UPLOAD_DIR / f"{job.id}.wav"

        # Preparation is idempotent so a resumed job can rebuild what it needs.
        if not resuming or not job.chunks or not wav_path.exists():
            info = audio.probe(source)
            if not info["has_audio"]:
                raise audio.AudioError(f"{job.filename} contains no audio track.")
            duration = info["duration"]
            job.meta.setdefault("title", Path(job.filename).stem)
            job.meta["duration"] = duration
            job.meta.setdefault(
                "recorded_at",
                time.strftime("%Y-%m-%d %H:%M", time.localtime(source.stat().st_mtime)),
            )
            if not wav_path.exists():
                audio.to_wav16k(source, wav_path)
            if not job.chunks:
                job.chunks = audio.plan_chunks(wav_path, duration)
            if "silences" not in job.meta:
                # Cached so a resumed job does not re-scan the whole file.
                job.meta["silences"] = [
                    list(r) for r in audio.silence_regions(wav_path)
                ]
            self._stage_progress(job, "prepare", 1.0)

        duration = job.meta.get("duration") or 0.0
        self._check_control(job)

        # ---- transcribe, one resumable chunk at a time ----
        self._update(job, stage="transcribe", message="Transcribing on the GPU…")
        samples = transcribe.load_audio(wav_path)

        # Resolve the language ONCE, from the loudest parts of the recording.
        # Detecting per chunk lets a silent stretch relabel the meeting; taking
        # it from chunk one is worse still, since recordings often open quiet.
        pinned = (job.options.get("language") or "").strip()
        language = pinned or job.meta.get("language")
        if not language:
            self._update(job, message="Detecting language…")
            language = transcribe.detect_language(
                samples, job.options.get("model", config.DEFAULT_WHISPER)
            )
            job.meta["language"] = language
            job.meta["language_source"] = "detected"
        elif pinned:
            job.meta["language"] = pinned
            job.meta["language_source"] = "pinned"

        silences = [tuple(r) for r in job.meta.get("silences", [])]

        for index in range(job.next_chunk, len(job.chunks)):
            self._check_control(job)
            chunk = job.chunks[index]

            # Whisper hallucinates stock phrases ("Thank you.", "Dziękuję.")
            # over silence. Skipping near-silent windows removes that noise and
            # costs nothing, since there is no speech there to lose.
            if audio.speech_fraction(chunk["start"], chunk["end"], silences) < 0.05:
                job.next_chunk = index + 1
                self._persist(job)
                self._stage_progress(job, "transcribe", chunk["end"] / max(duration, 1e-6))
                continue

            a = int(chunk["start"] * transcribe.SAMPLE_RATE)
            b = int(chunk["end"] * transcribe.SAMPLE_RATE)

            def on_segment(segment: dict, fraction: float, _c=chunk) -> None:
                done = _c["start"] + (_c["end"] - _c["start"]) * fraction
                self._stage_progress(job, "transcribe", min(done / max(duration, 1e-6), 1.0))
                self._emit(job.id, {"type": "segment", "segment": {
                    "start": segment["start"] + _c["start"],
                    "end": segment["end"] + _c["start"],
                    "text": segment["text"],
                }})

            result = transcribe.transcribe_window(
                samples[a:b],
                offset=chunk["start"],
                model_key=job.options.get("model", config.DEFAULT_WHISPER),
                language=language,
                on_segment=on_segment,
            )
            # Whisper repeats a stock phrase over quiet stretches ("Cześć!
            # Cześć! Cześć!"). Genuine speech almost never repeats the same
            # short line back to back, so collapsing consecutive duplicates
            # removes the artifact without touching distinct content. Done here
            # rather than per window so it also spans chunk boundaries.
            for seg in result["segments"]:
                previous = job.segments[-1] if job.segments else None
                if previous and previous["text"].strip().casefold() == seg["text"].strip().casefold():
                    previous["end"] = seg["end"]
                    continue
                job.segments.append(seg)
            job.next_chunk = index + 1
            # Persist per chunk — this is what makes pause and restart survivable.
            self._persist(job)
            self._stage_progress(job, "transcribe", chunk["end"] / max(duration, 1e-6))

        self._stage_progress(job, "transcribe", 1.0)
        if not job.segments:
            raise RuntimeError("Whisper found no speech in this recording.")

        # ---- diarize (optional, non-fatal) ----
        if job.options.get("diarize"):
            self._check_control(job)
            self._update(job, stage="diarize", message="Identifying speakers…")
            try:
                turns = diarize.diarize(
                    wav_path,
                    num_speakers=job.options.get("num_speakers"),
                    on_progress=lambda f: self._stage_progress(job, "diarize", f),
                )
                diarize.assign_speakers(job.segments, turns)
                names = diarize.prettify_labels(job.segments)
                job.meta["speakers"] = len(names)
            except diarize.DiarizationUnavailable as exc:
                job.meta["diarization_error"] = str(exc)
            except Exception as exc:
                job.meta["diarization_error"] = f"Diarization failed: {exc}"
            self._stage_progress(job, "diarize", 1.0)

        # ---- summarize (optional, non-fatal) ----
        if job.options.get("summarize"):
            self._check_control(job)
            self._update(job, stage="summarize", message="Summarizing…")
            try:
                job.summary = summarize.summarize(job.segments, job.meta)
            except summarize.SummaryUnavailable as exc:
                job.meta["summary_error"] = str(exc)
            except Exception as exc:
                job.meta["summary_error"] = f"Summarization failed: {exc}"
            self._stage_progress(job, "summarize", 1.0)

        wav_path.unlink(missing_ok=True)  # derived file; the original upload stays
        self._update(job, status="done", stage="done", progress=1.0,
                     message="Done", finished_at=time.time())
        self._emit(job.id, {"type": "done", "job": job.public()})

    # ---------- exports ----------

    def render(self, job: Job, fmt: str) -> tuple[str, str, str]:
        """Return (text, media_type, filename) for a download."""
        stem = Path(job.filename).stem
        if fmt == "srt":
            return formats.to_srt(job.segments), "text/plain; charset=utf-8", f"{stem}.srt"
        if fmt == "vtt":
            return formats.to_vtt(job.segments), "text/vtt; charset=utf-8", f"{stem}.vtt"
        if fmt == "txt":
            return formats.to_txt(job.segments), "text/plain; charset=utf-8", f"{stem}.txt"
        if fmt == "json":
            payload = {"meta": job.meta, "segments": job.segments, "summary": job.summary}
            return (json.dumps(payload, ensure_ascii=False, indent=2),
                    "application/json; charset=utf-8", f"{stem}.json")
        return (formats.to_markdown(job.segments, job.meta, job.summary),
                "text/markdown; charset=utf-8", f"{stem}.md")


store = JobStore()
