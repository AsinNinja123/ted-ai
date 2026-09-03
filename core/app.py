"""core/app.py — TedApi: the runtime object behind the HUD.

Owns the listen→think→speak conversation loop, deterministic command routing,
the LLM tool-calling loop, and every background watcher thread. The pywebview
window exposes this object's public methods (start/listen/stop/toggle_mute/ask)
to the JS side.
"""


# =============================================================================
#  READING THIS FILE    The Ted Code Book — Chapters 5, 6, 11 and 13
#                       (§5.1 – §5.3, §6.1 – §6.8, §11.6 – §11.8, §13.x)
# =============================================================================
#
#  WHAT THIS FILE IS
#      The monolith. One class, TedApi, about 3,700 lines, and it owns the
#      single most important function in the project: `_respond()`.
#
#      Every message you ever send Ted — typed in the box, spoken at the mic,
#      or fired at the remote endpoint from your phone — ends up inside
#      `_respond()`. What happens to it there is "the ladder": a series of
#      cheap local checks, each of which either handles the message and stops,
#      or passes it down to the next one. Only messages that survive all of
#      them reach the model.
#
#      This file is too big and everyone involved knows it. See §35. Do not
#      add anything to it that could live in another module.
#
#  WHERE IT SITS
#      hud.py                     creates one TedApi and hands it to the window
#      ui/ted_hud.html            calls TedApi's public methods through
#                                 window.pywebview.api.<method>()
#      core/routing.py            TedApi asks it "which tools does this need?"
#      core/llm.py                TedApi asks it "answer this" and gets back a
#                                 stream of text
#      core/tool_handlers.py      TedApi calls these when the model picks a tool
#
#  THE SHAPE OF IT, TOP TO BOTTOM
#      lines ~1-235    imports, small module-level helper functions, and the
#                      gate-5 allowlist (`_use_deterministic_command`)
#      ~237            class TedApi begins
#      ~238-332        __init__  — every piece of state Ted holds while running.
#                      If you want to know what Ted can remember *within* one
#                      session, read this method. It is the honest answer.
#      ~390            _respond   ← THE LADDER. The heart of the program.
#      ~917-1730       _assistant_command — what is left of the old regex
#                      dispatch. Mostly unreachable now; gate 5 only lets a
#                      short allowlist of message shapes reach it.
#      ~1997           _dispatch_tool — the switchboard. A giant if/elif that
#                      turns a tool NAME chosen by the model into a real action.
#                      When you add a tool, you add a branch here.
#      ~2797-3290      background threads: reminders, session summaries, the
#                      apps watcher, the iMessage bouncer
#      ~3290-end       the JS API surface — the methods the HTML window is
#                      allowed to call
#
#  IF YOU WANT TO CHANGE SOMETHING
#      "Ted should handle X without asking the model"
#            -> add a rung in _respond(), high up, and return early. §6.8.
#      "Ted should be able to do a new thing"
#            -> that is a tool, not a rung. §11.4 and §31.
#      "Ted should remember something new during a session"
#            -> add the attribute in __init__ first, so there is one place that
#               lists what exists.
#      "The window needs to call something new"
#            -> add a method near the bottom, in the JS API section, and call it
#               from the HTML as window.pywebview.api.your_method().
#
#  PYTHON YOU'LL SEE HERE THAT MIGHT BE NEW
#      class TedApi:   /   def method(self, ...)
#          A class is a template for an object that holds data (attributes) and
#          the functions that work on that data (methods). `self` is the object
#          itself, handed to every method automatically. `self.muted` is a piece
#          of data that lives as long as Ted is running.
#
#      @property
#          Makes a method look like a plain attribute from the outside. You
#          write `api.muted` and Python quietly calls a function. Ted uses it
#          where reading or writing a value needs to also *do* something —
#          setting `self.muted = True` also turns the microphone off.
#
#      the walrus, `:=`
#          Assign and test in one step.
#              if (revised := _revised_message_args(text, args)):
#          means "work out revised; if it is not empty, go into the if, and use
#          it inside". Saves a line; that is all it is.
#
#      `getattr(engine, "_playing", False)`
#          "Give me engine._playing, but if it does not exist, give me False
#          instead of crashing." Defensive reads against optional pieces.
#
#      f-strings:  f"Now watching {topic['label']}."
#          A string with expressions baked in. Anything in {curly braces} is
#          evaluated and dropped into the text.
# =============================================================================

import asyncio
import base64
import json
import mimetypes
import os
import random
import re
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime as _dt_cls

from core import (attachments, bouncer, codebase, conversation_examples, events,
                  features, lingo, llm, memory, messages, music, notebook, outcomes, pet,
                  relationship, routing, routines, system_state, task_state,
                  telemetry, tool_handlers as th, understanding, voice)
from core.actions import (close_app, open_app, get_running_apps,
                          system_volume as control_system_volume,
                          search_contacts, send_imessage_to_address)
from core.agents import Delegation, MacAgent, Plan
from core.hud_bridge import (js, set_state as _hud_set_state, add_message,
                             show_issue as _hud_show_issue)
from core.intents import (
    _normalize_cmd, _matches, _split_commands,
    _is_stop_command, _is_cancel_command, _is_repeat_command,
    _SLOWER_PHRASES, _FASTER_PHRASES, _MUTE_PHRASES,
    _SPOT_PAUSE, _SPOT_NEXT, _SPOT_PREV,
    _BRIEF_PHRASES, _HOLD_PHRASES, _RECALL_PHRASES, _THINK_ENTER, _THINK_EXIT,
    _chat_command, _reminders_command,
    _parse_open_apps, _parse_close_apps, _resolve_context_app,
    _parse_message_cmd, _parse_reminder, _parse_list_cmd,
    _parse_calc, _parse_cancel_scheduled, _is_timer_request,
    _parse_time_to_24h, _detect_mood, _MOOD_SEARCH, _MOOD_DESC, _parse_correction,
    _classify_content_speed, _extract_pattern_topic, _confused_reply,
    _fix_command_words, _strip_wake_phrase,
    is_memory_add_command, is_memory_drop_command, memory_referent,
)
from core.logs import error_log
from core.memory import (log_pattern, get_frequent_patterns,
                         save_session_summary, get_last_session_summary,
                         get_recent_memories, search_memories,
                         log_habit, get_habit_streak, get_all_habits,
                         list_facts, forget_fact, forget_fact_by_rowid,
                         get_facts_about)
from core.paths import SHORTCUTS_PATH, GATE5_LOG
from core.tools import TOOL_SCHEMAS
from core.voice import speak, speak_streaming, capture, engine

# Escape hatch for the single-call migration. The old path made two model calls
# per message (a throwaway "does this need a tool?" probe, then the real
# streaming reply); the new one does it in a single streamed call that can emit
# text or a tool call. Set TED_LEGACY_LADDER=1 to fall back if the new path
# misbehaves on real hardware. Temporary — delete once it has proven itself.
LEGACY_LADDER = os.environ.get("TED_LEGACY_LADDER") == "1"

def set_state(window, s):
    """Drive the HUD state indicator."""
    _hud_set_state(window, s)


def show_issue(window, text):
    """Surface a real problem. One wrapper, so every caller reports the same way."""
    _hud_show_issue(window, text)


# Gate-5 usage logging. See TedApi._assistant_command for why this exists.
_GATE5_TRACE = os.environ.get("TED_GATE5_TRACE") == "1"


