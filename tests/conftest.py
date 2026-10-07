from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "qualtrics"
QSF_FILES = sorted(FIXTURES.glob("*.qsf"))


def raw_blocks(raw: dict) -> list[dict]:
    payload = next(e["Payload"] for e in raw["SurveyElements"] if e["Element"] == "BL")
    blocks = payload.values() if isinstance(payload, dict) else payload
    return [b for b in blocks if isinstance(b, dict) and b.get("Type") != "Trash"]


def raw_questions(raw: dict) -> dict[str, dict]:
    return {
        e["Payload"]["QuestionID"]: e["Payload"]
        for e in raw["SurveyElements"]
        if e["Element"] == "SQ"
    }


def raw_flow(raw: dict) -> list[dict]:
    return next(e["Payload"] for e in raw["SurveyElements"] if e["Element"] == "FL")["Flow"]


@pytest.fixture(params=QSF_FILES, ids=[p.stem for p in QSF_FILES])
def qsf_path(request) -> Path:
    return request.param


@pytest.fixture
def raw(qsf_path) -> dict:
    return json.loads(qsf_path.read_text(encoding="utf-8"))


def minimal_qsf(
    questions: list[dict],
    *,
    flow: list[dict] | None = None,
    block_options: dict | None = None,
    block_elements: list[dict] | None = None,
) -> dict:
    """A small, valid QSF document around the given SQ payloads."""
    elements = block_elements or [
        {"Type": "Question", "QuestionID": q["QuestionID"]} for q in questions
    ]
    return {
        "SurveyEntry": {"SurveyID": "SV_test", "SurveyName": "Test", "SurveyLanguage": "EN"},
        "SurveyElements": [
            {
                "Element": "BL",
                "Payload": [
                    {
                        "Type": "Default",
                        "ID": "BL_1",
                        "Description": "Main",
                        "BlockElements": elements,
                        "Options": block_options or {},
                    }
                ],
            },
            {
                "Element": "FL",
                "Payload": {
                    "Type": "Root",
                    "FlowID": "FL_1",
                    "Flow": flow
                    if flow is not None
                    else [{"Type": "Block", "ID": "BL_1", "FlowID": "FL_2"}],
                },
            },
            *[{"Element": "SQ", "Payload": q} for q in questions],
        ],
    }


def mc(qid: str = "QID1", *, selector: str = "SAVR", **extra) -> dict:
    payload = {
        "QuestionID": qid,
        "QuestionType": "MC",
        "Selector": selector,
        "SubSelector": "TX",
        "DataExportTag": qid.replace("QID", "Q"),
        "QuestionText": "<p>Pick one</p>",
        "Choices": {"1": {"Display": "Yes"}, "2": {"Display": "No"}},
        "ChoiceOrder": [1, 2],
    }
    payload.update(extra)
    return payload
