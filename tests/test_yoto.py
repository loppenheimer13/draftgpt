"""Yoto integration: OAuth, token storage, card payloads, publishing."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from huddle.config import Settings
from huddle.domain.enums import Host
from huddle.yoto import auth, secrets
from huddle.yoto.client import YotoApiError, YotoClient
from huddle.yoto.content import (
    LABS_TRACK_CHAR_LIMIT,
    ChapterDraft,
    TrackDraft,
    build_content,
)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        yoto_client_id="test-client",
        yoto_redirect_uri="http://127.0.0.1:8787/auth/callback",
    )


# -- PKCE and tokens -------------------------------------------------------
def test_pkce_verifier_is_rfc_compliant() -> None:
    pair = auth.generate_pkce()
    assert 43 <= len(pair.verifier) <= 128
    assert "=" not in pair.challenge and "+" not in pair.challenge


def test_authorize_url_carries_pkce_and_state(settings: Settings) -> None:
    flow = auth.AuthorizationCodeFlow(settings)
    url = flow.authorize_url(state="abc123", code_challenge="chal")
    assert "code_challenge=chal" in url
    assert "code_challenge_method=S256" in url
    assert "state=abc123" in url
    assert "audience=https%3A%2F%2Fapi.yotoplay.com" in url


def test_public_client_never_sends_a_secret(settings: Settings) -> None:
    """A public client has no secret to send; leaking a placeholder would be
    worse than sending nothing."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"access_token": "at", "token_type": "Bearer",
                                         "expires_in": 3600})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    auth.AuthorizationCodeFlow(settings).exchange(
        code="c", code_verifier="v", client=client
    )
    assert "client_secret" not in captured["body"]
    assert "code_verifier=v" in captured["body"]


def test_refresh_keeps_the_old_token_when_none_is_returned() -> None:
    """Yoto may omit refresh_token on refresh, meaning "keep using yours".
    Blanking it would silently log the family out forever."""

    class Cred:
        access_token = refresh_token = None
        token_type = "Bearer"
        scope = expires_at = revoked_at = last_refreshed_at = last_error = None

    cred = Cred()
    auth.store_tokens(cred, auth.TokenSet("a1", "r1", "Bearer", None, None))
    auth.store_tokens(cred, auth.TokenSet("a2", None, "Bearer", None, None))
    assert secrets.decrypt(cred.refresh_token) == "r1"
    assert secrets.decrypt(cred.access_token) == "a2"


