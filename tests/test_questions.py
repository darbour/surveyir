"""Question kinds, built from small synthetic QSF payloads."""

from __future__ import annotations

import pytest

from surveyir import LoadError, load_qsf
from surveyir.model import (
    ChoiceQuestion,
    MatrixQuestion,
    RankOrderQuestion,
    SliderQuestion,
    TextEntryQuestion,
    TimingQuestion,
    UnsupportedQuestion,
)
from tests.conftest import mc, minimal_qsf


def load_one(payload: dict, **kw):
    survey = load_qsf(minimal_qsf([payload], **kw))
    return survey, survey.questions[payload["QuestionID"]]


def test_single_choice_with_int_choice_order():
    _, q = load_one(mc(RecodeValues={"1": "1", "2": "0"}))
    assert isinstance(q, ChoiceQuestion)
    assert not q.multiple and q.layout == "vertical"
    assert [c.id for c in q.choices] == ["1", "2"]
    assert [c.recode for c in q.choices] == [1, 0]
    assert q.text.plain == "Pick one"


def test_multiple_choice_text_entry_and_exclusive():
    _, q = load_one(
        mc(
            selector="MAVR",
            Choices={
                "1": {"Display": "A"},
                "2": {"Display": "Other", "TextEntry": "true"},
                "3": {"Display": "None", "ExclusiveAnswer": True},
            },
            ChoiceOrder=["3", "1", "2"],
        )
    )
    assert q.multiple
    assert [c.id for c in q.choices] == ["3", "1", "2"]
    assert [c.text_entry for c in q.choices] == [False, False, True]
    assert q.choices[0].exclusive


def test_choice_order_missing_ids_are_reported():
    survey, q = load_one(mc(ChoiceOrder=["1", "2", "9"]))
    assert [c.id for c in q.choices] == ["1", "2"]
    assert [d.code for d in survey.diagnostics] == ["dangling-order"]


def test_validation_force_and_request():
    _, q = load_one(mc(Validation={"Settings": {"ForceResponse": "ON", "Type": "None"}}))
    assert q.validation.required == "force"
    _, q = load_one(
        mc(
            Validation={
                "Settings": {
                    "ForceResponse": "ON",
                    "ForceResponseType": "RequestResponse",
                    "Type": "None",
                }
            }
        )
    )
    assert q.validation.required == "request"
    _, q = load_one(mc(Validation={"Settings": {"ForceResponse": "OFF", "Type": "None"}}))
    assert q.validation is None


def test_validation_numbers_are_numeric():
    _, q = load_one(
        {
            "QuestionID": "QID1",
            "QuestionType": "TE",
            "Selector": "SL",
            "DataExportTag": "age",
            "QuestionText": "Age?",
            "Validation": {
                "Settings": {
                    "ForceResponse": "ON",
                    "Type": "ContentType",
                    "ContentType": "ValidNumber",
                    "ValidNumber": {"Min": "18", "Max": "99", "NumDecimals": ""},
                }
            },
        }
    )
    assert isinstance(q, TextEntryQuestion) and q.mode == "single_line"
    assert q.validation.content_type == "ValidNumber"
    assert (q.validation.number.min, q.validation.number.max) == (18, 99)


def test_choice_randomization_advanced():
    _, q = load_one(
        mc(
            Randomization={
                "Type": "Advanced",
                "Advanced": {
                    "FixedOrder": ["{~Randomized~}", "{~Randomized~}", "2"],
                    "RandomizeAll": ["1", "3"],
                    "Undisplayed": [],
                    "TotalRandSubset": 0,
                },
            }
        )
    )
    r = q.randomization
    assert r.mode == "advanced" and r.slots == ["*", "*", "2"] and r.randomized == ["1", "3"]


