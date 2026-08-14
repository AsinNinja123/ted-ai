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
llm.error_log = SimpleNamespace(error=lambda *a, **k: None)
llm.intents._needs_web = lambda t: False
llm.intents._worth_extracting = lambda t: False
llm.features.HAS_KNOWLEDGE = False

SYSTEM = {"role": "system", "content": "You are Ted."}


def run(text, runtime=None, conversation=None):
    conv = conversation if conversation is not None else [dict(SYSTEM)]
    out = "".join(llm.ask_streaming(text, conv, tool_runtime=runtime))
    return out, conv


def runtime(dispatch, action_tools=(), schemas=None, failures=None, is_failure=None):
    if schemas is None:
        specs = {
            "open_app": ({"name": {"type": "string"}}, ["name"]),
            "close_app": ({"name": {"type": "string"}}, ["name"]),
            "get_weather": ({}, []),
            "calculate": ({"expression": {"type": "string"}}, ["expression"]),
            "set_timer": ({"duration": {"type": "string"}}, ["duration"]),
            "screen_describe": ({"question": {"type": "string"}}, []),
            "type_text": ({"text": {"type": "string"}}, ["text"]),
            "web_search": ({"query": {"type": "string"}}, ["query"]),
        }
        schemas = [{
            "type": "function",
            "function": {
                "name": name,
                "description": name,
                "parameters": {"type": "object", "properties": props,
                               "required": required, "additionalProperties": False},
            },
        } for name, (props, required) in specs.items()]
    return llm.ToolRuntime(
        schemas=schemas,
        dispatch=dispatch,
        action_tools=action_tools,
        on_failure=(failures.append if failures is not None else None),
        is_failure=is_failure,
    )


_orig_chat_create = llm.chat_create

# ═════════════════════════════════════════════════════════════════════════════
print("— conversation: one call, streamed straight through —")

escaped = r'{\n  "facts": [{"subject": "Charlie", "relationship": "LIKES", "object": "golf"}]\n}'
check("fact parser accepts Groq's escaped-whitespace JSON",
      llm._parse_fact_payload(escaped) == [{"subject": "Charlie",
                                            "relationship": "LIKES",
                                            "object": "golf"}])

llm.chat_create = scripted(FakeStream([text_chunk("Not much, "), text_chunk("you?")]))
out, conv = run("how are you", runtime(lambda n, a: "unused"))
check("reply is the streamed text", out == "Not much, you?")
check("conversation costs exactly one model call", llm.chat_create.calls == 1)
check("tool schemas rode along on that call", "tools" in llm.chat_create.kwargs[0])
check("short single-clause turns use low-latency reasoning",
      llm.chat_create.kwargs[0]["reasoning_effort"] == "none")
check("exchange stored", conv[-2:] == [{"role": "user", "content": "how are you"},
                                       {"role": "assistant", "content": "Not much, you?"}])

llm.chat_create = scripted(FakeStream([text_chunk("Hi.")]))
out, _ = run("hey", runtime(lambda n, a: "unused"))
check("a reply shorter than the decide-buffer still gets flushed", out == "Hi.")

llm.chat_create = scripted(FakeStream([text_chunk("Fine.")]))
out, _ = run("hey", None)
check("no runtime → plain conversation still works", out == "Fine.")
check("…and sends no tools", "tools" not in llm.chat_create.kwargs[0])

llm.chat_create = scripted(FakeStream([text_chunk("A plan.")]))
out, _ = run("analyze this problem and then plan the safest solution",
             runtime(lambda n, a: "unused"))
check("multi-step analytical turns retain full reasoning",
      out == "A plan." and llm.chat_create.kwargs[0]["reasoning_effort"] == "default")

# Closing an interrupted stream used to yield from ask_streaming's finally
# block, which raises RuntimeError("generator ignored GeneratorExit").
stream = FakeStream([text_chunk("x" * 60), text_chunk("never consumed")])
llm.chat_create = scripted(stream)
conv = [dict(SYSTEM)]
gen = llm.ask_streaming("long answer", conv, tool_runtime=runtime(lambda n, a: "unused"))
next(gen)
closed_cleanly = True
try:
    gen.close()
