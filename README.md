# Skryba

Local meeting transcription. Drop in a recording, get a searchable transcript
with speaker labels — without your audio ever leaving the machine.

Built because uploading client meetings to a cloud transcription service was not
an option.

Runs on Apple Silicon (MLX, on the GPU) and on Windows/Linux (faster-whisper,
CUDA if an NVIDIA GPU is present, otherwise CPU) — see [Backends](#backends).

```
┌──────────────────────────────────────┐
│  ⬆ Drop audio here                   │
├──────────────────────────────────────┤
│  standup.m4a       ████████░░  81%   │
├──────────────────────────────────────┤
│  ▶ ──────●──────────────  12:04      │
│                                      │
│  Speaker 1 · 00:12                   │
│  Okay, so the main thing this week…  │
│                                      │
│  Speaker 2 · 00:31                   │
│  Right, and the deadline moved to…   │
└──────────────────────────────────────┘
```

## Why

Whisper on Apple Silicon is usually run through `faster-whisper`, which has no
Metal backend — it pins your CPU cores and ignores the GPU entirely. Skryba uses
[MLX](https://github.com/ml-explore/mlx) instead, on the Mac. Elsewhere — Windows,
Linux — `faster-whisper` is exactly the right tool, and runs on an NVIDIA GPU via
CUDA when one is present. See [Backends](#backends).

Measured on an M2 (16 GB), transcribing a 35-minute meeting:

| Approach | Time | Speed |
| --- | --- | --- |
| `faster-whisper`, CPU int8 | ~80 min (projected) | 0.4× realtime |
| **Skryba** (MLX, `large-v3-turbo`) | **2 min 41 s** | **13× realtime** |
| Skryba (MLX, `large-v3`) | 6 min 56 s | 5.1× realtime |

## Features

- **Fast** — the GPU does the work: MLX on Apple Silicon, CUDA via
  `faster-whisper` on Windows/Linux
- **Private** — audio is transcribed locally; nothing is uploaded
- **Speaker labels** — optional diarization via [pyannote](https://github.com/pyannote/pyannote-audio)
- **AI summaries** — optional decisions and action items via the Claude API
  (the only step that sends data anywhere, and only when you ask)
- **Live progress** — text streams into the browser as it decodes
- **Exports** — Markdown, plain text, SRT, VTT, JSON
- **Crash-safe** — every transcript is written to disk as it is produced

## Backends

Skryba picks a transcription backend automatically, at import time, based on
platform (`app/transcribe.py`):

| Platform | Backend | Device |
| --- | --- | --- |
| macOS (Apple Silicon) | `mlx_whisper` | Metal GPU |
| Windows / Linux | `faster-whisper` | CUDA if an NVIDIA GPU is visible, else CPU (int8) |

Both backends implement the same four functions (`load_audio`,
`transcribe_window`, `detect_language`, `SAMPLE_RATE`), so the job pipeline
(`app/jobs.py`) never branches on platform. Force one explicitly with
`TRANSCRIBE_BACKEND=mlx` or `TRANSCRIBE_BACKEND=faster-whisper` in `.env` —
useful to test `faster-whisper` on a Mac, or to force CPU-only decoding.

Model names differ per backend for the same `--model` choice (`turbo` /
`large` / `medium` / `small`) — see `WHISPER_MODELS` and
`WHISPER_MODELS_FASTER` in `app/config.py`. Both download their own converted
weights from Hugging Face on first use.

## Requirements

- **macOS**: Apple Silicon (M1 or newer)
- **Windows / Linux**: any 64-bit machine; an NVIDIA GPU is optional but
  strongly recommended — `faster-whisper` on CPU is roughly 10-20× slower
- Python 3.13
- `ffmpeg` — `brew install ffmpeg` (macOS), `winget install Gyan.FFmpeg`
  (Windows), or your distro's package manager (Linux)

## Install

```bash
git clone https://github.com/KirilShy/Skryba.git
cd Skryba
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e .
```

On Windows, use the venv's `Scripts` path instead of `bin`:

```powershell
uv venv --python 3.13 .venv
uv pip install --python .venv\Scripts\python.exe -e .
```

`pyproject.toml` resolves `mlx-whisper` on macOS and `faster-whisper`
everywhere else automatically — nothing to choose by hand.

Speaker labels need extra dependencies (~2.5 GB, mostly PyTorch):

```bash
uv pip install --python .venv/bin/python -e . --extra diarize
```

## Run

```bash
./run.sh          # macOS / Linux
.\run.ps1         # Windows
```

Opens <http://127.0.0.1:8420>. Drag a recording onto the sidebar. Text appears
live as it decodes; clicking any timestamp seeks the audio player.

The first run downloads Whisper weights from Hugging Face (~1.6 GB for
`turbo`) into `~/.cache/huggingface` (`%USERPROFILE%\.cache\huggingface` on
Windows). Later runs are offline.

## Optional features

Copy `.env.example` to `.env` and fill in what you want. Toggles for features
you have not configured appear greyed out with the reason, so nothing fails
silently.

| Feature | Variable | Where to get it |
| --- | --- | --- |
| AI summary (OpenRouter) | `OPENROUTER_API_KEY` | [openrouter.ai/keys](https://openrouter.ai/keys) |
| AI summary (Anthropic) | `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) |
| Speaker labels | `HF_TOKEN` | [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) |

Either summary backend works; OpenRouter takes precedence when both keys are
present, and `SUMMARY_PROVIDER=anthropic` forces the other way. Pick the
OpenRouter model with `OPENROUTER_MODEL` (default `anthropic/claude-sonnet-5`)
— any model with structured-output support will do. A 35-minute meeting costs
roughly four cents to summarize.

For speaker labels you must also accept the model terms while signed in, on
**both** [`pyannote/speaker-diarization-3.1`](https://huggingface.co/pyannote/speaker-diarization-3.1)
and [`pyannote/segmentation-3.0`](https://huggingface.co/pyannote/segmentation-3.0).
A valid token without both acceptances returns an empty pipeline — Skryba
detects that specific case and tells you, rather than failing obscurely.

## Choosing a model

| Model | Speed on M2 (MLX) | When to use |
| --- | --- | --- |
| `turbo` | ~13× realtime | Default. Best accuracy per second. |
| `large` | ~5× realtime | Hard audio: heavy accents, crosstalk, poor mics. |
| `medium` / `small` | faster still | Quick drafts, or clean single-speaker audio. |

Relative speeds are similar on `faster-whisper`/CUDA; absolute numbers depend
on the GPU. On CPU, expect roughly 0.3-0.5× realtime for `turbo`.

Setting the language explicitly (`en`, `pl`, `uk`) is both faster and more
accurate than auto-detect when you already know it.

Transcript quality tracks microphone quality far more than model size. A table
mic in a reverberant room hurts more than dropping from `large` to `turbo`.

### Getting better transcripts

Audio is normalised before Whisper sees it — a high-pass to drop rumble, then
dynamic normalisation and loudness levelling. On one test recording a passage
measured 0.004 RMS; the old pipeline returned a single hallucinated line for
those 30 seconds, while the processed audio produced six segments of real
speech. This is on by default and is the largest single quality lever.

Two more worth setting in `.env`:

- `INITIAL_PROMPT` — seed recurring names and jargon so they are recognised
  rather than guessed at phonetically.
- Pin the language rather than relying on auto-detect. Detection samples the
  audio, and on a quiet or silence-heavy recording it can land on the wrong
  answer; one 73-minute Polish meeting was detected as English.

Note that `mlx-whisper` has no beam search decoder, so decoding is greedy with
Whisper's temperature-fallback schedule (`faster-whisper` does support beam
search, but Skryba keeps both backends on the same greedy settings so a
transcript doesn't change character depending on which machine ran it). Word
timestamps are enabled on both backends to drive Whisper's own hallucination
suppression, which costs some throughput.

## Layout

```
app/
  main.py          FastAPI routes, SSE progress stream, reader view
  jobs.py          job queue, pipeline orchestration, persistence
  transcribe.py    picks a backend by platform, re-exports its interface
  transcribe_mlx.py   MLX Whisper wrapper (Apple Silicon GPU)
  transcribe_fw.py    faster-whisper wrapper (CUDA / CPU)
  diarize.py       pyannote wrapper + speaker/segment alignment
  summarize.py     Claude structured-output summary
  formats.py       Markdown / TXT / SRT / VTT rendering
  audio.py         ffmpeg + ffprobe helpers
  static/          the single-page UI
data/
  uploads/         your recordings (git-ignored)
  jobs/            one JSON per job (git-ignored)
```

Jobs run one at a time — there is a single GPU, so queuing beats thrashing.

## Design notes

A few decisions that are not obvious from the code:

- **Progress comes from parsing Whisper's own console output — on MLX only.**
  `mlx_whisper` exposes no progress hook, but prints one line per decoded
  segment. Skryba captures that stream for the progress bar and live preview,
  while the authoritative segments still come from the return value — so a
  format change upstream costs you the preview, never the transcript.
  `faster-whisper` needs none of this: its `transcribe()` already returns a
  segment generator, so progress is driven straight off that.
- **Diarization and summarization are non-fatal.** If either fails you still get
  the transcript, and the UI explains what went wrong.
- **Turns are cut on a hard length cap, not only on pauses.** Whisper emits
  near-contiguous segments, so a pause-only rule produces two-minute walls of
  text with a single timestamp.

## Licence

MIT
