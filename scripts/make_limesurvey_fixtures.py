"""Write the hand-made LimeSurvey fixtures in tests/fixtures/limesurvey/.

    uv run python scripts/make_limesurvey_fixtures.py

The surveys are described once, below, and written in the layout each
LimeSurvey generation exports (``export_helper.php``):

* ``types_v3.lss``: 2.x / 3.x, text inline, one row per language (DBVersion 366);
* ``types_v6.lss``: 4.x - 6.x, text in ``*_l10ns`` tables (DBVersion 498);
* ``types_v7.lss``: 7.x, ranking items as subquestions (DBVersion 717);
* ``logic.lss``: relevance, piping, filters, validation, randomization, quotas (6.x);
* ``conditions.lss``: legacy condition rows instead of relevance (3.x);
* ``responses.lsa``: ``types_v6.lss`` with three responses (``.lsr``) and the
  matching CSV export, ``responses.csv``, written by hand below (not simulated).

The layout was checked against LimeSurvey's own demo surveys (the external
corpus); these files are small, exact cases for the tests.
"""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "limesurvey"

# --------------------------------------------------------------------------- specs


def T(en: str, de: str | None = None) -> dict[str, str]:
    return {"en": en, "de": de if de is not None else en}


def Q(qid, gid, type_, title, text, order, **kw) -> dict[str, Any]:
    return {
        "qid": qid,
        "gid": gid,
        "type": type_,
        "title": title,
        "text": text if isinstance(text, dict) else T(text),
        "order": order,
        "help": kw.pop("help", T("")),
        "relevance": kw.pop("relevance", "1"),
        "mandatory": kw.pop("mandatory", "N"),
        "other": kw.pop("other", "N"),
        "preg": kw.pop("preg", ""),
        "attributes": kw.pop("attributes", {}),
        "subquestions": kw.pop("subquestions", []),
        "answers": kw.pop("answers", []),
        "script": kw.pop("script", ""),
    }


def SQ(qid, title, text, scale=0, relevance="1") -> dict[str, Any]:
    return {
        "qid": qid,
        "title": title,
        "text": text if isinstance(text, dict) else T(text),
        "scale": scale,
        "relevance": relevance,
    }


def A(code, text, scale=0, assessment="0") -> dict[str, Any]:
    return {
        "code": code,
        "text": text if isinstance(text, dict) else T(text),
        "scale": scale,
        "assessment": assessment,
    }


