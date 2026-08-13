"""Model-provider routing for Ted.

Normal turns use Groq's free hosted model for speed.  If the key is absent,
Groq is unreachable, the model is down, or the free-tier limit is exhausted,
the same request is retried against a local Ollama model.  The rest of Ted sees
one OpenAI/Groq-shaped response interface, so chat, tools, JSON extraction, and
vision all share the same fallback behavior.
"""

from __future__ import annotations

import json
import subprocess
import time
from types import SimpleNamespace

import httpx
from groq import Groq

try:
    from config import GROQ_API_KEY
except Exception:
    GROQ_API_KEY = ""

try:
    from config import CLOUD_CHAT_MODEL
except Exception:
    CLOUD_CHAT_MODEL = "qwen/qwen3.6-27b"

try:
    from config import LOCAL_CHAT_MODEL
except Exception:
    LOCAL_CHAT_MODEL = "qwen3.5:35b-a3b"

try:
    from config import OLLAMA_URL
except Exception:
    OLLAMA_URL = "http://127.0.0.1:11434"


_groq = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
_active_provider = "none"
_last_cloud_error = ""


def active_provider() -> str:
    """Return ``groq``, ``ollama``, or ``none`` for the most recent call."""
    return _active_provider


def last_cloud_error() -> str:
    return _last_cloud_error


def groq_client():
    """Expose the Groq client for Whisper STT without creating a second one."""
    return _groq


def _ollama_messages(messages):
    """Translate OpenAI-style messages into Ollama's native chat format."""
    converted = []
    call_names = {}
    for message in messages:
        role = message.get("role", "user")
        item = {"role": role}
        content = message.get("content", "")

        # Ollama's native API puts base64 images in ``images`` rather than in
        # OpenAI content parts.
        if isinstance(content, list):
            text_parts, images = [], []
            for part in content:
                if part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
                elif part.get("type") == "image_url":
                    raw = (part.get("image_url") or {}).get("url", "")
                    if raw.startswith("data:") and "," in raw:
                        images.append(raw.split(",", 1)[1])
            item["content"] = "\n".join(p for p in text_parts if p)
            if images:
                item["images"] = images
        else:
            item["content"] = content or ""

        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            native_calls = []
            for call in tool_calls:
                fn = call.get("function") or {}
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                name = fn.get("name", "")
                if call.get("id"):
                    call_names[call["id"]] = name
                native_calls.append({
                    "type": "function",
                    "function": {"name": name, "arguments": args},
                })
            item["tool_calls"] = native_calls

        if role == "tool":
            name = message.get("name") or call_names.get(message.get("tool_call_id"), "")
            if name:
                item["tool_name"] = name
        converted.append(item)
    return converted


def _ensure_ollama():
    """Start the local Ollama service when the app is installed but idle."""
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=1.0)
        if r.status_code == 200:
            return
    except Exception:
        pass
    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        raise RuntimeError("Ollama is not installed or could not start") from exc
    for _ in range(20):
        try:
            if httpx.get(f"{OLLAMA_URL}/api/tags", timeout=1.0).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.15)
    raise RuntimeError("Ollama did not become ready")


def _ollama_payload(kwargs):
    payload = {
        "model": LOCAL_CHAT_MODEL,
        "messages": _ollama_messages(kwargs.get("messages") or []),
        "stream": bool(kwargs.get("stream", False)),
        "keep_alive": "10m",
    }
    if kwargs.get("tools"):
        payload["tools"] = kwargs["tools"]
    response_format = kwargs.get("response_format")
    if response_format and response_format.get("type") == "json_object":
        payload["format"] = "json"
    short_helper = not kwargs.get("tools") and (kwargs.get("max_tokens") or 0) <= 500
    effort = kwargs.get("reasoning_effort", "none" if short_helper else "default")
    payload["think"] = effort != "none"
    options = {}
    if kwargs.get("max_tokens") is not None:
        options["num_predict"] = kwargs["max_tokens"]
    if kwargs.get("temperature") is not None:
        options["temperature"] = kwargs["temperature"]
    if options:
        payload["options"] = options
    return payload


