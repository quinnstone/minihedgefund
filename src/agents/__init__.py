"""Agent layer — scouts, synthesis, risk, tax, PM, reflection.

Every agent is a thin wrapper around the Anthropic SDK that:
  - Accepts structured input (dict)
  - Returns structured output (dict matching its declared schema)
  - Logs token usage for the audit trail

The PM is the only agent with decision authority. Everything else
processes information.
"""

from .base import AgentResult, BaseAgent

__all__ = ["AgentResult", "BaseAgent"]
