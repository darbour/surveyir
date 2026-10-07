"""Question types, flow elements and quirks found in the wider public QSF corpus.

Each payload here is a trimmed copy of the shape seen in real exports.
"""

from __future__ import annotations

from surveyir import ColumnOptions, export, load_qsf, response_columns
from surveyir.model import (
    DrillDownQuestion,
    FileUploadQuestion,
    GraphicSliderQuestion,
    HeatMapQuestion,
    HighlightQuestion,
    HotSpotQuestion,
    LibraryBlockNode,
    MatrixQuestion,
    PickGroupRankQuestion,
    QuotaNode,
    RandomizerNode,
    SideBySideQuestion,
    SignatureQuestion,
    SliderQuestion,
    WebServiceNode,
)
from tests.conftest import mc, minimal_qsf


def one(payload: dict, **kw):
    survey = load_qsf(minimal_qsf([payload], **kw))
    return survey, survey.questions[payload["QuestionID"]]


def cols(survey, **options):
    return [
        (c.name, c.import_id)
        for c in response_columns(survey, include_embedded=False, options=ColumnOptions(**options))
    ]


# ------------------------------------------------------------------ question kinds


def test_side_by_side():
    payload = {
        "QuestionID": "QID5",
        "QuestionType": "SBS",
        "Selector": "SBSMatrix",
        "DataExportTag": "SBS",
        "QuestionText": "Rate",
        "Choices": {"1": {"Display": "Row 1"}, "2": {"Display": "Row 2"}},
        "ChoiceOrder": ["1", "2"],
        "NumberOfQuestions": 2,
        "AdditionalQuestions": {
            "1": {
                "QuestionText": "Col 1",
                "QuestionType": "Matrix",
                "Selector": "Likert",
                "SubSelector": "SingleAnswer",
                "DataExportTag": "SBS#1",
                "QuestionID": "QID5#1",
                "Choices": {"1": {"Display": "Row 1"}, "2": {"Display": "Row 2"}},
                "Answers": {"1": {"Display": "Yes"}, "2": {"Display": "No"}},
            },
            "2": {
                "QuestionText": "Col 2",
                "QuestionType": "Matrix",
                "Selector": "TE",
                "SubSelector": "Medium",
                "DataExportTag": "SBS#2",
                "QuestionID": "QID5#2",
                "Choices": {"1": {"Display": "Row 1"}, "2": {"Display": "Row 2"}},
                "Answers": {"1": {"Display": "Why"}},
            },
        },
    }
    survey, q = one(payload)
    assert isinstance(q, SideBySideQuestion)
    assert [(s.id, s.kind, s.mode) for s in q.questions] == [
        ("QID5#1", "matrix", "single"),
        ("QID5#2", "matrix", "text"),
    ]
    assert cols(survey) == [
        ("SBS#1_1", "QID5#1_1"),
        ("SBS#1_2", "QID5#1_2"),
        ("SBS#2_1_1", "QID5#2_1_1"),
        ("SBS#2_2_1", "QID5#2_2_1"),
    ]
    assert "Column 1 (single): Col 1" in export(survey, "markdown")


def test_drill_down():
    survey, q = one(
        {
            "QuestionID": "QID24",
            "QuestionType": "DD",
            "Selector": "DL",
            "DataExportTag": "country",
            "QuestionText": "Country?",
            "Choices": {"1": {"Display": "Please choose..."}},
            "ChoiceOrder": [1],
            "Answers": {"1": {"Display": "Afghanistan"}, "2": {"Display": "Albania"}},
            "AnswerMap": {"1": None, "2": None},
        }
    )
    assert isinstance(q, DrillDownQuestion)
    assert [o.text.plain for o in q.options] == ["Afghanistan", "Albania"]
    assert cols(survey) == [("country_1", "QID24_1")]


