"""Discord webhook composition + delivery."""

from .composer import compose_digest, compose_error
from .sender import DiscordSender

__all__ = ["compose_digest", "compose_error", "DiscordSender"]
