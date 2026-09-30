"""Talk to a language model running on this machine (or one you point at).

Skryba does not ship or download a model. It looks for a local model server —
Ollama, LM Studio, llama.cpp, Jan — and uses whatever is already loaded there.
Nothing here needs an API key, and the transcript never leaves the machine
unless LOCAL_LLM_URL deliberately points somewhere else.

Standard library only, so it works the same on macOS and Windows.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Iterator

from . import config


class LLMUnavailable(RuntimeError):
    pass


# Where local model servers listen by default.
_DEFAULT_URLS = [
    "http://127.0.0.1:11434",   # Ollama
    "http://127.0.0.1:1234",    # LM Studio
    "http://127.0.0.1:8080",    # llama.cpp server
    "http://127.0.0.1:1337",    # Jan
]
_PROBE_TIMEOUT = 0.6
_CACHE_SECONDS = 20.0
_cache: dict = {"at": 0.0, "value": None}


def _get_json(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as res:
        return json.load(res)


def _pick_model(names: list[str]) -> str | None:
    """Honour LOCAL_LLM_MODEL; otherwise prefer a chat model over an embedder."""
    if not names:
        return None
    wanted = config.LOCAL_LLM_MODEL
    if wanted:
        for name in names:
            if name == wanted or name.split(":")[0] == wanted:
                return name
        return wanted  # let the server report that it is missing
    chat = [n for n in names if "embed" not in n.lower()]
    return (chat or names)[0]


def _probe(base: str) -> dict | None:
    base = base.rstrip("/")
    # Ollama first: its native API is the only way to raise the context window.
    try:
        data = _get_json(f"{base}/api/tags", _PROBE_TIMEOUT)
        if isinstance(data.get("models"), list):
            model = _pick_model([m.get("name", "") for m in data["models"] if m.get("name")])
            return {"kind": "ollama", "base": base, "model": model}
    except Exception:
        pass
    try:
        data = _get_json(f"{base}/v1/models", _PROBE_TIMEOUT)
        if isinstance(data.get("data"), list):
            model = _pick_model([m.get("id", "") for m in data["data"] if m.get("id")])
            return {"kind": "openai", "base": base, "model": model}
    except Exception:
        pass
    return None


def discover(force: bool = False) -> dict | None:
    """The local server to use, or None. Cached briefly: the UI asks often."""
    now = time.time()
    if not force and now - _cache["at"] < _CACHE_SECONDS:
        return _cache["value"]
    urls = [config.LOCAL_LLM_URL] if config.LOCAL_LLM_URL else _DEFAULT_URLS
    found = None
    for url in urls:
        found = _probe(url)
        if found:
            break
    _cache.update(at=now, value=found)
    return found


def is_available() -> bool:
    server = discover()
    return bool(server and server.get("model"))


def label() -> str:
    server = discover()
    if not server:
        return "no local model found"
    if not server.get("model"):
        return "model server running, but no model loaded"
    name = {"ollama": "Ollama", "openai": "local server"}[server["kind"]]
    return f"{name} · {server['model']}"


def _post(url: str, payload: dict, timeout: float):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            body = json.loads(exc.read().decode("utf-8", "replace"))
            err = body.get("error")
            detail = err.get("message", "") if isinstance(err, dict) else str(err or "")
        except Exception:
            pass
        raise LLMUnavailable(f"The local model returned an error ({exc.code}). {detail}".strip()) from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        _cache["at"] = 0.0  # it may have gone away; look again next time
        raise LLMUnavailable(
            "Could not reach the local model. Is the model server still running?"
        ) from exc


def _require() -> dict:
    server = discover()
    if not server:
        raise LLMUnavailable(
            "No local model found. Start Ollama or LM Studio, or set LOCAL_LLM_URL."
        )
    if not server.get("model"):
        raise LLMUnavailable(
            "The model server is running but has no model. For Ollama, run: "
            "ollama pull qwen2.5:3b"
        )
    return server


def _body(server: dict, messages: list[dict], schema: dict | None,
          stream: bool, max_tokens: int) -> tuple[str, dict]:
    if server["kind"] == "ollama":
        payload = {
            "model": server["model"], "messages": messages, "stream": stream,
            # Ollama silently truncates to a small default window unless told
            # otherwise, which would drop most of a transcript chunk.
            "options": {"num_ctx": config.LOCAL_LLM_CONTEXT, "temperature": 0.2,
                        "num_predict": max_tokens},
        }
        if schema:
            payload["format"] = schema
        return f"{server['base']}/api/chat", payload
    payload = {
        "model": server["model"], "messages": messages, "stream": stream,
        "temperature": 0.2, "max_tokens": max_tokens,
    }
    if schema:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "result", "strict": True, "schema": schema},
        }
    return f"{server['base']}/v1/chat/completions", payload


def complete(messages: list[dict], schema: dict | None = None,
             max_tokens: int = 1500) -> str:
    """One full reply as text. With `schema`, the reply is constrained JSON."""
    server = _require()
    url, payload = _body(server, messages, schema, False, max_tokens)
    with _post(url, payload, config.LOCAL_LLM_TIMEOUT) as res:
        data = json.load(res)
    if server["kind"] == "ollama":
        text = (data.get("message") or {}).get("content", "")
    else:
        choices = data.get("choices") or [{}]
        text = (choices[0].get("message") or {}).get("content", "")
    if not (text or "").strip():
        raise LLMUnavailable("The local model returned an empty reply.")
    return text


def stream(messages: list[dict], max_tokens: int = 900) -> Iterator[str]:
    """Yield the reply piece by piece as the model produces it."""
    server = _require()
    url, payload = _body(server, messages, None, True, max_tokens)
    with _post(url, payload, config.LOCAL_LLM_TIMEOUT) as res:
        for raw in res:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            if server["kind"] == "ollama":
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                piece = (data.get("message") or {}).get("content", "")
                if piece:
                    yield piece
                if data.get("done"):
                    return
            else:
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    return
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    continue
                choices = data.get("choices") or [{}]
                piece = (choices[0].get("delta") or {}).get("content", "")
                if piece:
                    yield piece


def parse_json(text: str) -> dict:
    """Small models wrap JSON in prose or code fences; dig the object out."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    raise LLMUnavailable("The local model did not return valid JSON.")
