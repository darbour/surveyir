"""Legacy LimeSurvey conditions -> an ExpressionScript relevance equation.

Before relevance equations (and still in exports that carry a ``conditions``
table), a question's display rule was a list of condition rows. LimeSurvey turns
them into a relevance equation with ``LimeExpressionManager::
ConvertConditionsToRelevance``; this is the same algorithm, so the equation is
the one LimeSurvey itself would run:

* rows are taken in (qid, scenario, cqid, cfieldname, value) order;
* scenarios are ORed; within a scenario, rows about different questions (or
  different subquestions) are ANDed, and rows about the same one are ORed;
* a value ``" "`` means "not answered" (``is_empty``), ``RX`` is a regular
  expression (``regexMatch``), ``@SGQA@`` and ``{...}`` values are references,
  and numeric comparisons are guarded with ``!is_empty``;
* ``{TOKEN:ATTR}`` fields test participant attributes, and ``+`` prefixed fields
  (one multiple-choice option) are always ANDed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from .xmlfile import Row


def _int(v: str | None) -> int:
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return 0


def _float_str(value: str) -> bool:
    """PHP ``(string)(float)$value === (string)$value``."""
    try:
        f = float(value)
    except ValueError:
        return False
    if f.is_integer() and abs(f) < 1e15:
        return str(int(f)) == value
    return repr(f) == value


def convert(
    rows: list[Row],
    *,
    qtype: Mapping[str, str],
    qtitle: Mapping[str, str],
    qother: Mapping[str, bool],
    sgqa_to_code: Mapping[str, str],
    code_to_sgqa: Mapping[str, str],
) -> dict[str, str]:
    """qid -> relevance equation, for every question that has condition rows.

    ``qtype`` / ``qtitle`` / ``qother`` describe questions by qid; ``sgqa_to_code``
    maps a field name (``12345X6X78SQ001``) to its variable name (``Q1_SQ001``).
    """
    rows = sorted(
        rows,
        key=lambda r: (
            _int(r.get("qid")),
            _int(r.get("scenario")),
            _int(r.get("cqid")),
            r.get("cfieldname") or "",
            r.get("value") or "",
        ),
    )
    out: dict[str, str] = {}
    qid_ = scenario_ = cqid_ = None
    subqid_: str | None = None
    and_list: list[str] = []
    or_list: list[str] = []
    scenarios: list[str] = []
    previous: Row | None = None

    def flush_question() -> None:
        nonlocal and_list, or_list, scenarios
        if or_list:
            and_list.append("(" + " or ".join(or_list) + ")")
        if and_list:
            scenarios.append("(" + " and ".join(and_list) + ")")
        if qid_ is not None:
            out[qid_] = " or ".join(scenarios)

    for row in rows:
        qid = row.get("qid") or ""
        scenario = row.get("scenario") or "1"
        cqid = row.get("cqid") or "0"
        method = (row.get("method") or "").strip()
        cfield = row.get("cfieldname") or ""
        value = row.get("value") or ""
        if qid != qid_:
            if qid_ is not None:
                flush_question()
            qid_, scenario_, cqid_, subqid_ = qid, scenario, cqid, None
            and_list, or_list, scenarios = [], [], []
        if scenario != scenario_:
            if or_list:
                and_list.append("(" + " or ".join(or_list) + ")")
            scenarios.append("(" + " and ".join(and_list) + ")")
            and_list, or_list = [], []
            scenario_, cqid_, subqid_ = scenario, cqid, None
        if cqid != cqid_:
            and_list.append("(" + " or ".join(or_list) + ")")
            or_list = []
            cqid_, subqid_ = cqid, None
        ctype = qtype.get(cqid, "") if cqid != "0" else ""
        if ctype == "" and re.fullmatch(r"\{.+\}", cfield):
            fieldname = cfield[1:-1]
            subqid = fieldname
        elif ctype in ("M", "P"):
            if cfield.startswith("+"):
                name = sgqa_to_code.get(cfield[1:], cfield[1:])
                fieldname = f"{name}.NAOK"
                subqid = fieldname
            elif len(cfield) > 5 and cfield.endswith("other") and qother.get(cqid):
                fieldname = f"{cfield}.NAOK"
                subqid = fieldname
            else:
                code = f"{qtitle.get(cqid, '')}_{value}"
                sgqa = code_to_sgqa.get(code, code)
                fieldname = f"{sgqa}.NAOK"
                subqid = sgqa_to_code.get(cfield, cfield)
                value = "Y"
        else:
            name = sgqa_to_code.get(cfield, cfield)
            fieldname = f"{name}.NAOK"
            subqid = fieldname
        if subqid_ is not None and subqid_ != subqid:
            and_list.append("(" + " or ".join(or_list) + ")")
            or_list = []
        subqid_ = subqid

        if (
            re.fullmatch(r"@Q\d+.*@", value)
            or re.fullmatch(r"@[0-9]+X[0-9]+X.*@", value)
            or re.fullmatch(r"\{.+\}", value)
        ):
            value = value[1:-1]
        elif method == "RX":
            if not re.fullmatch(r"/.*/", value, re.S):
                value = f'"/{value}/"'
        elif not _float_str(value):
            value = f'"{value}"'

        if method == "RX":
            or_list.append(f"regexMatch({value},{fieldname})")
        elif value in ('" "', '""'):
            if method == "==":
                or_list.append(f"is_empty({fieldname})")
            elif method == "!=":
                or_list.append(f"!is_empty({fieldname})")
            else:
                or_list.append(f"{fieldname} {method} {value}")
        elif value == '"0"' or not re.fullmatch(r'".+"', value, re.S):
            if method in ("==", "<", "<=", ">="):
                or_list.append(f"(!is_empty({fieldname}) && ({fieldname} {method} {value}))")
            elif method == "!=":
                or_list.append(f"(is_empty({fieldname}) || ({fieldname} != {value}))")
            else:
                or_list.append(f"{fieldname} {method} {value}")
        elif method in ("<", "<="):
            or_list.append(f"(!is_empty({fieldname}) && ({fieldname} {method} {value}))")
        else:
            or_list.append(f"{fieldname} {method} {value}")

        prev_token = previous is not None and re.fullmatch(
            r"\{TOKEN:[^}]*\}", previous.get("cfieldname") or ""
        )
        if (cqid == "0" and re.fullmatch(r"\{TOKEN:[^}]*\}", cfield) and prev_token) or (
            cfield.startswith("+")
        ):
            cqid_ = "-1"
        previous = row
    if qid_ is not None:
        flush_question()
    return out
