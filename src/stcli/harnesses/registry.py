"""Which harnesses exist, and which of them are installed on this machine."""

from __future__ import annotations

import os
import shlex

from stcli.harnesses.base import ConfiguredHarness, Harness, Installed

ENV_HARNESS = "STCLI_HARNESS"

_REGISTRY: dict[str, type[Harness]] = {}


def register(harness: type[Harness]) -> type[Harness]:
    """Class decorator that makes a harness visible to stcli."""
    _REGISTRY[harness.name] = harness
    return harness


def registered_names() -> list[str]:
    return sorted(_REGISTRY)


def preferred_name(config: dict) -> str | None:
    """The harness the user asked stcli to reach for first, if any."""
    return os.environ.get(ENV_HARNESS) or config.get("harness")


def _templates(config: dict) -> dict[str, tuple[str, ...]]:
    """User-supplied argv templates, keyed by harness name.

    An entry for a harness stcli ships overrides how it is invoked; an entry
    for an unknown name defines a harness of its own.
    """
    raw = config.get("commands")
    if not isinstance(raw, dict):
        return {}

    templates: dict[str, tuple[str, ...]] = {}
    for name, argv in raw.items():
        if isinstance(argv, str):
            argv = shlex.split(argv)
        if isinstance(argv, list) and argv:
            templates[str(name)] = tuple(str(part) for part in argv)
    return templates


def known(config: dict | None = None) -> list[Harness]:
    """Every harness stcli could use here, preferred one first."""
    config = config or {}
    templates = _templates(config)

    harnesses: list[Harness] = []
    for name in registered_names():
        harness_cls = _REGISTRY[name]
        template = templates.get(name)
        if template:
            harnesses.append(ConfiguredHarness(name, template, label=harness_cls.label))
        else:
            harnesses.append(harness_cls())

    for name, template in templates.items():
        if name not in _REGISTRY:
            harnesses.append(ConfiguredHarness(name, template))

    preferred = preferred_name(config)
    if preferred:
        harnesses.sort(key=lambda h: h.name != preferred)
    return harnesses


def get(name: str, config: dict | None = None) -> Harness | None:
    for harness in known(config):
        if harness.name == name:
            return harness
    return None


def installed(config: dict | None = None, allow_wsl: bool = True) -> list[Installed]:
    """Locate the harnesses that are actually present, preferred one first.

    PATH is searched for all of them before WSL is searched for any, so the
    common case never pays for a WSL round trip.
    """
    harnesses = known(config)

    found = []
    for harness in harnesses:
        location = harness.locate(allow_wsl=False)
        if location:
            found.append(Installed(harness, location))
    if found or not allow_wsl:
        return found

    for harness in harnesses:
        location = harness.locate(allow_wsl=True)
        if location:
            found.append(Installed(harness, location))
    return found
