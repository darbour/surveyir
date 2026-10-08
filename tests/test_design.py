"""design(): nominal shares, nesting, conditions, contrast, order randomization, annotations."""

from __future__ import annotations

import importlib.util
import json

import pytest

from surveyir import load_qsf
from surveyir.runtime import design
from surveyir.runtime.design import FactorAnnotation
from tests.conftest import mc, minimal_qsf


def logic(*exprs: dict) -> dict:
    s: dict = {str(i): e for i, e in enumerate(exprs)}
    s["Type"] = "If"
    return {"0": s, "Type": "BooleanExpression"}


def ed(field: str, op: str, right: str) -> dict:
    return {
        "LogicType": "EmbeddedField",
        "LeftOperand": field,
        "Operator": op,
        "RightOperand": right,
        "Type": "Expression",
    }


def setter(flow_id: str, field: str, value: str) -> dict:
    return {
        "Type": "EmbeddedData",
        "FlowID": flow_id,
        "EmbeddedData": [{"Field": field, "Type": "Custom", "Value": value}],
    }


def randomizer(flow_id: str, arms: list[dict], subset: int = 1, even: bool = True) -> dict:
    return {
        "Type": "BlockRandomizer",
        "FlowID": flow_id,
        "SubSet": subset,
        "EvenPresentation": even,
        "Flow": arms,
    }


def group(flow_id: str, *children: dict) -> dict:
    return {"Type": "Group", "FlowID": flow_id, "Description": flow_id, "Flow": list(children)}


def nested_survey():
    """FL_2 (1 of 2) with arm FL_3 holding FL_5 (1 of 3), plus FL_9 (1 of 2) under a branch."""
    inner = randomizer(
        "FL_5", [setter("FL_6", "src", "a"), setter("FL_7", "src", "b"), setter("FL_8", "src", "c")]
    )
    flow = [
        randomizer(
            "FL_2",
            [
                group("FL_3", setter("FL_30", "frame", "gain"), inner),
                group("FL_4", setter("FL_40", "frame", "loss")),
            ],
        ),
        {
            "Type": "Branch",
            "FlowID": "FL_10",
            "BranchLogic": logic(ed("frame", "EqualTo", "gain")),
            "Flow": [randomizer("FL_9", [setter("FL_11", "x", "1"), setter("FL_12", "x", "2")])],
        },
        {"Type": "Block", "ID": "BL_1", "FlowID": "FL_20"},
    ]
    return load_qsf(minimal_qsf([mc()], flow=flow))


def last_shown_survey():
    flow = [
        randomizer("FL_2", [setter("FL_3", "Group", "A"), setter("FL_4", "Group", "B")], subset=2),
        {"Type": "Block", "ID": "BL_1", "FlowID": "FL_5"},
    ]
    return load_qsf(minimal_qsf([mc()], flow=flow))


def test_nominal_share_and_marginal_for_nested_randomizers():
    d = design(nested_survey())
    outer, inner, _ = d.factors
    assert [f.id for f in d.factors] == ["FL_2", "FL_5", "FL_9"]
    assert [a.nominal_share for a in outer.arms] == pytest.approx([0.5, 0.5])
    assert [a.nominal_marginal for a in outer.arms] == pytest.approx([0.5, 0.5])
    assert [a.nominal_share for a in inner.arms] == pytest.approx([1 / 3] * 3)
    assert [a.nominal_marginal for a in inner.arms] == pytest.approx([1 / 6] * 3)
    assert outer.arms[0].assignments == {"frame": "gain"}  # nested randomizer's fields excluded
    assert [a.assignments for a in inner.arms] == [{"src": "a"}, {"src": "b"}, {"src": "c"}]
    assert "nominal marginal 0.167" in d.summary()


def test_branch_conditions_are_excluded_from_nominal_marginal():
    branched = design(nested_survey()).factors[2]
    assert branched.condition is not None and branched.within == []
    # only respondents assigned "gain" reach FL_9, but the nominal marginal ignores that
    assert [a.nominal_marginal for a in branched.arms] == pytest.approx([0.5, 0.5])
    assert [a.nominal_share for a in branched.arms] == [a.nominal_marginal for a in branched.arms]


