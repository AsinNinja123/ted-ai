"""core/app.py — TedApi: the runtime object behind the HUD.

Owns the listen→think→speak conversation loop, deterministic command routing,
the LLM tool-calling loop, and every background watcher thread. The pywebview
window exposes this object's public methods (start/listen/stop/toggle_mute/ask)
to the JS side.
"""

import json
import random
import re
import threading
import time
import traceback
from datetime import date, datetime as _dt_cls

from core import features, llm, music, tool_handlers as th, voice
from core.actions import (close_app, open_app, get_running_apps,
                          search_contacts, send_imessage_to_address)
from core.hud_bridge import js, set_state, add_message, show_issue
from core.intents import (
    _normalize_cmd, _matches, _split_commands,
    _is_stop_command, _is_cancel_command, _is_repeat_command,
    _SLOWER_PHRASES, _FASTER_PHRASES, _MUTE_PHRASES,
    _SPOT_PAUSE, _SPOT_NEXT, _SPOT_PREV,
    _BRIEF_PHRASES, _HOLD_PHRASES, _RECALL_PHRASES, _THINK_ENTER, _THINK_EXIT,
    _chat_command, _reminders_command,
    _parse_open_apps, _parse_close_apps, _resolve_context_app,
    _parse_message_cmd, _parse_ask_claude, _parse_reminder, _parse_list_cmd,
    _parse_calc, _parse_cancel_scheduled, _is_timer_request, _is_countdown_request,
    _parse_time_to_24h, _detect_mood, _MOOD_SEARCH, _MOOD_DESC,
    _classify_content_speed, _extract_pattern_topic, _confused_reply,
    _fix_command_words, _strip_wake_phrase, _needs_web,
)
from core.logs import error_log
from core.memory import (save_goal, get_goals, complete_goal,
                         goals_needing_checkin, log_pattern, get_frequent_patterns,
                         save_session_summary, get_last_session_summary,
                         log_habit, get_habit_streak, get_all_habits)
from core.paths import SHORTCUTS_PATH
from core.tools import TOOL_SCHEMAS
from core.voice import speak, speak_streaming, capture, engine

try:
    from config import OWNER_NAME
except Exception:
    OWNER_NAME = "Charlie"
try:
    from config import STORE_LOCATION
