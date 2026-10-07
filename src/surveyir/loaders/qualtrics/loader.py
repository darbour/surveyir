"""Load Qualtrics survey files (.qsf) into a ``Survey``."""

from __future__ import annotations

import json
import logging
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import IO, Any

from ...model import (
    AuthenticatorNode,
    Block,
    BlockNode,
    BranchNode,
    EmbeddedDataNode,
    EmbeddedField,
    EndSurveyNode,
    FlowNode,
    GroupNode,
    LibraryBlockNode,
    LoopAndMerge,
    PageBreak,
    QuestionRef,
    Quota,
    QuotaNode,
    Randomization,
    RandomizerNode,
    ScoringCategory,
    SkipLogic,
    SourceInfo,
    Survey,
    TableOfContentsNode,
    UnsupportedNode,
    WebServiceNode,
)
from ...model.flow import EmbeddedSource, Termination
from ...text import make_text
from . import conjoint
from ._util import Diagnostics, Payload, as_mapping, first_str, to_int, truthy
from .logic import parse_logic, parse_skip_condition
from .questions import build_question, parse_advanced

logger = logging.getLogger(__name__)

#: Warning codes that describe defects in the source file itself. The survey is
#: still loaded faithfully (possibly reconstructed), so strict mode tolerates them.
SOURCE_DEFECTS = frozenset(
    {
        "block-without-id",
        "branch-without-logic",
        "dangling-order",
        "duplicate-element",
        "implicit-block",
        "inferred-element",
        "invalid-logic",
        "library-block",
        "malformed-element",
        "missing-block",
        "missing-loop-source",
        "missing-question",
        "no-flow",
        "question-without-id",
        "question-without-type",
        "unknown-block-element",
    }
)

#: Survey elements mapped to typed IR. Everything else is kept in Survey.extras.
MAPPED_ELEMENTS = {"BL", "SQ", "FL", "SO", "SCO", "QO", "CJ"}

EMBEDDED_SOURCES: dict[str, EmbeddedSource] = {"Custom": "custom", "Recipient": "recipient"}
TERMINATIONS: dict[str, Termination] = {
    "Redirect": "redirect",
    "DisplayMessage": "message",
    "DefaultMessage": "default",
    "": "default",
}