def test_pick_group_rank():
    survey, q = one(
        {
            "QuestionID": "QID1",
            "QuestionType": "PGR",
            "Selector": "DragAndDrop",
            "SubSelector": "Columns",
            "DataExportTag": "Q1",
            "QuestionText": "Sort",
            "Choices": {"1": {"Display": "a"}, "2": {"Display": "b"}},
            "ChoiceOrder": ["1", "2"],
            "Groups": ["Cart"],
            "NumberOfGroups": 1,
        }
    )
    assert isinstance(q, PickGroupRankQuestion) and q.groups == ["Cart"] and q.columns
    assert cols(survey) == [
        ("Q1_0_GROUP_1", "QID1_0_GROUP"),
        ("Q1_0_GROUP_2", "QID1_0_GROUP"),
        ("Q1_0_1_RANK", "QID1_G0_1_RANK"),
        ("Q1_0_2_RANK", "QID1_G0_2_RANK"),
    ]


def test_highlight():
    _, q = one(
        {
            "QuestionID": "QID9",
            "QuestionType": "HL",
            "Selector": "Text",
            "DataExportTag": "hl",
            "QuestionText": "Highlight",
            "Choices": {
                "55": {"WordIndex": 0, "WordLength": 4, "Word": "This", "Display": "1: This"}
            },
            "Answers": {"1": {"Display": "Like", "BGColor": "#1a9641"}},
            "HighlightText": "This is text",
            "ColorScale": "RdYlGn",
            "ExcludedWords": [],
        }
    )
    assert isinstance(q, HighlightQuestion)
    assert q.passage == "This is text" and q.words[0].text.plain == "This"
    assert q.categories[0].text.plain == "Like"


def test_hot_spot_and_heat_map():
    _, hs = one(
        {
            "QuestionID": "QID2",
            "QuestionType": "HotSpot",
            "Selector": "LikeDislike",
            "DataExportTag": "hs",
            "QuestionText": "Click",
            "GraphicID": "IM_1",
            "Choices": {"4": {"Display": "Left"}},
            "ChoiceOrder": ["4"],
            "Regions": [
                {
                    "Type": "Polygon",
                    "ChoiceID": "4",
                    "Description": "Region #1",
                    "Shapes": [[{"X": 1, "Y": 1}, {"X": 3, "Y": 1}, {"X": 3, "Y": 4}]],
                    "X": 1,
                    "Y": 1,
                    "Width": 2,
                    "Height": 3,
                }
            ],
            "Answers": {"1": {"Display": "Dislike"}, "3": {"Display": "Like"}},
        }
    )
    assert isinstance(hs, HotSpotQuestion) and hs.mode == "like_dislike"
    assert hs.regions[0].label == "Left" and hs.regions[0].points[2] == (3.0, 4.0)
    assert [s.text.plain for s in hs.states] == ["Dislike", "Like"]

    _, hm = one(
        {
            "QuestionID": "QID8",
            "QuestionType": "HeatMap",
            "Selector": "Image",
            "DataExportTag": "hm",
            "QuestionText": "Spot it",
            "GraphicID": "IM_2",
            "Clicks": 1,
            "Regions": [{"Description": "Region #1", "Type": "Polygon", "Shapes": [[]]}],
            "Choices": {"1": {"Display": "X,Y"}},
        }
    )
    assert isinstance(hm, HeatMapQuestion) and hm.max_clicks == 1 and hm.image.id == "IM_2"


def test_graphic_slider_with_list_choices():
    # PHP-style arrays: ids are positions starting at 0.
    _, q = one(
        {
            "QuestionID": "QID3",
            "QuestionType": "SS",
            "Selector": "TA",
            "DataExportTag": "g",
            "QuestionText": "Rate",
            "Category": "Gauges",
            "Scale": "TenGauge",
            "Choices": [{"Display": "0"}, {"Display": 1}, {"Display": 2}],
        }
    )
    assert isinstance(q, GraphicSliderQuestion)
    assert [(c.id, c.text.plain) for c in q.points] == [("0", "0"), ("1", "1"), ("2", "2")]


