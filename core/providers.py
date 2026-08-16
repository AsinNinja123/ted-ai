"""Model-provider routing for Ted.

Normal turns use Groq's free hosted model for speed.  If the key is absent,
Groq is unreachable, the model is down, or the free-tier limit is exhausted,
the same request is retried against a local Ollama model.  The rest of Ted sees
one OpenAI/Groq-shaped response interface, so chat, tools, JSON extraction, and
vision all share the same fallback behavior.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
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
    LOCAL_CHAT_MODEL = "qwen3.5:9b-q4_K_M"

try:
    from config import LOCAL_TOOL_MODEL
except Exception:
    LOCAL_TOOL_MODEL = "qwen3.5:35b-a3b"

try:
    from config import OLLAMA_URL
except Exception:
    OLLAMA_URL = "http://127.0.0.1:11434"


# max_retries=0 is deliberate. The SDK defaults to retrying twice, silently,
# with backoff — and its timeout is PER ATTEMPT, so one "30 second" request can
# take well over a minute while the user sees nothing at all. A real log shows
# `request accepted after 40939ms (groq)`: forty-one seconds inside a single
# create() call, no error, no output, no way to tell it apart from a hang.
#
# Retrying is still fine — but it belongs where it can say what it is doing.
# Below, a rate limit falls through to the local brain, and failing that
# surfaces as an error the user can read within a second or two.
_groq = Groq(api_key=GROQ_API_KEY, max_retries=0) if GROQ_API_KEY else None

# One-element list so the flag can be flipped from inside chat_create without a
# global declaration, and so tests can reset it.
_USAGE_SUPPORTED = [True]

# Groq reports the account's real ceiling and what is left of it on every
# response, in headers. Until now the diagnostics gauge was drawn against a
# number typed in from a blog post — 8,000 — which is per-model, per-account,
# and changes. Reading it means the gauge shows Charlie's actual remaining
# budget instead of my guess at it.
#
# Behind a capability flag for the same reason stream_options is: `with_raw_
# response` does not exist on every SDK version, and this module treats any
# cloud exception as grounds to fall back to the local brain. A telemetry
# nicety must never be able to take the cloud offline. That already happened
# once this week.
_HEADERS_SUPPORTED = [True]

# Why the last call did not use the cloud. The dashboard reported "0 rate
# limited" through fourteen consecutive rate limits, because the exception is
# caught HERE and turned into a local answer — llm.py never saw a
# RateLimitError to record. The panel was therefore telling Charlie the ceiling
# was fine while the ceiling was the entire reason his replies had gone from
# 600ms to 8 seconds. A diagnostics panel that under-reports is worse than none.
_last_fallback = ""

# Do not hammer Groq after it has explicitly said the account is out of
# budget. A 429 used to make every foreground and background helper try the
# same doomed request before falling through to Ollama. Apart from wasting a
# round trip, those probes keep the diagnostics noisy and can extend recovery.
_cloud_retry_at = 0.0
DEFAULT_CLOUD_COOLDOWN = 15.0
# A retry that turns out to be premature costs one 429 and one more fallback
# turn. A twenty-two minute blackout costs the whole session, so cap how long
# any single provider hint is allowed to pin the cloud off.
MAX_CLOUD_COOLDOWN = 120.0


def last_fallback_reason() -> str:
    """`rate_limit`, `unavailable`, or '' if the last call used the cloud."""
    return _last_fallback
_rate_limit = {"limit_tokens": 0, "remaining_tokens": 0,
               "limit_requests": 0, "remaining_requests": 0, "reset": ""}


def rate_limit_status():
    """The provider's own view of the budget, or zeros if never reported."""
    out = dict(_rate_limit)
    out["cooldown_seconds"] = cloud_cooldown_remaining()
    return out


def cloud_cooldown_remaining() -> int:
    """Whole seconds until auto mode will try the cloud again."""
    return max(0, int(_cloud_retry_at - time.time() + 0.999))


