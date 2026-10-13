"""Reading real exports into replay recordings (``surveyir.read_responses``).

Simulated respondents are exported as Qualtrics exports them (``runtime.rows``),
read back, and replayed: every decision and answer the export records must come
back, and every one it does not must be reconstructed correctly or reported.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from surveyir import (
    ColumnOptions,
    RandomAnswerer,
    Simulator,
    load,
    load_qsf,
    read_responses,
    write_responses_csv,
)
from surveyir.responses import REPLAY_FALLBACK, _subset_randomizers
from surveyir.runtime import Replayer
from surveyir.runtime.reconstruct import _below
from tests.conftest import mc, minimal_qsf

FIXTURES = Path(__file__).parent / "fixtures"
QSFS = sorted((FIXTURES / "qualtrics").glob("*.qsf"))
LAYOUTS = {
    "default": ColumnOptions(include_metadata=True),
    "joined": ColumnOptions(split_multi_value=False, include_metadata=True),
    "single_do": ColumnOptions(display_order="single", include_metadata=True),
}


def _export(survey, runs, tmp_path, options):
    return read_responses(
        write_responses_csv(survey, runs, tmp_path / "x.csv", options=options), survey
    )


@pytest.fixture(scope="module")
def simulated():
    cache = {}

    def get(path):
        if path not in cache:
            survey = load(path)
            cache[path] = (
                survey,
                list(Simulator(survey, seed=1, strict=False).run(4, RandomAnswerer(seed=2))),
            )
        return cache[path]

    return get


#: export columns that hold the time of the walk, not anything the respondent did
_CLOCK = {"StartDate", "EndDate", "RecordedDate", "Duration (in seconds)"}


def _rows(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.reader(f))


@pytest.mark.parametrize("layout", list(LAYOUTS))
@pytest.mark.parametrize("path", QSFS, ids=lambda p: p.stem)
def test_export_round_trips_through_replay(path, layout, simulated, tmp_path):
    """Export, read, replay, export again: the two exports are identical, so every
    decision and answer the export holds came back and drove the replay the same way."""
    survey, runs = simulated(path)
    first = write_responses_csv(survey, runs, tmp_path / "a.csv", options=LAYOUTS[layout])
    recordings = read_responses(first, survey)
    assert len(recordings) == len(runs)
    replayer = Replayer(survey, strict=False, fallback_kinds=REPLAY_FALLBACK)
    again = []
    for run, rec in zip(runs, recordings, strict=True):
        assert rec.id == f"R_sim{run.index:06d}"
        assert rec.finished is run.finished
        again.append(
            replayer.respondent(
                rec.orders, rec.answers, embedded=rec.embedded, seed=run.seed, drawn=rec.drawn
            )
        )
        assert again[-1].flow_order == run.flow_order
    second = write_responses_csv(survey, again, tmp_path / "b.csv", options=LAYOUTS[layout])
    a, b = _rows(first), _rows(second)
    keep = [i for i, name in enumerate(a[0]) if name not in _CLOCK]
    assert [[r[i] for i in keep] for r in a] == [[r[i] for i in keep] for r in b]


@pytest.mark.parametrize(
    "path", [p for p in QSFS if _subset_randomizers(load(p))], ids=lambda p: p.stem
)
def test_flow_decisions_are_reconstructed_without_display_order(path, simulated, tmp_path):
    """Without FL_*_DO columns, every decision recovered is right, and every one not
    recovered is reported."""
    survey, runs = simulated(path)
    options = ColumnOptions(display_order="none", include_metadata=True)
    for run, rec in zip(runs, _export(survey, runs, tmp_path, options), strict=True):
        for fid in _subset_randomizers(survey):
            truth = run.flow_order.get(fid)
            got = rec.orders.get(("flow", fid, None))
            key = ("flow", fid, None)
            if key in rec.conditional:  # kept to reproduce the response, and flagged
                assert key in rec.orders
            elif got is not None and truth is not None:
                assert set(got) == set(truth), fid
                assert key in rec.inferred
            elif truth is not None:  # reached on a path the kept, flagged choice did not take
                flagged = {k[1] for k in rec.conditional} | {
                    n for k in rec.conditional for n in _below(survey, k[1])
                }
                assert any(f"flow {fid}:" in n for n in rec.unrecoverable) or any(
                    fid in _below(survey, f) for f in flagged
                ), fid


def test_arms_that_only_set_embedded_data_are_recovered(simulated, tmp_path):
    """targeting_fairness: both arms show the same blocks; the arm sets embedded data
    that display logic reads. The answers still say which arm it was."""
    survey, runs = simulated(FIXTURES / "qualtrics" / "targeting_fairness.qsf")
    options = ColumnOptions(display_order="none", include_metadata=True)
    for run, rec in zip(runs, _export(survey, runs, tmp_path, options), strict=True):
        assert rec.orders[("flow", "FL_491", None)] == tuple(run.flow_order["FL_491"])


def test_a_decision_the_answers_cannot_tell_is_kept_and_flagged(simulated, tmp_path):
    """default_eric: the outer randomizer only orders two inner ones that hold the
    same blocks, so without display order the answers cannot tell its arms apart. One
    is kept, so the replay reproduces the response, and flagged as not known to be
    the respondent's; the inner conditions (opt-in or opt-out) are the respondent's."""
    survey, runs = simulated(FIXTURES / "qualtrics" / "default_eric.qsf")
    options = ColumnOptions(display_order="none", include_metadata=True)
    path = write_responses_csv(survey, runs, tmp_path / "x.csv", options=options)
    replayer = Replayer(survey, strict=False, fallback_kinds=REPLAY_FALLBACK)
    again = []
    for run, rec in zip(runs, read_responses(path, survey), strict=True):
        assert ("flow", "FL_344", None) in rec.orders
        assert ("flow", "FL_344", None) in rec.conditional
        assert any("FL_344" in n and "not known" in n for n in rec.unrecoverable)
        replayed = replayer.respondent(rec.orders, rec.answers, embedded=rec.embedded)
        inner = sorted(v for f, v in run.flow_order.items() if f != "FL_344")
        assert sorted(v for f, v in replayed.flow_order.items() if f != "FL_344") == inner
        assert set(replayed.state.displayed) == set(run.state.displayed)
        again.append(replayed)
    second = write_responses_csv(survey, again, tmp_path / "y.csv", options=options)
    a, b = _rows(path), _rows(second)
    keep = [i for i, name in enumerate(a[0]) if name not in _CLOCK]
    assert [[r[i] for i in keep] for r in a] == [[r[i] for i in keep] for r in b]


def test_legacy_export_is_matched_by_name(simulated, tmp_path):
    survey, runs = simulated(FIXTURES / "qualtrics" / "targeting_fairness.qsf")
    path = write_responses_csv(survey, runs, tmp_path / "x.csv", options=LAYOUTS["default"])
    rows = list(csv.reader(path.open(encoding="utf-8")))
    legacy = tmp_path / "legacy.csv"
    with legacy.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows[:2] + rows[3:])  # no ImportId row
    recordings = read_responses(legacy, survey)
    assert len(recordings) == len(runs)
    assert any("legacy export" in n for n in recordings[0].unrecoverable)
    full = read_responses(path, survey)
    assert [r.answers for r in recordings] == [r.answers for r in full]


def test_finished_only(simulated, tmp_path):
    survey, runs = simulated(FIXTURES / "qualtrics" / "targeting_fairness.qsf")
    path = write_responses_csv(survey, runs, tmp_path / "x.csv", options=LAYOUTS["default"])
    rows = list(csv.reader(path.open(encoding="utf-8")))
    col = rows[0].index("Finished")
    rows[3][col] = "0"
    with path.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
    assert [r.finished for r in read_responses(path, survey)][:2] == [False, True]
    assert len(read_responses(path, survey, finished_only=True)) == len(runs) - 1


def test_limesurvey_exports_dispatch():
    survey = load(FIXTURES / "limesurvey" / "types_v6.lss")
    recordings = read_responses(FIXTURES / "limesurvey" / "responses.lsa", survey)
    assert len(recordings) == 3
    assert all(r.id for r in recordings)
    assert all(r.finished is not None for r in recordings)


def test_random_draws_are_kept(tmp_path):
    """A field the flow sets from ${rand://...} takes the respondent's exported draw on
    replay (a randomization by random number follows them), not a new draw."""
    flow = [
        {
            "Type": "EmbeddedData",
            "FlowID": "FL_2",
            "EmbeddedData": [{"Field": "draw", "Type": "Custom", "Value": "${rand://int/1:1000}"}],
        },
        {"Type": "Block", "ID": "BL_1", "FlowID": "FL_3"},
    ]
    survey = load_qsf(minimal_qsf([mc()], flow=flow))
    runs = Simulator(survey, seed=1).run(3, RandomAnswerer(seed=2))
    path = write_responses_csv(survey, runs, tmp_path / "x.csv", options=LAYOUTS["default"])
    rows = _rows(path)
    col = rows[0].index("draw")
    for r in rows[3:]:
        r[col] = "1234"  # a value the simulator's draw could not produce
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
    replayer = Replayer(survey, fallback_kinds=REPLAY_FALLBACK)
    for rec in read_responses(path, survey):
        assert rec.drawn == {"draw": "1234"}
        again = replayer.respondent(rec.orders, rec.answers, embedded=rec.embedded, drawn=rec.drawn)
        assert again.embedded["draw"] == "1234"


def test_scale_flip_is_read_off_display_order(tmp_path):
    """The flip is not exported, but a flip-scale question's display order shows it."""
    q = mc(
        Choices={str(i): {"Display": f"C{i}"} for i in range(1, 5)},
        ChoiceOrder=[1, 2, 3, 4],
        Randomization={"Type": "ScaleReversal"},
    )
    survey = load_qsf(minimal_qsf([q]))
    runs = Simulator(survey, seed=3).run(8, RandomAnswerer(seed=4))
    recordings = read_responses(
        write_responses_csv(survey, runs, tmp_path / "x.csv", options=LAYOUTS["default"]), survey
    )
    flips = [rec.orders[("flip", "scale", None)][0] for rec in recordings]
    assert flips == [
        "flipped" if run.choice_order[("QID1", None)] == ["4", "3", "2", "1"] else "normal"
        for run in runs
    ]
    assert set(flips) == {"flipped", "normal"}


def _another_round_survey():
    """'Another round?' (QID1) is asked, and asked again after a second round
    (QID2) that only a 'yes' leads to: the export keeps only the last answer."""
    again = mc("QID1", Choices={"1": {"Display": "Yes"}, "2": {"Display": "No"}})
    second = {
        "QuestionID": "QID2",
        "QuestionType": "TE",
        "Selector": "SL",
        "DataExportTag": "Q2",
        "QuestionText": "Round two",
    }
    qsf = minimal_qsf([again, second], block_elements=[{"Type": "Question", "QuestionID": "QID1"}])
    qsf["SurveyElements"][0]["Payload"].append(
        {
            "Type": "Standard",
            "ID": "BL_2",
            "Description": "Round two",
            "BlockElements": [{"Type": "Question", "QuestionID": "QID2"}],
        }
    )
    flow = [
        {"Type": "Block", "ID": "BL_1", "FlowID": "FL_2"},
        {
            "Type": "Branch",
            "FlowID": "FL_3",
            "BranchLogic": {
                "0": {
                    "0": {
                        "LogicType": "Question",
                        "LeftOperand": "q://QID1/SelectableChoice/1",
                        "Operator": "Selected",
                        "Type": "Expression",
                    },
                    "Type": "If",
                },
                "Type": "BooleanExpression",
            },
            "Flow": [
                {"Type": "Block", "ID": "BL_2", "FlowID": "FL_4"},
                {"Type": "Block", "ID": "BL_1", "FlowID": "FL_5"},
            ],
        },
    ]
    qsf["SurveyElements"][1]["Payload"]["Flow"] = flow
    return load_qsf(qsf)


class _YesThenNo:
    """Says yes to another round, then no; answers round two."""

    __respondent_protocol__ = 2

    def __init__(self) -> None:
        self.asked = 0

    def answer(self, ctx):
        if ctx.view.qid == "QID2":
            return "round two answer"
        self.asked += 1
        return "1" if self.asked == 1 else "2"


def test_an_overwritten_earlier_answer_is_recovered(tmp_path):
    survey = _another_round_survey()
    run = Simulator(survey, seed=1).respondent(_YesThenNo())
    assert run.answers[("QID1", None)].value == "2"
    assert ("QID2", None) in run.state.displayed
    path = write_responses_csv(survey, [run], tmp_path / "x.csv", options=LAYOUTS["default"])
    plain = read_responses(path, survey, reconstruct=False)[0]
    replayer = Replayer(survey, fallback_kinds=REPLAY_FALLBACK)
    naive = replayer.respondent(plain.orders, plain.answers, embedded=plain.embedded)
    assert ("QID2", None) not in naive.state.displayed  # 'no' both times: no round two
    rec = read_responses(path, survey)[0]
    assert rec.earlier == {("QID1", None, 0): "1"}
    again = replayer.respondent(rec.orders, rec.answers, embedded=rec.embedded, earlier=rec.earlier)
    assert again.answers == run.answers
    assert list(again.state.displayed) == list(run.state.displayed)
    assert not rec.unrecoverable


def test_numeric_candidates_come_from_the_logic():
    from surveyir.runtime.reconstruct import _candidates

    class View:
        choices = ()
        multiple = None

    class Slider:
        kind = "slider"
        min, max = 0.0, 100.0
        items = ()

    final = type("A", (), {"value": {"4": 59}})()
    got = {
        json.dumps(json.loads(c), sort_keys=True)
        for c in _candidates(Slider(), View(), final, {"25", "75"})
    }
    for v in (0, 24, 25, 26, 74, 75, 76, 100):
        assert json.dumps({"4": v}) in got


def test_an_unexported_loop_order_is_recovered_from_what_it_showed(tmp_path):
    """A randomized loop whose follow-up shows only once round 1 has said yes: the
    export holds no loop order, but which rounds show the follow-up does."""
    follow = {
        "QuestionID": "QID2",
        "QuestionType": "TE",
        "Selector": "SL",
        "DataExportTag": "Q2",
        "QuestionText": "Since you said yes in round 1",
        "DisplayLogic": {
            "0": {
                "0": {
                    "LogicType": "Question",
                    "LeftOperand": "q://1_QID1/SelectableChoice/1",
                    "Operator": "Selected",
                    "Type": "Expression",
                },
                "Type": "If",
            },
            "Type": "BooleanExpression",
        },
    }
    options = {
        "Looping": "Static",
        "LoopingOptions": {
            "Static": {str(i): {"1": f"f{i}"} for i in range(1, 6)},
            "Randomization": "All",
        },
    }
    survey = load_qsf(minimal_qsf([mc("QID1"), follow], block_options=options))
    runs = Simulator(survey, seed=3).run(30, RandomAnswerer(seed=4))
    path = write_responses_csv(survey, runs, tmp_path / "x.csv", options=LAYOUTS["default"])
    replayer = Replayer(survey, fallback_kinds=REPLAY_FALLBACK)

    def paths(recordings):
        return [
            set(replayer.respondent(r.orders, r.answers, earlier=r.earlier).state.displayed)
            for r in recordings
        ]

    truth = [set(run.state.displayed) for run in runs]
    assert paths(read_responses(path, survey, reconstruct=False)) != truth
    assert paths(read_responses(path, survey)) == truth
