"""Discord webhook sender."""

from __future__ import annotations

import logging

import requests

from ..config import DiscordConfig

logger = logging.getLogger(__name__)


class DiscordSender:
    """Sends messages to Discord via webhook."""

    def __init__(self, config: DiscordConfig):
        self.config = config

    def send(self, embeds_payload: list[dict]) -> bool:
        """
        Send embeds to the Discord webhook.

        Discord allows max 10 embeds per message, so we chunk if needed.

        Returns:
            True if all messages sent successfully.
        """
        if not self.config.webhook_url:
            logger.error("No Discord webhook URL configured")
            return False

        chunks = [embeds_payload[i:i + 10] for i in range(0, len(embeds_payload), 10)]

        for i, chunk in enumerate(chunks):
            payload = {"embeds": chunk}
            # Add a username for the first message
            if i == 0:
                payload["username"] = "Financial Digest"

            try:
                resp = requests.post(
                    self.config.webhook_url,
                    json=payload,
                    timeout=30,
                )
                resp.raise_for_status()
            except requests.exceptions.HTTPError as e:
                logger.error(f"Discord webhook HTTP error: {e} - {resp.text}")
                return False
            except requests.exceptions.RequestException as e:
                logger.error(f"Discord webhook request failed: {e}")
                return False

        logger.info(f"Discord digest sent ({len(embeds_payload)} embeds in {len(chunks)} messages)")
        return True

    def send_test(self) -> bool:
        """Send a test message to verify webhook configuration."""
        embed = {
            "title": "Financial Digest - Test Message",
            "description": (
                "Your Discord webhook is configured correctly.\n"
                "You will receive the Weekly Financial Digest every Sunday at 8am EST."
            ),
            "color": 0x00AA00,
        }
        return self.send([embed])