def test_file_upload_and_signature():
    survey, fu = one(
        {
            "QuestionID": "QID40",
            "QuestionType": "FileUpload",
            "Selector": "FileUpload",
            "DataExportTag": "Q40",
            "QuestionText": "Upload",
            "Validation": {
                "Settings": {"ForceResponse": "ON", "Type": "ContentType", "ContentType": "Graphic"}
            },
            "ScreenCaptureText": "Capture Screen",
        }
    )
    assert isinstance(fu, FileUploadQuestion) and fu.validation.file_type == "Graphic"
    assert cols(survey) == [
        ("Q40_Id", "QID40_FILE_ID"),
        ("Q40_Name", "QID40_FILE_NAME"),
        ("Q40_Size", "QID40_FILE_SIZE"),
        ("Q40_Type", "QID40_FILE_TYPE"),
    ]
    _, sig = one(
        {
            "QuestionID": "QID9",
            "QuestionType": "Draw",
            "Selector": "Signature",
            "DataExportTag": "sig",
            "QuestionText": "Sign",
        }
    )
    assert isinstance(sig, SignatureQuestion)


def test_profile_matrix_has_per_row_scales():
    _, q = one(
        {
            "QuestionID": "QID12",
            "QuestionType": "Matrix",
            "Selector": "Profile",
            "SubSelector": "SingleAnswer",
            "DataExportTag": "p",
            "QuestionText": "Pick",
            "Choices": {"1": {"Display": "A"}, "2": {"Display": "B"}},
            "Answers": {
                "1": {"1": {"Display": "a1"}, "2": {"Display": "a2"}},
                "2": {"1": {"Display": "b1"}},
            },
            "AnswerOrder": [1, 2],
        }
    )
    assert isinstance(q, MatrixQuestion) and q.mode == "profile"
    assert [a.text.plain for a in q.row_columns["1"]] == ["a1", "a2"]
    assert [a.text.plain for a in q.row_columns["2"]] == ["b1"]


def test_slider_details():
    survey, q = one(
        {
            "QuestionID": "QID24",
            "QuestionType": "Slider",
            "Selector": "HSLIDER",
            "DataExportTag": "s",
            "QuestionText": "How much",
            "Choices": {"1": {"Display": "a"}, "14": {"Display": "b"}, "4": {"Display": "c"}},
            "ChoiceOrder": [1, "14", "4"],
            "Configuration": {
                "CSSliderMin": 0,
                "CSSliderMax": 100,
                "CustomStart": True,
                "SliderStartPositions": {"1": 0.5},
                "NotApplicable": True,
                "NotApplicableText": "Don't Know",
                "ShowValue": True,
            },
        }
    )
    assert isinstance(q, SliderQuestion)
    assert q.start_positions == {"1": 0.5} and q.not_applicable == "Don't Know"
    assert cols(survey) == [("s_1", "QID24_1"), ("s_14", "QID24_14"), ("s_4", "QID24_4")]
    assert cols(survey, slider_naming="position") == [
        ("s_1", "QID24_1"),
        ("s_2", "QID24_14"),
        ("s_3", "QID24_4"),
    ]


# ------------------------------------------------------------------ choices & logic


def test_randomization_variants():
    _, q = one(mc(Randomization={"Type": "SubSet", "TotalRandSubset": 1}))
    assert q.randomization.mode == "subset" and q.randomization.subset_size == 1
    _, q = one(mc(Randomization={"Type": "ScaleReversal"}))
    assert q.randomization.mode == "none" and q.randomization.flip_scale