except RuntimeError:
    closed_cleanly = False
check("interrupting a stream closes the generator cleanly", closed_cleanly)
check("an interrupted partial reply is not stored as a complete exchange", conv == [SYSTEM])
check("closing the generator closes the provider stream", stream.closed)

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

CHAINED_ACTIONS = []
llm.chat_create = scripted(
    FakeStream([tool_chunk(0, id="c1", name="open_app", args='{"name":"Notes"}')]),
    FakeStream([tool_chunk(0, id="c2", name="type_text", args='{"text":"buy milk"}')]),
    FakeStream([text_chunk("Done.")]),
)
out, _ = run("open Notes and then type buy milk", runtime(
    lambda n, a: (CHAINED_ACTIONS.append((n, a)),
                  "Notes opened." if n == "open_app" else "Text typed.")[1],
    action_tools={"open_app", "type_text"},
))
check("dependent action tools continue across model rounds",
      CHAINED_ACTIONS == [("open_app", {"name": "Notes"}),
                          ("type_text", {"text": "buy milk"})])
check("dependent action chain reports only verified handler results",
      out == "Notes opened. Text typed.")

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

REPAIRED = []
llm.chat_create = scripted(
    FakeStream([tool_chunk(0, id="c1", name="open_app", args='{"name": nonsense')]),
    FakeStream([tool_chunk(0, id="c2", name="open_app", args='{"name":"Notes"}')]),
)
out, _ = run("open something", runtime(
    lambda n, a: (REPAIRED.append(a), f"got {a}")[1], action_tools={"open_app"}))
check("malformed arguments are rejected, fed back, and repaired before execution",
      out == "got {'name': 'Notes'}" and REPAIRED == [{"name": "Notes"}])
check("the repair feedback says that the malformed action did not run",
      "nothing ran" in llm.chat_create.kwargs[1]["messages"][-1]["content"])


class Boom(FakeStream):
    def __iter__(self):
        raise RuntimeError("stream died")


llm.chat_create = scripted(Boom([]))
out, _ = run("hello", runtime(lambda n, a: "x"))
check("a stream that dies before any text says so honestly",
      out == "Something cut out — ask me again.")

print("\n— never end a turn silent —")

# Regression: "what's on my screen" ran the vision tool, the model then wrote
# nothing, and _respond turned the empty stream into a rotated "didn't quite
# catch that" — blaming the user for a tool that had actually run.
llm.chat_create = scripted(
    FakeStream([tool_chunk(0, id="c1", name="screen_describe", args="{}")]),
    FakeStream([]),                       # model returns nothing at all
)
out, _ = run("what's on my screen", runtime(lambda n, a: "Your editor is open.",
                                            action_tools=set()))
check("a silent model still says what the tool found", out == "Your editor is open.")

llm.chat_create = scripted(FakeStream([]))
out, _ = run("hello", runtime(lambda n, a: "x"))
check("nothing at all is admitted honestly, not silently",
      out == "Something cut out — ask me again.")

# Regression: gpt-oss re-calls a tool after seeing its result. For a slow tool
# (screenshot + vision call) three rounds of that is a minute of dead air and
# two wasted screenshots.
SHOTS = []
llm.chat_create = scripted(
    FakeStream([tool_chunk(0, id="c1", name="screen_describe", args="{}")]),
    FakeStream([tool_chunk(0, id="c2", name="screen_describe", args="{}")]),
    FakeStream([text_chunk("never reached")]),
)
out, _ = run("what's on my screen", runtime(
    lambda n, a: (SHOTS.append(n), "Your editor is open.")[1], action_tools=set()))
check("a repeated identical tool call is refused, not re-run", len(SHOTS) == 1)
check("…and the turn still answers from the first result",
      out == "Your editor is open.")

print("\n— what Ted said is what Ted remembers saying —")

