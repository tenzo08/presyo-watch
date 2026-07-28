"""Tests for settings validation.

The User-Agent check is the interesting one: rule 7 is a promise to the people running
these government servers, and this is where it stops being a promise and becomes a
precondition for the process starting at all.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from presyowatch.config import Settings

VALID_UA = "PresyoWatch/0.1 (+https://example.invalid; contact: tester@example.ph)"
VALID_DB = "postgresql+psycopg://u:p@host.neon.tech/db?sslmode=require"


def build(**overrides: str) -> Settings:
    values: dict[str, str] = {
        "database_url": VALID_DB,
        "http_user_agent": VALID_UA,
        "raw_cache_dir": "./data/cache/raw",
    }
    values.update(overrides)
    # `model_validate` rather than `Settings(...)`: it validates exactly this dict and
    # never consults the process environment or a developer's real .env, so these tests
    # assert the validators rather than whatever happens to be exported locally.
    return Settings.model_validate(values)


def test_valid_settings_load() -> None:
    settings = build()

    assert settings.http_user_agent == VALID_UA
    assert settings.raw_cache_dir == Path("./data/cache/raw")


def test_settings_are_frozen() -> None:
    """Configuration read once at startup should not drift mid-run."""
    settings = build()

    with pytest.raises(ValidationError):
        settings.http_user_agent = "something else"


@pytest.mark.parametrize(
    "user_agent",
    [
        "PresyoWatch/0.1",
        "Mozilla/5.0 (compatible)",
        "",
        "PresyoWatch (+https://github.com/tenzo08/presyo-watch)",
        "contact: not-an-email",
    ],
)
def test_user_agent_without_a_contact_email_is_refused(user_agent: str) -> None:
    with pytest.raises(ValidationError, match="contact email"):
        build(http_user_agent=user_agent)


@pytest.mark.parametrize(
    "user_agent",
    [
        "PresyoWatch/0.1 (contact: a@b.ph)",
        "PresyoWatch/0.1 (+url; mailto:someone@example.co.uk)",
        "bot someone.else@sub.domain.gov.ph here",
    ],
)
def test_user_agent_with_a_contact_email_is_accepted(user_agent: str) -> None:
    assert build(http_user_agent=user_agent).http_user_agent == user_agent


@pytest.mark.parametrize(
    "database_url",
    ["mysql://u:p@h/db", "sqlite:///local.db", "host.neon.tech/db", ""],
)
def test_non_postgres_database_url_is_refused(database_url: str) -> None:
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        build(database_url=database_url)


def test_env_example_documents_every_required_setting() -> None:
    """`.env.example` is the contract with whoever deploys this.

    A required setting missing from it means a first deploy fails with a validation
    error and no hint about what to add.
    """
    example = Path(__file__).parent.parent / ".env.example"
    text = example.read_text(encoding="utf-8")

    required = {
        name.upper() for name, field in Settings.model_fields.items() if field.is_required()
    }
    documented = {
        line.split("=", 1)[0].strip()
        for line in text.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }

    assert required <= documented, f"undocumented in .env.example: {required - documented}"
