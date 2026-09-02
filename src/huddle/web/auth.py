"""Browser sign-in with Yoto, and the session cookie that follows.

Huddle stores no password. A parent proves who they are to Yoto, and Yoto tells
us a user id -- which is also, conveniently, the only identity we need, since
the show is published to that same account's card.

The login is an OAuth2 authorization code flow with PKCE. Two details matter:

* the ``state`` parameter is generated per attempt, stored server-side, and
  consumed exactly once, which is what stops a forged callback;
* the PKCE verifier never leaves the server, so an intercepted code is useless.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from huddle.config import Settings
from huddle.database.models import Family, OAuthTransaction, YotoCredential
from huddle.yoto.auth import (
    AuthorizationCodeFlow,
    TokenSet,
    YotoAuthError,
    generate_pkce,
    generate_state,
    store_tokens,
)

logger = logging.getLogger(__name__)

#: A login left half-finished expires rather than lingering as a valid state.
TRANSACTION_TTL_MINUTES = 15
COOKIE_SALT = "huddle-session-v1"


def start_login(
    session: Session, settings: Settings, *, next_path: str = "/"
) -> str:
    """Create a login transaction and return the URL to send the browser to."""
    flow = AuthorizationCodeFlow(settings)
    pkce = generate_pkce()
    state = generate_state()

    _sweep_expired(session)
    session.add(
        OAuthTransaction(
            state=state,
            code_verifier=pkce.verifier,
            redirect_uri=settings.yoto_redirect_uri,
            next_path=next_path or "/",
            expires_at=datetime.now(UTC) + timedelta(minutes=TRANSACTION_TTL_MINUTES),
        )
    )
    session.flush()
    return flow.authorize_url(state=state, code_challenge=pkce.challenge)


def complete_login(
    session: Session, settings: Settings, *, code: str, state: str
) -> tuple[Family, str]:
    """Redeem the callback. Returns the family and where to send them next.

    The transaction row is deleted whether or not the exchange succeeds, so a
    ``state`` can never be replayed.
    """
    transaction = session.scalar(
        select(OAuthTransaction).where(OAuthTransaction.state == state)
    )
    if transaction is None:
        raise YotoAuthError("unknown or already-used login state")

    verifier = transaction.code_verifier
    redirect_uri = transaction.redirect_uri
    next_path = transaction.next_path
    expired = _aware(transaction.expires_at) < datetime.now(UTC)
    session.delete(transaction)
    session.flush()

    if expired:
        raise YotoAuthError("this login link expired; please try again")

    flow = AuthorizationCodeFlow(settings)
    tokens = flow.exchange(code=code, code_verifier=verifier, redirect_uri=redirect_uri)
    family = upsert_family(session, tokens)
    return family, next_path


def upsert_family(session: Session, tokens: TokenSet) -> Family:
    """Link a Yoto identity to a family row, creating one on first sign-in.

    Shared by the browser callback and the CLI device flow, which is why it is
    public: both paths must produce exactly the same account.
    """
    claims = tokens.claims
    yoto_user_id = claims.get("sub")
    if not yoto_user_id:
        raise YotoAuthError("Yoto did not return a user id; cannot identify this account")

    family = session.scalar(select(Family).where(Family.yoto_user_id == yoto_user_id))
    if family is None:
        family = Family(yoto_user_id=yoto_user_id)
        session.add(family)
        session.flush()

    # Refresh the label, but never overwrite a name a parent chose themselves.
    family.email = claims.get("email") or family.email
    family.display_name = family.display_name or claims.get("name") or claims.get("nickname")

    credential = family.credential
    if credential is None:
        credential = YotoCredential(family_id=family.id)
        session.add(credential)
        family.credential = credential
    store_tokens(credential, tokens)
    session.flush()
    return family


def _sweep_expired(session: Session) -> None:
    session.execute(
        delete(OAuthTransaction).where(OAuthTransaction.expires_at < datetime.now(UTC))
    )


# -- session cookie --------------------------------------------------------
def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    secret = settings.session_secret
    if not secret:
        # A per-process secret logs everyone out on restart, which is a
        # nuisance locally and unacceptable in production -- so say so.
        import secrets as pysecrets

        secret = pysecrets.token_urlsafe(32)
        logger.warning(
            "HUDDLE_SESSION_SECRET is not set; using a per-process secret. "
            "Everyone will be signed out whenever this process restarts."
        )
        settings.session_secret = secret
    return URLSafeTimedSerializer(secret, salt=COOKIE_SALT)


def issue_cookie(settings: Settings, family: Family) -> str:
    return _serializer(settings).dumps({"family_id": family.id})


def read_cookie(settings: Settings, value: str | None) -> str | None:
    """Return the family id in a signed cookie, or None if it is not valid."""
    if not value:
        return None
    try:
        payload = _serializer(settings).loads(
            value, max_age=settings.session_max_age_seconds
        )
    except BadSignature:
        return None
    except Exception:  # noqa: BLE001 - an expired or malformed cookie is a logout
        return None
    return payload.get("family_id") if isinstance(payload, dict) else None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
