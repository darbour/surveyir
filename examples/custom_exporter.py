"""Write an exporter and use it.

    uv run python examples/custom_exporter.py [survey.qsf]

To make an exporter available to `surveyir convert -t <name>`, register it in
your package's pyproject.toml:

    [project.entry-points."surveyir.exporters"]
    question-list = "my_package.exporters:QuestionList"
"""

from __future__ import annotations

import sys

import surveyir
from surveyir import Exporter


class QuestionList(Exporter):
    """One tab-separated line per answerable question."""

    name = "question-list"
    extension = ".tsv"
    summary = "Tab-separated: export tag, kind, number of choices, text"

    def __init__(self, *, width: int = 60) -> None:
        self.width = width

    def export(self, survey: surveyir.Survey) -> str:
        lines = ["tag\tkind\tchoices\ttext"]
        for q in survey.iter_questions(responses_only=True):
            items = getattr(q, "choices", None) or getattr(q, "items", None) or []
            lines.append(f"{q.export_tag}\t{q.kind}\t{len(items)}\t{q.text.plain[: self.width]}")
        return "\n".join(lines)


def main(path: str = "tests/fixtures/qualtrics/hiring_algorithms.qsf") -> None:
    survey = surveyir.load(path)
    print(QuestionList(width=50).export(survey))


if __name__ == "__main__":
    main(*sys.argv[1:])
