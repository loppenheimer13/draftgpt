"""Yoto OAuth2: PKCE authorization code, device code, and token refresh.

Two flows, one token store:

* :class:`AuthorizationCodeFlow` backs the web sign-in. PKCE is always used --
  it is required for a public client and harmless for a confidential one, and
  it is what stops a stolen authorization code from being redeemable.
* :class:`DeviceCodeFlow` backs ``huddle auth login --device`` for a box with no
  browser.

Both end at :func:`store_tokens`, so the scheduler never needs to know how a
subscriber signed in.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets as pysecrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from huddle.config import YOTO_AUDIENCE, Settings, get_settings
from huddle.yoto.secrets import decrypt, encrypt

logger = logging.getLogger(__name__)

#: Refresh this far before actual expiry so a long publish never dies mid-run.
EXPIRY_SKEW_SECONDS = 120


class YotoAuthError(RuntimeError):
    """A token endpoint refused us. Never carries the token or the secret."""

    def __init__(self, message: str, *, error: str | None = None, status: int | None = None):
        super().__init__(message)
        self.error = error
        self.status = status

    @property
    def is_permanent(self) -> bool:
        """True when retrying cannot help and the user must sign in again."""
        return self.error in {"invalid_grant", "invalid_client", "unauthorized_client"}


class AuthorizationPending(YotoAuthError):
    """Device flow: the user has not finished approving yet."""


@dataclass(frozen=True)
class PkcePair:
    verifier: str
    challenge: str


def generate_pkce() -> PkcePair:
    """A high-entropy verifier and its S256 challenge.

    RFC 7636 allows 43-128 characters; 64 random bytes lands at 86 after
    base64url encoding, comfortably inside that range.
    """
    verifier = _b64url(pysecrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return PkcePair(verifier=verifier, challenge=challenge)


def generate_state() -> str:
    """CSRF token binding the callback to the browser that started the login."""
    return pysecrets.token_urlsafe(32)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    refresh_token: str | None
    token_type: str
    expires_at: datetime | None
    scope: str | None
    id_token: str | None = None

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(UTC) >= self.expires_at - timedelta(seconds=EXPIRY_SKEW_SECONDS)

    @property
    def claims(self) -> dict[str, Any]:
        """Claims from the id token. Decoded, deliberately not verified.

        These are only used to label an account in Huddle's own UI. The tokens
        came straight from Yoto's token endpoint over TLS, so they are not
        attacker-supplied -- but nothing security-relevant is decided from them
        either, which is why skipping signature verification is acceptable here
        rather than merely convenient.
        """
        return decode_jwt_claims(self.id_token) if self.id_token else {}


def decode_jwt_claims(token: str) -> dict[str, Any]:
    """Read a JWT payload without verifying it. See the caveat above."""
    import json

    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload.encode()))
    except (ValueError, TypeError):
        return {}


def _token_set(payload: dict[str, Any]) -> TokenSet:
    expires_in = payload.get("expires_in")
    expires_at = None
    if isinstance(expires_in, int | float):
        expires_at = datetime.now(UTC) + timedelta(seconds=float(expires_in))
    elif payload.get("expires_at"):
        expires_at = datetime.fromtimestamp(float(payload["expires_at"]), tz=UTC)
    return TokenSet(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token"),
        token_type=payload.get("token_type", "Bearer"),
        expires_at=expires_at,
        scope=payload.get("scope"),
        id_token=payload.get("id_token"),
    )


def _post_token(settings: Settings, form: dict[str, str], client: httpx.Client | None = None):
    owned = client is None
    http = client or httpx.Client(timeout=settings.http_timeout_seconds)
    try:
        response = http.post(
            settings.token_url,
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    except httpx.HTTPError as exc:
        raise YotoAuthError(f"token endpoint unreachable: {exc}") from exc
    finally:
        if owned:
            http.close()

    try:
        payload = response.json()
    except ValueError:
        payload = {}

    if response.status_code >= 400 or "access_token" not in payload:
        error = payload.get("error")
        if error in {"authorization_pending", "slow_down"}:
            raise AuthorizationPending(
                payload.get("error_description") or "authorization pending",
                error=error,
                status=response.status_code,
            )
        raise YotoAuthError(
            payload.get("error_description") or f"token exchange failed ({response.status_code})",
            error=error,
            status=response.status_code,
        )
    return _token_set(payload)


class AuthorizationCodeFlow:
    """Browser sign-in with PKCE. Used by the web app and the CLI's local flow."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.yoto_client_id:
            raise YotoAuthError(
                "HUDDLE_YOTO_CLIENT_ID is not set -- create an app at dashboard.yoto.dev"
            )

    def authorize_url(
        self, *, state: str, code_challenge: str, redirect_uri: str | None = None
    ) -> str:
        from urllib.parse import urlencode

        params = {
            "audience": YOTO_AUDIENCE,
            "scope": self.settings.yoto_scopes,
            "response_type": "code",
            "client_id": self.settings.yoto_client_id or "",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "redirect_uri": redirect_uri or self.settings.yoto_redirect_uri,
            "state": state,
        }
        return f"{self.settings.authorize_url}?{urlencode(params)}"

    def exchange(
        self,
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str | None = None,
        client: httpx.Client | None = None,
    ) -> TokenSet:
        form = {
            "grant_type": "authorization_code",
            "client_id": self.settings.yoto_client_id or "",
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri or self.settings.yoto_redirect_uri,
        }
        if self.settings.yoto_client_secret:
            form["client_secret"] = self.settings.yoto_client_secret
        return _post_token(self.settings, form, client)