def _duration_seconds(raw) -> float:
    """Parse Groq retry/reset values such as ``19.2s`` or ``1m4s``."""
    text = str(raw or "").strip().lower()
    if not text:
        return 0.0
    if text.replace(".", "", 1).isdigit():
        return float(text)
    total = 0.0
    for value, unit in re.findall(r"([0-9]+(?:\.[0-9]+)?)\s*(ms|s|m|h)", text):
        scale = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}[unit]
        total += float(value) * scale
    return total


def _start_cloud_cooldown(exc) -> float:
    """Honor the earliest provider-approved retry, not the full bucket reset.

    Groq's ``x-ratelimit-reset-tokens`` can describe when the entire token
    bucket is full again (several minutes), while the 429 body says the request
    can be retried in fifteen seconds. Taking the maximum is why Ted stayed on
    Ollama long after the cloud was usable again.

    So every hint is a candidate and the *earliest* one wins, including the
    ``retry-after`` header — which used to short-circuit the chain and was
    therefore the same bug the paragraph above says was fixed. The result is
    capped, and it replaces any existing cooldown rather than only extending
    it: a shorter provider-approved retry is better information, not a
    regression.
    """
    global _cloud_retry_at
    candidates = []
    try:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None) or {}
        candidates = [value for value in (
            _duration_seconds(headers.get("retry-after")),
            _duration_seconds(headers.get("x-ratelimit-reset-tokens")),
            _duration_seconds(headers.get("x-ratelimit-reset-requests")),
        ) if value > 0]
        _read_rate_headers(headers)
    except Exception:
        pass
    match = re.search(
        r"(?:try again in|retry(?:ing)? after)\s+([0-9.]+\s*(?:ms|s|m|h))",
        str(exc), re.I)
    body_wait = _duration_seconds(match.group(1)) if match else 0.0
    if body_wait > 0:
        candidates.append(body_wait)
    seconds = min(candidates) if candidates else DEFAULT_CLOUD_COOLDOWN
    seconds = min(seconds, MAX_CLOUD_COOLDOWN)
    if candidates and max(candidates) - min(candidates) > 1.0:
        # Print the disagreement rather than making the next reader re-derive it.
        print(f"[provider] rate-limit hints disagree: "
              f"{'/'.join(f'{value:.1f}s' for value in sorted(candidates))} "
              f"— using {seconds:.1f}s")
    _cloud_retry_at = time.time() + seconds
    return seconds


def _note_usage(result):
    """Record token usage for NON-streamed calls.

    Fact extraction, session summaries, message composition and web synthesis
    all go through chat_create too. They are not conversation turns, so they
    never appear in turn_log — but they spend the same tokens against the same
    per-minute ceiling. Charlie's gauge was summing turns only and therefore
    always read low, which is part of why the limit kept arriving unannounced.

    Streamed turns are skipped here: their usage arrives in the stream and is
    already recorded by llm.py, and counting it twice would swap one wrong
    number for another.
    """
    try:
        usage = getattr(result, "usage", None)
        if usage is None:
            return
        from core import telemetry
        telemetry.note_side_usage(
            int(getattr(usage, "prompt_tokens", 0) or 0),
            int(getattr(usage, "completion_tokens", 0) or 0))
    except Exception:
        pass


def _read_rate_headers(headers):
    """Pull Groq's x-ratelimit-* headers into module state. Never raises."""
    try:
        def _num(name):
            raw = headers.get(name) or headers.get(name.title()) or ""
            raw = str(raw).strip()
            return int(float(raw)) if raw.replace(".", "", 1).isdigit() else 0
        limit = _num("x-ratelimit-limit-tokens")
        if limit:
            _rate_limit["limit_tokens"] = limit
            _rate_limit["remaining_tokens"] = _num("x-ratelimit-remaining-tokens")
            _rate_limit["limit_requests"] = _num("x-ratelimit-limit-requests")
            _rate_limit["remaining_requests"] = _num("x-ratelimit-remaining-requests")
            _rate_limit["reset"] = str(
                headers.get("x-ratelimit-reset-tokens", "") or "")
    except Exception:
        pass
