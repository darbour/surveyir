"""The simulator's seeded output on every fixture study is pinned (see golden_runs.py)."""

from __future__ import annotations

import json
import warnings

import pytest
from golden_runs import GOLDEN, all_snapshots

EXPECTED = json.loads(GOLDEN.read_text())


@pytest.fixture(scope="module")
def actual():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return all_snapshots(strict=False)


@pytest.mark.parametrize("study", sorted(EXPECTED))
def test_seeded_runs_are_unchanged(study, actual) -> None:
    assert actual[study] == EXPECTED[study]
