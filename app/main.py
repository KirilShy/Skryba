"""FastAPI app: upload, watch progress over SSE, read and export transcripts."""
from __future__ import annotations

import asyncio
import html
import json
import queue
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config, diarize, formats, summarize, transcribe
from .jobs import store

app = FastAPI(title="Skryba", docs_url=None, redoc_url=None)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_SAFE_NAME = re.compile(r"[^\w\s.\-()\[\]]", re.UNICODE)


def _safe_filename(name: str) -> str:
    """Keep the user's name readable but strip anything path-like."""
    name = Path(name or "recording").name
    name = _SAFE_NAME.sub("_", name).strip() or "recording"
    return name[:180]


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/api/capabilities")
async def capabilities() -> dict:
    return {
        "models": list(config.WHISPER_MODELS.keys()),
        "default_model": config.DEFAULT_WHISPER,
        "diarization": diarize.is_available(),
        "summarization": summarize.is_available(),
        "summary_provider": summarize.provider_label(),
        "transcribe_backend": transcribe.BACKEND,
    }


@app.post("/api/jobs")
async def create_job(
    file: UploadFile = File(...),
    model: str = Form(config.DEFAULT_WHISPER),
    language: str = Form(""),
    diarize_flag: bool = Form(False, alias="diarize"),
    summarize_flag: bool = Form(False, alias="summarize"),
    num_speakers: str = Form(""),
) -> dict:
    filename = _safe_filename(file.filename or "recording")
    suffix = Path(filename).suffix.lower()
    if suffix not in config.ALLOWED_SUFFIXES:
        raise HTTPException(
            400,
            f"'{suffix or 'no extension'}' isn't a supported format. "
            f"Supported: {', '.join(sorted(config.ALLOWED_SUFFIXES))}",
        )

    dest = config.UPLOAD_DIR / f"{int(time.time() * 1000)}-{filename}"

    def _save() -> None:
        with dest.open("wb") as out:
            shutil.copyfileobj(file.file, out, length=1024 * 1024)

    try:
        # A multi-hundred-MB recording would otherwise stall every open SSE
        # stream for the duration of the copy.
        await asyncio.to_thread(_save)
    finally:
        await file.close()

    if dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "The uploaded file is empty.")

    speakers = None
    if num_speakers.strip().isdigit():
        speakers = max(1, min(int(num_speakers), 20))

    job = store.create(filename, dest, {
        "model": model if model in config.WHISPER_MODELS else config.DEFAULT_WHISPER,
        "language": language.strip(),
        "diarize": bool(diarize_flag),
        "summarize": bool(summarize_flag),
        "num_speakers": speakers,
    })
    return job.public(include_segments=False)


@app.get("/api/jobs")
async def list_jobs() -> list[dict]:
    return store.list()


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "No such job.")
    return job.public()


@app.post("/api/jobs/{job_id}/pause")
async def pause_job(job_id: str) -> dict:
    """Stop after the current chunk. Finished chunks are already persisted."""
    if not store.get(job_id):
        raise HTTPException(404, "No such job.")
    if not store.pause(job_id):
        raise HTTPException(400, "This job is not running.")
    return {"paused": job_id}


@app.post("/api/jobs/{job_id}/resume")
async def resume_job(job_id: str) -> dict:
    if not store.get(job_id):
        raise HTTPException(404, "No such job.")
    if not store.resume(job_id):
        raise HTTPException(400, "This job is not paused.")
    return {"resumed": job_id}


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: str) -> dict:
    if not store.get(job_id):
        raise HTTPException(404, "No such job.")
    if not store.retry(job_id):
        raise HTTPException(400, "This job cannot be retried — check the source file still exists.")
    return {"retrying": job_id}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict:
    if not store.get(job_id):
        raise HTTPException(404, "No such job.")
    if not store.cancel(job_id):
        raise HTTPException(400, "This job cannot be canceled.")
    return {"canceled": job_id}


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str) -> dict:
    if not store.delete(job_id):
        raise HTTPException(404, "No such job.")
    return {"deleted": job_id}


@app.post("/api/jobs/{job_id}/summarize")
async def summarize_job(job_id: str) -> dict:
    """Summarize a transcript that was produced without the summary step."""
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "No such job.")
    if job.status != "done" or not job.segments:
        raise HTTPException(400, "This job has no finished transcript yet.")
    try:
        job.summary = await asyncio.to_thread(summarize.summarize, job.segments, job.meta)
    except summarize.SummaryUnavailable as exc:
        raise HTTPException(400, str(exc)) from exc
    job.meta.pop("summary_error", None)
    store.persist(job)
    return {"summary": job.summary}


