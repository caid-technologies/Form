"""Central environment-backed configuration access for Forma.

Application code imports :data:`config` instead of reading ``os.getenv``
directly. The object intentionally remains live: user-scoped integration
settings are resolved into explicit request snapshots by the hosted API, while
tests and local CLI flows may still patch the environment within a context
manager.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Dict, Iterable, Iterator, Mapping, Optional


TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
DEFAULT_GENERATION_WORKFLOW_ENV = "FORMA_DEFAULT_GENERATION_WORKFLOW"
DEFAULT_GENERATION_WORKFLOW = "web_research"
CLOUDFLARE_ENABLE_THINKING_ENV = "CLOUDFLARE_ENABLE_THINKING"


class AppConfig:
    """Single environment access boundary with consistent parsing semantics."""

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        value = os.environ.get(name)
        return default if value is None else value

    def optional(self, name: str) -> Optional[str]:
        value = self.get(name)
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    def first(self, names: Iterable[str], default: Optional[str] = None) -> Optional[str]:
        for name in names:
            value = self.optional(name)
            if value is not None:
                return value
        return default

    def is_set(self, name: str) -> bool:
        return name in os.environ

    def snapshot(self) -> Dict[str, str]:
        """Return a copy suitable for subprocesses and explicit config merging."""
        return dict(os.environ)

    def set(self, name: str, value: str) -> None:
        os.environ[name] = value

    def unset(self, name: str) -> None:
        os.environ.pop(name, None)

    def set_default(self, name: str, value: str) -> str:
        return os.environ.setdefault(name, value)

    def update(self, values: Mapping[str, str]) -> None:
        os.environ.update(values)

    def replace(self, values: Mapping[str, str]) -> None:
        """Replace the process environment with an explicit snapshot."""
        os.environ.clear()
        os.environ.update(values)

    @contextmanager
    def override(self, values: Mapping[str, Optional[str]]) -> Iterator[None]:
        """Temporarily apply non-None values, restoring the exact prior state."""
        active_values = {name: value for name, value in values.items() if value is not None}
        previous = {name: self.get(name) for name in active_values}
        self.update(active_values)
        try:
            yield
        finally:
            for name, value in previous.items():
                if value is None:
                    self.unset(name)
                else:
                    self.set(name, value)

    def boolean(self, name: str, default: bool = False) -> bool:
        value = self.get(name)
        if value is None:
            return default
        return value.strip().lower() in TRUE_VALUES

    def integer(self, name: str, default: int) -> int:
        value = self.optional(name)
        if value is None:
            return default
        try:
            return int(value)
        except ValueError:
            return default

    def number(self, name: str, default: float) -> float:
        value = self.optional(name)
        if value is None:
            return default
        try:
            return float(value)
        except ValueError:
            return default

    @property
    def default_generation_workflow(self) -> str:
        """Configured initial workflow; request-level selections still take precedence."""
        value = self.optional(DEFAULT_GENERATION_WORKFLOW_ENV)
        return value or DEFAULT_GENERATION_WORKFLOW

    @property
    def cloudflare_enable_thinking(self) -> bool:
        """Enable native model thinking for Cloudflare structured requests."""
        return self.boolean(CLOUDFLARE_ENABLE_THINKING_ENV, False)


config = AppConfig()


__all__ = [
    "AppConfig",
    "CLOUDFLARE_ENABLE_THINKING_ENV",
    "DEFAULT_GENERATION_WORKFLOW",
    "DEFAULT_GENERATION_WORKFLOW_ENV",
    "TRUE_VALUES",
    "config",
]
