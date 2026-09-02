"""Provider registry.

The show requests a *capability*, never a named provider. That indirection is
what lets recorded fixtures stand in for the live API in tests and offline
demos, and what would let a second data source be added without any caller
changing.
"""

from __future__ import annotations

from collections.abc import Callable

from huddle.config import Settings, get_settings
from huddle.providers.base import Provider, ProviderSpec

_BUILDERS: dict[str, Callable[[Settings], Provider]] = {}


ProviderBuilder = Callable[[Settings], Provider]


def register(key: str) -> Callable[[ProviderBuilder], ProviderBuilder]:
    def decorator(builder: Callable[[Settings], Provider]) -> Callable[[Settings], Provider]:
        _BUILDERS[key] = builder
        return builder

    return decorator


def _load_builtin() -> None:
    """Import adapter modules for their registration side effects."""
    from huddle.providers import espn, fixture  # noqa: F401


def available_keys() -> list[str]:
    _load_builtin()
    return sorted(_BUILDERS)


def build(key: str, settings: Settings | None = None) -> Provider:
    _load_builtin()
    if key not in _BUILDERS:
        raise KeyError(f"unknown provider '{key}'; available: {', '.join(available_keys())}")
    return _BUILDERS[key](settings or get_settings())


def all_specs(settings: Settings | None = None) -> list[ProviderSpec]:
    return [build(key, settings).spec for key in available_keys()]


def providers_for(capability: str, settings: Settings | None = None) -> list[Provider]:
    """Every enabled adapter that can serve ``capability``, in registration order."""
    found = []
    for key in available_keys():
        provider = build(key, settings)
        if provider.supports(capability):
            found.append(provider)
    return found


def resolve(capability: str, settings: Settings | None = None) -> Provider:
    """The preferred adapter for a capability.

    Fixtures win only when ``HUDDLE_USE_FIXTURES`` is set, so a test or an
    offline demo is always an explicit choice -- a misconfigured live run must
    fail loudly rather than quietly narrating recorded data as if it were today.
    """
    settings = settings or get_settings()
    candidates = providers_for(capability, settings)
    if not candidates:
        raise KeyError(f"no provider serves capability '{capability}'")

    fixtures = [p for p in candidates if p.spec.key.startswith("fixture")]
    live = [p for p in candidates if not p.spec.key.startswith("fixture")]
    if settings.use_fixtures and fixtures:
        return fixtures[0]
    return (live or fixtures)[0]
