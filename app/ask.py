"""Answer a question about one meeting, from its transcript.

A small local model cannot read a whole meeting at once, and answers better
when it is shown only what matters. So each question first selects the turns
that mention its words, and the model answers from those alone — with the
timestamps, so every claim can be checked against the audio.
"""
from __future__ import annotations

import math
import re
from typing import Iterator

from . import formats, llm

EXCERPT_BUDGET_CHARS = 5000
MAX_HITS = 8

# Words too common to say anything about which part of a meeting is relevant.
_STOP = set("""
a i o w z u na do to że się nie jest co jak czy ale tak no by już tam tu tego
tej ten ta te tym tych był była było były być ma mam mają może można oraz dla
od po przy za pod nad też tylko jeszcze bardzo kto kiedy gdzie ile który która
które jaki jaka jakie czym tym ich jego jej nas was oni one ono my wy ja ty on
są sa będzie będą będę mi ci go ją je mu im nam wam ich mnie ciebie sobie
to ten więc bo czyli właśnie wiesz znaczy chyba trochę teraz potem wtedy tutaj
jakie jakiś jakieś któryś coś ktoś nic wszystko wszyscy każdy mój moja twój
powiedział powiedziała mówił mówiła ustalono chodzi
the an and or of in on at is are was were be been it this that for with as by
from what who when where which how did does do about said say
""".split())

SYSTEM = """You answer questions about a meeting, using excerpts from its transcript.

Rules:
- Use only what the excerpts say. If they do not contain the answer, say so \
plainly instead of guessing.
- After each fact, give the time it was said in square brackets, copied from \
the excerpt, like [12:34].
- The transcript came from speech recognition, so some words are wrong. Read \
through obvious errors.
- Be brief: a few sentences or a short list.
- Answer in the language the question was asked in."""


def _stem(word: str) -> str:
    """Crude on purpose. Polish inflects heavily — Paweł, Pawła, Pawłem — and
    trimming to a common prefix matches those forms without a dictionary."""
    return word[:5] if len(word) > 5 else word


def _terms(text: str) -> list[str]:
    words = re.findall(r"[^\W\d_]{2,}|\d+", text.lower())
    return [_stem(w) for w in words if w not in _STOP]


def retrieve(segments: list[dict], question: str) -> list[dict]:
    """The turns most relevant to the question, in the order they were said."""
    turns = formats.group_by_turns(segments)
    if not turns:
        return []
    docs = [_terms(t["text"]) for t in turns]
    query = set(_terms(question))

    scores = [0.0] * len(turns)
    if query:
        n = len(docs)
        avg_len = sum(len(d) for d in docs) / n or 1.0
        for term in query:
            df = sum(1 for d in docs if term in d)
            if not df:
                continue
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            for i, d in enumerate(docs):
                tf = d.count(term)
                if tf:
                    scores[i] += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * len(d) / avg_len))

    ranked = [i for i in sorted(range(len(turns)), key=lambda i: -scores[i]) if scores[i] > 0]
    if ranked:
        chosen: list[int] = []
        for i in ranked[:MAX_HITS]:
            # The turn after a hit is usually the reply to it.
            for j in (i, i + 1):
                if 0 <= j < len(turns) and j not in chosen:
                    chosen.append(j)
    else:
        # Nothing matched — a broad question ("what was this about?"). Give an
        # even spread across the meeting rather than nothing.
        step = max(1, len(turns) // MAX_HITS)
        chosen = list(range(0, len(turns), step))[:MAX_HITS]

    picked, used = [], 0
    for i in chosen:
        cost = len(turns[i]["text"]) + 12
        if picked and used + cost > EXCERPT_BUDGET_CHARS:
            continue
        picked.append(i)
        used += cost
    return [turns[i] for i in sorted(picked)]


def _excerpts(turns: list[dict]) -> str:
    lines = []
    for t in turns:
        who = f"{t['speaker']}: " if t["speaker"] else ""
        lines.append(f"[{formats.short_clock(t['start'])}] {who}{t['text']}")
    return "\n\n".join(lines)


def sources(turns: list[dict]) -> list[dict]:
    return [{"start": t["start"], "clock": formats.short_clock(t["start"]),
             "speaker": t["speaker"], "text": t["text"][:220]} for t in turns]


def answer(segments: list[dict], question: str, summary: dict | None = None,
           turns: list[dict] | None = None) -> Iterator[str]:
    """Stream the answer. Raises llm.LLMUnavailable if no model can be reached."""
    if turns is None:
        turns = retrieve(segments, question)
    context = ""
    if summary and summary.get("summary"):
        context = f"Summary of the whole meeting:\n{summary['summary']}\n\n"
    prompt = (f"{context}Excerpts from the transcript:\n\n{_excerpts(turns)}\n\n"
              f"Question: {question.strip()}")
    return llm.stream([{"role": "system", "content": SYSTEM},
                       {"role": "user", "content": prompt}])