def test_block_randomization_variants():
    for options, expected in [
        (
            {
                "RandomizeQuestions": "RandomWithOnlyX",
                "Randomization": {"Advanced": {"TotalRandSubset": "1", "QuestionsPerPage": 0}},
            },
            ("subset", 1, None),
        ),
        (
            {
                "RandomizeQuestions": "RandomWithXPerPage",
                "Randomization": {"Advanced": {"QuestionsPerPage": 2}},
            },
            ("all", None, 2),
        ),
        ({"RandomizeQuestions": "RandomizeAll"}, ("all", None, None)),
    ]:
        survey = load_qsf(minimal_qsf([mc("QID1"), mc("QID2")], block_options=options))
        r = survey.blocks["BL_1"].randomization
        assert (r.mode, r.subset_size, r.per_page) == expected


def test_carry_forward_variants_and_columns():
    src = mc("QID1", selector="MAVR")
    dst = mc(
        "QID2",
        selector="MAVR",
        Choices={},
        ChoiceOrder=[],
        DynamicChoices={
            "DynamicType": "ChoiceGroup",
            "Type": "Dynamic",
            "Locator": "q://QID1/ChoiceGroup/AllChoices?displayLogic=0",
        },
    )
    survey = load_qsf(minimal_qsf([src, dst]))
    cf = survey.questions["QID2"].carry_forward
    assert (cf.source, cf.question_id, cf.mode, cf.options) == (
        "question",
        "QID1",
        "AllChoices",
        {"displayLogic": "0"},
    )
    assert ("Q2_x1", "QID2") in cols(survey)

    _, q = one(
        mc(
            DynamicChoices={
                "DynamicType": "ReferenceList",
                "Type": "Dynamic",
                "Locator": "refl://REFL_abc/ActiveSelections",
            }
        )
    )
    assert (q.carry_forward.source, q.carry_forward.list_id) == ("reference_list", "REFL_abc")


def test_choice_text_entry_options_and_exclusive_columns():
    _, q = one(
        mc(
            Choices={
                "1": {
                    "Display": "Other",
                    "TextEntry": "true",
                    "TextEntryForceResponse": True,
                    "TextEntryValidation": "ValidNumber",
                }
            },
            ChoiceOrder=["1"],
        )
    )
    c = q.choices[0]
    assert c.text_entry and c.text_entry_required and c.text_entry_validation == "ValidNumber"


def test_validation_types_ignore_stale_settings():
    _, q = one(
        mc(
            selector="MAVR",
            Validation={
                "Settings": {
                    "ForceResponse": "ON",
                    "Type": "ChoiceRange",
                    "MinChoices": "1",
                    "MaxChoices": "3",
                    "MinChars": "99",
                }
            },
        )
    )
    v = q.validation
    assert (v.min_choices, v.max_choices, v.min_chars) == (1, 3, None)


def test_skip_logic_value_and_quota_operators():
    survey = load_qsf(
        minimal_qsf(
            [mc("QID1")],
            block_elements=[
                {
                    "Type": "Question",
                    "QuestionID": "QID1",
                    "SkipLogic": [
                        {
                            "ChoiceLocator": "q://QID1/ChoiceTextEntryValue/1",
                            "Condition": "EqualTo",
                            "Value": "MSIE",
                            "QuestionID": "QID1",
                            "SkipToDestination": "ENDOFSURVEY",
                        }
                    ],
                }
            ],
        )
    )
    skip = survey.blocks["BL_1"].elements[0].skip_logic[0]
    assert (skip.condition.operator, skip.condition.right) == ("equal", "MSIE")


def test_side_by_side_locator_is_decoded():
    flow = [
        {
            "Type": "Branch",
            "FlowID": "FL_2",
            "BranchLogic": {
                "0": {
                    "0": {
                        "LogicType": "Question",
                        "LeftOperand": "q://QID5%232/SelectableChoice/1/2",
                        "Operator": "Selected",
                        "Type": "Expression",
                    },
                    "Type": "If",
                },
                "Type": "BooleanExpression",
            },
            "Flow": [],
        },
        {"Type": "Block", "ID": "BL_1", "FlowID": "FL_3"},
    ]
    survey = load_qsf(minimal_qsf([mc()], flow=flow))
    left = survey.flow[0].condition.left
    assert (left.question_id, left.choice_id, left.answer_id) == ("QID5#2", "1", "2")