@app.get("/api/jobs/{job_id}/audio")
async def job_audio(job_id: str) -> FileResponse:
    """Serve the original upload so the player can seek against timestamps."""
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "No such job.")
    path = Path(job.source_path)
    if not path.exists():
        raise HTTPException(404, "The source audio has been deleted.")
    return FileResponse(path, filename=job.filename)


@app.get("/api/jobs/{job_id}/download/{fmt}")
async def download(job_id: str, fmt: str) -> Response:
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "No such job.")
    if not job.segments:
        raise HTTPException(400, "This job has no transcript yet.")
    if fmt not in {"md", "txt", "srt", "vtt", "json"}:
        raise HTTPException(400, f"Unknown format '{fmt}'.")
    body, media_type, filename = store.render(job, fmt)
    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/jobs/{job_id}/events")
async def events(job_id: str) -> StreamingResponse:
    """Server-sent events: progress, live segments, completion."""
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "No such job.")

    async def stream():
        q = store.subscribe(job_id)
        loop = asyncio.get_running_loop()
        try:
            current = store.get(job_id)
            if current:
                yield _sse({"type": "state", "job": current.public(include_segments=False)})
                if current.status in ("done", "error"):
                    yield _sse({"type": "done", "job": current.public()})
                    return
            while True:
                try:
                    event = await loop.run_in_executor(None, q.get, True, 15.0)
                except queue.Empty:
                    yield ": keep-alive\n\n"  # keeps proxies and browsers from hanging up
                    continue
                yield _sse(event)
                if event.get("type") in ("done", "error"):
                    return
        finally:
            store.unsubscribe(job_id, q)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.get("/read/{job_id}", response_class=HTMLResponse)
async def read_page(job_id: str) -> HTMLResponse:
    """A clean, printable reading view — no sidebar, no controls, just the text."""
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "No such job.")
    if not job.segments:
        raise HTTPException(400, "This job has no transcript yet.")
    return HTMLResponse(_render_reader(job))


@app.get("/api/jobs/{job_id}/view/{fmt}")
async def view(job_id: str, fmt: str) -> Response:
    """Same content as /download but shown in the browser instead of saved."""
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "No such job.")
    if not job.segments:
        raise HTTPException(400, "This job has no transcript yet.")
    if fmt not in {"md", "txt", "srt", "vtt", "json"}:
        raise HTTPException(400, f"Unknown format '{fmt}'.")
    body, media_type, _ = store.render(job, fmt)
    # text/plain so the browser renders it inline rather than offering a save
    inline_type = "application/json; charset=utf-8" if fmt == "json" else "text/plain; charset=utf-8"
    return Response(content=body, media_type=inline_type,
                    headers={"Content-Disposition": "inline"})


@app.post("/api/jobs/{job_id}/save")
async def save_to_folder(job_id: str) -> dict:
    """Write every format to the export folder and reveal it in Finder."""
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "No such job.")
    if not job.segments:
        raise HTTPException(400, "This job has no transcript yet.")

    stem = Path(job.filename).stem
    written = []
    for fmt in ("md", "txt", "srt", "vtt", "json"):
        body, _, filename = store.render(job, fmt)
        path = config.EXPORT_DIR / filename
        path.write_text(body, encoding="utf-8")
        written.append(str(path))

    revealed = False
    target = config.EXPORT_DIR / f"{stem}.txt"
    # explorer.exe requires "/select," glued to the path with no space, unlike
    # every other Windows CLI flag — a separate argv item breaks the parse.
    reveal_cmd = {
        "darwin": ["open", "-R", str(target)],
        "win32": ["explorer", f"/select,{target}"],
    }.get(sys.platform)
    if reveal_cmd:
        try:
            # Local-only app: opening Finder/Explorer for the user is the whole point.
            subprocess.run(reveal_cmd, timeout=5, check=False)
            revealed = True
        except (OSError, subprocess.SubprocessError):
            revealed = False

    return {"folder": str(config.EXPORT_DIR), "files": written, "revealed": revealed}


