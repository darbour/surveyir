"""Load a Qualtrics survey and print what it contains.

uv run python examples/inspect_survey.py [path/to/survey.qsf]
"""

from __future__ import annotations

import sys

import surveyir


def main(path: str = "tests/fixtures/qualtrics/obedient_twins.qsf") -> None:
    survey = surveyir.load(path)
    print(f"{survey.name}: {len(survey.questions)} questions in {len(survey.blocks)} blocks")

    print("\nQuestions (in flow order):")
    for q in survey.iter_questions(responses_only=True):
        print(f"  {q.export_tag:<20} {q.kind:<12} {q.text.plain[:60]!r}")

    print("\nExperimental design:")
    print(surveyir.design(survey).summary() or "  no randomization")

    problems = [d for d in survey.diagnostics if d.level != "info"]
    print(f"\n{len(problems)} warning(s) or error(s) while loading")
    for d in problems:
        print(f"  {d.code}: {d.message}")


if __name__ == "__main__":
    main(*sys.argv[1:])
