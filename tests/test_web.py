"""The parent-facing web app, including the sign-in security properties."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from huddle.config import get_settings
from huddle.database.models import Family, FavoriteTeam, OAuthTransaction
from huddle.web import app as app_module
from huddle.web import auth as web_auth


@pytest.fixture
def client(db, monkeypatch) -> TestClient:
    monkeypatch.setitem(app_module.app.dependency_overrides, app_module.get_db, lambda: db)
    return TestClient(app_module.app)


@pytest.fixture
def signed_in(client: TestClient, family: Family) -> TestClient:
    client.cookies.set("huddle_session", web_auth.issue_cookie(get_settings(), family))
    return client


def test_landing_explains_the_promise(client: TestClient) -> None:
    body = client.get("/").text
    assert "Connect your Yoto account" in body
    assert "No betting" in body


def test_signed_out_browser_is_sent_to_sign_in(client: TestClient) -> None:
    response = client.get("/teams", headers={"accept": "text/html"}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login?next=/teams"


def test_signed_out_api_call_gets_a_status_not_a_redirect(client: TestClient) -> None:
    response = client.get("/teams", headers={"accept": "application/json"},
                          follow_redirects=False)
    assert response.status_code == 401


def test_dashboard_lists_followed_teams(signed_in, db, family, followed) -> None:
    assert "Atlanta Braves" in signed_in.get("/").text


def test_team_picker_marks_current_choices(signed_in, db, family, braves) -> None:
    db.add(FavoriteTeam(family_id=family.id, team_id=braves.id))
    db.flush()
    assert 'class="team on"' in signed_in.get("/teams?league=mlb").text


def test_team_search_filters(signed_in, db, braves, liverpool) -> None:
    assert "Liverpool" in signed_in.get("/teams?league=epl&q=liver").text
    assert "Liverpool" not in signed_in.get("/teams?league=epl&q=arsenal").text


def test_settings_clamp_rather_than_reject(signed_in, db, family) -> None:
    """A slider that sends 99 should produce a sane show, not a 500."""
    signed_in.post("/settings", data={
        "timezone": "Europe/London", "publish_hour": "99",
        "target_minutes": "40", "listener_age": "1",
    })
    db.refresh(family)
    assert family.publish_hour == 23
    assert family.target_minutes == 10
    assert family.listener_age == 3
    assert family.timezone == "Europe/London"


def test_unticking_every_segment_means_the_full_show(signed_in, db, family) -> None:
    """A parent who unticks everything has not asked for a silent card."""
    signed_in.post("/settings/segments", data={})
    db.refresh(family)
    assert family.segments == []
    assert len(family.enabled_segments) == 6


def test_safety_page_names_what_was_filtered(signed_in, db, family, followed, now,
                                             settings) -> None:
    from conftest import add_story
    from huddle.show.runner import build_show

    story = add_story(db, headline="Player arrested overnight", verdict="block")
    story.safety_category = "legal"
    story.safety_matched = "arrested"
    db.flush()
    build_show(db, family, now=now, settings=settings)

    body = signed_in.get("/safety").text
    assert "Player arrested overnight" in body
    assert "legal" in body


def test_healthz_reports_without_auth(client: TestClient) -> None:
    payload = client.get("/healthz").json()
    assert payload["status"] == "ok"


# -- sign-in security ------------------------------------------------------
def test_login_state_is_single_use(db, settings) -> None:
    """A replayed callback must fail, or a captured URL becomes a login."""
    web_auth.start_login(db, settings)
    transaction = db.scalar(select(OAuthTransaction))

    import contextlib

    from huddle.yoto.auth import YotoAuthError

    # The first attempt fails at the token endpoint (there isn't one here);
    # what matters is that it consumed the transaction on the way through.
    with contextlib.suppress(Exception):
        web_auth.complete_login(db, settings, code="c", state=transaction.state)

    with pytest.raises(YotoAuthError, match="already-used"):
        web_auth.complete_login(db, settings, code="c", state=transaction.state)


def test_expired_login_is_refused(db, settings) -> None:
    web_auth.start_login(db, settings)
    transaction = db.scalar(select(OAuthTransaction))
    transaction.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db.flush()

    from huddle.yoto.auth import YotoAuthError

    with pytest.raises(YotoAuthError, match="expired"):
        web_auth.complete_login(db, settings, code="c", state=transaction.state)


def test_unknown_state_is_refused(db, settings) -> None:
    from huddle.yoto.auth import YotoAuthError

    with pytest.raises(YotoAuthError):
        web_auth.complete_login(db, settings, code="c", state="forged")


def test_session_cookie_is_signed(settings, db, family) -> None:
    cookie = web_auth.issue_cookie(settings, family)
    assert web_auth.read_cookie(settings, cookie) == family.id
    assert web_auth.read_cookie(settings, cookie + "x") is None
    assert web_auth.read_cookie(settings, "not-a-cookie") is None
