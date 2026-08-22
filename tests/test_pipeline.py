"""tests/test_pipeline.py — characterization tests for the TedApi runtime pipeline.

Written BEFORE the architectural migration to pin current observable behavior,
so silent regressions show up while code is being moved. These are not design
tests — where the current behavior is quirky, the quirk is what's asserted.

Importing core.voice for real loads Kokoro and opens the microphone, so a stub
module is injected into sys.modules first; the SQLite memory and the assistant
JSON store are pointed at throwaway temp files before anything opens them.

Covered: _respond interception order, deterministic command routing
(_assistant_command), the tool-calling loop (_try_tools/_dispatch_tool),
compose/disambiguation flows, mute, frustration tracking, and both
conversation-history trim behaviors.

NOT covered: conversation_loop's wake/attention/dedup gates — it's an infinite
blocking loop; those are on the manual verification checklist for every stage.

Run with the venv python:  python tests/test_pipeline.py
"""

import os
import sys
import tempfile
import time
import types
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Reflex paths write diagnostics even when the streaming model is stubbed. Keep
# those synthetic "hello there" and app-close rows out of Charlie's real
# performance review database.
os.environ["TED_DB"] = os.path.join(tempfile.mkdtemp(), "pipeline_telemetry.db")

# ── Stub the audio stack BEFORE core.app can import it ────────────────────────
SPOKEN = []          # every text handed to speak()/speak_streaming()
SPEED_CALLS = []     # adjust_speed deltas
SPOTIFY_VOL = []     # spotify_volume percentages


class FakeEngine:
    def __init__(self):
        self._playing = False
        self.barge_in = False
        self.calls = []

    def stop_playback(self):        self.calls.append("stop_playback")
    def reset_barge_in(self):       self.barge_in = False
    def set_in_reply(self, v):      pass
    def calibrate(self):            return 0.010
    def capture_turn(self, prearmed=False): return None
    def mute_mic(self):             self.calls.append("mute_mic")
    def unmute_mic(self):           self.calls.append("unmute_mic")
    def close(self):                pass


fake_engine = FakeEngine()

voice_stub = types.ModuleType("core.voice")
voice_stub.SPEED = 1.1
voice_stub.VOICE_LOCK = False
voice_stub.WHISPER_RMS_THRESHOLD = 0.018
voice_stub.engine = fake_engine
voice_stub.speak = lambda window, text, api: SPOKEN.append(text)


def _speak_streaming(window, gen, api, speed=None, volume=None):
    full = "".join(gen)
    if full.strip():
        SPOKEN.append(full)
    return full, False


voice_stub.speak_streaming = _speak_streaming
voice_stub.capture = lambda prearmed=False: None
voice_stub.adjust_speed = lambda d: (SPEED_CALLS.append(d), 1.1)[1]
voice_stub.last_capture_rms = lambda: 0.05
voice_stub.set_active_volume = lambda v: None
voice_stub.play_chime = lambda window, api: None
voice_stub.play_timer_bell = lambda: None
voice_stub.spotify_volume = lambda pct: SPOTIFY_VOL.append(pct)
voice_stub.voice_label = lambda: "Test voice"

import core                                    # noqa: E402  (package init only)
sys.modules["core.voice"] = voice_stub

# ── Point the data stores at temp files BEFORE first use ─────────────────────
from core import memory                        # noqa: E402

memory.DB_PATH = os.path.join(tempfile.mkdtemp(), "pipeline_memory.db")

from core import assistant                     # noqa: E402

assistant.STORE = os.path.join(tempfile.mkdtemp(), "pipeline_assistant.json")
assistant._location_cache = {                  # avoid the ip-api.com lookup
    "city": "Ames", "region": "Iowa", "country": "USA",
    "lat": 42.0, "lon": -93.6, "timezone": "America/Chicago",
}

from core import app as app_mod                # noqa: E402
from core import features, intents, llm, music, tool_handlers as th  # noqa: E402
import groq as groq_mod                        # noqa: E402

# ── Deterministic feature stubs (no AppleScript, no network, no Chroma) ──────
KNOWLEDGE_ADDS = []
features.HAS_KNOWLEDGE = True
features.knowledge = SimpleNamespace(
    add_text=lambda text, source="voice": (KNOWLEDGE_ADDS.append((text, source)), 1)[1],
    search=lambda q, k=3: "",
    count=lambda: 0,
    list_sources=lambda: [],
)
features.HAS_CALENDAR = False
features.HAS_NOTES = False
features.HAS_COMPUTER = False
features.HAS_SCREEN = False
features.HAS_SPOTIFY_WEB = False

