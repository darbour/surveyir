"""Simulate respondents and write them as a Qualtrics-style CSV.

    uv run python examples/simulate_to_csv.py [survey.qsf] [n] > simulated.csv

The columns match a real Qualtrics export of the survey (with metadata), so the
same analysis code can read simulated and real data.
"""

from __future__ import annotations

import csv
import sys
from collections import Counter

import surveyir
from surveyir.columns import ColumnOptions
from surveyir.runtime import ScreenerAwareAnswerer, Simulator


def main(path: str = "tests/fixtures/qualtrics/obedient_twins.qsf", n: str = "100") -> None:
    survey = surveyir.load(path)
    options = ColumnOptions(include_metadata=True)
    sim = Simulator(survey, seed=1)
    runs = sim.run(int(n), ScreenerAwareAnswerer(survey, seed=2))

    writer = csv.writer(sys.stdout)
    writer.writerow([c.name for c in surveyir.response_columns(survey, options=options)])
    for run in runs:
        writer.writerow([value for _, value in run.cells(survey, options)])

    # A summary on stderr, so it doesn't end up in the CSV.
    for factor in surveyir.design(survey).between_subjects:
        shown = Counter(arm for r in runs for arm in r.flow_order.get(factor.id, []))
        print(f"{factor.id}: {dict(shown)}", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:])