def test_invalid_logic_is_reported():
    flow = [
        {
            "Type": "Branch",
            "FlowID": "FL_2",
            "BranchLogic": {
                "0": {
                    "0": {
                        "LogicType": "Question",
                        "LeftOperand": "q://undefined/undefined",
                        "Type": "Expression",
                    },
                    "Type": "If",
                },
                "Type": "BooleanExpression",
            },
            "Flow": [],
        }
    ]
    survey = load_qsf(minimal_qsf([mc()], flow=flow))
    assert "invalid-logic" in {d.code for d in survey.diagnostics}


# ------------------------------------------------------------------ flow & survey


def test_flow_extras_library_quota_legacy_randomizer_web_service():
    flow = [
        {"Type": "ReferenceSurvey", "ID": "LS_1", "FlowID": "FL_2", "LibraryID": "UR_1"},
        {"Type": "Quota", "FlowID": "FL_3", "Description": "Region quota"},
        {
            "Type": "Randomizer",
            "FlowID": "FL_4",
            "SubSet": 1,
            "EvenPresentation": True,
            "RandomizerOptions": {"Randomization": "Evenly"},
            "Flow": [{"Type": "Block", "ID": "BL_1", "FlowID": "FL_5"}],
        },
        {
            "Type": "WebService",
            "FlowID": "FL_6",
            "URL": "https://x",
            "Method": "GET",
            "RequestParams": [{"key": "min", "value": "0"}],
            "ResponseMap": [{"key": "random", "value": "MTurkCode"}],
        },
    ]
    survey = load_qsf(minimal_qsf([mc()], flow=flow))
    lib, quota, rand, ws = survey.flow
    assert isinstance(lib, LibraryBlockNode) and lib.reference_id == "LS_1"
    assert isinstance(quota, QuotaNode)
    assert isinstance(rand, RandomizerNode) and rand.subset_size == 1
    assert isinstance(ws, WebServiceNode)
    assert ws.request_params == {"min": "0"} and ws.sets_fields == ["MTurkCode"]


def test_quotas():
    doc = minimal_qsf([mc()])
    doc["SurveyElements"].append(
        {
            "Element": "QO",
            "Payload": {
                "ID": "QO_1",
                "Name": "women",
                "Occurrences": 50,
                "QuotaAction": "EndCurrentSurvey",
                "Logic": {
                    "0": {
                        "0": {
                            "LogicType": "Question",
                            "LeftOperand": "q://QID1/SelectableChoice/2",
                            "Operator": "Selected",
                            "Type": "Expression",
                        },
                        "Type": "If",
                    },
                    "Type": "BooleanExpression",
                },
            },
        }
    )
    quota = load_qsf(doc).quotas[0]
    assert (quota.name, quota.limit, quota.condition.left.choice_id) == ("women", 50, "2")


def test_one_block_per_element_and_list_valued_loops():
    doc = minimal_qsf([mc("QID1"), mc("QID2")])
    doc["SurveyElements"][0]["Payload"] = {
        "Type": "Standard",
        "ID": "BL_1",
        "Description": "A",
        "BlockElements": [{"Type": "Question", "QuestionID": "QID1"}],
        "Options": {
            "Looping": "Question",
            "LoopingOptions": {
                "QID": "QID9",
                "Locator": "q://QID9/LoopAndMerge/MergeOnNumericResponse?v=3",
                "Static": {"1": [], "2": [], "3": []},
                "Randomization": "None",
            },
        },
    }
    doc["SurveyElements"].insert(
        1,
        {
            "Element": "BL",
            "Payload": {
                "Type": "Standard",
                "ID": "BL_2",
                "Description": "B",
                "BlockElements": [{"Type": "Question", "QuestionID": "QID2"}],
            },
        },
    )
    survey = load_qsf(doc)
    assert list(survey.blocks) == ["BL_1", "BL_2"]
    assert list(survey.blocks["BL_1"].loop.fields) == ["1", "2", "3"]


