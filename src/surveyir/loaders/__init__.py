"""Loaders turn a source survey format into a ``Survey``.

Third-party packages can register loaders under the ``surveyir.loaders``
entry-point group; ``load()`` picks one by name or by file extension.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Protocol

from ..model import Survey


class Loader(Protocol):
    name: str
    extensions: tuple[str, ...]

    def load(self, source: Any) -> Survey: ...


def _builtin() -> dict[str, type]:
    from .qualtrics import QualtricsLoader

    return {"qualtrics": QualtricsLoader}


def available_loaders() -> dict[str, type]:
    """Loader classes by name: built-ins plus any registered entry points."""
    found = _builtin()
    for ep in entry_points(group="surveyir.loaders"):
        if ep.name not in found:
            found[ep.name] = ep.load()
    return found


def load(source: str | Path, *, format: str | None = None, **options: Any) -> Survey:
    """Load a survey file, choosing the loader by ``format`` or file extension."""
    loaders = available_loaders()
    if format is None:
        suffix = Path(source).suffix.lower()
        matches = [n for n, cls in loaders.items() if suffix in getattr(cls, "extensions", ())]
        if not matches:
            raise ValueError(f"No loader for {suffix!r} files; pass format= one of {list(loaders)}")
        format = matches[0]
    if format not in loaders:
        raise ValueError(f"Unknown format {format!r}; available: {list(loaders)}")
    return loaders[format](**options).load(source)
