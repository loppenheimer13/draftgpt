"""Provider adapter contracts.

Every external input enters the system through one of these. Adapters return
*normalized payload objects*, never ORM rows -- persistence is the ingestion
layer's job, which keeps adapters trivially testable against recorded fixtures.

Each adapter declares its licensing posture explicitly. An adapter whose terms
prohibit automated access must not be written; use a manual import adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from draftgpt.domain.enums import Capability, FreshnessClass


@dataclass(frozen=True)
class ProviderSpec:
    """Static declaration registered into ``source_providers`` on boot."""

    key: str
    display_name: str
    categories: tuple[str, ...]
    capabilities: tuple[Capability, ...]
    freshness_class: FreshnessClass = FreshnessClass.DAILY
    requires_auth: bool = False
    rate_limit_per_min: int | None = None
    raw_retention_days: int | None = 30
    #: Plain-language note on terms of use. Reviewed before any adapter ships.
    license_note: str = "unspecified"
    #: What the system does when this provider fails.
    fallback: str = "degrade and warn"


@dataclass
class FetchResult:
    """One adapter call's output plus the provenance the ingestion layer records."""

    capability: str
    records: list[dict[str, Any]] = field(default_factory=list)
    retrieved_at: datetime | None = None
    effective_at: datetime | None = None
    raw: Any = None
    partial: bool = False
    notes: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.records)


class ProviderError(RuntimeError):
    """Adapter failure. Never carries credentials or full payloads."""

    def __init__(self, provider: str, capability: str, message: str) -> None:
        super().__init__(f"[{provider}:{capability}] {message}")
        self.provider = provider
        self.capability = capability


class ProviderAuthError(ProviderError):
    """Credentials missing, expired, or insufficient for a private league."""


class ProviderUnavailable(ProviderError):
    """Transient: network, rate limit, or upstream outage. Safe to retry."""


@runtime_checkable
class Provider(Protocol):
    """Minimal contract. Adapters implement only the capabilities they declare."""

    spec: ProviderSpec

    def supports(self, capability: str) -> bool: ...

    def fetch(self, capability: str, **params: Any) -> FetchResult: ...

    def health_check(self) -> tuple[bool, str]: ...


class BaseProvider:
    """Convenience base implementing dispatch from capability -> method.

    An adapter defines ``fetch_<capability>(**params) -> FetchResult`` for each
    capability in its spec; the base class routes and validates.
    """

    spec: ProviderSpec

    def supports(self, capability: str) -> bool:
        return capability in {c.value for c in self.spec.capabilities}

    def fetch(self, capability: str, **params: Any) -> FetchResult:
        if not self.supports(capability):
            raise ProviderError(
                self.spec.key, capability, "capability not declared by this adapter"
            )
        handler = getattr(self, f"fetch_{capability}", None)
        if handler is None:
            raise ProviderError(
                self.spec.key, capability, "declared but not implemented"
            )
        result: FetchResult = handler(**params)
        result.capability = capability
        return result

    def health_check(self) -> tuple[bool, str]:
        return True, "ok"


class LeagueProvider(BaseProvider):
    """Marker for adapters serving league state (settings, rosters, draft...)."""


class ProjectionProvider(BaseProvider):
    """Marker for adapters serving projections, rankings, or tiers."""


class ContextProvider(BaseProvider):
    """Marker for schedule, injury, usage, weather, and betting context."""