def test_block_randomization_advanced_is_detected():
    survey = load_qsf(
        minimal_qsf(
            [mc("QID1"), mc("QID2")],
            block_options={
                "RandomizeQuestions": "Advanced",
                "Randomization": {
                    "Advanced": {
                        "FixedOrder": ["QID1", "{~Randomized~}"],
                        "RandomizeAll": ["QID2"],
                        "QuestionsPerPage": 0,
                    }
                },
            },
        )
    )
    r = survey.blocks["BL_1"].randomization
    assert r.mode == "advanced" and r.slots == ["QID1", "*"]


def test_matrix_rows_and_columns():
    _, q = load_one(
        {
            "QuestionID": "QID1",
            "QuestionType": "Matrix",
            "Selector": "Likert",
            "SubSelector": "SingleAnswer",
            "DataExportTag": "m",
            "QuestionText": "Rate",
            "Choices": {"1": {"Display": "Row A"}, "2": {"Display": "Row B"}},
            "ChoiceOrder": ["2", "1"],
            "Answers": {"1": {"Display": "Bad"}, "2": {"Display": "Good"}},
            "AnswerOrder": [1, 2],
            "RecodeValues": {"1": "-1", "2": "1"},
            "VariableNaming": {"1": "bad", "2": "good"},
        }
    )
    assert isinstance(q, MatrixQuestion) and q.mode == "single"
    assert [r.id for r in q.rows] == ["2", "1"]
    assert [(a.id, a.recode, a.variable_name) for a in q.columns] == [
        ("1", -1, "bad"),
        ("2", 1, "good"),
    ]


def test_rank_order_is_first_class():
    _, q = load_one(
        {
            "QuestionID": "QID1",
            "QuestionType": "RO",
            "Selector": "DND",
            "SubSelector": "TX",
            "DataExportTag": "rank",
            "QuestionText": "Rank",
            "Choices": {"1": {"Display": "a"}, "2": {"Display": "b"}},
        }
    )
    assert isinstance(q, RankOrderQuestion) and q.mode == "drag_and_drop"


def test_slider_settings_and_labels():
    _, q = load_one(
        {
            "QuestionID": "QID1",
            "QuestionType": "Slider",
            "Selector": "HSLIDER",
            "DataExportTag": "s",
            "QuestionText": "Slide",
            "Choices": {"1": {"Display": "x"}},
            "Labels": {"1": {"Display": "Low"}, "2": {"Display": "High"}},
            "Configuration": {
                "CSSliderMin": 0,
                "CSSliderMax": 100,
                "GridLines": 10,
                "NumDecimals": "0",
                "SnapToGrid": False,
            },
        }
    )
    assert isinstance(q, SliderQuestion)
    assert (q.min, q.max, q.grid_lines, q.labels) == (0, 100, 10, ["Low", "High"])


def test_timing_question():
    _, q = load_one(
        {
            "QuestionID": "QID1",
            "QuestionType": "Timing",
            "Selector": "PageTimer",
            "DataExportTag": "t",
            "QuestionText": "Timing",
            "Configuration": {"MinSeconds": "0", "MaxSeconds": 30},
        }
    )
    assert isinstance(q, TimingQuestion)
    assert (q.min_seconds, q.auto_advance_seconds) == (None, 30)


def test_unknown_type_is_unsupported_not_descriptive():
    payload = {
        "QuestionID": "QID1",
        "QuestionType": "VideoResponse",
        "Selector": "Video",
        "DataExportTag": "h",
        "QuestionText": "Click",
    }
    survey, q = load_one(payload)
    assert isinstance(q, UnsupportedQuestion)
    assert q.extras["QuestionType"] == "VideoResponse"
    assert survey.diagnostics[0].code == "unsupported-question"
    with pytest.raises(LoadError):
        load_qsf(minimal_qsf([payload]), strict=True)


def test_unmapped_keys_are_preserved_in_extras():
    _, q = load_one(
        mc(
            SomeFutureKey={"a": 1},
            Configuration={"QuestionDescriptionOption": "UseText", "NewThing": 2},
        )
    )
    assert q.extras["SomeFutureKey"] == {"a": 1}
    assert q.extras["Configuration"] == {"QuestionDescriptionOption": "UseText", "NewThing": 2}
    assert "QuestionID" not in q.extras and "Choices" not in q.extras


