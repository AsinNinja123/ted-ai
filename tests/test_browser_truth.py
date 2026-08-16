"""Ted reports what a browser is SHOWING, not what it was told to show.

The bug this pins: Ted opened a YouTube video and then said he could not
confirm it had opened, while the HUD sat there displaying the tab title. The
window check proved a window existed and nothing ever read the tab, so the only
honest thing left to say was "I can't verify" — even though a check was
available.

No real browser is driven here; the tab source is stubbed, because the point
under test is what Ted SAYS about each possible reading, including the failures.

Run with:  ~/ted-ai/venv/bin/python tests/test_browser_truth.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import system_state, tool_handlers as th  # noqa: E402

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {desc}")
    else:
        FAIL += 1
        print(f"  ✗ {desc}")


_real_tabs = system_state._browser_tabs


def stub_tabs(sequence):
    """Feed _active_tab a scripted sequence of reads, one per retry."""
    calls = {"n": 0}

    def fake(app_name):
        i = min(calls["n"], len(sequence) - 1)
        calls["n"] += 1
        return list(sequence[i]), 1
    system_state._browser_tabs = fake
    return calls


def tab(title, url, active=True):
    return {"title": title, "url": url, "host": "", "active": active, "window": 1}


try:
    print("\n— reading the active tab —")
    stub_tabs([[tab("Rick Astley - Never Gonna Give You Up",
                    "https://youtube.com/watch?v=x")]])
    got = th._active_tab("Brave Browser")
    check("returns the active tab", got and "Rick Astley" in got["title"])

    stub_tabs([[tab("Other", "https://a", active=False),
                tab("Chosen", "https://b", active=True)]])
    got = th._active_tab("Brave Browser")
    check("picks the ACTIVE tab, not the first one", got and got["title"] == "Chosen")

    # Chromium reports the URL as the title until the document commits. Reading
    # once would name the URL where the video title is about to be.
    calls = stub_tabs([
        [tab("https://youtube.com/watch?v=x", "https://youtube.com/watch?v=x")],
        [tab("https://youtube.com/watch?v=x", "https://youtube.com/watch?v=x")],
        [tab("Noah Kahan - Stick Season", "https://youtube.com/watch?v=x")],
    ])
    got = th._active_tab("Brave Browser", tries=3, delay=0)
    check("retries past a title that is still just the URL",
          got and got["title"] == "Noah Kahan - Stick Season")
    check("and stops as soon as it settles", calls["n"] == 3)

    calls = stub_tabs([[tab("Settled Immediately", "https://x")]])
    th._active_tab("Brave Browser", tries=3, delay=0)
    check("a settled title costs exactly one read", calls["n"] == 1)

    stub_tabs([[]])
    check("no tabs at all returns None", th._active_tab("Brave Browser", tries=2, delay=0) is None)

    def boom(app_name):
        raise RuntimeError("browser not scriptable")
    system_state._browser_tabs = boom
    check("an unscriptable browser returns None rather than raising",
          th._active_tab("Brave Browser", tries=2, delay=0) is None)

    print("\n— what Ted says about it —")
    stub_tabs([[tab("Noah Kahan - Stick Season", "https://youtube.com/watch?v=x")]])
    said = th._tab_report("Brave Browser", "YouTube")
    check("names the actual page title", "Noah Kahan - Stick Season" in said)
    check("does not hedge when it knows", "couldn't" not in said.lower())

    # Title never settles: the URL is still real information and beats silence.
    stub_tabs([[tab("https://example.com/thing", "https://example.com/thing")]])
    said = th._tab_report("Google Chrome", "example")
    check("falls back to the URL when the title never settles",
          "example.com/thing" in said)

    system_state._browser_tabs = boom
    said = th._tab_report("Brave Browser", "YouTube")
    check("separates what is known from what is not",
          "couldn't read the tab" in said.lower())
    check("still reports the opening it DID verify", "Opened YouTube" in said)
    check("does not claim to know the page", "showing" not in said.lower())

    print("\n— the tool exists and is reachable —")
    from core.tools import TOOL_SCHEMAS
    names = [s["function"]["name"] for s in TOOL_SCHEMAS]
    check("now_playing is a registered schema", "now_playing" in names)
    check("toggle_clock is gone", "toggle_clock" not in names)
    check("no duplicate schema names", len(names) == len(set(names)))

    from core import routing
    for phrase in ("what's playing", "what am I watching", "what is this song",
                   "skip this song"):
        picked = [routing.tool_name(s) for s in routing.select_tool_schemas(phrase)]
        check(f"'{phrase}' puts now_playing on the menu", "now_playing" in picked)
    # The menu is selected per request to keep the prompt small; a question
    # about the weather has no business carrying playback schemas.
    weather = [routing.tool_name(s) for s in routing.select_tool_schemas(
        "what's the weather tomorrow")]
    check("an unrelated question does not pay for now_playing",
          "now_playing" not in weather)

finally:
    system_state._browser_tabs = _real_tabs

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