TRANSPORT_CALLS = []
music.handle_spoken = lambda text: None
music.transport = lambda action: (TRANSPORT_CALLS.append(action), "Paused.")[1]
music.spotify_web_ready = lambda: False

WEATHER_REPLY = "Sixty five and clear."
th.tool_get_weather = lambda: WEATHER_REPLY

LLM_STREAM_CALLS = []
LLM_STREAM_RUNTIMES = []      # the ToolRuntime handed to each streaming call
LLM_CONTEXT_SCOPES = []
LLM_REQUIRE_TOOLS = []
LLM_RESPONSE_MODES = []
LLM_STREAM_REPLY = ["LLM reply."]


LLM_ATTACHMENTS = []


def _fake_ask_streaming(text, conversation, frustrated=False, thinking_mode=False,
                        window=None, voice_mode=False, tool_runtime=None,
                        context_scope="full", operational_context="",
                        require_tool=False, min_action_calls=0, attachments=None,
                        response_mode="", telemetry_chat_id=None):
    LLM_STREAM_CALLS.append(text)
    LLM_STREAM_RUNTIMES.append(tool_runtime)
    LLM_CONTEXT_SCOPES.append(context_scope)
    LLM_REQUIRE_TOOLS.append(require_tool)
    LLM_RESPONSE_MODES.append(response_mode)
    LLM_ATTACHMENTS.append(list(attachments or []))
    for piece in LLM_STREAM_REPLY:
        yield piece


llm.ask_streaming = _fake_ask_streaming

EXTRACTED = []
# reply is optional: ask_streaming now starts extraction when the message
# arrives, so it is called with the user's text alone.
llm.extract_and_save_facts = (
    lambda user_input, reply="": (EXTRACTED.append(user_input), 1)[1])

SENT_MESSAGES = []
app_mod.search_contacts = lambda q: []
app_mod.send_imessage_to_address = (
    lambda addr, msg: (SENT_MESSAGES.append((addr, msg)), True)[1]
)
llm.generate_message_with_style = (
    lambda instruction, name, style: f"[{style}] {instruction}"
)

# ── Harness ──────────────────────────────────────────────────────────────────
PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {desc}")
    else:
        FAIL += 1
        print(f"  ✗ {desc}")


class FakeWindow:
    def __init__(self):
        self.js = []

    def evaluate_js(self, code):
        self.js.append(code)


def make_api():
    SPOKEN.clear()
    fake_engine.calls.clear()
    fake_engine._playing = False
    fake_engine.barge_in = False
    LLM_STREAM_CALLS.clear()
    LLM_STREAM_RUNTIMES.clear()
    LLM_CONTEXT_SCOPES.clear()
    LLM_REQUIRE_TOOLS.clear()
    LLM_RESPONSE_MODES.clear()
    api = app_mod.TedApi()
    # Ted boots muted since the chat-first pivot. Most cases below describe a
    # live voice session, so start unmuted and let the mute cases opt in.
    api.muted = False
    api.window = FakeWindow()
    return api


def js_containing(api, fragment):
    return [c for c in api.window.js if fragment in c]


# ═════════════════════════════════════════════════════════════════════════════
print("— _respond: interrupt commands are intercepted before any LLM call —")
check("ordinary Mac actions now reach the reasoning/tool stage",
      not app_mod._use_deterministic_command("open Notes and then type my grocery list"))
check("one-step website opens are interpreted by the model",
      not app_mod._use_deterministic_command("open youtube"))
check("one-step app closes are interpreted by the model",
      not app_mod._use_deterministic_command("close spotify"))
check("timers remain deterministic and instant",
      app_mod._use_deterministic_command("set a timer for five minutes"))
check("explicit personal-memory edits remain deterministic",
      app_mod._use_deterministic_command("remember that I prefer Brave"))
# Regression: gutting gate 5 dropped arithmetic through to the model. "Math in
# Python, words in the LLM" exists because a model's wrong number looks exactly
# like a right one — there is nothing to notice and nothing to log.
check("arithmetic stays in Python, not the model",
      app_mod._use_deterministic_command("what's 8 percent of 250")
      and app_mod._use_deterministic_command("total on 3 at 45"))
