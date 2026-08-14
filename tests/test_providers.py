"""Provider routing and native Ollama request translation.

No network calls are made here. Run with: venv/bin/python tests/test_providers.py
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import providers

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
print("\n— an unsupported SDK argument must not take the cloud offline —")

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
                if "stream_options" in kw:
                    raise TypeError(
                        "Completions.create() got an unexpected keyword "
                        "argument 'stream_options'")
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
check("…the retry dropped only the unsupported argument",
      "stream_options" in seen[0] and "stream_options" not in seen[1])
check("…and the capability is remembered, not re-probed every turn",
      providers._USAGE_SUPPORTED[0] is False)

seen.clear()
providers.chat_create(messages=[], stream=True)
check("the next call does not send it again", "stream_options" not in seen[0])

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


print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