def test_expired_access_token_triggers_a_refresh(settings: Settings) -> None:
    class Cred:
        access_token = secrets.encrypt("old")
        refresh_token = secrets.encrypt("r1")
        token_type = "Bearer"
        scope = None
        expires_at = datetime.now(UTC) - timedelta(minutes=5)
        revoked_at = last_refreshed_at = last_error = None

    def handler(request: httpx.Request) -> httpx.Response:
        assert "grant_type=refresh_token" in request.content.decode()
        return httpx.Response(200, json={"access_token": "fresh", "token_type": "Bearer",
                                         "expires_in": 3600})

    cred = Cred()
    token = auth.valid_access_token(
        cred, settings, httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert token == "fresh"


def test_permanently_rejected_refresh_marks_the_credential_revoked(settings) -> None:
    """So the scheduler stops retrying nightly and the dashboard can ask the
    parent to reconnect."""

    class Cred:
        access_token = None
        refresh_token = secrets.encrypt("r1")
        token_type = "Bearer"
        scope = expires_at = revoked_at = last_refreshed_at = last_error = None

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    cred = Cred()
    with pytest.raises(auth.YotoAuthError):
        auth.valid_access_token(
            cred, settings, httpx.Client(transport=httpx.MockTransport(handler))
        )
    assert cred.revoked_at is not None


def test_id_token_claims_are_decoded() -> None:
    payload = base64.urlsafe_b64encode(
        json.dumps({"sub": "auth0|9", "email": "a@b.c"}).encode()
    ).decode().rstrip("=")
    tokens = auth.TokenSet("a", None, "Bearer", None, None, id_token=f"h.{payload}.s")
    assert tokens.claims["sub"] == "auth0|9"


# -- token encryption ------------------------------------------------------
def test_tokens_round_trip_through_encryption(monkeypatch) -> None:
    monkeypatch.setenv(secrets.ENV_KEY, secrets.generate_key())
    secrets.reset_cipher_cache()
    blob = secrets.encrypt("refresh-me")
    assert blob.startswith(secrets.PREFIX)
    assert "refresh-me" not in blob
    assert secrets.decrypt(blob) == "refresh-me"


def test_plaintext_rows_written_before_encryption_still_read(monkeypatch) -> None:
    """Turning encryption on must not need a migration."""
    monkeypatch.setenv(secrets.ENV_KEY, secrets.generate_key())
    secrets.reset_cipher_cache()
    assert secrets.decrypt("legacy-plaintext") == "legacy-plaintext"


def test_wrong_key_fails_loudly(monkeypatch) -> None:
    monkeypatch.setenv(secrets.ENV_KEY, secrets.generate_key())
    secrets.reset_cipher_cache()
    blob = secrets.encrypt("value")
    monkeypatch.setenv(secrets.ENV_KEY, secrets.generate_key())
    secrets.reset_cipher_cache()
    with pytest.raises(secrets.TokenDecryptionError):
        secrets.decrypt(blob)


# -- card payloads ---------------------------------------------------------
def chapter(host: str = Host.NOVA, voice: str | None = "v-nova") -> ChapterDraft:
    return ChapterDraft(
        key="01", title="Your Teams", segment="your_teams",
        tracks=[TrackDraft(key="01", title="Your Teams", script="The Braves won.",
                           segment="your_teams", host=str(host), voice_id=voice)],
    )


def test_labs_payload_carries_the_script_and_a_per_track_voice() -> None:
    """Per-track voice is what makes two hosts possible at all."""
    content = build_content([chapter()], title="Huddle", labs=True)
    track = content["content"]["chapters"][0]["tracks"][0]
    assert track["type"] == "elevenlabs"
    assert track["trackUrl"] == "The Braves won."
    assert track["voiceId"] == "v-nova"


def test_labs_payload_respects_the_character_limit() -> None:
    long = chapter()
    long.tracks[0].script = "x" * (LABS_TRACK_CHAR_LIMIT + 500)
    content = build_content([long], title="Huddle", labs=True)
    assert len(content["content"]["chapters"][0]["tracks"][0]["trackUrl"]) == LABS_TRACK_CHAR_LIMIT


def test_card_id_turns_a_create_into_an_update() -> None:
    """Reusing the card id is the whole point: the physical card keeps working
    and only what it plays changes."""
    assert "cardId" not in build_content([chapter()], title="Huddle", labs=True)
    content = build_content([chapter()], title="Huddle", labs=True, card_id="abc12")
    assert content["cardId"] == "abc12"


def test_upload_payload_requires_transcoded_audio() -> None:
    with pytest.raises(ValueError, match="no transcoded audio"):
        build_content([chapter()], title="Huddle", labs=False)


def test_upload_payload_points_at_the_content_hash() -> None:
    ch = chapter()
    ch.tracks[0].audio_sha256 = "deadbeef"
    ch.tracks[0].duration = 42.0
    ch.tracks[0].file_size = 1024
    content = build_content([ch], title="Huddle", labs=False)
    track = content["content"]["chapters"][0]["tracks"][0]
    assert track["trackUrl"] == "yoto:#deadbeef"
    assert track["type"] == "audio"


# -- API client ------------------------------------------------------------
def test_upload_url_never_receives_the_bearer_token() -> None:
    """The signed upload URL is itself the credential; sending our token to a
    storage host would leak it outside the API."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200)

    client = YotoClient(
        lambda: "secret-token",
        Settings(),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.upload_audio("https://uploads.example/abc", b"audio")
    assert seen["auth"] is None


def test_transcode_polling_stops_when_the_hash_appears() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(200, json={"transcode": {}})
        return httpx.Response(200, json={"transcode": {
            "transcodedSha256": "abc123",
            "transcodedInfo": {"duration": 30.5, "fileSize": 500, "format": "aac"},
        }})

    client = YotoClient(
        lambda: "t", Settings(), httpx.Client(transport=httpx.MockTransport(handler))
    )
    result = client.wait_for_transcode("up-1", sleep=lambda _s: None)
    assert result.track_url == "yoto:#abc123"
    assert result.duration == 30.5
    assert calls["n"] == 3


def test_transcode_timeout_is_an_error_not_a_silent_pass() -> None:
    client = YotoClient(
        lambda: "t",
        Settings(),
        httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"transcode": {}})
        )),
    )
    with pytest.raises(YotoApiError):
        client.wait_for_transcode("up-1", max_attempts=2, sleep=lambda _s: None)


def test_client_has_no_write_verbs_beyond_content_and_media() -> None:
    """Huddle publishes audio; it must never be able to control a player."""
    import inspect

    from huddle.yoto import client as client_module

    source = inspect.getsource(client_module)
    assert "sendDeviceCommand" not in source
    assert "devices" not in source.replace("never send a device command", "")