TYPES: dict[str, Any] = {
    "sid": "111111",
    "language": "en",
    "additional": "de",
    "settings": {"format": "G", "datestamp": "N", "anonymized": "Y", "shownoanswer": "Y"},
    "title": T("Question types", "Fragetypen"),
    "groups": [
        (1, 0, T("Choices", "Auswahl"), "", ""),
        (2, 1, T("Arrays", "Matrizen"), "", ""),
        (3, 2, T("Entry", "Eingabe"), "", ""),
    ],
    "questions": [
        Q(
            101,
            1,
            "L",
            "colour",
            T("Favourite colour?", "Lieblingsfarbe?"),
            1,
            other="Y",
            mandatory="Y",
            help=T("Pick one.", "Eine wählen."),
            answers=[
                A("A1", T("Red", "Rot")),
                A("A2", T("Green", "Grün")),
                A("A3", T("Blue", "Blau")),
            ],
        ),
        Q(102, 1, "!", "country", "Country?", 2, answers=[A("FR", "France"), A("DE", "Germany")]),
        Q(103, 1, "O", "brand", "Brand?", 3, answers=[A("B1", "Acme"), A("B2", "Globex")]),
        Q(104, 1, "5", "five", "Rate 1 to 5", 4),
        Q(105, 1, "G", "gender", "Gender?", 5),
        Q(106, 1, "Y", "agree", "Agree?", 6),
        Q(
            107,
            1,
            "M",
            "pets",
            "Which pets?",
            7,
            other="Y",
            attributes={"exclude_all_others": "SQ004"},
            subquestions=[
                SQ(1071, "SQ001", T("Dog", "Hund")),
                SQ(1072, "SQ002", T("Cat", "Katze")),
                SQ(1073, "SQ003", "Fish"),
                SQ(1074, "SQ004", "None"),
            ],
        ),
        Q(
            108,
            1,
            "P",
            "media",
            "Which media, and why?",
            8,
            subquestions=[SQ(1081, "SQ001", "TV"), SQ(1082, "SQ002", "Radio")],
        ),
        Q(
            201,
            2,
            "F",
            "rate",
            "Rate each",
            1,
            subquestions=[SQ(2011, "SQ001", "Price"), SQ(2012, "SQ002", "Quality")],
            answers=[
                A("A1", "Bad", assessment="1"),
                A("A2", "OK", assessment="2"),
                A("A3", "Good", assessment="3"),
            ],
        ),
        Q(
            202,
            2,
            "H",
            "bycol",
            "By column",
            2,
            subquestions=[SQ(2021, "SQ001", "Morning"), SQ(2022, "SQ002", "Evening")],
            answers=[A("A1", "Yes"), A("A2", "No")],
        ),
        Q(203, 2, "A", "a5", "Five point array", 3, subquestions=[SQ(2031, "SQ001", "One")]),
        Q(204, 2, "B", "b10", "Ten point array", 4, subquestions=[SQ(2041, "SQ001", "One")]),
        Q(205, 2, "C", "ync", "Yes/Uncertain/No array", 5, subquestions=[SQ(2051, "SQ001", "One")]),
        Q(
            206,
            2,
            "E",
            "isd",
            "Increase/Same/Decrease array",
            6,
            subquestions=[SQ(2061, "SQ001", "One")],
        ),
        Q(
            207,
            2,
            "1",
            "dual",
            "Dual scale",
            7,
            attributes={"dualscale_headerA": T("Now"), "dualscale_headerB": T("Before")},
            subquestions=[SQ(2071, "SQ001", "Item 1"), SQ(2072, "SQ002", "Item 2")],
            answers=[A("A1", "Low"), A("A2", "High"), A("B1", "Less", 1), A("B2", "More", 1)],
        ),
        Q(
            208,
            2,
            ":",
            "nums",
            "Numbers",
            8,
            subquestions=[
                SQ(2081, "R1", "Row 1"),
                SQ(2082, "R2", "Row 2"),
                SQ(2083, "C1", "Col 1", 1),
                SQ(2084, "C2", "Col 2", 1),
            ],
        ),
        Q(
            209,
            2,
            ";",
            "texts",
            "Texts",
            9,
            subquestions=[SQ(2091, "R1", "Row 1"), SQ(2093, "C1", "Col 1", 1)],
        ),
        Q(
            301,
            3,
            "K",
            "alloc",
            "Allocate 100",
            1,
            attributes={"equals_num_value": "100"},
            subquestions=[SQ(3011, "SQ001", "Saving"), SQ(3012, "SQ002", "Spending")],
        ),
        Q(
            302,
            3,
            "Q",
            "names",
            "Names",
            2,
            subquestions=[SQ(3021, "SQ001", "First"), SQ(3022, "SQ002", "Last")],
        ),
        Q(303, 3, "S", "short", "Short text", 3, attributes={"maximum_chars": "10"}),
        Q(304, 3, "T", "long", "Long text", 4),
        Q(305, 3, "U", "huge", "Huge text", 5),
        Q(
            306,
            3,
            "N",
            "age",
            "Age?",
            6,
            mandatory="Y",
            attributes={
                "min_num_value_n": "18",
                "max_num_value_n": "99",
                "num_value_int_only": "1",
            },
        ),
        Q(307, 3, "D", "when", "When?", 7),
        Q(
            308,
            3,
            "R",
            "rank",
            "Rank these",
            8,
            answers=[A("R1", "Alpha"), A("R2", "Beta"), A("R3", "Gamma")],
        ),
        Q(309, 3, "X", "info", "Thank you, {colour.shown} it is.", 9),
        Q(310, 3, "*", "score", "{sum(age, 1)}", 10, attributes={"hidden": "1"}),
        Q(311, 3, "|", "upload", "Upload a file", 11, attributes={"allowed_filetypes": "png, jpg"}),
    ],
}

