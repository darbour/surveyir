"""executability(): the static inventory of what can be administered exactly."""

from __future__ import annotations

import json
import warnings
from collections import Counter
from pathlib import Path

import pytest

from surveyir import load_qsf
from surveyir.model import (
    AuthenticatorNode,
    BlockNode,
    BranchNode,
    CarryForward,
    ChoiceQuestion,
    Comparison,
    EmbeddedDataNode,
    EmbeddedField,
    LibraryBlockNode,
    LoopAndMerge,
    Operand,
    Quota,
    UnsupportedNode,
    WebServiceNode,
)
from surveyir.runtime.executability import STATUSES, executability
from surveyir.runtime.execution import APPROXIMATIONS, ExecutionPolicy
from surveyir.text import make_text
from tests.conftest import QSF_FILES, mc, minimal_qsf

ROOT = Path(__file__).resolve().parents[1]
EXTERNAL = sorted((ROOT / "corpus" / "external").glob("*.qsf"))


def load(name: str):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return load_qsf(Path(__file__).parent / "fixtures" / "qualtrics" / f"{name}.qsf")


@pytest.mark.parametrize("path", QSF_FILES, ids=[p.stem for p in QSF_FILES])
def test_every_fixture(path):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        report = executability(load_qsf(path))
    assert report.features and report.summary()
    json.dumps(report.to_dict(ExecutionPolicy()))
    for f in report.features:
        assert f.status in STATUSES
        if f.status in ("needs_implementation", "approximated"):
            assert f.code in APPROXIMATIONS
        else:
            assert f.code is None
    assert ExecutionPolicy(strict=False) and report.blocking(ExecutionPolicy(strict=False)) == []


def rows(report, code):
    return [(f.location, f.status, f.affects) for f in report.features if f.code == code]


def test_fixture_spot_checks():
    donors = executability(load("promiscuous_donors"))
    assert rows(donors, "javascript") == [("QID481", "needs_implementation", "assignment")]
    assert [f.feature for f in donors.by_status("preserved_only")] == ["conjoint"]
    [blocking] = donors.blocking(ExecutionPolicy())
    assert blocking.location == "QID481"
    assert donors.blocking(ExecutionPolicy(implementations={"javascript": {"QID481": dict}})) == []
    assert donors.blocking(ExecutionPolicy(allow=frozenset({"javascript:QID481"}))) == []

    assert rows(executability(load("recommendation_algorithms")), "javascript") == [
        ("QID7", "needs_implementation", "assignment")
    ]
    assert {loc for loc, *_ in rows(executability(load("idea_evaluation")), "javascript")} == {
        "QID4",
        "QID9",
        "QID15",
        "QID19",
    }
    # story_beliefs declares its chapter texts as recipient fields, then sets them
    # in the flow, and never reads the panel fields (PROLIFIC_PID, ...) it declares
    stories = executability(load("story_beliefs"))
    assert rows(stories, "embedded.unset") == [] and stories.blocking(ExecutionPolicy()) == []

    # idea_generation's in-flow JavaScript is entirely commented out; the live
    # scripts that set embedded data sit in blocks the flow never reaches
    ideas = executability(load("idea_generation"))
    assert rows(ideas, "javascript") == []
    assert {f.feature for f in ideas.features} >= {"randomizer", "branch", "loop", "skip_logic"}


