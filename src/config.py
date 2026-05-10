"""Configuration management for the Financial Digest system."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv


@dataclass
class RedditConfig:
    """Reddit API configuration."""
    client_id: str
    client_secret: str
    user_agent: str


@dataclass
class NewsConfig:
    """NewsAPI configuration."""
    api_key: str


@dataclass
class FredConfig:
    """FRED API configuration."""
    api_key: str


@dataclass
class DiscordConfig:
    """Discord webhook configuration."""
    webhook_url: str


@dataclass
class AnthropicConfig:
    """Anthropic API configuration."""
    api_key: str


@dataclass
class Config:
    """Main configuration class."""
    anthropic: AnthropicConfig
    reddit: RedditConfig
    news: NewsConfig
    fred: FredConfig
    discord: DiscordConfig

    @classmethod
    def from_env(cls, env_file: Optional[str] = None) -> "Config":
        """Load configuration from environment variables."""
        if env_file:
            load_dotenv(env_file)
        else:
            load_dotenv()

        return cls(
            anthropic=AnthropicConfig(
                api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            ),
            reddit=RedditConfig(
                client_id=os.getenv("REDDIT_CLIENT_ID", ""),
                client_secret=os.getenv("REDDIT_CLIENT_SECRET", ""),
                user_agent=os.getenv("REDDIT_USER_AGENT", "MiniHedgeFund/1.0"),
            ),
            news=NewsConfig(
                api_key=os.getenv("NEWS_API_KEY", ""),
            ),
            fred=FredConfig(
                api_key=os.getenv("FRED_API_KEY", ""),
            ),
            discord=DiscordConfig(
                webhook_url=os.getenv("DISCORD_WEBHOOK_URL", ""),
            ),
        )

    def validate(self) -> list[str]:
        """Validate configuration and return list of missing items.

        Reddit creds are optional (we default to unauth JSON).
        """
        missing = []

        if not self.anthropic.api_key:
            missing.append("ANTHROPIC_API_KEY")
        if not self.news.api_key:
            missing.append("NEWS_API_KEY")
        if not self.fred.api_key:
            missing.append("FRED_API_KEY")
        if not self.discord.webhook_url:
            missing.append("DISCORD_WEBHOOK_URL")

        return missing