_active_provider = "none"
_last_cloud_error = ""
_last_model = ""

# ---------- which brain, on purpose ----------
#
# "auto" is the shipping behaviour: cloud first, local when the cloud fails.
# The other two exist for testing, and they deliberately do NOT fall back —
# the whole point of pinning a brain is to see what THAT brain does. A forced
# mode that quietly failed over would report the other model's behaviour under
# the label of the one you selected, which is the exact class of lie this
# project keeps having to fix.
#
# Stored on disk rather than in module state because the dashboard can run as
# a separate process (`python -m dashboard`) as well as in Ted's own thread.
_MODE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "runtime.json")
_VALID_MODES = ("auto", "cloud", "local")
_mode_cache = {"mtime": -1.0, "value": "auto"}


def get_provider_mode() -> str:
    """Return ``auto``, ``cloud``, or ``local``. Cheap enough for every call."""
    try:
        mtime = os.path.getmtime(_MODE_PATH)
    except OSError:
        return "auto"
    if mtime != _mode_cache["mtime"]:
        try:
            with open(_MODE_PATH, encoding="utf-8") as fh:
                value = (json.load(fh) or {}).get("provider_mode", "auto")
        except Exception:
            value = "auto"
        _mode_cache["mtime"] = mtime
        _mode_cache["value"] = value if value in _VALID_MODES else "auto"
    return _mode_cache["value"]


def set_provider_mode(mode: str) -> str:
    """Pin the brain. Returns the mode actually in force."""
    if mode not in _VALID_MODES:
        raise ValueError(f"mode must be one of {_VALID_MODES}")
    os.makedirs(os.path.dirname(_MODE_PATH), exist_ok=True)
    data = {}
    try:
        with open(_MODE_PATH, encoding="utf-8") as fh:
            data = json.load(fh) or {}
    except Exception:
        pass
    data["provider_mode"] = mode
    tmp = _MODE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, _MODE_PATH)
    _mode_cache["mtime"] = -1.0
    return mode


def active_provider() -> str:
    """Return ``groq``, ``ollama``, or ``none`` for the most recent call."""
    return _active_provider


def active_model() -> str:
    """The model name that actually served the most recent call."""
    return _last_model


def last_cloud_error() -> str:
    return _last_cloud_error


def local_model_ready() -> bool:
    """True when Ollama answers and has the configured model pulled.

    Used by the dashboard so switching to the local brain can say up front
    whether it will work, instead of pinning a mode that then fails on the
    next message.
    """
    try:
        res = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=1.5)
        names = [m.get("name", "") for m in (res.json() or {}).get("models", [])]
        def _ready(model):
            if ":" in model:
                return model in names
            base = model.split(":")[0]
            return any(n.split(":")[0] == base for n in names)
        return all(_ready(model) for model in {LOCAL_CHAT_MODEL, LOCAL_TOOL_MODEL})
    except Exception:
        return False


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


# When the local brain cannot start, every later cloud failure would pay the
# full startup wait again — 20 polls at a 1s timeout each, up to ~23 seconds of
# silence per message, on top of whatever the cloud failure already cost. One
# failure buys quiet for this long instead.
OLLAMA_RETRY_COOLDOWN = 300.0
OLLAMA_START_BUDGET = 6.0
# How long a streaming local turn may go between chunks once it has started
# producing. Distinct from the first-chunk budget, which has to cover a cold
# model load; see _OllamaStream.
OLLAMA_STALL_TIMEOUT = 30.0
_ollama_down_until = 0.0


def local_brain_available() -> bool:
    """False while the local brain is in its failure cooldown."""
    return time.time() >= _ollama_down_until


