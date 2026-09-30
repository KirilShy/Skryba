"""Which lines of a transcript deserve a second look.

Whisper reports how sure it was, and both backends used to throw that away.
Two things are worth knowing about those numbers before trusting them:

- avg_logprob / no_speech_prob / compression_ratio describe a whole 30-second
  decoding window, so every segment cut from one window shares them. They
  cannot tell a good line from a bad one inside that window.
- A hallucination is not low-confidence. Whisper emits "Dziękuje za oglądanie"
  over silence with a perfectly healthy score — it learned that line from
  subtitle files. Confidence alone never catches it.

So the per-line signal is word probability, and invented text gets its own
detectors: known subtitle-credit phrases, lines stranded in silence, and loops.
"""
from __future__ import annotations

import re

# Words below this are stored with the segment (as character spans) so the
# display threshold can be tuned later without re-transcribing anything.
STORE_WORD_BELOW = 0.5

# Display thresholds, calibrated on real meeting audio (see README).
DOUBT_WORD_BELOW = 0.3       # a word this unsure gets marked individually
CHECK_MEAN_BELOW = 0.5       # a line whose words average below this is flagged
MIN_WORDS_FOR_MEAN = 3       # "No.", "Tak, tak." carry too little to judge
# Whisper scores the first word after a timestamp low even when it is right,
# and particles like "po", "ci", "z" score low constantly. Marking either
# buries the real catches (a misheard name, "USB-C" heard as "cel").
MIN_DOUBT_WORD_CHARS = 3
# On a bad recording most lines are shaky. Flagging all of them says nothing,
# so keep only the worst share and report the overall quality separately.
MAX_CHECK_SHARE = 0.12

# A line with this much silence on both sides did not come from a conversation.
ISOLATION_SECONDS = 8.0
ISOLATED_MAX_WORDS = 5
LOOP_REPEATS = 3

# Credits and sign-offs Whisper learned from subtitle files and recites over
# silence. Matched on normalised text; extend freely.
_ARTIFACT_PATTERNS = [
    r"dzi[eę]kuj[eę] za ogl[aą]danie",
    r"dzi[eę]ki za ogl[aą]danie",
    r"napisy (stworzone|wykonane|zrobione) przez",
    r"amara ?org",
    r"zapraszam do subskryb",
    r"thanks? (you )?for watching",
    r"subtitles by",
    r"please subscribe",
    r"дякую за перегляд",
    r"спасибо за просмотр",
    r"субтитры (сделал|создавал|подготовил)",
    r"продолжение следует",
    r"^koniec$",
    r"^the end$",
]
_ARTIFACT_RE = re.compile("|".join(_ARTIFACT_PATTERNS))
# Plausible as real speech (a lecture does end this way), so these only count
# when the line is also stranded in silence.
_SIGNOFF_RE = re.compile(r"dzi[eę]kuj[eę] za uwag[eę]|do zobaczenia|thank you\b")


def _norm(text: str) -> str:
    return re.sub(r"[^\w ]", "", text.lower()).strip()


def pack(text: str, words: list[dict] | None, avg_logprob=None,
         no_speech_prob=None, compression_ratio=None) -> dict | None:
    """Condense a backend's raw confidence output into what we persist.

    `words` is a list of {"word": str, "probability": float}. Returns a small
    dict, or None when the backend gave us nothing to go on.
    """
    q: dict = {}
    if words:
        probs, low, cursor = [], [], 0
        for w in words:
            token = (w.get("word") or "").strip()
            if not token:
                continue
            p = float(w.get("probability", 1.0))
            probs.append(p)
            # Locate the word in the segment text so the UI can underline it.
            at = text.find(token, cursor)
            if at < 0:
                continue
            cursor = at + len(token)
            first = len(probs) == 1
            letters = sum(ch.isalnum() for ch in token)
            if p < STORE_WORD_BELOW and not first and letters >= MIN_DOUBT_WORD_CHARS:
                low.append([at, cursor, round(p, 2)])
        if probs:
            q["p"] = round(sum(probs) / len(probs), 3)
            q["n"] = len(probs)
            if low:
                q["lw"] = low
    if avg_logprob is not None:
        q["lp"] = round(float(avg_logprob), 2)
    if no_speech_prob is not None:
        q["nsp"] = round(float(no_speech_prob), 2)
    if compression_ratio is not None:
        q["cr"] = round(float(compression_ratio), 2)
    return q or None


