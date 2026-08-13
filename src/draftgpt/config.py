"""Runtime configuration. Credentials come from the environment only."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]

# Bumped whenever a deterministic formula or weight changes. Stored on every
# recommendation so old advice is interpretable after the engine evolves.
CALCULATION_VERSION = "calc-2026.08.0"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DRAFTGPT_", env_file=".env", extra="ignore", case_sensitive=False
    )

    database_url: str = Field(default="sqlite:///./data/draftgpt.db")
    season: int = Field(default=2026)

    league_provider: str = Field(default="espn")
    espn_league_id: str | None = Field(default=None)
    espn_s2: str | None = Field(default=None, repr=False)
    espn_swid: str | None = Field(default=None, repr=False)

    explanations_enabled: bool = Field(default=False)
    explanation_model: str = Field(default="claude-sonnet-5")

    fixtures_dir: Path = Field(default=REPO_ROOT / "fixtures")
    http_timeout_seconds: float = Field(default=20.0)

    @property
    def espn_cookies(self) -> dict[str, str]:
        """Cookie jar for private-league reads. Empty for public leagues."""
        if self.espn_s2 and self.espn_swid:
            swid = self.espn_swid
            if not swid.startswith("{"):
                swid = "{" + swid.strip("{}") + "}"
            return {"espn_s2": self.espn_s2, "SWID": swid}
        return {}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