def test_within_paths_and_condition_text():
    d = design(nested_survey())
    outer, inner, branched = d.factors
    assert outer.within == [] and outer.condition is None
    assert inner.within == [f"FL_2:{outer.arms[0].key}"] and inner.condition is None
    assert "frame" in (branched.condition or "") and "gain" in (branched.condition or "")
    assert f"if {branched.condition}" in d.summary()
    assert [[f.id for f in g] for g in d.crossed()] == [["FL_2"], ["FL_5"], ["FL_9"]]


def test_order_randomization_entries():
    q = mc("QID1", Randomization={"Type": "All"})
    doc = minimal_qsf(
        [q, mc("QID2")],
        block_options={
            "RandomizeQuestions": "RandomWithOnlyX",
            "Randomization": {"Advanced": {"TotalRandSubset": 1}, "EvenPresentation": True},
            "Looping": "Static",
            "LoopingOptions": {
                "Static": {"1": {"1": "apples"}, "2": {"1": "pears"}},
                "Randomization": "All",
            },
        },
    )
    d = design(load_qsf(doc))
    by_kind = {o.kind: o for o in d.order}
    assert set(by_kind) == {"block_questions", "loop", "choices"}
    block = by_kind["block_questions"]
    assert (block.location, block.mode, block.subset_size) == ("BL_1", "subset", 1)
    assert block.even_presentation
    assert (by_kind["loop"].location, by_kind["loop"].mode) == ("BL_1", "all")
    assert (by_kind["choices"].location, by_kind["choices"].mode) == ("QID1", "all")
    assert d.factors == [] and d.cells() == ([{}], True)
    assert "choices randomized at QID1 (all)" in d.summary()


def test_cells_incomplete_for_nested_conditional_and_multi_arm_factors():
    cells, complete = design(nested_survey()).cells()
    assert not complete and cells == [{"FL_2": "FL_3"}, {"FL_2": "FL_4"}]

    branch_only = [
        {
            "Type": "Branch",
            "FlowID": "FL_10",
            "BranchLogic": logic(ed("x", "EqualTo", "1")),
            "Flow": [randomizer("FL_9", [setter("FL_11", "y", "1"), setter("FL_12", "y", "2")])],
        }
    ]
    cells, complete = design(load_qsf(minimal_qsf([mc()], flow=branch_only))).cells()
    assert cells == [{}] and not complete

    two_of_three = [
        randomizer(
            "FL_2",
            [setter("FL_3", "a", "1"), setter("FL_4", "b", "1"), setter("FL_5", "c", "1")],
            subset=2,
        )
    ]
    d = design(load_qsf(minimal_qsf([mc()], flow=two_of_three)))
    assert d.factors[0].k == 2 and d.factors[0].contrast == "exposure"
    assert d.cells() == ([{}], False)


def test_cells_limit():
    flow = [
        randomizer(f"FL_{i}0", [setter(f"FL_{i}1", f"f{i}", "a"), setter(f"FL_{i}2", f"f{i}", "b")])
        for i in range(1, 5)
    ]
    d = design(load_qsf(minimal_qsf([mc()], flow=flow)))
    assert d.cells() == (d.cells(limit=16)[0], True) and len(d.cells()[0]) == 16
    cells, complete = d.cells(limit=5)
    assert len(cells) == 5 and not complete


def test_last_shown_factor_whose_arms_display_nothing_is_an_assignment():
    """Privacy's design: the arms only set Group; later branches on Group show one block."""
    (f,) = design(last_shown_survey()).factors
    assert (f.k, f.n) == (2, 2)
    assert f.contrast == "assignment"
    assert f.recorded_field == f.last_shown_assigns == ["Group"]
    assert f.between_subjects  # kept for doublecast: the recorded field is 1-of-n
    assert [a.nominal_share for a in f.arms] == [1.0, 1.0]
    first = design(last_shown_survey()).summary().splitlines()[0]
    assert first == (
        "FL_2: assignment to Group (arms display nothing; all run, the last one wins: each "
        "value with nominal share 1/2), 2 of 2, evenly presented"
    )


