"""Read LimeSurvey export files into plain tables.

LimeSurvey writes every export (survey structure ``.lss``, group ``.lsg``,
question ``.lsq``, responses ``.lsr``, participants ``.lst``, timings ``.lsi``)
with one XML writer (``export_helper.php``, ``buildXMLFromQuery``):

    <document>
      <LimeSurveyDocType>Survey</LimeSurveyDocType>
      <DBVersion>703</DBVersion>
      <languages><language>en</language>...</languages>
      <questions>
        <fields><fieldname>qid</fieldname>...</fields>
        <rows><row><qid><![CDATA[12]]></qid>...</row></rows>
      </questions>
      ...
    </document>

A NULL value is left out of its row (so a missing column is not the same as an
empty one), element names starting with a digit get a ``_`` prefix, and empty
tables are left out. ``.lsa`` is a zip of the survey's ``.lss`` with its
``.lsr``, ``.lst`` and ``.lsi`` files. The tab-separated ``.txt`` structure
export is read by ``read_tsv``.
"""

from __future__ import annotations

import csv
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

Row = dict[str, str | None]


@dataclass
class Document:
    """One LimeSurvey XML export."""

    doctype: str
    db_version: int | None
    languages: list[str] = field(default_factory=list)
    #: table name -> rows; a missing key in a row is NULL
    tables: dict[str, list[Row]] = field(default_factory=dict)
    #: table name -> declared field names (in file order)
    fields: dict[str, list[str]] = field(default_factory=dict)

    def rows(self, name: str) -> list[Row]:
        return self.tables.get(name, [])


@dataclass
class Archive:
    """A survey archive (``.lsa``): the structure and, when exported, its data."""

    survey: Document
    responses: Document | None = None
    tokens: Document | None = None
    timings: Document | None = None
    names: dict[str, str] = field(default_factory=dict)


class NotLimeSurvey(ValueError):
    pass


def _name(tag: str) -> str:
    """Undo the writer's renaming: ``_123X4X5`` -> ``123X4X5``."""
    return tag[1:] if tag.startswith("_") and tag[1:2].isdigit() else tag


def parse_document(data: bytes | str) -> Document:
    """Parse one XML export. Raises ``NotLimeSurvey`` for anything else."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise NotLimeSurvey(f"not XML: {e}") from e
    if root.tag != "document":
        raise NotLimeSurvey(f"root element is <{root.tag}>, not <document>")
    doctype = (root.findtext("LimeSurveyDocType") or "").strip()
    if not doctype:
        raise NotLimeSurvey("no <LimeSurveyDocType>")
    version = (root.findtext("DBVersion") or "").strip()
    doc = Document(doctype=doctype, db_version=int(version) if version.isdigit() else None)
    langs = root.find("languages")
    if langs is not None:
        doc.languages = [(x.text or "").strip() for x in langs if (x.text or "").strip()]
    for child in root:
        if child.tag in ("LimeSurveyDocType", "DBVersion", "languages"):
            continue
        rows_el = child.find("rows")
        fields_el = child.find("fields")
        if rows_el is None:
            continue
        if fields_el is not None:
            doc.fields[child.tag] = [_name(f.text or "") for f in fields_el]
        rows: list[Row] = []
        for row in rows_el:
            rows.append({_name(c.tag): (c.text if c.text is not None else "") for c in row})
        doc.tables[child.tag] = rows
    return doc


def read_bytes(source: str | Path | IO[bytes] | IO[str] | bytes) -> tuple[bytes, str]:
    """(content, file name) for a path, an open file or raw bytes."""
    if isinstance(source, bytes):
        return source, ""
    if isinstance(source, (str, Path)):
        p = Path(source)
        return p.read_bytes(), p.name
    data = source.read()
    name = getattr(source, "name", "") or ""
    return (data.encode("utf-8") if isinstance(data, str) else data), str(name)


def read_archive(data: bytes) -> Archive:
    """Open an ``.lsa`` (zip) archive."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise NotLimeSurvey(f"not a survey archive: {e}") from e
    found: dict[str, Document] = {}
    names: dict[str, str] = {}
    for info in zf.infolist():
        ext = Path(info.filename).suffix.lower()
        if ext not in (".lss", ".lsr", ".lst", ".lsi"):
            continue
        found[ext] = parse_document(zf.read(info))
        names[ext] = info.filename
    if ".lss" not in found:
        raise NotLimeSurvey("the archive holds no .lss survey structure")
    return Archive(
        survey=found[".lss"],
        responses=found.get(".lsr"),
        tokens=found.get(".lst"),
        timings=found.get(".lsi"),
        names=names,
    )


# --------------------------------------------------------------------------- .txt


#: TSV row classes -> the table they become
_TSV_CLASSES = {
    "S": "surveys",
    "SL": "surveys_languagesettings",
    "G": "groups",
    "Q": "questions",
    "SQ": "subquestions",
    "A": "answers",
    "C": "conditions",
    "AS": "assessments",
    "QTA": "quota",
    "QTAM": "quota_members",
    "QTALS": "quota_languagesettings",
}

_TSV_FIXED = (
    "id",
    "related_id",
    "class",
    "type/scale",
    "name",
    "relevance",
    "text",
    "help",
    "language",
    "validation",
    "mandatory",
    "encrypted",
    "other",
    "default",
    "same_default",
    "question_theme_name",
    "same_script",
)


