"""Families, their Yoto account link, and the teams they follow.

A family is identified by their Yoto user id -- Huddle has no password of its
own and never sees one. A parent signs in with Yoto, picks teams across any
leagues in the catalogue, and every setting that shapes the daily show hangs
off that link.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from huddle.database.models.base import Base, TimestampMixin, pk


class Family(Base, TimestampMixin):
    """One Yoto account that has connected to Huddle."""

    __tablename__ = "families"
    __table_args__ = (UniqueConstraint("yoto_user_id", name="uq_families_yoto_user_id"),)

    id: Mapped[str] = pk()
    #: The ``sub`` claim from Yoto's id token. Our only identity anchor.
    yoto_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="America/New_York")
    #: Local hour at which today's show should already be on the card.
    publish_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=6)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    #: Segments to include, in broadcast order. Empty means the full show.
    segments: Mapped[list] = mapped_column(nullable=False, default=list)
    #: Running-time ceiling in minutes. The writer trims to fit rather than
    #: reading everything it found, so a nine-minute "short show" cannot
    #: happen. It never pads in the other direction: on a quiet day the show
    #: is simply shorter, because there is no honest way to invent sport.
    target_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=5)

    #: Roughly how old the listener is, which decides how much a host explains.
    #: Not a birthday and not a profile -- one number, chosen by the parent.
    listener_age: Mapped[int] = mapped_column(Integer, nullable=False, default=8)

    #: Per-family voice overrides for the two hosts; fall back to settings.
    nova_voice_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rae_voice_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    audio_pipeline: Mapped[str | None] = mapped_column(String(16), nullable=True)

    #: The MYO card Huddle republishes each day. Set on the first successful
    #: publish and reused forever, so the physical card keeps working.
    card_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    last_published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    credential: Mapped[YotoCredential | None] = relationship(
        back_populates="family", uselist=False, cascade="all, delete-orphan"
    )
    favorite_teams: Mapped[list[FavoriteTeam]] = relationship(
        back_populates="family", cascade="all, delete-orphan"
    )

    @property
    def enabled_segments(self) -> list[str]:
        from huddle.domain.enums import SEGMENT_ORDER

        if not self.segments:
            return list(SEGMENT_ORDER)
        # Stored order is advisory; broadcast order is canonical, so a show
        # never opens on Birthday Club because of how a checkbox list saved.
        chosen = set(self.segments)
        return [segment for segment in SEGMENT_ORDER if segment in chosen]


class YotoCredential(Base, TimestampMixin):
    """OAuth tokens for one family.

    Token values are written through :mod:`huddle.yoto.secrets`, which encrypts
    them when a key is configured. The column is text either way, so enabling
    encryption later does not need a migration.
    """

    __tablename__ = "yoto_credentials"
    __table_args__ = (UniqueConstraint("family_id", name="uq_yoto_credentials_family_id"),)

    id: Mapped[str] = pk()
    family_id: Mapped[str] = mapped_column(
        ForeignKey("families.id", ondelete="CASCADE"), nullable=False
    )
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_type: Mapped[str] = mapped_column(String(32), nullable=False, default="Bearer")
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Set when a refresh is rejected, so the scheduler stops retrying and the
    #: dashboard can ask the parent to reconnect.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    family: Mapped[Family] = relationship(back_populates="credential")


class FavoriteTeam(Base, TimestampMixin):
    """One team a family follows. Any league in the catalogue."""

    __tablename__ = "favorite_teams"
    __table_args__ = (
        UniqueConstraint("family_id", "team_id", name="uq_favorite_teams_family_id"),
    )

    id: Mapped[str] = pk()
    family_id: Mapped[str] = mapped_column(
        ForeignKey("families.id", ondelete="CASCADE"), nullable=False, index=True
    )
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), nullable=False, index=True)
    #: Lower sorts earlier, so the first team picked leads the show.
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: What the kid calls them, if that differs from the official name.
    nickname: Mapped[str | None] = mapped_column(String(96), nullable=True)

    family: Mapped[Family] = relationship(back_populates="favorite_teams")


class OAuthTransaction(Base, TimestampMixin):
    """One in-flight browser login: the PKCE verifier and CSRF state.

    Rows are single-use and short-lived; the callback deletes them on
    redemption, and anything past ``expires_at`` is swept on the next login.
    """

    __tablename__ = "oauth_transactions"
    __table_args__ = (UniqueConstraint("state", name="uq_oauth_transactions_state"),)

    id: Mapped[str] = pk()
    state: Mapped[str] = mapped_column(String(64), nullable=False)
    code_verifier: Mapped[str] = mapped_column(String(128), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(String(500), nullable=False)
    #: Where to send the browser once the login completes.
    next_path: Mapped[str] = mapped_column(String(500), nullable=False, default="/")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
