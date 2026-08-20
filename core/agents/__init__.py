"""Task-owning agents and their shared contracts."""

from core.agents.base import (AgentResult, BaseAgent, ConfirmationGate,
                              Delegation, Plan)
from core.agents.mac import MacAgent

__all__ = [
    "AgentResult", "BaseAgent", "ConfirmationGate", "Delegation", "Plan",
    "MacAgent",
]
