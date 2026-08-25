"""One verification-shaped result for every tool, including legacy strings."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class ActionOutcome:
    ok: bool
    changed: bool
    observed_state: dict = field(default_factory=dict)
    expected_state: dict = field(default_factory=dict)
    matches_goal: bool = False
    recoverable: bool = True
    report: str = ""
    failure: str = ""

    def as_dict(self):
        return asdict(self)


def normalize(tool, args, result, *, is_failure=None, acted=True):
    """Preserve rich AgentResult evidence and safely wrap old string tools."""
    if hasattr(result, "as_dict") and hasattr(result, "evidence"):
        raw = result.as_dict()
        ok = bool(raw.get("ok"))
        evidence = dict(raw.get("evidence") or {})
        report = raw.get("did") or raw.get("failed") or ""
        failure = raw.get("failed") or ""
    else:
        report = str(result or "")
        failed = bool(is_failure(report)) if is_failure else not bool(report.strip())
        ok = not failed
        evidence = {"tool": tool, "args": dict(args or {}), "result": report}
        failure = report if failed else ""
    expected = {"tool": tool, "args": dict(args or {})}
    # A successful legacy action is verified only as far as its handler's
    # returned evidence. MacAgent tools already return stronger observed state.
    return ActionOutcome(
        ok=ok, changed=bool(acted and ok), observed_state=evidence,
        expected_state=expected, matches_goal=ok, recoverable=not ok,
        report=report, failure=failure,
    )
