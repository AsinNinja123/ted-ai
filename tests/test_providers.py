"""Provider routing and native Ollama request translation.

No network calls are made here. Run with: venv/bin/python tests/test_providers.py
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import providers

# A real key may be present on Charlie's Mac. Unit tests must never spend it.
providers._openai = None

PASS = FAIL = 0


def check(desc, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {desc}")
    else:
        FAIL += 1
        print(f"  ✗ {desc}")


print("— Ollama translation —")
messages = providers._ollama_messages([
    {"role": "user", "content": [
        {"type": "text", "text": "read this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]},
    {"role": "assistant", "content": "", "tool_calls": [{
        "id": "c1", "type": "function",
        "function": {"name": "get_weather", "arguments": "{}"},
    }]},
    {"role": "tool", "tool_call_id": "c1", "content": "clear"},
])
check("OpenAI image parts become native Ollama images",
      messages[0]["content"] == "read this" and messages[0]["images"] == ["AAAA"])
check("assistant tool arguments become objects",
      messages[1]["tool_calls"][0]["function"]["arguments"] == {})
check("tool result is linked by tool name", messages[2].get("tool_name") == "get_weather")

payload = providers._ollama_payload({
    "messages": [{"role": "user", "content": "hello"}],
    "stream": True,
    "max_tokens": 99,
    "reasoning_effort": "none",
    "response_format": {"type": "json_object"},
})
check("local payload preserves streaming/token cap",
      payload["stream"] and payload["options"]["num_predict"] == 99)
check("plain local chat uses the fast fallback model",
      payload["model"] == providers.LOCAL_CHAT_MODEL)
tool_payload = providers._ollama_payload({
    "messages": [{"role": "user", "content": "open Notes"}],
    "tools": [{"type": "function", "function": {"name": "open_app"}}],
})
check("tool-bearing local turns use the stronger agent model",
      tool_payload["model"] == providers.LOCAL_TOOL_MODEL)
vision_payload = providers._ollama_payload({
    "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}],
})
check("local screenshots use the stronger multimodal model",
      vision_payload["model"] == providers.LOCAL_TOOL_MODEL)
check("local context allocation stays bounded", payload["options"]["num_ctx"] == 8192)
check("local payload can disable thinking", payload["think"] is False)
check("JSON mode maps to Ollama format", payload["format"] == "json")


print("\n— provider fallback —")
original_groq = providers._groq
original_local = providers._ollama_create


class FakeCompletions:
    def __init__(self, value=None, error=None):
        self.value, self.error = value, error

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return self.value


cloud_result = object()
cloud_completions = FakeCompletions(value=cloud_result)
providers._groq = SimpleNamespace(chat=SimpleNamespace(completions=cloud_completions))
providers._ollama_create = lambda **kwargs: (_ for _ in ()).throw(AssertionError("local called"))
check("healthy Groq stays primary", providers.chat_create(messages=[]) is cloud_result)
check("configured free brain model is sent to Groq",
      cloud_completions.kwargs["model"] == providers.CLOUD_CHAT_MODEL)

local_result = object()
providers._groq = SimpleNamespace(chat=SimpleNamespace(
    completions=FakeCompletions(error=RuntimeError("offline"))))
providers._ollama_create = lambda **kwargs: local_result
check("any cloud outage falls back locally",
      providers.chat_create(messages=[]) is local_result
      and providers.active_provider() == "ollama")

providers._groq = None
check("missing Groq key also uses local", providers.chat_create(messages=[]) is local_result)

providers._groq = original_groq
providers._ollama_create = original_local

print("\n— the local brain fails fast, and stays failed —")

# Charlie's case: the ollama binary exists so Popen succeeds, but the server
# never answers. The old loop polled 20 times at a 1s timeout, so EVERY cloud
# failure paid up to ~23 seconds of silence before the error — indistinguishable
# from a freeze, and repeated on the next message, and the one after that.
import time as _time

_orig_url = providers.OLLAMA_URL
_orig_popen = providers.subprocess.Popen
providers.OLLAMA_URL = "http://127.0.0.1:59999"     # nothing is listening
providers.OLLAMA_START_BUDGET = 1.0
providers._ollama_down_until = 0.0
providers.subprocess.Popen = lambda *a, **k: None   # "starts" but never serves

t0 = _time.time()
try:
    providers._ensure_ollama()
    first_error = ""
except Exception as e:
    first_error = str(e)
first = _time.time() - t0
check("a dead local brain gives up inside its budget",
      first < providers.OLLAMA_START_BUDGET * 2.5)
check("…and says so", "did not become ready" in first_error)

t0 = _time.time()
try:
    providers._ensure_ollama()
except Exception as e:
    second_error = str(e)
second = _time.time() - t0
check("the next call does not pay the wait again", second < 0.05)
check("…and explains it is in cooldown rather than pretending",
      "not retried" in second_error)
check("local_brain_available reports the cooldown",
      not providers.local_brain_available())

providers._ollama_down_until = 0.0
check("…and recovers once the cooldown clears", providers.local_brain_available())

providers.OLLAMA_URL = _orig_url
providers.subprocess.Popen = _orig_popen

print("\n" + "=" * 50)
print("\n— streaming usage works through the installed SDK —")

# Aug 14: stream_options was added to get exact token counts. The installed SDK
# rejected it with a TypeError, chat_create treats ANY cloud exception as
# grounds to fall back, and so every single conversation silently moved to the
# local brain at ten times the latency. The log line said "Groq unavailable".
# Groq was entirely available; we were sending it something it did not know.
providers._USAGE_SUPPORTED[0] = True
seen = []


class _OldSDK:
    class chat:
        class completions:
            @staticmethod
            def create(**kw):
                seen.append(dict(kw))
                return "cloud-response"


_saved_groq, _saved_mode = providers._groq, providers.get_provider_mode()
providers._groq = _OldSDK
providers.set_provider_mode("auto")
local_hits = []
_saved_ollama = providers._ollama_create
providers._ollama_create = lambda **kw: local_hits.append(1)

out = providers.chat_create(messages=[], stream=True)
check("an old SDK still gets a cloud answer", out == "cloud-response")
check("…and the turn never fell through to the local brain", not local_hits)
check("…without sending an unsupported named argument",
      "stream_options" not in seen[0])
check("…the HTTP API still receives the usage request",
      seen[0].get("extra_body", {}).get("stream_options") ==
      {"include_usage": True})

seen.clear()
providers.chat_create(messages=[], stream=True)
check("the next call requests exact usage the same safe way",
      "stream_options" not in seen[0]
      and "stream_options" in seen[0].get("extra_body", {}))

# A TypeError that is NOT about stream_options must still behave like a real
# failure, or this rescue would swallow genuine bugs.
providers._USAGE_SUPPORTED[0] = True


class _Broken:
    class chat:
        class completions:
            @staticmethod
            def create(**kw):
                raise TypeError("create() got an unexpected keyword argument 'nonsense'")


providers._groq = _Broken
providers.chat_create(messages=[], stream=True)
check("an unrelated TypeError still falls back to the local brain",
      len(local_hits) == 1)

providers._groq, providers._ollama_create = _saved_groq, _saved_ollama
providers._USAGE_SUPPORTED[0] = True
providers.set_provider_mode(_saved_mode)


print("\n— cloud quota is conserved —")

providers.set_provider_mode("auto")
providers._cloud_retry_at = 0.0
cloud_hits, local_hits = [], []
providers._groq = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions(
    value="cloud")))
providers._groq.chat.completions.create = lambda **kw: cloud_hits.append(kw) or "cloud"
providers._ollama_create = lambda **kw: local_hits.append(kw) or "local"
out = providers.chat_create(messages=[], _ted_workload="background")
check("background helpers stay local in auto mode", out == "local" and not cloud_hits)
check("…and that intentional routing is not reported as a fallback",
      providers.last_fallback_reason() == "")


class _Headers:
    headers = {"retry-after": "19.2s"}


class _RateLimited(RuntimeError):
    response = _Headers()


def _limited(**kw):
    cloud_hits.append(kw)
    raise _RateLimited("429 rate limit; try again in 19.2s")


providers._groq.chat.completions.create = _limited
out = providers.chat_create(messages=[])
check("a 429 falls back locally", out == "local")
check("…and honors the provider cooldown", providers.cloud_cooldown_remaining() >= 19)
cloud_count = len(cloud_hits)
out = providers.chat_create(messages=[])
check("foreground calls skip a cloud known to be rate-limited", out == "local")
check("…without another doomed cloud request", len(cloud_hits) == cloud_count)


# The handover had no user-visible signal at all. degraded_reason is recorded
# after chat_create returns, and for a streamed local turn that is after the
# model has loaded and started generating — so the one thing that would explain
# an eight-second wait only existed once the wait was over.
_notices, _order = [], []
providers.set_fallback_notice(lambda reason, detail: (
    _notices.append(reason), _order.append("notice")))
providers._ollama_create = lambda **kw: _order.append("local") or "local"

providers._cloud_retry_at = 0.0
providers._groq.chat.completions.create = _limited
_order.clear()
providers.chat_create(messages=[])
check("a cloud→local handover tells the user it is happening",
      _notices == ["rate_limit"])
check("…before the slow local call, not after it",
      _order == ["notice", "local"])

_notices.clear()
providers.chat_create(messages=[], _ted_workload="background")
check("background helpers route locally without toasting the user",
      _notices == [])

_notices.clear()
providers._groq.chat.completions.create = lambda **kw: "cloud"
providers._cloud_retry_at = 0.0
providers.chat_create(messages=[])
check("a healthy cloud turn says nothing", _notices == [])

providers.set_fallback_notice(None)
providers._ollama_create = lambda **kw: local_hits.append(kw) or "local"
providers._groq.chat.completions.create = _limited


class _LongResetHeaders:
    headers = {"x-ratelimit-reset-tokens": "4m19s"}


class _SoonRetry(RuntimeError):
    response = _LongResetHeaders()


providers._cloud_retry_at = 0.0
wait = providers._start_cloud_cooldown(
    _SoonRetry("429 rate limit; please try again in 14.97s"))
check("the 429 retry time beats the much later full-bucket reset header",
      14.9 <= wait <= 15.1 and providers.cloud_cooldown_remaining() <= 16)


# The docstring above _start_cloud_cooldown always promised "earliest wins",
# but `retry-after` short-circuited the chain and was never compared against
# anything. When that header carried a bucket-refill figure the long value won
# outright — 38 RATE LIMITED lines in one launch log, one of them pausing the
# cloud for 1321s while the 429 body said 13s.
class _LongRetryAfterHeaders:
    headers = {"retry-after": "1321s", "x-ratelimit-reset-tokens": "22m1s"}


class _LongRetryAfter(RuntimeError):
    response = _LongRetryAfterHeaders()


providers._cloud_retry_at = 0.0
wait = providers._start_cloud_cooldown(
    _LongRetryAfter("429 rate limit; please try again in 13.185s"))
check("a bucket-refill retry-after header loses to the 429 body's own retry",
      13.1 <= wait <= 13.3)

providers._cloud_retry_at = 0.0
wait = providers._start_cloud_cooldown(_LongRetryAfter("429 rate limit reached"))
check("with no body hint the cooldown is still clamped, not 22 minutes",
      wait == providers.MAX_CLOUD_COOLDOWN)

# The cooldown used to only ever extend (`max(...)`), so one bad long value
# pinned the cloud off for its full duration even when the next 429 said the
# request could be retried in seconds.
providers._cloud_retry_at = _time.time() + 600.0
providers._start_cloud_cooldown(
    _SoonRetry("429 rate limit; please try again in 5s"))
check("a shorter provider-approved retry replaces a longer active cooldown",
      providers.cloud_cooldown_remaining() <= 6)

providers._cloud_retry_at = 0.0
providers._groq, providers._ollama_create = _saved_groq, _saved_ollama
providers.set_provider_mode(_saved_mode)


print("\n— the local rescue gets time to wake up, but not to hang —")

# The streaming rescue path built its client with read=min(timeout, 25.0),
# throwing away the 180s budget _ollama_create had just computed *because* a
# cold 24 GB model cannot load and emit a first token in 25 seconds. So the
# fallback failed precisely when the cloud was rate-limited and the local model
# was cold — the one moment it existed for. First chunk and later chunks are
# two different waits and now have two different budgets.
import json          # noqa: E402
import socket        # noqa: E402
import threading     # noqa: E402

_chunk = json.dumps({"message": {"content": "hi"}}).encode()


def _stalling_server(sock):
    """Answer one /api/chat, send a single NDJSON chunk, then go quiet."""
    conn, _ = sock.accept()
    try:
        conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/x-ndjson\r\n"
                     b"Transfer-Encoding: chunked\r\n\r\n")
        body = _chunk + b"\n"
        conn.sendall(f"{len(body):x}\r\n".encode() + body + b"\r\n")
        _time.sleep(10.0)          # the stall the watchdog has to catch
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


_sock = socket.socket()
_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
_sock.bind(("127.0.0.1", 0))
_sock.listen(1)
_orig_stream_url = providers.OLLAMA_URL
providers.OLLAMA_URL = f"http://127.0.0.1:{_sock.getsockname()[1]}"
threading.Thread(target=_stalling_server, args=(_sock,), daemon=True).start()

_stream = providers._OllamaStream(
    {"model": "x", "messages": [], "stream": True}, timeout=180.0, stall_timeout=1.0)
check("the first chunk keeps the full cold-load budget, not a 25s cap",
      _stream._client.timeout.read == 180.0)

_t0 = _time.time()
_got, _err = [], ""
try:
    for _c in _stream:
        _got.append(_c.choices[0].delta.content)
except Exception as e:
    _err = f"{type(e).__name__}: {e}"
_elapsed = _time.time() - _t0

check("chunks that do arrive are yielded", _got and _got[0] == "hi")
check("…and a stalled generation then fails fast instead of looking frozen",
      0.5 < _elapsed < 5.0)
check("…as an honest timeout, not a silently empty answer",
      "ReadTimeout" in _err and "no output" in _err)

_stream.close()

# Barge-in closes a stream mid-response. At that moment the reader thread is
# parked in a socket read, so closing inline would wait on the very connection
# that is stuck — the caller would freeze on the interrupt meant to unfreeze it.
_sock2 = socket.socket()
_sock2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
_sock2.bind(("127.0.0.1", 0))
_sock2.listen(1)
providers.OLLAMA_URL = f"http://127.0.0.1:{_sock2.getsockname()[1]}"
threading.Thread(target=_stalling_server, args=(_sock2,), daemon=True).start()

_stream2 = providers._OllamaStream(
    {"model": "x", "messages": [], "stream": True}, timeout=180.0, stall_timeout=60.0)
for _c in _stream2:
    break                      # barge-in: stop reading while the socket is live
_t1 = _time.time()
_stream2.close()
check("closing a live stream mid-response returns immediately",
      _time.time() - _t1 < 1.0)

try:
    _sock2.close()
except Exception:
    pass
providers.OLLAMA_URL = _orig_stream_url
try:
    _sock.close()
except Exception:
    pass


print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
