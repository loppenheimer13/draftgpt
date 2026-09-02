"""Teams, athletes, games, and news across every league in the catalogue.

One shape serves all fourteen leagues. A soccer club and a college basketball
programme are both a :class:`Team`; a match and a doubleheader game are both a
:class:`Game`. Sport-specific detail that only matters for narration lives in
``extra`` rather than in a column, so adding a league never needs a migration.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from huddle.database.models.base import Base, ProvenanceMixin, TimestampMixin, pk


class Team(Base, TimestampMixin, ProvenanceMixin):
    """One club, franchise, or programme."""

    __tablename__ = "teams"
    __table_args__ = (
        UniqueConstraint("league_key", "external_id", name="uq_teams_league_key"),
        Index("ix_teams_league_search", "league_key", "search_name"),
    )

    id: Mapped[str] = pk()
    league_key: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)

    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    #: "Diamondbacks" -- what a host actually says after the first mention.
    short_name: Mapped[str] = mapped_column(String(96), nullable=False)
    location: Mapped[str | None] = mapped_column(String(96), nullable=True)
    abbreviation: Mapped[str | None] = mapped_column(String(12), nullable=True)
    #: Lowercased name plus location, for the parent's team search box.
    search_name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str | None] = mapped_column(String(160), nullable=True)
    color: Mapped[str | None] = mapped_column(String(16), nullable=True)
    logo_url: Mapped[str | None] = mapped_column(String(600), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    #: Current record and standing, refreshed daily. Free text because every
    #: sport words it differently ("82-57", "12-3-2", "2nd in NL East").
    record_summary: Mapped[str | None] = mapped_column(String(64), nullable=True)
    standing_summary: Mapped[str | None] = mapped_column(String(200), nullable=True)


class Athlete(Base, TimestampMixin, ProvenanceMixin):
    """A player, kept mainly so Birthday Club has someone to celebrate."""

    __tablename__ = "athletes"
    __table_args__ = (
        UniqueConstraint("league_key", "external_id", name="uq_athletes_league_key"),
        #: Birthday Club queries by calendar day across every league at once.
        Index("ix_athletes_birthday", "birth_month", "birth_day"),
    )

    id: Mapped[str] = pk()
    league_key: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id"), nullable=True, index=True)

    full_name: Mapped[str] = mapped_column(String(160), nullable=False)
    position: Mapped[str | None] = mapped_column(String(64), nullable=True)
    jersey: Mapped[str | None] = mapped_column(String(8), nullable=True)
    birth_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: Denormalized so the daily birthday lookup is one indexed query, and so a
    #: player with a known day but unknown year is still celebrated.
    birth_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    birth_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    birth_place: Mapped[str | None] = mapped_column(String(160), nullable=True)
    headshot_url: Mapped[str | None] = mapped_column(String(600), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Game(Base, TimestampMixin, ProvenanceMixin):
    """One scheduled, live, or completed contest."""

    __tablename__ = "games"
    __table_args__ = (
        UniqueConstraint("league_key", "external_id", name="uq_games_league_key"),
        Index("ix_games_league_start", "league_key", "start_at"),
        Index("ix_games_home", "home_team_id", "start_at"),
        Index("ix_games_away", "away_team_id", "start_at"),
    )

    id: Mapped[str] = pk()
    league_key: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)

    name: Mapped[str | None] = mapped_column(String(240), nullable=True)
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    state: Mapped[str] = mapped_column(String(8), nullable=False, default="pre", index=True)
    #: "Final", "Bot 7th", "Sat, October 3rd at 7:00 PM EDT" -- provider prose,
    #: rewritten before it is ever spoken.
    status_detail: Mapped[str | None] = mapped_column(String(200), nullable=True)
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    home_team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id"), nullable=True)
    away_team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id"), nullable=True)
    home_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_score: Mapped[int | None] = mapped_column(Integer, nullable=True)

    venue: Mapped[str | None] = mapped_column(String(200), nullable=True)
    broadcast: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: Event notes ("NBA Canada Games"), neutral-site flags, week numbers.
    extra: Mapped[dict] = mapped_column(nullable=False, default=dict)


class NewsStory(Base, TimestampMixin, ProvenanceMixin):
    """A candidate story, with the safety verdict that decided its fate.

    Blocked stories are kept rather than discarded. The parent dashboard shows
    what was filtered out and why, which is the only way a claim about safety
    can be checked instead of merely trusted.
    """

    __tablename__ = "news_stories"
    __table_args__ = (
        UniqueConstraint("league_key", "external_id", name="uq_news_stories_league_key"),
        Index("ix_news_stories_published", "published_at"),
        Index("ix_news_stories_verdict", "safety_verdict"),
    )

    id: Mapped[str] = pk()
    league_key: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)

    headline: Mapped[str] = mapped_column(String(400), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    story_type: Mapped[str | None] = mapped_column(String(48), nullable=True)
    url: Mapped[str | None] = mapped_column(String(800), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Provider labels, usually league and team names. Used both for safety
    #: scanning and to match a story to a family's followed teams.
    categories: Mapped[list] = mapped_column(nullable=False, default=list)
    team_external_ids: Mapped[list] = mapped_column(nullable=False, default=list)

    safety_verdict: Mapped[str] = mapped_column(String(8), nullable=False, default="allow")
    safety_category: Mapped[str | None] = mapped_column(String(48), nullable=True)
    safety_matched: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: Airable, but not as the lead -- needs adult framing to make sense.
    caution: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
