"""MacAgent: one owner for a complete Mac task, not one model call per click."""

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
        "ui_press",
    })
    CONSEQUENT_METHODS = frozenset({
        "clean_up", "close_app", "press_key", "type_text", "ui_fill", "ui_press",
    })
    DEFAULT_PROTECTED_APPS = frozenset({"Ted", "Python", "python3"})

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

    def _dry_run(self, method, args):
        if method == "clean_up":
            apps = list(self._list_apps() or [])
            targets = [app for app in apps if app not in self._protected]
            return AgentResult(
                ok=True,
                did="No apps were changed (dry run).",
                evidence={"dry_run": True, "would_close": targets},
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
            return self._clean_up()
        if method not in self.TOOLS:
            return AgentResult(False, "Nothing was changed.", {},
                               failed=f"MacAgent has no method called '{method}'.")
        return self._call(method, args)

    def _call(self, method, args):
        result = str(self._dispatch(method, dict(args)) or "").strip()
        failed = tool_handlers.looks_like_failure(result)
        return AgentResult(
            ok=not failed,
            did=result or "The tool returned no result.",
            evidence={"tool": method, "args": dict(args), "result": result},
            failed=result if failed else None,
        )

    def _clean_up(self):
        before = list(self._list_apps() or [])
        targets = [app for app in before if app not in self._protected]
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
            "results": results,
        }
        if not targets:
            return AgentResult(True, "No user apps needed closing.", evidence)
        if failures:
            did = (f"Closed {', '.join(closed)}." if closed else "No apps were closed.")
            return AgentResult(False, did, evidence,
                               failed=f"Could not close: {', '.join(failures)}.")
        return AgentResult(True, f"Closed {', '.join(closed)}.", evidence)