LOGIC: dict[str, Any] = {
    "sid": "222222",
    "language": "en",
    "additional": "",
    "settings": {
        "format": "G",
        "datestamp": "N",
        "anonymized": "N",
        "usetokens": "Y",
        "shownoanswer": "N",
    },
    "title": T("Logic"),
    "groups": [
        (10, 0, T("Screen"), "", ""),
        (11, 1, T("Follow-up"), 'consent == "Y"', ""),
        (12, 2, T("Arm A"), "", "rg1"),
        (13, 3, T("Arm B"), "", "rg1"),
        (14, 4, T("Order"), "", ""),
    ],
    "questions": [
        Q(401, 10, "N", "age", "Your age?", 1, mandatory="Y"),
        Q(402, 10, "Y", "consent", "Continue?", 2, mandatory="Y"),
        Q(
            403,
            10,
            "M",
            "pets",
            "Which pets?",
            3,
            other="Y",
            subquestions=[
                SQ(4031, "SQ001", "Dog"),
                SQ(4032, "SQ002", "Cat"),
                SQ(4033, "SQ003", "Fish"),
            ],
        ),
        Q(
            404,
            10,
            "*",
            "npets",
            "{count(pets_SQ001, pets_SQ002, pets_SQ003)}",
            4,
            attributes={"hidden": "1"},
        ),
        Q(501, 11, "S", "dogname", "Your dog's name?", 1, relevance='pets_SQ001 == "Y"'),
        Q(
            502,
            11,
            "F",
            "petrate",
            "Rate your pets",
            2,
            attributes={"array_filter": "pets"},
            subquestions=[
                SQ(5021, "SQ001", "Dog"),
                SQ(5022, "SQ002", "Cat"),
                SQ(5023, "SQ003", "Fish", relevance="age >= 30"),
            ],
            answers=[A("A1", "Bad"), A("A2", "Good")],
        ),
        Q(
            503,
            11,
            "X",
            "hello",
            "Hello {TOKEN:FIRSTNAME}, you have {npets} pet(s).",
            3,
            relevance="age >= 18",
        ),
        Q(
            504,
            11,
            "L",
            "likes",
            "Do you like surveys?",
            4,
            answers=[A("A1", "Yes"), A("A2", "No")],
        ),
        Q(505, 11, "T", "why", "Why, {dogname}?", 5, relevance='likes == "A1"'),
        Q(
            506,
            11,
            "K",
            "budget",
            "Split 10",
            6,
            attributes={
                "em_validation_q": "sum(self.NAOK) == 10",
                "em_validation_q_tip": T("The parts must add up to 10."),
            },
            subquestions=[SQ(5061, "SQ001", "Food"), SQ(5062, "SQ002", "Toys")],
        ),
        Q(601, 12, "X", "armA", "You are in arm A.", 1),
        Q(701, 13, "X", "armB", "You are in arm B.", 1),
        Q(801, 14, "S", "o1", "First", 1, attributes={"random_group": "g"}),
        Q(802, 14, "S", "fixed", "Fixed", 2),
        Q(803, 14, "S", "o2", "Second", 3, attributes={"random_group": "g"}),
        Q(804, 14, "S", "o3", "Third", 4, attributes={"random_group": "g"}),
    ],
    "quotas": [
        {
            "id": "1",
            "name": "Few dislikers",
            "qlimit": "1",
            "action": "1",
            "active": "1",
            "members": [(504, "A2")],
        },
    ],
}

CONDITIONS: dict[str, Any] = {
    "sid": "333333",
    "language": "en",
    "additional": "",
    "settings": {"format": "S", "datestamp": "N", "anonymized": "Y"},
    "title": T("Conditions"),
    "groups": [(20, 0, T("Only group"), "", "")],
    "questions": [
        Q(901, 20, "L", "c1", "Pick", 1, answers=[A("A1", "One"), A("A2", "Two")]),
        Q(
            902,
            20,
            "M",
            "c2",
            "Tick",
            2,
            subquestions=[SQ(9021, "SQ001", "First"), SQ(9022, "SQ002", "Second")],
        ),
        Q(903, 20, "N", "c3", "Number", 3),
        Q(904, 20, "S", "c4", "Text", 4),
        Q(911, 20, "X", "t1", "c1 is One, or c2 First ticked", 5),
        Q(912, 20, "X", "t2", "c1 not answered", 6),
        Q(913, 20, "X", "t3", "c3 at least 5 and c4 matches", 7),
        Q(914, 20, "X", "t4", "token attribute", 8),
    ],
    # cid, qid, cqid, cfieldname, method, value, scenario
    "conditions": [
        (1, 911, 901, "333333X20X901", "==", "A1", 1),
        (2, 911, 902, "333333X20X902", "==", "SQ001", 2),
        (3, 912, 901, "333333X20X901", "==", " ", 1),
        (4, 913, 903, "333333X20X903", ">=", "5", 1),
        (5, 913, 904, "333333X20X904", "RX", "^[a-z]+$", 1),
        (6, 914, 0, "{TOKEN:ATTRIBUTE_1}", "==", "gold", 1),
    ],
}

