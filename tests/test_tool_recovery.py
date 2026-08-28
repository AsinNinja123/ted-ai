"""Router misses must not cost round trips.

The failure this file pins, straight out of data/ted_launch.log:

    [tools] rejected web_search: unknown tool 'web_search'   (x4)
    [tools] rejected notebook_read: unknown tool 'notebook_read'
    [timing] round 3 after 12522ms
    [tools] find_tools({'query': 'read notebook pages'})
    [timing] round 4 after 19431ms

select_tool_schemas hands over a small menu. When it guesses the family wrong,
the model reaches for a tool that exists but was not sent. Every one of those
rounds re-sends the whole ~4,400-token prompt, so a wrong guess cost more than
the entire schema budget it was saving.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import routing
from core.llm import ToolRuntime
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
    return [routing.tool_name(s) for s in schemas]


def runtime(menu, **kw):
    kw.setdefault("catalog", routing.catalog())
    return ToolRuntime(
        schemas=[routing.catalog()[n] for n in menu],
        dispatch=lambda name, args: f"ran {name}",
        **kw)


print("— the catalogue is readable and is a copy —")
cat = routing.catalog()
check("every tool contract is reachable by name",
      len(cat) == len(TOOL_SCHEMAS) and "web_search" in cat)
cat.pop("web_search")
check("mutating the returned catalogue cannot corrupt the router",
      "web_search" in routing.catalog())
check("the capability Charlie removed is still absent from the catalogue",
      "code_write" not in routing.catalog())

print("\n— a router miss is recovered in the same round, not two later —")
rt = runtime(["open_app", "close_app"])
check("a real tool the menu omitted is admitted on demand",
      routing.tool_name(rt.admit("web_search")) == "web_search")
check("the admitted tool is visible to the next round too",
      "web_search" in rt.schema_by_name and "web_search" in names(rt.schemas))
check("admitting is recorded so router misses can be counted",
      rt.admitted == ["web_search"])
check("a tool already on the menu is returned without re-adding it",
      routing.tool_name(rt.admit("open_app")) == "open_app"
      and rt.admitted == ["web_search"])
check("a tool Ted genuinely does not have is still refused",
      rt.admit("launch_missiles") is None)

print("\n— a confused model cannot pull the whole catalogue —")
rt = runtime(["open_app"], max_admissions=2)
rt.admit("web_search"); rt.admit("calculate")
check("admissions stop at the bound",
      rt.admit("get_weather") is None and len(rt.admitted) == 2)
check("the menu stays small after recovery", len(rt.schemas) == 3)

print("\n— opting out keeps the old strict behaviour —")
rt = ToolRuntime(schemas=[routing.catalog()["open_app"]],
                 dispatch=lambda n, a: "x")
check("no catalogue means an unseen name stays an error",
      rt.admit("web_search") is None)

print("\n— find_tools returns capabilities, not whatever shared a common word —")
found = names(routing.discover_tool_schemas("current date and recent sports results"))
check("the sports query loads web_search", "web_search" in found)
check("it no longer loads code_overview, bouncer_status, get_emails or now_playing",
      not ({"code_overview", "bouncer_status", "get_emails", "now_playing"}
           & set(found)))
found = names(routing.discover_tool_schemas("send Gavin a message about tonight"))
check("a messaging query loads send_message", "send_message" in found)
check("it does not pad the menu with unrelated contracts",
      not ({"code_search", "browse_to", "add_knowledge"} & set(found)))
found = names(routing.discover_tool_schemas("read notebook pages"))
check("a notebook query loads the notebook tools",
      "notebook_read" in found)
check("discovery respects the exclude list",
      "web_search" not in names(routing.discover_tool_schemas(
          "look up the current score", exclude=("web_search",))))
check("a query with no capability in it loads nothing at all",
      routing.discover_tool_schemas("hmm okay sure whatever") == [])
check("discovery stays within its limit",
      len(routing.discover_tool_schemas("play some music", limit=2)) <= 2)

print("\n— a capability with no family at all sends an empty menu —")
check("Ted's notebook now has a family instead of producing no tools",
      "notebook_read" in names(routing.select_tool_schemas(
          "read my notebook page about things to add to ted")))
check("asking to write in the notebook loads the write side too",
      "notebook_write" in names(routing.select_tool_schemas(
          "jot that down in your notebook")))
check("Apple Notes is still a separate family and is not swallowed",
      names(routing.select_tool_schemas("take a note that I owe Sam $20"))
      == ["notes_add", "notes_get"])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