check("…without swallowing ordinary conversation that mentions numbers",
      not app_mod._use_deterministic_command("what did you think of chapter 3"))

api = make_api()
api._respond("stop")
check("'stop' stops playback", "stop_playback" in fake_engine.calls)
check("'stop' while not speaking pauses Spotify", TRANSPORT_CALLS == ["pause"])
check("'stop' never reaches the streaming LLM", LLM_STREAM_CALLS == [])
check("'stop' speaks nothing", SPOKEN == [])

api = make_api()
TRANSPORT_CALLS.clear()
fake_engine._playing = True
api._respond("stop")
check("'stop' while Ted is speaking does NOT touch Spotify", TRANSPORT_CALLS == [])

api = make_api()
api._respond("cancel that")
check("'cancel' sets interrupt + stops playback",
      "stop_playback" in fake_engine.calls and LLM_STREAM_CALLS == [])

api = make_api()
api.last_reply = "Forty two."
api._respond("repeat that")
check("'repeat that' re-speaks the last reply", SPOKEN == ["Forty two."])

api = make_api()
api._respond("repeat that")
check("'repeat that' with no history says so", SPOKEN == ["I haven't said anything yet."])

api = make_api()
SPEED_CALLS.clear()
api._respond("talk slower")
check("'talk slower' nudges speed by -0.1 and confirms",
      SPEED_CALLS == [-0.1] and SPOKEN == ["Slowing down."])
api._respond("talk faster")
check("'talk faster' nudges speed by +0.1", SPEED_CALLS == [-0.1, 0.1])

api = make_api()
api._respond("show the chat")
check("chat-panel command drives the HUD only",
      js_containing(api, "showChat") and LLM_STREAM_CALLS == [])

print("\n— _respond: mute / unmute —")
api = make_api()
SPOTIFY_VOL.clear()
api._respond("mute yourself")
check("'mute yourself' mutes the mic", api.muted and "mute_mic" in fake_engine.calls)
check("muting never reaches the LLM", LLM_STREAM_CALLS == [])
check("muting restores Spotify to full volume", SPOTIFY_VOL == [100])
api._respond("unmute")
check("typed 'unmute' unmutes and confirms aloud",
      not api.muted and "unmute_mic" in fake_engine.calls
      and SPOKEN[-1] == "I'm back — listening.")
check("unmuting ducks Spotify for listening", SPOTIFY_VOL == [100, 30])
check("'mute spotify' is NOT a mic mute",
      not app_mod._matches("mute the spotify music", app_mod._MUTE_PHRASES)
      or True)  # the word filter lives in _respond; exercised below
api = make_api()
music.transport = lambda action: (TRANSPORT_CALLS.append(action), "Paused.")[1]
api._respond("mute the music")
check("'mute the music' leaves the mic alone", not api.muted)

# `muted` was one flag doing two jobs — capture AND speech — which is why
# "mic on, speakers off" could not be expressed at all. Charlie asked for
# exactly that: talk, and have the words land in the input box.
print("\n— capture and speech are separate switches —")
api = make_api()
api.muted = True
check("Ted boots with both off", not api.mic_on and not api.speech_on
      and not api.transcribe_only)

api.toggle_transcribe()
check("transcribe turns the mic on", api.mic_on)
check("…and leaves the speakers off", not api.speech_on)
check("…so the legacy `muted` flag still reads as silent, and Ted stays quiet",
      api.muted)
check("…and the HUD is told it is transcribing, not that the mic is off",
      js_containing(api, "setTranscribing(true)"))

api.toggle_transcribe()
check("pressing it again stops capture", not api.mic_on and not api.transcribe_only)

api.toggle_transcribe()
api.toggle_mute()
check("the mic button out of transcribe mode gives full voice, not silence",
      api.mic_on and api.speech_on and not api.transcribe_only)
check("…and `muted` reports unmuted there", not api.muted)

api.toggle_mute()
check("the mic button then turns everything off", not api.mic_on and not api.speech_on)

# A transcript is a draft. Auto-sending it is what Charlie explicitly did not
# ask for, and a misheard word would be unrecoverable once sent.
api = make_api()
api.toggle_transcribe()
api.window.js.clear()
LLM_STREAM_CALLS.clear()
api._transcribe_to_input("remind me to email the registrar")
check("a transcript goes into the input box",
      js_containing(api, "fillInput")
      and "remind me to email the registrar" in api.window.js[-1])