# --------------------------------------------------------------------------- writers


def table(name: str, fields: list[str], rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return []
    out = [f" <{name}>", "  <fields>"]
    out += [f"   <fieldname>{f}</fieldname>" for f in fields]
    out += ["  </fields>", "  <rows>"]
    for r in rows:
        out.append("   <row>")
        for f in fields:
            v = r.get(f)
            if v is None:
                continue  # NULL
            tag = f"_{f}" if f[:1].isdigit() else f
            out.append(f"    <{tag}><![CDATA[{v}]]></{tag}>")
        out.append("   </row>")
    out += ["  </rows>", f" </{name}>"]
    return out


def document(doctype: str, version: int, langs: list[str], tables: list[list[str]]) -> str:
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<document>",
        f" <LimeSurveyDocType>{doctype}</LimeSurveyDocType>",
        f" <DBVersion>{version}</DBVersion>",
        " <languages>",
        *[f"  <language>{x}</language>" for x in langs],
        " </languages>",
    ]
    for t in tables:
        out += t
    out += ["</document>", ""]
    return "\n".join(out)


def langs_of(spec: dict[str, Any]) -> list[str]:
    return [spec["language"]] + [x for x in spec["additional"].split() if x]


def survey_rows(spec: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    sid = spec["sid"]
    survey = {
        "sid": sid,
        "language": spec["language"],
        "additional_languages": spec["additional"],
        **spec["settings"],
    }
    ls = [
        {
            "surveyls_survey_id": sid,
            "surveyls_language": lang,
            "surveyls_title": spec["title"].get(lang, spec["title"]["en"]),
            "surveyls_description": "",
            "surveyls_welcometext": "Welcome",
            "surveyls_endtext": "Thanks",
            "surveyls_url": "",
        }
        for lang in langs_of(spec)
    ]
    return [survey], ls


def attribute_rows(spec: dict[str, Any]) -> list[dict]:
    rows = []
    for q in spec["questions"]:
        for name, v in q["attributes"].items():
            if isinstance(v, dict):
                rows += [
                    {"qid": q["qid"], "attribute": name, "value": v.get(lang, ""), "language": lang}
                    for lang in langs_of(spec)
                ]
            else:
                rows.append({"qid": q["qid"], "attribute": name, "value": v})
    return rows


def write_l10n(spec: dict[str, Any], version: int, *, ranking_subquestions: bool) -> str:
    sid = spec["sid"]
    langs = langs_of(spec)
    survey, ls = survey_rows(spec)
    groups, gl = [], []
    for gid, order, name, rel, rgroup in spec["groups"]:
        groups.append(
            {
                "gid": gid,
                "sid": sid,
                "group_order": order,
                "randomization_group": rgroup,
                "grelevance": rel,
            }
        )
        gl += [
            {
                "id": f"{gid}{i}",
                "gid": gid,
                "group_name": name.get(lang, name["en"]),
                "description": "",
                "language": lang,
            }
            for i, lang in enumerate(langs)
        ]
    qrows, sqrows, ql, answers, al = [], [], [], [], []
    aid = 1
    for q in spec["questions"]:
        subs = list(q["subquestions"])
        anss = list(q["answers"])
        if ranking_subquestions and q["type"] == "R":
            subs = [SQ(q["qid"] * 10 + i, a["code"], a["text"]) for i, a in enumerate(anss, 1)]
            anss = []
        qrows.append(
            {
                "qid": q["qid"],
                "parent_qid": 0,
                "sid": sid,
                "gid": q["gid"],
                "type": q["type"],
                "title": q["title"],
                "preg": q["preg"],
                "other": q["other"],
                "mandatory": q["mandatory"],
                "encrypted": "N",
                "question_order": q["order"],
                "scale_id": 0,
                "same_default": 0,
                "relevance": q["relevance"],
                "question_theme_name": "",
                "modulename": "",
                "same_script": 0,
            }
        )
        ql += [
            {
                "id": f"{q['qid']}{i}",
                "qid": q["qid"],
                "question": q["text"].get(lang, q["text"]["en"]),
                "help": q["help"].get(lang, ""),
                "script": q["script"],
                "language": lang,
            }
            for i, lang in enumerate(langs)
        ]
        for order, s in enumerate(subs, 1):
            sqrows.append(
                {
                    "qid": s["qid"],
                    "parent_qid": q["qid"],
                    "sid": sid,
                    "gid": q["gid"],
                    "type": "T",
                    "title": s["title"],
                    "preg": "",
                    "other": "N",
                    "mandatory": "",
                    "encrypted": "N",
                    "question_order": order,
                    "scale_id": s["scale"],
                    "same_default": 0,
                    "relevance": s["relevance"],
                    "question_theme_name": "",
                    "modulename": "",
                    "same_script": 0,
                }
            )
            ql += [
                {
                    "id": f"{s['qid']}{i}",
                    "qid": s["qid"],
                    "question": s["text"].get(lang, s["text"]["en"]),
                    "help": "",
                    "script": "",
                    "language": lang,
                }
                for i, lang in enumerate(langs)
            ]
        for order, a in enumerate(anss, 1):
            answers.append(
                {
                    "aid": aid,
                    "qid": q["qid"],
                    "code": a["code"],
                    "sortorder": order,
                    "assessment_value": a["assessment"],
                    "scale_id": a["scale"],
                }
            )
            al += [
                {
                    "id": f"{aid}{i}",
                    "aid": aid,
                    "answer": a["text"].get(lang, a["text"]["en"]),
                    "language": lang,
                }
                for i, lang in enumerate(langs)
            ]
            aid += 1
    quota, members = quota_rows(spec)
    tables = [
        table(
            "answers", ["aid", "qid", "code", "sortorder", "assessment_value", "scale_id"], answers
        ),
        table("answer_l10ns", ["id", "aid", "answer", "language"], al),
        table("groups", ["gid", "sid", "group_order", "randomization_group", "grelevance"], groups),
        table("group_l10ns", ["id", "gid", "group_name", "description", "language"], gl),
        table("questions", list(qrows[0]), qrows),
        table("subquestions", list(qrows[0]), sqrows),
        table("question_l10ns", ["id", "qid", "question", "help", "script", "language"], ql),
        table(
            "question_attributes", ["qid", "attribute", "value", "language"], attribute_rows(spec)
        ),
        table("quota", ["id", "sid", "name", "qlimit", "action", "active", "autoload_url"], quota),
        table("quota_members", ["id", "sid", "qid", "quota_id", "code"], members),
        table("surveys", list(survey[0]), survey),
        table("surveys_languagesettings", list(ls[0]), ls),
    ]
    return document("Survey", version, langs, tables)


def quota_rows(spec: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    quota, members = [], []
    for i, q in enumerate(spec.get("quotas", []), 1):
        quota.append(
            {
                "id": q["id"],
                "sid": spec["sid"],
                "name": q["name"],
                "qlimit": q["qlimit"],
                "action": q["action"],
                "active": q["active"],
                "autoload_url": "0",
            }
        )
        members += [
            {"id": f"{i}{j}", "sid": spec["sid"], "qid": qid, "quota_id": q["id"], "code": code}
            for j, (qid, code) in enumerate(q["members"])
        ]
    return quota, members


def write_legacy(spec: dict[str, Any], version: int) -> str:
    sid = spec["sid"]
    langs = langs_of(spec)
    survey, ls = survey_rows(spec)
    groups = [
        {
            "gid": gid,
            "sid": sid,
            "group_name": name.get(lang, name["en"]),
            "group_order": order,
            "description": "",
            "language": lang,
            "randomization_group": rgroup,
            "grelevance": rel,
        }
        for gid, order, name, rel, rgroup in spec["groups"]
        for lang in langs
    ]
    fields = [
        "qid",
        "parent_qid",
        "sid",
        "gid",
        "type",
        "title",
        "question",
        "preg",
        "help",
        "other",
        "mandatory",
        "question_order",
        "language",
        "scale_id",
        "same_default",
        "relevance",
    ]
    qrows, sqrows, answers = [], [], []
    for q in spec["questions"]:
        for lang in langs:
            qrows.append(
                {
                    "qid": q["qid"],
                    "parent_qid": 0,
                    "sid": sid,
                    "gid": q["gid"],
                    "type": q["type"],
                    "title": q["title"],
                    "question": q["text"].get(lang, q["text"]["en"]),
                    "preg": q["preg"],
                    "help": q["help"].get(lang, ""),
                    "other": q["other"],
                    "mandatory": q["mandatory"],
                    "question_order": q["order"],
                    "language": lang,
                    "scale_id": 0,
                    "same_default": 0,
                    "relevance": q["relevance"],
                }
            )
            for order, s in enumerate(q["subquestions"], 1):
                sqrows.append(
                    {
                        "qid": s["qid"],
                        "parent_qid": q["qid"],
                        "sid": sid,
                        "gid": q["gid"],
                        "type": "T",
                        "title": s["title"],
                        "question": s["text"].get(lang, s["text"]["en"]),
                        "preg": "",
                        "help": "",
                        "other": "N",
                        "mandatory": "",
                        "question_order": order,
                        "language": lang,
                        "scale_id": s["scale"],
                        "same_default": 0,
                        "relevance": s["relevance"],
                    }
                )
            for order, a in enumerate(q["answers"], 1):
                answers.append(
                    {
                        "qid": q["qid"],
                        "code": a["code"],
                        "answer": a["text"].get(lang, a["text"]["en"]),
                        "sortorder": order,
                        "assessment_value": a["assessment"],
                        "language": lang,
                        "scale_id": a["scale"],
                    }
                )
    conditions = [
        {
            "cid": c[0],
            "qid": c[1],
            "cqid": c[2],
            "cfieldname": c[3],
            "method": c[4],
            "value": c[5],
            "scenario": c[6],
        }
        for c in spec.get("conditions", [])
    ]
    quota, members = quota_rows(spec)
    tables = [
        table(
            "answers",
            ["qid", "code", "answer", "sortorder", "assessment_value", "language", "scale_id"],
            answers,
        ),
        table(
            "conditions",
            ["cid", "qid", "cqid", "cfieldname", "method", "value", "scenario"],
            conditions,
        ),
        table("groups", list(groups[0]), groups),
        table("questions", fields, qrows),
        table("subquestions", fields, sqrows),
        table(
            "question_attributes", ["qid", "attribute", "value", "language"], attribute_rows(spec)
        ),
        table("quota", ["id", "sid", "name", "qlimit", "action", "active", "autoload_url"], quota),
        table("quota_members", ["id", "sid", "qid", "quota_id", "code"], members),
        table("surveys", list(survey[0]), survey),
        table("surveys_languagesettings", list(ls[0]), ls),
    ]
    return document("Survey", version, langs, tables)


#: three hand-made responses to TYPES, as (field -> value); SGQA field names
RESPONSES: list[dict[str, str]] = [
    {
        "id": "1",
        "submitdate": "1980-01-01 00:00:00",
        "lastpage": "3",
        "startlanguage": "en",
        "seed": "123",
        "111111X1X101": "A2",
        "111111X1X102": "FR",
        "111111X1X103": "B1",
        "111111X1X103comment": "cheap",
        "111111X1X104": "4",
        "111111X1X105": "F",
        "111111X1X106": "Y",
        "111111X1X107SQ001": "Y",
        "111111X1X107SQ002": "",
        "111111X1X107SQ003": "Y",
        "111111X1X107SQ004": "",
        "111111X1X107other": "Parrot",
        "111111X1X108SQ001": "Y",
        "111111X1X108SQ001comment": "news",
        "111111X2X201SQ001": "A3",
        "111111X2X201SQ002": "A1",
        "111111X2X207SQ001#0": "A2",
        "111111X2X207SQ001#1": "B1",
        "111111X2X208R1_C1": "3",
        "111111X2X208R2_C2": "7",
        "111111X2X209R1_C1": "hello",
        "111111X3X301SQ001": "40",
        "111111X3X301SQ002": "60",
        "111111X3X302SQ001": "Ada",
        "111111X3X302SQ002": "Lovelace",
        "111111X3X303": "short",
        "111111X3X306": "36",
        "111111X3X307": "2024-05-01 00:00",
        "111111X3X3081": "R3",
        "111111X3X3082": "R1",
        "111111X3X3083": "R2",
        "111111X3X310": "37",
    },
    {
        "id": "2",
        "submitdate": "1980-01-01 00:00:00",
        "lastpage": "3",
        "startlanguage": "de",
        "seed": "456",
        "111111X1X101": "-oth-",
        "111111X1X101other": "Purple",
        "111111X1X107SQ004": "Y",
        "111111X3X306": "70",
        "111111X3X310": "71",
    },
    {
        "id": "3",
        "submitdate": "",
        "lastpage": "1",
        "startlanguage": "en",
        "seed": "789",
        "111111X1X101": "A1",
    },
]


def write_archive(lss: str) -> bytes:
    fields = ["id", "submitdate", "lastpage", "startlanguage", "seed"]
    for r in RESPONSES:
        fields += [k for k in r if k not in fields]
    rows = [{k.replace("#", "-"): v for k, v in r.items() if v != ""} for r in RESPONSES]
    lsr = document(
        "Responses", 498, ["en"], [table("responses", [f.replace("#", "-") for f in fields], rows)]
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in (("survey_111111.lss", lss), ("survey_111111_responses.lsr", lsr)):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))  # reproducible
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, text)
    return buf.getvalue()