# Regression: a preamble long enough to commit (>_TOOL_DECIDE_CHARS) streams to
# the HUD and the speaker, then a tool call arrives. That text was being dropped
# from full_reply, so the stored turn — and therefore memory and the chat
# transcript — was missing a sentence the user had already seen.
_stored = {}
_orig_remember = llm._remember_exchange
llm._remember_exchange = lambda u, r, c: _stored.__setitem__("reply", r)

preamble = "Let me pull that up for you, one moment while I check. "
assert len(preamble) > llm._TOOL_DECIDE_CHARS
llm.chat_create = scripted(
    FakeStream([text_chunk(preamble),
                tool_chunk(0, id="c1", name="get_weather", args="{}")]),
    FakeStream([text_chunk("It is 71 and clear.")]),
)
out, _ = run("what's the weather", runtime(lambda n, a: "71F clear",
                                          action_tools=set()))
check("a committed preamble still reaches the user", preamble in out)
check("…and is stored in the turn, not silently dropped",
      preamble in _stored.get("reply", ""))
check("…alongside the narrated result",
      "It is 71 and clear." in _stored.get("reply", ""))

# The honesty rule still wins: a SHORT preamble ahead of a failed action is
# discarded, so the user sees only the failure. This is the invariant the fix
# above must not have loosened.
_stored.clear()
llm.chat_create = scripted(
    FakeStream([text_chunk("Sure! "),
                tool_chunk(0, id="c1", name="open_app", args='{"name": "Spotify"}')]),
)
out, _ = run("open spotify", runtime(lambda n, a: "Spotify isn't installed.",
                                     action_tools={"open_app"}))
check("a short preamble before a failed action is still suppressed",
      out == "Spotify isn't installed.")
check("…and never enters the stored turn either",
      _stored.get("reply", "") == "Spotify isn't installed.")

llm._remember_exchange = _orig_remember

print("\n— the health dot does not cry wolf —")

# Regression: groq_ok() gated on active_provider() == "groq", but that is
# "none" until the first completion of the session — so the HUD lit up a Groq
# outage at boot, before Groq had been asked for anything.
_orig_active = llm.providers.active_provider
llm._GROQ_OK = True
llm.providers.active_provider = lambda: "none"
check("a fresh session is not reported as a Groq outage", llm.groq_ok())
llm.providers.active_provider = lambda: "groq"
check("a served cloud turn reports healthy", llm.groq_ok())
llm.providers.active_provider = lambda: "ollama"
check("a real fall back to the local brain reports Groq down", not llm.groq_ok())
llm.providers.active_provider = _orig_active

print("\n— a claimed action with no tool call is corrected, not shipped —")

# Ted said "Closed VS Code and Notes." having called no tool at all, then a
# minute later insisted it had no way to close apps — while close_app was in
# its menu the whole time, and close_app itself verifies before confirming.
# The lie was upstream of every safeguard: the model narrated the outcome
# instead of acting, and text with no tool call streams straight through.

check("a past-tense action claim is recognised",
      llm.claims_completed_action("Closed VS Code and Notes."))
check("...as is a first-person one", llm.claims_completed_action("I've sent it."))
check("an inability is not a claim",
      not llm.claims_completed_action("I can't close apps, so I couldn't."))
check("an intention is not a claim",
      not llm.claims_completed_action("Opening VS Code now..."))
check("a question is not a claim",
      not llm.claims_completed_action("Want me to close VS Code?"))
check("an action the USER took is not Ted's claim",
      not llm.claims_completed_action(
          "You closed that tab yourself a minute ago, so it should be gone."))

llm.chat_create = scripted(FakeStream([text_chunk("Closed VS Code and Notes.")]))
out, _ = run("close vs code and notes", runtime(lambda n, a: "unused"))
check("the user is told nothing actually ran", "didn't actually run" in out)
check("...while still seeing what the model said", "Closed VS Code" in out)

llm.chat_create = scripted(FakeStream([text_chunk(
    "You closed that tab yourself a minute ago, so it should be gone.")]))
out, _ = run("did I close it?", runtime(lambda n, a: "unused"))
check("ordinary conversation gets no disclaimer",
      "didn't actually run" not in out)

llm.chat_create = _orig_chat_create

print("\n" + "=" * 50)
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