def read_tsv(text: str) -> Document:
    """The tab-separated survey structure (``tsvSurveyExport``) as a ``Document`` in
    the legacy (one row per language) shape, ``doctype`` ``Survey``.

    Rows follow each other: a ``G`` row opens a group, ``Q`` a question in it, and
    ``SQ`` / ``A`` rows belong to the last question. ``S`` / ``SL`` rows are
    survey settings (``name`` -> ``text``). Question attribute columns follow the
    fixed ones. Ids are kept when present, else numbered in order.
    """
    reader = csv.reader(io.StringIO(text.lstrip("﻿")), delimiter="\t", quoting=csv.QUOTE_NONE)
    header = next(reader, None)
    if not header or "class" not in header:
        raise NotLimeSurvey("no 'class' column: not a LimeSurvey tab-separated export")
    col = {h: i for i, h in enumerate(header)}
    attr_cols = [h for h in header if h not in _TSV_FIXED]
    doc = Document(doctype="Survey", db_version=None)
    survey: Row = {}
    settings: dict[str, Row] = {}
    tables: dict[str, list[Row]] = {}
    gid = qid = parent = 0
    next_id = 1000000
    sortorder = 0
    order: dict[str, int] = {}
    base_lang = ""

    def cell(r: list[str], name: str) -> str:
        i = col.get(name)
        return r[i] if i is not None and i < len(r) else ""

    for r in reader:
        if not r or not any(r):
            continue
        cls = cell(r, "class")
        lang = cell(r, "language")
        name = cell(r, "name")
        if cls == "S":
            survey[name] = cell(r, "text")
            continue
        if cls == "SL":
            settings.setdefault(lang, {"surveyls_language": lang})[name] = cell(r, "text")
            continue
        rid = cell(r, "id")
        if cls == "G":
            if rid.isdigit():
                gid = int(rid)
            elif not lang or lang == base_lang or not base_lang:
                next_id += 1
                gid = next_id
            base_lang = base_lang or lang
            key = f"G{gid}"
            order.setdefault(key, len([k for k in order if k.startswith("G")]))
            tables.setdefault("groups", []).append(
                {
                    "gid": str(gid),
                    "group_name": name,
                    "group_order": str(order[key]),
                    "description": cell(r, "text"),
                    "language": lang,
                    "randomization_group": cell(r, "type/scale"),
                    "grelevance": cell(r, "relevance"),
                }
            )
        elif cls in ("Q", "SQ"):
            if rid.isdigit():
                this = int(rid)
            elif not lang or lang == base_lang:
                next_id += 1
                this = next_id
            else:  # another language of the row with the same name
                this = _find_id(tables, "questions" if cls == "Q" else "subquestions", name, qid)
            if cls == "Q":
                qid = parent = this
                sortorder += 1
            row: Row = {
                "qid": str(this),
                "parent_qid": "0" if cls == "Q" else str(parent),
                "gid": str(gid),
                "type": cell(r, "type/scale") if cls == "Q" else "",
                "title": name,
                "question": cell(r, "text"),
                "help": cell(r, "help"),
                "preg": cell(r, "validation"),
                "other": cell(r, "other") or "N",
                "mandatory": cell(r, "mandatory") or "N",
                "question_order": str(sortorder),
                "language": lang,
                "scale_id": cell(r, "type/scale") if cls == "SQ" else "0",
                "same_default": cell(r, "same_default") or "0",
                "relevance": cell(r, "relevance"),
                "question_theme_name": cell(r, "question_theme_name"),
            }
            if cls == "SQ":
                sortorder += 1
                row["question_order"] = str(sortorder)
            tables.setdefault("questions" if cls == "Q" else "subquestions", []).append(row)
            default = cell(r, "default")
            if default and cls == "Q":
                tables.setdefault("defaultvalues", []).append(
                    {
                        "qid": str(this),
                        "sqid": "0",
                        "scale_id": "0",
                        "language": lang,
                        "specialtype": "",
                        "defaultvalue": default,
                    }
                )
            if cls == "Q":
                for a in attr_cols:
                    v = cell(r, a)
                    if v != "":
                        tables.setdefault("question_attributes", []).append(
                            {"qid": str(this), "attribute": a, "value": v, "language": lang}
                        )
        elif cls == "A":
            sortorder += 1
            tables.setdefault("answers", []).append(
                {
                    "qid": str(qid),
                    "code": name,
                    "answer": cell(r, "text"),
                    "sortorder": str(sortorder),
                    "assessment_value": cell(r, "relevance") or "0",
                    "language": lang,
                    "scale_id": cell(r, "type/scale") or "0",
                }
            )
        elif cls in _TSV_CLASSES:
            tables.setdefault(_TSV_CLASSES[cls], []).append(
                {h: (r[i] if i < len(r) else "") for h, i in col.items()}
            )
    survey.setdefault("sid", survey.get("sid") or "0")
    tables["surveys"] = [survey]
    tables["surveys_languagesettings"] = list(settings.values())
    doc.tables = tables
    langs = [survey.get("language") or base_lang]
    langs += (survey.get("additional_languages") or "").split()
    doc.languages = [x for x in langs if x]
    return doc


def _find_id(tables: dict[str, list[Row]], table: str, name: str, fallback: int) -> int:
    for row in reversed(tables.get(table, [])):
        if row.get("title") == name:
            return int(row["qid"] or fallback)
    return fallback


def sniff(data: bytes) -> str:
    """``xml``, ``zip`` or ``tsv``."""
    if data[:2] == b"PK":
        return "zip"
    head = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n")
    if head.startswith(b"<"):
        return "xml"
    first = head.split(b"\n", 1)[0]
    if b"\t" in first and re.search(rb"\bclass\b", first):
        return "tsv"
    return "unknown"


def coerce(value: Any) -> str:
    return "" if value is None else str(value)