class _OllamaStream:
    """Adapt Ollama NDJSON chunks to the shape consumed by ``_stream_turn``."""

    def __init__(self, payload, timeout):
        self._client = httpx.Client(timeout=httpx.Timeout(timeout, connect=4.0))
        self._ctx = self._client.stream("POST", f"{OLLAMA_URL}/api/chat", json=payload)
        self._response = self._ctx.__enter__()
        self._response.raise_for_status()
        self._closed = False
        self._call_index = 0

    def __iter__(self):
        for line in self._response.iter_lines():
            if not line:
                continue
            data = json.loads(line)
            if data.get("error"):
                raise RuntimeError(data["error"])
            message = data.get("message") or {}
            content = message.get("content") or ""
            tool_deltas = []
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                idx = fn.get("index", self._call_index)
                self._call_index = max(self._call_index, idx + 1)
                args = fn.get("arguments", {})
                if not isinstance(args, str):
                    args = json.dumps(args, separators=(",", ":"))
                tool_deltas.append(SimpleNamespace(
                    index=idx,
                    id=f"ollama_call_{idx}",
                    function=SimpleNamespace(name=fn.get("name", ""), arguments=args),
                ))
            delta = SimpleNamespace(content=content, tool_calls=tool_deltas or None)
            yield SimpleNamespace(choices=[SimpleNamespace(delta=delta)])

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._ctx.__exit__(None, None, None)
        finally:
            self._client.close()


def _ollama_create(**kwargs):
    _ensure_ollama()
    payload = _ollama_payload(kwargs)
    # A cold 24 GB model can take much longer to load than a network request.
    timeout = max(float(kwargs.get("timeout") or 30.0), 180.0)
    if payload["stream"]:
        return _OllamaStream(payload, timeout)
    with httpx.Client(timeout=httpx.Timeout(timeout, connect=4.0)) as client:
        response = client.post(f"{OLLAMA_URL}/api/chat", json=payload)
        response.raise_for_status()
        data = response.json()
    message = data.get("message") or {}
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=message.get("content") or "",
        tool_calls=message.get("tool_calls") or None,
    ))])


def chat_create(**kwargs):
    """Run a chat request on free Groq, falling back to local Ollama.

    The caller never needs to decide whether an error is a rate limit, outage,
    missing key, or lost internet connection: every cloud failure is eligible
    for the local fallback.  If both providers fail, the local exception is
    raised with the cloud failure chained for logs.
    """
    global _active_provider, _last_cloud_error
    cloud_error = None
    if _groq is not None:
        params = dict(kwargs)
        params["model"] = CLOUD_CHAT_MODEL
        # Tool-bearing foreground turns get Qwen's thinking mode. Tiny helper
        # calls (titles, JSON extraction, short compositions) do not: otherwise
        # their small output budget can be consumed entirely by hidden reasoning
        # and return an empty answer.
        short_helper = not params.get("tools") and (params.get("max_tokens") or 0) <= 500
        params.setdefault("reasoning_effort", "none" if short_helper else "default")
        params.setdefault("reasoning_format", "hidden")
        try:
            result = _groq.chat.completions.create(**params)
            _active_provider = "groq"
            _last_cloud_error = ""
            return result
        except Exception as exc:
            cloud_error = exc
            _last_cloud_error = str(exc)
            print(f"[provider] Groq unavailable ({str(exc)[:100]}) — using local {LOCAL_CHAT_MODEL}")

    try:
        result = _ollama_create(**kwargs)
        _active_provider = "ollama"
        return result
    except Exception as local_error:
        _active_provider = "none"
        if cloud_error is not None:
            raise RuntimeError(
                f"Both brains failed; Groq: {cloud_error}; Ollama: {local_error}"
            ) from local_error
        raise
