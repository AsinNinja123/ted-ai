"""MacAgent: one owner for a complete Mac task, not one model call per click.

The Ted Code Book — Chapter 36. Start at §36.4 if you are here because something
recursed, §36.5 if you are here about a confirmation prompt.
"""

from __future__ import annotations

from core.agents.base import AgentResult, BaseAgent
from core import tool_handlers


class MacAgent(BaseAgent):
    """Wrap the existing tool dispatcher while orchestration moves out of it.

    ``dispatch`` is injected deliberately. During migration it can be the bound
    ``TedApi._dispatch_tool`` method; later it can point directly at the kept
    handler registry. Either way, the agent owns the multi-step loop now.
    """

    name = "MacAgent"
    TOOLS = frozenset({
        "open_app", "close_app", "press_key", "type_text", "scroll",
        "system_volume", "system_brightness", "clipboard_read",
        "clipboard_write", "screen_describe", "ui_inspect", "ui_fill",
        "ui_press", "terminal_read",
    })
    # Empty on purpose. Whether a call is consequential is decided by
    # core/tool_handlers.needs_confirmation, which TedApi asks before it ever
    # reaches this agent (§11.7). Holding a second opinion here is how the two
    # paths drift apart: closing, typing and clicking were all immediate before
    # the agent existed, and listing them here would have quietly added a yes/no
    # prompt Charlie never asked for.
    CONSEQUENT_METHODS = frozenset()
    # clean_up closes everything in one shot with no per-app model judgment in
    # between, so this list is the ONLY thing standing between "tidy up" and
    # Ted quitting himself. It deliberately does not trust
    # core/actions._SELF_PROCESSES, which is currently {""} in the working tree
    # and therefore protects nothing. Terminal and iTerm are here because Ted
    # is often launched from one, and closing his own host mid-loop ends the
    # turn with no reply and no error.
    DEFAULT_PROTECTED_APPS = frozenset({
        "Ted", "Python", "python3", "Terminal", "iTerm2", "iTerm",
        "Electron", "Finder",
    })

    def __init__(self, dispatch, list_apps, *, protected_apps=None,
                 confirmation_gate=None):
        super().__init__(confirmation_gate=confirmation_gate)
        self._dispatch = dispatch
        self._list_apps = list_apps
        self._protected = frozenset(protected_apps or self.DEFAULT_PROTECTED_APPS)

    def describe(self):
        try:
            apps = list(self._list_apps() or [])
        except Exception as exc:
            return f"MacAgent: app state unavailable ({exc})"
        visible = [app for app in apps if app not in self._protected]
        if not visible:
            return "MacAgent: no closable user apps are open"
        return f"MacAgent: {len(visible)} closable user app{'s' if len(visible) != 1 else ''} open"

    def needs_confirmation(self, method, args):
        return method in self.CONSEQUENT_METHODS

    def _targets(self, args):
        """Apps this cleanup would close: open, not protected, not spared.

        `exclude` holds names the user asked to keep, already resolved to real
        running app names by the caller — this agent does not guess at spelling.
        """
        spared = {str(name).lower() for name in (args or {}).get("exclude") or ()}
        return [app for app in (self._list_apps() or [])
                if app not in self._protected and app.lower() not in spared]

    def _dry_run(self, method, args):
        if method == "clean_up":
            return AgentResult(
                ok=True,
                did="No apps were changed (dry run).",
                evidence={"dry_run": True, "would_close": self._targets(args),
                          "spared": list((args or {}).get("exclude") or ())},
            )
        if method not in self.TOOLS:
            return AgentResult(False, "Nothing was changed.", {},
                               failed=f"MacAgent has no method called '{method}'.")
        return AgentResult(
            ok=True,
            did="Nothing was changed (dry run).",
            evidence={"dry_run": True, "would_call": method, "args": dict(args)},
        )

    def _run(self, method, args):
        if method == "clean_up":
            return self._clean_up(args)
        if method not in self.TOOLS:
            return AgentResult(False, "Nothing was changed.", {},
                               failed=f"MacAgent has no method called '{method}'.")
        return self._call(method, args)

    def _call(self, method, args):
        result = str(self._dispatch(method, dict(args)) or "").strip()
        evidence = {"tool": method, "args": dict(args), "result": result}
        # A wall gets its own wording (§11.9). Ted stopping is correct here —
        # macOS will not let anyone click its permission dialogs but Charlie —
        # so the useful thing is saying WHICH wall, immediately, in the thought
        # bubble, instead of going quiet and waiting to be prodded.
        if tool_handlers.needs_human_hand(result):
            evidence["blocked_on"] = "human"
            return AgentResult(
                ok=False,
                did=result,
                evidence=evidence,
                failed=f"{result} I can see it but I can't click it — "
                       f"that one needs you.",
            )
        failed = tool_handlers.looks_like_failure(result)
        return AgentResult(
            ok=not failed,
            did=result or "The tool returned no result.",
            evidence=evidence,
            failed=result if failed else None,
        )

    def _clean_up(self, args=None):
        before = list(self._list_apps() or [])
        targets = self._targets(args)
        spared = list((args or {}).get("exclude") or ())
        results = []
        closed = []
        failures = []
        for app in targets:
            raw = str(self._dispatch("close_app", {"name": app}) or "").strip()
            ok = not tool_handlers.looks_like_failure(raw)
            results.append({"app": app, "ok": ok, "result": raw})
            (closed if ok else failures).append(app)

        evidence = {
            "apps_before": before,
            "attempted": targets,
            "closed": closed,
            "spared": spared,
            "results": results,
        }
        if not targets:
            return AgentResult(True, "No user apps needed closing.", evidence)
        if failures:
            did = (f"Closed {', '.join(closed)}." if closed else "No apps were closed.")
            return AgentResult(False, did, evidence,
                               failed=f"Could not close: {', '.join(failures)}.")
        did = f"Closed {', '.join(closed)}."
        if spared:
            did += f" Left {', '.join(spared)} open."
        return AgentResult(True, did, evidence)
