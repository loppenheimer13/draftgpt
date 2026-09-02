"""All ORM models. Importing this module registers every table on ``Base``."""

from huddle.database.models.base import Base, ProvenanceMixin, TimestampMixin, new_id, utcnow
from huddle.database.models.family import (
    Family,
    FavoriteTeam,
    OAuthTransaction,
    YotoCredential,
)
from huddle.database.models.show import ShowRun, ShowSegmentRow
from huddle.database.models.source import (
    IngestionRun,
    ManualOverride,
    SourceFreshness,
    SourceProvider,
)
from huddle.database.models.sports import Athlete, Game, NewsStory, Team

__all__ = [
    "Athlete",
    "Base",
    "Family",
    "FavoriteTeam",
    "Game",
    "IngestionRun",
    "ManualOverride",
    "NewsStory",
    "OAuthTransaction",
    "ProvenanceMixin",
    "ShowRun",
    "ShowSegmentRow",
    "SourceFreshness",
    "SourceProvider",
    "Team",
    "TimestampMixin",
    "YotoCredential",
    "new_id",
    "utcnow",
]