def _render_reader(job) -> str:
    """Standalone HTML for the reading view. Shares the app's theme tokens."""
    turns = formats.group_by_turns(job.segments)
    speakers: list[str] = []
    for t in turns:
        if t["speaker"] and t["speaker"] not in speakers:
            speakers.append(t["speaker"])

    rows = []
    for t in turns:
        stamp = formats.short_clock(t["start"])
        who = ""
        if t["speaker"]:
            idx = speakers.index(t["speaker"]) % 6
            who = f'<div class="who sp{idx}">{html.escape(t["speaker"])}</div>'
        rows.append(
            f'<section><a class="stamp" href="#t{int(t["start"])}" id="t{int(t["start"])}">'
            f'{stamp}</a><div class="body">{who}<p>{html.escape(t["text"])}</p></div></section>'
        )

    meta_bits = []
    if job.meta.get("duration"):
        meta_bits.append(formats.short_clock(job.meta["duration"]))
    if job.meta.get("language"):
        meta_bits.append(str(job.meta["language"]).upper())
    if speakers:
        meta_bits.append(f"{len(speakers)} speakers")

    summary_html = ""
    if job.summary:
        s = job.summary
        parts = []
        if s.get("headline"):
            parts.append(f'<p class="headline">{html.escape(s["headline"])}</p>')
        if s.get("summary"):
            for para in str(s["summary"]).split("\n\n"):
                parts.append(f"<p>{html.escape(para)}</p>")
        if parts:
            summary_html = f'<div class="summary"><h2>Summary</h2>{"".join(parts)}</div>'

    title = html.escape(job.meta.get("title") or job.filename)
    return f"""<!doctype html>
<html lang="{html.escape(str(job.meta.get('language') or 'en'))}">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<link rel="icon" href="/static/brand/favicon.ico" sizes="any">
<link rel="icon" type="image/png" sizes="32x32" href="/static/brand/icon-32.png">
<link rel="apple-touch-icon" sizes="180x180" href="/static/brand/icon-180.png">
<style>
:root {{ --bg:#fbfbfc; --fg:#1c1c1f; --dim:#6b6b76; --accent:#4f46e5; --rule:#e3e3e7; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#131316; --fg:#ececf1; --dim:#9a9aa5; --accent:#8b85f5; --rule:#2e2e35; }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg);
  font:17px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI",Georgia,serif;
  -webkit-font-smoothing:antialiased; }}
.wrap {{ max-width:720px; margin:0 auto; padding:56px 24px 96px; }}
.mark {{ width:30px; height:30px; display:block; margin-bottom:14px; }}
@media print {{ .mark {{ display:none; }} }}
h1 {{ font-size:28px; line-height:1.25; margin:0 0 6px; letter-spacing:-.02em; }}
.meta {{ color:var(--dim); font-size:14px; margin-bottom:8px; }}
.actions {{ margin:20px 0 40px; padding-bottom:28px; border-bottom:1px solid var(--rule); }}
.actions a {{ font-size:13px; color:var(--accent); text-decoration:none;
  border:1px solid var(--rule); border-radius:8px; padding:6px 11px; margin-right:6px;
  display:inline-block; }}
.actions a:hover {{ border-color:var(--accent); }}
.summary {{ margin-bottom:40px; padding-bottom:28px; border-bottom:1px solid var(--rule); }}
.summary h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.06em;
  color:var(--dim); margin:0 0 12px; }}
.headline {{ font-size:19px; font-weight:600; }}
section {{ display:flex; gap:18px; margin-bottom:26px; }}
.stamp {{ flex:none; width:52px; text-align:right; font-size:13px; color:var(--dim);
  text-decoration:none; font-variant-numeric:tabular-nums; padding-top:4px;
  font-family:-apple-system,system-ui,sans-serif; scroll-margin-top:20px; }}
.stamp:hover {{ color:var(--accent); }}
.body {{ flex:1; min-width:0; }}
.body p {{ margin:0; }}
.who {{ font-size:13px; font-weight:700; margin-bottom:3px;
  font-family:-apple-system,system-ui,sans-serif; }}
.sp0{{color:#6366f1}} .sp1{{color:#0891b2}} .sp2{{color:#c2410c}}
.sp3{{color:#7c3aed}} .sp4{{color:#15803d}} .sp5{{color:#be185d}}
@media print {{
  body {{ background:#fff; color:#000; }}
  .actions {{ display:none; }}
  section {{ page-break-inside:avoid; }}
}}
</style></head>
<body><div class="wrap">
<img class="mark" src="/static/brand/mark-128.png" width="30" height="30" alt="">
<h1>{title}</h1>
<div class="meta">{html.escape(" · ".join(meta_bits))}</div>
<div class="actions">
  <a href="/api/jobs/{job.id}/view/txt" target="_blank">Open .txt</a>
  <a href="/api/jobs/{job.id}/view/md" target="_blank">Open .md</a>
  <a href="/api/jobs/{job.id}/download/md">Download</a>
  <a href="/">&larr; Back to app</a>
</div>
{summary_html}
{"".join(rows)}
</div></body></html>"""