def _ensure_ollama():
    """Start the local Ollama service when the app is installed but idle.

    Fails fast and stays failed for a while. The point of the local brain is to
    rescue a cloud outage; a rescue that takes twenty seconds and then throws is
    worse for the user than an immediate honest error, because it is
    indistinguishable from a freeze.
    """
    global _ollama_down_until
    now = time.time()
    if now < _ollama_down_until:
        raise RuntimeError(
            f"local brain unavailable, not retried for another "
            f"{int(_ollama_down_until - now)}s")

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
        _ollama_down_until = time.time() + OLLAMA_RETRY_COOLDOWN
        print("[provider] Ollama is not installed — local fallback disabled "
              f"for {int(OLLAMA_RETRY_COOLDOWN / 60)} min")
        raise RuntimeError("Ollama is not installed or could not start") from exc

    deadline = time.time() + OLLAMA_START_BUDGET
    while time.time() < deadline:
        try:
            if httpx.get(f"{OLLAMA_URL}/api/tags", timeout=1.0).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.15)
    _ollama_down_until = time.time() + OLLAMA_RETRY_COOLDOWN
    print(f"[provider] Ollama did not start within {OLLAMA_START_BUDGET:.0f}s — "
          f"local fallback disabled for {int(OLLAMA_RETRY_COOLDOWN / 60)} min")
    raise RuntimeError("Ollama did not become ready")


def _ollama_payload(kwargs):
    model = _local_model_for(kwargs)
    payload = {
        "model": model,
        "messages": _ollama_messages(kwargs.get("messages") or []),
        "stream": bool(kwargs.get("stream", False)),
        # The TOOL model is the rescue model: it is what a rate-limited
        # foreground turn falls back to. It used to have the SHORTER
        # keep-alive, so it unloaded first and was reliably cold at the one
        # moment it mattered. It gets the longer residency now. Both resident
        # is ~30 GB of the 48 GB here, which is why chat is not also 30m.
        "keep_alive": "30m" if model == LOCAL_TOOL_MODEL else "10m",
    }
    if kwargs.get("tools"):
        payload["tools"] = kwargs["tools"]
    response_format = kwargs.get("response_format")
    if response_format and response_format.get("type") == "json_object":
        payload["format"] = "json"
    short_helper = not kwargs.get("tools") and (kwargs.get("max_tokens") or 0) <= 500
    effort = kwargs.get("reasoning_effort", "none" if short_helper else "default")
    payload["think"] = effort != "none"
    # Ted's prompts are deliberately compact. Capping the local KV cache keeps
    # two fallback specialists resident comfortably on a 48 GB Mac and avoids
    # allocating a huge model-default context that no normal turn uses.
    options = {"num_ctx": 8192}
    if kwargs.get("max_tokens") is not None:
        options["num_predict"] = kwargs["max_tokens"]
    if kwargs.get("temperature") is not None:
        options["temperature"] = kwargs["temperature"]
    if options:
        payload["options"] = options
    return payload


def _local_model_for(kwargs):
    """Use a fast chat model for prose/helpers and the stronger MoE for tools."""
    if kwargs.get("tools"):
        return LOCAL_TOOL_MODEL
    for message in kwargs.get("messages") or []:
        content = message.get("content")
        if isinstance(content, list) and any(
                part.get("type") == "image_url" for part in content if isinstance(part, dict)):
            return LOCAL_TOOL_MODEL
    return LOCAL_CHAT_MODEL


