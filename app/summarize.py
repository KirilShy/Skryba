"""Post-transcription summary via the Claude API.

This is the one step that leaves your machine. Transcription and diarization
are fully local; only the finished text is sent, and only when you ask for it.
"""
from __future__ import annotations

import os

from pydantic import BaseModel, Field

from . import config, formats


class SummaryUnavailable(RuntimeError):
    pass


class ActionItem(BaseModel):
    owner: str | None = Field(description="Who owns this, or null if nobody was named")
    task: str = Field(description="What needs to be done, phrased as an imperative")
    due: str | None = Field(description="Deadline as stated in the meeting, or null")


class MeetingSummary(BaseModel):
    headline: str = Field(description="One sentence capturing what this meeting was about")
    summary: str = Field(description="Two to four paragraphs of prose covering what was discussed")
    key_points: list[str] = Field(description="The substantive points raised, most important first")
    decisions: list[str] = Field(description="Decisions actually reached. Empty list if none were.")
    action_items: list[ActionItem] = Field(description="Concrete follow-ups agreed in the meeting")
    open_questions: list[str] = Field(description="Questions raised but left unresolved")


SYSTEM = """You summarize meeting transcripts produced by automatic speech recognition.

The transcript is machine-generated, so expect misheard words, missing \
punctuation, and speaker labels that occasionally attach a sentence to the wrong \
person. Read through those errors rather than quoting them literally.

Rules:
- Report only what the transcript supports. Never invent a decision, an owner, \
or a deadline that was not discussed.
- If nothing was decided, return an empty decisions list. An honest empty list \
is far more useful than a plausible-sounding fabrication.
- When speaker labels are present, attribute action items to those speakers. \
When they are absent, set owner to null rather than guessing.
- Write in the language the meeting was conducted in.
- Be specific: prefer "agreed to move the launch to March 14" over "discussed timing"."""


def is_available() -> bool:
    """True when the SDK will find credentials.

    It resolves an API key, then an auth token, then an `ant auth login`
    profile on disk — checking only the env var would grey out the summary
    toggle for anyone signed in through the CLI.
    """
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    from pathlib import Path
    return (Path.home() / ".config" / "anthropic").is_dir()


def summarize(segments: list[dict], meta: dict) -> dict:
    """Return a MeetingSummary as a plain dict."""
    try:
        import anthropic
    except ImportError as exc:
        raise SummaryUnavailable("The anthropic package is not installed.") from exc

    transcript = formats.to_txt(segments).strip()
    if not transcript:
        raise SummaryUnavailable("The transcript is empty — nothing to summarize.")

    context = []
    if meta.get("title"):
        context.append(f"Recording: {meta['title']}")
    if meta.get("duration"):
        context.append(f"Duration: {formats.short_clock(meta['duration'])}")
    if meta.get("speakers"):
        context.append(f"Speakers detected: {meta['speakers']}")
    header = "\n".join(context)

    try:
        client = anthropic.Anthropic()
        response = client.messages.parse(
            model=config.CLAUDE_MODEL,
            max_tokens=16000,
            system=SYSTEM,
            messages=[{
                "role": "user",
                "content": f"{header}\n\nTranscript:\n\n{transcript}",
            }],
            output_format=MeetingSummary,
        )
    except anthropic.AuthenticationError as exc:
        raise SummaryUnavailable(
            "ANTHROPIC_API_KEY was rejected. Check the key in your environment."
        ) from exc
    except anthropic.RateLimitError as exc:
        raise SummaryUnavailable("Rate limited by the Claude API. Try again shortly.") from exc
    except anthropic.APIStatusError as exc:
        raise SummaryUnavailable(f"Claude API error ({exc.status_code}).") from exc
    except anthropic.APIConnectionError as exc:
        raise SummaryUnavailable("Could not reach the Claude API — check your connection.") from exc

    parsed = response.parsed_output
    if parsed is None:
        raise SummaryUnavailable("Claude returned no structured output.")
    return parsed.model_dump()