except Exception:
    STORE_LOCATION = ""

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
        self._loop_started    = False             # prevents starting the loop twice
        self.muted            = False             # ears off when True
        self.interrupt_speech = False             # set True to cut off current playback
        self.last_reply       = ""               # stored so 'repeat that' works
        self._last_cmd        = ("", 0.0)        # (normalized_text, timestamp) for dedup
        self._pending_msg             = None   # ([(name,addr),...], msg_text, expire_time) awaiting disambiguation
        self._pending_compose         = None   # dict awaiting message/email style/content input
        self._pending_disambig_compose = None  # {instruction, style} saved during contact disambiguation

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
        self.held_thought       = None         # topic saved by "hold that thought"
        self.whispering         = False        # True → lower TTS volume to match user's level

        # ── Background thread bookkeeping ──────────────────────────────────────
        self._session_summary_last_written = 0.0  # epoch; prevents re-writing an unchanged session
        self._pattern_check_done = False           # proactive offer fires at most once per startup
        self._last_wake_time     = 0.0             # epoch; for wake-word cooldown (echo prevention)
        self._last_fired_timer   = None            # last timer that fired — for snooze

    @property
    def busy(self):
        """True when Ted is processing a turn. Thread-safe via Lock."""
        return self._busy.locked()

    @property
    def active_conversation(self):
        """Returns the conversation list."""
        return self.ted_conversation

    def _respond(self, text, echo_user=True, spoken_prefix=None):
        """
        Think about `text` and answer out loud.
        Intercepts stop/cancel commands before they ever reach the LLM.
        spoken_prefix: if set, spoken before the reply (correction ack,
                       long-gap greeting). Pass None for the normal flow.
        Returns True if the user barged in by voice during the reply.
        """
        w = self.window

        # ── mute/unmute from typed input or the remote endpoint ──
        # (Voice mute is intercepted in conversation_loop; while muted there is
        # no voice path at all — the mic is physically off — so typing is how
        # 'unmute' arrives.)
        _tn_mute = _normalize_cmd(text)
        if self.muted and (_tn_mute.startswith("unmute") or _tn_mute in
                           ("listen", "start listening", "wake up", "turn on mic",
                            "turn on microphone", "mic on")):
            self.toggle_mute()
            if echo_user:
                add_message(w, "user", text)
            reply = "I'm back — listening."
            self.last_reply = reply
            add_message(w, "ted", reply)
            speak(w, reply, self)
            return False
        if (not self.muted and _matches(text, _MUTE_PHRASES)
                and not any(x in _tn_mute.split() for x in ("spotify", "music", "song", "audio"))):
            if echo_user:
                add_message(w, "user", text)
            self.toggle_mute()
            return False

        # ── stop command: cut Ted off, and also pause Spotify if Ted isn't speaking ──
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

        # ── cancel command: cut off and go quiet ──
        if _is_cancel_command(text):
            self.interrupt_speech = True
            engine.stop_playback()
            if echo_user:
                add_message(w, "user", text)
            set_state(w, "idle")
            return False

        # ── chat-panel command: just drive the UI, don't think about it ──
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

        engine.reset_barge_in()
        self.interrupt_speech = False
        if echo_user:
            add_message(w, "user", text)

        if spoken_prefix:
            speak(w, spoken_prefix, self)

        set_state(w, "thinking")

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

        # ── fast deterministic commands (no LLM call — regex/rule-based) ──
        asst_result = self._assistant_command(text)
        if asst_result is not None:
            engine.reset_barge_in()
            self.last_reply = asst_result
            add_message(w, "ted", asst_result)
            if th.looks_like_failure(asst_result):
                show_issue(w, asst_result)
            speak(w, asst_result, self)
            return False

        # ── tool calling: LLM picks the right action from natural language ──
        tool_result = self._try_tools(text)
        if tool_result is not None:
            self.last_reply = tool_result
            add_message(w, "ted", tool_result)
            speak(w, tool_result, self)
            return False

        # ── no tool matched: streaming LLM for general conversation ──
        # Give immediate audio feedback before a slow web lookup so Ted doesn't freeze.
        if _needs_web(text):
            speak(w, "Looking that up.", self)
            engine.reset_barge_in()
            self.interrupt_speech = False
        else:
            time.sleep(0.15)
        gen = llm.ask_streaming(text, self.active_conversation,
                                frustrated=self.user_frustrated,
                                thinking_mode=self.thinking_mode,
                                window=w)
        # Voice expressiveness: adjust speed by content type
        resp_speed = voice.SPEED * _classify_content_speed(text)
        # Whisper volume scale
        resp_vol = 0.50 if self.whispering else 1.0
        full, barged = speak_streaming(w, gen, self, speed=resp_speed, volume=resp_vol)
        if full.strip():
            self.last_reply = full
            add_message(w, "ted", full)
        else:
            # LLM returned nothing — rotate a confused-response phrase
            err = _confused_reply()
            self.last_reply = err
            add_message(w, "ted", err)
            speak(w, err, self)
        # If Groq was unreachable this turn, leave the HUD on the error state
        # (yellow sphere) until the next good turn — speak_streaming reset it to idle.
        if not llm.groq_ok():
            set_state(w, "error")
        return barged

    def _assistant_command(self, text):
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

        # ── ask Claude (second brain) ──
        acq = _parse_ask_claude(text)
        if acq is not None:
            if not acq:
                return "Ask Claude what?"
            set_state(self.window, "thinking")
            return llm.ask_claude(acq)

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

        # ── goal tracking ──
        # "I'm working on X"
        gm = re.search(r"\bi'?m working on\s+(.+)", text, re.I)
        if not gm:
            gm = re.search(r"\bmy goal is\s+(?:to\s+)?(.+)", text, re.I)
        if gm:
            gname = gm.group(1).strip().rstrip(".")
            if 2 <= len(gname.split()) <= 12:
                save_goal(gname)
                return f"Tracking: {gname}."

        # "I finished / I completed X" → only fires if it matches a saved goal
        gdone = re.search(
            r"\b(?:i finished|i completed|i'm done with|done with)\s+(.+?)\.?\s*$",
            text, re.I,
        )
        if gdone:
            gname = gdone.group(1).strip()
            if complete_goal(gname):
                return "Marked done."
            # No matching goal → fall through to LLM

        # "What am I working on?"
        if re.search(
            r"\bwhat am i working on\b|\bmy (?:active )?goals?\b|\bshow my goals\b",
            text, re.I,
        ):
            goals = get_goals()
            if not goals:
                return "You don't have any active goals saved right now."
            names = "; ".join(g["name"] for g in goals[:5])
            return f"You're working on: {names}."

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
                    mode_req = action_def.get("mode")
                    if mode_req and mode_req != "ted":
                        continue
                    return self._execute_shortcut(action_def)

        # ── cash & change calculator ──
        calc = _parse_calc(text)
        if calc:
            return calc

        # ── fireworks-season countdown ──
        if _is_countdown_request(text):
            name, days = assistant.next_firework_holiday()
            if days == 0:
                return f"It's {name} today — the big one."
            return f"{days} day" + ("s" if days != 1 else "") + f" until {name}."

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
                assistant.add_reminder(f"Reminder — {task}.", due, kind="reminder")
                _spoken_time = time.strftime("%-I:%M %p", time.localtime(due)).lstrip("0")
                return f"Reminder set for {_spoken_time} — {task}."
            assistant.add_reminder(f"Reminder — {task}.", None, kind="reminder")
            return f"Reminder added: {task}."

        # ── named lists (reorder list, to-do, etc.) ──
        li = _parse_list_cmd(text)
        if li is not None:
            return li

        # ── knowledge base: "remember this / note this / add to knowledge: ..." ──
        km = re.search(
            r'\b(?:remember this|add to (?:your )?knowledge|note this|save this'
            r'|teach you(?:rself)?)\s*[:\-,]?\s*(.+)',
            text, re.I,
        )
        if km:
            body = km.group(1).strip()
            if body and features.HAS_KNOWLEDGE:
                n = features.knowledge.add_text(body, source="voice")
                return "Got it, saved." if n else "Couldn't save that — knowledge base unavailable."
            return "What should I remember?"

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
            _subp.run(["osascript", "-e", f"set volume output volume {level}"], capture_output=True)
            return f"System volume set to {level}."

        if re.search(r'\b(?:system|computer) volume (?:up|louder|higher)\b', text, re.I):
            _subp.run(["osascript", "-e",
                       "set volume output volume (output volume of (get volume settings) + 15)"],
                      capture_output=True)
            return "Volume up."

        if re.search(r'\b(?:system|computer) volume (?:down|lower|quieter)\b', text, re.I):
            _subp.run(["osascript", "-e",
                       "set volume output volume (output volume of (get volume settings) - 15)"],
                      capture_output=True)
            return "Volume down."

        if re.search(r'\bmute (?:the )?(?:computer|system|audio|sound)\b', text, re.I):
            _subp.run(["osascript", "-e", "set volume with output muted"], capture_output=True)
            return "System muted."

        if re.search(r'\bunmute (?:the )?(?:computer|system|audio|sound)\b', text, re.I):
            _subp.run(["osascript", "-e", "set volume without output muted"], capture_output=True)
            return "Unmuted."

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
        """Agentic tool loop — up to 3 rounds of LLM → execute → feed back.
        When the LLM stops calling tools it synthesizes a final spoken reply.
        Returns that reply string, or None to fall through to the streaming LLM."""
        if not th.likely_command(text):
            return None

        MAX_ROUNDS = 3
        history = [m for m in self.active_conversation if m["role"] != "system"][-8:]
        messages = [
            {"role": "system", "content": (
                llm.SYSTEM_PROMPT +
                " The user's input may contain speech recognition errors or unusual "
                "phrasing — interpret intent over literal words when choosing tools."
            )},
            *history,
            {"role": "user", "content": text},
        ]

        import groq as _groq_mod
        for round_num in range(MAX_ROUNDS):
            try:
                resp = llm.groq_client.chat.completions.create(
                    model=llm.CHAT_MODEL,
                    messages=messages,
                    tools=TOOL_SCHEMAS,
                    tool_choice="auto",
                    max_tokens=300,
                    temperature=0.1,
                    timeout=12.0,
                )
            except _groq_mod.RateLimitError:
                print("[tools] rate limited — skipping tool path")
                return None
            except Exception as e:
                print(f"[tools] round {round_num + 1} error: {e}")
                return None

            msg = resp.choices[0].message

            # No tool calls → LLM has synthesized the final answer
            if not msg.tool_calls:
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

            # Execute each tool and append its result
            _round_results = []
            _all_actions = True
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except Exception:
                    args = {}
                result = self._dispatch_tool(tc.function.name, args)
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

    def _dispatch_tool(self, name, args):
        """Route a tool call from the LLM to the right Python handler.
        Returns a spoken-style result string; on any error returns an honest
        failure message (never None-→-"Done.")."""
        try:
            if name == "open_app":
                return th.tool_open_app(args.get("name", ""))
            if name == "close_app":
                return close_app(args.get("name", ""))
            if name == "browse_to":
                return th.tool_browse_to(args.get("site", ""))
            if name == "play_music":
                return features.spotify_web.play_track(args.get("query", ""), args.get("artist"))
            if name == "play_playlist":
                return features.spotify_web.play_playlist(args.get("name", ""), args.get("shuffle", False))
            if name == "spotify_control":
                return th.tool_spotify_control(args.get("action", ""))
            if name == "send_message":
                return self._compose_and_send(
                    args.get("contact", ""),
                    instruction=args.get("instruction"),
                    style=args.get("style"),
                )
            if name == "set_reminder":
                return th.tool_set_reminder(args.get("text", ""), args.get("when", ""))
            if name == "set_timer":
                return th.tool_set_timer(args.get("duration", ""))
            if name == "get_reminders":
                return th.tool_get_reminders()
            if name == "list_add":
                return th.tool_list_add(args.get("list_name", ""), args.get("item", ""))
            if name == "list_get":
                return th.tool_list_get(args.get("list_name", ""))
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
                import subprocess as _sp
                action = args.get("action", "get")
                level  = args.get("level")
                if action == "set" and level is not None:
                    lv = max(0, min(100, int(level)))
                    _sp.run(["osascript", "-e", f"set volume output volume {lv}"],
                            capture_output=True)
                    return f"System volume set to {lv}."
                elif action == "up":
                    _sp.run(["osascript", "-e",
                             "set volume output volume (output volume of (get volume settings) + 15)"],
                            capture_output=True)
                    return "Volume up."
                elif action == "down":
                    _sp.run(["osascript", "-e",
                             "set volume output volume (output volume of (get volume settings) - 15)"],
                            capture_output=True)
                    return "Volume down."
                elif action == "mute":
                    _sp.run(["osascript", "-e", "set volume with output muted"],
                            capture_output=True)
                    return "System muted."
                elif action == "unmute":
                    _sp.run(["osascript", "-e", "set volume without output muted"],
                            capture_output=True)
                    return "Unmuted."
                else:
                    r = _sp.run(["osascript", "-e",
                                 "output volume of (get volume settings)"],
                                capture_output=True, text=True)
                    return f"System volume is at {r.stdout.strip()}."

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

            # ── Computer control ─────────────────────────────────────────────
            if name == "type_text":
                if features.HAS_COMPUTER:
                    return features.computer.type_text(args.get("text", ""))
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

    def _compose_and_send(self, contact, instruction=None, style=None):
        """Find the contact, then orchestrate the ask-style → generate → send flow."""
        candidates = search_contacts(contact)
        if not candidates:
            return f"I couldn't find {contact.title()} in your contacts."

        if len(candidates) > 1:
            # Save compose intent separately — disambiguation uses _pending_msg alone
            self._pending_disambig_compose = {"instruction": instruction, "style": style}
            self._pending_msg = (candidates, None, time.time() + 30)
            names = [c[0] for c in candidates]
            choices = " or ".join(names) if len(names) == 2 else ", ".join(names[:-1]) + f", or {names[-1]}"
            return f"I found a few — {choices}. Which one?"

        name, addr = candidates[0]
        return self._continue_compose(name, addr, instruction, style)

    def _continue_compose(self, name, addr, instruction, style):
        """Continue the compose flow once we have a confirmed contact."""
        if not addr:
            return f"I found {name} but they don't have a phone or email saved."
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
            ordinals = {"first": 0, "one": 0, "second": 1, "two": 1,
                        "third": 2, "three": 2, "fourth": 3, "four": 3}
            for ow, idx in ordinals.items():
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
            return self._continue_compose(name, addr, compose.get("instruction"), compose.get("style"))

        # Legacy path: msg_text was pre-generated before disambiguation
        if not msg_text:
            return f"What should I say to {name}?"
        if not addr:
            return f"I found {name} but they don't have a phone number in your contacts."
        ok = send_imessage_to_address(addr, msg_text)
        return f"Sent to {name}." if ok else f"Couldn't reach {name}."

    def _briefing(self):
        """Morning rundown: date, weather, calendar, reminders, motivational closer."""
        parts = [f"It's {date.today().strftime('%A, %B %d')}."]
        try:
            w = assistant.get_weather(STORE_LOCATION)
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
        and speak them aloud when Ted is free. Also pings the user about stale goals
        once per day.

        Uses a spin-wait (up to 30 s) before each spoken reminder so Ted never
        interrupts a response mid-sentence — reminders wait for the lock to free.
        """
        if not features.HAS_ASSISTANT:
            return
        _goal_checkin_at = time.time() + 3600   # first potential check-in after 1 h
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
                except Exception as e:
                    print("Reminder speak error:", e)
                finally:
                    try:
                        self._busy.release()
                    except RuntimeError:
                        pass

            # ── goal check-in: nudge on stale goals once per day ──────────
            if (not self.muted and time.time() >= _goal_checkin_at):
                try:
                    stale = goals_needing_checkin(days=3)
                    if stale:
                        goal = random.choice(stale)
                        msg = f"Hey, how's it going with {goal['name']}?"
                        if self._busy.acquire(blocking=False):
                            try:
                                add_message(self.window, "ted", msg)
                                speak(self.window, msg, self)
                            except Exception as e:
                                print(f"Goal check-in error: {e}")
                            finally:
                                try:
                                    self._busy.release()
                                except RuntimeError:
                                    pass
                except Exception as e:
                    print(f"[goals] check-in skipped: {e}")
                _goal_checkin_at = time.time() + 86400   # next check in 24 h
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
            speak(w, _startup_greeting(), self)
        except Exception:
            pass
        # Update the HUD Voice readout to reflect the actual TTS engine in use
        js(w, f"tedHud.setVoice({json.dumps(voice.voice_label())})")
        time.sleep(SETTLE_AFTER_TALK)

        # ── Session recap: mention last session if gap > 4 hours ──
        try:
            prev = get_last_session_summary(min_gap_hours=4.0)
            if prev:
                recap = f"Last time we talked — {prev}"
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
        while True:
            if self.busy:
                _now_b = time.time()
                if _busy_stuck_since == 0.0:
                    _busy_stuck_since = _now_b
                elif _now_b - _busy_stuck_since > 30.0:
                    # Response has held the lock too long. First SIGNAL it to wind
                    # down (interrupt + stop playback) and give it a moment; only
                    # force-release if it STILL hasn't let go. This avoids two
                    # _respond flows touching the engine at once when the original
                    # thread is merely slow rather than truly hung.
                    self.interrupt_speech = True
                    engine.stop_playback()
                    time.sleep(1.0)
                    if self._busy.locked():
                        try:
                            self._busy.release()
                        except RuntimeError:
                            pass
                        print("[watchdog] response stuck >30s — force-released busy lock")
                    set_state(w, "idle")
                    _busy_stuck_since = 0.0
                time.sleep(0.05)
                continue
            _busy_stuck_since = 0.0  # lock released normally — reset watchdog
            if self.muted:
                # Muted = mic PHYSICALLY off. No listening of any kind — the old
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

            if self.busy or self.muted:
                continue
            if not text:
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
                    speak(w, random.choice(["Yes.", "Go ahead.", "Sir.", "Here."]), self)
                    prearmed = False
                    continue
                voice.play_chime(w, self)
                time.sleep(0.08)   # brief gap after chime before processing

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

    def session_summary_watch(self, check_interval=300, idle_threshold=1800):
        """Background thread: after 30 min of silence, write a session summary
        to Neo4j so the next startup can recap what was discussed.
        """
        while True:
            time.sleep(check_interval)
            if (self.last_exchange_time > 0
                    and self.last_exchange_time > self._session_summary_last_written
                    and time.time() - self.last_exchange_time >= idle_threshold):
                summary = llm.generate_session_summary(self.active_conversation)
                if summary:
                    save_session_summary(summary)
                    self._session_summary_last_written = time.time()
                    print(f"[session] summary written ({len(summary)} chars)")

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

    def start(self):
        """Launch background daemon threads. Called by webview after the
        window is ready. Safe to call multiple times — subsequent calls are no-ops."""
        if self._loop_started:
            return True
        self._loop_started = True
        threading.Thread(target=self.conversation_loop,     daemon=True).start()
        threading.Thread(target=self.reminder_watch,        daemon=True).start()
        threading.Thread(target=self.session_summary_watch, daemon=True).start()
        threading.Thread(target=self.apps_watch,            daemon=True).start()
        try:
            from core.proactive import ProactiveScheduler
            _sched = ProactiveScheduler(self, speak_fn=speak, add_message_fn=add_message)
            threading.Thread(target=_sched.run, daemon=True).start()
            print("[proactive] scheduler started")
        except Exception as e:
            print(f"[proactive] scheduler unavailable: {e}")
        try:
            from core.remote import RemoteServer
            RemoteServer(self).start()
        except Exception as e:
            print(f"[remote] unavailable: {e}")
        return True

    def listen(self):
        return self.start()

    def stop(self):
        """Stop button: cut off whatever Ted is saying and go back to listening.
        Does NOT change the mic — Ted keeps his ears on."""
        self.interrupt_speech = True
        engine.stop_playback()
        set_state(self.window, "idle")
        return True

    def toggle_mute(self):
        """Mute button: mic on/off only. Releases the macOS orange indicator when
        muted and adjusts Spotify volume so music plays at full when not listening."""
        self.muted = not self.muted
        if self.muted:
            engine.mute_mic()               # removes mic tap → orange dot off
            voice.spotify_volume(100)       # full volume — not listening, enjoy the music
        else:
            engine.unmute_mic()             # reinstalls mic tap → back to listening
            voice.spotify_volume(30)        # lower so Ted doesn't pick up the music
        js(self.window, f"tedHud.setMuted({str(self.muted).lower()})")
        return self.muted

    def ask(self, text):
        """Handle typed input from the HUD text box.
        Interrupts any ongoing speech, then runs _respond() on a background thread
        (so the JS call returns immediately and the webview doesn't freeze)."""
        self.interrupt_speech = True
        engine.stop_playback()
        def flow():
            for _ in range(400):
                if self._busy.acquire(blocking=False):
                    break
                time.sleep(0.05)
            else:
                self._busy.acquire()     # blocking fallback if spin-wait timed out
            try:
                self._respond(text, echo_user=False)  # echo_user=False: we already show the typed text
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
