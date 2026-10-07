"""Export the IR itself as JSON (the canonical interchange format)."""

from __future__ import annotations

import json
from typing import Any

from ..model import Survey
from .base import Exporter


def _strip(obj: Any, keys: set[str]) -> Any:
    if isinstance(obj, dict):
        return {k: _strip(v, keys) for k, v in obj.items() if k not in keys}
    if isinstance(obj, list):
        return [_strip(v, keys) for v in obj]
    return obj


class JsonExporter(Exporter):
    """The full IR as JSON. Validates against ``surveyir schema`` output.

    Options:
        indent: JSON indentation (None for compact).
        compact: drop ``extras`` and ``html`` (source-format detail), keeping
            only the typed IR. Smaller, but no longer lossless.
    """

    name = "json"
    extension = ".json"
    media_type = "application/json"
    summary = "The surveyir IR as JSON (lossless unless compact=True)."

    def __init__(self, *, indent: int | None = 2, compact: bool = False) -> None:
        self.indent = indent
        self.compact = compact

    def export(self, survey: Survey) -> str:
        data = survey.model_dump(mode="json")
        if self.compact:
            data = _strip(data, {"extras", "html", "options"})
        return json.dumps(data, indent=self.indent, ensure_ascii=False)