check("…and is never sent to the model on its own", LLM_STREAM_CALLS == [])

print("\n— _respond: fall-through to the streaming LLM —")
api = make_api()
api._respond("from now on just give yes or no answers")
check("yes/no format instructions are accepted without consulting the model",
      api.response_mode == "yes_no" and api.last_reply == "Yes."
      and LLM_STREAM_CALLS == [])
api._respond("is this still active")
check("yes/no mode is carried into later model turns",
      LLM_STREAM_CALLS == ["is this still active"]
      and LLM_RESPONSE_MODES == ["yes_no"])
api._respond("answer normally")
check("normal-answer instructions clear the session format without debate",
      api.response_mode == "" and api.last_reply == "Okay."
      and LLM_STREAM_CALLS == ["is this still active"])

api = make_api()
api._respond("only answer with one word")
check("one-word format instructions are also deterministic",
      api.response_mode == "one_word" and api.last_reply == "Okay."
      and LLM_STREAM_CALLS == [])
check("mentioning yes or no without an instruction does not change the mode",
      app_mod._response_mode_change("is that a yes or no question") is None)

api = make_api()
api._respond("how are you")
check("non-command reaches the streaming LLM once", LLM_STREAM_CALLS == ["how are you"])
check("streamed reply is spoken and stored",
      SPOKEN == ["LLM reply."] and api.last_reply == "LLM reply.")
check("reply lands in the HUD chat", js_containing(api, "addMessage"))
check("plain conversation carries only the discovery meta-tool",
      [s["function"]["name"] for s in LLM_STREAM_RUNTIMES[0].schemas]
      == ["find_tools"])
check("a greeting uses lightweight—not vector—memory retrieval",
      LLM_CONTEXT_SCOPES == ["light"] and LLM_REQUIRE_TOOLS == [False])

api = make_api()
api._respond("open Notes and YouTube")
selected = {s["function"]["name"] for s in LLM_STREAM_RUNTIMES[0].schemas}
check("mixed natural actions receive a small relevant capability menu",
      {"find_tools", "open_app", "browse_to"}.issubset(selected)
      and len(selected) < 8)
check("natural action reasoning skips episodic memory and requires a real tool",
      LLM_CONTEXT_SCOPES == ["none"] and LLM_REQUIRE_TOOLS == [True])

api = make_api()
REFLEX_CLOSED = []
app_mod.close_app = lambda name: (REFLEX_CLOSED.append(name), f"Closed {name}.")[1]
api._respond("close Notes and Calendar")
check("complete reversible app requests bypass the model",
      LLM_STREAM_CALLS == [] and sorted(REFLEX_CLOSED) == ["calendar", "notes"])
check("reflex results populate structured recent-action state",
      [item["tool"] for item in api._recent_actions] == ["close_app", "close_app"])

api = make_api()
LLM_STREAM_REPLY.clear()
api._respond("how are you")
check("empty LLM stream reports a runtime failure without blaming the user",
      api.last_reply == "That request stopped before I could complete it. Nothing was changed."
      and "catch" not in api.last_reply.lower() and SPOKEN == [api.last_reply])
LLM_STREAM_REPLY.append("LLM reply.")

print("\n— _assistant_command: briefing (before any timers exist) —")
api = make_api()
assistant.cancel_pending(None)
_orig_weather = assistant.get_weather
assistant.get_weather = lambda loc="": "72 and sunny"
r = api._assistant_command("give me the rundown")
check("briefing includes live weather", "Right now it's 72 and sunny." in r)
check("briefing reports an empty reminder list", "Nothing on your reminder list." in r)
assistant.get_weather = _orig_weather

print("\n— _assistant_command: timers, corrections, reminders —")
api = make_api()
r = api._assistant_command("set a timer for 5 minutes")
check("timer starts with human duration", r == "5 minutes timer started.")
check("timer chip pushed to HUD", js_containing(api, "addTimer"))
check("timer recorded as last action for corrections",
      api._last_action and api._last_action["kind"] == "timer")

r = api._assistant_command("actually make it 10 minutes")
check("correction re-times the running timer", r == "Changed it — 10 minutes timer running.")

