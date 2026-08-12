"""tests/test_single_call.py — the merged tool + conversation call.

ask_streaming used to be conversation-only, with a separate non-streamed
"probe" call in front of it deciding whether a tool was needed. That was two
round trips on every message, and the probe's answer was thrown away. Now one
streamed call carries the tool schemas and either answers or calls a tool.

What must hold, and what these pin:

  1. Plain conversation costs exactly ONE model call.
  2. A tool turn never leaks the model's preamble. If it says "Sure, opening
     that!" and the tool then reports a failure, the user sees the failure and
     ONLY the failure. This is the honesty rule (README §5.3) — a preamble
     streamed ahead of a failed action is how it gets broken.
  3. ACTION tool results are spoken verbatim, and the loop stops there. No
     extra round for the model to re-narrate "Spotify isn't open" into
     "Playing your music!".
  4. Non-action tools get a follow-up round that narrates the result.
  5. Tool chains still work (tool → tool → answer), capped at MAX_TOOL_ROUNDS.
  6. Failure is honest: a crashed handler never becomes "Done."

Run with the venv python:  python tests/test_single_call.py
"""

import os
import sys
import types
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# core.llm imports core.voice transitively through nothing, but core.features
# can pull heavy optional deps — stub the ones that open hardware.
voice_stub = types.ModuleType("core.voice")
voice_stub.SPEED = 1.1
sys.modules.setdefault("core.voice", voice_stub)

from core import llm  # noqa: E402

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {desc}")
    else:
        FAIL += 1
        print(f"  ✗ {desc}")


# ── fake streaming responses ─────────────────────────────────────────────────
def text_chunk(s):
    return SimpleNamespace(choices=[SimpleNamespace(
        delta=SimpleNamespace(content=s, tool_calls=None))])


def tool_chunk(index, id=None, name=None, args=None, content=None):
    fn = SimpleNamespace(name=name, arguments=args)
    tc = SimpleNamespace(index=index, id=id, function=fn)
    return SimpleNamespace(choices=[SimpleNamespace(
        delta=SimpleNamespace(content=content, tool_calls=[tc]))])


class FakeStream:
    """One streamed completion: a list of chunks, plus a .close()."""

    def __init__(self, chunks):
        self._chunks = chunks
        self.closed = False

    def __iter__(self):
        return iter(self._chunks)

    def close(self):
        self.closed = True


def scripted(*streams):
    """chat_create replacement returning each stream in turn; counts calls."""
    state = {"i": 0}

    def fake(**kwargs):
        fake.kwargs.append(kwargs)
        item = streams[state["i"]]
        state["i"] += 1
        fake.calls = state["i"]
        if isinstance(item, BaseException):
            raise item
        return item

    fake.calls = 0
    fake.kwargs = []
    return fake


# Neutralise everything ask_streaming does around the model call.
llm.detect_action = lambda text: None
llm.get_memory = lambda q: ""
llm.get_facts_about = lambda who: ""
llm.format_memories_for_prompt = lambda: ""
llm.save_memory = lambda *a, **k: None
llm.extract_and_save_facts = lambda *a, **k: None
llm.intents._needs_web = lambda t: False
llm.intents._worth_extracting = lambda t: False
llm.features.HAS_KNOWLEDGE = False

SYSTEM = {"role": "system", "content": "You are Ted."}


def run(text, runtime=None, conversation=None):
    conv = conversation if conversation is not None else [dict(SYSTEM)]
    out = "".join(llm.ask_streaming(text, conv, tool_runtime=runtime))
    return out, conv


def runtime(dispatch, action_tools=(), schemas=None, failures=None):
    return llm.ToolRuntime(
        schemas=schemas if schemas is not None else [],
        dispatch=dispatch,
        action_tools=action_tools,
        on_failure=(failures.append if failures is not None else None),
    )


_orig_chat_create = llm.chat_create

# ═════════════════════════════════════════════════════════════════════════════
print("— conversation: one call, streamed straight through —")

llm.chat_create = scripted(FakeStream([text_chunk("Not much, "), text_chunk("you?")]))
out, conv = run("how are you", runtime(lambda n, a: "unused"))
check("reply is the streamed text", out == "Not much, you?")
check("conversation costs exactly one model call", llm.chat_create.calls == 1)
check("tool schemas rode along on that call", "tools" in llm.chat_create.kwargs[0])
check("exchange stored", conv[-2:] == [{"role": "user", "content": "how are you"},
                                       {"role": "assistant", "content": "Not much, you?"}])

llm.chat_create = scripted(FakeStream([text_chunk("Hi.")]))
out, _ = run("hey", runtime(lambda n, a: "unused"))
check("a reply shorter than the decide-buffer still gets flushed", out == "Hi.")