def test_untagged_elements_and_missing_blocks_are_recovered():
    doc = {
        "SurveyElements": [
            {"Type": "Question", "Payload": mc("QID1")},
            {"Type": "Question", "Payload": mc("QID2")},
        ]
    }
    survey = load_qsf(doc)
    assert list(survey.questions) == ["QID1", "QID2"]
    codes = {d.code for d in survey.diagnostics}
    assert {"inferred-element", "implicit-block", "no-flow"} <= codes


def test_iter_questions_tolerates_flow_to_missing_block():
    flow = [
        {"Type": "Block", "ID": "BL_missing", "FlowID": "FL_2"},
        {"Type": "Block", "ID": "BL_1", "FlowID": "FL_3"},
    ]
    survey = load_qsf(minimal_qsf([mc()], flow=flow))
    assert [q.id for q in survey.iter_questions()] == ["QID1"]


def test_column_options_single_display_order_and_unsplit_multi():
    survey = load_qsf(minimal_qsf([mc("QID1", selector="MAVR", Randomization={"Type": "All"})]))
    assert cols(survey, split_multi_value=False, display_order="single") == [
        ("Q1", "QID1"),
        ("Q1_DO", "QID1_DO"),
    ]


# ------------------------------------------------------------------ known codes without examples


def _bare(qtype, selector=None, sub=None, **extra):
    payload = {
        "QuestionID": "QID1",
        "QuestionType": qtype,
        "DataExportTag": "q",
        "QuestionText": "Q",
    }
    if selector:
        payload["Selector"] = selector
    if sub:
        payload["SubSelector"] = sub
    payload.update(extra)
    return payload


def test_codes_without_public_examples_load_as_unsupported_with_probable_type():
    cases = [
        (_bare("TE", "CalendarSingle"), "Calendar"),
        (_bare("FileUpload", "ScreenCapture", ScreenCaptureText="Capture"), "Screen Capture"),
        (_bare("FileUpload", "VideoCapture", "VideoToText"), "Video Response"),
        (_bare("TreeSelect", "SearchOnly"), "Org Hierarchy"),
        (_bare("TE", "AUTO", "SDS"), "Location Selector"),
    ]
    for payload, name in cases:
        survey, q = one(payload)
        assert q.kind == "unsupported"
        assert q.extras == payload  # verbatim, nothing dropped
        assert name in survey.diagnostics[0].message


def test_question_type_plugins_are_detected():
    payload = _bare(
        "TE", "SL", QuestionTypePluginProperties={"ID": "@qualtrics/arcgis-map", "Version": "1.0.0"}
    )
    survey, q = one(payload)
    assert q.kind == "unsupported" and q.extras["QuestionTypePluginProperties"]["ID"]
    assert survey.diagnostics[0].code == "plugin-question"


def test_unknown_text_selector_warns_and_empty_selector_defaults():
    survey, q = one(_bare("TE", "Mystery"))
    assert q.mode == "other" and survey.diagnostics[0].code == "unknown-selector"
    survey, q = one(_bare("TE"))
    assert q.mode == "single_line" and survey.diagnostics[0].level == "info"


def test_screen_capture_text_is_preserved_on_plain_uploads():
    _, q = one(_bare("FileUpload", "FileUpload", ScreenCaptureText="Capture Screen"))
    assert q.extras["ScreenCaptureText"] == "Capture Screen"


# ------------------------------------------------------------------ specialty columns (vendor docs)


def objs(survey, **options):
    return [
        (c.name, c.import_object, c.evidence)
        for c in response_columns(survey, include_embedded=False, options=ColumnOptions(**options))
    ]