#: the same responses as a CSV export with question-code headings
CODE_HEADINGS = {
    "111111X1X101": "colour",
    "111111X1X101other": "colour[other]",
    "111111X1X102": "country",
    "111111X1X103": "brand",
    "111111X1X103comment": "brand[comment]",
    "111111X1X104": "five",
    "111111X1X105": "gender",
    "111111X1X106": "agree",
    "111111X1X107SQ001": "pets[SQ001]",
    "111111X1X107SQ002": "pets[SQ002]",
    "111111X1X107SQ003": "pets[SQ003]",
    "111111X1X107SQ004": "pets[SQ004]",
    "111111X1X107other": "pets[other]",
    "111111X1X108SQ001": "media[SQ001]",
    "111111X1X108SQ001comment": "media[SQ001comment]",
    "111111X2X201SQ001": "rate[SQ001]",
    "111111X2X201SQ002": "rate[SQ002]",
    "111111X2X207SQ001#0": "dual[SQ001][1]",
    "111111X2X207SQ001#1": "dual[SQ001][2]",
    "111111X2X208R1_C1": "nums[R1_C1]",
    "111111X2X208R2_C2": "nums[R2_C2]",
    "111111X2X209R1_C1": "texts[R1_C1]",
    "111111X3X301SQ001": "alloc[SQ001]",
    "111111X3X301SQ002": "alloc[SQ002]",
    "111111X3X302SQ001": "names[SQ001]",
    "111111X3X302SQ002": "names[SQ002]",
    "111111X3X303": "short",
    "111111X3X306": "age",
    "111111X3X307": "when",
    "111111X3X3081": "rank[1]",
    "111111X3X3082": "rank[2]",
    "111111X3X3083": "rank[3]",
    "111111X3X310": "score",
}