class _OllamaStream:
    """Adapt Ollama NDJSON chunks to the shape consumed by ``_stream_turn``."""

    def __init__(self, payload, timeout, stall_timeout=OLLAMA_STALL_TIMEOUT):
        # Two different waits, two different budgets. The FIRST chunk has to
        # cover loading a cold 24 GB model off disk, so it gets the full
        # timeout; capping it at 25s is what made the rate-limit rescue fail
        # exactly when it was needed most. Every chunk after that should
        # arrive steadily, so a stalled generation still fails fast instead of
        # looking frozen for three minutes.
        #
        # httpx applies ONE read timeout to a whole stream, so the tighter
        # inter-chunk deadline is enforced here instead: a reader thread feeds
        # lines into a queue and the consumer waits on the queue, not on the
        # socket. Closing httpx from a second thread is not an option — it
        # waits on the same connection the read is blocked in, so a watchdog
        # deadlocks rather than rescuing anything.
        self._client = httpx.Client(timeout=httpx.Timeout(
            timeout, connect=4.0, read=timeout))
        self._ctx = self._client.stream("POST", f"{OLLAMA_URL}/api/chat", json=payload)
        self._response = self._ctx.__enter__()
        self._response.raise_for_status()
        self._closed = False
        self._call_index = 0
        self._first_budget = timeout
        self._stall_timeout = stall_timeout
        self._lines = queue.Queue()
        self._drained = threading.Event()
        threading.Thread(target=self._read_lines, daemon=True).start()

    def _read_lines(self):
        """Drain the socket into the queue. ``(None, None)`` marks a clean end."""
        try:
            for line in self._response.iter_lines():
                self._lines.put((line, None))
        except Exception as exc:
            self._lines.put((None, exc))
        else:
            self._lines.put((None, None))
        finally:
            self._drained.set()

    def _abandon(self):
        """Give up on a stalled stream without waiting for the socket.

        The reader is blocked inside a socket read and closing the client waits
        on that same connection, so the close is handed to a daemon thread that
        nobody joins. The caller gets its timeout now; the connection is
        reclaimed whenever Ollama gets around to dropping it.
        """
        if self._closed:
            return
        self._closed = True
        threading.Thread(target=self._shutdown, daemon=True).start()

    def __iter__(self):
        budget = self._first_budget
        while True:
            try:
                line, error = self._lines.get(timeout=budget)
            except queue.Empty:
                self._abandon()
                raise httpx.ReadTimeout(
                    f"local model produced no output for {budget:.0f}s") from None
            # Every chunk that arrives moves the stream onto the short budget.
            budget = self._stall_timeout
            if error is not None:
                raise error
            if line is None:
                return
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
        """Close, without ever blocking the caller on the reader thread.

        Barge-in closes a stream mid-response, and at that moment the reader is
        parked in a socket read the close would have to wait on. Only a stream
        the reader has already finished with can be closed inline; anything
        else is abandoned to a daemon thread, same as a stall.
        """
        if self._closed:
            return
        if not self._drained.is_set():
            self._abandon()
            return
        self._closed = True
        self._shutdown()

    def _shutdown(self):
        try:
            self._ctx.__exit__(None, None, None)
        except Exception:
            pass
        finally:
            try:
                self._client.close()
            except Exception:
                pass


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
    global _active_provider, _last_cloud_error, _last_model, _last_fallback
    cloud_error = None
    workload = kwargs.pop("_ted_workload", "foreground")
    local_model = _local_model_for(kwargs)
    mode = get_provider_mode()
    if mode == "local":
        # Pinned. No cloud attempt at all, so what comes back is unambiguously
        # the local brain's own behaviour and its own latency.
        result = _ollama_create(**kwargs)
        _active_provider, _last_model = "ollama", local_model
        _last_fallback = ""
        return result

    # Titles, fact extraction and session summaries are useful, but they must
    # not consume the scarce cloud allowance needed for Charlie's next actual
    # message. Explicit cloud pinning still means cloud, including helpers.
    background_local = mode == "auto" and workload == "background"
    cooling_down = mode == "auto" and cloud_cooldown_remaining() > 0
    if background_local:
        result = _ollama_create(**kwargs)
        _active_provider, _last_model = "ollama", local_model
        _last_fallback = ""
        return result
    if cooling_down:
        _last_fallback = "rate_limit"
        _last_cloud_error = (
            f"cloud cooldown active for {cloud_cooldown_remaining()}s")

    if _groq is not None and not cooling_down:
        params = dict(kwargs)
        params["model"] = CLOUD_CHAT_MODEL
        # Ask Groq to append a final usage chunk to the stream. Without this a
        # streamed turn reports no token counts at all and the dashboard is
        # left estimating from character length — which is close enough to be
        # believed and wrong enough to matter next to an 8,000/minute ceiling.
        #
        # The installed SDK does not expose stream_options as a named keyword,
        # although Groq's HTTP API supports it. extra_body is the SDK's intended
        # compatibility escape hatch, so this reaches the API without making a
        # supported cloud request look like an SDK failure.
        if params.get("stream") and _USAGE_SUPPORTED[0]:
            extra_body = dict(params.get("extra_body") or {})
            extra_body.setdefault("stream_options", {"include_usage": True})
            params["extra_body"] = extra_body
        # Tool-bearing foreground turns get Qwen's thinking mode. Tiny helper
        # calls (titles, JSON extraction, short compositions) do not: otherwise
        # their small output budget can be consumed entirely by hidden reasoning
        # and return an empty answer.
        short_helper = not params.get("tools") and (params.get("max_tokens") or 0) <= 500
        params.setdefault("reasoning_effort", "none" if short_helper else "default")
        params.setdefault("reasoning_format", "hidden")
        try:
            try:
                if _HEADERS_SUPPORTED[0]:
                    # with_raw_response gives the HTTP headers alongside the
                    # normal return value. .parse() hands back exactly what
                    # .create() would have, streaming included, so callers
                    # cannot tell the difference.
                    try:
                        raw = _groq.chat.completions.with_raw_response.create(**params)
                        _read_rate_headers(raw.headers)
                        result = raw.parse()
                    except (AttributeError, TypeError) as hexc:
                        if "stream_options" in str(hexc):
                            raise
                        _HEADERS_SUPPORTED[0] = False
                        print("[provider] this groq SDK cannot expose response "
                              "headers — the rate-limit gauge will estimate.")
                        result = _groq.chat.completions.create(**params)
                else:
                    result = _groq.chat.completions.create(**params)
            except TypeError:
                # Do not reinterpret unrelated programming errors as provider
                # outages. The header wrapper compatibility path above already
                # retries through the ordinary SDK method where appropriate.
                raise
            _active_provider, _last_model = "groq", CLOUD_CHAT_MODEL
            _last_cloud_error = ""
            _last_fallback = ""
            _note_usage(result)
            return result
        except Exception as exc:
            cloud_error = exc
            _last_cloud_error = str(exc)
            _last_fallback = ("rate_limit"
                              if "429" in str(exc)
                              or "rate limit" in str(exc).lower()
                              else "unavailable")
            if "429" in str(exc) or "rate limit" in str(exc).lower():
                wait = _start_cloud_cooldown(exc)
                print(f"[provider] RATE LIMITED on {CLOUD_CHAT_MODEL} — "
                      f"trying local {local_model}; cloud paused "
                      f"for {wait:.1f}s")
            else:
                print(f"[provider] Groq unavailable ({str(exc)[:100]}) — "
                      f"using local {local_model}")

    if mode == "cloud":
        # Pinned to the cloud: surface the real cloud failure instead of
        # quietly answering as a different model.
        _active_provider, _last_model = "none", ""
        raise cloud_error if cloud_error is not None else RuntimeError(
            "Cloud brain pinned but no Groq API key is configured.")

    try:
        result = _ollama_create(**kwargs)
        _active_provider, _last_model = "ollama", local_model
        return result
    except Exception as local_error:
        _active_provider, _last_model = "none", ""
        if cloud_error is not None:
            raise RuntimeError(
                f"Both brains failed; Groq: {cloud_error}; Ollama: {local_error}"
            ) from local_error
        raise