def test_last_shown_factor_whose_arms_display_blocks_is_an_order_contrast():
    """Every respondent sees both stimuli; Group only records which came last."""

    def arm(flow_id: str, value: str) -> dict:
        return {
            "Type": "Group",
            "FlowID": flow_id,
            "Description": value,
            "Flow": [
                setter(f"{flow_id}a", "Group", value),
                {"Type": "Block", "ID": "BL_1", "FlowID": f"{flow_id}b"},
            ],
        }

    flow = [randomizer("FL_2", [arm("FL_3", "A"), arm("FL_4", "B")], subset=2)]
    (f,) = design(load_qsf(minimal_qsf([mc()], flow=flow))).factors
    assert f.contrast == "order" and f.recorded_field == ["Group"]
    assert (
        "order contrast; field Group records the last arm shown"
        in design(load_qsf(minimal_qsf([mc()], flow=flow))).summary()
    )


def test_order_exposure_and_assignment_contrasts():
    def shows(flow_id: str) -> dict:
        return {"Type": "Block", "ID": "BL_1", "FlowID": flow_id}

    flow = [
        randomizer("FL_2", [setter("FL_3", "a", "1"), setter("FL_4", "b", "1")], subset=2),
        randomizer("FL_5", [shows("FL_6"), shows("FL_7")]),
        randomizer("FL_8", [setter("FL_9", "c", "1"), setter("FL_10", "c", "2")]),
    ]
    order, exposure, assignment = design(load_qsf(minimal_qsf([mc()], flow=flow))).factors
    assert order.contrast == "order" and order.recorded_field == [] and not order.between_subjects
    assert exposure.contrast == "exposure" and exposure.recorded_field == []
    # 1 of 2 arms that display nothing and set c: later logic on c decides what is shown
    assert assignment.contrast == "assignment" and assignment.recorded_field == ["c"]
    assert assignment.between_subjects
    dumped = order.model_dump()
    assert dumped["contrast"] == "order" and dumped["recorded_field"] == []


def test_annotations_from_dict_are_stored_and_summarized():
    notes = {
        "FL_2": {"contrast": "exposure", "treatment": "gain vs loss frame", "note": "primary"},
        "FL_9": FactorAnnotation(contrast="order"),
    }
    d = design(nested_survey(), annotations=notes)
    by_id = {f.id: f for f in d.factors}
    assert by_id["FL_2"].annotation == FactorAnnotation(
        contrast="exposure", treatment="gain vs loss frame", note="primary"
    )
    assert by_id["FL_5"].annotation is None
    lines = d.summary().splitlines()
    assert lines[1] == (
        "  declared: exposure contrast (the structure gives assignment); "
        "treatment: gain vs loss frame; note: primary"
    )
    assert "  declared: order contrast (the structure gives assignment)" in lines


def test_annotations_from_json_file(tmp_path):
    path = tmp_path / "notes.json"
    path.write_text(json.dumps({"FL_2": {"contrast": "order", "note": "last shown"}}))
    (f,) = design(last_shown_survey(), annotations=path).factors
    assert f.annotation == FactorAnnotation(contrast="order", note="last shown")
    assert design(last_shown_survey(), annotations=str(path)).factors[0].annotation is not None


def test_annotations_from_yaml_file(tmp_path):
    path = tmp_path / "notes.yaml"
    path.write_text("FL_2:\n  contrast: exposure\n  treatment: last arm\n")
    if importlib.util.find_spec("yaml") is None:
        with pytest.raises(ImportError, match="PyYAML"):
            design(last_shown_survey(), annotations=path)
        return
    (f,) = design(last_shown_survey(), annotations=path).factors
    assert f.annotation == FactorAnnotation(contrast="exposure", treatment="last arm")


def test_annotations_reject_unknown_factors_and_fields():
    with pytest.raises(ValueError, match="unknown factors"):
        design(last_shown_survey(), annotations={"FL_99": {"contrast": "order"}})
    with pytest.raises(ValueError):
        design(last_shown_survey(), annotations={"FL_2": {"contrast": "between"}})
    with pytest.raises(ValueError):
        design(last_shown_survey(), annotations={"FL_2": {"typo": "x"}})
