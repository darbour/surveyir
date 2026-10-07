"""The simulator against real respondents (19 Twin-2K-500 studies, 13,879 people).

tests/fixtures/validation/twin.json holds aggregates computed from the studies'
response files by scripts/validate_runtime.py (the response rows themselves are
not in this repository). These tests check that simulated respondents reproduce
what real Qualtrics respondents experienced:

* each flow randomizer shows the same number of arms per respondent, the arms
  carry the same names as the real FL_<id>_DO_<arm> columns, and arm frequencies
  match (within sampling error);
* blocks with randomized question order show the same number of questions;
* replaying real respondents' embedded data and answers through the logic
  evaluator never predicts a question or branch was hidden when it was answered.

Set SURVEYIR_TWIN_DAT to the Twin-2K-500-Mega-Study ``.dat`` directory to also
recompute the aggregates from the raw responses and compare them to the fixture.
"""

from __future__ import annotations

import json
import math
import os
from collections import Counter
from pathlib import Path

import pytest

from surveyir import load_qsf
from surveyir.model import TextEntryQuestion, iter_comparisons, walk
from surveyir.runtime import RandomAnswerer, Simulator

ROOT = Path(__file__).resolve().parents[1]
DATA = json.loads((ROOT / "tests" / "fixtures" / "validation" / "twin.json").read_text())
STUDIES = sorted(DATA)


class ScreenerAware(RandomAnswerer):
    """Random answers, except text answers contain every string the survey's own
    logic tests that question's text for, so screeners such as "end the survey
    unless the answer contains X" let simulated respondents through (validation only)."""

    def __init__(self, survey, seed: int) -> None:
        super().__init__(seed)
        self.strings: dict[str, list[str]] = {}
        conditions = [n.condition for n in walk(survey.flow) if getattr(n, "condition", None)]
        conditions += [q.display_logic for q in survey.questions.values() if q.display_logic]
        for cond in conditions:
            for c in iter_comparisons(cond):
                if c.left.selector == "ChoiceTextEntryValue" and c.right and c.left.question_id:
                    self.strings.setdefault(c.left.question_id, []).append(c.right)

    def __call__(self, view, state):
        if isinstance(view.question, TextEntryQuestion) and view.question.mode != "form":
            return " ".join(self.strings.get(view.question.id, ["text"]))
        return super().__call__(view, state)


@pytest.fixture(scope="module")
def simulations() -> dict:
    out = {}
    for study in STUDIES:
        survey = load_qsf(ROOT / "tests" / "fixtures" / "qualtrics" / f"{study}.qsf")
        out[study] = Simulator(survey, seed=7).run(2000, ScreenerAware(survey, seed=9))
    return out


#: study -> (compared, too few real respondents, not reached in simulation)
COVERAGE: dict[str, tuple[int, int, int]] = {}


@pytest.mark.parametrize("study", STUDIES)
def test_flow_randomizers_match_real_respondents(study, simulations):
    runs = simulations[study]
    compared = small = unreached = 0
    for fid, real in DATA[study]["randomizers"].items():
        reached = [r for r in runs if fid in r.flow_order]
        if real["reached"] < 50:
            small += 1  # too few real respondents to compare frequencies
            continue
        if len(reached) < 50:
            unreached += 1  # behind answer-dependent branches the answerer rarely satisfies
            continue
        compared += 1
        shown = Counter(len(r.flow_order[fid]) for r in reached)
        assert set(shown) == {int(k) for k in real["shown_per_respondent"]}, fid
        sim_arms = Counter(k for r in reached for k in r.flow_order[fid])
        assert set(sim_arms) == {a for a, n in real["arms"].items() if n}, fid
        for arm, n in real["arms"].items():
            p_real, p_sim = n / real["reached"], sim_arms[arm] / len(reached)
            tol = 3 * math.sqrt(max(p_real * (1 - p_real), 0.01) / real["reached"]) + 0.02
            assert abs(p_real - p_sim) <= tol, (fid, arm, p_real, p_sim)
    COVERAGE[study] = (compared, small, unreached)


def test_validation_coverage(simulations):
    """Most real randomizers must actually be compared, not skipped as unreached."""
    for study in STUDIES:
        if study not in COVERAGE:
            test_flow_randomizers_match_real_respondents(study, simulations)
    compared = sum(c for c, _, _ in COVERAGE.values())
    unreached = sum(u for _, _, u in COVERAGE.values())
    assert compared >= 50 and unreached <= 0.05 * (compared + unreached), COVERAGE


@pytest.mark.parametrize("study", STUDIES)
def test_randomized_blocks_show_same_number_of_questions(study, simulations):
    for block_id, real in DATA[study]["blocks"].items():
        orders = [
            r.block_order[(block_id, None)]
            for r in simulations[study]
            if (block_id, None) in r.block_order
        ]
        if orders:
            assert {len(o) for o in orders} == {int(k) for k in real["shown_per_respondent"]}


@pytest.mark.parametrize("study", STUDIES)
def test_logic_replay_has_no_violations(study):
    replay = DATA[study]["replay"]
    assert replay.get("display_violations", 0) == 0
    assert replay.get("branch_violations", 0) == 0


@pytest.mark.skipif(not os.environ.get("SURVEYIR_TWIN_DAT"), reason="raw responses not available")
def test_aggregates_are_reproducible_from_raw_responses(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "validate_runtime", ROOT / "scripts" / "validate_runtime.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out = tmp_path / "twin.json"
    monkeypatch.setattr(module, "OUT", out)
    module.main(Path(os.environ["SURVEYIR_TWIN_DAT"]).expanduser())
    assert json.loads(out.read_text()) == DATA
