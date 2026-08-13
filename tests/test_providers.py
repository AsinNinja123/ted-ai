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

print("\n" + "=" * 50)
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
