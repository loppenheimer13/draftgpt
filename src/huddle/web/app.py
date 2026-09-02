"""The parent-facing web app.

Deliberately small. A parent does four things here: connect Yoto, pick teams,
set how long and how grown-up the show should be, and check what was filtered
out. Everything else is the scheduler's job.

The safety page is not decoration. It lists, by name, every story the filter
removed and why -- which is the only way a claim about children's content can
be verified rather than merely believed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from huddle.catalog import LEAGUES, SportGroup, get_league, leagues_in_group
from huddle.config import Settings, get_settings
from huddle.database.models import Family, FavoriteTeam, ShowRun, Team
from huddle.database.session import get_session_factory
from huddle.domain.enums import SEGMENT_ORDER, SEGMENT_TITLES
from huddle.ingestion.sync import sync_rosters, sync_team_details
from huddle.web import auth as web_auth
from huddle.web import views
from huddle.yoto.auth import YotoAuthError

logger = logging.getLogger(__name__)

app = FastAPI(title="Huddle", docs_url=None, redoc_url=None)


@app.exception_handler(HTTPException)
def _unauthorized_to_login(request: Request, exc: HTTPException):
    """Send a signed-out browser to the front door instead of a bare 401.

    Anything that is not a page request still gets the status code, so a script
    or a probe sees the real answer.
    """
    accepts_html = "text/html" in (request.headers.get("accept") or "")
    if exc.status_code == 401 and accepts_html:
        return RedirectResponse(f"/login?next={request.url.path}", status_code=302)
    return HTMLResponse(
        views.error_page("Something went wrong", str(exc.detail)),
        status_code=exc.status_code,
    )


def get_db() -> Iterator[Session]:
    factory = get_session_factory()
    db = factory()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def settings_dep() -> Settings:
    return get_settings()


def current_family(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(settings_dep),
) -> Family | None:
    family_id = web_auth.read_cookie(
        settings, request.cookies.get(settings.session_cookie_name)
    )
    if not family_id:
        return None
    return db.get(Family, family_id)


def require_family(family: Family | None = Depends(current_family)) -> Family:
    if family is None:
        # A browser gets sent to the front door; an API client gets a status.
        raise HTTPException(status_code=401, detail="not signed in")
    return family


# -- pages -----------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home(
    db: Session = Depends(get_db),
    family: Family | None = Depends(current_family),
) -> HTMLResponse:
    if family is None:
        return HTMLResponse(views.landing())

    favourites = _favourites(db, family)
    latest = db.scalar(
        select(ShowRun)
        .where(ShowRun.family_id == family.id)
        .order_by(ShowRun.show_date.desc())
        .limit(1)
    )
    return HTMLResponse(views.dashboard(family, favourites, latest))


@app.get("/login")
def login(
    next: str = "/",
    db: Session = Depends(get_db),
    settings: Settings = Depends(settings_dep),
):
    try:
        url = web_auth.start_login(db, settings, next_path=next)
    except YotoAuthError as exc:
        return HTMLResponse(views.error_page("Cannot start sign-in", str(exc)), status_code=500)
    return RedirectResponse(url, status_code=302)


@app.get("/auth/callback")
def auth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    db: Session = Depends(get_db),
    settings: Settings = Depends(settings_dep),
):
    if error:
        return HTMLResponse(
            views.error_page("Yoto declined the sign-in", error_description or error),
            status_code=400,
        )
    if not code or not state:
        return HTMLResponse(
            views.error_page("Incomplete sign-in", "The link was missing information."),
            status_code=400,
        )

    try:
        family, next_path = web_auth.complete_login(db, settings, code=code, state=state)
    except YotoAuthError as exc:
        return HTMLResponse(views.error_page("Sign-in failed", str(exc)), status_code=400)

    response = RedirectResponse(next_path or "/", status_code=302)
    response.set_cookie(
        settings.session_cookie_name,
        web_auth.issue_cookie(settings, family),
        max_age=settings.session_max_age_seconds,
        httponly=True,
        samesite="lax",
        # The cookie carries a login; over plain HTTP that is only acceptable
        # on a loopback address, which is where local development runs.
        secure=settings.web_base_url.startswith("https://"),
    )
    return response


@app.get("/logout")
def logout(settings: Settings = Depends(settings_dep)):
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie(settings.session_cookie_name)
    return response


@app.get("/teams", response_class=HTMLResponse)
def teams_page(
    league: str = Query(default="nfl"),
    q: str = Query(default=""),
    db: Session = Depends(get_db),
    family: Family = Depends(require_family),
) -> HTMLResponse:
    spec = get_league(league) if league in {s.key for s in LEAGUES} else LEAGUES[0]
    stmt = select(Team).where(Team.league_key == spec.key, Team.active.is_(True))
    if q:
        stmt = stmt.where(Team.search_name.contains(q.lower()))
    # Large college and cup competitions have hundreds of entrants, so the
    # picker searches rather than rendering the lot.
    limit = 60 if spec.large_roster else 400
    teams = list(db.scalars(stmt.order_by(Team.display_name).limit(limit)))

    chosen = {row.team_id for row in db.scalars(
        select(FavoriteTeam).where(FavoriteTeam.family_id == family.id)
    )}
    groups = {group: leagues_in_group(group) for group in SportGroup}
    return HTMLResponse(views.teams_page(family, spec, teams, chosen, groups, q))


@app.post("/teams/follow")
def follow_team(
    team_id: str = Form(...),
    league: str = Form(default="nfl"),
    db: Session = Depends(get_db),
    family: Family = Depends(require_family),
):
    team = db.get(Team, team_id)
    if team is None:
        raise HTTPException(status_code=404, detail="unknown team")

    existing = db.scalar(
        select(FavoriteTeam).where(
            FavoriteTeam.family_id == family.id, FavoriteTeam.team_id == team_id
        )
    )
    if existing is None:
        next_order = db.scalar(
            select(func.coalesce(func.max(FavoriteTeam.sort_order), -1)).where(
                FavoriteTeam.family_id == family.id
            )
        )
        db.add(
            FavoriteTeam(
                family_id=family.id, team_id=team_id, sort_order=(next_order or 0) + 1
            )
        )
        db.flush()
        # Pull this team's record and roster straight away so the first show
        # after signing up is a real one rather than an empty template.
        try:
            sync_team_details(db, [team_id])
            sync_rosters(db, [team_id])
        except Exception:  # noqa: BLE001 - the follow itself must still succeed
            logger.warning("could not warm data for team %s", team_id, exc_info=True)
    else:
        db.delete(existing)

    return RedirectResponse(f"/teams?league={league}", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(family: Family = Depends(require_family)) -> HTMLResponse:
    return HTMLResponse(views.settings_page(family, SEGMENT_ORDER, SEGMENT_TITLES))


@app.post("/settings")
def save_settings(
    request: Request,
    timezone: str = Form(default="America/New_York"),
    publish_hour: int = Form(default=6),
    target_minutes: int = Form(default=5),
    listener_age: int = Form(default=8),
    db: Session = Depends(get_db),
    family: Family = Depends(require_family),
):
    family.timezone = timezone.strip() or "America/New_York"
    # Clamp rather than reject: a slider that sends 99 should produce a sane
    # show, not a 500.
    family.publish_hour = max(0, min(23, publish_hour))
    family.target_minutes = max(3, min(10, target_minutes))
    family.listener_age = max(3, min(16, listener_age))
    db.flush()
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/settings/segments")
async def save_segments(
    request: Request,
    db: Session = Depends(get_db),
    family: Family = Depends(require_family),
):
    form = await request.form()
    chosen = [name for name in SEGMENT_ORDER if form.get(f"segment_{name}")]
    # An empty selection means "everything", not "nothing" -- a parent who
    # unticks every box has not asked for a silent card.
    family.segments = chosen if chosen else []
    db.flush()
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.get("/safety", response_class=HTMLResponse)
def safety_page(
    db: Session = Depends(get_db),
    family: Family = Depends(require_family),
) -> HTMLResponse:
    latest = db.scalar(
        select(ShowRun)
        .where(ShowRun.family_id == family.id)
        .order_by(ShowRun.show_date.desc())
        .limit(1)
    )
    return HTMLResponse(views.safety_page(family, latest))


@app.get("/show", response_class=HTMLResponse)
def show_page(
    db: Session = Depends(get_db),
    family: Family = Depends(require_family),
) -> HTMLResponse:
    """The transcript. A parent should be able to read exactly what was said."""
    latest = db.scalar(
        select(ShowRun)
        .where(ShowRun.family_id == family.id)
        .order_by(ShowRun.show_date.desc())
        .limit(1)
    )
    return HTMLResponse(views.transcript_page(family, latest))


@app.post("/show/build")
def build_now(
    publish: str = Form(default=""),
    db: Session = Depends(get_db),
    family: Family = Depends(require_family),
    settings: Settings = Depends(settings_dep),
):
    """Write today's show now, optionally sending it to the card."""
    from huddle.show.runner import build_show, publish_for_family

    result = build_show(db, family, force=True, settings=settings)
    if publish and result.segments:
        publish_for_family(db, family, result.run, settings=settings)
    return RedirectResponse("/show", status_code=303)


@app.get("/healthz")
def healthz(db: Session = Depends(get_db)) -> dict[str, object]:
    families = db.scalar(select(func.count()).select_from(Family)) or 0
    return {
        "status": "ok",
        "families": families,
        "checked_at": datetime.now(UTC).isoformat(),
    }


def _favourites(db: Session, family: Family) -> list[tuple[FavoriteTeam, Team]]:
    rows = list(
        db.scalars(
            select(FavoriteTeam)
            .where(FavoriteTeam.family_id == family.id)
            .order_by(FavoriteTeam.sort_order)
        )
    )
    if not rows:
        return []
    teams = {
        team.id: team
        for team in db.scalars(select(Team).where(Team.id.in_([r.team_id for r in rows])))
    }
    return [(row, teams[row.team_id]) for row in rows if row.team_id in teams]
