"""The exporter interface and registry.

An exporter turns a ``Survey`` into some output format. Write one by
subclassing ``Exporter`` and registering it under the ``surveyir.exporters``
entry-point group in your package's ``pyproject.toml``::

    [project.entry-points."surveyir.exporters"]
    myformat = "mypackage.exporters:MyExporter"

Exporter options are constructor keyword arguments, so the same exporter can be
configured once and reused across many surveys.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, ClassVar

from ..model import Survey


class Exporter(ABC):
    #: Registry name, e.g. "markdown".
    name: ClassVar[str]
    #: Default file extension, including the dot.
    extension: ClassVar[str] = ".txt"
    media_type: ClassVar[str] = "text/plain"
    #: One-line description shown by ``surveyir formats``.
    summary: ClassVar[str] = ""

    def __init__(self, **options: Any) -> None:
        if options:
            raise TypeError(f"{type(self).__name__} got unknown options: {sorted(options)}")

    @abstractmethod
    def export(self, survey: Survey) -> str | bytes:
        """Return the exported document."""

    def write(self, survey: Survey, path: str | Path) -> Path:
        path = Path(path)
        data = self.export(survey)
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data, encoding="utf-8")
        return path


def _builtin() -> dict[str, type[Exporter]]:
    from .codebook import CodebookExporter
    from .json import JsonExporter
    from .markdown import MarkdownExporter

    return {cls.name: cls for cls in (JsonExporter, MarkdownExporter, CodebookExporter)}


def available_exporters() -> dict[str, type[Exporter]]:
    """Exporter classes by name: built-ins plus registered entry points."""
    found = _builtin()
    for ep in entry_points(group="surveyir.exporters"):
        if ep.name not in found:
            found[ep.name] = ep.load()
    return found


def get_exporter(name: str, **options: Any) -> Exporter:
    exporters = available_exporters()
    if name not in exporters:
        raise ValueError(f"Unknown export format {name!r}; available: {sorted(exporters)}")
    return exporters[name](**options)


def export(survey: Survey, format: str, **options: Any) -> str | bytes:
    """Export ``survey`` with the named exporter."""
    return get_exporter(format, **options).export(survey)