def write_csv() -> str:
    header = ["id", "submitdate", "lastpage", "startlanguage", "seed", *CODE_HEADINGS.values()]
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(header)
    for r in RESPONSES:
        named = {CODE_HEADINGS.get(k, k): v for k, v in r.items()}
        w.writerow([named.get(h, "") for h in header])
    return buf.getvalue()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    v6 = write_l10n(TYPES, 498, ranking_subquestions=False)
    (OUT / "types_v3.lss").write_text(write_legacy(TYPES, 366), encoding="utf-8")
    (OUT / "types_v6.lss").write_text(v6, encoding="utf-8")
    (OUT / "types_v7.lss").write_text(
        write_l10n(TYPES, 717, ranking_subquestions=True), encoding="utf-8"
    )
    (OUT / "logic.lss").write_text(
        write_l10n(LOGIC, 498, ranking_subquestions=False), encoding="utf-8"
    )
    (OUT / "conditions.lss").write_text(write_legacy(CONDITIONS, 366), encoding="utf-8")
    (OUT / "responses.lsa").write_bytes(write_archive(v6))
    (OUT / "responses.csv").write_text(write_csv(), encoding="utf-8")
    print("wrote", sorted(p.name for p in OUT.iterdir() if p.is_file()))


if __name__ == "__main__":
    main()
