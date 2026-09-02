"""Checks for Ted's notebook: exact pages, exact entries, and the index that
means he never has to guess which pages exist."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import notebook


PASS = FAIL = 0


def check(label, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ {label}")


with tempfile.TemporaryDirectory() as tmp:
    notebook.DB_PATH = os.path.join(tmp, "memory.db")

    print("\n— writing —")
    page, number, made = notebook.add_entry("fixes", "gate 5 eats arithmetic")
    check("the first write creates the page", made and page == "fixes" and number == 1)
    _, number2, made2 = notebook.add_entry("fixes", "barge-in floor too low")
    check("the second write appends to it", not made2 and number2 == 2)
    check("writing stores the text verbatim, unsummarised",
          notebook.read_page("fixes")["entries"][0]["body"] == "gate 5 eats arithmetic")

    print("\n— one page, however it is named —")
    notebook.add_entry("my fixes page", "third thing")
    check("'my fixes page' is the same page as 'fixes'",
          len(notebook.list_pages()) == 1 and notebook.read_page("fixes")["total"] == 3)
    notebook.add_entry("FIXES", "fourth thing")
    check("…and so is 'FIXES' — case is not a new page",
          len(notebook.list_pages()) == 1)
    check("the page keeps the capitalisation it was created with",
          notebook.list_pages()[0]["name"] == "fixes")

    print("\n— reading —")
    doc = notebook.read_page("fixes")
    check("entries come back oldest first, numbered from 1",
          [e["number"] for e in doc["entries"]] == [1, 2, 3, 4])
    check("a page that does not exist reads as None, not as empty",
          notebook.read_page("nothing here") is None)
    for i in range(80):
        notebook.add_entry("long", f"line {i}")
    big = notebook.read_page("long", limit=10)
    check("a long page is capped but still says how much there is",
          len(big["entries"]) == 10 and big["total"] == 80)
    check("…and the numbers on a capped read are still true page numbers",
          big["entries"][0]["number"] == 71 and big["entries"][-1]["number"] == 80)

    print("\n— editing —")
    _, edited = notebook.edit_entry("fixes", 2, "barge-in pitch floor too low on speakers")
    check("an edit replaces exactly the entry it names",
          edited == 2
          and notebook.read_page("fixes")["entries"][1]["body"].endswith("on speakers"))
    check("…and leaves its neighbours alone",
          notebook.read_page("fixes")["entries"][0]["body"] == "gate 5 eats arithmetic")
    _, last = notebook.edit_entry("fixes", -1, "rewritten last line")
    check("entry -1 means the last one", last == 4)

    def raises(fn, exc):
        try:
            fn()
        except exc:
            return True
        except Exception:
            return False
        return False

    check("editing an entry that isn't there raises rather than inventing one",
          raises(lambda: notebook.edit_entry("fixes", 99, "nope"), KeyError))
    check("editing a page that isn't there raises",
          raises(lambda: notebook.edit_entry("ghosts", 1, "nope"), KeyError))
    check("an empty entry is refused — a blank line is not a note",
          raises(lambda: notebook.add_entry("fixes", "   "), ValueError))
    check("an unnamed page is refused",
          raises(lambda: notebook.add_entry("", "something"), ValueError))

    print("\n— deleting —")
    _, gone_n, gone_body = notebook.delete_entry("fixes", 1)
    check("deleting an entry reports what was removed",
          gone_n == 1 and gone_body == "gate 5 eats arithmetic")
    check("…and the rest renumber, because a page is an ordered list",
          notebook.read_page("fixes")["entries"][0]["body"].endswith("on speakers"))
    _, removed = notebook.delete_page("fixes")
    check("deleting a page reports how much went with it", removed == 3)
    check("…and the page is gone", notebook.read_page("fixes") is None)

    print("\n— searching —")
    notebook.add_entry("ideas", "a chat search over old sessions")
    hits = notebook.search("chat search")
    check("search finds the entry and names its page",
          len(hits) == 1 and hits[0]["page"] == "ideas")
    check("search is exact, not fuzzy — a non-match finds nothing",
          notebook.search("zzzz") == [])
    check("an empty query finds nothing rather than everything",
          notebook.search("") == [])
    notebook.add_entry("ideas", "100% of the time")
    check("a LIKE wildcard in the query is a literal, not a wildcard",
          notebook.search("100%")[0]["body"] == "100% of the time"
          and notebook.search("%")  # matches the literal percent sign only
          and all("%" in h["body"] for h in notebook.search("%")))

    print("\n— the per-turn index —")
    line = notebook.index_line()
    check("the index names every page so Ted never guesses which exist",
          "ideas" in line and "long" in line)
    check("…with sizes, so he knows a page is empty before reading it",
          "(2 entries)" in line)
    check("…and never leaks contents into every prompt",
          "chat search" not in line)
    with tempfile.TemporaryDirectory() as tmp2:
        notebook.DB_PATH = os.path.join(tmp2, "memory.db")
        check("an empty notebook costs nothing in the prompt",
              notebook.index_line() == "")
        notebook.add_entry("solo", "just one")
        check("…and one entry is described in the singular",
              "(1 entry)" in notebook.index_line())

    print("\n— renaming —")
    notebook.DB_PATH = os.path.join(tmp, "memory.db")
    was, now = notebook.rename_page("ideas", "someday")
    check("renaming keeps the entries", was == "ideas"
          and notebook.read_page("someday")["total"] == 2)
    notebook.add_entry("keep", "x")
    check("renaming onto an existing page is refused rather than merging",
          raises(lambda: notebook.rename_page("someday", "keep"), ValueError))


# ── the wiring, checked against the files themselves ────────────────────────
# The storage layer being right is half of it. These read the source so a
# notebook that works but is never reachable — no tool, no dispatch, no index
# in the prompt — fails here instead of failing silently in conversation.
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    return open(os.path.join(_root, rel), encoding="utf-8").read()


print("\n— the wiring —")
from core.tools import TOOL_SCHEMAS
_names = {t["function"]["name"] for t in TOOL_SCHEMAS}
check("all five notebook tools are on the menu",
      {"notebook_read", "notebook_write", "notebook_edit",
       "notebook_delete", "notebook_search"} <= _names)

app_src = _read("core/app.py")
check("…and every one of them is dispatched",
      all(f'name == "{n}"' in app_src for n in
          ("notebook_read", "notebook_write", "notebook_edit",
           "notebook_delete", "notebook_search")))

from core import tool_handlers as th
check("writing, editing and deleting count as actions, so their result is "
      "spoken verbatim",
      {"notebook_write", "notebook_edit", "notebook_delete"} <= th.ACTION_TOOLS)
check("deleting a whole page needs a yes first",
      th.needs_confirmation("notebook_delete", {"page": "fixes"}))
check("…but crossing out one entry does not",
      not th.needs_confirmation("notebook_delete", {"page": "fixes", "entry": 2}))

llm_src = _read("core/llm.py")
check("the page index is loaded only when notebook tools are relevant",
      "_load_notebook" in llm_src
      and 'name.startswith("notebook_")' in llm_src)
check("…and reaches the prompt",
      "notebook_index" in llm_src and "notebook.index_line()" in llm_src)
check("the persona tells Ted the notebook is his to read, write and edit",
      "notebook_read" in llm_src and "never guessed" in llm_src)

hud = _read("ui/ted_hud.html")
check("there is a Notebook button in the sidebar",
      'id="notebtn"' in hud and "tedHud.toggleNotebook()" in hud)
check("…that loads the notebook page",
      "127.0.0.1:5175/notebook" in hud)
check("…and stops refreshing when it is closed",
      "hideNotebook:function" in hud and "about:blank" in hud)

paper = _read("dashboard/notebook.html")
check("the page is ruled paper written in a handwriting face",
      "repeating-linear-gradient" in paper and "Bradley Hand" in paper)
check("…and its text sits on the rules rather than between them",
      "--rule:30px" in paper and "line-height:var(--rule)" in paper)
check("every line on it is editable in place",
      'contenteditable="true"' in paper)

print("\n— the pixel pet —")
pet_html = _read("ui/ted_pet.html")
check("the pet has voice, silent transcription, and text controls",
      all(f'id="{name}"' in pet_html for name in
          ("voice", "transcribe", "text", "pet-input")))
check("right-click reveals only the small close-pet x",
      "oncontextmenu" in pet_html and 'id="close-x"' in pet_html
      and "shutdown_ted" not in pet_html)
check("the pet can close while Ted keeps running",
      "pet_close" in pet_html and "def pet_close" in app_src)
check("typed pet turns are mirrored into the full conversation",
      "pet_ask" in pet_html and "def pet_ask" in app_src)
check("text mode explicitly focuses the native pet before the textarea",
      "pet_focus" in pet_html and "def pet_focus" in app_src
      and "focus=True" in _read("core/pet.py"))
check("only double-clicking Ted restores the full dashboard",
      "ondblclick" in pet_html and "pet_open_dashboard" in pet_html
      and "def pet_open_dashboard" in app_src and "document.onmouseup" not in pet_html)
check("the icon controls stay hidden until Ted or the area below is hovered",
      "#character:hover~#controls" in pet_html
      and "#hover-zone:hover~#controls" in pet_html
      and "opacity:0" in pet_html and "pointer-events:none" in pet_html)
check("inactive first-click is dispatched away from Cocoa's event thread",
      "acceptsFirstMouse:" in _read("core/pet.py")
      and "setAcceptsMouseMovedEvents_" in _read("core/pet.py")
      and "addGlobalMonitorForEventsMatchingMask_handler_" in _read("core/pet.py")
      and 'name="pet-click"' in _read("core/pet.py")
      and "scheduledTimerWithTimeInterval_repeats_block_" not in _read("core/pet.py")
      and "addLocalMonitorForEventsMatchingMask_handler_" not in _read("core/pet.py")
      and "nativePress:function" in pet_html)
check("Ted uses one unified large-head, small-body sprite",
      'id="bear"' in pet_html and 'src="ted_pet_chibi.png"' in pet_html
      and "#head-piece" not in pet_html and "#body-piece" not in pet_html)
check("the Messages-style composer sits under the controls and grows downward",
      "pet_resize_input" in pet_html and "def pet_resize_input" in app_src
      and "FixPoint.NORTH | FixPoint.WEST" in _read("core/pet.py")
      and "border-radius:21px" in pet_html)
check("the smooth comic bubble stays beside Ted and off his face",
      "#bubble{" in pet_html and "right:8px;top:9px;width:126px" in pet_html
      and "border-radius:48%" in pet_html)
check("Ted has sleepy, thinking, and long-work animations",
      all(token in pet_html for token in
          ('id="sleep"', 'id="glasses"', 'id="chalkboard"', 'id="work-prop"', "state='working'")))
check("pet controls use lightweight translucent rounded glass styling",
      "backdrop-filter:blur(6px)" in pet_html and "border-radius:50%" in pet_html
      and "visibility:hidden" in pet_html)
check("the full HUD can reopen a closed pet",
      'id="petbtn"' in hud and "api().pet_open()" in hud
      and "def pet_open" in app_src)
check("the pet opens only from the HUD button, never automatically at startup",
      "pet.open_pet(webview, api)" not in _read("hud.py")
      and "pet.open_pet(webview, js_api=self)" in app_src)
check("the old in-chat bear surfaces remain removed",
      'id="bear-id"' not in hud and "ted_bear.js" not in hud)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