llm.chat_create = scripted(FakeStream([text_chunk("Fine.")]))
out, _ = run("hey", None)
check("no runtime → plain conversation still works", out == "Fine.")
check("…and sends no tools", "tools" not in llm.chat_create.kwargs[0])

print("\n— action tools: ground truth, spoken verbatim, loop stops —")

calls = []
llm.chat_create = scripted(FakeStream([
    tool_chunk(0, id="c1", name="open_app", args='{"name":'),
    tool_chunk(0, args=' "spotify"}'),
]))
out, conv = run("open spotify", runtime(
    lambda n, a: (calls.append((n, a)), "Opening Spotify.")[1],
    action_tools={"open_app"}))
check("action result is the reply, verbatim", out == "Opening Spotify.")
check("action turn takes ONE call — no re-narration round", llm.chat_create.calls == 1)
check("streamed argument fragments are reassembled",
      calls == [("open_app", {"name": "spotify"})])
check("action result is what gets remembered",
      conv[-1] == {"role": "assistant", "content": "Opening Spotify."})

fails = []
llm.chat_create = scripted(FakeStream([
    tool_chunk(0, id="c1", name="open_app", args='{"name": "spotify"}',
               content="Sure, opening that for you! "),
    text_chunk("Enjoy the music!"),
]))
out, _ = run("open spotify", runtime(
    lambda n, a: "Spotify isn't open.", action_tools={"open_app"}, failures=fails))
check("THE HONESTY RULE: preamble is discarded, only ground truth reaches the user",
      out == "Spotify isn't open.")
check("no cheerful lie survives anywhere in the reply",
      "opening that for you" not in out and "Enjoy" not in out)
check("the failure was handed to the HUD hook", fails == ["Spotify isn't open."])

llm.chat_create = scripted(FakeStream([
    tool_chunk(0, id="c1", name="open_app", args='{"name": "spotify"}'),
    tool_chunk(1, id="c2", name="close_app", args='{"name": "mail"}'),
]))
out, _ = run("open spotify and close mail", runtime(
    lambda n, a: f"{n} ok.", action_tools={"open_app", "close_app"}))
check("parallel action calls are all run, in index order",
      out == "open_app ok. close_app ok.")

print("\n— non-action tools: one follow-up round narrates the result —")

llm.chat_create = scripted(
    FakeStream([tool_chunk(0, id="c1", name="get_weather", args="{}")]),
    FakeStream([text_chunk("It's sixty five and clear out.")]),
)
out, _ = run("what's it like outside", runtime(lambda n, a: "65F clear",
                                               action_tools={"open_app"}))
check("non-action tool result is narrated", out == "It's sixty five and clear out.")
check("that costs two calls", llm.chat_create.calls == 2)

llm.chat_create = scripted(
    FakeStream([tool_chunk(0, id="c1", name="get_weather", args="{}")]),
    FakeStream([tool_chunk(0, id="c2", name="calculate", args='{"expression": "1+1"}')]),
    FakeStream([text_chunk("Two.")]),
)
out, _ = run("chain", runtime(lambda n, a: f"{n} result", action_tools=set()))
check("tool chains keep going until the model answers", out == "Two.")
check("chain used three calls", llm.chat_create.calls == 3)

llm.chat_create = scripted(
    FakeStream([tool_chunk(0, id="c1", name="get_weather", args="{}")]),
    FakeStream([tool_chunk(0, id="c2", name="get_weather", args="{}")]),
    FakeStream([tool_chunk(0, id="c3", name="get_weather", args="{}")]),
)
out, _ = run("loop forever", runtime(lambda n, a: "tool said this",
                                     action_tools=set()))
check("a runaway chain stops at MAX_TOOL_ROUNDS", llm.chat_create.calls <= llm.MAX_TOOL_ROUNDS)
check("…and still says the last real result rather than nothing",
      out == "tool said this")

print("\n— failure is honest —")

llm.chat_create = scripted(FakeStream([
    tool_chunk(0, id="c1", name="set_timer", args='{"duration": "5m"}'),
]))
out, _ = run("set a timer", runtime(lambda n, a: None, action_tools={"set_timer"}))
check("a handler returning None never becomes 'Done.'",
      out == "That didn't go through — something failed on my end.")

llm.chat_create = scripted(FakeStream([
    tool_chunk(0, id="c1", name="open_app", args='{"name": nonsense'),
]))
out, _ = run("open something", runtime(lambda n, a: f"got {a}",
                                       action_tools={"open_app"}))
check("unparseable tool arguments degrade to empty args, not a crash",
      out == "got {}")


class Boom(FakeStream):
    def __iter__(self):
        raise RuntimeError("stream died")


llm.chat_create = scripted(Boom([]))
out, _ = run("hello", runtime(lambda n, a: "x"))
check("a stream that dies before any text says so honestly",
      out == "Something cut out — ask me again.")

llm.chat_create = _orig_chat_create

print("\n" + "=" * 50)
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