def assess(segments: list[dict], finished: bool = True) -> list[dict | None]:
    """One verdict per segment: None, or {"level", "why", "words"}.

    level is "artifact" (probably not said at all) or "check" (said, but the
    wording is doubtful). Computed on read rather than stored, so old
    transcripts pick up better rules without being re-run. Segments a person
    has edited or confirmed carry ok=True and are never flagged.
    """
    verdicts: list[dict | None] = [None] * len(segments)
    norms = [_norm(s.get("text", "")) for s in segments]

    for i, seg in enumerate(segments):
        if seg.get("ok"):
            continue
        text, q = norms[i], seg.get("q") or {}
        n_words = len(text.split())
        if not n_words:
            continue

        gap_before = seg["start"] - segments[i - 1]["end"] if i else seg["start"]
        if i + 1 < len(segments):
            gap_after = segments[i + 1]["start"] - seg["end"]
        else:
            # While a job is still running, the next line simply hasn't arrived.
            gap_after = ISOLATION_SECONDS if finished else 0.0
        isolated = gap_before >= ISOLATION_SECONDS and gap_after >= ISOLATION_SECONDS

        why = None
        if _ARTIFACT_RE.search(text):
            why = "A subtitle credit Whisper tends to invent over silence"
        elif isolated and _SIGNOFF_RE.search(text):
            why = "A sign-off with silence on both sides — likely invented"
        elif isolated and n_words <= ISOLATED_MAX_WORDS and q.get("p", 1.0) < 0.75:
            # Suspicious, but people do say one short thing into a quiet room.
            # Ask for a look; don't strike it through as invented.
            verdicts[i] = {"level": "check", "words": [],
                           "why": "A few words alone in a long silence — may not have been said"}
            continue
        else:
            run = 1
            while i - run >= 0 and norms[i - run] == text:
                run += 1
            fwd = 1
            while i + fwd < len(segments) and norms[i + fwd] == text:
                fwd += 1
            if n_words >= 3 and run + fwd - 1 >= LOOP_REPEATS:
                why = "The same line repeated — Whisper looping"
            elif q.get("cr", 0) > 2.4:
                why = "Highly repetitive text — Whisper looping"
        if why:
            verdicts[i] = {"level": "artifact", "why": why, "words": []}
            continue

        doubtful = [w[:2] for w in q.get("lw", []) if w[2] < DOUBT_WORD_BELOW]
        mean = q.get("p")
        if mean is not None and q.get("n", 0) >= MIN_WORDS_FOR_MEAN and mean < CHECK_MEAN_BELOW:
            verdicts[i] = {"level": "check", "words": doubtful,
                           "why": "Whisper was unsure of most of this line"}
        elif doubtful:
            verdicts[i] = {"level": "check", "words": doubtful,
                           "why": "Whisper was unsure of the marked word"
                                  + ("s" if len(doubtful) > 1 else "")}

    # Keep only the shakiest lines. Rank by the weakest evidence on each: the
    # line's average, pulled down further by its single worst word.
    checks = [i for i, v in enumerate(verdicts) if v and v["level"] == "check"]
    limit = max(3, int(len(segments) * MAX_CHECK_SHARE))
    if len(checks) > limit:
        def badness(i: int) -> float:
            q = segments[i].get("q") or {}
            worst = min((w[2] for w in q.get("lw", [])), default=1.0)
            return min(q.get("p", 1.0), worst + 0.15)
        for i in sorted(checks, key=badness)[limit:]:
            verdicts[i] = None
    return verdicts


def audio_grade(segments: list[dict]) -> dict | None:
    """How well Whisper could hear the recording overall.

    Returns None for transcripts made before confidence was recorded.
    """
    means = sorted(s["q"]["p"] for s in segments if (s.get("q") or {}).get("p") is not None)
    if len(means) < 10:
        return None
    median = means[len(means) // 2]
    if median >= 0.88:
        label = "clear"
    elif median >= 0.75:
        label = "mixed"
    else:
        label = "difficult"
    return {"label": label, "confidence": round(median, 2)}


def annotate(segments: list[dict], finished: bool = True) -> list[dict]:
    """Copies of the segments with a `flag` attached where one applies."""
    out = []
    for seg, verdict in zip(segments, assess(segments, finished)):
        item = {k: v for k, v in seg.items() if k != "q"}
        if verdict:
            item["flag"] = verdict
        out.append(item)
    return out


def count(segments: list[dict]) -> int:
    return sum(1 for v in assess(segments) if v)