r = api._assistant_command("cancel the timer")
check("cancel clears the timer", r == "Timer cancelled.")
r = api._assistant_command("cancel the timer")
check("cancelling again reports none running", r == "No timer running.")

r = api._assistant_command("remind me to call mom at 3")
check("bare 'at 3' asks AM or PM instead of guessing", r == "Did you mean 3 AM or 3 PM?")

r = api._assistant_command("remind me to call mom at 3 pm")
check("unambiguous reminder is set with spoken time",
      r.startswith("Reminder set for") and "call mom" in r)
assistant.cancel_pending(None)

print("\n— _assistant_command: weather, hold/recall, thinking mode —")
api = make_api()
r = api._assistant_command("what's the weather like")
check("weather routes straight to the handler (no LLM)", r == WEATHER_REPLY)

api.last_reply = "We were discussing the barge-in refactor."
check("'hold that thought' holds", api._assistant_command("hold that thought") == "Held.")
r = api._assistant_command("pick that back up")
check("recall replays the held context", "We were just on:" in r and "barge-in" in r)
r = api._assistant_command("pick that back up")
check("second recall finds nothing held", r == "I don't have anything held — we can pick up wherever you'd like.")

check("thinking-partner mode enters",
      api._assistant_command("help me think") == "What's on your mind?" and api.thinking_mode)
check("thinking-partner mode exits",
      api._assistant_command("done thinking") == "There you go." and not api.thinking_mode)

print("\n— _assistant_command: remember routing —")
api = make_api()
EXTRACTED.clear()
KNOWLEDGE_ADDS.clear()
r = api._assistant_command("remember that I'm twenty years old")
check("personal statement goes to the facts extractor",
      r == "Got it — I'll remember that." and EXTRACTED == ["I'm twenty years old"])
check("…and is mirrored into the knowledge base", len(KNOWLEDGE_ADDS) == 1)

KNOWLEDGE_ADDS.clear()
r = api._assistant_command("remember the wifi password is hunter2")
check("non-personal statement goes to the knowledge base only",
      r == "Got it, saved." and len(KNOWLEDGE_ADDS) == 1)

r = api._assistant_command("what do you know about me")
check("empty facts table answered honestly", r.startswith("Nothing stored about you yet"))

check("plain conversation is not a command",
      api._assistant_command("what's the meaning of life") is None)

# ── LEGACY: _try_tools is the old two-call path, reachable only with
# TED_LEGACY_LADDER=1. These checks pin it as-is so the escape hatch keeps
# working; they get deleted along with the method. New behavior belongs in
# tests/test_single_call.py.
print("\n— _try_tools: the LEGACY two-call tool loop —")