def test_trashed_questions_are_dropped():
    doc = minimal_qsf([mc("QID1")])
    doc["SurveyElements"][0]["Payload"].append(
        {
            "Type": "Trash",
            "ID": "BL_t",
            "BlockElements": [{"Type": "Question", "QuestionID": "QID2"}],
        }
    )
    doc["SurveyElements"].append({"Element": "SQ", "Payload": mc("QID2")})
    assert list(load_qsf(doc).questions) == ["QID1"]


def test_skip_logic():
    survey = load_qsf(
        minimal_qsf(
            [mc("QID1"), mc("QID2")],
            block_elements=[
                {
                    "Type": "Question",
                    "QuestionID": "QID1",
                    "SkipLogic": [
                        {
                            "SkipLogicID": 1,
                            "ChoiceLocator": "q://QID1/SelectableChoice/2",
                            "Condition": "Selected",
                            "QuestionID": "QID1",
                            "SkipToDestination": "ENDOFSURVEY",
                        }
                    ],
                },
                {"Type": "Page Break"},
                {"Type": "Question", "QuestionID": "QID2"},
            ],
        )
    )
    block = survey.blocks["BL_1"]
    assert block.pages() == [["QID1"], ["QID2"]]
    skip = block.elements[0].skip_logic[0]
    assert skip.destination == "end_of_survey"
    assert skip.condition.left.choice_id == "2"


def test_translations():
    _, q = load_one(
        mc(
            Language={
                "ES": {
                    "QuestionText": "Elige",
                    "Choices": {"1": {"Display": "Sí"}, "2": {"Display": "No"}},
                }
            }
        )
    )
    assert q.translations["ES"].text.plain == "Elige"
    assert q.translations["ES"].choices["1"].plain == "Sí"


def test_flow_nodes():
    flow = [
        {
            "Type": "EmbeddedData",
            "FlowID": "FL_2",
            "EmbeddedData": [
                {"Field": "pid", "Type": "Recipient", "Description": "pid"},
                {"Field": "arm", "Type": "Custom", "Value": "1"},
            ],
        },
        {
            "Type": "BlockRandomizer",
            "FlowID": "FL_3",
            "SubSet": "1",
            "EvenPresentation": True,
            "Flow": [
                {"Type": "Block", "ID": "BL_1", "FlowID": "FL_4"},
                {
                    "Type": "Branch",
                    "FlowID": "FL_5",
                    "BranchLogic": {
                        "0": {
                            "0": {
                                "LogicType": "EmbeddedField",
                                "LeftOperand": "arm",
                                "Operator": "EqualTo",
                                "RightOperand": "1",
                                "Type": "Expression",
                            },
                            "Type": "If",
                        },
                        "Type": "BooleanExpression",
                    },
                    "Flow": [{"Type": "EndSurvey", "FlowID": "FL_6"}],
                },
            ],
        },
        {"Type": "Conjoint", "FlowID": "FL_7"},
    ]
    survey = load_qsf(minimal_qsf([mc()], flow=flow))
    types = [n.type for n in survey.walk_flow()]
    assert types == ["embedded_data", "randomizer", "block", "branch", "end_survey", "unsupported"]
    ed = survey.flow[0]
    assert [(f.name, f.source) for f in ed.fields] == [("pid", "recipient"), ("arm", "custom")]
    rand = survey.flow[1]
    assert rand.subset_size == 1 and rand.even_presentation
    assert rand.children[1].condition.right == "1"
    assert any(d.code == "unsupported-flow" for d in survey.diagnostics)


def test_strict_tolerates_source_defects_but_not_gaps():
    doc = {"SurveyElements": [{"Type": "Question", "Payload": mc("QID1")}]}
    survey = load_qsf(doc, strict=True)  # reconstructed: inferred element, implicit block
    assert {"inferred-element", "implicit-block"} <= {d.code for d in survey.diagnostics}