class LoadError(ValueError):
    """Raised in strict mode, or when the input is not a QSF document at all."""

    def __init__(self, message: str, diagnostics: list | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or []


def _loader_id() -> str:
    try:
        return f"surveyir.loaders.qualtrics {version('surveyir')}"
    except PackageNotFoundError:  # pragma: no cover - running from a source tree
        return "surveyir.loaders.qualtrics"


class QualtricsLoader:
    """Loader for Qualtrics ``.qsf`` exports.

    ``strict=True`` raises ``LoadError`` if anything could not be represented:
    any error, or any warning other than a known defect of the source file
    (``SOURCE_DEFECTS``), such as an unsupported question type. Diagnostics are
    always recorded on ``Survey.diagnostics``.
    """

    name = "qualtrics"
    extensions = (".qsf",)

    def __init__(self, *, strict: bool = False) -> None:
        self.strict = strict

    # ---------------------------------------------------------------- entry points

    def load(self, source: str | Path | IO[str] | IO[bytes] | dict[str, Any]) -> Survey:
        if isinstance(source, dict):
            data = source
        elif isinstance(source, (str, Path)):
            with open(source, encoding="utf-8-sig") as f:
                data = json.load(f)
        else:
            data = json.load(source)
        return self.load_data(data)

    def load_data(self, data: dict[str, Any]) -> Survey:
        if not isinstance(data, dict) or "SurveyElements" not in data:
            raise LoadError("Not a QSF document: missing 'SurveyElements'")
        diags = Diagnostics()
        survey = _Builder(data, diags).build()
        for d in survey.diagnostics:
            logger.debug("%s %s [%s] %s", d.level, d.location or "-", d.code, d.message)
        if self.strict:
            problems = [
                d
                for d in survey.diagnostics
                if d.level == "error" or (d.level == "warning" and d.code not in SOURCE_DEFECTS)
            ]
            if problems:
                raise LoadError(
                    f"{len(problems)} problem(s) loading survey; first: {problems[0].message}",
                    problems,
                )
        return survey


def load_qsf(
    source: str | Path | IO[str] | IO[bytes] | dict[str, Any], *, strict: bool = False
) -> Survey:
    """Load a Qualtrics ``.qsf`` file (path, open file, or parsed dict)."""
    return QualtricsLoader(strict=strict).load(source)


# --------------------------------------------------------------------------- builder


def _key_values(value: Any) -> dict[str, str]:
    """Qualtrics key/value lists: [{"key": k, "value": v}, ...] (or a plain dict)."""
    if isinstance(value, list):
        return {
            str(kv.get("key")): "" if kv.get("value") is None else str(kv.get("value"))
            for kv in value
            if isinstance(kv, dict) and kv.get("key") is not None
        }
    return {k: "" if v is None else str(v) for k, v in as_mapping(value).items()}


def _infer_element(el: dict[str, Any]) -> dict[str, Any] | None:
    """Wrap an untagged survey element (written by some third-party tools)."""
    kind = el.get("Type")
    if kind == "Question" and isinstance(el.get("Payload"), dict):
        return {"Element": "SQ", "Payload": el["Payload"]}
    if "BlockElements" in el and "ID" in el:
        return {"Element": "BL", "Payload": el}
    if kind in ("Flow", "Root") and isinstance(el.get("Flow"), list):
        return {"Element": "FL", "Payload": el}
    return None


class _Builder:
    def __init__(self, data: dict[str, Any], diags: Diagnostics) -> None:
        self.data = data
        self.diags = diags
        self.elements: dict[str, list[dict[str, Any]]] = {}
        inferred = 0
        for el in data.get("SurveyElements") or []:
            if not isinstance(el, dict):
                continue
            if "Element" not in el:
                guess = _infer_element(el)
                if guess is not None:
                    el, inferred = guess, inferred + 1
            self.elements.setdefault(str(el.get("Element")), []).append(el)
        if inferred:
            self.diags.warn(
                "inferred-element",
                f"{inferred} survey element(s) had no 'Element' tag; type inferred from shape",
            )

    def first_payload(self, tag: str) -> Any:
        els = self.elements.get(tag) or []
        if len(els) > 1:
            self.diags.warn("duplicate-element", f"{len(els)} '{tag}' elements; using the first")
        return els[0].get("Payload") if els else None

    # ---------------------------------------------------------------- survey

    def build(self) -> Survey:
        entry = Payload(self.data.get("SurveyEntry"))
        blocks, trashed_questions = self.blocks()
        questions = self.questions(blocks, trashed_questions)
        flow, flow_extras = self.flow(blocks)

        options = self.first_payload("SO")
        scoring = self.scoring()
        proj = self.first_payload("PROJ")
        languages = sorted({lang for q in questions.values() for lang in q.translations})

        if self.elements.get("None"):
            self.diags.warn(
                "malformed-element",
                f"{len(self.elements['None'])} survey element(s) without an 'Element' tag",
            )
        unmapped = {
            tag: [el.get("Payload") for el in els]
            for tag, els in self.elements.items()
            if tag not in MAPPED_ELEMENTS
        }
        extras: dict[str, Any] = {}
        if unmapped:
            extras["elements"] = unmapped
        if flow_extras:
            extras["flow"] = flow_extras
        top_rest = {
            k: v for k, v in self.data.items() if k not in {"SurveyEntry", "SurveyElements"}
        }
        if top_rest:
            extras["document"] = top_rest

        survey = Survey(
            id=str(entry.get("SurveyID") or ""),
            name=str(entry.get("SurveyName") or "Untitled survey"),
            description=entry.get("SurveyDescription") or None,
            language=entry.get("SurveyLanguage") or None,
            languages=languages,
            created=entry.get("SurveyCreationDate") or None,
            modified=entry.get("LastModified") or None,
            source=SourceInfo(
                format="qualtrics.qsf",
                format_version=(proj or {}).get("SchemaVersion")
                if isinstance(proj, dict)
                else None,
                loader=_loader_id(),
            ),
            blocks=blocks,
            questions=questions,
            flow=flow,
            scoring_categories=scoring,
            quotas=self.quotas(),
            options=options if isinstance(options, dict) else {},
            extras={**extras, **({"SurveyEntry": entry.rest()} if entry.rest() else {})},
        )
        self.check(survey)
        flow_text = json.dumps(self.elements.get("FL", []))
        survey.conjoints = conjoint.legacy(self.elements.get("CJ", []), flow_text, self.diags)
        survey.conjoints += conjoint.diy(survey)
        survey.diagnostics = self.diags.items
        return survey

    def quotas(self) -> list[Quota]:
        quotas = []
        for el in self.elements.get("QO", []):
            q = Payload(el.get("Payload"))
            qid = q.get("ID")
            if not qid:
                continue
            quotas.append(
                Quota(
                    id=str(qid),
                    name=str(q.get("Name") or qid),
                    limit=to_int(q.get("Occurrences")),
                    condition=parse_logic(q.get("Logic"), self.diags, str(qid)),
                    action=q.get("QuotaAction"),
                    over_quota_action=q.get("OverQuotaAction"),
                    extras=q.rest(),
                )
            )
        return quotas

    def scoring(self) -> list[ScoringCategory]:
        sco = self.first_payload("SCO")
        cats = sco.get("ScoringCategories") if isinstance(sco, dict) else None
        return [
            ScoringCategory(
                id=str(c.get("ID")),
                name=str(c.get("Name") or c.get("ID")),
                description=c.get("Description"),
            )
            for c in cats or []
            if isinstance(c, dict)
        ]

    # ---------------------------------------------------------------- blocks

    def blocks(self) -> tuple[dict[str, Block], set[str]]:
        # Normally one BL element holds every block; some exports write one per block.
        raw_blocks: list[Any] = []
        for el in self.elements.get("BL", []):
            payload = el.get("Payload")
            if isinstance(payload, dict) and "ID" in payload and "BlockElements" in payload:
                raw_blocks.append(payload)
            else:
                raw_blocks.extend(as_mapping(payload).values())
        blocks: dict[str, Block] = {}
        trashed: set[str] = set()
        for raw in raw_blocks:
            if not isinstance(raw, dict):
                continue
            b = Payload(raw)
            bid = b.get("ID")
            btype = b.get("Type", "Standard")
            elements_raw = b.get("BlockElements") or []
            if btype == "Trash":
                trashed.update(
                    str(e.get("QuestionID")) for e in elements_raw if isinstance(e, dict)
                )
                continue
            if not bid:
                self.diags.warn("block-without-id", "Block without ID skipped")
                continue
            elements = []
            for e in elements_raw:
                if not isinstance(e, dict):
                    continue
                etype = e.get("Type")
                if etype == "Page Break":
                    elements.append(PageBreak())
                elif etype == "Question":
                    elements.append(
                        QuestionRef(
                            question_id=str(e.get("QuestionID")),
                            skip_logic=self.skip_logic(
                                e.get("SkipLogic"), str(e.get("QuestionID"))
                            ),
                        )
                    )
                else:
                    self.diags.warn(
                        "unknown-block-element", f"Block element type {etype!r} ignored", bid
                    )
            opts = b.sub("Options")
            randomization = self.block_randomization(opts)
            loop = self.loop(opts, bid)
            description = b.get("Description") or ""
            extras = b.rest()
            extras.pop("Options", None)
            if opts.rest():
                extras["Options"] = opts.rest()
            if b.data.get("SubType") in ("", None):
                extras.pop("SubType", None)
            blocks[bid] = Block(
                id=bid,
                description=description,
                elements=elements,
                randomization=randomization,
                loop=loop,
                is_default=btype == "Default",
                extras=extras,
            )
        if not blocks:
            questions = [
                str(el["Payload"]["QuestionID"])
                for el in self.elements.get("SQ", [])
                if isinstance(el.get("Payload"), dict) and el["Payload"].get("QuestionID")
            ]
            if questions:
                self.diags.warn(
                    "implicit-block",
                    "No usable blocks; all questions placed in one block in file order",
                )
                blocks["BL_implicit"] = Block(
                    id="BL_implicit",
                    description="(implicit)",
                    elements=[QuestionRef(question_id=q) for q in questions],
                )
        return blocks, trashed

    def skip_logic(self, items: Any, qid: str) -> list[SkipLogic]:
        result = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            dest = str(item.get("SkipToDestination", ""))
            if dest == "ENDOFBLOCK":
                kwargs: dict[str, Any] = {"destination": "end_of_block"}
            elif dest == "ENDOFSURVEY":
                kwargs = {"destination": "end_of_survey"}
            else:
                kwargs = {"destination": "question", "target_question_id": dest or None}
            known = {
                "Value",
                "ChoiceLocator",
                "Locator",
                "Condition",
                "QuestionID",
                "Description",
                "SkipToDestination",
                "SkipLogicID",
                "SkipToDescription",
            }
            result.append(
                SkipLogic(
                    condition=parse_skip_condition(item, self.diags, qid),
                    extras={k: v for k, v in item.items() if k not in known},
                    **kwargs,
                )
            )
        return result

    def block_randomization(self, opts: Payload) -> Randomization | None:
        mode = str(opts.get("RandomizeQuestions", "false"))
        rand = opts.get("Randomization")
        rand = rand if isinstance(rand, dict) else {}
        even = truthy(rand.get("EvenPresentation", False))
        adv: dict[str, Any] = as_mapping(rand.get("Advanced"))
        per_page = to_int(adv.get("QuestionsPerPage")) or None
        if mode in ("false", "False", "", "None"):
            return None
        if mode == "Advanced":
            return parse_advanced(adv, even=even)
        if mode == "RandomWithXPerPage":
            return Randomization(mode="all", per_page=per_page, even_presentation=even)
        if mode == "RandomWithOnlyX":
            subset = to_int(adv.get("TotalRandSubset")) or to_int(rand.get("TotalRandSubset"))
            return Randomization(mode="subset", subset_size=subset, even_presentation=even)
        if mode in ("true", "True", "All", "RandomizeAll"):
            return Randomization(mode="all", even_presentation=even)
        self.diags.warn("unknown-randomization", f"Block randomization {mode!r}; treated as 'all'")
        return Randomization(mode="all", even_presentation=even)

    def loop(self, opts: Payload, bid: str) -> LoopAndMerge | None:
        looping = opts.get("Looping")
        lopts = opts.get("LoopingOptions")
        if looping in (None, "None", "", False):
            return None
        lopts = lopts if isinstance(lopts, dict) else {}
        lp = Payload(lopts)
        rand_mode = str(lp.get("Randomization", "None"))
        subset = to_int(lp.get("TotalRandSubset"))
        randomization = None
        if rand_mode == "All":
            randomization = Randomization(mode="all")
        elif rand_mode == "Subset":
            randomization = Randomization(mode="subset", subset_size=subset)
        static = lp.get("Static")
        fields = {}
        # Loop id -> {field number -> value}. Loops with no fields are written as [].
        fields = {
            k: {fk: "" if fv is None else str(fv) for fk, fv in as_mapping(v).items()}
            for k, v in as_mapping(static).items()
        }
        if looping == "Static":
            return LoopAndMerge(
                source="static", fields=fields, randomization=randomization, extras=lp.rest()
            )
        if looping == "Question":
            return LoopAndMerge(
                source="question",
                question_id=lp.get("QID"),
                locator=lp.get("ChoiceGroupLocator") or lp.get("Locator"),
                fields=fields,
                randomization=randomization,
                extras=lp.rest(),
            )
        self.diags.warn("unknown-loop", f"Loop & Merge type {looping!r} kept in extras", bid)
        return LoopAndMerge(
            source="static",
            fields=fields,
            randomization=randomization,
            extras={"Looping": looping, **lp.rest()},
        )

    # ---------------------------------------------------------------- questions

    def questions(self, blocks: dict[str, Block], trashed: set[str]) -> dict:
        in_blocks = {qid for b in blocks.values() for qid in b.question_ids}
        questions = {}
        for el in self.elements.get("SQ", []):
            payload = el.get("Payload")
            if not isinstance(payload, dict) or not payload.get("QuestionID"):
                self.diags.warn("question-without-id", "Question element without QuestionID")
                continue
            qid = str(payload["QuestionID"])
            if qid not in in_blocks:
                if qid not in trashed:
                    self.diags.info("orphan-question", "Question not in any block; dropped", qid)
                continue
            try:
                questions[qid] = build_question(payload, self.diags)
            except Exception as exc:  # keep loading; never lose the question silently
                self.diags.error("question-failed", f"Could not load question: {exc}", qid)
                from ...model import Origin, UnsupportedQuestion

                questions[qid] = UnsupportedQuestion(
                    id=qid,
                    export_tag=str(payload.get("DataExportTag") or qid),
                    origin=Origin(
                        format="qualtrics",
                        type=first_str(payload.get("QuestionType")),
                        selector=payload.get("Selector"),
                        sub_selector=payload.get("SubSelector"),
                    ),
                    text=make_text(payload.get("QuestionText", "")),
                    extras=dict(payload),
                )
        return questions

    # ---------------------------------------------------------------- flow

    def flow(self, blocks: dict[str, Block]) -> tuple[list[FlowNode], dict[str, Any]]:
        payload = self.first_payload("FL")
        if not isinstance(payload, dict):
            self.diags.warn("no-flow", "Survey has no flow; blocks are presented in file order")
            return [BlockNode(id=f"implicit-{bid}", block_id=bid) for bid in blocks], {}
        root = Payload(payload)
        root.drop("Type", "FlowID")
        nodes = self.flow_nodes(root.get("Flow") or [], blocks)
        return nodes, root.rest()

    def flow_nodes(self, items: list[Any], blocks: dict[str, Block]) -> list[FlowNode]:
        return [n for n in (self.flow_node(i, blocks) for i in items) if n is not None]

    def flow_node(self, item: Any, blocks: dict[str, Block]) -> FlowNode | None:
        if not isinstance(item, dict):
            return None
        p = Payload(item)
        ftype = str(p.get("Type"))
        fid = str(p.get("FlowID") or "")
        description = p.get("Description") or None

        def children() -> list[FlowNode]:
            return self.flow_nodes(p.get("Flow") or [], blocks)

        def base() -> dict[str, Any]:
            return {"id": fid, "description": description}

        if ftype in ("Block", "Standard"):
            bid = str(p.get("ID"))
            if bid not in blocks:
                self.diags.warn("missing-block", f"Flow references unknown block {bid}", fid)
            return BlockNode(**base(), block_id=bid, extras=p.rest())
        if ftype == "EmbeddedData":
            fields = []
            for f in p.get("EmbeddedData") or []:
                if not isinstance(f, dict):
                    continue
                fp = Payload(f)
                source = str(fp.get("Type") or "Custom")
                value = fp.get("Value")
                fp.drop("Description")  # duplicates Field
                fields.append(
                    EmbeddedField(
                        name=str(fp.get("Field")),
                        source=EMBEDDED_SOURCES.get(
                            source, "panel" if source.lower().startswith("panel") else "other"
                        ),
                        value=make_text(str(value)) if value is not None else None,
                        variable_type=fp.get("VariableType"),
                        extras=fp.rest(),
                    )
                )
            return EmbeddedDataNode(**base(), fields=fields, extras=p.rest())
        if ftype == "Branch":
            condition = parse_logic(p.get("BranchLogic"), self.diags, fid)
            if condition is None:
                self.diags.warn("branch-without-logic", "Branch has no readable condition", fid)
            return BranchNode(**base(), condition=condition, children=children(), extras=p.rest())
        if ftype in ("BlockRandomizer", "Randomizer"):  # "Randomizer" in older exports
            return RandomizerNode(
                **base(),
                subset_size=to_int(p.get("SubSet")),
                even_presentation=truthy(p.get("EvenPresentation", False)),
                children=children(),
                extras=p.rest(),
            )
        if ftype == "Group":
            return GroupNode(**base(), children=children(), extras=p.rest())
        if ftype == "EndSurvey":
            opts = p.sub("Options")
            term = str(opts.get("SurveyTermination") or "")
            redirect_url = opts.get("EOSRedirectURL") or None
            message_id = opts.get("EOSMessage") or None
            response_flag = opts.get("ResponseFlag") or None
            ignore_response = truthy(opts.get("IgnoreResponse", False))
            extras = p.rest()
            extras.pop("Options", None)
            if opts.rest():
                extras["Options"] = opts.rest()
            return EndSurveyNode(
                **base(),
                termination=TERMINATIONS.get(term, "other"),
                redirect_url=redirect_url,
                message_id=message_id,
                response_flag=response_flag,
                ignore_response=ignore_response,
                extras=extras,
            )
        if ftype == "WebService":
            return WebServiceNode(
                **base(),
                url=p.get("URL"),
                method=p.get("Method"),
                request_params=_key_values(p.get("RequestParams")),
                response_map=_key_values(p.get("ResponseMap")),
                extras=p.rest(),
            )
        if ftype == "Authenticator":
            return AuthenticatorNode(**base(), children=children(), extras=p.rest())
        if ftype == "TableOfContents":
            return TableOfContentsNode(**base(), children=children(), extras=p.rest())
        if ftype == "ReferenceSurvey":
            self.diags.warn(
                "library-block",
                "Flow includes a library block whose questions are not in this file",
                fid,
            )
            return LibraryBlockNode(
                **base(),
                reference_id=str(p.get("ID") or ""),
                library_id=p.get("LibraryID"),
                extras=p.rest(),
            )
        if ftype == "Quota":
            return QuotaNode(**base(), extras=p.rest())
        self.diags.warn("unsupported-flow", f"Flow element type {ftype!r} kept verbatim", fid)
        return UnsupportedNode(**base(), source_type=ftype, children=children(), extras=p.rest())

    # ---------------------------------------------------------------- checks

    def check(self, survey: Survey) -> None:
        for bid, block in survey.blocks.items():
            for qid in block.question_ids:
                if qid not in survey.questions:
                    self.diags.warn(
                        "missing-question", f"Block lists {qid} but no question defines it", bid
                    )
            if (
                block.loop
                and block.loop.question_id
                and block.loop.question_id not in survey.questions
            ):
                self.diags.warn(
                    "missing-loop-source",
                    f"Loop & Merge source {block.loop.question_id} is not in the survey",
                    bid,
                )
        tags: dict[str, list[str]] = {}
        for q in survey.questions.values():
            if q.kind not in ("descriptive",):
                tags.setdefault(q.export_tag, []).append(q.id)
        for tag, ids in tags.items():
            if len(ids) > 1:
                self.diags.info(
                    "duplicate-export-tag", f"Export tag {tag!r} used by {', '.join(ids)}"
                )
