"""Export-column naming checked against real Qualtrics exports (external corpus)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.column_oracle import evaluate

ROOT = Path(__file__).resolve().parents[1]
HAVE = (ROOT / "corpus" / "external_columns").exists() and (ROOT / "corpus" / "external").exists()

#: Minimum share of real export columns predicted exactly, per question kind.
#: Misses that remain are survey-revision mismatches the pairing cannot detect.
MIN_RECALL = 0.97


@pytest.mark.skipif(not HAVE, reason="external corpus not downloaded")
def test_columns_match_real_exports():
    paired, counts = evaluate(ROOT)
    assert paired > 100
    low = {}
    for kind in {k for k, _ in counts}:
        hit, miss = counts[(kind, True)], counts[(kind, False)]
        if hit + miss >= 20 and hit / (hit + miss) < MIN_RECALL:
            low[kind] = f"{hit}/{hit + miss}"
    assert low == {}
