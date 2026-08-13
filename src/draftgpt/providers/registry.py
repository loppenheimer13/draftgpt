"""Provider registry.

Commands request a *capability*, never a named provider. That indirection is
what lets a fixture projection source stand in for a licensed one, and what
lets a second league platform be added without touching evaluation code.
"""

from __future__ import annotations

from collections.abc import Callable

from draftgpt.config import Settings, get_settings
from draftgpt.providers.base import Provider, ProviderSpec

_BUILDERS: dict[str, Callable[[Settings], Provider]] = {}


ProviderBuilder = Callable[[Settings], Provider]


def register(key: str) -> Callable[[ProviderBuilder], ProviderBuilder]:
    def decorator(builder: Callable[[Settings], Provider]) -> Callable[[Settings], Provider]:
        _BUILDERS[key] = builder
        return builder

    return decorator


def _load_builtin() -> None:
    """Import adapter modules for their registration side effects."""
    from draftgpt.providers import espn, fixture, nflverse  # noqa: F401


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

    Preference order: the configured league provider first for league
    capabilities, then registration order. Fixture adapters register last so a
    real source always wins when both are present.
    """
    settings = settings or get_settings()
    candidates = providers_for(capability, settings)
    if not candidates:
        raise KeyError(f"no provider serves capability '{capability}'")
    preferred = settings.league_provider
    for provider in candidates:
        if provider.spec.key == preferred:
            return provider
    non_fixture = [p for p in candidates if not p.spec.key.startswith("fixture")]
    return (non_fixture or candidates)[0]
