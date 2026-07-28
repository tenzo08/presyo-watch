"""Settings loaded from the environment.

Deliberately holds only the variables documented in ``.env.example``. Tunables that
have sensible defaults and no deployment-specific value live with the component that
uses them, not here — an environment variable nobody sets is just a config file with
extra steps.
"""

import re
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Deliberately loose. The point is to prove a human is reachable, not to validate the
# grammar of RFC 5322.
_CONTACT_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")


class Settings(BaseSettings):
    """Runtime configuration, read from the process environment or ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    database_url: str
    http_user_agent: str
    raw_cache_dir: Path

    @field_validator("database_url")
    @classmethod
    def _must_be_postgres(cls, value: str) -> str:
        if not value.startswith("postgresql"):
            msg = "DATABASE_URL must be a postgresql:// or postgresql+driver:// URL"
            raise ValueError(msg)
        return value

    @field_validator("http_user_agent")
    @classmethod
    def _must_identify_a_human(cls, value: str) -> str:
        """Enforce CLAUDE.md rule 7 at the boundary rather than by convention.

        A source operator who wants us to stop must be able to find out who we are and
        tell us so. If the User-Agent cannot carry that, the process should not start.
        """
        if not _CONTACT_EMAIL.search(value):
            msg = (
                "HTTP_USER_AGENT must contain a contact email address so source "
                "operators can reach a human (CLAUDE.md rule 7). "
                f"Got: {value!r}"
            )
            raise ValueError(msg)
        return value
