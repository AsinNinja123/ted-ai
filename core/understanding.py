"""A compact, inspectable record of what Ted believes a turn means.

This is not a second chatbot call. It resolves the cheap parts locally—lingo,
references to the active task, constraints, and whether clarification is
warranted—then gives the main model that interpretation alongside the original
words. The original text always wins; this record is evidence, not authority.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re


_REFERENTIAL = re.compile(
    r"\b(?:it|that|that one|this|them|him|her|the other one|same thing|go ahead|"
    r"continue|keep going|pick up where we left off|again|repeat(?: that| it)?|"
    r"one more time|same(?: thing| way| task)?|move it|change it|send it|do it)\b", re.I)
_TASK_RECALL = re.compile(
    r"\b(?:what (?:were|are) we (?:doing|working on)|where were we|"
    r"what(?:'s| is) the current (?:task|plan)|what did we finish)\b", re.I)
_CORRECTION = re.compile(
    r"^(?:no[, ]+|actually\b|instead\b|i meant\b|not that\b|the other\b)", re.I)
_CONSTRAINT = re.compile(
    r"\b(?:but|except|without|leave|keep|don't|do not|before|after|at|on|using|use)\b.{0,90}",
    re.I,
)
_HIGH_RISK = re.compile(
    r"\b(?:send|email|text|delete|remove|forget|purchase|buy|pay|post|submit|cancel)\b", re.I)


@dataclass
class TurnInterpretation:
    original: str
    expanded: str
    mode: str
    goal: str
    references: dict = field(default_factory=dict)
    constraints: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    confidence: float = 0.8
    clarification_policy: str = "assume_safe"
    continues_task_id: int | None = None
    retrieval_sources: list[str] = field(default_factory=list)

    def as_dict(self):
        return asdict(self)

    def for_prompt(self):
        refs = "; ".join(f"{key} → {value}" for key, value in self.references.items())
        parts = [f"mode={self.mode}", f"goal={self.goal}",
                 f"confidence={self.confidence:.2f}",
                 f"clarification={self.clarification_policy}"]
        if refs:
            parts.append("references=" + refs)
        if self.constraints:
            parts.append("constraints=" + "; ".join(self.constraints))
        if self.missing_information:
            parts.append("missing=" + "; ".join(self.missing_information))
        if self.retrieval_sources:
            parts.append("sources=" + ",".join(self.retrieval_sources))
        return (
            "TURN INTERPRETATION (local, advisory; Charlie's original words win): "
            + " | ".join(parts)
        )


def resolve(text, expanded=None, *, action_likely=False, active_task=None,
            recent_actions=()):
    original = " ".join(str(text or "").strip().split())
    expanded = " ".join(str(expanded or original).strip().split())
    active_task = active_task or {}
    is_reference = bool(_REFERENTIAL.search(original))
    task_recall = bool(_TASK_RECALL.search(original))
    # "that I am late" and "this project" introduce a subject; they are not
    # unresolved pointers to an earlier turn.
    if re.search(r"\b(?:that|this)\s+(?:i|you|he|she|we|they|the|project|answer|class)\b",
                 original, re.I):
        is_reference = False
    correction = bool(_CORRECTION.search(original) or re.search(r"\binstead\b", original, re.I))
    references = {}
    task_id = None

    if (is_reference or task_recall) and active_task:
        task_id = active_task.get("id")
        references["referring language"] = active_task.get("goal", "the active task")
    elif is_reference and recent_actions:
        last = list(recent_actions)[-1]
        references["referring language"] = (
            f"the recent {last.get('tool', 'action')} action: {last.get('result', '')}"
        ).strip()

    if task_recall:
        mode = "information"
    elif action_likely or correction or is_reference:
        mode = "action"
    elif original.endswith("?") or re.match(
            r"^(?:what|why|how|when|where|who|should|is|are|do|does|can)\b", original, re.I):
        mode = "information"
    else:
        mode = "conversation"

    goal = expanded
    if correction and active_task:
        goal = f"Revise active goal '{active_task.get('goal', '')}' with: {expanded}"
    elif is_reference and active_task:
        goal = f"Continue active goal '{active_task.get('goal', '')}' with: {expanded}"

    constraints = [m.group(0).strip(" .,;") for m in _CONSTRAINT.finditer(expanded)][:4]
    missing = []
    confidence = 0.88
    policy = "assume_safe"
    if is_reference and not references and len(original.split()) <= 5:
        missing.append("the referent for the referring phrase")
        confidence = 0.48
        policy = "ask_one_question"
    elif correction and active_task:
        confidence = 0.94
    elif references:
        confidence = 0.91

    if mode == "action" and _HIGH_RISK.search(expanded):
        policy = "confirm_before_external_or_destructive_action"
    if missing:
        policy = "ask_one_question"

    if mode == "action":
        sources = ["active_task", "operational_state", "approved_preferences"]
    elif re.search(r"\b(?:current|latest|today|tomorrow|right now|price|weather|news)\b",
                   expanded, re.I):
        sources = ["live_tool", "recent_history"]
    elif mode == "information":
        sources = ["known_facts", "session_memory", "knowledge"]
    else:
        sources = ["recent_history", "approved_relationship_memory"]

    return TurnInterpretation(
        original=original, expanded=expanded, mode=mode, goal=goal,
        references=references, constraints=constraints,
        missing_information=missing, confidence=confidence,
        clarification_policy=policy, continues_task_id=task_id,
        retrieval_sources=sources,
    )
