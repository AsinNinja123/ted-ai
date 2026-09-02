"""Regressions for reflex routing, dynamic tools, and prompt-weight policy."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import routing
from core.tools import TOOL_SCHEMAS


PASS = FAIL = 0


def check(desc, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {desc}")
    else:
        FAIL += 1
        print(f"  ✗ {desc}")


def names(schemas):
    return [routing.tool_name(schema) for schema in schemas]


print("— conservative zero-model reflexes —")
plan = routing.plan_reflex("close Notes and Calendar")
check("two fully resolved app targets use the reflex lane",
      plan and plan.calls == (
          ("close_app", {"name": "notes"}),
          ("close_app", {"name": "calendar"}),
      ))
plan = routing.plan_reflex("Could you open VS Code, please?")
check("polite natural wrappers still reach the safe reflex",
      plan and plan.calls == (("open_app", {"name": "vs code"}),))
plan = routing.plan_reflex("alight Ted, uh, let's open Claude and Chat GPT please")
check("fillers and ChatGPT spelling still reach the multi-app reflex",
      plan and plan.calls == (
          ("open_app", {"name": "claude"}),
          ("open_app", {"name": "chat gpt"}),
      ))
plan = routing.plan_reflex("close chatGTP")
check("a safe app reflex tolerates a transposed app name",
      plan and len(plan.calls) == 1
      and plan.calls[0][0] == "close_app"
      and routing.APPS[plan.calls[0][1]["name"]] == "ChatGPT")
plan = routing.plan_reflex("open YouTube and play any video")
check("YouTube playback is one complete outcome instead of a home-page open",
      plan and plan.calls == (("play_youtube", {"query": ""}),))
check("a website mixed with an app declines the entire reflex",
      routing.plan_reflex("open Notes and YouTube") is None)
check("a second capability declines the entire reflex",
      routing.plan_reflex("open Notes and set a timer") is None)
check("contextual pronouns are left to reasoning",
      routing.plan_reflex("close it") is None)
check("dependent sequences are left to reasoning",
      routing.plan_reflex("open Notes, then close it") is None)
check("mixed app/web targets require two completed tool calls",
      routing.expected_action_calls("open Notes and open YouTube") == 2)
check("contextual plural targets require two completed tool calls",
      routing.expected_action_calls("close the two apps I just opened") == 2)
check("two target groups across two stages require four calls",
      routing.expected_action_calls(
          "open Notes and Messages, then close both") == 4)
check("dependent non-app stages are counted",
      routing.expected_action_calls(
          "copy this to my clipboard, then read it back") == 2)
check("two different capabilities joined by and are both required",
      routing.expected_action_calls(
          "open Notes and send a message to Gavin") == 2)
compound_control = (
    "open a terminal, open a command-line assistant and prompt it to build a calculator")
check("punctuation and mixed verbs preserve every requested stage",
      routing.expected_action_calls(compound_control) == 4)
check("compound app-plus-input requests receive keyboard capabilities",
      {"open_app", "type_text", "press_key"}.issubset(
          names(routing.select_tool_schemas(compound_control))))
check("commas inside typed payloads do not invent another stage",
      routing.expected_action_calls("type hello, world into Notes") == 1)
check("unrelated comma-separated actions are both required",
      routing.expected_action_calls("open Notes, set a timer for ten minutes") == 2)
terminal_chain = "open Terminal and run the status command"
check("terminal command chains get entry and submission tools",
      routing.expected_action_calls(terminal_chain) == 3
      and {"open_app", "type_text", "press_key"}.issubset(
          names(routing.select_tool_schemas(terminal_chain))))
check("discussion containing an action verb does not force execution",
      not routing.likely_action_request(
          "I wonder whether I should remove that from my workflow"))
check("a polite direct request still requires execution",
      routing.likely_action_request("Could you pause the music?"))

doc = routing.plan_document(
    "open doc and write a 2 page paper on WW2. 12 font. Double spaced")
check("the failed real-session document wording gets a complete staged plan",
      doc and doc["target_words"] == 600 and doc["font_size"] == 12
      and doc["line_spacing"] == "double" and doc["app"] == "google_docs")
polite_doc = routing.plan_document(
    "Hey Ted, could you open a Google Doc and draft an 11-point report?")
check("polite document requests and hyphenated point sizes are recognized",
      polite_doc and polite_doc["font_size"] == 11)
check("writing prose in chat does not create a document",
      routing.plan_document("write me a two page paper about WW2") is None)

# Regression: the first version of this classifier matched any sentence opening
# with write/check/show/find/read/tell/create/search/remove. Every line below
# is an ordinary chatbot request that was being treated as a Mac command —
# memory withheld, prose suppressed, and a tool call forced with no tool that
# could satisfy it. A verb that is also conversational must not qualify here;
# missing a real action only costs tool_choice="auto", which already works.
for phrase in ("write me a poem about fall",
               "tell me what you think of this design",
               "check my code for bugs",
               "show me an example of a decorator",
               "find the bug in this function",
               "read this back to me and summarize it",
               "create a function that reverses a string",
               "search for a better approach",
               "remove the third paragraph",
               "send me your best guess"):
    check(f"conversation is not an action: {phrase!r}",
          not routing.likely_action_request(phrase))

for phrase in ("open Notes",
               "close VS Code and Notes",
               "open youtube.com in Brave",
               "play the song Maine",
               "pause the music",
               "text Gavin that I'm running late",
               "set a timer for ten minutes",
               "add it to my calendar",
               "log my workout",
               "copy this to my clipboard"):
    check(f"real action still qualifies: {phrase!r}",
          routing.likely_action_request(phrase))

# Aug 14, from a real session: "play a different one" arrived with an empty
# menu because the music family regex wanted the literal words song/music/
# spotify and "play" was not one of them. Ted burned a find_tools round trip,
# hit the free-tier rate limit mid-recovery, fell through to the local brain,
# and took 7.8 seconds to change a song.
_LAST_PLAY = ("Recent verified actions: play_music({'query': 'Let It Go'}) "
              "-> Playing Let It Go.")
for phrase in ("play a different one", "ok play another disney song",
               "play something else", "its not playing", "it's not playing",
               "skip this one", "play the next one"):
    check(f"music request reaches the music tools: {phrase!r}",
          "play_music" in names(routing.select_tool_schemas(phrase, _LAST_PLAY)))
check("a non-music turn is not given music tools by the continuation words",
      "play_music" not in names(routing.select_tool_schemas("how are you", _LAST_PLAY)))

check("an action turn still gets no episodic recall",
      routing.memory_scope_for("open Notes", []) == "none")
check("a greeting skips vector/episodic retrieval but keeps lightweight context",
      routing.memory_scope_for("alright Ted, how are you?", []) == "light")
check("a conversational verb keeps its ordinary memory scope",
      routing.memory_scope_for("write me a poem about fall", []) == "relevant")


print("\n— dynamic capability menus —")
chat = routing.select_tool_schemas("how are you")
check("plain conversation carries no tool contracts", names(chat) == [])
apps_web = routing.select_tool_schemas("open Notes and YouTube")
check("mixed app/web request gets both relevant families",
      {"open_app", "close_app", "browse_to"}.issubset(names(apps_web))
      and "find_tools" not in names(apps_web))
clipboard = routing.select_tool_schemas(
    "put this on my clipboard, then read the clipboard")
check("dependent clipboard request gets read and write contracts",
      {"clipboard_read", "clipboard_write"}.issubset(names(clipboard)))
found = routing.discover_tool_schemas("send a text message", exclude={"find_tools"})
check("capability discovery can recover an initially absent message tool",
      "send_message" in names(found))
check("an unmatched but explicit action retains one discovery escape hatch",
      names(routing.select_tool_schemas("relaunch frobnicator")) == ["find_tools"])
check("an initial capability menu is hard-capped",
      len(routing.select_tool_schemas("click and type into the screen control")) <= 8)
check("operational actions skip episodic memory",
      routing.memory_scope_for("close the app", apps_web) == "none")
check("explicit recall earns full memory",
      routing.memory_scope_for("what do you remember about me", chat) == "full")
check("ordinary conversation gets relevant retrieval only",
      routing.memory_scope_for("how was your day", chat) == "relevant")


print("\n— playlist editing is its own family —")


def _names(text):
    return names(routing.select_tool_schemas(text))


check("editing a playlist loads the editing tools",
      {"add_to_playlist", "create_playlist"} <= set(_names("add this to my gym playlist")))
check("…including when the word 'playlist' is absent from the verb phrase",
      "remove_from_playlist" in _names("remove this song from country"))
# Folding these into the music family would have put four extra schemas on
# every transport command, which is the opposite of what Part 4 is trying to do.
check("plain transport does NOT pay for the editing schemas",
      not ({"add_to_playlist", "remove_from_playlist", "create_playlist",
            "delete_playlist"} & set(_names("skip this song"))))
check("…nor does starting music", "create_playlist" not in _names("play noah kahan"))
check("'what am I listening to' reaches the music tools at all",
      "spotify_control" in _names("what am I listening to"))

print("\n— prompt weight —")
full_chars = len(json.dumps(TOOL_SCHEMAS, separators=(",", ":")))
app_chars = len(json.dumps(apps_web, separators=(",", ":")))
chat_chars = len(json.dumps(chat, separators=(",", ":")))
check("an app/web request removes at least 70% of tool-schema text",
      app_chars <= full_chars * 0.30)
check("plain conversation removes at least 90% of tool-schema text",
      chat_chars <= full_chars * 0.10)
print(f"  full={full_chars} chars app/web={app_chars} chars chat={chat_chars} chars")


print("\n— which brain answers —")
# The bias is asymmetric on purpose: a wrong LOCAL costs answer quality, a
# wrong CLOUD costs tokens that refill every minute. So "unsure" means cloud.
for phrase in ("thanks", "hey ted", "yeah", "ok cool", "never mind", "goodnight"):
    check(f"small talk stays local: {phrase!r}",
          routing.classify_brain(phrase).is_local)
for phrase in ("what time is it", "what's playing", "what apps are open"):
    check(f"live-state lookup stays local: {phrase!r}",
          routing.classify_brain(phrase).is_local)
for phrase in ("how do i write a python decorator",
               "explain why the fallback fires",
               "write me an essay about the civil war",
               "summarize this for me",
               "should i take the 8am section"):
    check(f"thinking work goes to the cloud: {phrase!r}",
          not routing.classify_brain(phrase).is_local)
check("a multi-stage request goes to the cloud",
      not routing.classify_brain("open notes and then play some music").is_local)
check("a long request goes to the cloud",
      not routing.classify_brain(" ".join(["word"] * 40)).is_local)

# Tools mean the 35B local model, which is the rescue brain rather than a
# saving, and an image means the multimodal path. Neither is a local win.
check("a turn carrying tool schemas is never routed local",
      not routing.classify_brain("play some music", schemas=[{"x": 1}]).is_local)
check("an attachment is never routed local",
      not routing.classify_brain("what is this", has_attachment=True).is_local)
check("an explicit pin beats every rule",
      routing.classify_brain("write me an essay", pinned="local").is_local
      and routing.classify_brain("thanks", pinned="cloud").brain == "cloud")

print("\n— the local router as tiebreak —")
# Only "not obviously simple" earns a second opinion; every other reason is a
# positive finding and must not spend 0.1s re-asking.
asked = []


def _fake_ask(reply):
    def ask(system, user):
        asked.append(user)
        return reply
    return ask


asked.clear()
routing.classify_brain_with_model("thanks", ask=_fake_ask("CLOUD"))
check("a rule-settled turn never consults the model", not asked)

asked.clear()
out = routing.classify_brain_with_model("is gavin coming over tonight",
                                        ask=_fake_ask("LOCAL"))
check("an ambiguous turn does consult the model", len(asked) == 1)
check("…and the message reaches it wrapped as data, not as a question",
      "BEGIN MESSAGE" in asked[0] and "Do not answer it" in asked[0])
check("a LOCAL verdict is honoured",
      out.is_local and out.decided_by == "model")
check("a CLOUD verdict is honoured",
      routing.classify_brain_with_model(
          "is gavin coming over tonight", ask=_fake_ask("CLOUD")).brain == "cloud")

# A 3B model asked "is this simple?" about "what is the capital of Iowa" will
# reply "Des Moines". That is not a verdict and must not be read as one.
answered = routing.classify_brain_with_model(
    "whats the capital of iowa", ask=_fake_ask("Des Moines"))
check("a model that answered instead of labelling keeps the rule",
      answered.decided_by == "rule")


def _boom(system, user):
    raise RuntimeError("ollama is down")


check("a router that raises never breaks the turn",
      routing.classify_brain_with_model(
          "is gavin coming over tonight", ask=_boom).decided_by == "rule")

print("\n" + "=" * 50)
print("\n— the cleanup lane: pattern picks the action, a model reads the tail —")
APPS = ["Preview", "ChatGPT", "Brave Browser", "Claude"]
say = lambda reply: (lambda system, user, num_predict=4: reply)

check("bare 'clean up' never asks a model at all",
      routing.cleanup_reflex("clean up") and routing.cleanup_reflex("tidy up"))
check("a tail is still recognised as a cleanup's shape",
      not routing.cleanup_reflex("clean up but leave brave")
      and routing.cleanup_request("clean up but leave brave"))
check("something that only starts the same way is not settled by the pattern",
      not routing.cleanup_reflex("clean up Chrome"))

check("a spared app is resolved to the exact running name",
      routing.extract_kept_apps("clean up but leave brave", APPS,
                                ask=say("Brave Browser")) == ["Brave Browser"])
check("a partial name still resolves",
      routing.extract_kept_apps("clean up but leave brave", APPS,
                                ask=say("brave")) == ["Brave Browser"])
check("several spared apps come back in order",
      routing.extract_kept_apps("x", APPS, ask=say("Brave Browser, Claude"))
      == ["Brave Browser", "Claude"])
check("explicit multi-app exclusions do not depend on a small model preserving both",
      routing.extract_kept_apps(
          "clean up but leave chatGPT and brave open", APPS,
          ask=lambda *_a, **_k: (_ for _ in ()).throw(
              AssertionError("explicit names should not call the router")))
      == ["ChatGPT", "Brave Browser"])
check("misspelled cleanup exclusions resolve to the real running app",
      routing.extract_kept_apps(
          "clean up but leave chatGTP and brave open", APPS,
          ask=lambda *_a, **_k: "NONE")
      == ["ChatGPT", "Brave Browser"])
check("NONE means a cleanup that spares nothing",
      routing.extract_kept_apps("clean up", APPS, ask=say("NONE")) == [])
check("NO means this was never a cleanup",
      routing.extract_kept_apps("clean up Chrome", APPS, ask=say("NO"))
      == routing.NOT_A_CLEANUP)
check("an invented app name is refused, not guessed at",
      routing.extract_kept_apps("x", APPS, ask=say("Firefox")) is None)
check("a silent router is refused too",
      routing.extract_kept_apps("x", APPS, ask=say("")) is None)
check("a router that raises cannot take the turn down",
      routing.extract_kept_apps(
          "x", APPS,
          ask=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ollama down"))) is None)

print("\n— verified volume reflex and dependent actions —")
volume = routing.plan_system_volume("what is my volume at right now")
check("current-volume questions always read the Mac instead of trusting chat history",
      volume and volume.calls == (("system_volume", {"action": "get"}),))
volume = routing.plan_system_volume("set volume to 37%")
check("explicit volume changes bypass model tool selection",
      volume and volume.calls == (("system_volume", {"action": "set", "level": 37}),))
recent = [{"tool": "system_volume", "args": {"action": "get"},
           "result": "System volume is at 50%."}]
followup = routing.plan_system_volume("set it to 42", recent)
check("a volume follow-up resolves its pronoun from verified action context",
      followup and followup.calls == (("system_volume", {"action": "set", "level": 42}),))
check("the same vague follow-up is not guessed without volume context",
      routing.plan_system_volume("set it to 42", []) is None)
chain = "Check the weather, then add the forecast to a note."
check("read-then-write chains are treated as actions that must finish",
      routing.likely_action_request(chain)
      and routing.expected_action_calls(chain) == 2)

print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
