"""Post-transcription summary via the Claude API.

This is the one step that leaves your machine. Transcription and diarization
are fully local; only the finished text is sent, and only when you ask for it.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

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


def _strictify(schema: dict) -> dict:
    """Make a Pydantic JSON schema acceptable to strict structured outputs.

    Strict mode requires every object to forbid extra keys and to list all of
    its properties as required. Pydantic marks optional fields as not-required,
    which strict mode rejects, so we require everything and let nullable types
    express optionality.
    """
    if isinstance(schema, dict):
        if schema.get("type") == "object" and "properties" in schema:
            schema["additionalProperties"] = False
            schema["required"] = list(schema["properties"].keys())
        for value in schema.values():
            _strictify(value)
    elif isinstance(schema, list):
        for item in schema:
            _strictify(item)
    return schema


def active_provider() -> str | None:
    """Which backend will actually be used, or None when none is configured."""
    forced = config.SUMMARY_PROVIDER
    has_openrouter = bool(config.OPENROUTER_API_KEY)
    has_anthropic = bool(
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or (Path.home() / ".config" / "anthropic").is_dir()
    )
    if forced == "openrouter":
        return "openrouter" if has_openrouter else None
    if forced == "anthropic":
        return "anthropic" if has_anthropic else None
    if has_openrouter:
        return "openrouter"
    if has_anthropic:
        return "anthropic"
    return None


def is_available() -> bool:
    return active_provider() is not None


def provider_label() -> str:
    """Human-readable description of the configured backend, for the UI."""
    provider = active_provider()
    if provider == "openrouter":
        return f"OpenRouter · {config.OPENROUTER_MODEL}"
    if provider == "anthropic":
        return f"Anthropic · {config.CLAUDE_MODEL}"
    return "not configured"


def _build_prompt(segments: list[dict], meta: dict) -> str:
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
    return f"{chr(10).join(context)}\n\nTranscript:\n\n{transcript}"


def summarize(segments: list[dict], meta: dict) -> dict:
    """Return a MeetingSummary as a plain dict, using whichever backend is set."""
    provider = active_provider()
    prompt = _build_prompt(segments, meta)
    if provider == "openrouter":
        return _summarize_openrouter(prompt)
    if provider == "anthropic":
        return _summarize_anthropic(prompt)
    raise SummaryUnavailable(
        "No summary provider configured. Set OPENROUTER_API_KEY or "
        "ANTHROPIC_API_KEY in your .env file."
    )


def _summarize_anthropic(prompt: str) -> dict:
    try:
        import anthropic
    except ImportError as exc:
        raise SummaryUnavailable("The anthropic package is not installed.") from exc

    try:
        client = anthropic.Anthropic()
        response = client.messages.parse(
            model=config.CLAUDE_MODEL,
            max_tokens=16000,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
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


def _summarize_openrouter(prompt: str) -> dict:
    """OpenRouter speaks the OpenAI chat-completions dialect, not Anthropic's."""
    try:
        import openai
    except ImportError as exc:
        raise SummaryUnavailable(
            "The openai package is needed for OpenRouter. Install it with:\n"
            "  uv pip install --python .venv/bin/python openai"
        ) from exc

    schema = _strictify(MeetingSummary.model_json_schema())
    client = openai.OpenAI(
        api_key=config.OPENROUTER_API_KEY,
        base_url=config.OPENROUTER_BASE_URL,
    )
    try:
        response = client.chat.completions.create(
            model=config.OPENROUTER_MODEL,
            max_tokens=8000,
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "meeting_summary", "strict": True, "schema": schema},
            },
            extra_headers={
                # OpenRouter shows these on its activity page; harmless if unset.
                "HTTP-Referer": "https://github.com/KirilShy/Skryba",
                "X-Title": "Skryba",
            },
        )
    except openai.AuthenticationError as exc:
        raise SummaryUnavailable("OPENROUTER_API_KEY was rejected. Check the key.") from exc
    except openai.RateLimitError as exc:
        raise SummaryUnavailable("Rate limited by OpenRouter. Try again shortly.") from exc
    except openai.APIStatusError as exc:
        detail = ""
        try:
            detail = f" — {exc.response.json().get('error', {}).get('message', '')}"
        except Exception:
            pass
        raise SummaryUnavailable(f"OpenRouter error ({exc.status_code}){detail}") from exc
    except openai.APIConnectionError as exc:
        raise SummaryUnavailable("Could not reach OpenRouter — check your connection.") from exc

    if not response.choices:
        raise SummaryUnavailable("OpenRouter returned no choices.")
    text = (response.choices[0].message.content or "").strip()
    if not text:
        raise SummaryUnavailable("OpenRouter returned an empty response.")

    # Not every model honours strict schemas; recover a JSON object if one is
    # wrapped in prose or a fenced code block before giving up.
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise SummaryUnavailable(
                f"{config.OPENROUTER_MODEL} did not return JSON. "
                "Try a model that supports structured outputs."
            )
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise SummaryUnavailable("OpenRouter returned malformed JSON.") from exc

    try:
        return MeetingSummary.model_validate(data).model_dump()
    except ValidationError as exc:
        raise SummaryUnavailable(
            f"{config.OPENROUTER_MODEL} returned JSON that does not match the "
            f"expected shape: {str(exc)[:200]}"
        ) from exc
