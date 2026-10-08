"""The simulator against real respondents (19 Twin-2K-500 studies, 13,879 people).

tests/fixtures/validation/twin.json holds aggregates computed from the studies'
response files by scripts/validate_runtime.py (the response rows themselves are
not in this repository). These are consistency checks against observed
responses, not a validation of what respondents experienced:

* each flow randomizer shows the same number of arms per respondent, the arms
  carry the same names as the real FL_<id>_DO_<arm> columns, and arm frequencies
  match (within sampling error);
* blocks with randomized question order show the same number of questions;
* feeding real respondents' final embedded data and answers to the logic
  evaluator never predicts a question or branch was hidden when it was answered.
  The recorded result also counts the other direction (predicted shown but
  unanswered), which is unknown rather than a violation, and the checks that
  depend on fields set inside the flow, whose final value may not be the value
  at the decision point. See the script's docstring.
* replaying each finished respondent through the simulator with their recorded
  randomization, answers and inputs (``runtime.replay``), so every branch and
  display condition sees the state at that point, never displays less than they
  answered, and the embedded data the flow sets ends as exported.

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
from surveyir.runtime import ScreenerAwareAnswerer, Simulator

ROOT = Path(__file__).resolve().parents[1]
DATA = json.loads((ROOT / "tests" / "fixtures" / "validation" / "twin.json").read_text())
STUDIES = sorted(DATA)

#: question JavaScript these studies run and the simulator does not (``surveyir check``).
#: The checks below compare flow randomizers, block question order and logic replayed
#: on real responses, none of which the scripts drive: they change what is shown
#: (exposure), or randomize into embedded data (promiscuous_donors' conjoint profiles,
#: recommendation_algorithms' platform pick and screen-out field), which the replay
#: reads from the real responses instead.
ALLOW: dict[str, set[str]] = {
    "context_effects": {"javascript:QID631"},
    "default_eric": {"javascript:QID631"},
    "idea_evaluation": {f"javascript:{q}" for q in ("QID4", "QID9", "QID15", "QID19")},
    "promiscuous_donors": {"javascript:QID481"},
    "recommendation_algorithms": {"javascript:QID7"},
}


@pytest.fixture(scope="module")
def simulations() -> dict:
    out = {}
    for study in STUDIES:
        survey = load_qsf(ROOT / "tests" / "fixtures" / "qualtrics" / f"{study}.qsf")
        sim = Simulator(survey, seed=7, allow=ALLOW.get(study, ()))
        out[study] = sim.run(2000, ScreenerAwareAnswerer(survey, seed=9))
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
def test_recorded_consistency_checks_have_no_violations(study):
    """Reads the recorded result in twin.json; recomputing it needs the raw data (below)."""
    checks = DATA[study]["consistency"]
    for kind in ("display", "branch"):
        t = checks[kind]
        assert t["violations"] == 0, kind
        fs = t["final_state_dependent"]
        assert fs["violations"] == 0 and fs["checks"] <= t["checks"], kind
        assert t["unknown"] <= t["checks"] and fs["unknown"] <= t["unknown"]


def test_recorded_consistency_checks_cover_both_directions():
    """The recorded checks are not vacuous, and both directions and the final-state
    subset are reported."""
    totals = {
        kind: {
            k: sum(DATA[s]["consistency"][kind][k] for s in STUDIES) for k in ("checks", "unknown")
        }
        for kind in ("display", "branch")
    }
    assert totals["display"]["checks"] > 10_000 and totals["branch"]["checks"] > 20_000
    assert totals["branch"]["unknown"] > 0  # taken but unanswered: reported, not a violation
    final_state = sum(
        DATA[s]["consistency"][k]["final_state_dependent"]["checks"]
        for s in STUDIES
        for k in ("display", "branch")
    )
    assert 0 < final_state < totals["display"]["checks"] + totals["branch"]["checks"]


@pytest.mark.skipif(not os.environ.get("SURVEYIR_TWIN_DAT"), reason="raw responses not available")
def test_aggregates_are_reproducible_from_raw_responses(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "validate_runtime", ROOT / "scripts" / "validate_runtime.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out = tmp_path / "twin.json"
    monkeypatch.setattr(module, "OUT", out)
    module.main(Path(os.environ["SURVEYIR_TWIN_DAT"]).expanduser())
    assert json.loads(out.read_text()) == DATA


#: studies whose respondents the replay cannot administer, and why (see twin.json)
REPLAY_UNVERIFIABLE = {
    # The flow shows the "consent for non-twins" block only if embedded field
    # ``drop`` is "yes"; nothing declares or exports ``drop``, so it came from
    # outside. Every respondent answered that block's question, and none answered
    # the unconditional, forced-response "consent" block after it: the fielded
    # flow evidently differed from this QSF here. Not a runtime discrepancy.
    "obedient_twins": {"undeclared_field": 1001},
}
#: embedded agreement below 100%, and why
EMBEDDED_DISAGREEMENT: dict[str, tuple[int, int]] = {
    # study -> (agree, compared) where a disagreement is understood and documented.
    # Empty: every embedded field the flow sets matches the export. (story_beliefs
    # disagreed until embedded data was stored as typed, markup and whitespace kept.)
}


@pytest.mark.parametrize("study", STUDIES)
def test_recorded_replay_has_no_violations(study):
    """Reads the recorded replay in twin.json (recomputed from raw data below)."""
    r = DATA[study]["replay"]
    assert r["violations"] == 0  # nothing answered that the replay did not display
    assert r["not_in_display_order"] == 0  # nor displayed what the recorded order omits
    assert r["in_display_order_not_displayed"] == 0  # nor hid what Qualtrics recorded shown
    assert r["ended_early"] == 0  # every finished respondent finishes the replay
    assert r["replayed"] + sum(r["unverifiable"].values()) == r["finished"]
    assert r["unverifiable"] == REPLAY_UNVERIFIABLE.get(study, {})
    assert r["unknown"] <= r["answered"] or r["replayed"] == 0
    assert set(r["allowed"]) <= {"javascript"}  # no reconstructed answer is invalid
    assert not r["order_fallbacks"]  # every order came from the response
    e = r["embedded"]
    assert (e["agree"], e["compared"]) == EMBEDDED_DISAGREEMENT.get(
        study, (e["compared"], e["compared"])
    )


def test_recorded_replay_covers_the_studies():
    replayed = sum(DATA[s]["replay"]["replayed"] for s in STUDIES)
    answered = sum(DATA[s]["replay"]["answered"] for s in STUDIES)
    compared = sum(DATA[s]["replay"]["embedded"]["compared"] for s in STUDIES)
    finished = sum(DATA[s]["replay"]["finished"] for s in STUDIES)
    assert replayed >= 0.9 * finished and answered > 200_000 and compared > 15_000
    # some unknowns (optional comment boxes, choices the export lacks) are reported
    assert sum(DATA[s]["replay"]["unknown"] for s in STUDIES) > 0