def test_signature_exports_like_file_upload():
    survey, _ = one(
        {
            "QuestionID": "QID13",
            "QuestionType": "Draw",
            "Selector": "Signature",
            "DataExportTag": "Q10",
            "QuestionText": "Sign",
        }
    )
    assert cols(survey) == [
        ("Q10_Id", "QID13_FILE_ID"),
        ("Q10_Name", "QID13_FILE_NAME"),
        ("Q10_Size", "QID13_FILE_SIZE"),
        ("Q10_Type", "QID13_FILE_TYPE"),
    ]


def test_heat_map_columns():
    survey, _ = one(
        {
            "QuestionID": "QID7",
            "QuestionType": "HeatMap",
            "Selector": "Image",
            "DataExportTag": "Q1",
            "QuestionText": "Click",
            "Clicks": 2,
            "Regions": [{"Description": "Region #1", "Type": "Polygon", "Shapes": [[]]}],
        }
    )
    assert [(n, o) for n, o, _ in objs(survey)] == [
        ("Q1_1_x", {"ImportId": "QID7", "point": 1, "coord": "x"}),
        ("Q1_1_y", {"ImportId": "QID7", "point": 1, "coord": "y"}),
        ("Q1_2_x", {"ImportId": "QID7", "point": 2, "coord": "x"}),
        ("Q1_2_y", {"ImportId": "QID7", "point": 2, "coord": "y"}),
        ("Q1", {"ImportId": "QID7_REGIONS"}),
    ]
    assert {e for *_, e in objs(survey)} == {"vendor_docs"}


def test_highlight_columns_split_and_unsplit():
    survey, _ = one(
        {
            "QuestionID": "QID12",
            "QuestionType": "HL",
            "Selector": "Text",
            "DataExportTag": "Q9",
            "QuestionText": "Highlight",
            "HighlightText": "a b",
            "Choices": {
                "64": {"Word": "a", "Display": "1: a"},
                "65": {"Word": "b", "Display": "2: b"},
            },
            "Answers": {"1": {"Display": "Like"}, "2": {"Display": "Dislike"}},
        }
    )
    assert [(n, o) for n, o, _ in objs(survey)][:2] == [
        ("Q9_1_64", {"ImportId": "QID12_1", "choiceId": "64"}),
        ("Q9_1_65", {"ImportId": "QID12_1", "choiceId": "65"}),
    ]
    assert cols(survey, split_multi_value=False) == [("Q9_1", "QID12_1"), ("Q9_2", "QID12_2")]


def test_metadata_scores_and_display_order_of_blocks_and_flow():
    flow = [
        {
            "Type": "BlockRandomizer",
            "FlowID": "FL_15",
            "SubSet": 1,
            "Flow": [
                {"Type": "Block", "ID": "BL_1", "FlowID": "FL_16"},
                {"Type": "EmbeddedData", "FlowID": "FL_17", "EmbeddedData": []},
            ],
        }
    ]
    doc = minimal_qsf(
        [mc("QID1"), mc("QID2")], flow=flow, block_options={"RandomizeQuestions": "RandomizeAll"}
    )
    doc["SurveyElements"][0]["Payload"][0]["Description"] = "Main Block"
    doc["SurveyElements"].append(
        {"Element": "SCO", "Payload": {"ScoringCategories": [{"ID": "SC_abc", "Name": "Score"}]}}
    )
    survey = load_qsf(doc)
    out = [(n, o) for n, o, _ in objs(survey, include_metadata=True, time_zone="America/Denver")]
    assert out[0] == ("StartDate", {"ImportId": "startDate", "timeZone": "America/Denver"})
    assert ("ResponseId", {"ImportId": "_recordId"}) in out
    assert ("MainBlock_DO_Q1", {"ImportId": "BL_1_DO", "choiceId": "Q1"}) in out
    assert ("FL_15_DO_MainBlock", {"ImportId": "FL_15_DO", "choiceId": "MainBlock"}) in out
    assert ("FL_15_DO_FL_17", {"ImportId": "FL_15_DO", "choiceId": "FL_17"}) in out
    assert ("SC0", {"ImportId": "SC_abc"}) in out
    single = [n for n, *_ in objs(survey, split_multi_value=False)]
    assert "MainBlock_DO" in single and "FL_15_DO" in single


