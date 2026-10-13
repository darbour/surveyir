"""Reading real exports into replay recordings (``surveyir.read_responses``).

Simulated respondents are exported as Qualtrics exports them (``runtime.rows``),
read back, and replayed: every decision and answer the export records must come
back, and every one it does not must be reconstructed correctly or reported.
"""

from __future__ import annotations

import csv
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
            if got is not None and truth is not None:
                assert set(got) == set(truth), fid
                assert ("flow", fid, None) in rec.inferred
            elif truth is not None:
                assert any(f"flow {fid}:" in n for n in rec.unrecoverable), fid


def test_arms_that_only_set_embedded_data_are_recovered(simulated, tmp_path):
    """targeting_fairness: both arms show the same blocks; the arm sets embedded data
    that display logic reads. The answers still say which arm it was."""
    survey, runs = simulated(FIXTURES / "qualtrics" / "targeting_fairness.qsf")
    options = ColumnOptions(display_order="none", include_metadata=True)
    for run, rec in zip(runs, _export(survey, runs, tmp_path, options), strict=True):
        assert rec.orders[("flow", "FL_491", None)] == tuple(run.flow_order["FL_491"])


def test_inner_decisions_follow_whichever_outer_arm_is_drawn(simulated, tmp_path):
    """default_eric: the outer randomizer only orders two inner randomizers, which
    hold the same blocks; without display order the outer one cannot be told, but
    the inner conditions (opt-in or opt-out) can, under either outer arm."""
    survey, runs = simulated(FIXTURES / "qualtrics" / "default_eric.qsf")
    options = ColumnOptions(display_order="none", include_metadata=True)
    for run, rec in zip(runs, _export(survey, runs, tmp_path, options), strict=True):
        assert ("flow", "FL_344", None) not in rec.orders
        assert any("FL_344" in n for n in rec.unrecoverable)
        # the inner decisions are kept for whichever outer arm is drawn, and marked:
        # the respondent reached only one of the two inner randomizers of each kind
        assert rec.conditional and set(rec.conditional) <= set(rec.orders)
        inner = [v for f, v in run.flow_order.items() if f != "FL_344"]
        for _ in range(3):  # whichever outer arm a replay draws
            again = Replayer(survey, strict=False, fallback_kinds=("flow", *REPLAY_FALLBACK))
            replayed = again.respondent(rec.orders, rec.answers, embedded=rec.embedded, seed=_)
            assert sorted(v for f, v in replayed.flow_order.items() if f != "FL_344") == sorted(
                inner
            )


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
