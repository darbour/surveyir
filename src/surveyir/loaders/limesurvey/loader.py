"""Load LimeSurvey surveys (.lss, .lsa, .lsg, .lsq, tab-separated .txt) into a ``Survey``.

How a LimeSurvey survey becomes the IR:

* **Groups** become blocks (``G<gid>``), in group order. The survey's format
  decides the pages: group by group (``G``, the default) puts each group on one
  page, question by question (``S``) puts each question on its own page, and all
  in one (``A``) is administered group by group (a ``limesurvey-all-in-one``
  diagnostic says so).
* **Questions** keep their codes (``title``) as ids and export tags; see
  ``questions`` for the type mapping. Their help text is shown under the
  question, as LimeSurvey shows it.
* **Relevance** (ExpressionScript, or legacy conditions converted the way
  LimeSurvey converts them) becomes display logic, lowered to comparisons where
  that is exact (``expressions.lower``). A question whose relevance reads a
  question on the same page is shown or hidden as that page is answered
  (``in_page_display_logic``). Subquestion relevance and array filters become
  choice display logic. Group relevance becomes a flow branch around the block.
* **Randomization**: groups sharing a randomization group are shuffled in the
  flow, questions sharing a ``random_group`` are shuffled among their own
  positions within the group, and ``random_order`` / ``answer_order`` shuffle or
  sort options.
* **Quotas** become ``Quota`` conditions (a response counts when, for each
  question in the quota, its answer is one of the quota's codes), checked after
  each page that holds a quota question.
* **Participant attributes** (``TOKEN:ATTR``) read by logic or text are declared
  at the start of the flow as recipient fields, so a run that does not supply
  them records it.
* Everything else (survey settings, question attributes, assessments, label
  sets, themes, ...) is kept verbatim in ``extras``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import IO, Any

from ...model import (
    And,
    Block,
    BlockNode,
    BranchNode,
    Comparison,
    Condition,
    EmbeddedDataNode,
    EmbeddedField,
    Expression,
    FlowNode,
    Operand,
    Or,
    PageBreak,
    QuestionRef,
    Quota,
    QuotaNode,
    Randomization,
    RandomizerNode,
    SourceInfo,
    Survey,
    Var,
    iter_comparisons,
    iter_expressions,
    iter_nodes,
)
from ...model.text import Text
from ..qualtrics._util import Diagnostics
from ..qualtrics.loader import LoadError
from . import conditions as cond_mod
from . import expressions as ex
from .questions import TYPES, Context, build
from .tables import GRow, QRow, Structure, normalize
from .xmlfile import (
    Archive,
    Document,
    NotLimeSurvey,
    parse_document,
    read_archive,
    read_bytes,
    read_tsv,
    sniff,
)

logger = logging.getLogger(__name__)

#: warning codes that describe the source file, not a loss: strict mode tolerates them
SOURCE_DEFECTS = frozenset(
    {
        "limesurvey-duplicate-code",
        "limesurvey-invalid-expression",
        "limesurvey-missing-group",
        "limesurvey-orphan-subquestion",
        "limesurvey-unknown-variable",
    }
)


def _loader_id() -> str:
    try:
        return f"surveyir.loaders.limesurvey {version('surveyir')}"
    except PackageNotFoundError:  # pragma: no cover
        return "surveyir.loaders.limesurvey"


def _on(v: Any) -> bool:
    return str(v or "").strip().lower() in ("1", "y", "yes", "true", "on")


class LimeSurveyLoader:
    """Loads LimeSurvey survey structure exports.

    ``.lss`` (survey), ``.lsa`` (survey archive: the structure, plus responses
    that ``limesurvey_responses`` reads), ``.lsg`` (one group), ``.lsq`` (one
    question) and the tab-separated ``.txt`` export (only with
    ``format="limesurvey"``, since ``.txt`` is ambiguous).
    """

    name = "limesurvey"
    extensions = (".lss", ".lsa", ".lsg", ".lsq")

    def __init__(self, strict: bool = False, language: str | None = None) -> None:
        self.strict = strict
        self.language = language

    def load(self, source: str | Path | IO[bytes] | IO[str] | bytes) -> Survey:
        data, name = read_bytes(source)
        kind = sniff(data)
        archive: Archive | None = None
        try:
            if kind == "zip":
                archive = read_archive(data)
                doc = archive.survey
            elif kind == "xml":
                doc = parse_document(data)
            elif kind == "tsv":
                doc = read_tsv(data.decode("utf-8-sig", errors="replace"))
            else:
                raise NotLimeSurvey("not a LimeSurvey export (XML, archive or tab-separated)")
        except NotLimeSurvey as e:
            raise LoadError(f"{name or 'input'}: {e}") from e
        survey = self.load_document(doc, file_name=name)
        if archive is not None:
            survey.extras["archive"] = {
                "files": archive.names,
                "responses": len(archive.responses.rows("responses")) if archive.responses else 0,
            }
        return survey

    def load_document(self, doc: Document, *, file_name: str = "") -> Survey:
        if doc.doctype not in ("Survey", "Group", "Question"):
            raise LoadError(
                f"{file_name or 'input'} is a LimeSurvey {doc.doctype} export, not a survey"
            )
        diags = Diagnostics()
        survey = _Builder(normalize(doc), doc, diags, self.language).build()
        if self.strict:
            bad = [
                d
                for d in diags.items
                if d.level == "error" or (d.level == "warning" and d.code not in SOURCE_DEFECTS)
            ]
            if bad:
                raise LoadError(
                    f"{len(bad)} problem(s) loading {file_name or 'survey'}: "
                    + "; ".join(f"[{d.code}] {d.message}" for d in bad[:5]),
                    diagnostics=diags.items,
                )
        return survey


def load_lss(source: str | Path | IO[bytes] | IO[str] | bytes, **options: Any) -> Survey:
    """Load a LimeSurvey export (``.lss``, ``.lsa``, ``.lsg``, ``.lsq`` or ``.txt``)."""
    return LimeSurveyLoader(**options).load(source)


# --------------------------------------------------------------------------- builder


class _Builder:
    def __init__(
        self, st: Structure, doc: Document, diags: Diagnostics, language: str | None
    ) -> None:
        self.st, self.doc, self.d = st, doc, diags
        self.lang = language if language in st.languages else st.languages[0]
        self.sid = st.sid or "0"
        self.fmt = (st.survey.get("format") or "G").upper()
        self.subs: dict[str, list[QRow]] = {}
        for s in st.subquestions:
            self.subs.setdefault(s.parent_qid, []).append(s)
        self.answers: dict[str, list] = {}
        for a in st.answers:
            self.answers.setdefault(a.qid, []).append(a)
        self.by_qid = {q.qid: q for q in st.questions}
        self.ids: dict[str, str] = {}  # qid -> IR question id (its code)
        self.names: dict[str, ex.Target] = {}
        self.sgqa: dict[str, str] = {}  # field name -> variable name
        self.participant: dict[str, None] = {}
        self.shapes: dict[str, str] = {}
        #: question id -> its variable names in field order, with their parts
        self.variables: dict[str, list[tuple[str, str | None]]] = {}
        self.statics: dict[str, dict[str, str]] = {}  # question id -> QID, GID, ...
        self.group_names: dict[str, str] = {}

    # ---------------------------------------------------------------- names

    def _ids(self) -> None:
        seen: set[str] = set()
        for q in self.st.questions:
            code = q.title or f"q{q.qid}"
            if code in seen:
                self.d.warn(
                    "limesurvey-duplicate-code",
                    f"question code {code!r} is used twice; the second is {code}_{q.qid}",
                    q.qid,
                )
                code = f"{code}_{q.qid}"
            seen.add(code)
            self.ids[q.qid] = code

    def _names(self) -> None:
        for q in self.st.questions:
            code = self.ids[q.qid]
            base = f"{self.sid}X{q.gid}X{q.qid}"
            t = q.type
            subs = self.subs.get(q.qid, [])
            s0 = [s.title for s in subs if s.scale_id == 0]
            s1 = [s.title for s in subs if s.scale_id == 1]
            other = (q.row.get("other") or "N").upper() == "Y"

            def add(name: str, field: str, part: str | None, code: str = code) -> None:
                target = ex.Target(code, part)
                self.names[name] = target
                self.names[field] = target
                self.sgqa[field] = name
                self.variables.setdefault(code, []).append((name, part))

            if t in ("M", "P"):
                for c in s0:
                    add(f"{code}_{c}", f"{base}{c}", c)
                    if t == "P":
                        add(f"{code}_{c}comment", f"{base}{c}comment", f"{c}comment")
                if other:
                    add(f"{code}_other", f"{base}other", "other")
                    if t == "P":
                        add(f"{code}_othercomment", f"{base}othercomment", "othercomment")
            elif t in ("F", "H", "A", "B", "C", "E", "K", "Q"):
                for c in s0:
                    add(f"{code}_{c}", f"{base}{c}", c)
            elif t == "1":  # variables Q_SQ_0 / Q_SQ_1; fields 123X4X5SQ#0 / #1
                for c in s0:
                    for k in ("0", "1"):
                        add(f"{code}_{c}_{k}", f"{base}{c}#{k}", f"{c}#{k}")
            elif t in (":", ";"):
                for r in s0:
                    for c in s1:
                        add(f"{code}_{r}_{c}", f"{base}{r}_{c}", f"{r}_{c}")
            elif t == "R":
                n = len(s0) or len([a for a in self.answers.get(q.qid, []) if a.scale_id == 0])
                limit = self._attrs(q).get("max_subquestions") or self._attrs(q).get("max_answers")
                if isinstance(limit, str) and limit.isdigit() and int(limit) > 0:
                    n = min(n, int(limit))
                for i in range(1, n + 1):
                    add(f"{code}_{i}", f"{base}{i}", str(i))
            else:
                add(code, base, None)
                if other and t in ("L", "!"):
                    add(f"{code}_other", f"{base}other", "other")
                if t == "O":
                    add(f"{code}_comment", f"{base}comment", "comment")
                if t == "|":
                    add(f"{code}_filecount", f"{base}_filecount", "filecount")
            self.names.setdefault(code, ex.Target(code, None))
            self.statics[code] = {
                "QID": q.qid,
                "GID": q.gid,
                "SGQ": base,
                "QUESTION_CODE": code,
                "GROUPNAME": self.group_names.get(q.gid, ""),
            }

    def scope(self, owner: str | None) -> ex.Scope:
        return ex.Scope(self.names, self.variables, owner, self.statics.get(owner or "", {}))

    def _attrs(self, q: QRow) -> dict[str, Any]:
        return dict(self.st.attributes.get(q.qid, {}))

    # ---------------------------------------------------------------- text / logic

    def text(self, markup: str | None, owner: str | None) -> Text:
        def bad(part: str, why: str) -> None:
            self.d.info(
                "limesurvey-literal-braces",
                f"{part[:60]!r} is not an expression ({why}); shown as written",
                owner,
            )

        out = ex.make_text(markup, self.scope(owner), on_error=bad)
        self._note_vars([p.expression for p in out.pipes if p.expression is not None], owner)
        return out

    def _note_vars(self, trees: list[Any], where: str | None) -> None:
        for tree in trees:
            for n in iter_nodes(tree):
                if isinstance(n, Var):
                    if n.kind == "participant" and n.field:
                        self.participant.setdefault(n.field, None)
                    elif n.kind == "unknown":
                        self.d.warn(
                            "limesurvey-unknown-variable",
                            f"{n.name!r} names no question of this survey",
                            where,
                        )

    def condition(self, source: str | None, owner: str | None, where: str) -> Condition | None:
        """A relevance (or validation) equation as a condition; None if always true."""
        src = (source or "").strip()
        if src in ("", "1"):
            return None
        try:
            tree = ex.compile_expression(src, self.scope(owner))
        except ex.ExpressionError as e:
            self.d.warn(
                "limesurvey-invalid-expression",
                f"{src!r} does not parse ({e}); LimeSurvey treats it as false",
                where,
            )
            return Expression(language="expression_script", source=src, tree=None, location=where)
        self._note_vars([tree], where)
        return ex.lower(tree, src, self.shapes, location=where)

    # ---------------------------------------------------------------- build

    def build(self) -> Survey:
        st = self.st
        for g in st.groups:
            t = g.texts.get(self.lang) or next(iter(g.texts.values()), {}) if g.texts else {}
            self.group_names[g.gid] = (t or {}).get("group_name", "")
        self._ids()
        self._names()
        settings = st.settings.get(self.lang) or next(iter(st.settings.values()), {})
        legacy = cond_mod.convert(
            st.conditions,
            qtype={q.qid: q.type for q in st.questions},
            qtitle={q.qid: self.ids.get(q.qid, q.title) for q in st.questions},
            qother={q.qid: (q.row.get("other") or "N").upper() == "Y" for q in st.questions},
            sgqa_to_code=self.sgqa,
            code_to_sgqa={v: k for k, v in self.sgqa.items()},
        )
        show_no_answer = (st.survey.get("shownoanswer") or "Y").upper() != "N"

        # questions, by group
        questions: dict[str, Any] = {}
        by_group: dict[str, list[QRow]] = {}
        for q in st.questions:
            by_group.setdefault(q.gid, []).append(q)
            kind = TYPES.get(q.type, ("unsupported", ""))[0]
            self.shapes[self.ids[q.qid]] = _shape(q.type, kind)
        for q in st.questions:
            attrs = self._attrs(q)
            ctx = Context(
                lang=self.lang,
                languages=[self.lang] + [x for x in st.languages if x != self.lang],
                text=self.text,
                attrs=attrs,
                subquestions=self.subs.get(q.qid, []),
                answers=self.answers.get(q.qid, []),
                show_no_answer=show_no_answer,
                survey_languages=st.languages,
            )
            other = (q.row.get("other") or "N").upper() == "Y"
            iq = build(q, ctx, other=other)
            if iq.kind == "unsupported":
                self.d.warn(
                    "unsupported-question",
                    f"question type {q.type!r} is not modeled; kept as unsupported",
                    q.qid,
                )
            iq.extras.update(
                {
                    "limesurvey": {
                        "qid": q.qid,
                        "gid": q.gid,
                        "sgqa": f"{self.sid}X{q.gid}X{q.qid}",
                        "relevance": q.row.get("relevance") or "1",
                        "help": _help(q, self.lang),
                    },
                    "attributes": attrs,
                    "row": {
                        k: v
                        for k, v in q.row.items()
                        if k not in ("question", "help", "script", "language", "title", "type")
                    },
                }
            )
            iq.default_value = self._default(q, iq)
            questions[iq.id] = (q, iq, legacy.get(q.qid))

        # groups -> blocks
        groups = list(st.groups)
        known = {g.gid for g in groups}
        for gid in by_group:
            if gid not in known:
                if self.doc.doctype == "Survey":
                    self.d.warn(
                        "limesurvey-missing-group",
                        f"questions refer to group {gid}, which is not in the file; "
                        "they are placed in a group of their own",
                        gid,
                    )
                groups.append(
                    GRow(
                        gid=gid,
                        order=len(groups),
                        randomization_group="",
                        relevance="",
                        row={},
                        texts={},
                    )
                )
        blocks: dict[str, Block] = {}
        page_of: dict[str, str] = {}  # question id -> page key
        for g in groups:
            bid = f"G{g.gid}"
            qs = by_group.get(g.gid, [])
            elements: list[Any] = []
            for i, q in enumerate(qs):
                if i and self.fmt == "S":
                    elements.append(PageBreak())
                elements.append(QuestionRef(question_id=self.ids[q.qid]))
                page_of[self.ids[q.qid]] = f"{bid}/{i}" if self.fmt == "S" else bid
            text = g.texts.get(self.lang) or next(iter(g.texts.values()), {}) if g.texts else {}
            blocks[bid] = Block(
                id=bid,
                description=(text or {}).get("group_name", "") or bid,
                elements=elements,
                randomization=self._question_groups(qs),
                extras={
                    "limesurvey": {"gid": g.gid, "group_order": g.order},
                    "description": (text or {}).get("description", ""),
                    "texts": g.texts,
                    "row": g.row,
                },
            )
        if self.fmt == "A":
            self.d.info(
                "limesurvey-all-in-one",
                "the survey shows all groups on one page; it is administered group by group",
            )

        # relevance, now that pages are known
        out_questions = {}
        for qid, (q, iq, legacy_rel) in questions.items():
            src = legacy_rel if legacy_rel is not None else (q.row.get("relevance") or "")
            if legacy_rel is not None:
                iq.extras["limesurvey"]["relevance_from_conditions"] = legacy_rel
            cond = self.condition(src, iq.id, iq.id)
            if cond is not None:
                same_page = {
                    r for r in _question_refs(cond) if page_of.get(r) == page_of.get(iq.id)
                } - {iq.id}
                if same_page:
                    iq.in_page_display_logic = cond
                else:
                    iq.display_logic = cond
            self._choice_logic(q, iq)
            self._validation_logic(q, iq)
            out_questions[qid] = iq

        flow = self._flow(groups, blocks, by_group)
        quotas = self._quotas()
        if quotas:
            flow = self._quota_checks(flow, quotas, blocks)
        if self.participant:
            flow.insert(
                0,
                EmbeddedDataNode(
                    id="participant",
                    description="participant attributes read by the survey",
                    fields=[
                        EmbeddedField(name=f"TOKEN:{a}", source="recipient")
                        for a in self.participant
                    ],
                ),
            )
        title = settings.get("surveyls_title") or f"Survey {self.sid}"
        description = Text(
            html=settings.get("surveyls_description") or "",
        )
        survey = Survey(
            id=self.sid,
            name=title,
            description=_plain(description.html) or None,
            language=self.lang,
            languages=[x for x in st.languages if x != self.lang],
            created=st.survey.get("datecreated"),
            modified=st.survey.get("lastmodified"),
            source=SourceInfo(
                format="limesurvey.lss",
                format_version=str(self.doc.db_version) if self.doc.db_version else None,
                loader=_loader_id(),
            ),
            blocks=blocks,
            questions={iq.id: iq for iq in out_questions.values()},
            flow=flow,
            quotas=quotas,
            options=dict(st.survey),
            diagnostics=self.d.items,
            extras={
                "doctype": self.doc.doctype,
                "era": st.era,
                "language_settings": st.settings,
                "assessments": st.assessments,
                "url_parameters": st.url_parameters,
                "tables": st.other_tables,
                "welcome": settings.get("surveyls_welcometext") or "",
                "end": settings.get("surveyls_endtext") or "",
                "end_url": settings.get("surveyls_url") or "",
            },
        )
        return survey

    # ---------------------------------------------------------------- pieces

    def _question_groups(self, qs: list[QRow]) -> Randomization | None:
        """Questions sharing a ``random_group`` are shuffled among their own positions."""
        groups: dict[str, list[str]] = {}
        for q in qs:
            name = self.st.attributes.get(q.qid, {}).get("random_group")
            if isinstance(name, str) and name.strip():
                groups.setdefault(name.strip(), []).append(self.ids[q.qid])
        pools = [ids for ids in groups.values() if len(ids) > 1]
        if not pools:
            return None
        return Randomization(
            mode="advanced",
            randomized=pools[0],
            groups=pools[1:],
            per_page=1 if self.fmt == "S" else None,
        )

    def _choice_logic(self, q: QRow, iq: Any) -> None:
        items = (
            getattr(iq, "choices", None)
            or getattr(iq, "rows", None)
            or getattr(iq, "items", None)
            or getattr(iq, "fields", None)
            or []
        )
        attrs = self.st.attributes.get(q.qid, {})
        filters = [
            (src.strip(), False)
            for src in str(attrs.get("array_filter") or "").split(";")
            if src.strip()
        ] + [
            (src.strip(), True)
            for src in str(attrs.get("array_filter_exclude") or "").split(";")
            if src.strip()
        ]
        for c in items:
            terms: list[Condition] = []
            rel = (c.extras.get("limesurvey") or {}).get("relevance")
            if rel:
                cond = self.condition(rel, iq.id, f"{iq.id}/{c.id}")
                if cond is not None:
                    terms.append(cond)
            for src, exclude in filters:
                t = self._filter(src, c.id, exclude)
                if t is not None:
                    terms.append(t)
            if terms:
                c.display_logic = terms[0] if len(terms) == 1 else And(terms=terms)

    def _filter(self, source: str, code: str, exclude: bool) -> Condition | None:
        """``array_filter``: show the option only if the same code was chosen in ``source``."""
        target = self.names.get(f"{source}_{code}")
        shape = self.shapes.get(source, "")
        if target is None:
            return None
        if shape == "choice/multiple":
            return Comparison(
                left=Operand(
                    kind="question", question_id=source, choice_id=code, raw=f"{source}_{code}"
                ),
                operator="not_selected" if exclude else "selected",
                source_operator="array_filter_exclude" if exclude else "array_filter",
            )
        src = f"{'' if exclude else '!'}is_empty({source}_{code}.NAOK)"
        return self.condition(src, None, f"array_filter {source}")

    def _validation_logic(self, q: QRow, iq: Any) -> None:
        attrs = self.st.attributes.get(q.qid, {})
        v = iq.validation
        sources = {
            "expression": [attrs.get("em_validation_q")],
            "item_expression": [attrs.get("em_validation_sq")],
        }
        if q.type == "K":  # sum limits, as LimeSurvey generates them
            checks = [
                f"sum(self.NAOK) {op} {attrs[name]}"
                for name, op in (
                    ("min_num_value", ">="),
                    ("max_num_value", "<="),
                    ("equals_num_value", "=="),
                )
                if _number_attr(attrs.get(name))
            ]
            if checks:
                rule = " and ".join(checks)
                if v is not None and not v.custom_message or v is None:
                    tip = _sum_tip(attrs)
                    if v is None:
                        from ...model import Validation

                        v = iq.validation = Validation()
                    v.custom_message = tip
                if _on(attrs.get("value_range_allows_missing")):
                    rule = f"count(self.NAOK) == 0 or ({rule})"
                sources["expression"].append(rule)
        for field, parts in sources.items():
            parts = [x for x in parts if isinstance(x, str) and x.strip()]
            if not parts:
                continue
            src = " and ".join(f"({x})" for x in parts) if len(parts) > 1 else parts[0]
            name = "em_validation_q" if field == "expression" else "em_validation_sq"
            cond = self.condition(src, iq.id, f"{iq.id}/{name}")
            if cond is None:
                continue
            if v is None:
                from ...model import Validation

                v = iq.validation = Validation()
            setattr(v, field, cond)

    def _default(self, q: QRow, iq: Any) -> Any:
        rows = {k: v for k, v in self.st.defaults.items() if k[0] == q.qid}
        if not rows:
            return None

        def value(texts: Mapping[str, str]) -> str:
            return texts.get(self.lang) or next(iter(texts.values()), "")

        kind = iq.kind
        if kind == "choice" and getattr(iq, "multiple", False):
            subs = {s.qid: s.title for s in self.subs.get(q.qid, [])}
            return [subs[k[1]] for k, t in rows.items() if k[1] in subs and value(t) == "Y"]
        if kind in ("matrix", "text_entry") and any(k[1] not in ("0", "") for k in rows):
            subs = {s.qid: s.title for s in self.subs.get(q.qid, [])}
            return {subs.get(k[1], k[1]): value(t) for k, t in rows.items()}
        first = next(iter(rows.values()))
        return value(first) or None

    def _flow(
        self, groups: list[Any], blocks: dict[str, Block], by_group: dict[str, list[QRow]]
    ) -> list[FlowNode]:
        nodes: list[tuple[str, FlowNode]] = []
        for g in groups:
            bid = f"G{g.gid}"
            node: FlowNode = BlockNode(id=f"FL_{bid}", block_id=bid)
            cond = self.condition(g.relevance, None, bid)
            if cond is not None:
                node = BranchNode(
                    id=f"FL_{bid}_relevance",
                    description=f"group relevance: {g.relevance}",
                    condition=cond,
                    children=[node],
                )
            nodes.append((g.randomization_group or "", node))
        # randomization groups: members shuffle among themselves
        out: list[FlowNode] = []
        placed: set[str] = set()
        for rgroup, node in nodes:
            if not rgroup:
                out.append(node)
                continue
            if rgroup in placed:
                continue
            members = [n for r, n in nodes if r == rgroup]
            positions = [j for j, (r, _) in enumerate(nodes) if r == rgroup]
            if positions != list(range(positions[0], positions[0] + len(positions))):
                self.d.warn(
                    "limesurvey-randomization-group-split",
                    f"groups of randomization group {rgroup!r} are not adjacent; they are "
                    "shuffled as one run at the first group's position",
                    rgroup,
                )
            placed.add(rgroup)
            if len(members) == 1:
                out.append(members[0])
                continue
            out.append(
                RandomizerNode(
                    id=f"FL_random_{rgroup}",
                    description=f"randomization group {rgroup}",
                    children=members,
                )
            )
        return out

    def _quotas(self) -> list[Quota]:
        members: dict[str, dict[str, list[str]]] = {}
        for m in self.st.quota_members:
            members.setdefault(m.get("quota_id") or "", {}).setdefault(
                m.get("qid") or "", []
            ).append(m.get("code") or "")
        out = []
        for row in self.st.quotas:
            qid = row.get("id") or ""
            if (row.get("active") or "1") == "0":
                continue
            terms: list[Condition] = []
            for question_qid, codes in members.get(qid, {}).items():
                code = self.ids.get(question_qid)
                if code is None:
                    continue
                terms.append(self._quota_term(code, codes))
            texts = self.st.quota_texts.get(qid, {})
            action = row.get("action") or "1"
            qlimit = row.get("qlimit") or ""
            condition: Condition | None = None
            if terms:
                condition = terms[0] if len(terms) == 1 else And(terms=terms)
            out.append(
                Quota(
                    id=f"quota_{qid}",
                    name=row.get("name") or f"quota {qid}",
                    limit=int(qlimit) if qlimit.isdigit() else None,
                    condition=condition,
                    action={"1": "end_survey", "2": "allow_change"}.get(action, action),
                    extras={"row": row, "texts": texts},
                )
            )
        return out

    def _quota_term(self, code: str, values: list[str]) -> Condition:
        shape = self.shapes.get(code, "")
        terms: list[Condition] = []
        for v in values:
            if shape == "choice/multiple":
                terms.append(
                    Comparison(
                        left=Operand(kind="question", question_id=code, choice_id=v, raw=code),
                        operator="selected",
                        source_operator="quota",
                    )
                )
            elif shape == "matrix/single" and "_" in v:
                row, col = v.split("_", 1)  # never used by LimeSurvey; kept for safety
                terms.append(
                    Comparison(
                        left=Operand(
                            kind="question",
                            question_id=code,
                            choice_id=row,
                            answer_id=col,
                            raw=code,
                        ),
                        operator="selected",
                        source_operator="quota",
                    )
                )
            else:
                terms.append(
                    Comparison(
                        left=Operand(kind="question", question_id=code, raw=code),
                        operator="equal",
                        source_operator="quota",
                        right=v,
                    )
                )
        return terms[0] if len(terms) == 1 else Or(terms=terms)

    def _quota_checks(
        self, flow: list[FlowNode], quotas: list[Quota], blocks: dict[str, Block]
    ) -> list[FlowNode]:
        """A quota check after every block that holds a quota question."""
        quota_questions: set[str] = set()
        for quota in quotas:
            if quota.condition is not None:
                quota_questions |= {
                    c.left.question_id or "" for c in iter_comparisons(quota.condition)
                }

        def visit(nodes: list[FlowNode]) -> list[FlowNode]:
            out: list[FlowNode] = []
            for node in nodes:
                if isinstance(node, (BranchNode, RandomizerNode)):
                    node = node.model_copy(update={"children": visit(list(node.children))})
                out.append(node)
                if isinstance(node, BlockNode):
                    block = blocks.get(node.block_id)
                    if block is not None and quota_questions & set(block.question_ids):
                        out.append(QuotaNode(id=f"FL_{node.block_id}_quota"))
            return out

        return visit(flow)


def _help(q: QRow, lang: str) -> str:
    t = q.texts.get(lang) or next(iter(q.texts.values()), {}) if q.texts else {}
    return _plain((t or {}).get("help", ""))


def _sum_tip(attrs: Mapping[str, Any]) -> str:
    """The tip LimeSurvey shows for a multiple numerical input's total."""
    lo, hi, eq = (attrs.get(k) for k in ("min_num_value", "max_num_value", "equals_num_value"))
    if _number_attr(eq):
        return f"The sum must equal {eq}."
    if _number_attr(lo) and _number_attr(hi):
        return f"The sum must be between {lo} and {hi}."
    if _number_attr(lo):
        return f"The sum must be at least {lo}."
    return f"The sum must be at most {hi}."


def _number_attr(v: Any) -> bool:
    return isinstance(v, str) and re.fullmatch(r"-?\d+(\.\d+)?", v.strip()) is not None


def _shape(t: str, kind: str) -> str:
    if kind == "choice":
        return "choice/multiple" if t in ("M", "P") else "choice/single"
    if kind == "matrix":
        return "matrix/text" if t in (":", ";") else "matrix/single"
    if kind == "text_entry":
        return "text_entry/form" if t in ("K", "Q") else "text_entry/single"
    return kind


def _question_refs(cond: Condition) -> set[str]:
    out = {c.left.question_id for c in iter_comparisons(cond) if c.left.question_id}
    for e in iter_expressions(cond):
        out |= {
            n.question_id
            for n in iter_nodes(e.tree)
            if isinstance(n, Var) and n.kind in ("question", "self") and n.question_id
        }
    return {x for x in out if x}


def _plain(html: str) -> str:
    from ...text import html_to_text

    return html_to_text(html)


__all__ = ["LimeSurveyLoader", "LoadError", "load_lss"]
