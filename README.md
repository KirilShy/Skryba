# Skryba

Local meeting transcription for Apple Silicon. Drop in a recording, get a
searchable transcript with speaker labels — without your audio ever leaving the
machine.

Built because uploading client meetings to a cloud transcription service was not
an option.

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
[MLX](https://github.com/ml-explore/mlx) instead.

Measured on an M2 (16 GB), transcribing a 35-minute meeting:

| Approach | Time | Speed |
| --- | --- | --- |
| `faster-whisper`, CPU int8 | ~80 min (projected) | 0.4× realtime |
| **Skryba** (MLX, `large-v3-turbo`) | **2 min 41 s** | **13× realtime** |
| Skryba (MLX, `large-v3`) | 6 min 56 s | 5.1× realtime |

## Features

- **Fast** — Whisper on the GPU via MLX, ~13× realtime on an M2
- **Private** — audio is transcribed locally; nothing is uploaded
- **Speaker labels** — optional diarization via [pyannote](https://github.com/pyannote/pyannote-audio)
- **AI summaries** — optional decisions and action items via the Claude API
  (the only step that sends data anywhere, and only when you ask)
- **Live progress** — text streams into the browser as it decodes
- **Exports** — Markdown, plain text, SRT, VTT, JSON
- **Crash-safe** — every transcript is written to disk as it is produced

## Requirements

- Apple Silicon Mac (M1 or newer)
- Python 3.13
- `ffmpeg` — `brew install ffmpeg`

## Install

```bash
git clone https://github.com/KirilShy/Skryba.git
cd Skryba
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r pyproject.toml
```

Speaker labels need extra dependencies (~2.5 GB, mostly PyTorch):

```bash
uv pip install --python .venv/bin/python -r pyproject.toml --extra diarize
```

## Run

```bash
./run.sh
```

Opens <http://127.0.0.1:8420>. Drag a recording onto the sidebar. Text appears
live as it decodes; clicking any timestamp seeks the audio player.

The first run downloads Whisper weights from Hugging Face (~1.6 GB for
`turbo`) into `~/.cache/huggingface`. Later runs are offline.

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

| Model | Speed on M2 | When to use |
| --- | --- | --- |
| `turbo` | ~13× realtime | Default. Best accuracy per second. |
| `large` | ~5× realtime | Hard audio: heavy accents, crosstalk, poor mics. |
| `medium` / `small` | faster still | Quick drafts, or clean single-speaker audio. |

Setting the language explicitly (`en`, `pl`, `uk`) is both faster and more
accurate than auto-detect when you already know it.

Transcript quality tracks microphone quality far more than model size. A table
mic in a reverberant room hurts more than dropping from `large` to `turbo`.

## Layout

```
app/
  main.py        FastAPI routes, SSE progress stream, reader view
  jobs.py        job queue, pipeline orchestration, persistence
  transcribe.py  MLX Whisper wrapper with live progress
  diarize.py     pyannote wrapper + speaker/segment alignment
  summarize.py   Claude structured-output summary
  formats.py     Markdown / TXT / SRT / VTT rendering
  audio.py       ffmpeg + ffprobe helpers
  static/        the single-page UI
data/
  uploads/       your recordings (git-ignored)
  jobs/          one JSON per job (git-ignored)
```

Jobs run one at a time — there is a single GPU, so queuing beats thrashing.

## Design notes

A few decisions that are not obvious from the code:

- **Progress comes from parsing Whisper's own console output.** `mlx_whisper`
  exposes no progress hook, but prints one line per decoded segment. Skryba
  captures that stream for the progress bar and live preview, while the
  authoritative segments still come from the return value — so a format change
  upstream costs you the preview, never the transcript.
- **Diarization and summarization are non-fatal.** If either fails you still get
  the transcript, and the UI explains what went wrong.
- **Turns are cut on a hard length cap, not only on pauses.** Whisper emits
  near-contiguous segments, so a pause-only rule produces two-minute walls of
  text with a single timestamp.

## Licence

MIT
