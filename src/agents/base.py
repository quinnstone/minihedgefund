"""Base agent — Anthropic SDK wrapper with prompt caching + structured output.

Subclasses declare:
  - `name`: identifier used in logs and the audit trail
  - `model`: claude-opus-4-7 (judgment) or claude-haiku-4-5 (bulk classification)
  - `system_prompt()`: static role + rules (cached)
  - `user_prompt(input_data)`: per-run data (not cached)
  - `output_schema()`: JSON schema the response must conform to

The base class handles:
  - Anthropic API call with forced tool-use for guaranteed structured output
  - Prompt caching on the system message (5-minute TTL)
  - Token + latency capture for the per-week audit log
  - Retry on transient errors
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from anthropic import Anthropic, APIError

logger = logging.getLogger(__name__)


# Per the system prompt: Opus 4.7 = `claude-opus-4-7`, Haiku 4.5 = `claude-haiku-4-5-20251001`
MODEL_OPUS = "claude-opus-4-7"
MODEL_HAIKU = "claude-haiku-4-5-20251001"

DEFAULT_MAX_TOKENS = 4096
DEFAULT_RETRIES = 2


@dataclass
class AgentResult:
    """Outcome of one agent run — surfaces both the answer and the cost."""

    agent_name: str
    model: str
    success: bool
    output: dict = field(default_factory=dict)
    error: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    latency_seconds: float = 0.0

    @property
    def estimated_cost_usd(self) -> float:
        """Rough cost using public list prices for Opus 4.7 / Haiku 4.5."""
        if self.model == MODEL_OPUS:
            in_rate, out_rate, cache_read_rate = 15.0, 75.0, 1.5
        else:
            in_rate, out_rate, cache_read_rate = 1.0, 5.0, 0.1
        return (
            self.input_tokens * in_rate
            + self.output_tokens * out_rate
            + self.cache_read_tokens * cache_read_rate
        ) / 1_000_000

    def to_dict(self) -> dict:
        return {
            "agent_name": self.agent_name,
            "model": self.model,
            "success": self.success,
            "output": self.output,
            "error": self.error,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "latency_seconds": round(self.latency_seconds, 3),
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
        }


class BaseAgent(ABC):
    """Subclass and implement the four `@abstractmethod`s below."""

    name: str = "unnamed_agent"
    model: str = MODEL_OPUS
    max_tokens: int = DEFAULT_MAX_TOKENS

    def __init__(self, api_key: str, retries: int = DEFAULT_RETRIES):
        if not api_key:
            raise ValueError(f"{self.name}: ANTHROPIC_API_KEY is required")
        self._client = Anthropic(api_key=api_key)
        self._retries = retries

    @abstractmethod
    def system_prompt(self) -> str:
        """Static role/rules. Cached for cost efficiency."""

    @abstractmethod
    def user_prompt(self, input_data: dict) -> str:
        """Per-run user message — the data for this week."""

    @abstractmethod
    def output_schema(self) -> dict:
        """JSON Schema describing the required output object.

        Use the strictest schema you can — every constraint here is enforced
        by the API and saves a parsing failure downstream."""

    def tool_description(self) -> str:
        return f"Submit your structured analysis as {self.name}."

    def run(self, input_data: dict) -> AgentResult:
        tool_name = f"submit_{self.name}_output"
        tools = [{
            "name": tool_name,
            "description": self.tool_description(),
            "input_schema": self.output_schema(),
        }]

        messages = [{"role": "user", "content": self.user_prompt(input_data)}]
        system_blocks = [{
            "type": "text",
            "text": self.system_prompt(),
            "cache_control": {"type": "ephemeral"},
        }]

        last_error: Optional[str] = None
        started = time.time()

        for attempt in range(self._retries + 1):
            try:
                resp = self._client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=system_blocks,
                    tools=tools,
                    tool_choice={"type": "tool", "name": tool_name},
                    messages=messages,
                )
                output = self._extract_tool_input(resp, tool_name)
                usage = getattr(resp, "usage", None)
                return AgentResult(
                    agent_name=self.name,
                    model=self.model,
                    success=True,
                    output=output,
                    input_tokens=getattr(usage, "input_tokens", 0) or 0,
                    output_tokens=getattr(usage, "output_tokens", 0) or 0,
                    cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
                    cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
                    latency_seconds=time.time() - started,
                )
            except APIError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("agent %s attempt %d failed: %s", self.name, attempt + 1, last_error)
                if attempt < self._retries:
                    time.sleep(2 ** attempt)
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("agent %s unexpected error", self.name)
                break

        return AgentResult(
            agent_name=self.name,
            model=self.model,
            success=False,
            error=last_error or "unknown error",
            latency_seconds=time.time() - started,
        )

    @staticmethod
    def _extract_tool_input(response: Any, expected_tool_name: str) -> dict:
        for block in getattr(response, "content", []):
            if getattr(block, "type", "") == "tool_use" and getattr(block, "name", "") == expected_tool_name:
                return dict(block.input or {})
        raise ValueError(f"no tool_use block named {expected_tool_name!r} in response")
