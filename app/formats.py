"""Turn a list of segments into the files you actually want to keep.

A segment is a plain dict: {"start": float, "end": float, "text": str,
"speaker": str | None}. Times are seconds from the start of the recording.
"""
from __future__ import annotations


def _clock(seconds: float, millis_sep: str) -> str:
    """Format as HH:MM:SS<sep>mmm.

    Round to whole milliseconds *first*, then decompose. Rounding after the
    split lets 59.9999s carry into a 60th second that never reaches the
    minutes field, emitting the invalid timestamp 00:00:60,000.
    """
    total_ms = int(round(max(seconds, 0.0) * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{millis_sep}{millis:03d}"


def short_clock(seconds: float) -> str:
    """mm:ss, or h:mm:ss once the recording passes an hour."""
    hours, rem = divmod(int(seconds), 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def to_srt(segments: list[dict]) -> str:
    lines = []
    for i, seg in enumerate(segments, start=1):
        label = f"{seg['speaker']}: " if seg.get("speaker") else ""
        lines.append(str(i))
        lines.append(f"{_clock(seg['start'], ',')} --> {_clock(seg['end'], ',')}")
        lines.append(f"{label}{seg['text'].strip()}")
        lines.append("")
    return "\n".join(lines)


def to_vtt(segments: list[dict]) -> str:
    lines = ["WEBVTT", ""]
    for seg in segments:
        label = f"<v {seg['speaker']}>" if seg.get("speaker") else ""
        lines.append(f"{_clock(seg['start'], '.')} --> {_clock(seg['end'], '.')}")
        lines.append(f"{label}{seg['text'].strip()}")
        lines.append("")
    return "\n".join(lines)


def to_txt(segments: list[dict]) -> str:
    """Timestamped plain text, one line per segment."""
    out = []
    for seg in segments:
        label = f"{seg['speaker']}: " if seg.get("speaker") else ""
        out.append(f"[{short_clock(seg['start'])}] {label}{seg['text'].strip()}")
    return "\n".join(out) + "\n"


# A turn is cut when the speaker changes, on a clear pause, or at a hard length
# cap. The cap must be unconditional: Whisper emits near-contiguous segments, so
# a rule that also required a pause almost never fired and produced two-minute
# walls of text with a single timestamp.
MAX_TURN_SECONDS = 35.0
PAUSE_SECONDS = 1.2


def group_by_turns(segments: list[dict]) -> list[dict]:
    """Merge Whisper's prosody-sized fragments into readable paragraphs.

    Whisper cuts every few seconds, so a single speaker's contribution arrives
    as a dozen pieces. Reading is much easier one turn at a time.
    """
    turns: list[dict] = []
    for seg in segments:
        speaker = seg.get("speaker")
        text = seg["text"].strip()
        if not text:
            continue

        current = turns[-1] if turns else None
        if current is not None and current["speaker"] == speaker:
            too_long = seg["end"] - current["start"] >= MAX_TURN_SECONDS
            paused = seg["start"] - current["end"] >= PAUSE_SECONDS
            if not (too_long or paused):
                current["text"] += " " + text
                current["end"] = seg["end"]
                continue

        turns.append({
            "speaker": speaker,
            "start": seg["start"],
            "end": seg["end"],
            "text": text,
        })
    return turns


def to_markdown(segments: list[dict], meta: dict, summary: dict | None = None) -> str:
    """The main human-readable artifact: summary on top, transcript below."""
    out = [f"# {meta.get('title', 'Meeting transcript')}", ""]

    duration = meta.get("duration")
    facts = []
    if duration:
        facts.append(f"**Duration:** {short_clock(duration)}")
    if meta.get("recorded_at"):
        facts.append(f"**Recorded:** {meta['recorded_at']}")
    if meta.get("language"):
        facts.append(f"**Language:** {meta['language']}")
    if meta.get("speakers"):
        facts.append(f"**Speakers:** {meta['speakers']}")
    if facts:
        out += [" · ".join(facts), ""]

    if summary:
        if summary.get("headline"):
            out += [f"> {summary['headline']}", ""]
        if summary.get("summary"):
            out += ["## Summary", "", summary["summary"], ""]
        if summary.get("key_points"):
            out += ["## Key points", ""]
            out += [f"- {p}" for p in summary["key_points"]]
            out += [""]
        if summary.get("decisions"):
            out += ["## Decisions", ""]
            out += [f"- {d}" for d in summary["decisions"]]
            out += [""]
        if summary.get("action_items"):
            out += ["## Action items", "", "| Owner | Task | Due |", "| --- | --- | --- |"]
            for a in summary["action_items"]:
                owner = a.get("owner") or "—"
                due = a.get("due") or "—"
                out.append(f"| {owner} | {a.get('task', '')} | {due} |")
            out += [""]
        if summary.get("open_questions"):
            out += ["## Open questions", ""]
            out += [f"- {q}" for q in summary["open_questions"]]
            out += [""]

    out += ["## Transcript", ""]
    for turn in group_by_turns(segments):
        stamp = short_clock(turn["start"])
        if turn["speaker"]:
            out.append(f"**{turn['speaker']}** · `{stamp}`")
            out.append("")
            out.append(turn["text"])
        else:
            out.append(f"`{stamp}` {turn['text']}")
        out.append("")
    return "\n".join(out)
