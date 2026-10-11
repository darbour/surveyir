"""Bring the three generations of LimeSurvey structure files into one shape.

* **2.x / 3.x**: text lives on the rows themselves, one row per language
  (``questions`` has ``question``, ``help`` and ``language``; ``answers`` has
  ``answer`` and ``language`` and no ``aid``; ``groups`` has ``group_name``).
* **4.x - 6.x**: text moves to ``*_l10ns`` tables (``question_l10ns``,
  ``answer_l10ns``, ``group_l10ns``, ``defaultvalue_l10ns``), one row per
  language, keyed by the base row's id.
* **7.x**: as 4.x, but ranking items are subquestions instead of answers.

Which shape a file has is decided as LimeSurvey's own importer decides it: by
whether ``question_l10ns`` is present, not by ``DBVersion``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from .xmlfile import Document, Row

ERA_LEGACY = "legacy"
ERA_L10N = "l10n"


def _int(v: str | None, default: int = 0) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


@dataclass
class QRow:
    """A question or subquestion, every language merged."""

    qid: str
    parent_qid: str
    gid: str
    type: str
    title: str
    order: int
    scale_id: int
    row: Row  # the base row, verbatim (extras)
    #: language -> {"question": ..., "help": ..., "script": ...}
    texts: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass
class ARow:
    """An answer option, every language merged."""

    qid: str
    code: str
    order: int
    scale_id: int
    assessment_value: str
    row: Row
    texts: dict[str, str] = field(default_factory=dict)


@dataclass
class GRow:
    gid: str
    order: int
    randomization_group: str
    relevance: str
    row: Row
    #: language -> {"group_name": ..., "description": ...}
    texts: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass
class Structure:
    era: str
    sid: str
    survey: Row
    #: language -> survey language settings row
    settings: dict[str, Row]
    languages: list[str]
    groups: list[GRow]
    questions: list[QRow]
    subquestions: list[QRow]
    answers: list[ARow]
    #: qid -> attribute -> value (language-independent) or {language: value}
    attributes: dict[str, dict[str, str | dict[str, str]]]
    conditions: list[Row]
    #: (qid, sqid, scale_id, specialtype) -> {language: value}
    defaults: dict[tuple[str, str, str, str], dict[str, str]]
    quotas: list[Row]
    quota_members: list[Row]
    quota_texts: dict[str, dict[str, Row]]
    assessments: list[Row]
    url_parameters: list[Row]
    #: tables not read into the structure, verbatim
    other_tables: dict[str, list[Row]]


_READ = {
    "surveys",
    "surveys_languagesettings",
    "groups",
    "group_l10ns",
    "questions",
    "subquestions",
    "question_l10ns",
    "answers",
    "answer_l10ns",
    "question_attributes",
    "conditions",
    "defaultvalues",
    "defaultvalue_l10ns",
    "quota",
    "quota_members",
    "quota_languagesettings",
    "assessments",
    "survey_url_parameters",
}


def normalize(doc: Document) -> Structure:
    era = ERA_L10N if "question_l10ns" in doc.tables else ERA_LEGACY
    surveys = doc.rows("surveys")
    survey = surveys[0] if surveys else {}
    sid = survey.get("sid") or ""
    if not sid:
        for name in ("questions", "groups"):
            rows = doc.rows(name)
            if rows and rows[0].get("sid"):
                sid = rows[0]["sid"] or ""
                break
    base = survey.get("language") or (doc.languages[0] if doc.languages else "en")
    languages = [base] + [
        x for x in (survey.get("additional_languages") or "").split() if x and x != base
    ]
    for lang in doc.languages:
        if lang not in languages:
            languages.append(lang)
    settings = {
        (r.get("surveyls_language") or base): r for r in doc.rows("surveys_languagesettings")
    }
    groups = _groups(doc, era, base)
    questions = _questions(doc, "questions", era, base)
    subquestions = _questions(doc, "subquestions", era, base)
    # 2.x put subquestions in "questions" with a parent
    if not doc.rows("subquestions"):
        subquestions = [q for q in questions if q.parent_qid not in ("", "0")]
        questions = [q for q in questions if q.parent_qid in ("", "0")]
    answers = _answers(doc, era, base)
    attributes: dict[str, dict[str, str | dict[str, str]]] = {}
    for r in doc.rows("question_attributes"):
        qid, name = r.get("qid") or "", r.get("attribute") or ""
        value = r.get("value") or ""
        lang = r.get("language")
        slot = attributes.setdefault(qid, {})
        if lang:
            existing = slot.get(name)
            texts = existing if isinstance(existing, dict) else {}
            texts[lang] = value
            slot[name] = texts
        else:
            slot[name] = value
    defaults: dict[tuple[str, str, str, str], dict[str, str]] = {}
    l10n = {}
    for r in doc.rows("defaultvalue_l10ns"):
        l10n.setdefault(r.get("dvid") or "", {})[r.get("language") or base] = (
            r.get("defaultvalue") or ""
        )
    for r in doc.rows("defaultvalues"):
        key = (
            r.get("qid") or "",
            r.get("sqid") or "0",
            r.get("scale_id") or "0",
            r.get("specialtype") or "",
        )
        if "dvid" in r:
            defaults.setdefault(key, {}).update(l10n.get(r.get("dvid") or "", {}))
        else:
            defaults.setdefault(key, {})[r.get("language") or base] = r.get("defaultvalue") or ""
    quota_texts: dict[str, dict[str, Row]] = {}
    for r in doc.rows("quota_languagesettings"):
        quota_texts.setdefault(r.get("quotals_quota_id") or "", {})[
            r.get("quotals_language") or base
        ] = r
    return Structure(
        era=era,
        sid=sid,
        survey=survey,
        settings=settings,
        languages=languages,
        groups=groups,
        questions=questions,
        subquestions=subquestions,
        answers=answers,
        attributes=attributes,
        conditions=doc.rows("conditions"),
        defaults=defaults,
        quotas=doc.rows("quota"),
        quota_members=doc.rows("quota_members"),
        quota_texts=quota_texts,
        assessments=doc.rows("assessments"),
        url_parameters=doc.rows("survey_url_parameters"),
        other_tables={k: v for k, v in doc.tables.items() if k not in _READ},
    )


def _merge(rows: Iterable[Row], key: str, base: str) -> dict[str, tuple[Row, dict[str, Row]]]:
    """Group per-language rows by ``key``: id -> (base-language row, {lang: row})."""
    out: dict[str, tuple[Row, dict[str, Row]]] = {}
    for r in rows:
        k = r.get(key) or ""
        lang = r.get("language") or base
        if k not in out:
            out[k] = (r, {})
        elif lang == base:
            out[k] = (r, out[k][1])
        out[k][1][lang] = r
    return out


def _groups(doc: Document, era: str, base: str) -> list[GRow]:
    out = []
    if era == ERA_L10N:
        texts: dict[str, dict[str, dict[str, str]]] = {}
        for r in doc.rows("group_l10ns"):
            texts.setdefault(r.get("gid") or "", {})[r.get("language") or base] = {
                "group_name": r.get("group_name") or "",
                "description": r.get("description") or "",
            }
        for r in doc.rows("groups"):
            gid = r.get("gid") or ""
            out.append(
                GRow(
                    gid=gid,
                    order=_int(r.get("group_order")),
                    randomization_group=r.get("randomization_group") or "",
                    relevance=r.get("grelevance") or "",
                    row=r,
                    texts=texts.get(gid, {}),
                )
            )
    else:
        for gid, (r, langs) in _merge(doc.rows("groups"), "gid", base).items():
            out.append(
                GRow(
                    gid=gid,
                    order=_int(r.get("group_order")),
                    randomization_group=r.get("randomization_group") or "",
                    relevance=r.get("grelevance") or "",
                    row=r,
                    texts={
                        lang: {
                            "group_name": x.get("group_name") or "",
                            "description": x.get("description") or "",
                        }
                        for lang, x in langs.items()
                    },
                )
            )
    out.sort(key=lambda g: (g.order, _int(g.gid)))
    return out


def _questions(doc: Document, table: str, era: str, base: str) -> list[QRow]:
    out = []
    if era == ERA_L10N:
        texts: dict[str, dict[str, dict[str, str]]] = {}
        for r in doc.rows("question_l10ns"):
            texts.setdefault(r.get("qid") or "", {})[r.get("language") or base] = {
                "question": r.get("question") or "",
                "help": r.get("help") or "",
                "script": r.get("script") or "",
            }
        merged = {(r.get("qid") or ""): (r, {}) for r in doc.rows(table)}
    else:
        texts = {}
        merged = _merge(doc.rows(table), "qid", base)
        for qid, (_, langs) in merged.items():
            texts[qid] = {
                lang: {
                    "question": x.get("question") or "",
                    "help": x.get("help") or "",
                    "script": x.get("script") or "",
                }
                for lang, x in langs.items()
            }
    for qid, (r, _) in merged.items():
        out.append(
            QRow(
                qid=qid,
                parent_qid=r.get("parent_qid") or "0",
                gid=r.get("gid") or "",
                type=r.get("type") or "",
                title=r.get("title") or "",
                order=_int(r.get("question_order")),
                scale_id=_int(r.get("scale_id")),
                row=r,
                texts=texts.get(qid, {}),
            )
        )
    out.sort(key=lambda q: (q.order, _int(q.qid)))
    return out


def _answers(doc: Document, era: str, base: str) -> list[ARow]:
    out = []
    if era == ERA_L10N:
        texts: dict[str, dict[str, str]] = {}
        for r in doc.rows("answer_l10ns"):
            texts.setdefault(r.get("aid") or "", {})[r.get("language") or base] = (
                r.get("answer") or ""
            )
        for r in doc.rows("answers"):
            out.append(
                ARow(
                    qid=r.get("qid") or "",
                    code=r.get("code") or "",
                    order=_int(r.get("sortorder")),
                    scale_id=_int(r.get("scale_id")),
                    assessment_value=r.get("assessment_value") or "0",
                    row=r,
                    texts=texts.get(r.get("aid") or "", {}),
                )
            )
    else:
        merged: dict[tuple[str, str, str], tuple[Row, dict[str, str]]] = {}
        for r in doc.rows("answers"):
            key = (r.get("qid") or "", r.get("code") or "", r.get("scale_id") or "0")
            lang = r.get("language") or base
            if key not in merged or lang == base:
                merged[key] = (r, merged.get(key, ({}, {}))[1])
            merged[key][1][lang] = r.get("answer") or ""
        for (qid, code, scale), (r, langs) in merged.items():
            out.append(
                ARow(
                    qid=qid,
                    code=code,
                    order=_int(r.get("sortorder")),
                    scale_id=_int(scale),
                    assessment_value=r.get("assessment_value") or "0",
                    row=r,
                    texts=langs,
                )
            )
    out.sort(key=lambda a: (a.scale_id, a.order, a.code))
    return out
