"""Discord webhook sender.

Discord limits a single message to:
  - At most 10 embeds, AND
  - At most 6000 characters TOTAL across all embeds in the message, summed
    over title + description + footer.text + every fields[].name and
    fields[].value across every embed.

The 6000 cap is per-MESSAGE-summed, not per-embed. As the report grows
(accumulating reflection lessons, more open positions, more weekly actions)
we routinely brushed that ceiling. send() now chunks the embed list into
messages such that each message stays under both limits, sending multiple
POSTs back-to-back when necessary.
"""

from __future__ import annotations

import logging

import requests

from ..config import DiscordConfig

logger = logging.getLogger(__name__)

# Conservative ceiling — Discord's hard limit is 6000, leave headroom for
# JSON encoding overhead and any character-counting quirks.
MAX_CHARS_PER_MESSAGE = 5800
MAX_EMBEDS_PER_MESSAGE = 10


def _embed_char_count(embed: dict) -> int:
    """Sum the fields Discord counts toward the 6000-char-per-message cap.

    Per Discord API docs: "The combined sum of characters in all title,
    description, field.name, field.value, footer.text, and author.name
    fields across all embeds attached to a message must not exceed 6000."
    """
    total = 0
    total += len(embed.get("title") or "")
    total += len(embed.get("description") or "")
    footer = embed.get("footer") or {}
    total += len(footer.get("text") or "")
    author = embed.get("author") or {}
    total += len(author.get("name") or "")
    for f in embed.get("fields") or []:
        total += len(f.get("name") or "")
        total += len(f.get("value") or "")
    return total


def chunk_embeds(
    embeds: list[dict],
    max_chars: int = MAX_CHARS_PER_MESSAGE,
    max_count: int = MAX_EMBEDS_PER_MESSAGE,
) -> list[list[dict]]:
    """Split an embeds list into batches that each fit Discord's per-message limits."""
    batches: list[list[dict]] = []
    current: list[dict] = []
    current_chars = 0

    for e in embeds:
        e_chars = _embed_char_count(e)

        # Start a new batch if adding this embed would bust either limit
        would_overflow = (
            current and (
                current_chars + e_chars > max_chars
                or len(current) >= max_count
            )
        )
        if would_overflow:
            batches.append(current)
            current = []
            current_chars = 0

        current.append(e)
        current_chars += e_chars

    if current:
        batches.append(current)
    return batches


class DiscordSender:
    """Sends messages to Discord via webhook."""

    def __init__(self, config: DiscordConfig):
        self.config = config

    def send(self, embeds_payload: list[dict]) -> bool:
        """Send embeds via webhook. Chunks into multiple messages if needed.

        Returns True iff every chunk was accepted by Discord.
        """
        if not self.config.webhook_url:
            logger.error("No Discord webhook URL configured")
            return False

        batches = chunk_embeds(embeds_payload)

        for i, chunk in enumerate(batches):
            payload = {"embeds": chunk}
            # Add a username for the first message
            if i == 0:
                payload["username"] = "MiniHedgeFund"

            try:
                resp = requests.post(
                    self.config.webhook_url,
                    json=payload,
                    timeout=30,
                )
                resp.raise_for_status()
            except requests.exceptions.HTTPError as e:
                logger.error(
                    "Discord webhook HTTP error on chunk %d/%d: %s - %s",
                    i + 1, len(batches), e, resp.text,
                )
                return False
            except requests.exceptions.RequestException as e:
                logger.error("Discord webhook request failed: %s", e)
                return False

        logger.info(
            "Discord digest sent (%d embeds in %d messages)",
            len(embeds_payload), len(batches),
        )
        return True

    def send_test(self) -> bool:
        """Send a test message to verify webhook configuration."""
        embed = {
            "title": "MiniHedgeFund — Test Message",
            "description": (
                "Your Discord webhook is configured correctly.\n"
                "You will receive the Monday cycle every week at 10am ET."
            ),
            "color": 0x00AA00,
        }
        return self.send([embed])