def test_api_option_names_and_subsets():
    survey = load_qsf(minimal_qsf([mc("QID1", selector="MAVR"), mc("QID2")]))
    opts = ColumnOptions(breakoutSets=False, includeDisplayOrder=False, questionIds=["QID1"])
    assert not opts.split_multi_value and opts.do_layout == "none"
    assert [c.name for c in response_columns(survey, include_embedded=False, options=opts)] == [
        "Q1"
    ]


def test_read_header_and_infer_options():
    from surveyir import infer_options, read_header

    text = (
        "StartDate,Q1_1,Q1_2,Q1_DO\n"
        "Start Date,Pick - a,Pick - b,Pick - Display Order\n"
        '"{""ImportId"":""startDate"",""timeZone"":""UTC""}",'
        '"{""ImportId"":""QID1"",""choiceId"":""1""}",'
        '"{""ImportId"":""QID1"",""choiceId"":""2""}",'
        '"{""ImportId"":""QID1_DO""}"\n'
    )
    header = read_header(text)
    assert header[1] == ("Q1_1", {"ImportId": "QID1", "choiceId": "1"})
    opts = infer_options(header)
    assert opts.split_multi_value and opts.do_layout == "single"
    assert read_header("V1,V2\nResponseID,ResponseSet\n") == []  # legacy export


# ------------------------------------------------------------------ conjoint


SDT_JS = """
var featurearray = {"Party" : ["Democrat","Republican"],"Age" : ["35","70"]};
var restrictionarray = [];
var probabilityarray = {};
var weighted = 0;
var K = 5;
var N = 2;
Qualtrics.SurveyEngine.setEmbeddedData('F-1-1', 'Party');
"""


def test_diy_conjoint_from_design_tool_javascript():
    js_q = mc("QID1", QuestionJS=SDT_JS)
    shown = mc("QID2", QuestionText="Profile 1: ${e://Field/F-1-1-1} vs ${e://Field/F-1-2-1}")
    survey = load_qsf(minimal_qsf([js_q, shown]))
    (design,) = survey.conjoints
    assert design.kind == "diy_js" and design.confidence == "high"
    assert [(f.name, f.levels) for f in design.features] == [
        ("Party", ["Democrat", "Republican"]),
        ("Age", ["35", "70"]),
    ]
    assert (design.tasks, design.profiles, design.field_prefix) == (5, 2, "F")
    assert design.question_ids == ["QID1", "QID2"]


def test_diy_conjoint_from_field_names_only():
    q = mc("QID2", QuestionText="${e://Field/K-3-2-4} or ${e://Field/K-1-1-1}")
    (design,) = load_qsf(minimal_qsf([q])).conjoints
    assert (design.tasks, design.profiles, len(design.features)) == (3, 2, 2)
    assert design.confidence == "medium" and design.field_prefix == "K"


def test_legacy_conjoint_only_when_used():
    doc = minimal_qsf([mc()])
    doc["SurveyElements"].append(
        {"Element": "CJ", "Payload": {"ID": "CJ_1", "Type": "SelfExplicated", "Features": []}}
    )
    assert load_qsf(doc).conjoints == []
    doc["SurveyElements"][-1]["Payload"]["Features"] = [{"Name": "Price", "Levels": ["$1", "$2"]}]
    (design,) = load_qsf(doc).conjoints
    assert design.kind == "self_explicated_legacy" and design.features[0].levels == ["$1", "$2"]
