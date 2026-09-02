"""Runtime configuration. Credentials come from the environment only.

Nothing here is read at import time beyond defaults, so tests can point
``HUDDLE_DATABASE_URL`` at a temporary file and call :func:`reset_settings`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Bumped whenever a scoring formula or weight changes. Stamped onto every
#: digest run so an old episode stays interpretable after the engine evolves.
CALCULATION_VERSION = "calc-2026.09.0"

#: Yoto's hosted login, resource API, and Labs (text-to-speech) origins.
YOTO_LOGIN_BASE = "https://login.yotoplay.com"
YOTO_API_BASE = "https://api.yotoplay.com"
YOTO_LABS_BASE = "https://labs.api.yotoplay.com"

#: The API audience Yoto access tokens must be minted for.
YOTO_AUDIENCE = "https://api.yotoplay.com"

#: What Huddle needs and nothing more. ``user:content:manage`` covers creating
#: and updating the MYO card (and implies icon management); ``offline_access``
#: is what makes an unattended daily refresh possible at all. Device scopes are
#: deliberately not requested -- Huddle publishes audio, it never controls a
#: player, and requesting them would make the app ineligible for verification.
YOTO_SCOPES = "profile offline_access user:content:manage"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HUDDLE_", env_file=".env", extra="ignore", case_sensitive=False
    )

    # -- storage -----------------------------------------------------------
    database_url: str = Field(default="sqlite:///./data/huddle.db")
    season: int = Field(default=2026)

    # -- Yoto application --------------------------------------------------
    yoto_client_id: str | None = Field(default=None)
    #: Only set for a confidential client. A public (PKCE-only) client leaves
    #: this empty, and the token exchange then omits the secret entirely.
    yoto_client_secret: str | None = Field(default=None, repr=False)
    yoto_redirect_uri: str = Field(default="http://127.0.0.1:8787/auth/callback")
    yoto_login_base: str = Field(default=YOTO_LOGIN_BASE)
    yoto_api_base: str = Field(default=YOTO_API_BASE)
    yoto_labs_base: str = Field(default=YOTO_LABS_BASE)
    yoto_scopes: str = Field(default=YOTO_SCOPES)

    # -- audio -------------------------------------------------------------
    #: ``labs`` posts the script to Yoto and lets Yoto synthesise it.
    #: ``upload`` synthesises locally, then uploads and transcodes.
    audio_pipeline: str = Field(default="labs")
    #: One ElevenLabs voice per host. Yoto Labs speaks ElevenLabs voice ids,
    #: so the same values work on both pipelines. Two distinct voices are what
    #: make the show a conversation rather than a monologue; if they are set to
    #: the same id the show still works, it just sounds like one presenter.
    nova_voice_id: str = Field(default="JBFqnCBsd6RMkjVDRZzb")
    rae_voice_id: str = Field(default="EXAVITQu4vr4xnSDxMaL")
    tts_provider: str = Field(default="elevenlabs", description="only used by the upload pipeline")
    elevenlabs_api_key: str | None = Field(default=None, repr=False)
    elevenlabs_model: str = Field(default="eleven_multilingual_v2")

    # -- narration ---------------------------------------------------------
    #: When false (the default) scripts are rendered from templates alone. The
    #: model may only rephrase facts already in the brief -- it is never a source.
    narration_enabled: bool = Field(default=False)
    narration_model: str = Field(default="claude-opus-5")
    anthropic_api_key: str | None = Field(default=None, repr=False)

    # -- web ---------------------------------------------------------------
    web_base_url: str = Field(default="http://127.0.0.1:8787")
    #: Signs the login cookie. Generated per process if unset, which logs
    #: everyone out on restart -- fine locally, set it in production.
    session_secret: str | None = Field(default=None, repr=False)
    session_cookie_name: str = Field(default="huddle_session")
    session_max_age_seconds: int = Field(default=60 * 60 * 24 * 30)

    # -- scheduling --------------------------------------------------------
    #: Local hour at which the daily show is built, in each family's timezone.
    default_publish_hour: int = Field(default=6)
    default_timezone: str = Field(default="America/New_York")

    # -- misc --------------------------------------------------------------
    fixtures_dir: Path = Field(default=REPO_ROOT / "fixtures")
    http_timeout_seconds: float = Field(default=30.0)
    use_fixtures: bool = Field(default=False)

    @property
    def is_public_client(self) -> bool:
        """A client with no secret must use PKCE and may not be trusted with one."""
        return not self.yoto_client_secret

    @property
    def authorize_url(self) -> str:
        return f"{self.yoto_login_base.rstrip('/')}/authorize"

    @property
    def token_url(self) -> str:
        return f"{self.yoto_login_base.rstrip('/')}/oauth/token"

    @property
    def device_code_url(self) -> str:
        return f"{self.yoto_login_base.rstrip('/')}/oauth/device/code"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings() -> None:
    """Test hook: drop the cached settings so new environment values apply."""
    get_settings.cache_clear()