class DeviceCodeFlow:
    """Device authorization grant, for a machine with no browser."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.yoto_client_id:
            raise YotoAuthError("HUDDLE_YOTO_CLIENT_ID is not set")

    def start(self, client: httpx.Client | None = None) -> dict[str, Any]:
        owned = client is None
        http = client or httpx.Client(timeout=self.settings.http_timeout_seconds)
        try:
            response = http.post(
                self.settings.device_code_url,
                data={
                    "client_id": self.settings.yoto_client_id or "",
                    "scope": self.settings.yoto_scopes,
                    "audience": YOTO_AUDIENCE,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            raise YotoAuthError(f"device code endpoint unreachable: {exc}") from exc
        finally:
            if owned:
                http.close()

        payload = response.json() if response.content else {}
        if response.status_code >= 400 or "device_code" not in payload:
            raise YotoAuthError(
                payload.get("error_description") or "device authorization failed",
                error=payload.get("error"),
                status=response.status_code,
            )
        return payload

    def poll(
        self,
        device_code: str,
        *,
        interval: int = 5,
        expires_in: int = 600,
        client: httpx.Client | None = None,
        sleep=time.sleep,
    ) -> TokenSet:
        """Block until the user approves, the code expires, or we are refused.

        ``slow_down`` widens the interval permanently, as the RFC requires --
        ignoring it is how a client gets rate limited off the endpoint.
        """
        deadline = time.monotonic() + expires_in
        form = {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": self.settings.yoto_client_id or "",
            "device_code": device_code,
        }
        if self.settings.yoto_client_secret:
            form["client_secret"] = self.settings.yoto_client_secret

        while time.monotonic() < deadline:
            sleep(interval)
            try:
                return _post_token(self.settings, form, client)
            except AuthorizationPending as pending:
                if pending.error == "slow_down":
                    interval += 5
                continue
        raise YotoAuthError("device code expired before it was approved", error="expired_token")


def refresh_tokens(
    refresh_token: str, settings: Settings | None = None, client: httpx.Client | None = None
) -> TokenSet:
    """Swap a refresh token for a new access token.

    Yoto may rotate the refresh token on use. When it returns a new one the
    caller must persist it; :func:`store_tokens` handles that, and keeps the
    previous value when the response omits it.
    """
    settings = settings or get_settings()
    form = {
        "grant_type": "refresh_token",
        "client_id": settings.yoto_client_id or "",
        "refresh_token": refresh_token,
    }
    if settings.yoto_client_secret:
        form["client_secret"] = settings.yoto_client_secret
    return _post_token(settings, form, client)


# -- persistence -----------------------------------------------------------
def store_tokens(credential, tokens: TokenSet) -> None:
    """Write a token set onto a :class:`YotoCredential`, encrypting values.

    A refresh response that omits ``refresh_token`` means "keep using the one
    you have", so the stored value is preserved rather than blanked.
    """
    credential.access_token = encrypt(tokens.access_token)
    if tokens.refresh_token:
        credential.refresh_token = encrypt(tokens.refresh_token)
    credential.token_type = tokens.token_type
    credential.expires_at = tokens.expires_at
    credential.scope = tokens.scope
    credential.last_refreshed_at = datetime.now(UTC)
    credential.revoked_at = None
    credential.last_error = None


def load_tokens(credential) -> TokenSet:
    return TokenSet(
        access_token=decrypt(credential.access_token) or "",
        refresh_token=decrypt(credential.refresh_token),
        token_type=credential.token_type or "Bearer",
        expires_at=credential.expires_at,
        scope=credential.scope,
    )


def valid_access_token(
    credential, settings: Settings | None = None, client: httpx.Client | None = None
) -> str:
    """Return a usable access token, refreshing first if it is close to expiry.

    Raises :class:`YotoAuthError` when the subscriber must sign in again; the
    credential is marked revoked so the scheduler stops retrying it nightly.
    """
    if credential.revoked_at is not None:
        raise YotoAuthError("credential was revoked; the subscriber must reconnect Yoto")

    tokens = load_tokens(credential)
    if tokens.access_token and not tokens.is_expired:
        return tokens.access_token

    if not tokens.refresh_token:
        raise YotoAuthError("no refresh token stored; the subscriber must reconnect Yoto")

    try:
        refreshed = refresh_tokens(tokens.refresh_token, settings, client)
    except YotoAuthError as exc:
        credential.last_error = str(exc)[:500]
        if exc.is_permanent:
            credential.revoked_at = datetime.now(UTC)
        raise
    store_tokens(credential, refreshed)
    return refreshed.access_token
