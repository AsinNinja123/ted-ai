"""Pinning a brain, and the warm/cold distinction the HUD needs.

The picker is only worth having if "Local" means local. A forced mode that
quietly fell back would report one model's behaviour under the other's name —
which is the failure this project keeps having to fix.

No model is called and the live data/runtime.json is never touched: _MODE_PATH
is redirected at a scratch file first.

Run with:  ~/ted-ai/venv/bin/python tests/test_provider_pin.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import providers  # noqa: E402

providers._MODE_PATH = os.path.join(tempfile.mkdtemp(), "runtime.json")
providers._mode_cache["mtime"] = -1.0

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {desc}")
    else:
        FAIL += 1
        print(f"  ✗ {desc}")


print("\n— the setting —")
check("defaults to auto with no file at all", providers.get_provider_mode() == "auto")
for mode in ("luna", "cloud", "local", "auto"):
    providers.set_provider_mode(mode)
    check(f"'{mode}' is accepted and read back", providers.get_provider_mode() == mode)

providers.set_provider_mode("local")
# Survives a restart because it never lived in the window: a fresh read of the
# file, with the cache invalidated, is what Ted's next process actually does.
providers._mode_cache["mtime"] = -1.0
check("the choice survives a restart", providers.get_provider_mode() == "local")

raised = False
try:
    providers.set_provider_mode("gpt5")
except ValueError:
    raised = True
check("an unknown mode is rejected", raised)
check("and the previous mode is untouched", providers.get_provider_mode() == "local")

# A corrupt file must not take Ted down with it — auto is the safe reading.
with open(providers._MODE_PATH, "w", encoding="utf-8") as fh:
    fh.write("{not json")
providers._mode_cache["mtime"] = -1.0
check("a corrupt settings file falls back to auto",
      providers.get_provider_mode() == "auto")
providers.set_provider_mode("auto")

print("\n— pinned means pinned —")
calls = []


def fake_ollama(**kwargs):
    calls.append("ollama")
    return "local-answer"


real_ollama = providers._ollama_create
real_groq = providers._groq
real_openai = providers._openai
providers._ollama_create = fake_ollama


class ExplodingGroq:
    """Any cloud attempt at all is a failure of the contract, so make it loud."""
    class chat:
        class completions:
            @staticmethod
            def create(**kwargs):
                raise AssertionError("cloud was called while pinned to local")


providers._groq = ExplodingGroq
providers._openai = ExplodingGroq

providers.set_provider_mode("local")
result = providers.chat_create(messages=[{"role": "user", "content": "hi"}])
check("local mode answers from the local brain", result == "local-answer")
check("and never touches the cloud", calls == ["ollama"])
check("active_provider reports the local brain",
      providers.active_provider() == "ollama")

print("\n— Luna is independently pinnable —")


class FakeLuna:
    class chat:
        class completions:
            @staticmethod
            def create(**kwargs):
                calls.append(("luna", kwargs))
                return "luna-answer"


providers._openai = FakeLuna
providers.set_provider_mode("luna")
result = providers.chat_create(messages=[{"role": "user", "content": "hi"}],
                               max_tokens=80)
check("Luna pin answers from Luna", result == "luna-answer")
check("Luna receives its configured model and translated token limit",
      calls[-1][1].get("model") == providers.PRIMARY_CHAT_MODEL
      and calls[-1][1].get("max_completion_tokens") == 80
      and "max_tokens" not in calls[-1][1])
providers.chat_create(messages=[{"role": "user", "content": "open Terminal"}],
                      tools=[{"type": "function", "function": {"name": "open_app"}}],
                      reasoning_effort="default")
check("Luna tool calls disable reasoning on Chat Completions",
      calls[-1][1].get("reasoning_effort") == "none")
check("active_provider reports OpenAI", providers.active_provider() == "openai")
providers.set_provider_mode("auto")
result = providers.chat_create(messages=[{"role": "user", "content": "foreground"}])
check("auto prefers Luna for foreground turns when configured",
      result == "luna-answer" and providers.active_provider() == "openai")

# Background work is sent local under auto to protect the cloud allowance,
# but an explicit cloud pin must mean cloud even for helpers.
calls.clear()
providers.set_provider_mode("auto")
providers.chat_create(messages=[{"role": "user", "content": "title this"}],
                      _ted_workload="background")
check("auto keeps background helpers off the cloud allowance", calls == ["ollama"])

providers._ollama_create = real_ollama
providers._groq = real_groq
providers._openai = real_openai
providers.set_provider_mode("auto")

print("\n— pulled is not loaded —")
import httpx  # noqa: E402

real_get = httpx.get


class FakeRes:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


def stub_http(payload):
    httpx.get = lambda url, **kw: FakeRes(payload)


stub_http({"models": [{"name": providers.LOCAL_CHAT_MODEL},
                      {"name": providers.LOCAL_TOOL_MODEL}]})
check("both models pulled reads as ready", providers.local_model_ready() is True)

stub_http({"models": []})
check("nothing loaded reads as cold", providers.local_model_warm() is False)
check("and nothing pulled reads as not ready", providers.local_model_ready() is False)

stub_http({"models": [{"name": providers.LOCAL_TOOL_MODEL}]})
check("a resident model reads as warm", providers.local_model_warm() is True)


def boom(url, **kw):
    raise RuntimeError("ollama is not running")


httpx.get = boom
check("ollama being down is not warm", providers.local_model_warm() is False)
check("ollama being down is not ready", providers.local_model_ready() is False)
httpx.get = real_get

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