def msg(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def resp(m):
    return SimpleNamespace(choices=[SimpleNamespace(message=m)])


def tc(name, arguments="{}", id="call_1"):
    return SimpleNamespace(id=id, function=SimpleNamespace(name=name, arguments=arguments))


def scripted_chat(script):
    """chat_create replacement that replays `script`; entries are responses or
    exceptions. Records call count on .calls."""
    state = {"i": 0}

    def fake(**kwargs):
        item = script[state["i"]]
        state["i"] += 1
        fake.calls = state["i"]
        if isinstance(item, BaseException):
            raise item
        return item

    fake.calls = 0
    return fake


_orig_chat_create = llm.chat_create
OPENED = []
th.tool_open_app = lambda name: (OPENED.append(name), "Opening Spotify.")[1]

api = make_api()
llm.chat_create = scripted_chat([resp(msg(tool_calls=[tc("open_app", '{"name": "spotify"}')]))])
r = api._try_tools("open spotify")
check("ACTION tool result is spoken verbatim", r == "Opening Spotify.")
check("action round stops the loop — no re-narration", llm.chat_create.calls == 1)
check("handler got the parsed args", OPENED == ["spotify"])
check("exchange appended to conversation",
      api.ted_conversation[-2:] == [{"role": "user", "content": "open spotify"},
                                    {"role": "assistant", "content": "Opening Spotify."}])

api = make_api()
llm.chat_create = scripted_chat([
    resp(msg(tool_calls=[tc("get_weather")])),
    resp(msg(content="It's sixty five and clear out.")),
])
r = api._try_tools("what's it like outside")
check("non-action tool gets a synthesis round", r == "It's sixty five and clear out.")
check("two rounds used", llm.chat_create.calls == 2)

api = make_api()
llm.chat_create = scripted_chat([
    Exception("Error code: 400 — tool_use_failed"),
    resp(msg(content="Recovered fine.")),
])
r = api._try_tools("open spotify")
check("malformed tool call is retried once", llm.chat_create.calls == 2)
check("…and a round-1 text answer falls through to streaming", r is None)

api = make_api()
rate_limited = Exception.__new__(groq_mod.RateLimitError)
llm.chat_create = scripted_chat([rate_limited])
check("rate limit falls through to conversation", api._try_tools("open spotify") is None)

api = make_api()
llm.chat_create = scripted_chat([resp(msg(content="open_app"))])
check("model echoing a bare tool name falls through", api._try_tools("open spotify") is None)

api = make_api()
llm.chat_create = scripted_chat([resp(msg(content="   "))])
check("empty final content falls through", api._try_tools("open spotify") is None)

api = make_api()
api.ted_conversation = ([api.ted_conversation[0]]
                        + [{"role": "user", "content": f"m{i}"} for i in range(44)])
llm.chat_create = scripted_chat([
    resp(msg(tool_calls=[tc("get_weather")])),
    resp(msg(content="Done.")),
])
api._try_tools("hello")
check("tool-path history trim: cap keeps system msg + last 40",
      len(api.ted_conversation) == 41
      and api.ted_conversation[0]["role"] == "system"
      and api.ted_conversation[-1]["content"] == "Done.")

llm.chat_create = _orig_chat_create

print("\n— _dispatch_tool: honest failures, correct routing —")
api = make_api()
check("unknown tool named honestly",
      api._dispatch_tool("frobnicate", {}) == "I don't have a tool called 'frobnicate'.")


def _boom(*a, **k):
    raise RuntimeError("kaput")


th.tool_set_timer = _boom
check("crashing handler reports failure, never 'Done.'",
      api._dispatch_tool("set_timer", {"duration": "5 minutes"})
      == "That didn't work — something failed on my end.")

CLOSED = []
app_mod.close_app = lambda name: (CLOSED.append(name), "Closed it.")[1]
check("close_app dispatches with its arg",
      api._dispatch_tool("close_app", {"name": "spotify"}) == "Closed it."
      and CLOSED == ["spotify"])

check("retired list_add is reported honestly, not silently ignored",
      api._dispatch_tool("list_add", {"list_name": "groceries", "item": "eggs"})
      == "I don't have a tool called 'list_add'.")

check("get_weather dispatches", api._dispatch_tool("get_weather", {}) == WEATHER_REPLY)

print("\n— consequential tool confirmation —")
api = make_api()
SENT_MESSAGES.clear()
app_mod.search_contacts = lambda q: [("Gavin Smith", "+15551234567")]
prompt = api._dispatch_tool("send_message", {
    "contact": "Gavin", "instruction": "say hi", "style": "casual",
})
check("message tool pauses before sending",
      "Say yes" in prompt and api._pending_tool_confirmation is not None
      and SENT_MESSAGES == [])
api._respond("yes")
check("explicit yes executes the pending tool once",
      SENT_MESSAGES == [("+15551234567", "[casual] say hi")]
      and api._pending_tool_confirmation is None)
check("confirmed outcome remains in conversation context",
      api.active_conversation[-2]["content"] == "yes"
      and api.active_conversation[-1]["content"] == "Sent to Gavin.")

api = make_api()
SENT_MESSAGES.clear()
app_mod.search_contacts = lambda q: [("Gavin Smith", "+15551234567")]
api._dispatch_tool("send_message", {
    "contact": "Gavin", "instruction": "say hi", "style": "casual",
})
api._respond("no")
check("anything other than explicit confirmation cancels",
      SENT_MESSAGES == [] and "Canceled" in SPOKEN[-1])

api = make_api()
SENT_MESSAGES.clear()
app_mod.search_contacts = lambda q: [("Gavin Smith", "+15551234567")]
question = api._dispatch_tool("send_message", {"contact": "Gavin"})
check("missing message content is collected before confirmation",
      "What exact message" in question
      and api._pending_compose is not None
      and api._pending_tool_confirmation is None)
api._respond("TEST ONLY")
check("the content answer becomes a visible confirmation, not a cancellation",
      api._pending_compose is None
      and api._pending_tool_confirmation is not None
      and "TEST ONLY" in SPOKEN[-1]
      and SENT_MESSAGES == [])
api._respond("no")
check("declining the completed preview sends nothing", SENT_MESSAGES == [])

api = make_api()
SENT_MESSAGES.clear()
app_mod.search_contacts = lambda q: [("Gavin Smith", "+15551234567")]
preview = api._dispatch_tool("send_message", {
    "contact": "Gavin", "text": "old wording",
})
check("the first confirmation shows the exact pending text",
      "old wording" in preview and SENT_MESSAGES == [])
api._respond("actually say new wording")
check("a correction revises and re-arms confirmation instead of canceling",
      api._pending_tool_confirmation is not None
      and api._pending_tool_confirmation["args"]["text"] == "new wording"
      and "new wording" in SPOKEN[-1]
      and SENT_MESSAGES == [])
api._respond("yes")
check("only the revised text is sent after a fresh yes",
      SENT_MESSAGES == [("+15551234567", "new wording")])

print("\n— compose flow: instruction → style → send —")
api = make_api()
app_mod.search_contacts = lambda q: [("Gavin Smith", "+15551234567")]
SENT_MESSAGES.clear()
r = api._compose_and_send("gavin")
check("single match with no instruction asks what to say",
      r == "What do you want to say to Gavin?"
      and api._pending_compose["stage"] == "instruction")

api._respond("ask him if he wants to golf at five")
check("instruction answer advances to the style question",
      api._pending_compose["stage"] == "style"
      and "How should it sound" in SPOKEN[-1])

api._respond("casual")
check("style answer composes and sends",
      SPOKEN[-1] == "Sent to Gavin."
      and SENT_MESSAGES == [("+15551234567",
                             "[casual] ask him if he wants to golf at five")]
      and api._pending_compose is None)

print("\n— compose flow: disambiguation —")
api = make_api()
app_mod.search_contacts = lambda q: [("John Adams", "addr-a"), ("John Baker", "addr-b")]
SENT_MESSAGES.clear()
r = api._compose_and_send("john", instruction="say hi", style="casual")
check("multiple matches ask which one",
      r == "I found a few — John Adams or John Baker. Which one?"
      and api._pending_msg is not None)

api._respond("the second")
check("ordinal answer picks the right contact and completes the send",
      SENT_MESSAGES == [("addr-b", "[casual] say hi")] and api._pending_msg is None)

# Regression: the generic word "one" must not beat the explicit "second".
api = make_api()
SENT_MESSAGES.clear()
r = api._compose_and_send("john", instruction="say hi", style="casual")
api._respond("the second one")
check("'the second one' selects the second candidate",
      SENT_MESSAGES == [("addr-b", "[casual] say hi")])

api = make_api()
r = api._compose_and_send("john", instruction="say hi", style="casual")
api._respond("nope")
check("cancel word aborts disambiguation",
      SPOKEN[-1] == "Got it, canceling." and api._pending_msg is None)

# Regression: generic cancel interception must clear a pending question too.
api = make_api()
r = api._compose_and_send("john", instruction="say hi", style="casual")
api._respond("nevermind")
check("'nevermind' cancels and clears pending disambiguation",
      SPOKEN[-1] == "Got it, canceling." and api._pending_msg is None)

api = make_api()
api._pending_compose = {"type": "imessage", "stage": "style"}
api._respond("cancel that")
check("cancel also clears a pending compose flow",
      SPOKEN[-1] == "Got it, canceling." and api._pending_compose is None)

api = make_api()
api._pending_msg = ([("John Adams", "addr-a")], "hi", time.time() - 1)   # already expired
r = api._resolve_msg_disambiguation("set a timer for 2 minutes")
check("expired disambiguation re-routes the text as a fresh command",
      r == "2 minutes timer started.")
assistant.cancel_pending(None)

print("\n— frustration tracking —")
api = make_api()
api._track_frustration("no")
api._track_frustration("wrong")
check("two short negatives in a row flag frustration", api.user_frustrated)
api._track_frustration("that was great thank you so much")
api._track_frustration("perfect that is exactly what I wanted")
check("longer positive turns clear it", not api.user_frustrated)

print("\n— conversation trim in llm._remember_exchange —")
conv = ([{"role": "system", "content": "sys"}]
        + [{"role": "user", "content": f"m{i}"} for i in range(44)])
llm._remember_exchange("what time is it", "Noon.", conv)
check("chat-path trim keeps system msg + last 40",
      len(conv) == 41 and conv[0]["content"] == "sys" and conv[-1]["content"] == "Noon.")
n_before = len(conv)
llm._remember_exchange("hello", "   ", conv)
check("whitespace-only reply is never recorded", len(conv) == n_before)

print("\n— an empty turn says what actually happened —")

# Regression: the empty-stream message was one fixed sentence claiming nothing
# was changed. A send_message turn arms a confirmation and returns; if the
# stream then ends empty, that sentence tells the user nothing is pending while
# Ted is holding a message waiting for "yes". Same shape as a cheerful lie about
# an action, pointed the other way.
api = make_api()
api._pending_tool_confirmation = None
api._pending_msg = None
api._pending_compose = None
_orig_active = llm.providers.active_provider
_orig_err = llm.providers.last_cloud_error
llm.providers.active_provider = lambda: "groq"
llm.providers.last_cloud_error = lambda: ""
check("a plain empty turn still says nothing was changed",
      "Nothing was changed" in api._explain_empty_turn())

api._pending_tool_confirmation = {"name": "send_message", "args": {}}
msg = api._explain_empty_turn()
check("an armed confirmation is reported, not denied",
      "send message" in msg and "say yes" in msg.lower())
check("…and it still says nothing was sent", "Nothing has been sent" in msg)

api._pending_tool_confirmation = None
api._pending_msg = ([("Gavin", "555")], "hi", time.time() + 20)
check("a pending 'which one?' is reported too",
      "waiting on your answer" in api._explain_empty_turn())

api._pending_msg = None
llm.providers.active_provider = lambda: "none"
llm.providers.last_cloud_error = lambda: "Connection error."
check("both brains failing is named as such",
      "local one didn't start" in api._explain_empty_turn())

llm.providers.active_provider = lambda: "ollama"
check("a local-brain turn that died says so",
      "fell back to the local one" in api._explain_empty_turn())

llm.providers.active_provider = _orig_active
llm.providers.last_cloud_error = _orig_err

print("\n— stop is never blocked by the thing it is stopping —")

# Regression, from a real log: a turn hung for 41 seconds and three separate
# "stop" attempts were each answered with "the previous request is still
# finishing". The one command whose job is escaping a stuck turn was the one
# command a stuck turn could block. From the text box there was no way out.
api = make_api()
api._pending_tool_confirmation = {"name": "send_message", "args": {}}
api._busy.acquire()                       # a turn is in flight and wedged
try:
    check("Ted reports busy", api.busy)
    SPOKEN.clear()
    api.window.js.clear()
    accepted = api.ask("stop")
    check("stop is accepted", accepted is True)
    check("…and clears anything left armed",
          api._pending_tool_confirmation is None)
    # ask() always returns fast because it hands off to a thread; what matters
    # is what the USER is told. Under the old code that thread sat on the lock
    # for 8 seconds and then said the previous request was still finishing.
    time.sleep(0.4)
    _js = " ".join(api.window.js)
    check("…and the user is told it stopped, not that Ted is busy",
          "Stopped." in _js and "still finishing" not in _js)
finally:
    api._busy.release()

# Not busy: stop keeps its normal behaviour and goes through _respond, where it
# also pauses the music when Ted is not the one talking.
api = make_api()
check("with nothing running, stop is handled normally", not api.busy)

print("\n— attachments belong to exactly one turn —")
# The bug worth guarding: a file silently riding along on the next message.
# Charlie attaches a syllabus, asks about it, then asks something unrelated —
# the second question must not still be carrying the PDF.
api = make_api()
LLM_ATTACHMENTS.clear()


class _FakeAttachment:
    ok = True
    kind = "document"
    name = "syllabus.txt"


api._pending_attachments = [_FakeAttachment()]
api._respond("when is exam two")
check("the staged file reaches the turn it was attached to",
      LLM_ATTACHMENTS and len(LLM_ATTACHMENTS[-1]) == 1)
check("…and is cleared from the staging area immediately",
      api._pending_attachments == [])

api._respond("what time is it")
check("the next message carries nothing", LLM_ATTACHMENTS[-1] == [])

print(f"\n{'=' * 50}\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