def _log_gate5(text, result, line=None):
    """Append one JSON line per deterministic-command hit. Best-effort and
    never allowed to break a reply — a logger that can take Ted down is worse
    than no logger."""
    try:
        rec = {"t": _dt_cls.now().isoformat(timespec="seconds"),
               "text": (text or "")[:200],
               "result": (str(result) or "")[:200]}
        if line is not None:
            rec["line"] = line
        with open(GATE5_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _revised_message_args(text, current):
    """Turn a correction at the confirmation prompt into a fresh preview.

    Returns None when the response is not recognizably a revision. Consequential
    actions still require explicit yes; this only lets “actually, say …” edit
    the pending message instead of being mistaken for a cancellation.
    """
    raw = (text or "").strip()
    quoted = re.search(r'["“](.+?)["”]', raw)
    if quoted:
        return {"contact": current.get("contact", ""), "text": quoted.group(1)}
    match = re.match(
        r"^(?:no[, ]+)?(?:actually[, ]+)?(?:change (?:it|that)(?: to)?|"
        r"replace (?:it|that) with|say|send)\s+(.+)$", raw, re.I)
    if match:
        return {"contact": current.get("contact", ""), "text": match.group(1).strip()}
    vibe = re.match(
        r"^(?:no[, ]+)?(?:actually[, ]+)?make (?:it|that)\s+(.+)$", raw, re.I)
    if vibe and current.get("text"):
        return {
            "contact": current.get("contact", ""),
            "instruction": ("Rewrite this message without changing its meaning: "
                            + current["text"]),
            "style": vibe.group(1).strip(),
        }
    return None

try:
    from config import OWNER_NAME
except Exception:
    OWNER_NAME = "Charlie"
try:
    from config import WEATHER_LOCATION
except Exception:
    WEATHER_LOCATION = ""
try:
    from config import DAILY_BRIEFING_TIME
except Exception:
    DAILY_BRIEFING_TIME = ""   # e.g. "7:30am" — spoken rundown every morning
try:
    from config import ATTENTION_WINDOW
except Exception:
    ATTENTION_WINDOW = 90   # seconds of open conversation after each interaction;
                            # after that, "Hey Ted"/"Ted, …" re-engages. 0 = always listening.

assistant = features.assistant   # None when the module is unavailable

SETTLE_AFTER_TALK = 0.25
WAKE_MIN_RMS       = 0.017   # minimum RMS for a valid wake-word trigger
WAKE_COOLDOWN_SECS = 1.5     # seconds before wake word can fire again (echo prevention)

# ── voice shortcuts ──
SHORTCUTS = {}
try:
    import os as _os
    if _os.path.exists(SHORTCUTS_PATH):
        with open(SHORTCUTS_PATH) as _f:
            SHORTCUTS = json.load(_f)
        print(f"[shortcuts] loaded {len(SHORTCUTS)} shortcut(s)")
except Exception as _e:
    print(f"[shortcuts] failed to load: {_e}")


# [BOOK §6.6] ─── GATE 5: the deterministic allowlist ────────────────────────
#
# This function answers one question: "is this message one of the few shapes we
# handle in plain Python, without asking a model at all?"
#
# It used to be the opposite — about fifty regular expressions that tried to
# catch every command, with the model only getting what was left over. That
# made Ted feel like a vending machine: any phrasing nobody had thought of
# simply did not work. It was gutted deliberately, and what survives is a short
# allowlist of things that genuinely should not go to a model.
#
# Arithmetic is the one that looks out of place and is not. A language model
# doing "8 percent of 250" fails SILENTLY — a wrong number reads exactly like a
# right one, there is nothing to log and nothing to notice. Math in Python,
# words in the model. (§34)
#
# Returning True here does not answer the message. It only means
# _assistant_command below gets a look at it first.
def _use_deterministic_command(text):
    """Keep only genuinely local/stateful controls ahead of the reasoning model.

    The old Gate 5 tried to recognize every capability with regexes, so app,
    screen, calendar, web, notes, and computer requests often never reached the
    tool-capable brain. Novel phrasing could not recover. These remaining cases
    are either latency/safety critical or manipulate Ted's own conversation
    state and therefore should stay deterministic.
    """
    if LEGACY_LADDER:
        return True
    t = text.strip().lower()
    if any(_matches(text, phrases) for phrases in
           (_BRIEF_PHRASES, _HOLD_PHRASES, _RECALL_PHRASES, _THINK_ENTER, _THINK_EXIT)):
        return True
    # Mirror the matcher gate 5 actually uses below (equality or prefix on the
    # normalized text). A loose substring test disagreed with it: "think" is a
    # shortcut, so "what did you think of chapter 3" was routed into the 750-line
    # regex dispatch, which then declined to fire the shortcut anyway. Two places
    # deciding "is this a command" by different rules is the bug class this
    # rewrite exists to remove.
    _t_norm = _normalize_cmd(text)
    for _phrase in SHORTCUTS:
        _k_norm = _normalize_cmd(str(_phrase))
        if _k_norm and (_k_norm == _t_norm or _t_norm.startswith(_k_norm)):
            return True
    # Arithmetic stays in Python. A language model doing "8 percent of 250" is
    # the exact failure the "math in Python, words in the LLM" rule exists to
    # prevent, and it fails silently — a wrong number reads like a right one.
    if _parse_calc(text) is not None:
        return True
    if (_parse_correction(text) or _parse_cancel_scheduled(text)
            or _is_timer_request(text) or _parse_reminder(text)):
        return True
    # Explicit memory control. Safe to claim the ambiguous "forget that" here:
    # the cancel branch runs earlier in the same turn and only declines the
    # phrase when a memory was written moments ago, so a plain never-mind never
    # reaches this gate at all.
    if is_memory_add_command(text) or is_memory_drop_command(text):
        return True
    return bool(re.search(
        r"\b(?:remember|remeber|rember|remmember|forget .*about me|what do you "
        r"(?:know|remember) about me|recalibrat|calibrate .*?(?:mic|microphone|ears)|"
        r"snooze|every (?:day|monday|"
        r"tuesday|wednesday|thursday|friday|saturday|sunday|\d+ minutes)|"
        r"index my documents|scan my inbox|list indexed files)\b", t))


def _response_mode_change(text):
    """Recognize explicit session-only response-format instructions.

    Formatting is Charlie's choice, not a topic for Ted to debate. This stays
    narrow so a question that merely contains "yes or no" does not silently
    change every later response.
    """
    t = " ".join((text or "").lower().replace("/", " or ").split())
    if re.search(
            r"\b(?:answer|reply|respond|talk|speak) normally\b|"
            r"\b(?:normal|full|regular) answers? (?:again|now)\b|"
            r"\bstop (?:the )?(?:one[- ]word|yes or no) answers?\b|"
            r"\b(?:you can|feel free to) (?:explain|elaborate) again\b", t):
        return "normal"
    if re.search(
            r"\b(?:only|just) (?:answer|reply|respond|say|give(?: me)?)\b.{0,24}"
            r"\byes or no\b|"
            r"\b(?:answer|reply|respond|give answers?)\b.{0,24}"
            r"\b(?:only |just )?yes or no\b", t):
        return "yes_no"
    if re.search(
            r"\b(?:only|just) (?:answer|reply|respond|say|use)\b.{0,18}"
            r"\b(?:one|1)[- ]word\b|"
            r"\b(?:answer|reply|respond)\b.{0,18}\b(?:one|1)[- ]word\b", t):
        return "one_word"
    return None

# ---------- time-based startup greetings ----------
_GREET_MORNING = [
    f"Good morning {OWNER_NAME}. How can I assist you?",
    "Morning. What are we working on today?",
    "Good morning — up and running. What do you need?",
    f"Morning, {OWNER_NAME}. Ready when you are.",
    "Rise and shine. What's the game plan today?",
]
_GREET_AFTERNOON = [
    f"Good afternoon {OWNER_NAME}. How can I assist you today?",
    "Afternoon. What do you need from me?",
    "Good afternoon — what are we tackling?",
    f"{OWNER_NAME}, afternoon. What's on the list?",
    "Afternoon. I'm here. What's up?",
]
_GREET_NIGHT = [
    "Late night? What do we need to get done?",
    "Burning the midnight oil — I'm with you. What do you need?",
    "Still at it? What are we working on?",
    "Late one. I'm here. What do you need?",
    "Night owl hours. What's on your mind?",
]

def _startup_greeting():
    """Return a time-appropriate greeting for Ted's startup."""
    h = time.localtime().tm_hour   # 0–23
    if 5 <= h < 11:
        return random.choice(_GREET_MORNING)
    elif 11 <= h < 23:
        return random.choice(_GREET_AFTERNOON)
    else:
        return random.choice(_GREET_NIGHT)


# ---------- API the HUD calls ----------
class TedApi:
    def __init__(self):
        # ── Core state ─────────────────────────────────────────────────────────
        self.window           = None              # set by main() after webview creates window
        self._busy            = threading.Lock()  # held during every listen→reply turn
        # The HUD's durable sidebar chat ID. It travels with telemetry so the
        # diagnostics panel can copy or remove one conversation at a time.
        self._active_chat_id  = None
        # Which sidebar chat is currently mirrored into ted_conversation.
        # The HUD transcript and model context used to drift apart after a
        # restart because set_active_chat changed only the numeric ID.
        self._conversation_chat_id = None
        self._loop_started    = False             # prevents starting the loop twice
        # One flag used to do two jobs, which is why "mic on but speakers off"
        # was unrepresentable. Capture and speech are separate now:
        #   (False, False)  chat only — how Ted boots
        #   (True,  True)   voice mode, the ● button
        #   (True,  False)  transcribe — talk, and the text lands in the input box
        # `muted` survives as a property meaning "not speech_on"; see below.
        self.mic_on           = False             # is capture running
        self.speech_on        = False             # is TTS allowed to play
        self.transcribe_only  = False             # capture routes to the input box
        self.pet_silent_chat  = False             # capture becomes a silent answered turn
        self.interrupt_speech = False             # set True to cut off current playback
        self.last_reply       = ""               # stored so 'repeat that' works
        self._last_cmd        = ("", 0.0)        # (normalized_text, timestamp) for dedup
        self._pending_msg             = None   # ([(name,addr),...], msg_text, expire_time) awaiting disambiguation
        self._pending_compose         = None   # dict awaiting message/email style/content input
        self._pending_disambig_compose = None  # {instruction, style} saved during contact disambiguation
        self._pending_tool_confirmation = None  # {name,args,expires} awaiting yes/no
        self._pending_lingo          = None   # {term,expires} awaiting Charlie's meaning
        # The previous user turn, so a bare "remember this" has something to
        # point at. One message deep on purpose: "this" does not reach further
        # back than that in real speech, and keeping a longer tail would invite
        # Ted to store something Charlie said ten minutes ago.
        self._prev_user_text = ""
        self._cur_user_text  = ""
        # The last memory written, and when. Two jobs: it is what a following
        # "forget that" removes, and its freshness is what tells the cancel
        # handler to keep its hands off that phrase.
        self._last_memory = None              # {"text","table","id","at"}
        # Structured ground truth for context like "close those again". This is
        # intentionally separate from prose chat history so command reasoning
        # does not need twenty old messages just to resolve a pronoun.
        self._recent_actions = []
        # The most recent thing Ted actually looked at. See
        # _dispatch_and_record for why this is not in _recent_actions.
        self._last_screen = ""
        # Replaced atomically by apps_watch. This is live machine state, not a
        # memory: every model turn sees what macOS most recently confirmed.
        self._live_state = system_state.collect(include_remote=False)

        # Augment the base system prompt with the user's auto-detected location so
        # Ted can answer "what's the weather" / "where am I" without config.
        _loc = assistant.get_location() if features.HAS_ASSISTANT else None
        _loc_line = (
            f" The user's current location is {_loc['city']}, {_loc['region']}, "
            f"{_loc['country']} (lat {_loc['lat']}, lon {_loc['lon']})."
            if _loc else ""
        )
        self.ted_conversation = [{"role": "system", "content": llm.SYSTEM_PROMPT + _loc_line}]

        # ── Humanization / personality state ───────────────────────────────────
        self.last_exchange_time = 0.0          # epoch; drives long-gap greeting + session summary
        self.user_frustrated    = False        # True → tell Groq to drop cheerful energy
        self._frustration_log   = []           # rolling window of (timestamp, bool)
        self.thinking_mode      = False        # True → Socratic mode (no advice, only questions)
        self.response_mode      = ""           # "yes_no" | "one_word" for this live session
        self.held_thought       = None         # topic saved by "hold that thought"
        self.whispering         = False        # True → lower TTS volume to match user's level

        # ── Background thread bookkeeping ──────────────────────────────────────
        self._session_summary_last_written = 0.0  # epoch; prevents re-writing an unchanged session
        # Session memory state. One memory row per session, upserted — so the
        # periodic flush, the idle watcher and the shutdown hook all refine the
        # SAME row instead of leaving three near-duplicates behind.
        self._session_row_id     = None
        self._session_started_at = _dt_cls.now().isoformat()

        # Every memory write in the process reports here, whichever path made
        # it — explicit, background extraction, or the session summary. The
        # sink is registered once and core/memory.py owns the decision about
        # what counts as an event; this end only draws it.
        memory.set_event_sink(self._on_memory_event)
        # Files staged for the NEXT message only. Filled by attach_files /
        # attach_data, drained by the turn that sends them.
        self._pending_attachments = []
        # The text the bouncer last announced, awaiting "read it" or "open it".
        self._pending_text_message = None
        # Why the bouncer is not running, if it is not. Empty when it is.
        self._bouncer_blocked = ""
        self._session_exchanges  = 0
        self._memory_lock        = threading.Lock()
        self._pattern_check_done = False           # proactive offer fires at most once per startup
        self._last_wake_time     = 0.0             # epoch; for wake-word cooldown (echo prevention)
        self._last_fired_timer   = None            # last timer that fired — for snooze

        # ── The agent layer. MacAgent owns whole Mac tasks; it calls back into
        #    _dispatch_tool for the individual handlers, which is why the
        #    dispatch it receives is flagged _from_agent=True. Without that
        #    flag the guard in _dispatch_tool would route straight back into
        #    the agent and recurse until the stack blew.
        self.mac_agent = MacAgent(
            dispatch=lambda name, args: self._dispatch_tool(
                name, args, _from_agent=True),
            list_apps=get_running_apps,
        )
        # When a standalone dashboard owns port 5175 the HUD is not on this
        # process's SSE stream, so agent events would vanish with no error.
        events.BUS.add_listener(self._mirror_event_to_hud)

        # ── Attention: after ATTENTION_WINDOW s of silence Ted goes to standby
        #    and only "Hey Ted" (or typing) re-engages — so room conversation
        #    doesn't get answered. Starts engaged.
        self.attention_until   = time.time() + max(ATTENTION_WINDOW, 5)
        self._last_action      = None   # {"kind","rid","task","label","ts"} — for "actually make it …"

    # How long after a memory write "forget that" still means that memory
    # rather than "never mind". Short on purpose: past this, the cancel reading
    # is the likelier one and the ambiguous phrase goes back to meaning cancel.
    MEMORY_REFERENT_WINDOW = 180.0

    # [BOOK §36.3] ─── RUNNING AN AGENT FROM SYNCHRONOUS CODE ─────────────────
    # Every agent method is a coroutine, because confirmation has to be able to
    # wait for a browser click without freezing the thread. This file has no
    # event loop of its own — it is threads all the way down — so calling
    # agent.execute() directly would build a coroutine object and run nothing.
    #
    # asyncio.run() makes a loop, runs the coroutine to completion, closes the
    # loop. Confirmation still works across threads: ConfirmationGate.resolve()
    # (called from the Flask thread that serves /api/confirm) hands the result
    # back with loop.call_soon_threadsafe, and the loop is alive the whole time
    # because this thread is parked inside asyncio.run waiting for it.
    #
    # The rule: never call this from a thread that already has a running loop.
    # Nothing in Ted does today. When two agents need to run at once, replace
    # the body with one long-lived loop on a daemon thread plus
    # asyncio.run_coroutine_threadsafe(...).result() — the signature stays.
    def _run_agent(self, agent, method, args=None, plan_id=None, dry_run=False):
        """Run one agent method to completion and return its AgentResult."""
        return asyncio.run(agent.execute(method, args or {}, plan_id=plan_id,
                                         dry_run=dry_run))

    # A plain refusal must never reach the small model. Asked "which apps does
    # 'no' spare?", a 3B will sometimes name one, and Ted would narrow a list
    # the user was trying to reject outright.
    _PLAIN_REFUSAL = frozenset({
        "no", "nope", "nah", "cancel", "stop", "never mind", "nevermind",
        "forget it", "no thanks", "don't", "dont", "no dont", "no don't",
    })

    @staticmethod
    def _keep_apps_from_reply(text):
        """Apps a reply at the cleanup prompt asks to spare, else an empty list.

        Empty means "this was not a correction", and the caller falls through to
        the cancellation it has always been — which is also what happens when
        the router is unavailable. Failing closed is the whole point.
        """
        if _normalize_cmd(text) in TedApi._PLAIN_REFUSAL:
            return []
        verdict = routing.extract_kept_apps(text, get_running_apps())
        if verdict in (None, routing.NOT_A_CLEANUP) or not verdict:
            return []
        return list(verdict)

    @staticmethod
    def _agent_reply(result):
        """Speak an AgentResult the way ACTION tools are spoken: ground truth.

        did and failed are often the same sentence for a single-step call —
        "Google Chrome isn't open." is both what happened and why it failed.
        Printing both produced a stutter in the log and in Ted's mouth.
        """
        if result.ok:
            return result.did
        parts = [p for p in (result.did, result.failed) if p]
        if len(parts) == 2 and parts[0] != parts[1]:
            return f"{parts[0]} {parts[1]}"
        return parts[0] if parts else "That didn't go through."

    def _mirror_event_to_hud(self, event):
        """Fallback path for runtime events when no browser is on the stream.

        Memory events are excluded because _on_memory_event already has its own
        fallback; mirroring them here too would draw every toast twice.
        """
        if events.BUS.subscriber_count > 0:
            return
        if event.kind not in ("plan", "agent_started", "agent_result",
                              "confirmation_required", "confirmation_resolved"):
            return
        try:
            js(self.window,
               f"tedHud.runtimeEvent({json.dumps(event.kind)},"
               f"{json.dumps(event.as_dict())})")
        except Exception:
            pass

    def _on_memory_event(self, ev):
        """Draw a memory change on the HUD. Registered once, in __init__.

        This is the only consumer of core/memory.memory_event, and it does not
        decide anything — it records what was written so a following "forget
        that" has a referent, and shows it. The toast is clickable: it opens
        the memory panel on that exact row, because being told Ted learned
        something is only half useful if fixing it means going to find it.
        """
        try:
            self._last_memory = {
                "text": ev.get("text", ""),
                "table": ev.get("table", "facts"),
                "id": ev.get("id"),
                "at": time.time(),
            }
            # The HUD and launch log normally consume this same event. Keep the
            # direct bridge only as a compatibility fallback when a standalone
            # dashboard process owns port 5175 and therefore cannot share this
            # in-process bus.
            has_local_stream = events.BUS.subscriber_count > 0
            events.emit("memory", ev)
            if not has_local_stream:
                js(self.window, f"tedHud.memoryEvent({json.dumps(ev)})")
        except Exception as e:
            error_log.error(f"[memory] event to HUD failed: {e}")

    def _memory_pending(self):
        """True when a memory was written recently enough to still be 'that'."""
        m = self._last_memory
        return bool(m) and (time.time() - m["at"]) < self.MEMORY_REFERENT_WINDOW

    @property
    def busy(self):
        """True when Ted is processing a turn. Thread-safe via Lock."""
        return self._busy.locked()

    # ── Attention helpers ──────────────────────────────────────────────────────

    def _engaged(self):
        """True while Ted is in open conversation (no wake word needed)."""
        return ATTENTION_WINDOW <= 0 or time.time() < self.attention_until

    def _touch_attention(self):
        """Extend the open-conversation window after any accepted interaction.
        The HUD gets the deadline and flips itself to STANDBY when it passes —
        Python is usually blocked inside capture() at that moment."""
        if ATTENTION_WINDOW > 0:
            self.attention_until = time.time() + ATTENTION_WINDOW
            js(self.window, f"tedHud.setAttention({int(self.attention_until * 1000)})")
        else:
            js(self.window, "tedHud.setAttention(0)")   # 0 = always engaged

    @property
    def active_conversation(self):
        """Returns the conversation list."""
        return self.ted_conversation

    @active_conversation.setter
    def active_conversation(self, value):
        # _try_tools assigns a trimmed copy back when the history exceeds 42
        # messages — without this setter every tool call crashed the turn.
        self.ted_conversation = value

    # [BOOK §6] ═══ THE LADDER ═══════════════════════════════════════════════
    #
    # THE most important function in Ted. Every message ends up here: typed in
    # the box (ask), spoken at the mic (conversation_loop), or sent from your
    # phone (core/remote.py). They all converge on this one method.
    #
    # The shape is a ladder, not a branch. Each rung asks a cheap local
    # question. If the answer is yes, the rung handles the message and RETURNS
    # — the message never goes any further down. Only messages that survive
    # every rung reach the model at the bottom, which is the expensive part.
    #
    # The rungs, in the order they are checked:
    #
    #   §6.2  1. mute / unmute            must be instant, and the model must
    #                                     not "discuss" being muted
    #         2. stop                     latency-critical; also pauses Spotify
    #                                     if Ted was not the one talking
    #         3. cancel                   stop, and clear any pending question
    #         4. UI commands              show the chat log, repeat that, speak
    #                                     faster — these drive the window, they
    #                                     are not thoughts
    #   §6.3  5. pending flows            you are answering a question Ted asked
    #                                     last turn: a confirmation, "which
    #                                     John?", "what should it say?"
    #   §6.4  6. lingo                    "when I say X I mean Y" — cheap,
    #                                     explicit, and needed by the very next
    #                                     routing decision
    #   §6.5  7. routines                 phrase -> actions you authored
    #                                     yourself. Zero tokens.
    #         8. documents                a complete, unambiguous doc request
    #         9. reflexes                 "open Spotify" — complete and
    #                                     reversible. Zero tokens.
    #   §6.6 10. gate 5                   the deterministic allowlist above
    #   §6.7 11. ONE STREAMED MODEL CALL  everything else
    #
    # A rung that returns early must do three things before it goes, and
    # forgetting any of them is the most common bug when adding one:
    #     engine.reset_barge_in()      or the tail of your own voice counts as
    #                                  interrupting the reply to it
    #     add_message(w, "ted", reply) or the window shows nothing
    #     self.last_reply = reply      or "repeat that" says the wrong thing
    #
    # Adding your own rung: §6.8.
    # ═════════════════════════════════════════════════════════════════════════
    def _respond(self, text, echo_user=True, spoken_prefix=None):
        """
        Think about `text` and answer out loud.
        Intercepts stop/cancel commands before they ever reach the LLM.
        spoken_prefix: if set, spoken before the reply (correction ack,
                       long-gap greeting). Pass None for the normal flow.
        Returns True if the user barged in by voice during the reply.
        """
        w = self.window
        _persisted_task = task_state.active_for(self._active_chat_id)
        self._active_task_id = _persisted_task["id"] if _persisted_task else None
        self._touch_attention()   # any processed input keeps the conversation open

        # Roll the referent window forward before anything reads it, so that
        # during THIS turn _prev_user_text is the message before it. That is
        # what a bare "remember this" points at.
        self._prev_user_text, self._cur_user_text = self._cur_user_text, text

        # [BOOK §6.2] RUNG 1 ── mute/unmute from typing or the remote endpoint ──
        # (Voice mute is intercepted in conversation_loop; while muted there is
        # no voice path at all — the mic is physically off — so typing is how
        # 'unmute' arrives.)
        # Both directions are handled here regardless of current state. Ted now
        # BOOTS MUTED (chat-first), so "mute yourself" arrives while already
        # muted far more often than it used to — and the old `not self.muted`
        # guard let it fall all the way through to the model, which then
        # cheerfully discussed muting instead of answering. Answer the intent.
        _tn_mute = _normalize_cmd(text)
        _wants_unmute = (_tn_mute.startswith("unmute") or _tn_mute in
                         ("listen", "start listening", "wake up", "turn on mic",
                          "turn on microphone", "mic on"))
        _wants_mute = (_matches(text, _MUTE_PHRASES)
                       and not any(x in _tn_mute.split()
                                   for x in ("spotify", "music", "song", "audio")))
        if _wants_unmute:
            if echo_user:
                add_message(w, "user", text)
            if not self.mic_on or self.transcribe_only or not self.speech_on:
                self.toggle_mute()
                reply = "I'm back — listening."
            else:
                reply = "Mic's already on."
            self.last_reply = reply
            add_message(w, "ted", reply)
            speak(w, reply, self)
            return False
        if _wants_mute:
            if echo_user:
                add_message(w, "user", text)
            if self.mic_on:
                # Muting is silent on purpose — speaking here would be the last
                # thing you hear after asking for quiet.
                self.muted = True
                self._apply_mic(False)
                self._push_mic_state()
            else:
                reply = "Mic's already off."
                self.last_reply = reply
                add_message(w, "ted", reply)
            return False

        # [BOOK §6.2] RUNG 2 ── stop: cut Ted off; pause Spotify if Ted wasn't speaking ──
        if _is_stop_command(text):
            was_speaking = getattr(engine, "_playing", False)
            self.interrupt_speech = True
            engine.stop_playback()
            if not was_speaking:
                # Ted wasn't talking — user almost certainly means "stop the music"
                try:
                    music.transport("pause")
                except Exception:
                    pass
            if echo_user:
                add_message(w, "user", text)
            set_state(w, "idle")
            return False

        # [BOOK §6.2] RUNG 3 ── cancel: cut off, go quiet, clear pending state ──
        # A fresh memory write makes "forget that" mean the memory, not the
        # request; the memory handler in _assistant_command picks it up instead.
        if _is_cancel_command(text, memory_pending=self._memory_pending()):
            self.interrupt_speech = True
            engine.stop_playback()
            if echo_user:
                add_message(w, "user", text)
            # A cancel while Ted is asking a compose/disambiguation question
            # must also clear that pending state. Previously "nevermind" went
            # silent here and the old question remained armed until expiry.
            _had_pending = (self._pending_msg is not None
                            or self._pending_compose is not None
                            or self._pending_tool_confirmation is not None
                            or self._pending_lingo is not None)
            if _had_pending:
                self._pending_msg = None
                self._pending_compose = None
                self._pending_disambig_compose = None
                self._pending_tool_confirmation = None
                self._pending_lingo = None
            _cancelled_task = task_state.cancel_active(self._active_chat_id)
            if _had_pending or _cancelled_task:
                reply = "Got it, canceling."
                self.last_reply = reply
                add_message(w, "ted", reply)
                speak(w, reply, self)
            set_state(w, "idle")
            return False

        # [BOOK §6.2] RUNG 4 ── UI commands: drive the window, never think ──
        cc = _chat_command(text)
        if cc:
            self.interrupt_speech = True
            engine.stop_playback()
            if echo_user:
                add_message(w, "user", text)
            js(w, f"tedHud.{'showChat' if cc=='show' else 'hideChat' if cc=='hide' else 'toggleChat'}()")
            return False

        # ── reminders-panel command ──
        rc = _reminders_command(text)
        if rc:
            self.interrupt_speech = True
            engine.stop_playback()
            if echo_user:
                add_message(w, "user", text)
            if rc == "show" and features.HAS_ASSISTANT:
                pend = assistant.pending_reminders()
                timers = [r for r in assistant._load()["reminders"]
                          if not r["done"] and r.get("kind") == "timer" and r.get("due")]
                items = []
                spoken_parts = []
                for r in pend:
                    label = r["text"].replace("Reminder — ", "").rstrip(".")
                    ts = r.get("due")
                    when = time.strftime("%-I:%M %p", time.localtime(ts)) if ts else "standing"
                    items.append({"label": label, "when": when, "kind": "reminder"})
                    spoken_parts.append(f"{label} at {when}" if ts else label)
                for r in timers:
                    secs_left = max(0, r["due"] - time.time())
                    m, s = int(secs_left) // 60, int(secs_left) % 60
                    label = r["text"].replace("Time's up — your ", "").replace(" timer is done.", "")
                    items.append({"label": label, "when": f"{m}:{s:02d} left", "kind": "timer"})
                    spoken_parts.append(f"{label} timer with {m} minutes {s} seconds left")
                js(w, f"tedHud.showReminders({json.dumps(items)})")
                # Also speak the schedule aloud
                if spoken_parts:
                    reply = "You've got " + (
                        spoken_parts[0] if len(spoken_parts) == 1
                        else ", ".join(spoken_parts[:-1]) + ", and " + spoken_parts[-1]
                    ) + "."
                else:
                    reply = "Nothing on your schedule right now."
                engine.reset_barge_in()
                self.interrupt_speech = False
                self.last_reply = reply
                add_message(w, "ted", reply)
                speak(w, reply, self)
            else:
                js(w, "tedHud.hideReminders()")
            return False

        # ── repeat: say the last thing again, don't think anew ──
        if _is_repeat_command(text):
            engine.reset_barge_in()
            self.interrupt_speech = False
            if echo_user:
                add_message(w, "user", text)
            speak(w, self.last_reply or "I haven't said anything yet.", self)
            return False

        # ── speaking rate: adjust live, then confirm at the new rate ──
        if _matches(text, _SLOWER_PHRASES):
            voice.adjust_speed(-0.1)
            engine.reset_barge_in(); self.interrupt_speech = False
            if echo_user:
                add_message(w, "user", text)
            speak(w, "Slowing down.", self)
            return False
        if _matches(text, _FASTER_PHRASES):
            voice.adjust_speed(0.1)
            engine.reset_barge_in(); self.interrupt_speech = False
            if echo_user:
                add_message(w, "user", text)
            speak(w, "Speeding up.", self)
            return False

        # [BOOK §6.3] RUNG 5 ── pending flows: you are answering Ted, not asking ──
        # ── pending confirmation for a consequential model-selected action ──
        if self._pending_tool_confirmation is not None:
            pending = self._pending_tool_confirmation
            self._pending_tool_confirmation = None
            _confirmation_cancelled = False
            if time.time() > pending["expires"]:
                result = "That confirmation expired, so I didn't do it."
                _confirmation_cancelled = True
            elif _normalize_cmd(text) in {
                    "yes", "yeah", "yep", "confirm", "do it", "send it", "go ahead"}:
                result = self._dispatch_and_record(
                    pending["name"], pending["args"], confirmed=True)
            elif pending["name"] == "send_message" and (
                    revised := _revised_message_args(text, pending["args"])):
                result = self._dispatch_tool("send_message", revised)
            elif pending["name"] == "clean_up" and (
                    _keep := self._keep_apps_from_reply(text)):
                # "don't close brave" is a narrowing of the same request. The
                # old yes/no reading made Charlie restart the whole thing to
                # save one app. Same distinction _revised_message_args draws
                # for a pending message — consent is still explicit, it is just
                # consent to a corrected list.
                _args = dict(pending["args"])
                _args["exclude"] = list(dict.fromkeys(
                    list(_args.get("exclude") or []) + _keep))
                result = self._dispatch_tool("clean_up", _args)
            else:
                result = "Canceled — nothing was sent or changed."
                _confirmation_cancelled = True
            if _confirmation_cancelled:
                task_state.cancel_active(self._active_chat_id)
            elif not th.looks_like_failure(result):
                task_state.complete(self._active_task_id, result)
            if echo_user:
                add_message(w, "user", text)
            self.last_reply = result
            add_message(w, "ted", result)
            speak(w, result, self)
            self.active_conversation.extend([
                {"role": "user", "content": text},
                {"role": "assistant", "content": result},
            ])
            if len(self.active_conversation) > 42:
                self.active_conversation = (
                    [self.active_conversation[0]] + self.active_conversation[-40:]
                )
            return False

        # ── pending compose flow: user answering "what to say / what style?" ──
        if self._pending_compose is not None:
            result = self._handle_pending_compose(text)
            engine.reset_barge_in()
            self.interrupt_speech = False
            if echo_user:
                add_message(w, "user", text)
            if result:
                self.last_reply = result
                add_message(w, "ted", result)
                speak(w, result, self)
            return False

        # ── pending contact disambiguation: user is answering "which John?" ──
        if self._pending_msg is not None:
            result = self._resolve_msg_disambiguation(text)
            engine.reset_barge_in()
            self.interrupt_speech = False
            if echo_user:
                add_message(w, "user", text)
            if result:
                self.last_reply = result
                add_message(w, "ted", result)
                speak(w, result, self)
            return False

        # ── pending personal-lingo clarification ──
        if self._pending_lingo is not None:
            pending = self._pending_lingo
            self._pending_lingo = None
            if time.time() > pending["expires"]:
                result = "That lingo question expired, so I didn't save anything."
            else:
                parsed = lingo.parse_definition(text)
                meaning = parsed[1] if parsed else re.sub(
                    r"^(?:it|that|the term)?\s*(?:means|is|refers to)\s+", "",
                    text.strip(), flags=re.I).strip(" .!?\"'“”")
                if not meaning or _is_cancel_command(text):
                    result = "No problem — I didn't save that term."
                else:
                    saved = lingo.remember(
                        pending["term"], meaning,
                        note="Learned after Ted asked Charlie for clarification")
                    result = (f"Got it — when you say “{saved['term']},” I'll understand "
                              f"“{saved['meaning']}.”")
            engine.reset_barge_in()
            self.interrupt_speech = False
            if echo_user:
                add_message(w, "user", text)
            self.last_reply = result
            add_message(w, "ted", result)
            speak(w, result, self)
            self.active_conversation.extend([
                {"role": "user", "content": text},
                {"role": "assistant", "content": result},
            ])
            return False

        engine.reset_barge_in()
        self.interrupt_speech = False
        if echo_user:
            add_message(w, "user", text)

        if spoken_prefix:
            speak(w, spoken_prefix, self)

        set_state(w, "thinking")

        # ── response format: Charlie chooses brevity; Ted does not debate it ──
        # The observed failure was "from now on just give yes or no answers"
        # receiving "No", then a lecture about why Ted would not comply. This
        # is session state, like thinking mode—not a preference to extract into
        # permanent memory and not a request the model gets to reinterpret.
        _mode_change = _response_mode_change(text)
        if _mode_change is not None:
            self.response_mode = "" if _mode_change == "normal" else _mode_change
            reply = "Yes." if _mode_change == "yes_no" else "Okay."
            self.last_reply = reply
            add_message(w, "ted", reply)
            speak(w, reply, self)
            self.active_conversation.extend([
                {"role": "user", "content": text},
                {"role": "assistant", "content": reply},
            ])
            if len(self.active_conversation) > 42:
                self.active_conversation = (
                    [self.active_conversation[0]] + self.active_conversation[-40:]
                )
            _mturn = telemetry.Turn(text, source="reflex",
                                    chat_id=self._active_chat_id)
            _mturn.provider = "reflex"
            _mturn.forced = "n/a"
            _mturn.brain_choice = "session response-format control"
            _mturn.finish(reply=reply)
            set_state(w, "idle")
            return False

        # [BOOK §6.4] RUNG 6 ── personal shorthand: "when I say X I mean Y" ──
        # Definitions are cheap, explicit, and should become available to the
        # very next routing decision without waiting for fact extraction.
        definition = lingo.parse_definition(text)
        if definition:
            saved = lingo.remember(*definition)
            reply = (f"Got it — “{saved['term']}” means “{saved['meaning']}.” "
                     "I'll use that before I choose tools or routines.")
            self.last_reply = reply
            add_message(w, "ted", reply)
            speak(w, reply, self)
            self.active_conversation.extend([
                {"role": "user", "content": text},
                {"role": "assistant", "content": reply},
            ])
            return False

        routing_text, matched_lingo = lingo.expand(text, record_usage=True)
        _lingo_context = lingo.context_line(matched_lingo)
        _active_task = task_state.active_for(self._active_chat_id)
        _interpretation = understanding.resolve(
            text, routing_text,
            action_likely=routing.likely_action_request(routing_text),
            active_task=_active_task,
            recent_actions=self._recent_actions,
        )
        if _interpretation.mode == "action":
            self._active_task_id = task_state.begin_or_continue(
                self._active_chat_id, _interpretation)
            _active_task = task_state.active_for(self._active_chat_id)
        task_state.save_interpretation(
            self._active_chat_id, _interpretation, self._active_task_id)

        # ── email auth setup (handled here so we can speak mid-flow) ──
        if re.search(
            r'\b(connect|set\s*up|setup|link|authorize|sign\s*in\s*to|log\s*in\s*to)\b.{0,20}\bemail\b'
            r'|\bemail\b.{0,20}\b(connect|set\s*up|link|authorize)\b',
            text, re.I,
        ):
            from core import email as _email_mod
            if _email_mod.is_connected():
                reply = "Your email is already connected. Ask me to check your inbox!"
            else:
                reply = (
                    "To connect email, open a terminal and run: "
                    "python3 ted-ai/setup_email.py — "
                    "it'll walk you through it in about 30 seconds."
                )
            self.last_reply = reply
            add_message(w, "ted", reply)
            speak(w, reply, self)
            return False

        # [BOOK §6.5] RUNGS 7-9 ── the zero-token lanes: routines, documents, reflexes ──
        # ── fast deterministic commands (no LLM call — regex/rule-based) ──
        # Personal sayings are checked before generic reflexes. They are
        # explicitly authored in the dashboard, contain only low-risk actions,
        # and therefore should never spend tokens asking a model what they mean.
        routine = routines.match_routine(routing_text)
        if routine is not None:
            _rturn = telemetry.Turn(text, source="routine",
                                    chat_id=self._active_chat_id)
            _rturn.provider = "routine"
            _rturn.forced = "n/a"
            results = self._execute_routine(routine)
            for step in routine["steps"]:
                _rturn.note_tool(step["tool"])
            engine.reset_barge_in()
            reply = " ".join(results)
            self.last_reply = reply
            add_message(w, "ted", reply)
            failed = [result for result in results if th.looks_like_failure(result)]
            if failed:
                show_issue(w, reply)
            else:
                task_state.complete(self._active_task_id, reply)
            _rturn.finish(reply=reply, error="; ".join(failed))
            speak(w, reply, self)
            return False

        # Only complete, reversible app requests qualify. A partial/ambiguous
        # match declines the whole turn and reaches the reasoner below.
        document_plan = routing.plan_document(routing_text)
        if document_plan is not None:
            _rturn = telemetry.Turn(text, source="document",
                                    chat_id=self._active_chat_id)
            result = self._create_document_workflow(document_plan)
            _rturn.provider = llm.providers.active_provider()
            _rturn.model = llm.providers.active_model()
            _rturn.note_tool("create_document")
            _failed = th.looks_like_failure(result)
            task_state.record_action(
                self._active_task_id, "create_document",
                outcomes.normalize("create_document", document_plan, result,
                                   is_failure=th.looks_like_failure, acted=True))
            _rturn.finish(reply=result, error=result if _failed else "")
            self.last_reply = result
            add_message(w, "ted", result)
            if _failed:
                show_issue(w, result)
            else:
                task_state.complete(self._active_task_id, result)
            speak(w, result, self)
            self.active_conversation.extend([
                {"role": "user", "content": text},
                {"role": "assistant", "content": result},
            ])
            return False

        # [BOOK §7.7] The cleanup lane.
        #
        # The model WAS given a clean_up tool, listed first, with a description
        # ending "Do NOT chain close_app calls to do this". It chained
        # close_app anyway — twice, on a rate-limited free tier. So this lane
        # does not ask it whether to clean up.
        #
        # Bare "clean up" is settled by the pattern and costs nothing. A tail
        # like "but leave brave" is a cleanup whose SHAPE the pattern knows and
        # whose MEANING it does not, so llama3.2:3b reads the tail. A regex
        # tried that and needed a new alternative for every phrasing.
        #
        # Deliberately the RAW text, not routing_text: lingo (ch. 18) expands
        # "Clean up" into a sentence, and matching the expansion read "clean up
        # Chrome" as a whole-desktop cleanup. What Charlie typed is the request.
        _clean_keep = []
        _clean_asked = False
        _is_clean = routing.cleanup_reflex(text)
        if not _is_clean and routing.cleanup_request(text):
            _clean_asked = True
            _verdict = routing.extract_kept_apps(text, get_running_apps())
            if _verdict == routing.NOT_A_CLEANUP:
                _is_clean = False            # "clean up Chrome" is one close
            elif _verdict is None:
                _is_clean = False            # router silent — never guess here
            else:
                _is_clean, _clean_keep = True, _verdict
        if _is_clean:
            _cturn = telemetry.Turn(text, source="reflex",
                                    chat_id=self._active_chat_id)
            # Say which it actually was. A turn the small local router shaped is
            # not the same as one the pattern settled for free, and the
            # diagnostics panel is the only place that difference shows.
            _cturn.provider = "router" if _clean_asked else "reflex"
            _cturn.forced = "n/a"
            print(f"[plan] cleanup via {'llama router' if _clean_asked else 'reflex'}"
                  + (f", sparing {', '.join(_clean_keep)}" if _clean_keep else ""))
            result = self._dispatch_and_record("clean_up", {"exclude": _clean_keep})
            _cturn.note_tool("clean_up")
            self.interrupt_speech = False
            if echo_user:
                add_message(w, "user", text)
            self.last_reply = result
            add_message(w, "ted", result)
            speak(w, result, self)
            if not th.looks_like_failure(result):
                task_state.complete(self._active_task_id, result)
            self.active_conversation.extend([
                {"role": "user", "content": text},
                {"role": "assistant", "content": result},
            ])
            _cturn.finish(reply=result,
                          error=result if th.looks_like_failure(result) else "")
            set_state(w, "idle")
            return False

        reflex = (routing.plan_system_volume(routing_text, self._recent_actions)
                  or routing.plan_reflex(routing_text))
        if reflex is not None:
            # Logged like any other turn. A reflex hit costs zero tokens and no
            # model call, which is the whole point of the lane — but if it is
            # never recorded the diagnostics panel makes it look like Ted
            # simply did less work that minute.
            _rturn = telemetry.Turn(text, source="reflex",
                                    chat_id=self._active_chat_id)
            _rturn.provider = "reflex"
            _rturn.forced = "n/a"
            results = self._execute_reflex(reflex)
            for _name, _ in reflex.calls:
                _rturn.note_tool(_name)
            # Same as every other early return below: the barge-in detector has
            # to be cleared before Ted speaks, or the tail of the user's own
            # request counts as an interruption of the reply to it.
            engine.reset_barge_in()
            reply = " ".join(results)
            self.last_reply = reply
            add_message(w, "ted", reply)
            _failed = [r for r in results if th.looks_like_failure(r)]
            if _failed:
                show_issue(w, reply)
            else:
                task_state.complete(self._active_task_id, reply)
            _rturn.finish(reply=reply, error="; ".join(_failed))
            speak(w, reply, self)
            return False

        asst_result = self._assistant_command(text) if _use_deterministic_command(text) else None
        if asst_result is not None:
            if _interpretation.mode == "action":
                task_state.record_action(
                    self._active_task_id, "deterministic_command",
                    outcomes.normalize("deterministic_command", {"request": routing_text},
                                       asst_result, is_failure=th.looks_like_failure,
                                       acted=True))
            engine.reset_barge_in()
            self.last_reply = asst_result
            add_message(w, "ted", asst_result)
            if th.looks_like_failure(asst_result):
                show_issue(w, asst_result)
            elif _interpretation.mode == "action":
                task_state.complete(self._active_task_id, asst_result)
            speak(w, asst_result, self)
            return False

        # ── LEGACY LADDER (TED_LEGACY_LADDER=1): the old two-call path ──
        # Kept as a one-env-var escape hatch while the single-call path proves
        # itself on real hardware. Delete this branch once it has.
        if LEGACY_LADDER:
            tool_result = self._try_tools(text)
            if tool_result is not None:
                self.last_reply = tool_result
                add_message(w, "ted", tool_result)
                speak(w, tool_result, self)
                return False

        # [BOOK §6.7] RUNG 11 ── THE MODEL ────────────────────────────────────
        # Everything above was free. From here down the turn costs tokens.
        #
        # What happens, in order:
        #   routing.select_tool_schemas   pick a SMALL tool menu for this
        #                                 message, not the whole catalogue (§7.2)
        #   llm.ToolRuntime               a holder for "these tools, and how to
        #                                 run one when the model picks it" (§11.2)
        #   llm.ask_streaming             build the prompt, make ONE streamed
        #                                 call, hand back chunks as they arrive
        #   speak_streaming               say and display each chunk immediately
        #
        # Note `_selected_dispatch` below: it intercepts one special tool name,
        # `find_tools`, which is how the model asks for capabilities the router
        # did not give it. That escape hatch is what lets the router be
        # approximate. (§7.3)
        # ── one streamed call that either answers or reaches for a tool ──
        time.sleep(0.15)

        def _note_action_result(result):
            """Surface a failed action on the HUD. Ground truth either way —
            the result string itself is what gets spoken, unchanged."""
            if th.looks_like_failure(result):
                show_issue(w, result)

        _runtime = None
        _selected_schemas = []
        # Local reference resolution is stronger than the verb-only router for
        # short continuations such as "keep going" and "do it again".
        _action_likely = _interpretation.mode == "action"
        _needs_operational = bool(
            _action_likely or _interpretation.references
            or _interpretation.missing_information)
        # Write the requested-work lower bound to the durable scratchpad before
        # building its prompt card. That way the model sees what is already
        # verified and how many real actions remain, including after restart.
        _minimum_actions = routing.expected_action_calls(routing_text)
        _new_request_actions = _minimum_actions
        if _interpretation.references and _active_task:
            _original_minimum = routing.expected_action_calls(
                _active_task.get("goal") or "")
            _completed_count = len(_active_task.get("completed_steps") or [])
            if re.search(r"\b(?:again|repeat|one more time|same)\b", text, re.I):
                _minimum_actions = max(_minimum_actions, _original_minimum)
            elif _minimum_actions == 0:
                _minimum_actions = max(1, _original_minimum - _completed_count)
        if _action_likely and self._active_task_id:
            _plan_source = routing_text
            if (_interpretation.references and _active_task
                    and (_new_request_actions == 0 or re.search(
                        r"\b(?:again|repeat|one more time|same)\b", text, re.I))):
                _plan_source = _active_task.get("goal") or routing_text
            task_state.set_plan(
                self._active_task_id, _minimum_actions,
                routing.requested_action_steps(_plan_source))
            _active_task = task_state.active_for(self._active_chat_id)
        # Computed unconditionally. It is a few hundred characters of already
        # verified action results, and select_tool_schemas now needs it even on
        # turns that do not look operational — "say yes" while a terminal sits
        # on a confirm prompt is the case that made this necessary. What gets
        # INJECTED into the prompt is still gated by _needs_operational below.
        _recent_context = routing.operational_context(self._recent_actions)
        _selection_text = routing_text
        if _interpretation.references and _active_task:
            _selection_text += " " + " ".join(
                str(_active_task.get(key) or "")
                for key in ("goal", "last_user_text", "current_step"))
        if not LEGACY_LADDER:
            # Only recent verified actions may influence pronoun-based tool
            # selection. Passing the whole generated context here used words in
            # behavior examples, relationship memory, and the live app tree to
            # accidentally load dozens of unrelated schemas for "delete that".
            _selected_schemas = routing.select_tool_schemas(
                _selection_text, _recent_context, self._last_screen)
        _context_scope = routing.memory_scope_for(routing_text, _selected_schemas)
        _live_context = (system_state.format_for_prompt(self._live_state)
                         if _needs_operational and _selected_schemas else "")
        _relationship_context = (
            relationship.working_context(limit=3, query=routing_text, max_chars=600)
            if _context_scope in ("none", "relevant", "full") else "")
        _task_context = (task_state.format_for_prompt(_active_task)
                         if _active_task and _needs_operational else "")
        _behavior_example = (
            conversation_examples.select(
                text, _interpretation, frustrated=self.user_frustrated)
            if conversation_examples.needed(
                text, _interpretation, frustrated=self.user_frustrated) else "")
        _interpretation_context = (
            _interpretation.for_prompt()
            if (_needs_operational or _interpretation.constraints) else "")
        _op_context = "\n".join(
            part for part in (
                _lingo_context, _interpretation_context, _task_context,
                _relationship_context, _behavior_example, _live_context,
                # Selection above may read the recent actions on any turn; the
                # PROMPT only pays for them when the turn is operational.
                _recent_context if _needs_operational else "",
            ) if part)
        if not LEGACY_LADDER and _selected_schemas:
            def _selected_dispatch(name, args):
                if name == "find_tools":
                    if _runtime.discovery_used:
                        return ("Tool discovery was already used for this turn. "
                                "Use the loaded capability or explain the limitation.")
                    existing = set(_runtime.schema_by_name)
                    capacity = max(0, 8 - (len(existing) - ("find_tools" in existing)))
                    found = routing.discover_tool_schemas(
                        args.get("query", ""), exclude=existing,
                        limit=min(4, capacity))
                    added = _runtime.consume_discovery(found, max_total=8)
                    if added:
                        return ("Loaded capabilities: " + ", ".join(added)
                                + ". Now use the appropriate tool.")
                    return ("No matching capability is available. Ask one short "
                            "clarifying question or explain the limitation.")
                return self._dispatch_and_record(name, args)

            _runtime = llm.ToolRuntime(
                schemas=_selected_schemas,
                dispatch=_selected_dispatch,
                action_tools=th.ACTION_TOOLS,
                on_failure=_note_action_result,
                is_failure=th.looks_like_failure,
                progress_reader=(
                    lambda: task_state.progress(self._active_task_id)),
                # Router-miss recovery. The menu above is small on purpose; this
                # is what stops a tool the router omitted from costing two extra
                # round trips at ~4,400 tokens each.
                catalog=routing.catalog(),
            )
        # Attachments belong to exactly one turn. Taken rather than read, so a
        # file cannot silently ride along on the next message — and cleared
        # before the call, so a failure mid-turn does not strand it either.
        _attached, self._pending_attachments = self._pending_attachments, []
        # Keep old/test callers byte-compatible when there is no HUD chat.
        # Real typed HUD turns always have one because send waits for the chat
        # save before entering this path.
        _telemetry_chat = ({"telemetry_chat_id": self._active_chat_id}
                           if self._active_chat_id is not None else {})
        _response_style = ({"response_mode": self.response_mode}
                           if self.response_mode else {})
        gen = llm.ask_streaming(text, self.active_conversation,
                                frustrated=self.user_frustrated,
                                thinking_mode=self.thinking_mode,
                                window=w,
                                voice_mode=not self.muted,
                                tool_runtime=_runtime,
                                context_scope=_context_scope,
                                operational_context=_op_context,
                                require_tool=_action_likely,
                                min_action_calls=_minimum_actions,
                                attachments=_attached,
                                **_telemetry_chat,
                                **_response_style)
        # Voice expressiveness: adjust speed by content type
        resp_speed = voice.SPEED * _classify_content_speed(text)
        # Whisper volume scale
        resp_vol = 0.50 if self.whispering else 1.0
        full, barged = speak_streaming(w, gen, self, speed=resp_speed, volume=resp_vol)
        if full.strip():
            self.last_reply = full
            add_message(w, "ted", full)
            if (_action_likely and self._pending_tool_confirmation is None
                    and self._pending_msg is None
                    and self._pending_compose is None):
                # complete() consults the ledger itself. A sentence from an
                # early failed attempt may remain in the truthful final report
                # even after later observation proved recovery, so scanning the
                # concatenated prose for "failed" is the wrong completion gate.
                task_state.complete(self._active_task_id, full)
        else:
            # An empty stream is a runtime failure, not a failure to understand
            # the user. Never blame the request with "didn't catch that."
            err = self._explain_empty_turn()
            self.last_reply = err
            add_message(w, "ted", err)
            show_issue(w, err)
            speak(w, err, self)
        # If Groq was unreachable this turn, leave the HUD on the error state
        # (yellow sphere) until the next good turn — speak_streaming reset it to idle.
        if not llm.groq_ok():
            set_state(w, "error")
        return barged

    def _explain_empty_turn(self):
        """Say what actually happened when a turn produced no text.

        The old wording was one fixed sentence: "That request stopped before I
        could complete it. Nothing was changed." It was wrong in the case that
        matters most. A send_message turn arms a confirmation and then returns;
        if the stream ends empty around it, the user is told nothing was changed
        while Ted is in fact holding a message waiting for "yes" — the exact
        cheerful-lie shape the honesty rule exists to prevent, pointed the other
        way.

        So: report the pending confirmation if there is one, name the brain that
        failed if one did, and only fall back to the generic sentence when
        neither is true.
        """
        pending = self._pending_tool_confirmation
        if pending:
            what = (pending.get("name") or "that").replace("_", " ")
            return (f"I got as far as preparing {what} and stopped there. "
                    "Nothing has been sent — say yes to go ahead, or anything "
                    "else to cancel.")

        if self._pending_msg is not None or self._pending_compose is not None:
            return ("I stopped part way through and I'm still waiting on your "
                    "answer to finish it. Nothing was sent.")

        try:
            provider = llm.providers.active_provider()
            cloud_error = llm.providers.last_cloud_error()
        except Exception:
            provider, cloud_error = "", ""

        if provider == "none" and cloud_error:
            return ("Neither brain answered — the cloud model was unreachable "
                    "and the local one didn't start. Nothing was changed.")
        if provider == "ollama":
            return ("The cloud model was unavailable so I fell back to the "
                    "local one, and that didn't finish. Nothing was changed.")
        if cloud_error:
            return (f"The cloud model failed on that one. Nothing was changed.")

        return "That request stopped before I could complete it. Nothing was changed."

    def _assistant_command(self, text):
        """Instrumented wrapper around the deterministic-command dispatch.

        WHY THIS EXISTS: _assistant_command_impl is ~750 lines, ~300 branches
        and ~50 regexes, and it runs BEFORE the model gets a say — so any
        phrasing it catches is decided without intelligence, and a regex
        written for one intent can swallow a message meant for another. The
        plan is to delete most of it. But deleting 300 branches from memory is
        guesswork, and guesswork here breaks a daily driver.

        So: log every branch that fires, with the text that triggered it, and
        decide from a week of real usage instead. `python tools/gate5_report.py`
        ranks what actually fired. Anything that never fires is dead weight and
        can go; anything that does gets read individually, and mostly deleted
        too, because a tool already covers it.

        Set TED_GATE5_TRACE=1 for exact line-number attribution (line tracing,
        slightly slower — off by default so the hot path stays hot).

        This wrapper is temporary. It comes out with the branches.
        """
        result = None
        line = None
        if _GATE5_TRACE:
            result, line = self._assistant_command_traced(text)
        else:
            result = self._assistant_command_impl(text)
        if result is not None:
            _log_gate5(text, result, line)
        return result

    def _assistant_command_traced(self, text):
        """Run the dispatch under a line tracer to capture which return fired."""
        import sys as _sys
        seen = {"line": None}

        def _local(frame, event, arg):
            if event == "line":
                seen["line"] = frame.f_lineno
            return _local

        def _global(frame, event, arg):
            if event == "call" and frame.f_code.co_name == "_assistant_command_impl":
                return _local
            return None

        old = _sys.gettrace()
        _sys.settrace(_global)
        try:
            return self._assistant_command_impl(text), seen["line"]
        finally:
            _sys.settrace(old)

    def _assistant_command_impl(self, text):
        """Personal-assistant commands. Returns a spoken reply string if this was a
        command (possibly ""), or None to fall through to the LLM."""
        if not features.HAS_ASSISTANT:
            return None

        # ── pending disambiguation: user is answering "which John?" ────────
        if self._pending_msg is not None:
            return self._resolve_msg_disambiguation(text)

        # ── compound command: split and execute each part ──────────────────
        parts = _split_commands(text)
        if len(parts) > 1:
            results = []
            for part in parts:
                r = self._assistant_command(part)
                if r:               # None = not a command; "" = silent success
                    results.append(r)
            return " ".join(results) if results else None

        t = text.lower().strip()

        # ── correction: "actually make it 20 minutes" edits the last timer/reminder ──
        corr = _parse_correction(text)
        if corr and self._last_action and time.time() - self._last_action["ts"] < 120:
            fixed = self._apply_correction(corr)
            if fixed:
                return fixed
            # unparseable correction value — fall through so the LLM can respond

        # ── context: "open that" / "open it" → open the last-mentioned app ──
        if re.search(r'\b(open|launch|pull up|start)\s+(that|it|the\s+app)\b', t):
            ctx_key = _resolve_context_app(self.last_reply)
            if ctx_key:
                return open_app(ctx_key)

        # ── app launcher: "open spotify and chrome", "launch firefox" ──
        open_keys = _parse_open_apps(t)
        if open_keys:
            results = [open_app(k) for k in open_keys]
            return " ".join(results)

        # ── app closer: "close spotify and messages", "quit chrome" ──
        close_keys = _parse_close_apps(t)
        if close_keys:
            results = [close_app(k) for k in close_keys]
            return " ".join(results)

        # ── iMessage: "message gavin [and ask him if he wants to golf at 5]" ──
        msg_cmd = _parse_message_cmd(t)
        if msg_cmd:
            contact, instruction = msg_cmd
            return self._send_message_to_contact(contact, instruction)

        # ── explicit web lookup: "look up X" / "search for X" / "google X" ──
        from core.intents import _parse_lookup
        lookup_q = _parse_lookup(text)
        if lookup_q:
            speak(self.window, "Looking that up.", self)
            engine.reset_barge_in()
            self.interrupt_speech = False
            set_state(self.window, "thinking")
            return llm.web_answer(lookup_q)

        # ── Spotify ──
        sp = music.handle_spoken(text)
        if sp is not None:
            return sp

        # ── daily briefing / rundown ──
        if _matches(text, _BRIEF_PHRASES):
            return self._briefing()

        # ── hold that thought ──
        if _matches(text, _HOLD_PHRASES):
            ctx = self.last_reply[:120].rstrip() if self.last_reply else "(nothing said yet)"
            self.held_thought = ctx
            return "Held."

        # ── pick that back up ──
        if _matches(text, _RECALL_PHRASES):
            if not self.held_thought:
                return "I don't have anything held — we can pick up wherever you'd like."
            recap = f"We were just on: {self.held_thought}"
            self.held_thought = None
            return recap

        # ── thinking partner mode ──
        if _matches(text, _THINK_ENTER) and not self.thinking_mode:
            self.thinking_mode = True
            return "What's on your mind?"
        if _matches(text, _THINK_EXIT) and self.thinking_mode:
            self.thinking_mode = False
            return "There you go."

        # ── mood music ──
        mood = _detect_mood(text)
        if mood and features.HAS_SPOTIFY_WEB:
            query = _MOOD_SEARCH[mood]
            desc  = _MOOD_DESC[mood]
            if music.spotify_web_ready():
                result = features.spotify_web.play_track(query)
                return f"Playing {desc}. {result}"
            return f"I'd play {desc} but Spotify isn't connected yet."

        # ── weather (direct call — no LLM needed) ──
        if re.search(
            r'\b(?:weather|temperature|temp|forecast|how(?:\'s| is) it outside'
            r'|what(?:\'s| is) it like outside|rain today|going to rain)\b',
            text, re.I,
        ):
            return th.tool_get_weather()

        # ── voice shortcuts ──
        if SHORTCUTS:
            t_norm = _normalize_cmd(text)
            for key, action_def in SHORTCUTS.items():
                k_norm = _normalize_cmd(key)
                if k_norm and (k_norm == t_norm or t_norm.startswith(k_norm)):
                    return self._execute_shortcut(action_def)

        # ── mic recalibration: fixes 'Ted seems deaf' after a noisy launch ──
        # (the VAD threshold is set from ambient noise at startup; if the room
        # was loud then, quiet speech gets rejected before transcription)
        if re.search(r"\brecalibrat|\bcalibrate (?:the |your )?(?:mic|microphone|ears)\b",
                     text, re.I):
            # Calibration reads real ambient frames. With the mic released
            # there are none, so this would sit for the timeout and then report
            # a threshold it never measured — a confident wrong number, which
            # is the failure mode this project keeps having to design out.
            if not engine.mic_is_open():
                return ("The mic is off, so there's nothing to calibrate "
                        "against. Turn voice on and ask me again.")
            speak(self.window, "Recalibrating — stay quiet for a second.", self)
            try:
                thr = engine.calibrate()
                return f"Done. New silence threshold {thr:.3f}."
            except Exception:
                return "Calibration failed — mic may be busy."

        # ── quick spoken math ──
        calc = _parse_calc(text)
        if calc:
            return calc

        # ── cancel a running timer / reminders ──
        csch = _parse_cancel_scheduled(text)
        if csch:
            kind = None if csch == "all" else csch
            n = assistant.cancel_pending(kind)
            if csch == "timer":
                if n:
                    js(self.window, "tedHud.clearTimer()")
                return "Timer cancelled." if n else "No timer running."
            if csch == "reminder":
                return (f"Cleared {n} reminder" + ("s" if n != 1 else "") + ".") if n \
                    else "No reminders set."
            if n:
                js(self.window, "tedHud.clearTimer()")
            return f"Cleared {n} scheduled item" + ("s" if n != 1 else "") + "." if n \
                else "Nothing scheduled."

        # ── timer ──
        if _is_timer_request(text):
            secs = assistant.parse_duration(text)
            if not secs:
                m = re.search(r"(\d+(?:\.\d+)?)", text)
                secs = float(m.group(1)) * 60 if m else None
            if not secs:
                return "How long should I set the timer for?"
            human = assistant.human_duration(secs)
            end_ts = time.time() + secs
            # Extract optional label: "set a pasta timer" → "pasta"
            _lbl_m = re.search(r'(?:set |start )?(?:an? )?(\w+) timer\b', text, re.I)
            _lbl = _lbl_m.group(1).lower() if _lbl_m else None
            _skip = {"a", "an", "the", "my", "new", "quick", "short", "long", "minute", "second", "hour"}
            if _lbl in _skip:
                _lbl = None
            timer_text = (f"Time's up — your {_lbl} timer is done."
                          if _lbl else f"Time's up — your {human} timer is done.")
            rid = assistant.add_reminder(timer_text, end_ts, kind="timer", label=_lbl)
            end_ms = int(end_ts * 1000)
            safe_label = (_lbl or human).replace("'", "\\'")
            js(self.window, f"tedHud.addTimer({rid}, {end_ms}, '{safe_label}')")
            self._last_action = {"kind": "timer", "rid": rid, "task": None,
                                 "label": _lbl, "ts": time.time()}
            return f"{_lbl.capitalize() + ' timer' if _lbl else human + ' timer'} started."

        # ── reminder ──
        rem = _parse_reminder(text)
        if rem:
            task, due = rem
            # Ambiguity check: bare "at 3" with no am/pm and hour ∈ [1,12]
            _ambig = re.search(r"\bat\s+(1[0-2]|[1-9])(?:\s*o.?clock)?\s*$", text, re.I)
            if _ambig and due:
                hr = int(_ambig.group(1))
                if hr <= 12:
                    # Ask which half of the day rather than guessing
                    return f"Did you mean {hr} AM or {hr} PM?"
            if due:
                rid = assistant.add_reminder(f"Reminder — {task}.", due, kind="reminder")
                self._last_action = {"kind": "reminder", "rid": rid, "task": task,
                                     "label": None, "ts": time.time()}
                _spoken_time = time.strftime("%-I:%M %p", time.localtime(due)).lstrip("0")
                return f"Reminder set for {_spoken_time} — {task}."
            rid = assistant.add_reminder(f"Reminder — {task}.", None, kind="reminder")
            self._last_action = {"kind": "reminder", "rid": rid, "task": task,
                                 "label": None, "ts": time.time()}
            return f"Reminder added: {task}."

        # (named-list intent removed 2026-08 — feature retired)

        # ── explicit memory control: "remember this" / "forget that" ──────────
        # These are the REFERRING forms, where the thing to act on is not in
        # this sentence. They run ahead of the _REMEMBER_VERB regex below, which
        # handles the self-contained kind ("remember I'm 20") and would
        # otherwise answer a bare "remember this" with "What should I remember?"
        # while the answer was sitting in the previous message.
        if is_memory_drop_command(text) and self._memory_pending():
            gone = self._last_memory
            n = 0
            try:
                # By row id, not by words. Fact rows are the only thing
                # removable this way; a session summary is Ted's own note about
                # a whole conversation and is not what "forget that" means.
                if gone.get("table") == "facts":
                    n = forget_fact_by_rowid(gone.get("id"))
            except Exception as e:
                error_log.error(f"[memory] explicit forget failed: {e}")
            self._last_memory = None
            if n:
                return f"Forgotten — I've dropped \"{gone['text']}\"."
            # Ground truth over a comfortable answer: if nothing was deleted,
            # do not claim it was.
            return ("I couldn't find that one to remove — open the memory panel "
                    "and delete it there if it's still listed.")

        if is_memory_add_command(text):
            referent = memory_referent(text, self._prev_user_text)
            if not referent:
                return "What should I remember?"
            saved = 0
            try:
                saved = llm.extract_and_save_facts(referent, "")
            except Exception as e:
                error_log.error(f"[memory] explicit remember failed: {e}")
            if features.HAS_KNOWLEDGE:
                try:
                    features.knowledge.add_text(referent, source="voice")
                except Exception:
                    pass
            if saved:
                # The toast already named it — memory.memory_event fired inside
                # save_fact — so the spoken reply does not repeat it back.
                return "Got it — I'll remember that."
            return ("Saved it to my notes, though I couldn't pin it down as a "
                    "fact about you.")

        # ── remember: personal facts → facts table, everything else → knowledge base ──
        # "remember this" used to be the only phrasing that matched, and it only
        # ever wrote to the knowledge base. That meant "remember I'm 20" got a
        # cheerful "got it" and stored nothing Ted would actually recall, since
        # only the facts table is injected into every prompt. Both holes fixed here.
        _REMEMBER_VERB = (r'(?:remember|remeber|rember|remmember)(?: that| this| it)?'
                          r'|add to (?:your )?knowledge|note (?:this|that)|save this'
                          r'|don\'?t forget|do not forget|teach you(?:rself)?')
        # Leading form:  "remember that I'm 20"
        km = re.search(rf'\b(?:{_REMEMBER_VERB})\s*[:\-,]?\s*(.+)', text, re.I)
        body = km.group(1).strip().rstrip(".") if km else ""
        # When the verb ENDS the sentence ("...remember that"), the regex
        # backtracks and hands back the bare filler word as the body. Treat that
        # as no body at all so the trailing form below gets its turn.
        if body.lower().strip(".,!? ") in ("that", "this", "it", "", "them", "those"):
            body = ""
        if not body:
            # Trailing form: "I'm 20 years old, remember that" — people state the
            # fact first and tack the instruction on the end. The leading pattern
            # finds nothing after the verb, so look for the statement BEFORE it.
            tm = re.search(rf'^(.+?)[\s,.]*\b(?:{_REMEMBER_VERB})[\s.!?]*$', text, re.I)
            if tm:
                km = tm
                body = tm.group(1).strip().rstrip(".,")
        if km:
            if not body:
                return "What should I remember?"

            # Personal statement? Then it belongs in the facts table, which is
            # what gets fed into the prompt on every single turn.
            personal = re.search(r"\b(i|i'?m|im|my|mine|me|we|our)\b", body, re.I)
            if personal:
                try:
                    saved = llm.extract_and_save_facts(body, "")
                except Exception as e:
                    error_log.error(f"[memory] explicit remember failed: {e}")
                    saved = 0
                if saved:
                    # Mirror into the knowledge base too so it's searchable later.
                    if features.HAS_KNOWLEDGE:
                        try:
                            features.knowledge.add_text(body, source="voice")
                        except Exception:
                            pass
                    return "Got it — I'll remember that."
                # Extraction produced nothing usable. Say so rather than
                # claiming to remember something that was never stored.
                if features.HAS_KNOWLEDGE and features.knowledge.add_text(body, source="voice"):
                    return "Saved it to my notes, though I couldn't pin it down as a fact about you."
                return "I couldn't store that one — say it as a plain statement, like 'remember I'm twenty.'"

            if features.HAS_KNOWLEDGE:
                n = features.knowledge.add_text(body, source="voice")
                return "Got it, saved." if n else "Couldn't save that — knowledge base unavailable."
            return "Couldn't save that — knowledge base unavailable."

        # ── "what do you know about me" / "forget what you know about me" ──
        if re.search(r"\bwhat do you (?:know|remember) about me\b", text, re.I):
            facts = list_facts(OWNER_NAME)
            if not facts:
                return "Nothing stored about you yet. Say 'remember I'm...' and I'll start keeping track."
            readable = ", ".join(f"{r.replace('_', ' ').lower()} {o}" for r, o in facts[:8])
            return f"Here's what I have: you {readable}."

        if re.search(r"\bforget (?:everything |what )?(?:you know )?about me\b", text, re.I):
            n = forget_fact(OWNER_NAME)
            return f"Cleared {n} fact{'s' if n != 1 else ''} about you." if n else "I didn't have anything stored about you."

        # ── knowledge query: "what do you know about X" ──
        kq = re.search(r'\bwhat do you know about\s+(.+)', text, re.I)
        if kq:
            query = kq.group(1).strip().rstrip("?.")
            if features.HAS_KNOWLEDGE:
                result = features.knowledge.search(query, k=2)
                if result:
                    snippet = result[:350].rstrip()
                    return f"Here's what I have on {query}: {snippet}"
                return f"Nothing stored on {query} yet."
            return "Knowledge base isn't available right now."

        # ── knowledge inventory: "what's in your knowledge base" ──
        if re.search(
            r'\b(what(?:\'s| is) in your knowledge|how many things do you know'
            r'|list your knowledge|knowledge base)\b',
            text, re.I,
        ):
            if features.HAS_KNOWLEDGE:
                n = features.knowledge.count()
                sources = features.knowledge.list_sources()
                if n == 0:
                    return "The knowledge base is empty — try saying 'remember this' followed by anything you want stored."
                src_str = (", ".join(sources[:6]) + ("..." if len(sources) > 6 else "")) if sources else "various"
                return f"{n} chunk{'s' if n != 1 else ''} stored, from: {src_str}."
            return "Knowledge base isn't available right now."

        # ── proactive trigger: "every day at 8am [do X]" ──
        daily_m = re.search(
            r'\bevery (?:day|morning|night|evening|afternoon)\s+at\s+'
            r'([\d]{1,2}(?::\d{2})?\s*(?:am|pm)?)\s+(.+)',
            text, re.I,
        )
        if daily_m:
            raw_time = daily_m.group(1).strip()
            action = daily_m.group(2).strip().rstrip(".")
            hhmm = _parse_time_to_24h(raw_time)
            if hhmm:
                try:
                    from core.proactive import add_trigger
                    add_trigger(
                        description=f"daily at {raw_time}",
                        schedule_type="daily_at",
                        schedule_value=hhmm,
                        action_text=action,
                    )
                    return f"Got it — I'll say '{action}' every day at {raw_time}."
                except Exception as e:
                    print(f"[proactive] add_trigger failed: {e}")
                    return "Couldn't save that trigger."
            return f"Couldn't parse '{raw_time}' as a time."

        # ── proactive trigger: "every Monday at 9am [do X]" ──
        weekday_m = re.search(
            r'\bevery\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+at\s+'
            r'([\d]{1,2}(?::\d{2})?\s*(?:am|pm)?)\s+(.+)',
            text, re.I,
        )
        if weekday_m:
            day_name = weekday_m.group(1).strip()
            raw_time = weekday_m.group(2).strip()
            action = weekday_m.group(3).strip().rstrip(".")
            day_abbr = day_name[:3].upper()
            hhmm = _parse_time_to_24h(raw_time)
            if hhmm:
                try:
                    from core.proactive import add_trigger
                    add_trigger(
                        description=f"every {day_name} at {raw_time}",
                        schedule_type="weekday_at",
                        schedule_value=f"{day_abbr}:{hhmm}",
                        action_text=action,
                    )
                    return f"Got it — every {day_name.capitalize()} at {raw_time} I'll say '{action}'."
                except Exception as e:
                    print(f"[proactive] add_trigger failed: {e}")
                    return "Couldn't save that trigger."

        # ── proactive trigger: "every 30 minutes [do X]" ──
        interval_m = re.search(
            r'\bevery\s+(\d+)\s+minutes?\s+(.+)',
            text, re.I,
        )
        if interval_m:
            mins = interval_m.group(1)
            action = interval_m.group(2).strip().rstrip(".")
            try:
                from core.proactive import add_trigger
                add_trigger(
                    description=f"every {mins} minutes",
                    schedule_type="interval_mins",
                    schedule_value=mins,
                    action_text=action,
                )
                return f"Got it — every {mins} minutes I'll say '{action}'."
            except Exception as e:
                print(f"[proactive] add_trigger failed: {e}")
                return "Couldn't save that trigger."

        # ── list proactive triggers ──
        if re.search(
            r'\b(what proactive|list (?:my )?(?:scheduled|daily|proactive)|'
            r'what(?:\'s| are) my (?:scheduled|daily|proactive)|show (?:my )?triggers)\b',
            text, re.I,
        ):
            try:
                from core.proactive import list_triggers
                triggers = list_triggers()
                if not triggers:
                    return "No proactive triggers set. Try saying 'every day at 8am give me the weather'."
                parts = [t["description"] for t in triggers[:5]]
                return "Active triggers: " + "; ".join(parts) + "."
            except Exception:
                return "Proactive module unavailable."

        # ── snooze ──
        snooze_m = re.search(r'\bsnooze(?: for (.+))?\b', text, re.I)
        if snooze_m:
            if self._last_fired_timer is None:
                return "No recent timer to snooze."
            dur_str = snooze_m.group(1) or "5 minutes"
            secs = assistant.parse_duration(dur_str) or 300
            human = assistant.human_duration(secs)
            end_ts = time.time() + secs
            orig_text  = self._last_fired_timer.get("text", "Time's up.")
            orig_label = self._last_fired_timer.get("label")
            rid = assistant.add_reminder(orig_text, end_ts, kind="timer", label=orig_label)
            end_ms = int(end_ts * 1000)
            safe_label = human.replace("'", "\\'")
            js(self.window, f"tedHud.addTimer({rid}, {end_ms}, '{safe_label}')")
            return f"Snoozed for {human}."

        # ── cancel by label: "cancel the pasta timer" ──
        label_cancel_m = re.search(
            r'\bcancel (?:the |my )?(\w+) (?:timer|reminder|alarm)\b', text, re.I,
        )
        if label_cancel_m:
            lbl = label_cancel_m.group(1).lower()
            if lbl not in {"all", "every", "a", "an", "the"}:
                from core.assistant import cancel_by_label
                n = cancel_by_label(lbl)
                if n:
                    js(self.window, "tedHud.clearTimer()")
                    return f"Cancelled the {lbl} timer."
                return f"No {lbl} timer found."

        # ── calendar ──
        if features.HAS_CALENDAR and re.search(
            r'\bwhat(?:\'s| is| do i have)(?:.{0,20})calendar today'
            r'|\bwhat do i have today\b|\btoday(?:\'s)? (?:schedule|events|calendar)\b',
            text, re.I,
        ):
            events = features.calendar.get_today_events()
            if not events:
                return "Nothing on your calendar today."
            return f"Today you have {len(events)} event{'s' if len(events) != 1 else ''}: {features.calendar.format_events_for_speech(events)}"

        if features.HAS_CALENDAR and re.search(
            r'\bwhat do i have tomorrow\b|\btomorrow(?:\'s)? (?:schedule|events|calendar)\b',
            text, re.I,
        ):
            events = features.calendar.get_tomorrow_events()
            if not events:
                return "Nothing on your calendar tomorrow."
            return f"Tomorrow you have {len(events)} event{'s' if len(events) != 1 else ''}: {features.calendar.format_events_for_speech(events)}"

        if features.HAS_CALENDAR and re.search(
            r'\bthis week(?:\'s)? (?:schedule|events|calendar)\b'
            r'|\bcalendar this week\b|\bwhat do i have this week\b',
            text, re.I,
        ):
            events = features.calendar.get_week_events()
            if not events:
                return "Nothing on your calendar this week."
            return f"This week: {features.calendar.format_events_for_speech(events)}"

        if features.HAS_CALENDAR and re.search(
            r'\b(?:what(?:\'s| is) my next (?:event|meeting)|next (?:event|meeting|appointment))\b',
            text, re.I,
        ):
            evt = features.calendar.get_next_event()
            if not evt:
                return "Nothing coming up on your calendar."
            title = evt["title"]
            start = evt.get("start_dt")
            if start:
                time_str = start.strftime("%-I:%M %p on %A")
                return f"Next up: {title} at {time_str}."
            return f"Next up: {title}."

        if features.HAS_CALENDAR:
            cal_add_m = re.search(
                r'\b(?:add|schedule|put|create) (?:a |an )?(?:meeting|event|appointment|call)?\s*'
                r'(?:called |titled |named )?["\']?(.+?)["\']? (?:at|on|for) (.+)',
                text, re.I,
            )
            if cal_add_m:
                title    = cal_add_m.group(1).strip().rstrip(",")
                when_str = cal_add_m.group(2).strip()
                due = assistant.parse_when(when_str)
                if due:
                    start_dt = _dt_cls.fromtimestamp(due)
                    result = features.calendar.add_event(title, start_dt)
                    return result
                return f"Couldn't parse '{when_str}' as a time."

        # ── Apple Notes ──
        if features.HAS_NOTES:
            note_add_m = re.search(
                r'\b(?:add|make|write|create|take) (?:a |an )?note[:\-,]?\s*(.+)',
                text, re.I,
            )
            if note_add_m:
                body = note_add_m.group(1).strip()
                # Use first few words as title
                words = body.split()
                title = " ".join(words[:5]).rstrip(",.:") if words else body
                return features.notes.add_note(title, body)

            note_read_m = re.search(
                r'\b(?:read|get|open|show) (?:my )?note (?:about|on|for) (.+)',
                text, re.I,
            )
            if note_read_m:
                query = note_read_m.group(1).strip().rstrip("?.")
                content = features.notes.get_note(query)
                if content:
                    snippet = content[:400].strip()
                    return f"{snippet}{'...' if len(content) > 400 else ''}"
                return f"No note found matching '{query}'."

            note_append_m = re.search(
                r'\b(?:append|add) to (?:my )?(.+?) note[:\-,]?\s*(.+)',
                text, re.I,
            )
            if note_append_m:
                note_title = note_append_m.group(1).strip()
                addition   = note_append_m.group(2).strip()
                return features.notes.append_to_note(note_title, addition)

        # ── Clipboard ──
        import subprocess as _subp
        if re.search(
            r'\bwhat(?:\'s| is) in (?:my )?clipboard\b|\bread (?:the )?clipboard\b'
            r'|\bwhat(?:\'s| is| did i| have i) (?:in my |just )?cop(?:ied|y)\b',
            text, re.I,
        ):
            clip = _subp.run(["pbpaste"], capture_output=True, text=True).stdout.strip()
            if not clip:
                return "Clipboard is empty."
            snippet = clip[:300].rstrip()
            return f"Clipboard says: {snippet}{'...' if len(clip) > 300 else ''}"

        if re.search(
            r'\bcopy (?:that|last (?:reply|response)|it) to (?:my )?clipboard\b',
            text, re.I,
        ):
            content = self.last_reply or ""
            if content:
                _subp.run(["pbcopy"], input=content, text=True)
                return "Copied."
            return "Nothing to copy yet."

        if re.search(
            r'\b(?:add|save) (?:clipboard|what I (?:just )?copied) to (?:my )?knowledge\b'
            r'|\bremember what I (?:just )?copied\b',
            text, re.I,
        ):
            clip = _subp.run(["pbpaste"], capture_output=True, text=True).stdout.strip()
            if clip and features.HAS_KNOWLEDGE:
                n = features.knowledge.add_text(clip, source="clipboard")
                return f"Saved {n} chunk{'s' if n != 1 else ''} from clipboard."
            return "Clipboard is empty." if not clip else "Knowledge base unavailable."

        # ── System volume ──
        vol_set_m = re.search(
            r'\b(?:set |change )?(?:system|computer) volume to (\d+)\b', text, re.I,
        )
        if vol_set_m:
            level = max(0, min(100, int(vol_set_m.group(1))))
            return control_system_volume("set", level)

        if re.search(r'\b(?:system|computer) volume (?:up|louder|higher)\b', text, re.I):
            return control_system_volume("up")

        if re.search(r'\b(?:system|computer) volume (?:down|lower|quieter)\b', text, re.I):
            return control_system_volume("down")

        if re.search(r'\bmute (?:the )?(?:computer|system|audio|sound)\b', text, re.I):
            return control_system_volume("mute")

        if re.search(r'\bunmute (?:the )?(?:computer|system|audio|sound)\b', text, re.I):
            return control_system_volume("unmute")

        # ── Brightness ──
        if re.search(
            r'\b(?:increase|turn up|raise|crank up|brighten) (?:the )?(?:screen )?brightness\b',
            text, re.I,
        ):
            for _ in range(3):
                _subp.run(["osascript", "-e",
                           'tell application "System Events" to key code 144'],
                          capture_output=True)
            return "Brightness up."

        if re.search(
            r'\b(?:decrease|turn down|lower|dim|reduce) (?:the )?(?:screen )?brightness\b',
            text, re.I,
        ):
            for _ in range(3):
                _subp.run(["osascript", "-e",
                           'tell application "System Events" to key code 145'],
                          capture_output=True)
            return "Brightness down."

        # ── Computer control ──
        if features.HAS_COMPUTER:
            type_m = re.search(r'\btype (?:this[:\-]? )?(.+)', text, re.I)
            if type_m:
                to_type = type_m.group(1).strip()
                return features.computer.type_text(to_type)

            press_m = re.search(
                r'\bpress (enter|return|escape|esc|tab|space|delete|backspace|'
                r'copy|paste|cut|undo|redo|save|select all)\b',
                text, re.I,
            )
            if press_m:
                return features.computer.press_key(press_m.group(1))

            if re.search(
                r'\bwhat(?:\'s| is) (?:the )?(?:focused|active|current) app\b'
                r'|\bwhat app am i (?:in|using|on)\b',
                text, re.I,
            ):
                app_name = features.computer.get_focused_app()
                return f"You're in {app_name}."

        # ── Screen awareness ──
        if re.search(
            r'\bwhat(?:\'s| is) on (?:my )?screen\b|\bdescribe (?:my )?screen\b'
            r'|\bread (?:my )?screen\b|\bwhat(?:\'s| is) on the screen\b',
            text, re.I,
        ):
            if features.HAS_SCREEN:
                set_state(self.window, "thinking")
                return features.screen.describe_screen("Briefly describe what's on the screen.")
            return "Screen module unavailable."

        screen_q_m = re.search(
            r'\bwhat does (?:this|the screen) say\b|\bread (?:this|the text on screen)\b',
            text, re.I,
        )
        if screen_q_m and features.HAS_SCREEN:
            set_state(self.window, "thinking")
            return features.screen.describe_screen("Read all visible text on the screen.")

        # ── Habits ──
        habit_log_m = re.search(
            r'\bI (?:just |did |)?'
            r'(worked out|ran|exercised|meditated|journaled|read|studied|stretched|walked|cycled)\b',
            text, re.I,
        )
        if habit_log_m:
            habit_name = habit_log_m.group(1).lower()
            is_new = log_habit(habit_name)
            info = get_habit_streak(habit_name)
            streak = info["streak"] if info else 1
            if is_new:
                return f"Tracked. {streak}-day {habit_name} streak."
            return f"{habit_name.capitalize()} already logged today. {streak}-day streak."

        habit_log_custom_m = re.search(
            r'\blog that I (.+?) today\b|\bI did (.+?) today\b', text, re.I,
        )
        if habit_log_custom_m:
            habit_name = (habit_log_custom_m.group(1) or habit_log_custom_m.group(2)).strip().lower()
            is_new = log_habit(habit_name)
            info = get_habit_streak(habit_name)
            streak = info["streak"] if info else 1
            if is_new:
                return f"Tracked {habit_name}. {streak}-day streak."
            return f"{habit_name.capitalize()} already logged today."

        habit_streak_m = re.search(
            r'\bwhat(?:\'s| is) my (.+?) streak\b|\bhow many days (?:have i|in a row).{0,20}(.+)\b',
            text, re.I,
        )
        if habit_streak_m:
            habit_name = (habit_streak_m.group(1) or habit_streak_m.group(2) or "").strip().lower()
            info = get_habit_streak(habit_name)
            if info and info["streak"] > 0:
                return f"{info['streak']}-day {habit_name} streak."
            if info:
                return f"No active {habit_name} streak right now."
            return f"No habit data for {habit_name} yet."

        if re.search(
            r'\b(?:show|list|what are) (?:my )?habits\b'
            r'|\bhow(?:\'s| is) my habit\b|\bmy habit (?:stats?|streaks?)\b',
            text, re.I,
        ):
            habits = get_all_habits()
            if not habits:
                return "No habits tracked yet. Say 'I worked out today' to start one."
            parts = [f"{h['name']} ({h['streak']} day{'s' if h['streak'] != 1 else ''})" for h in habits[:6]]
            return "Habits: " + ", ".join(parts) + "."

        # ── Knowledge inbox ──
        if features.HAS_KNOWLEDGE and re.search(
            r'\b(?:index|scan) (?:my )?(?:documents?|inbox|files?)\b'
            r'|\bscan (?:my )?inbox\b|\bindex (?:my )?inbox\b',
            text, re.I,
        ):
            result = features.knowledge.index_inbox()
            n = result["indexed"]
            c = result["total_chunks"]
            if n == 0:
                return f"No new files in inbox. ({result['skipped']} already indexed.)"
            return f"Indexed {n} file{'s' if n != 1 else ''}, {c} chunks added."

        if features.HAS_KNOWLEDGE and re.search(
            r'\bwhat(?:\'s| have you) indexed\b|\bwhat files (?:do you know about|have you (?:read|indexed))\b',
            text, re.I,
        ):
            files = features.knowledge.list_indexed_files()
            if not files:
                return "No files indexed yet. Drop files in ~/ted-ai/inbox/ and say 'index my documents'."
            names = ", ".join(files[:8])
            return f"Indexed files: {names}."

        return None

    def _try_tools(self, text):
        """LEGACY (TED_LEGACY_LADDER=1 only). The two-call tool path.

        Superseded by the single streamed call in llm.ask_streaming, which
        carries the tool schemas itself. Kept only as a revert switch; delete
        this method once the single-call path has run on real hardware for a
        while. Nothing but the legacy branch in _respond calls it.

        Agentic tool loop — up to 3 rounds of LLM → execute → feed back.
        When the LLM stops calling tools it synthesizes a final spoken reply.
        Returns that reply string, or None to fall through to the streaming LLM.

        The old likely_command() keyword gate is gone: it only let the model
        reason about turns containing a hardcoded verb, which meant any novel
        phrasing was locked out of every tool. Now EVERY turn gets a cheap
        round-1 look; if the model doesn't reach for a tool we return None
        immediately and the streaming path answers with full chat quality —
        so conversation costs one fast extra call, and nothing is gated."""
        MAX_ROUNDS = 3
        # stable_window (not [-8:]) so the probe's prompt prefix stays
        # cacheable — see llm.stable_window for why this matters for speed.
        history = llm.stable_window(
            [m for m in self.active_conversation if m["role"] != "system"], 8)
        # The tool loop used to run blind to what Ted knows about the user, so
        # "open YouTube in Brave from now on" was stored as a fact and then
        # ignored at the moment it mattered. Facts are cached in-process and
        # change rarely, so they sit in the stable prefix.
        try:
            _facts = get_facts_about(OWNER_NAME)
        except Exception:
            _facts = ""
        messages = [
            {"role": "system", "content": (
                # Deliberately NOT the full persona: the tool loop only needs to
                # pick a tool, and every token here is re-read on the probe that
                # runs before EVERY reply. The short version keeps that fast.
                f"You are Ted, {OWNER_NAME}'s assistant, choosing tools. "
                "Input may contain speech-recognition errors or unusual phrasing — "
                "interpret intent over literal words. "
                "Honour the user's stated preferences below: if they've said they "
                "want a site opened in a particular browser, pass that browser. "
                "If a detail a tool needs is missing, pick the reasonable default "
                "and proceed. Never stall. If no tool fits, don't force one."
                + (f"\nKnown preferences: {_facts}" if _facts else "")
            )},
            *history,
            {"role": "user", "content": text},
        ]

        import groq as _groq_mod
        _malformed_retry_used = False
        round_num = 0
        while round_num < MAX_ROUNDS:
            round_num += 1
            # Round 1 is a cheap PROBE: we only need to know whether a tool
            # fires. Without the escape hatch below, conversational turns made
            # the model compose a full (discarded) answer here before the real
            # streaming reply — which doubled response time for plain chat.
            probe = (round_num == 1)
            call_messages = messages if not probe else messages + [{
                "role": "system",
                "content": ("If this turn needs no tool, reply with exactly "
                            "the single word CHAT and nothing else."),
            }]
            try:
                _t_probe = time.time()
                resp = llm.chat_create(
                    messages=call_messages,
                    tools=TOOL_SCHEMAS,
                    tool_choice="auto",
                    # 120 covers any tool call's JSON arguments while keeping
                    # a stray conversational answer cheap to throw away.
                    max_tokens=120 if probe else 300,
                    temperature=0.1,
                    # A slow probe delays EVERY reply — fail fast and let the
                    # streaming path answer instead of making the user wait.
                    timeout=6.0 if probe else 12.0,
                )
                if probe:
                    print(f"[timing] tool probe {int((time.time() - _t_probe) * 1000)}ms")
            except _groq_mod.RateLimitError:
                print("[tools] rate limited — skipping tool path")
                return None
            except Exception as e:
                # The model sometimes emits a syntactically broken function call
                # (Groq returns 400 tool_use_failed). One clean retry usually
                # produces a valid call instead of dropping to plain conversation.
                if "tool_use_failed" in str(e) and not _malformed_retry_used:
                    _malformed_retry_used = True
                    round_num -= 1          # retry doesn't consume a round
                    print("[tools] malformed tool call from model — retrying once")
                    continue
                print(f"[tools] round {round_num} error: {e}")
                return None

            msg = resp.choices[0].message

            # No tool calls → LLM has synthesized the final answer
            if not msg.tool_calls:
                # Round 1 with no tool call = this turn is conversation, not a
                # command. Fall through so the streaming path answers it with
                # proper mode-aware length/formatting instead of this loop's
                # clipped 300-token non-streamed reply.
                if round_num == 1:
                    return None
                final = (msg.content or "").strip()
                if not final:
                    return None  # fall through to streaming LLM
                # Guard: models sometimes echo the tool name as text instead of
                # calling it. Treat a bare tool name as a failed turn.
                _tool_names = {t["function"]["name"] for t in TOOL_SCHEMAS}
                if final.lower().replace(" ", "_") in _tool_names:
                    print(f"[tools] model echoed tool name '{final}' — falling through")
                    return None
                self.active_conversation.append({"role": "user",      "content": text})
                self.active_conversation.append({"role": "assistant", "content": final})
                if len(self.active_conversation) > 42:
                    self.active_conversation = (
                        [self.active_conversation[0]] + self.active_conversation[-40:]
                    )
                return final

            # Append the assistant's tool-call turn to the running message chain
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            })

            # [BOOK §36.2] Say what this round intends BEFORE doing any of it.
            # The launch log used to show tool calls one at a time with no
            # record of the intent that spawned them, which is why one "clean
            # up" read as four unrelated closes. The plan is also what draws
            # the header of the thought bubble; without announce() the HUD
            # shows agent lines with nothing above them.
            _calls = []
            for tc in msg.tool_calls:
                try:
                    _calls.append((tc, json.loads(tc.function.arguments)))
                except Exception:
                    _calls.append((tc, {}))
            _plan = Plan(
                heard=text,
                steps=[Delegation(
                    "MacAgent" if (tc.function.name in MacAgent.TOOLS
                                   or tc.function.name == "clean_up")
                    else "direct",
                    tc.function.name, dict(a))
                    for tc, a in _calls],
                parallel=False,
            ).announce()
            print(f"[plan] {' | '.join(d.agent + '.' + d.method for d in _plan.steps)}")

            # Execute each tool and append its result
            _round_results = []
            _all_actions = True
            for tc, args in _calls:
                result = self._dispatch_tool(tc.function.name, args,
                                             plan_id=_plan.id)
                if result is None:
                    # A None here means the handler crashed or the tool is unknown.
                    # Never turn that into a cheerful "Done." — report the truth.
                    result = "That didn't go through — something failed on my end."
                print(f"[tools] {tc.function.name}({args}) → {result[:80]}")
                _round_results.append(result)
                if tc.function.name not in th.ACTION_TOOLS:
                    _all_actions = False
                elif th.looks_like_failure(result):
                    # A real action failed — surface the actual reason on the HUD.
                    show_issue(self.window, result)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

            # ACTION tools report ground truth. Speak their result verbatim and STOP —
            # never let the LLM take another round to re-narrate (that's where it turns
            # "Spotify isn't open" into a cheerful fake "Playing your music!").
            if _all_actions and _round_results:
                final = " ".join(_round_results)
                self.active_conversation.append({"role": "user",      "content": text})
                self.active_conversation.append({"role": "assistant", "content": final})
                if len(self.active_conversation) > 42:
                    self.active_conversation = (
                        [self.active_conversation[0]] + self.active_conversation[-40:]
                    )
                return final

        # Exceeded max rounds — speak the last batch of tool results directly
        tool_results = [m["content"] for m in messages if m.get("role") == "tool"]
        fallback = " ".join(tool_results[-3:]) if tool_results else None
        if fallback:
            self.active_conversation.append({"role": "user",      "content": text})
            self.active_conversation.append({"role": "assistant", "content": fallback})
        return fallback

    def _record_action(self, name, args, result):
        """Remember compact structured action ground truth for later references."""
        self._recent_actions.append({
            "tool": name,
            "args": dict(args or {}),
            "result": str(result or ""),
            "ts": time.time(),
        })
        self._recent_actions = self._recent_actions[-8:]

    def _dispatch_and_record(self, name, args, confirmed=False):
        result = self._dispatch_tool(name, args, confirmed=confirmed)
        # A screen read is not an action and never enters _recent_actions —
        # correctly, since that list drives pronoun resolution and is capped at
        # eight, and a terminal dump would evict every real action in it. But
        # it IS the evidence that says whether something is waiting for an
        # answer, so it gets its own slot. Truncated: only the tail of a
        # terminal matters, and this is read on every turn.
        if name in routing.OBSERVATION_TOOLS:
            self._last_screen = str(result or "")[-2000:]
            observation_failed = bool(re.match(
                r"^(?:i couldn'?t|could not|accessibility permission|"
                r"computer control unavailable|no readable|no terminal output)",
                str(result or "").strip(), re.I))
            task_state.record_observation(
                getattr(self, "_active_task_id", None), name, args, result,
                failed=observation_failed)
        # Consequential tools have not acted when they merely arm confirmation.
        acted = (name in th.ACTION_TOOLS
                 and (confirmed or not th.needs_confirmation(name, args)))
        if acted:
            self._record_action(name, args, result)
            task_state.record_action(
                getattr(self, "_active_task_id", None), name,
                outcomes.normalize(name, args, result,
                                   is_failure=th.looks_like_failure, acted=True))
        elif name in th.ACTION_TOOLS:
            task_state.mark_waiting(getattr(self, "_active_task_id", None))
        return result

    def _execute_reflex(self, plan):
        """Run independent reversible app calls concurrently and preserve order."""
        calls = list(plan.calls)

        def _run(call):
            name, args = call
            return self._dispatch_tool(name, args)

        if len(calls) == 1:
            results = [_run(calls[0])]
        else:
            with ThreadPoolExecutor(max_workers=min(4, len(calls))) as pool:
                results = list(pool.map(_run, calls))
        for (name, args), result in zip(calls, results):
            self._record_action(name, args, result)
            task_state.record_action(
                getattr(self, "_active_task_id", None), name,
                outcomes.normalize(name, args, result,
                                   is_failure=th.looks_like_failure, acted=True))
        return results

    def _execute_routine(self, routine):
        """Execute a dashboard-authored routine and preserve configured order."""
        steps = list(routine.get("steps") or [])

        def _run(step):
            return self._dispatch_and_record(step["tool"], step.get("args") or {})

        # Keystrokes, scrolls and semantic presses act on whichever app is
        # frontmost, so they must never race the app/browser launch that
        # establishes that state. Other listed routine actions are independent
        # and intentionally safe to fan out.
        can_parallel = (routine.get("parallel") and len(steps) > 1
                        and not any(step["tool"] in routines.FOCUS_DEPENDENT_ACTIONS
                                    for step in steps))
        if can_parallel:
            with ThreadPoolExecutor(max_workers=min(4, len(steps))) as pool:
                results = list(pool.map(_run, steps))
        else:
            results = [_run(step) for step in steps]
        try:
            routines.note_run(routine["id"])
        except Exception as exc:
            print(f"[routines] couldn't update run count: {exc}")
        return [str(result or "That action did not return a result.") for result in results]

    def _create_document_workflow(self, args):
        """Draft prose first, then perform one deterministic editor workflow."""
        instructions = str(args.get("instructions") or "").strip()
        if not instructions:
            return "I need to know what the document should say, so nothing was created."
        draft = llm.generate_document_draft(
            instructions, args.get("target_words") or 600)
        if not draft:
            return "I couldn't draft the document, so I didn't open an empty file."
        if not features.HAS_COMPUTER:
            return "I drafted the document, but computer control is unavailable, so nothing was opened."
        return features.computer.create_document(
            draft,
            args.get("app", "google_docs"),
            args.get("browser", "Chrome"),
            font_size=args.get("font_size"),
            line_spacing=args.get("line_spacing"),
        )

    # [BOOK §11.6] ─── THE SWITCHBOARD ───────────────────────────────────────
    #
    # The model has chosen a tool by NAME and supplied ARGUMENTS as a
    # dictionary. This method turns that into something actually happening.
    #
    # It is one very long chain of `if name == "...":` branches. That is not
    # elegant and it does not need to be — it is a lookup table written in
    # if-statements, and its only job is to be complete and obvious.
    #
    # WHEN YOU ADD A TOOL, THIS IS THE STEP PEOPLE FORGET. A schema in
    # core/tools.py with no branch here means the model calls something that
    # silently does nothing. Chapter 31 walks the full three-step process.
    #
    # Two rules every branch follows:
    #   * Read arguments with args.get("x", ""), never args["x"] — anything not
    #     in the schema's "required" list may simply be absent.
    #   * Return the SENTENCE Ted will say, and let it be the truth. Do not
    #     return "Done" because the call did not raise. (§11.8)
    def _dispatch_tool(self, name, args, confirmed=False, plan_id=None,
                       _from_agent=False):
        """Route a tool call from the LLM to the right Python handler.
        Returns a spoken-style result string; on any error returns an honest
        failure message (never None-→-"Done.")."""
        # [BOOK §36.4] Mac work belongs to MacAgent now. The agent owns the task
        # and reports one AgentResult with evidence; the branches below still
        # own the individual handlers, and the agent calls back into them.
        #
        # _from_agent is what stops that callback re-entering here as a fresh
        # delegation. It is not a style choice — without it this recurses.
        if not _from_agent and (name in MacAgent.TOOLS or name == "clean_up"):
            js(self.window, f"tedHud.noteAppUse({json.dumps(name)})")
            # Not every door into this method comes from the tool loop: the
            # reflex path and the "yes, do it" confirmation path both arrive
            # with no plan. The HUD drops any agent event whose plan_id it has
            # never seen, so without this the thought bubble would silently
            # skip exactly the runs the user is watching for.
            if plan_id is None:
                plan_id = Plan(
                    heard=self._cur_user_text or name,
                    steps=[Delegation("MacAgent", name, dict(args or {}))],
                ).announce().id
                print(f"[plan] MacAgent.{name}")
            # th.needs_confirmation is the one function BOTH gates ask (§11.7).
            # Asking it here too is what stops the agent path from quietly
            # dropping a gate the handler path still enforces. The agent's own
            # ConfirmationGate stays unused for now: it blocks the turn waiting
            # on a click, and Ted's established flow is to ask, return, and let
            # the next turn answer. Two confirmation models is the duplicated
            # judgment bug again — pick this one.
            if th.needs_confirmation(name, args) and not confirmed:
                preview = self._run_agent(self.mac_agent, name, args,
                                          plan_id=plan_id, dry_run=True)
                self._pending_tool_confirmation = {
                    "name": name, "args": dict(args),
                    "expires": time.time() + 60,
                }
                would = preview.evidence.get("would_close") or []
                if not would:
                    self._pending_tool_confirmation = None
                    return "Nothing of yours is open, so there is nothing to close."
                return (f"That would close {', '.join(would)}. "
                        "Say yes to do it, or anything else to cancel.")
            result = self._run_agent(self.mac_agent, name, args,
                                     plan_id=plan_id)
            return self._agent_reply(result)
        try:
            # Surface tool use in the HUD's connected-apps panel (best-effort)
            js(self.window, f"tedHud.noteAppUse({json.dumps(name)})")
            # Collect required human content before arming confirmation. The old
            # order asked "what should I say?" while a yes/no confirmation was
            # already pending, so the user's answer was interpreted as "no" and
            # canceled the message it was meant to fill.
            if (name == "send_message" and not confirmed
                    and not (args.get("text") or args.get("instruction"))):
                self._pending_compose = {
                    "type": "tool_message", "stage": "text",
                    "contact": args.get("contact", ""),
                }
                return (f"What exact message should I send to "
                        f"{args.get('contact', 'them')}?")
            if name == "send_message" and not confirmed and args.get("instruction"):
                # Compose first, then ask for consent to the actual bytes that
                # will be sent. The former flow asked Charlie to approve a
                # brief and promised to show the message later, then sent the
                # unseen generated wording immediately after “yes.”
                preview = llm.generate_message_with_style(
                    args.get("instruction", ""), args.get("contact", ""),
                    args.get("style") or "natural and casual")
                if not preview or not preview.strip():
                    return "I couldn't draft that message, so nothing is ready to send."
                args = dict(args)
                args["text"] = preview.strip()
                args.pop("instruction", None)
                args.pop("style", None)
            if th.needs_confirmation(name, args) and not confirmed:
                self._pending_tool_confirmation = {
                    "name": name, "args": dict(args), "expires": time.time() + 60,
                }
                if name == "send_message":
                    target = args.get("contact", "that contact")
                    # Show what is about to be sent. Confirming a message you
                    # cannot see is not consent — the user asked "what message
                    # were you going to send? I didn't tell you what to send"
                    # after being asked to approve a blank one.
                    body = (args.get("text") or "").strip()
                    if body:
                        return (f"Ready to send {target}: \u201c{body}\u201d "
                                "Say yes to send it, or anything else to cancel.")
                    return (f"I don't have anything to say to {target} yet. "
                            "What do you want the message to be?")
                if name == "send_email":
                    target = args.get("to", "that address")
                    return f"Ready to email {target}. Say yes to send it, or anything else to cancel."
                if name == "code_write":
                    # Name the file, its size now, and the size it would become.
                    # "Say yes to let me edit myself" is not consent to anything
                    # in particular; the numbers are what make it a real choice.
                    path = args.get("path", "a file")
                    rel, error = codebase.resolve(path)
                    if error:
                        self._pending_tool_confirmation = None
                        return f"I can't change that: {error}."
                    new_len = len(args.get("content", "") or "")
                    full = os.path.join(codebase.ROOT, rel)
                    old_len = os.path.getsize(full) if os.path.isfile(full) else 0
                    what = "rewrite" if old_len else "create"
                    reason = (args.get("reason") or "").strip()
                    detail = f" ({reason})" if reason else ""
                    size = (f"{old_len:,} → {new_len:,} characters" if old_len
                            else f"{new_len:,} characters")
                    return (f"I want to {what} my own {rel}{detail}: {size}. "
                            f"I'll keep a backup of the old version. "
                            f"Say yes to let me, or anything else to cancel.")
                if name == "notebook_delete":
                    # Deleting a page is the one notebook call that cannot be
                    # undone by writing the entry again, so it names the cost:
                    # how many entries are about to go.
                    doc = notebook.read_page(args.get("page", ""))
                    if doc is None:
                        self._pending_tool_confirmation = None
                        return f"There's no notebook page called '{args.get('page', '')}'."
                    n = doc["total"]
                    return (f"That deletes the whole '{doc['name']}' page and the {n} "
                            + ("entry" if n == 1 else "entries")
                            + " on it. Say yes to do it, or anything else to cancel.")
                action = args.get("action", "change")
                return f"Ready to {action.replace('_', ' ')} that email. Say yes to confirm, or anything else to cancel."
            if name == "web_search":
                result = llm.search_web(args.get("query", ""))
                if result == "__NO_RESULTS__":
                    return "No useful web results were found for that query."
                if result == "__SEARCH_ERROR__":
                    return "Live web search is unavailable right now."
                return result
            if name == "bouncer_watch":
                rule, error = bouncer.allow(args.get("who", ""),
                                            args.get("mode", "announce"))
                if error:
                    return f"I didn't change the list — {error}."
                if rule["mode"] == "ignore":
                    return f"I'll stay quiet about texts from {rule['pattern']}."
                started = self._ensure_bouncer_running()
                return (f"I'll tell you when {rule['pattern']} texts you." + started)
            if name == "bouncer_status":
                lines = [bouncer.describe_rules()]
                ok, reason = messages.available()
                if not ok:
                    lines.append(f"I can't actually read your messages yet: {reason}")
                elif self._bouncer_blocked:
                    lines.append("Access works now, but the watcher isn't running "
                                 "— restart Ted to start it.")
                return " ".join(lines)
            if name == "bouncer_toggle":
                want = bool(args.get("on"))
                bouncer.set_enabled(want)
                if not want:
                    return "Bouncer off. I won't mention incoming texts."
                ok, reason = messages.available()
                if not ok:
                    return f"Bouncer on, but I still can't read your messages: {reason}"
                return "Bouncer on." + self._ensure_bouncer_running()
            if name == "text_respond":
                if args.get("action") == "open":
                    return self.open_pending_text()
                return self.read_pending_text()
            if name == "code_overview":
                return codebase.overview()
            if name == "code_search":
                return codebase.search(args.get("query", ""))
            if name == "code_read":
                return codebase.read(args.get("path", ""),
                                     args.get("start", 1), args.get("end", 0))
            if name == "code_tree":
                return codebase.tree(args.get("subdir", ""))
            if name == "code_history":
                return codebase.history(args.get("path", ""), args.get("count", 8))
            if name == "code_diff":
                return codebase.diff(args.get("path", ""))
            if name == "code_write":
                # Only reachable with confirmed=True, because code_write is in
                # CONFIRMATION_TOOLS and the gate above returns first otherwise.
                # codebase.write refuses again on its own, which is deliberate
                # belt-and-braces: the rule Charlie set is that Ted never edits
                # himself unasked, and one gate is one mistake away from gone.
                return codebase.write(args.get("path", ""),
                                      args.get("content", ""),
                                      confirmed=confirmed)
            if name == "show_image":
                return self._show_images(args.get("query", ""),
                                         args.get("count", 3))
            if name == "open_app":
                return th.tool_open_app(args.get("name", ""))
            if name == "close_app":
                return close_app(args.get("name", ""))
            if name == "browse_to":
                return th.tool_browse_to(
                    args.get("site", ""), args.get("browser"),
                    args.get("new_window", False))
            if name == "play_youtube":
                return th.tool_play_youtube(
                    args.get("query", ""), args.get("browser"))
            if name == "play_music":
                return features.spotify_web.play_track(args.get("query", ""), args.get("artist"))
            if name == "play_playlist":
                return features.spotify_web.play_playlist(args.get("name", ""), args.get("shuffle", False))
            if name == "spotify_control":
                return th.tool_spotify_control(args.get("action", ""))
            if name == "now_playing":
                return th.tool_now_playing()
            if name == "search_chats":
                return th.tool_search_chats(args.get("query", ""), args.get("limit", 6))
            if name == "add_to_playlist":
                # track omitted = whatever is playing; that is the common ask.
                return features.spotify_web.add_to_playlist(
                    args.get("playlist", ""), args.get("track") or None)
            if name == "remove_from_playlist":
                return features.spotify_web.remove_from_playlist(
                    args.get("playlist", ""), args.get("track") or None)
            if name == "create_playlist":
                return features.spotify_web.create_playlist(
                    args.get("name", ""), args.get("public", False),
                    args.get("description", ""))
            if name == "delete_playlist":
                return features.spotify_web.delete_playlist(args.get("name", ""))
            if name == "send_message":
                return self._compose_and_send(
                    args.get("contact", ""),
                    instruction=args.get("instruction"),
                    style=args.get("style"),
                    text=args.get("text"),
                )
            if name == "set_reminder":
                return th.tool_set_reminder(args.get("text", ""), args.get("when", ""))
            if name == "set_timer":
                return th.tool_set_timer(args.get("duration", ""))
            if name == "get_reminders":
                return th.tool_get_reminders()
            if name == "get_weather":
                return th.tool_get_weather()
            if name == "get_emails":
                return th.tool_get_emails(args.get("limit", 5))
            if name == "read_email":
                return th.tool_read_email(args.get("number", 1), args.get("mode", "summarized"))
            if name == "email_action":
                number = args.get("number", 1)
                action = args.get("action", "")
                reply_text = args.get("reply_text")
                if action == "reply" and not reply_text:
                    # Ask for reply content
                    from core.email import get_cached_email
                    meta = get_cached_email(number)
                    sender = meta["sender_name"] if meta else f"email {number}"
                    self._pending_compose = {
                        "type": "email_reply", "number": number,
                        "stage": "instruction", "style": None,
                    }
                    return f"What do you want to say to {sender}?"
                return th.tool_email_action(number, action, reply_text)
            if name == "send_email":
                return self._compose_and_send_email(
                    args.get("to", ""),
                    args.get("subject", ""),
                    args.get("instruction", ""),
                    args.get("style"),
                )
            if name == "search_knowledge":
                if features.HAS_KNOWLEDGE:
                    result = features.knowledge.search(args.get("query", ""), k=3)
                    return result or "Nothing stored on that topic yet."
                return "Knowledge base isn't available."
            if name == "add_knowledge":
                if features.HAS_KNOWLEDGE:
                    n = features.knowledge.add_text(
                        args.get("text", ""),
                        source=args.get("source", "voice"),
                    )
                    return f"Saved. {n} chunk{'s' if n != 1 else ''} stored."
                return "Knowledge base isn't available."

            # ── Calendar ──────────────────────────────────────────────────────
            if name == "calendar_get":
                if features.HAS_CALENDAR:
                    period = args.get("period", "today")
                    if period == "tomorrow":
                        events = features.calendar.get_tomorrow_events()
                    elif period == "week":
                        events = features.calendar.get_week_events()
                    elif period == "next":
                        evt = features.calendar.get_next_event()
                        events = [evt] if evt else []
                    else:
                        events = features.calendar.get_today_events()
                    return features.calendar.format_events_for_speech(events)
                return "Calendar module unavailable."

            if name == "calendar_add":
                if features.HAS_CALENDAR:
                    title    = args.get("title", "")
                    when_str = args.get("when", "")
                    due = assistant.parse_when(when_str) if when_str else None
                    if due:
                        start_dt = _dt_cls.fromtimestamp(due)
                        end_dt = None
                        if args.get("end"):
                            end_ts = assistant.parse_when(args["end"])
                            if end_ts:
                                end_dt = _dt_cls.fromtimestamp(end_ts)
                        return features.calendar.add_event(title, start_dt, end_dt,
                                                           notes=args.get("notes", ""))
                    return f"Couldn't parse '{when_str}' as a time."
                return "Calendar module unavailable."

            # ── Notes ─────────────────────────────────────────────────────────
            if name == "notes_add":
                if features.HAS_NOTES:
                    title = args.get("title", "")
                    body  = args.get("body",  "")
                    mode  = args.get("mode", "new")
                    if mode == "append":
                        return features.notes.append_to_note(title, body)
                    return features.notes.add_note(title, body)
                return "Notes module unavailable."

            if name == "notes_get":
                if features.HAS_NOTES:
                    query = args.get("query", "")
                    content = features.notes.get_note(query)
                    if content:
                        return content[:400] + ("..." if len(content) > 400 else "")
                    results = features.notes.search_notes(query)
                    if results:
                        titles = ", ".join(r["title"] for r in results[:3])
                        return f"Found notes: {titles}."
                    return f"No notes found matching '{query}'."
                return "Notes module unavailable."

            # ── Ted's notebook ───────────────────────────────────────────────
            # Ted's own pages, distinct from Apple Notes above. Every branch
            # reports exactly what landed — page name, entry number, the text —
            # because these are ACTION_TOOLS and their return value is spoken
            # verbatim. "Added it" with no number is the kind of vague success
            # report that later turns out to have written to the wrong page.
            if name == "notebook_read":
                page = (args.get("page") or "").strip()
                if not page:
                    pages = notebook.list_pages()
                    if not pages:
                        return ("Your notebook is empty — no pages yet. "
                                "Tell me what to start one about.")
                    return "Notebook pages: " + "; ".join(
                        f"{p['name']} ({p['entries']} "
                        + ("entry" if p["entries"] == 1 else "entries") + ")"
                        for p in pages) + "."
                try:
                    doc = notebook.read_page(page)
                except ValueError as e:
                    return str(e)
                if doc is None:
                    known = ", ".join(p["name"] for p in notebook.list_pages())
                    return (f"There's no notebook page called '{page}'."
                            + (f" You have: {known}." if known else
                               " Your notebook is empty."))
                if not doc["entries"]:
                    return f"'{doc['name']}' exists but nothing is written on it yet."
                lines = "\n".join(f"{e['number']}. {e['body']}" for e in doc["entries"])
                head = f"'{doc['name']}' ({doc['total']} "
                head += "entry" if doc["total"] == 1 else "entries"
                head += ", last written " + doc["updated"][:10] + "):"
                more = ("" if len(doc["entries"]) == doc["total"] else
                        f"\n(showing the last {len(doc['entries'])} of {doc['total']}.)")
                return f"{head}\n{lines}{more}"

            if name == "notebook_write":
                try:
                    page, number, made = notebook.add_entry(
                        args.get("page", ""), args.get("text", ""), writer="ted")
                except ValueError as e:
                    return f"I didn't write that down — {e}."
                opened = f"Started a new page '{page}' and wrote" if made else f"Wrote"
                return f"{opened} entry {number} on '{page}'."

            if name == "notebook_edit":
                try:
                    page, number = notebook.edit_entry(
                        args.get("page", ""), args.get("entry"),
                        args.get("text", ""), writer="ted")
                except (KeyError, ValueError) as e:
                    return f"I didn't change anything — {str(e).strip(chr(39))}."
                return f"Rewrote entry {number} on '{page}'."

            if name == "notebook_delete":
                entry = args.get("entry")
                try:
                    if entry in (None, ""):
                        # Only reachable with confirmed=True; needs_confirmation()
                        # sends the unconfirmed call to the pending-yes flow first.
                        page, count = notebook.delete_page(args.get("page", ""))
                        return (f"Deleted the page '{page}' and the {count} "
                                + ("entry" if count == 1 else "entries") + " on it.")
                    page, number, body = notebook.delete_entry(
                        args.get("page", ""), entry)
                    return f"Crossed out entry {number} on '{page}': \u201c{body}\u201d"
                except (KeyError, ValueError) as e:
                    return f"I didn't delete anything — {str(e).strip(chr(39))}."

            if name == "notebook_search":
                hits = notebook.search(args.get("query", ""))
                if not hits:
                    return f"Nothing in your notebook mentions '{args.get('query', '')}'."
                return "Found in your notebook: " + " | ".join(
                    f"{h['page']} — {h['body'][:160]}" for h in hits[:6])

            # ── Clipboard ────────────────────────────────────────────────────
            if name == "clipboard_read":
                import subprocess as _sp
                clip = _sp.run(["pbpaste"], capture_output=True, text=True).stdout.strip()
                return clip[:400] if clip else "Clipboard is empty."

            if name == "clipboard_write":
                import subprocess as _sp
                content = args.get("text", "")
                if content:
                    _sp.run(["pbcopy"], input=content, text=True)
                    return "Copied to clipboard."
                return "No text to copy."

            # ── System controls ──────────────────────────────────────────────
            if name == "system_volume":
                action = args.get("action", "get")
                level  = args.get("level")
                return control_system_volume(action, level)

            if name == "system_brightness":
                import subprocess as _sp
                action = args.get("action", "up")
                kc = 144 if action == "up" else 145
                for _ in range(3):
                    _sp.run(["osascript", "-e",
                             f'tell application "System Events" to key code {kc}'],
                            capture_output=True)
                return f"Brightness {'up' if action == 'up' else 'down'}."

            # ── Screen ───────────────────────────────────────────────────────
            if name == "screen_describe":
                if features.HAS_SCREEN:
                    question = args.get("question", "Briefly describe what's on the screen.")
                    return features.screen.describe_screen(question)
                return "Screen module unavailable."

            if name == "ui_inspect":
                if features.HAS_COMPUTER:
                    return features.computer.inspect_ui(args.get("query", ""))
                return "Computer module unavailable."

            if name == "terminal_read":
                if features.HAS_COMPUTER:
                    return features.computer.read_terminal()
                return "Computer module unavailable."

            if name == "ui_press":
                if features.HAS_COMPUTER:
                    return features.computer.press_target(
                        args.get("target", ""), args.get("expected", ""),
                        args.get("remember_as", ""), args.get("timeout", 12))
                return "Computer module unavailable."

            if name == "ui_fill":
                if features.HAS_COMPUTER:
                    return features.computer.fill_field(
                        args.get("target", ""), args.get("text", ""),
                        args.get("timeout", 12))
                return "Computer module unavailable."

            # ── Computer control ─────────────────────────────────────────────
            if name == "create_document":
                if features.HAS_COMPUTER:
                    return self._create_document_workflow(args)
                return "Computer module unavailable."
            if name == "learn_lingo":
                saved = lingo.remember(args.get("term", ""), args.get("meaning", ""))
                return f"Learned that “{saved['term']}” means “{saved['meaning']}.”"
            if name == "clarify_lingo":
                term = str(args.get("term") or "").strip()
                if not term:
                    return "Which term should I ask Charlie about?"
                self._pending_lingo = {"term": term, "expires": time.time() + 120}
                return f"What does “{term}” mean when you say it?"
            if name == "type_text":
                if features.HAS_COMPUTER:
                    return features.computer.type_text(args.get("text", ""))
                return "Computer module unavailable."
            if name == "press_key":
                if features.HAS_COMPUTER:
                    return features.computer.press_key(args.get("key", ""))
                return "Computer module unavailable."
            if name == "scroll":
                if features.HAS_COMPUTER:
                    return features.computer.scroll(
                        args.get("direction", "down"), args.get("amount", 600))
                return "Computer module unavailable."

            # ── Habits ───────────────────────────────────────────────────────
            if name == "log_habit":
                habit_name = args.get("name", "").lower()
                is_new = log_habit(habit_name)
                info   = get_habit_streak(habit_name)
                streak = info["streak"] if info else 1
                if is_new:
                    return f"Tracked {habit_name}. {streak}-day streak."
                return f"{habit_name.capitalize()} already logged today. {streak}-day streak."

            if name == "calculate":
                return th.tool_calculate(args.get("expression", ""))
            if name == "get_habit_streak":
                habit_name = args.get("name", "").lower()
                info = get_habit_streak(habit_name)
                if info and info["streak"] > 0:
                    return f"{info['streak']}-day {habit_name} streak."
                if info:
                    return f"No active {habit_name} streak right now."
                return f"No habit data for '{habit_name}' yet."

            return f"I don't have a tool called '{name}'."
        except Exception as e:
            print(f"[tools] dispatch error ({name}): {e}")
            return "That didn't work — something failed on my end."

    # ── Message / Email compose flow ──────────────────────────────────────────

    def _compose_and_send(self, contact, instruction=None, style=None, text=None):
        """Find the contact, then orchestrate the ask-style → generate → send flow.

        `text` is the user's own wording and is sent byte for byte. There used to
        be no way to express that: every message went through instruction →
        "how should it sound?" → a model rewrite, so asking Ted to send
        'hey this is ted' got you a question about the vibe and then something
        Ted wrote instead. Words the user typed are not a brief.
        """
        candidates = search_contacts(contact)
        if not candidates:
            return f"I couldn't find {contact.title()} in your contacts."

        if len(candidates) > 1:
            # Save compose intent separately — disambiguation uses _pending_msg alone
            self._pending_disambig_compose = {"instruction": instruction,
                                              "style": style, "text": text}
            self._pending_msg = (candidates, None, time.time() + 30)
            names = [c[0] for c in candidates]
            choices = " or ".join(names) if len(names) == 2 else ", ".join(names[:-1]) + f", or {names[-1]}"
            return f"I found a few — {choices}. Which one?"

        name, addr = candidates[0]
        return self._continue_compose(name, addr, instruction, style, text)

    def _continue_compose(self, name, addr, instruction, style, text=None):
        """Continue the compose flow once we have a confirmed contact."""
        if not addr:
            return f"I found {name} but they don't have a phone or email saved."
        # The user's own words: no style question, no rewrite, no improvement.
        if text and text.strip():
            ok = send_imessage_to_address(addr, text)
            first = name.split()[0]
            return f"Sent to {first}." if ok else f"Couldn't reach {name}."
        if not instruction:
            self._pending_compose = {
                "type": "imessage", "stage": "instruction",
                "contact_name": name, "contact_addr": addr, "style": style,
            }
            return f"What do you want to say to {name.split()[0]}?"
        if not style:
            self._pending_compose = {
                "type": "imessage", "stage": "style",
                "contact_name": name, "contact_addr": addr, "instruction": instruction,
            }
            return "How should it sound — casual, formal, short, funny? Describe the vibe."
        set_state(self.window, "thinking")
        msg = llm.generate_message_with_style(instruction, name, style)
        ok = send_imessage_to_address(addr, msg)
        return f"Sent to {name.split()[0]}." if ok else f"Couldn't reach {name}."

    def _compose_and_send_email(self, to, subject, instruction, style=None):
        """Orchestrate the ask-style → generate → send flow for email."""
        if not style:
            self._pending_compose = {
                "type": "email_send", "stage": "style",
                "to": to, "subject": subject, "instruction": instruction,
            }
            return "How do you want it to sound — professional, casual, brief, detailed?"
        set_state(self.window, "thinking")
        return th.tool_send_email_composed(to, subject, instruction, style)

    def _handle_pending_compose(self, text):
        """Process user's answer to a compose-flow question (style or instruction)."""
        pc = self._pending_compose
        kind = pc.get("type")

        if kind == "tool_message" and pc.get("stage") == "text":
            self._pending_compose = None
            return self._dispatch_tool("send_message", {
                "contact": pc.get("contact", ""),
                "text": text,
            })

        if kind == "imessage":
            if pc["stage"] == "instruction":
                pc["instruction"] = text
                pc["stage"] = "style"
                self._pending_compose = pc
                return "How should it sound — casual, formal, short, funny? Or just describe the vibe."

            if pc["stage"] == "style":
                self._pending_compose = None
                set_state(self.window, "thinking")
                msg = llm.generate_message_with_style(pc["instruction"], pc["contact_name"], text)
                ok = send_imessage_to_address(pc["contact_addr"], msg)
                first = pc["contact_name"].split()[0]
                return f"Sent to {first}." if ok else f"Couldn't reach {pc['contact_name']}."

        if kind == "email_reply":
            if pc["stage"] == "instruction":
                pc["instruction"] = text
                pc["stage"] = "style"
                self._pending_compose = pc
                return "How should the reply sound — casual, professional, brief?"
            if pc["stage"] == "style":
                self._pending_compose = None
                from core.email import reply_to_email, get_cached_email
                meta = get_cached_email(pc["number"])
                sender = meta["sender_name"] if meta else ""
                set_state(self.window, "thinking")
                body = llm.generate_email_body(pc["instruction"], sender, "", text)
                return reply_to_email(pc["number"], body)

        if kind == "email_send":
            if pc["stage"] == "style":
                self._pending_compose = None
                set_state(self.window, "thinking")
                return th.tool_send_email_composed(pc["to"], pc["subject"], pc["instruction"], text)

        self._pending_compose = None
        return None

    def _send_message_to_contact(self, contact, instruction=None, message=None):
        """Search Contacts.app for the contact, handle disambiguation, then send.
        message: pre-composed text (from tool calling — LLM already wrote it).
        instruction: spoken instruction (legacy path — goes through generate_message_text)."""
        candidates = search_contacts(contact)

        if not candidates:
            return f"I couldn't find {contact.title()} in your contacts."

        if message:
            msg_text = message
        elif instruction:
            set_state(self.window, "thinking")
            msg_text = llm.generate_message_text(instruction, contact)
        else:
            msg_text = None

        if len(candidates) == 1:
            name, addr = candidates[0]
            if not msg_text:
                return f"What should I say to {name}?"
            if not addr:
                return f"I found {name} but they don't have a phone number or email in your contacts."
            ok = send_imessage_to_address(addr, msg_text)
            return f"Sent to {name}." if ok else f"Couldn't reach {name} — check that Messages is set up."

        # Multiple matches — save state and ask for clarification (expires in 20s)
        self._pending_msg = (candidates, msg_text, time.time() + 20)
        names = [c[0] for c in candidates]
        if len(names) == 2:
            choices = f"{names[0]} or {names[1]}"
        else:
            choices = ", ".join(names[:-1]) + f", or {names[-1]}"
        return f"I found a few — {choices}. Which one?"

    def _resolve_msg_disambiguation(self, text):
        """User is answering 'which John?' — match their reply to a candidate."""
        candidates, msg_text, expire_time = self._pending_msg
        if time.time() > expire_time:
            self._pending_msg = None
            return self._assistant_command(text)   # re-process as a fresh command
        t_norm = _normalize_cmd(text)
        words = set(t_norm.split())

        # Cancel phrases
        if any(w in words for w in ("cancel", "nevermind", "forget", "nope", "nothing")):
            self._pending_msg = None
            return "Got it, canceling."

        chosen = None
        # Try matching by any word in the candidate's name
        for name, addr in candidates:
            name_words = set(_normalize_cmd(name).split())
            if name_words & words:          # any overlap
                chosen = (name, addr)
                break

        # Try ordinals: "first", "second", "the first one", etc.
        if not chosen:
            # Check explicit ordinals before generic number words. Otherwise
            # "the second one" sees "one" first and selects candidate zero.
            ordinals = (("first", 0), ("second", 1), ("third", 2), ("fourth", 3),
                        ("one", 0), ("two", 1), ("three", 2), ("four", 3))
            for ow, idx in ordinals:
                if ow in words and idx < len(candidates):
                    chosen = candidates[idx]
                    break

        if not chosen:
            self._pending_msg = None
            return "I didn't catch that — you can try again."

        self._pending_msg = None
        name, addr = chosen

        # If this disambiguation was triggered by a compose flow, continue it
        compose = self._pending_disambig_compose
        if compose is not None:
            self._pending_disambig_compose = None
            return self._continue_compose(name, addr, compose.get("instruction"),
                                          compose.get("style"), compose.get("text"))

        # Legacy path: msg_text was pre-generated before disambiguation
        if not msg_text:
            return f"What should I say to {name}?"
        if not addr:
            return f"I found {name} but they don't have a phone number in your contacts."
        ok = send_imessage_to_address(addr, msg_text)
        return f"Sent to {name}." if ok else f"Couldn't reach {name}."

    def _apply_correction(self, value):
        """Re-do the last timer/reminder with a corrected time/duration.
        Returns a spoken confirmation, or None if `value` doesn't parse
        (caller falls through to the LLM)."""
        la = self._last_action
        if la["kind"] == "timer":
            secs = assistant.parse_duration(value)
            if not secs:
                m = re.search(r"(\d+(?:\.\d+)?)", value)
                secs = float(m.group(1)) * 60 if m else None
            if not secs:
                return None
            assistant.cancel_by_id(la["rid"])
            js(self.window, f"tedHud.clearTimerById({la['rid']})")
            human = assistant.human_duration(secs)
            end_ts = time.time() + secs
            lbl = la.get("label")
            timer_text = (f"Time's up — your {lbl} timer is done." if lbl
                          else f"Time's up — your {human} timer is done.")
            rid = assistant.add_reminder(timer_text, end_ts, kind="timer", label=lbl)
            safe_label = (lbl or human).replace("'", "\\'")
            js(self.window, f"tedHud.addTimer({rid}, {int(end_ts * 1000)}, '{safe_label}')")
            self._last_action = {"kind": "timer", "rid": rid, "task": None,
                                 "label": lbl, "ts": time.time()}
            return f"Changed it — {human} timer running."

        if la["kind"] == "reminder":
            due = assistant.parse_when(value)
            if not due:
                secs = assistant.parse_duration(value)
                due = time.time() + secs if secs else None
            if not due:
                return None
            assistant.cancel_by_id(la["rid"])
            rid = assistant.add_reminder(f"Reminder — {la['task']}.", due, kind="reminder")
            self._last_action = {"kind": "reminder", "rid": rid, "task": la["task"],
                                 "label": None, "ts": time.time()}
            spoken = time.strftime("%-I:%M %p", time.localtime(due)).lstrip("0")
            return f"Moved it — reminder now at {spoken}."
        return None

    def _briefing(self):
        """Morning rundown: date, weather, calendar, reminders, motivational closer."""
        parts = [f"It's {date.today().strftime('%A, %B %d')}."]
        try:
            w = assistant.get_weather(WEATHER_LOCATION)
            if w:
                parts.append(f"Right now it's {w}.")
        except Exception:
            pass
        try:
            if features.HAS_CALENDAR:
                today_events = features.calendar.get_today_events()
                if today_events:
                    n = len(today_events)
                    summary = features.calendar.format_events_for_speech(today_events)
                    parts.append(f"You've got {n} event{'s' if n != 1 else ''} today: {summary}")
        except Exception:
            pass
        try:
            pend = assistant.pending_reminders()
            if pend:
                items = "; ".join(r["text"].replace("Reminder — ", "").rstrip(".")
                                  for r in pend[:5])
                parts.append(f"On your list: {items}.")
            else:
                parts.append("Nothing on your reminder list.")
        except Exception:
            pass
        _closers = [
            "Let's make it a good one.",
            "You've got this.",
            "Should be a solid day.",
            "Ready when you are.",
        ]
        parts.append(random.choice(_closers))
        return " ".join(parts)

    def _execute_shortcut(self, action_def):
        """Execute a shortcut action from shortcuts.json."""
        action = action_def.get("action", "")
        if action == "morning_briefing":
            return self._briefing()
        if action == "thinking_partner":
            self.thinking_mode = True
            return "Sure — what's on your mind?"
        # Unknown action: fall through to LLM
        return None

    # ── Background threads ─────────────────────────────────────────────────────

    def reminder_watch(self, interval=4):
        """Background thread: poll for due reminders/timers every `interval` seconds
        and speak them aloud when Ted is free.

        Uses a spin-wait (up to 30 s) before each spoken reminder so Ted never
        interrupts a response mid-sentence — reminders wait for the lock to free.
        """
        if not features.HAS_ASSISTANT:
            return
        while True:
            if self.muted:           # stay quiet; reminders wait until unmuted
                time.sleep(interval)
                continue
            try:
                due = assistant.pop_due()
            except Exception:
                due = []
            for r in due:
                # Spin-wait up to 30 s for a free slot — don't interrupt mid-response
                acquired = False
                for _ in range(300):
                    if self.muted:
                        break
                    if self._busy.acquire(blocking=False):
                        acquired = True
                        break
                    time.sleep(0.1)
                if not acquired or self.muted:
                    if acquired:
                        self._busy.release()
                    break
                try:
                    if r.get("kind") == "timer":
                        js(self.window, f"tedHud.clearTimerById({r['id']})")
                        js(self.window, "tedHud.flashAlarm()")
                        voice.play_timer_bell()
                        self._last_fired_timer = r   # saved for snooze
                    add_message(self.window, "ted", r["text"])
                    speak(self.window, r["text"], self)
                    # Ted spoke first — open the conversation so "snooze" /
                    # "stop" work without a wake word even from standby.
                    self._touch_attention()
                except Exception as e:
                    print("Reminder speak error:", e)
                finally:
                    try:
                        self._busy.release()
                    except RuntimeError:
                        pass

            time.sleep(interval)

    def _track_frustration(self, text):
        """Update self.user_frustrated based on recent short, negative inputs."""
        _FRU_WORDS = {"no", "wrong", "stop", "ugh", "nope", "not right", "that's not",
                      "forget it", "nevermind", "incorrect", "no no"}
        t = text.lower()
        is_short = len(text.split()) < 5
        is_negative = any(w in t for w in _FRU_WORDS)
        self._frustration_log.append((time.time(), is_short and is_negative))
        self._frustration_log = self._frustration_log[-5:]
        recent = self._frustration_log[-3:]
        fru_count = sum(1 for _, fru in recent if fru)
        prev = self.user_frustrated
        self.user_frustrated = fru_count >= 2
        if self.user_frustrated and not prev:
            print("[mood] frustration detected — adjusting tone")
        elif not self.user_frustrated and prev:
            print("[mood] frustration cleared")

    # [BOOK §5.2] ─── THE SPOKEN WAY IN ──────────────────────────────────────
    # A loop on a background thread: listen, transcribe, hand the text to
    # _respond, listen again. Runs for the whole life of the program, but does
    # nothing while muted — and Ted boots muted, because it is a chat app now.
    def conversation_loop(self):
        """Main listen→respond loop — runs forever on a background daemon thread.

        Flow:
          startup: greeting → session recap → missed reminders → pattern check
          loop:    capture() → wake-word strip → _busy lock → _respond() → release
          prearmed: when the user barges in, skip the silence-settle and go straight
                    back to capture() so their follow-up isn't lost.
        """
        w = self.window
        time.sleep(0.5)   # let the webview finish rendering before speaking
        try:
            _greet = _startup_greeting()
            add_message(w, "ted", _greet)     # always show in chat
            speak(w, _greet, self)            # no-op while muted (chat-first start)
        except Exception:
            pass
        # Update the HUD Voice readout to reflect the actual TTS engine in use
        js(w, f"tedHud.setVoice({json.dumps(voice.voice_label())})")
        self._touch_attention()   # seed the HUD with the initial attention deadline
        time.sleep(SETTLE_AFTER_TALK)

        # ── Session recap: mention the last real conversation, if there was one ──
        # Only fires when at least 4 hours have passed, and only for sessions
        # that produced an actual memory — restarting Ted after a test run
        # shouldn't make him recap the test run.
        try:
            if get_last_session_summary(min_gap_hours=4.0):
                mems = get_recent_memories(limit=1)
                if mems:
                    m = mems[0]
                    when = m["when"] if m["when"] not in ("", "earlier today") else "earlier"
                    recap = f"{when.capitalize()} — {m['text']}"
                    add_message(w, "ted", recap)
                    speak(w, recap, self)
                    time.sleep(SETTLE_AFTER_TALK)
        except Exception:
            pass

        # ── Missed reminders: fire any that were due while Ted was offline ──
        if features.HAS_ASSISTANT:
            try:
                missed = assistant.fire_due()
                if missed:
                    for r in missed:
                        _txt = r.get("text", "").replace("Reminder — ", "").rstrip(".")
                        msg = f"Heads up — you had a reminder I missed: {_txt}."
                        add_message(w, "ted", msg)
                        speak(w, msg, self)
                        time.sleep(SETTLE_AFTER_TALK)
            except Exception:
                pass

        # ── Proactive pattern offer: check for habits matching current hour ──
        try:
            if not self._pattern_check_done:
                self._pattern_check_done = True
                patterns = get_frequent_patterns(min_count=3)
                hour_now = time.localtime().tm_hour
                hot = [p for p in patterns if abs(p["hour"] - hour_now) <= 1]
                if hot:
                    top = hot[0]
                    msg = (f"You usually ask about {top['topic']} around this time "
                           f"— want me to pull it up?")
                    add_message(w, "ted", msg)
                    speak(w, msg, self)
                    time.sleep(SETTLE_AFTER_TALK)
        except Exception:
            pass

        # prearmed=True tells capture() the mic is already listening (after a barge-in)
        # so it skips the VAD pre-roll silence and grabs the follow-up immediately.
        prearmed = False
        _busy_stuck_since = 0.0   # timestamp when the current busy period started
        _busy_warning_shown = False
        while True:
            if self.busy:
                _now_b = time.time()
                if _busy_stuck_since == 0.0:
                    _busy_stuck_since = _now_b
                elif _now_b - _busy_stuck_since > 30.0 and not _busy_warning_shown:
                    # Signal a slow response to wind down, but never release a
                    # lock owned by another thread. Force-releasing allowed a
                    # second response to enter while the first still touched the
                    # conversation, HUD, and audio engine.
                    self.interrupt_speech = True
                    engine.stop_playback()
                    print("[watchdog] response still busy after 30s — requested cancellation")
                    set_state(w, "idle")
                    _busy_warning_shown = True
                time.sleep(0.05)
                continue
            _busy_stuck_since = 0.0  # lock released normally — reset watchdog
            _busy_warning_shown = False
            if not self.mic_on:
                # Mic off = PHYSICALLY off. No listening of any kind — the old
                # behaviour re-enabled the mic here to catch a voice unmute, which
                # kept flashing the orange indicator and let interference un-mute
                # Ted on its own. Unmute with the ● button, by typing 'unmute',
                # or via the remote endpoint.
                time.sleep(0.3)
                continue

            set_state(w, "idle")
            # Mic loss recovery: retry up to 5 times before giving up
            _mic_retries = 0
            while True:
                try:
                    text = capture(prearmed=prearmed)
                    break
                except Exception as e:
                    _mic_retries += 1
                    print(f"Mic capture error (attempt {_mic_retries}/5): {e}")
                    error_log.error(f"Mic capture error: {e}")
                    if _mic_retries == 1:
                        try:
                            speak(w, "Mic dropped. Reconnecting.", self)
                        except Exception:
                            pass
                    if _mic_retries >= 5:
                        try:
                            speak(w, "Microphone unavailable.", self)
                        except Exception:
                            pass
                        text = None
                        break
                    time.sleep(2)
            prearmed = False
            js(w, "tedHud.micIdle()")
            if self.busy or not self.mic_on:
                continue
            if not text:
                continue

            # Transcribe mode: the words are the deliverable, not a request.
            if self.transcribe_only:
                self._transcribe_to_input(text)
                continue

            # ── Whisper detection: check capture RMS ──
            _rms = voice.last_capture_rms()
            self.whispering = (0 < _rms < voice.WHISPER_RMS_THRESHOLD)
            voice.set_active_volume(0.50 if self.whispering else 1.0)

            # ── Phonetic correction: fix common Whisper mishearings of command verbs ──
            text = _fix_command_words(text)

            # ── Wake phrase detection: strip "Hey Ted" prefix ──
            text, was_wake = _strip_wake_phrase(text)
            if was_wake:
                # RMS guard: if the audio was near-silent, it's likely an echo — ignore
                if _rms > 0 and _rms < WAKE_MIN_RMS:
                    prearmed = False
                    continue
                # Cooldown: prevent rapid re-triggering from feedback
                _now = time.time()
                if _now - self._last_wake_time < WAKE_COOLDOWN_SECS:
                    prearmed = False
                    continue
                self._last_wake_time = _now
                if not text.strip():
                    # Just the wake phrase alone — acknowledge and re-listen
                    self._touch_attention()
                    speak(w, random.choice(["Yes.", "Go ahead.", "Sir.", "Here."]), self)
                    prearmed = False
                    continue
                voice.play_chime(w, self)
                time.sleep(0.08)   # brief gap after chime before processing

            # ── attention gate: in standby, only "Hey Ted" gets through ────────
            # After ATTENTION_WINDOW s of silence Ted stops treating room noise
            # as commands. The one standby exception: stopping Ted's own voice
            # mid-announcement (a reminder can fire while unengaged).
            if was_wake:
                self._touch_attention()
            elif not self._engaged():
                if _is_stop_command(text) and getattr(engine, "_playing", False):
                    self.interrupt_speech = True
                    engine.stop_playback()
                    set_state(w, "idle")
                else:
                    print(f"   (standby — not addressed: {text!r})")
                    js(w, f"tedHud.flashHeard({json.dumps(text)}, true)")
                prearmed = False
                continue
            js(w, f"tedHud.flashHeard({json.dumps(text)}, false)")

            # ── interrupt-priority commands: bypass the busy lock ──────────────
            # Stop/pause/skip/mute execute immediately even if Ted is mid-response.

            # ── voice mute toggle (exclude "mute spotify/music" → those pause Spotify) ──
            _tn = _normalize_cmd(text)
            _mute_words = _tn.split()
            _is_mute_cmd = (_matches(text, _MUTE_PHRASES) and
                            not any(w in _mute_words for w in ("spotify", "music", "song", "audio")))
            if _is_mute_cmd:
                self.toggle_mute()
                prearmed = False
                continue

            if _is_stop_command(text):
                self.interrupt_speech = True
                engine.stop_playback()
                if not getattr(engine, "_playing", False):
                    try:
                        music.transport("pause")
                    except Exception:
                        pass
                set_state(w, "idle")
                prearmed = False
                continue
            if _matches(text, _SPOT_PAUSE):
                self.interrupt_speech = True
                engine.stop_playback()
                try:
                    music.transport("pause")
                except Exception:
                    pass
                set_state(w, "idle")
                prearmed = False
                continue
            if _matches(text, _SPOT_NEXT):
                try:
                    music.transport("next")
                except Exception:
                    pass
                prearmed = False
                continue
            if _matches(text, _SPOT_PREV):
                try:
                    music.transport("previous")
                except Exception:
                    pass
                prearmed = False
                continue

            # ── deduplication: drop exact same command if repeated within 4 s ──
            _cmd_key = _normalize_cmd(text)
            _cmd_now = time.time()
            if _cmd_key and _cmd_key == self._last_cmd[0] and _cmd_now - self._last_cmd[1] < 4.0:
                print(f"[dedup] skipping repeated: {text!r}")
                prearmed = False
                continue
            self._last_cmd = (_cmd_key, _cmd_now)

            if not self._busy.acquire(blocking=True, timeout=8.0):
                continue   # previous command still running after 8 s — drop and re-listen
            barged = False
            try:
                # ── long-gap greeting (48 h since last exchange) ──────────────
                spoken_prefix = None
                if self.last_exchange_time > 0 and \
                   time.time() - self.last_exchange_time > 48 * 3600:
                    _gap_ted = ["hey, it's been a minute —",
                                "been a few days —",
                                "good to hear from you again —"]
                    spoken_prefix = random.choice(_gap_ted)
                    # Reset so it doesn't re-trigger later in the same session
                    self.last_exchange_time = time.time()

                barged = self._respond(text, spoken_prefix=spoken_prefix)
            except Exception as e:
                tb = traceback.format_exc()
                print(f"Ted error: {e}\n{tb}")
                error_log.error(f"conversation_loop unhandled: {e}\n{tb}")
                try:
                    speak(w, "Brief fault. Back online.", self)
                except Exception:
                    pass
                set_state(w, "idle")
            finally:
                try:
                    self._busy.release()
                except RuntimeError:
                    pass

            # ── frustration tracking ────────────────────────────────────────
            self._track_frustration(text)
            self.last_exchange_time = time.time()
            self._session_exchanges += 1

            # ── log topic + hour for pattern recognition ──────────────────────
            try:
                topic = _extract_pattern_topic(text)
                if topic:
                    log_pattern(topic, time.localtime().tm_hour)
            except Exception:
                pass

            if barged:
                prearmed = True
                continue
            time.sleep(SETTLE_AFTER_TALK)

    # ── Session memory ─────────────────────────────────────────────────────────
    #
    # Three things can trigger a write: a periodic flush (crash insurance), the
    # idle watcher (you wandered off), and shutdown (window closed / Ctrl-C /
    # SIGTERM). All three call write_session_memory(), which upserts ONE row per
    # session, so whichever fires first just gets refined by the ones after it.
    #
    # Most sessions produce nothing at all — llm.generate_session_summary()
    # returns None for testing, commands and small talk, which is the intended
    # behaviour, not a failure. `end_session=True` starts a fresh memory row
    # afterwards so a long gap doesn't keep folding into yesterday's memory.

    def write_session_memory(self, reason="flush", end_session=False):
        """Generate and store a memory of the current session. Returns True if a
        memory was written. Never raises — memory is never worth crashing over."""
        with self._memory_lock:
            try:
                if self.last_exchange_time <= 0:
                    return False                      # nothing happened this session
                if self.last_exchange_time <= self._session_summary_last_written:
                    return False                      # nothing new since the last write

                mem = llm.generate_session_summary(self.active_conversation)
                self._session_summary_last_written = time.time()

                if not mem:
                    print(f"[memory] {reason}: nothing worth remembering this session")
                else:
                    self._session_row_id = save_session_summary(
                        mem["text"],
                        topics=mem.get("topics", ""),
                        started=self._session_started_at,
                        exchanges=self._session_exchanges,
                        row_id=self._session_row_id,
                    )
                    print(f"[memory] {reason}: remembered — {mem['text'][:90]}")

                # Raw transcripts become session memories above. This second,
                # conservative pass looks only for durable interaction lessons
                # and leaves inferences proposed until Charlie reviews them.
                relationship.reflect_session(self.active_conversation)

                if end_session:
                    self._session_row_id     = None
                    self._session_started_at = _dt_cls.now().isoformat()
                    self._session_exchanges  = 0
                return bool(mem)
            except Exception as e:
                error_log.error(f"[memory] session memory write failed ({reason}): {e}")
                return False

    def session_summary_watch(self, check_interval=120, idle_threshold=600,
                              flush_every=12):
        """Background thread. Writes a session memory when either:

          • you've been quiet for `idle_threshold` seconds (default 10 min —
            down from 30, which was long enough that closing the laptop usually
            beat it), or
          • `flush_every` exchanges have happened since the last write, so a
            crash or force-quit mid-session can't lose it.
        """
        last_flush_count = 0
        while True:
            time.sleep(check_interval)
            try:
                if self.last_exchange_time <= 0:
                    continue
                idle = time.time() - self.last_exchange_time
                if idle >= idle_threshold:
                    if self.write_session_memory(reason="idle", end_session=True):
                        last_flush_count = 0
                elif self._session_exchanges - last_flush_count >= flush_every:
                    self.write_session_memory(reason="flush")
                    last_flush_count = self._session_exchanges
            except Exception as e:
                error_log.error(f"session_summary_watch: {e}")

    def apps_watch(self, interval=5):
        """Poll running apps + connection health every 5 s and push both to the HUD.
        This doubles as the HUD's heartbeat — if these pushes stop arriving, the
        JS side turns the sphere red (Python crashed or hung)."""
        _prev = []
        while True:
            try:
                apps = get_running_apps()
            except Exception:
                apps = _prev
            try:
                # One assignment keeps readers from observing a half-built
                # snapshot while this watcher refreshes it.
                self._live_state = system_state.collect(apps=apps)
                js(self.window, "tedHud.setComputerState(%s)" %
                   json.dumps(self._live_state))
            except Exception as e:
                error_log.error(f"apps_watch live state: {e}")
            try:
                if apps != _prev:
                    _prev = apps
                    js(self.window, f"tedHud.setOpenApps({json.dumps(apps)})")
            except Exception:
                pass
            # Connection health dots — Groq / Neo4j memory / Spotify.
            # Groq uses a tracked flag (no wasteful ping); memory checks the driver;
            # Spotify reads the apps list we already have, or the Web API toggle.
            try:
                try:
                    import core.memory as _mem
                    mem_ok = (_mem._get_driver() is not None)
                except Exception:
                    mem_ok = False
                spot_ok = ("Spotify" in apps) or music.spotify_web_ready()
                # Now-playing chip uses the same verified state given to the
                # model, including Apple Music or a remote Spotify device.
                try:
                    media = self._live_state.get("media") or {}
                    np = media.get("title")
                    if np and media.get("artist"):
                        np += " — " + media["artist"]
                    js(self.window, f"tedHud.setNowPlaying({json.dumps(np)})")
                except Exception:
                    pass
                js(self.window,
                   "tedHud.setHealth({groq:%s,memory:%s,spotify:%s})" % (
                       "true" if llm.groq_ok() else "false",
                       "true" if mem_ok else "false",
                       "true" if spot_ok else "false",
                   ))
            except Exception:
                pass
            time.sleep(interval)

    # ── JS API surface (called from the HUD) ───────────────────────────────────

    def _announce_local_handover(self, reason, detail):
        """Toast the cloud→local handover while it is happening."""
        if not self.window:
            return
        show_issue(self.window, "Cloud is rate limited — switching to the local "
                                "brain, this turn will be slower…"
                   if reason == "rate_limit" else
                   "Cloud is unavailable — switching to the local brain…")

    def start(self):
        """Launch background daemon threads. Called by webview after the
        window is ready. Safe to call multiple times — subsequent calls are no-ops."""
        if self._loop_started:
            return True
        self._loop_started = True
        # Say when the cloud hands off. A local rescue turn is slower than a
        # cloud one and, unexplained, that slowness is what Charlie experiences
        # as Ted freezing. The reason was already known — it was just recorded
        # for telemetry after the wait instead of shown during it.
        llm.providers.set_fallback_notice(self._announce_local_handover)
        threading.Thread(target=self.conversation_loop,     daemon=True).start()
        threading.Thread(target=self.reminder_watch,        daemon=True).start()
        threading.Thread(target=self.session_summary_watch, daemon=True).start()
        threading.Thread(target=self.apps_watch,            daemon=True).start()
        # Only worth a thread if Charlie is actually watching something.
        if bouncer.enabled():
            threading.Thread(target=self.messages_watch_loop, daemon=True,
                             name="bouncer").start()
        # Load the knowledge store now, on a thread, so the first message does
        # not pay for it inside the retrieval budget.
        if features.HAS_KNOWLEDGE:
            threading.Thread(target=features.knowledge.warm, daemon=True,
                             name="knowledge-warm").start()
        try:
            from core.proactive import ProactiveScheduler
            _sched = ProactiveScheduler(self, speak_fn=speak, add_message_fn=add_message)
            threading.Thread(target=_sched.run, daemon=True).start()
            print("[proactive] scheduler started")
        except Exception as e:
            print(f"[proactive] scheduler unavailable: {e}")
        # Config-driven morning briefing: register (or retime) the trigger once
        if DAILY_BRIEFING_TIME:
            try:
                from core.intents import _parse_time_to_24h
                from core.proactive import add_trigger, list_triggers, remove_trigger
                hhmm = _parse_time_to_24h(DAILY_BRIEFING_TIME)
                if hhmm:
                    existing = [t for t in list_triggers()
                                if t.get("description") == "daily briefing"]
                    if not any(t.get("schedule_value") == hhmm for t in existing):
                        for t in existing:
                            remove_trigger(t["id"])
                        add_trigger(description="daily briefing",
                                    schedule_type="daily_at", schedule_value=hhmm,
                                    action_text="give me the rundown")
                        print(f"[proactive] daily briefing scheduled at {hhmm}")
                else:
                    print(f"[proactive] couldn't parse DAILY_BRIEFING_TIME={DAILY_BRIEFING_TIME!r}")
            except Exception as e:
                print(f"[proactive] briefing setup failed: {e}")
        try:
            from core.remote import RemoteServer
            RemoteServer(self).start()
        except Exception as e:
            print(f"[remote] unavailable: {e}")
        return True

    def listen(self):
        return self.start()

    def cancel_timer(self, rid):
        """HUD click-to-cancel on a timer chip. Returns True if cancelled."""
        if not features.HAS_ASSISTANT:
            return False
        try:
            ok = assistant.cancel_by_id(int(rid))
        except Exception:
            ok = False
        if ok:
            js(self.window, f"tedHud.clearTimerById({int(rid)})")
        return ok

    def stop(self):
        """Stop button: cut off whatever Ted is saying and go back to listening.
        Does NOT change the mic — Ted keeps his ears on."""
        self.interrupt_speech = True
        engine.stop_playback()
        set_state(self.window, "idle")
        return True

    def close_app_direct(self, name):
        """HUD apps panel: close an application by name (the ✕ button).
        Returns the result string; never raises into the webview."""
        try:
            return close_app(name)
        except Exception as e:
            return f"Couldn't close {name}: {e}"

    @property
    def muted(self):
        """Legacy flag, now meaning exactly one thing: TTS is not allowed.

        Every reader of this was asking "should Ted stay silent" — speak(),
        speak_streaming(), the reminder loop, the proactive scheduler. Capture
        is a separate question and its callers ask `mic_on` directly, so
        transcribe mode (mic on, speakers off) reads as muted here and is
        silent, which is the whole point of it.
        """
        return not self.speech_on

    @muted.setter
    def muted(self, value):
        # Assigning the old flag still means "turn Ted all the way off/on".
        self.mic_on = not value
        self.speech_on = not value
        if value:
            self.transcribe_only = False
            self.pet_silent_chat = False

    def _apply_mic(self, on):
        """Start or stop capture, and keep the OS indicator honest about it.

        This is the ONLY place the microphone is claimed. Ted boots with no mic
        tap at all (core/voice.py), so nothing lights the macOS recording
        indicator until the voice or transcribe button is pressed.
        """
        if on:
            voice.prepare_mic()             # claims the mic, calibrates once
            voice.spotify_volume(30)        # lower so Ted doesn't pick up the music
        else:
            voice.release_mic()             # removes mic tap → orange dot off
            voice.spotify_volume(100)       # full volume — not listening, enjoy the music

    def _push_mic_state(self):
        js(self.window, f"tedHud.setMuted({str(not self.mic_on).lower()})")
        js(self.window, f"tedHud.setTranscribing({str(self.transcribe_only).lower()})")
        pet.set_mode(self.pet_mode())

    # ── the notification bouncer ───────────────────────────────────────────

    def _ensure_bouncer_running(self):
        """Start the watcher if it isn't already. Returns a sentence, or ''.

        Adding the first name to the list has to actually start watching.
        Otherwise "I'll tell you when Gavin texts" is true only after the next
        restart, which is exactly the kind of claim this project keeps having
        to stop Ted from making.
        """
        if any(t.name == "bouncer" and t.is_alive()
               for t in threading.enumerate()):
            return ""
        if not bouncer.enabled():
            bouncer.set_enabled(True)
        ok, reason = messages.available()
        if not ok:
            return f" I can't read your messages yet, though: {reason}"
        threading.Thread(target=self.messages_watch_loop, daemon=True,
                         name="bouncer").start()
        return ""

    def messages_watch_loop(self, interval=12):
        """Watch for incoming texts and announce only the ones that qualify.

        The permission failure is reported once and then the loop stops. A
        watcher that retries a denied read every twelve seconds forever writes
        a log line every twelve seconds forever, and Charlie would never see
        the one that told him how to fix it.
        """
        ok, reason = messages.available()
        if not ok:
            print(f"[bouncer] not watching — {reason}")
            self._bouncer_blocked = reason
            return
        self._bouncer_blocked = ""
        # Start from now. Announcing the backlog on first run would read out
        # every text Charlie has ever received.
        last = int(bouncer.get_state("last_rowid", "0") or 0)
        if last <= 0:
            last = messages.latest_rowid()
            bouncer.set_state("last_rowid", last)
        print(f"[bouncer] watching from message {last}")
        while True:
            time.sleep(interval)
            try:
                if not bouncer.enabled():
                    continue
                fresh, error = messages.incoming_since(last)
                if error:
                    print(f"[bouncer] {error}")
                    continue
                for msg in fresh:
                    last = max(last, msg["id"])
                    name = messages.contact_name(msg["handle"])
                    announce, why = bouncer.decide(msg["handle"], name)
                    if not announce:
                        print(f"[bouncer] held back {name or msg['handle']}: {why}")
                        continue
                    self._announce_text(msg, name)
                bouncer.set_state("last_rowid", last)
            except Exception as exc:
                error_log.error(f"messages_watch_loop: {exc}")

    def _announce_text(self, msg, name):
        """Say who texted and offer the two things Charlie asked for.

        Deliberately does NOT read the message out. The whole point of a
        bouncer is that it tells you who is at the door before opening it, and
        a text read aloud in a lecture cannot be un-read.
        """
        line = messages.describe(msg, name)
        self._pending_text_message = {
            "handle": msg["handle"], "name": name or msg["handle"],
            "body": msg.get("body", ""), "at": time.time(),
        }
        js(self.window, "tedHud.incomingText(%s)" % json.dumps({
            "who": name or msg["handle"],
            "line": line,
            "preview": messages.preview(msg, 90),
            "handle": msg["handle"],
        }))
        add_message(self.window, "ted", f"{line} Want me to read it, or open it?")
        if not self.muted:
            speak(f"{line} Want me to read it, or open it?")
        print(f"[bouncer] announced {name or msg['handle']}")

    def read_pending_text(self):
        """'Read it' — the HUD button and the spoken answer land here."""
        pending = self._pending_text_message
        if not pending:
            return "There's no message waiting."
        self._pending_text_message = None
        body = " ".join((pending.get("body") or "").split())
        if not body:
            return (f"{pending['name']} sent something I can't read as text — "
                    f"probably an image or a reaction.")
        return f"{pending['name']} says: {body}"

    def open_pending_text(self):
        """'Open it' — bring the thread up in Messages."""
        pending = self._pending_text_message
        if not pending:
            return "There's no message waiting."
        self._pending_text_message = None
        return messages.open_conversation(pending["handle"])

    def _show_images(self, query, count=3):
        """Put pictures in the chat itself rather than opening a browser.

        The images are pushed straight to the window instead of being returned
        as markdown for the model to repeat. Model narration is not a reliable
        renderer — it paraphrases, drops URLs, and invents captions — and this
        project's rule is that the verified result is the truth about what
        happened. So the HUD is told what was actually shown, and the model is
        told only how many, in words it cannot turn into a broken image.
        """
        query = str(query or "").strip()
        if not query:
            return "I need to know what to show a picture of."
        try:
            count = max(1, min(int(count or 3), 4))
        except (TypeError, ValueError):
            count = 3
        found = th.find_images(query, count)
        if not found:
            return (f"I couldn't find any pictures of {query}, so nothing was "
                    f"added to the chat.")
        js(self.window, "tedHud.showMedia(%s)" % json.dumps({
            "query": query, "images": found}))
        return (f"Showed {len(found)} picture{'s' if len(found) != 1 else ''} "
                f"of {query} in the chat.")

    def open_url_external(self, url):
        """Open a link clicked in the chat, in the real browser.

        The HUD is a page inside pywebview, so a live href would navigate the
        window away from Ted with no way back — the app would simply become
        the website. Every link in a reply is inert and routed through here.
        """
        url = str(url or "").strip()
        # Only http(s). A chat bubble is model output, and file:// or
        # javascript: reaching `open` would turn a hallucinated link into a
        # local action.
        if not url.lower().startswith(("http://", "https://")):
            print(f"[link] refused non-web URL: {url[:80]!r}")
            return False
        try:
            result = th.tool_browse_to(url)
            print(f"[link] {url[:80]} → {result[:60]}")
            return not th.looks_like_failure(result)
        except Exception as exc:
            error_log.error(f"[link] could not open {url[:80]}: {exc}")
            return False

    # ── attachments ────────────────────────────────────────────────────────
    # Three ways in, because all three are things Charlie will actually try:
    # the paperclip (native picker, real paths), dragging a file onto the
    # window, and pasting a screenshot straight off the clipboard. The last two
    # arrive as bytes because the browser sandbox never gives up a real path.

    def attach_pick(self):
        """Paperclip button: open the native file picker and stage what's chosen."""
        try:
            import webview
            chosen = self.window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=True)
        except Exception as exc:
            error_log.error(f"[attach] file dialog failed: {exc}")
            self.show_issue("I couldn't open the file picker.")
            return []
        return self.attach_files(list(chosen or []))

    def attach_files(self, paths):
        """Stage real filesystem paths. Returns one chip description per file."""
        staged = []
        for att in attachments.load_many(paths):
            if att.error:
                # A file that cannot be read is reported now, on the chip, and
                # never staged — far better than Ted receiving an empty
                # attachment and inventing what was in it.
                self.show_issue(f"{att.name}: {att.error}")
            else:
                self._pending_attachments.append(att)
            staged.append(att.as_dict())
        print(f"[attach] staged {len(self._pending_attachments)} file(s)")
        return staged

    def attach_data(self, name, data_url):
        """Stage a dropped or pasted file that arrived as bytes, not a path.

        Written to a temp file first so exactly one code path resolves
        attachments — a second in-memory branch is a second place for the
        image handling to be subtly different.
        """
        try:
            header, _, payload = (data_url or "").partition(",")
            if not payload:
                raise ValueError("empty data URL")
            raw = base64.b64decode(payload)
            if len(raw) > attachments.MAX_BYTES:
                self.show_issue(f"{name} is too big to attach.")
                return []
            safe = os.path.basename(name or "pasted") or "pasted"
            if "." not in safe:
                # A pasted screenshot has no filename at all; give it the
                # extension its own MIME type claims so kind_for works.
                ext = mimetypes.guess_extension(
                    header.split(":")[-1].split(";")[0]) or ".png"
                safe += ext
            folder = tempfile.mkdtemp(prefix="ted-attach-")
            path = os.path.join(folder, safe)
            with open(path, "wb") as fh:
                fh.write(raw)
        except Exception as exc:
            error_log.error(f"[attach] could not stage {name!r}: {exc}")
            self.show_issue(f"I couldn't read {name}.")
            return []
        return self.attach_files([path])

    def attach_clear(self):
        """The × on a staged chip, or sending a message that drops them."""
        self._pending_attachments = []
        return True

    def attach_pending(self):
        """What is staged right now, so the HUD can redraw its chips."""
        return [a.as_dict() for a in self._pending_attachments]

    def toggle_mute(self):
        """Mic button: full voice mode on/off — capture and speech together."""
        going_on = not self.mic_on or self.transcribe_only or not self.speech_on
        self.mic_on = going_on
        self.speech_on = going_on
        self.transcribe_only = False
        self.pet_silent_chat = False
        self._apply_mic(self.mic_on)
        self._push_mic_state()
        return self.muted

    def music_now_playing(self):
        """What Spotify is playing, for the HUD strip. Costs no tokens."""
        if not features.HAS_SPOTIFY_WEB or features.spotify_web is None:
            return {"playing": False, "title": "", "artist": ""}
        try:
            return features.spotify_web.now_playing()
        except Exception as e:
            print(f"[music] now playing: {e}")
            return {"playing": False, "title": "", "artist": ""}

    def music_transport(self, action):
        """HUD transport buttons: previous / play / pause / next.

        Goes straight to the same handler the tool uses, deliberately NOT
        through the model. Pressing skip should not spend a token, wait on a
        rate limit, or risk being narrated into something it did not do.
        """
        if action not in ("play", "pause", "next", "previous"):
            return {"say": f"Unknown transport action '{action}'.",
                    "now": self.music_now_playing()}
        try:
            said = th.tool_spotify_control(action)
        except Exception as e:
            print(f"[music] transport {action}: {e}")
            said = f"Couldn't {action}: {e}"
        return {"say": said, "now": self.music_now_playing()}

    def _transcribe_to_input(self, text):
        """Put a spoken transcript in the input box. Never send it.

        Auto-sending is the one thing Charlie ruled out, and it is the right
        call: a misheard word is editable in the box and unrecoverable once
        sent. capture() has already applied the junk-fragment and Whisper
        phantom gates, which protect the transcript regardless of where it
        ends up — a cough must not become text any more than it should have
        become a command.
        """
        cleaned = _fix_command_words(text)
        js(self.window, f"tedHud.fillInput({json.dumps(cleaned)})")
        return cleaned

    def toggle_transcribe(self):
        """Transcribe button: mic on, speakers off, text into the input box.

        Charlie asked for this by name — talk instead of type, without Ted
        answering out loud. It deliberately does not auto-send: the transcript
        lands in the box so he can edit it and press enter.
        """
        self.transcribe_only = not self.transcribe_only
        self.mic_on = self.transcribe_only
        self.speech_on = False
        self.pet_silent_chat = False
        self._apply_mic(self.mic_on)
        self._push_mic_state()
        return self.transcribe_only

    def pet_mode(self):
        if self.mic_on and self.speech_on:
            return "voice"
        if self.mic_on and self.pet_silent_chat:
            return "transcribe"
        return "off"

    def pet_voice_mode(self):
        """Pet voice button: listen and answer aloud, or turn capture off."""
        if self.pet_mode() == "voice":
            self.mic_on = self.speech_on = False
        else:
            self.mic_on = self.speech_on = True
        self.transcribe_only = False
        self.pet_silent_chat = False
        self._apply_mic(self.mic_on)
        self._push_mic_state()
        return self.pet_mode()

    def pet_transcribe_mode(self):
        """Pet transcript button: listen, answer in bubbles, never use TTS."""
        turning_on = self.pet_mode() != "transcribe"
        self.mic_on = turning_on
        self.speech_on = False
        self.transcribe_only = False
        self.pet_silent_chat = turning_on
        self._apply_mic(self.mic_on)
        self._push_mic_state()
        return self.pet_mode()

    def shutdown_ted(self):
        """Right-click pet action: close every Ted window and end the runtime."""
        self.mic_on = self.speech_on = self.pet_silent_chat = False
        try:
            self._apply_mic(False)
        except Exception:
            pass

        # Let the JavaScript bridge return before destroying the window that
        # owns the bridge call. WKWebView can otherwise wait on itself while
        # pywebview tears the native object down.
        def close_windows():
            time.sleep(0.05)
            pet.close_pet()
            try:
                self.window.destroy()
            except Exception:
                pass

        threading.Thread(target=close_windows, daemon=True,
                         name="pet-shutdown").start()
        return True

    def pet_close(self):
        """Close only the companion window; Ted and the full HUD keep running."""
        def close_window():
            time.sleep(0.05)
            pet.close_pet()

        threading.Thread(target=close_window, daemon=True,
                         name="pet-close").start()
        return True

    def pet_focus(self):
        """Give the frameless pet keyboard focus before editing its text box."""
        return pet.focus_pet()

    def pet_open_dashboard(self):
        """A double-click on Ted restores the main chat/dashboard window."""
        return pet.show_dashboard(self.window)

    def pet_open(self):
        """Main HUD button: restore or recreate the companion window."""
        try:
            import webview
            return pet.open_pet(webview, js_api=self) is not None
        except Exception as exc:
            error_log.error(f"[pet] could not reopen: {exc}")
            self.show_issue("I couldn't reopen the pet window.")
            return False

    def pet_resize_input(self, extra_height=0):
        """Let the one-line pet composer grow downward as Charlie types."""
        return pet.resize_for_input(extra_height)

    def pet_ask(self, text):
        """Send typed pet input while keeping it visible in the full HUD too."""
        text = str(text or "").strip()
        if not text:
            return False
        add_message(self.window, "user", text)
        return self.ask(text)

    # [BOOK §5.1] ─── THE TYPED WAY IN ───────────────────────────────────────
    # Called from the window as window.pywebview.api.ask(text) when you press
    # send. It is the shortest path in the program: take the busy lock, call
    # _respond, release it.
    #
    # Historical note worth keeping: this method used to take the lock BEFORE
    # doing anything, with an eight second timeout — which meant "stop" was
    # queued behind the very thing it was meant to stop. A real log shows a turn
    # hung for 41 seconds while three separate stop attempts were each answered
    # "the previous request is still finishing". Stop now bypasses the lock.
    def set_active_chat(self, chat_id=None, pending_user_text=""):
        """Attach runtime state to the visible chat and restore its context."""
        try:
            parsed = int(chat_id) if chat_id is not None else None
        except (TypeError, ValueError):
            parsed = None
        self._active_chat_id = parsed
        if parsed == self._conversation_chat_id:
            return True

        system_message = self.ted_conversation[0]
        history = task_state.load_chat_history(
            parsed, limit=24, exclude_trailing_user=pending_user_text)
        self.ted_conversation = [system_message, *history]
        self._conversation_chat_id = parsed
        current = task_state.active_for(parsed)
        self._active_task_id = current["id"] if current else None

        user_turns = [item["content"] for item in history if item["role"] == "user"]
        self._prev_user_text = user_turns[-2] if len(user_turns) > 1 else ""
        self._cur_user_text = user_turns[-1] if user_turns else ""
        if parsed is not None:
            print(f"[session] restored chat {parsed}: {len(history)} turns"
                  + (f", task #{self._active_task_id}" if current else ""), flush=True)
        return True

    def ask(self, text, chat_id=None):
        """Handle typed input from the HUD text box.
        Interrupts any ongoing speech, then runs _respond() on a background thread
        (so the JS call returns immediately and the webview doesn't freeze)."""
        if chat_id is not None:
            self.set_active_chat(chat_id, pending_user_text=text)
        self.interrupt_speech = True
        engine.stop_playback()
        print(f"[input] typed request received: {text!r}", flush=True)

        # STOP MUST NEVER QUEUE BEHIND THE THING IT IS STOPPING.
        #
        # Every typed turn used to wait up to 8s for the busy lock, stop
        # included — so the one command whose entire purpose is escaping a stuck
        # turn was the one command a stuck turn could block. In a real log: a
        # request hung for 41 seconds and three separate "stop" attempts were
        # answered with "the previous request is still finishing". There is no
        # way out of that from the text box, which is what being frozen means.
        #
        # Only when the lock is actually held. Otherwise stop behaves normally
        # and keeps its usual job of pausing music when Ted is not speaking.
        if _is_stop_command(text) and self.busy:
            self._pending_msg = None
            self._pending_compose = None
            self._pending_disambig_compose = None
            self._pending_tool_confirmation = None
            self._pending_lingo = None
            print("[input] stop accepted while busy — cancelling in flight",
                  flush=True)
            add_message(self.window, "ted", "Stopped.")
            set_state(self.window, "idle")
            return True

        def flow():
            if not self._busy.acquire(timeout=8.0):
                reply = ("The previous request is still finishing, so I did not run this one. "
                         "Try it once more.")
                print("[input] request rejected: busy for more than 8s", flush=True)
                self.last_reply = reply
                add_message(self.window, "ted", reply)
                show_issue(self.window, reply)
                set_state(self.window, "idle")
                return
            try:
                self._respond(text, echo_user=False)  # echo_user=False: we already show the typed text
                # Typed turns count toward session memory too — otherwise a
                # whole conversation held in the text box is never remembered.
                self.last_exchange_time  = time.time()
                self._session_exchanges += 1
            except Exception as e:
                print("Ted error:", e)
                set_state(self.window, "idle")
            finally:
                try:
                    self._busy.release()
                except RuntimeError:
                    pass
        threading.Thread(target=flow, daemon=True).start()
        return True