def synthetic():
    survey = load_qsf(minimal_qsf([mc("QID1"), mc("QID2", QuestionJS="jQuery('x').hide();")]))
    geo = Comparison(
        left=Operand(kind="geo_ip", name="loc://CountryName", raw="loc://CountryName"),
        operator="equal",
        source_operator="EqualTo",
        right="US",
    )
    pid = Comparison(
        left=Operand(kind="embedded_data", name="pid", raw="pid"),
        operator="not_empty",
        source_operator="NotEmpty",
    )
    q1 = survey.questions["QID1"]
    assert isinstance(q1, ChoiceQuestion)
    q1.carry_forward = CarryForward(source="reference_list", list_id="RL_1", mode="x", raw="rl")
    q1.text = make_text("Hi ${loc://City} on ${date://CurrentDate/SH} ${rand://pick/a}")
    survey.blocks["BL_1"].loop = LoopAndMerge(
        source="question", question_id="QID9", locator="q://QID9/Odd"
    )
    survey.quotas.append(Quota(id="QO_1", name="q", limit=5, action="SkipToEnd", condition=geo))
    survey.flow = [
        EmbeddedDataNode(id="FL_3", fields=[EmbeddedField(name="pid", source="recipient")]),
        WebServiceNode(id="FL_4", url="https://x", response_map={"a": "arm"}),
        BranchNode(id="FL_5", condition=geo, children=[BlockNode(id="FL_6", block_id="BL_1")]),
        BranchNode(id="FL_7", condition=pid, children=[]),
        BranchNode(id="FL_8", condition=None, children=[]),
        LibraryBlockNode(id="FL_9", reference_id="LS_1"),
        AuthenticatorNode(id="FL_10"),
        UnsupportedNode(id="FL_11", source_type="Mystery"),
        BlockNode(id="FL_12", block_id="BL_gone"),
    ]
    return survey


def test_synthetic_inventory():
    report = executability(synthetic())
    got = {(f.code, f.location): (f.status, f.affects) for f in report.features if f.code}
    assert got == {
        ("embedded.unset", "pid"): ("needs_implementation", "routing"),
        ("web_service", "FL_4"): ("needs_implementation", "assignment"),
        ("logic.geo_ip", "loc://CountryName"): ("needs_implementation", "routing"),
        ("branch.no_condition", "FL_8"): ("approximated", "routing"),
        ("library_block", "FL_9"): ("needs_implementation", "exposure"),
        ("flow.authenticator", "FL_10"): ("approximated", "routing"),
        ("flow.unsupported", "FL_11"): ("approximated", "routing"),
        ("flow.missing_block", "BL_gone"): ("approximated", "exposure"),
        ("quota.action_ignored", "QO_1"): ("approximated", "routing"),
        ("javascript", "QID2"): ("needs_implementation", "exposure"),
        ("carry_forward.reference", "QID1"): ("approximated", "exposure"),
        ("loop.unknown_mode", "BL_1"): ("approximated", "exposure"),
        ("pipe.loc", "City"): ("needs_implementation", "exposure"),
        ("pipe.date", "CurrentDate/SH"): ("approximated", "exposure"),
        ("pipe.random_unsupported", "pick/a"): ("approximated", "exposure"),
    }
    blocking = report.blocking(ExecutionPolicy())
    assert {(f.code, f.location) for f in blocking} == set(got)
    assert len(blocking) == len(got) + 1  # the GeoIP condition is in a branch and a quota
    policy = ExecutionPolicy(
        allow=frozenset({"flow.unsupported", "flow.authenticator:FL_10"}),
        implementations={
            "location": {"CountryName": "US", "City": "Paris"},
            "web_service": lambda node, state: {},
            "embedded": {"pid": "p1"},
        },
    )
    left = {(f.code, f.location) for f in report.blocking(policy)}
    assert left == set(got) - {
        ("flow.unsupported", "FL_11"),
        ("flow.authenticator", "FL_10"),
        ("web_service", "FL_4"),
        ("logic.geo_ip", "loc://CountryName"),
        ("pipe.loc", "City"),
        ("embedded.unset", "pid"),
    }
    text = report.summary()
    assert "Needs implementation:" in text and "Approximated:" in text
    data = report.to_dict(policy)
    assert data["counts"]["approximated"] == 9 and len(data["blocking"]) == len(left)
    assert all(
        f.resolution().startswith("implementations=")
        for f in report.features
        if f.status == "needs_implementation" and f.feature != "library_block"
    )


@pytest.mark.skipif(not EXTERNAL, reason="external corpus not downloaded")
def test_external_corpus_inventory():
    statuses: Counter[str] = Counter()
    for path in EXTERNAL:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                survey = load_qsf(path)
        except Exception:  # noqa: BLE001 - files that fail to load are the loader's concern
            continue
        report = executability(survey)
        statuses.update(f.status for f in report.features)
        assert all(f.code is None or f.code in APPROXIMATIONS for f in report.features)
    assert statuses["executable"] and statuses["needs_implementation"]
