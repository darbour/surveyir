"""Response-data columns a survey produces, following Qualtrics export naming.

Simulated responses written with these columns line up one-to-one with a real
Qualtrics CSV export, so simulated and human data can be analyzed with the same
code. Each column also carries its third-header-row object (``ImportId`` plus
keys such as ``choiceId``), which stays stable when export tags are renamed.

How sure each rule is: ``Column.evidence`` is ``"corpus"`` for rules checked
against real third-party exports (see docs/parity.md), ``"vendor_docs"`` for
rules taken from export screenshots on Qualtrics' support pages, and
``"inferred"`` otherwise.
"""

from __future__ import annotations

import html as _html
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, computed_field

from .model import (
    Block,
    BlockNode,
    Choice,
    ChoiceQuestion,
    ConstantSumQuestion,
    DrillDownQuestion,
    EmbeddedDataNode,
    FileUploadQuestion,
    GraphicSliderQuestion,
    HeatMapQuestion,
    HighlightQuestion,
    HotSpotQuestion,
    MatrixQuestion,
    MetaInfoQuestion,
    PickGroupRankQuestion,
    Question,
    RandomizerNode,
    RankOrderQuestion,
    SideBySideQuestion,
    SignatureQuestion,
    SliderQuestion,
    Survey,
    Text,
    TextEntryQuestion,
    TimingQuestion,
)

ColumnPart = Literal[
    "metadata",
    "response",
    "text",
    "display_order",
    "timing",
    "meta",
    "file",
    "coordinate",
    "region",
    "score",
    "embedded_data",
    "key",
]
Evidence = Literal["corpus", "vendor_docs", "inferred"]

TIMING_PARTS = [
    ("First Click", "FIRST_CLICK"),
    ("Last Click", "LAST_CLICK"),
    ("Page Submit", "PAGE_SUBMIT"),
    ("Click Count", "CLICK_COUNT"),
]
FILE_PARTS = [
    ("Id", "FILE_ID"),
    ("Name", "FILE_NAME"),
    ("Size", "FILE_SIZE"),
    ("Type", "FILE_TYPE"),
]
META_PARTS = [
    ("Browser", "BROWSER"),
    ("Version", "VERSION"),
    ("Operating System", "OS"),
    ("Resolution", "RESOLUTION"),
]
#: Standard response metadata at the start of every export (name, ImportId, has time zone).
METADATA = [
    ("StartDate", "startDate", True),
    ("EndDate", "endDate", True),
    ("Status", "status", False),
    ("IPAddress", "ipAddress", False),
    ("Progress", "progress", False),
    ("Duration (in seconds)", "duration", False),
    ("Finished", "finished", False),
    ("RecordedDate", "recordedDate", True),
    ("ResponseId", "_recordId", False),
    ("RecipientLastName", "recipientLastName", False),
    ("RecipientFirstName", "recipientFirstName", False),
    ("RecipientEmail", "recipientEmail", False),
    ("ExternalReference", "externalDataReference", False),
    ("LocationLatitude", "locationLatitude", False),
    ("LocationLongitude", "locationLongitude", False),
    ("DistributionChannel", "distributionChannel", False),
    ("UserLanguage", "userLanguage", False),
]


class ColumnOptions(BaseModel):
    """Qualtrics export settings that change the column layout.

    None of these are recorded in a .qsf; they are chosen when the data is
    exported. Field names follow the Qualtrics UI; the v3 export API names
    (``breakoutSets``, ``includeDisplayOrder``, ``questionIds``, ...) are
    accepted as aliases. ``infer_options`` recovers them from a real export.
    """

    model_config = ConfigDict(populate_by_name=True)

    split_multi_value: bool = Field(
        default=True,
        validation_alias=AliasChoices("split_multi_value", "breakout_sets", "breakoutSets"),
        description="'Split multi-value fields into columns': one column per choice for "
        "multi-select (and highlight) questions; False gives one comma-separated column.",
    )
    display_order: Literal["auto", "split", "single", "none"] = Field(
        default="auto",
        description="Display-order columns for randomized questions, blocks and flow "
        "randomizers: one per item ('split'), one pipe-separated column ('single'), or "
        "none. 'auto' follows split_multi_value, as Qualtrics does.",
    )
    slider_naming: Literal["id", "position"] = Field(
        default="id",
        description="Slider item columns named by choice id (Qualtrics UI exports) or by "
        "position in the item order (seen in some API exports). ImportIds always use ids.",
    )
    include_metadata: bool = Field(
        default=False,
        description="Prepend the standard response metadata columns (StartDate ... "
        "UserLanguage). Anonymized exports omit some of them.",
    )
    time_zone: str = Field(
        default="UTC", description="Time zone written into date-column ImportIds."
    )
    question_ids: list[str] | None = Field(
        default=None,
        validation_alias=AliasChoices("question_ids", "questionIds"),
        description="Export only these questions (whole questions, by QID).",
    )
    embedded_data_ids: list[str] | None = Field(
        default=None,
        validation_alias=AliasChoices("embedded_data_ids", "embeddedDataIds"),
        description="Export only these embedded-data fields.",
    )

    def __init__(self, **data: Any) -> None:
        if data.get("includeDisplayOrder") is False or data.get("include_display_order") is False:
            data.setdefault("display_order", "none")
        data.pop("includeDisplayOrder", None)
        data.pop("include_display_order", None)
        super().__init__(**data)

    @property
    def breakout_sets(self) -> bool:
        return self.split_multi_value

    @property
    def do_layout(self) -> Literal["split", "single", "none"]:
        if self.display_order == "auto":
            return "split" if self.split_multi_value else "single"
        return self.display_order


class Column(BaseModel):
    """One column of a Qualtrics-style response export.

    ``import_object`` is the exact third header row cell. ``import_extra`` holds
    its keys beyond ``ImportId`` (``choiceId`` for split multi-value and
    display-order columns, ``point``/``coord`` for heat maps, ``timeZone`` for
    dates). Match exports on the whole object: several heat-map columns share an
    ImportId.

    A matrix row's own export tag replaces the column name, except tags that
    look like stale Qualtrics defaults (``Q<n>_<rowid>`` where ``Q<n>`` is not
    the question's current tag), which real exports ignore.
    """

    name: str
    import_id: str
    import_extra: dict[str, str | int | bool] = Field(default_factory=dict)
    choice_id: str | None = None
    question_id: str | None = None
    part: ColumnPart = "response"
    row_id: str | None = None
    answer_id: str | None = None
    loop: str | None = None
    evidence: Evidence = "corpus"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verified(self) -> bool:
        """True when the naming rule was confirmed against real exports or vendor docs."""
        return self.evidence != "inferred"

    @property
    def import_object(self) -> dict[str, Any]:
        return {"ImportId": self.import_id, **self.import_extra}


def _row_tag(row: Choice, question_tag: str) -> str | None:
    """A matrix row's own export tag, unless it is a stale auto-generated default.

    Qualtrics fills per-row tags like ``Q15_3`` (default question tag + row id); real
    exports ignore those once the question is renamed, but honor custom tags.
    """
    t = row.export_tag
    if not t:
        return None
    m = re.fullmatch(r"(Q\d+(?:\.\d+)?)_" + re.escape(row.id), t)
    if m and m.group(1) != question_tag:
        return None
    return t


def _carried(survey: Survey | None, q: Question) -> list[Choice]:
    """Choices a question gets at run time from carry forward ("x"-prefixed ids)."""
    cf = getattr(q, "carry_forward", None)
    if survey is None or cf is None or cf.source != "question":
        return []
    src = survey.questions.get(cf.question_id or "")
    items = (
        getattr(src, "choices", None) or getattr(src, "items", None) or getattr(src, "rows", None)
    )
    return [c.model_copy(update={"id": f"x{c.id}"}) for c in items or []]


def _display_order(
    name: str,
    import_id: str,
    keys: Sequence[str],
    options: ColumnOptions,
    *,
    question_id: str | None = None,
) -> Iterator[Column]:
    layout = options.do_layout
    if layout == "single":
        yield Column(
            name=f"{name}_DO", import_id=import_id, question_id=question_id, part="display_order"
        )
    elif layout == "split":
        for key in keys:
            yield Column(
                name=f"{name}_DO_{key}",
                import_id=import_id,
                import_extra={"choiceId": key},
                choice_id=key,
                question_id=question_id,
                part="display_order",
            )


def _question_columns(
    q: Question, survey: Survey | None, options: ColumnOptions
) -> Iterator[Column]:
    tag, qid = q.export_tag, q.id

    def col(suffix: str, imp_suffix: str, **kw) -> Column:
        return Column(name=f"{tag}{suffix}", import_id=f"{qid}{imp_suffix}", question_id=qid, **kw)

    def do(keys: Sequence[str]) -> Iterator[Column]:
        return _display_order(tag, f"{qid}_DO", keys, options, question_id=qid)

    if isinstance(q, ChoiceQuestion):
        choices = q.choices + [
            c for c in _carried(survey, q) if c.id not in {x.id for x in q.choices}
        ]
        if q.multiple and options.split_multi_value:
            for c in choices:
                yield Column(
                    name=c.export_tag or f"{tag}_{c.id}",
                    import_id=qid,
                    import_extra={"choiceId": c.id},
                    choice_id=c.id,
                    question_id=qid,
                )
        else:
            yield col("", "")
        for c in choices:
            if c.text_entry:
                yield col(f"_{c.id}_TEXT", f"_{c.id}_TEXT", part="text", choice_id=c.id)
        if q.randomization is not None:
            yield from do([c.id for c in choices])
    elif isinstance(q, MatrixQuestion):
        rows = q.rows + [r for r in _carried(survey, q) if r.id not in {x.id for x in q.rows}]
        for pos, row in enumerate(rows, start=1):
            row_name = _row_tag(row, tag) or f"{tag}_{pos}"
            if q.mode in ("multiple", "text"):
                for apos, ans in enumerate(q.columns, start=1):
                    yield Column(
                        name=f"{row_name}_{apos}",
                        import_id=f"{qid}_{row.id}_{ans.id}",
                        question_id=qid,
                        row_id=row.id,
                        answer_id=ans.id,
                    )
            else:
                yield Column(
                    name=row_name, import_id=f"{qid}_{row.id}", question_id=qid, row_id=row.id
                )
            if row.text_entry:
                yield Column(
                    name=f"{row_name}_TEXT",
                    import_id=f"{qid}_{row.id}_TEXT",
                    question_id=qid,
                    part="text",
                    row_id=row.id,
                )
        if q.randomization is not None:
            yield from do([str(i) for i in range(1, len(rows) + 1)])
    elif isinstance(q, SideBySideQuestion):
        for sub in q.questions:  # sub ids/tags already carry the "#n" suffix
            yield from _question_columns(sub, survey, options)
    elif isinstance(q, TextEntryQuestion):
        if q.mode == "form":
            for f in q.fields:
                yield col(f"_{f.id}", f"_{f.id}", choice_id=f.id)
        else:
            yield col("", "_TEXT")
    elif isinstance(q, (SliderQuestion, ConstantSumQuestion, RankOrderQuestion)):
        items = q.items + [c for c in _carried(survey, q) if c.id not in {x.id for x in q.items}]
        for pos, c in enumerate(items, start=1):
            by_position = isinstance(q, SliderQuestion) and options.slider_naming == "position"
            name = str(pos) if by_position else c.id
            yield col(f"_{name}", f"_{c.id}", choice_id=c.id)
            if c.text_entry:
                yield col(f"_{c.id}_TEXT", f"_{c.id}_TEXT", part="text", choice_id=c.id)
        if q.randomization is not None:
            yield from do([c.id for c in items])
    elif isinstance(q, DrillDownQuestion):
        for level in q.levels:
            yield col(f"_{level.id}", f"_{level.id}", choice_id=level.id)
    elif isinstance(q, PickGroupRankQuestion):
        items = q.items + [c for c in _carried(survey, q) if c.id not in {x.id for x in q.items}]
        for g in range(len(q.groups)):
            for c in items:
                yield Column(
                    name=f"{tag}_{g}_GROUP_{c.id}",
                    import_id=f"{qid}_{g}_GROUP",
                    import_extra={"choiceId": c.id},
                    choice_id=c.id,
                    question_id=qid,
                )
            for c in items:
                yield col(f"_{g}_{c.id}_RANK", f"_G{g}_{c.id}_RANK", choice_id=c.id)
    elif isinstance(q, FileUploadQuestion):
        for label, imp in FILE_PARTS:
            yield col(f"_{label}", f"_{imp}", part="file")
    elif isinstance(q, SignatureQuestion):  # exported like a file upload (vendor screenshot)
        for label, imp in FILE_PARTS:
            yield col(f"_{label}", f"_{imp}", part="file", evidence="vendor_docs")
    elif isinstance(q, TimingQuestion):
        for label, imp in TIMING_PARTS:
            yield col(f"_{label}", f"_{imp}", part="timing")
    elif isinstance(q, MetaInfoQuestion):
        for label, imp in META_PARTS:
            yield col(f"_{label}", f"_{imp}", part="meta")
    elif isinstance(q, HotSpotQuestion):
        # Vendor screenshot matches tag_<ChoiceID> for 4 of 5 regions; match on ImportId.
        for r in q.regions:
            if r.id:
                yield col(f"_{r.id}", f"_{r.id}", choice_id=r.id, evidence="vendor_docs")
    elif isinstance(q, HeatMapQuestion):
        # Two columns per allowed click, then the region column if regions exist.
        for k in range(1, (q.max_clicks or 1) + 1):
            for coord in ("x", "y"):
                yield Column(
                    name=f"{tag}_{k}_{coord}",
                    import_id=qid,
                    import_extra={"point": k, "coord": coord},
                    question_id=qid,
                    part="coordinate",
                    evidence="vendor_docs",
                )
        if q.regions:
            yield col("", "_REGIONS", part="region", evidence="vendor_docs")
    elif isinstance(q, HighlightQuestion):
        for cat in q.categories:
            if options.split_multi_value:
                for w in q.words:
                    yield Column(
                        name=f"{tag}_{cat.id}_{w.id}",
                        import_id=f"{qid}_{cat.id}",
                        import_extra={"choiceId": w.id},
                        question_id=qid,
                        choice_id=w.id,
                        answer_id=cat.id,
                        evidence="vendor_docs",
                    )
            else:
                yield col(f"_{cat.id}", f"_{cat.id}", answer_id=cat.id, evidence="vendor_docs")
    elif isinstance(q, GraphicSliderQuestion):
        # Vendor screenshot (older export format) shows a single coded column.
        yield col("", "", evidence="vendor_docs")


def _loop_ids(survey: Survey, block: Block) -> list[str] | None:
    loop = block.loop
    if loop is None:
        return None
    if loop.fields:  # static loops, and question loops that also carry a field table
        return sorted(loop.fields, key=lambda k: (not k.isdigit(), int(k) if k.isdigit() else 0, k))
    src = survey.questions.get(loop.question_id or "")
    items = getattr(src, "choices", None) or getattr(src, "items", None) or []
    return [c.id for c in items]


def _squash(text: str) -> str:
    """Block descriptions as they appear in display-order column names."""
    return re.sub(r"\s+", "", text)


def question_columns(
    survey: Survey,
    question: Question,
    block: Block | None = None,
    options: ColumnOptions | None = None,
) -> list[Column]:
    """Columns for one question, expanded for Loop & Merge if ``block`` loops."""
    cols = list(_question_columns(question, survey, options or ColumnOptions()))
    loops = _loop_ids(survey, block) if block is not None else None
    if not loops:
        return cols
    return [
        c.model_copy(update={"name": f"{n}_{c.name}", "import_id": f"{n}_{c.import_id}", "loop": n})
        for n in loops
        for c in cols
    ]


def _flow_display_order(survey: Survey, options: ColumnOptions) -> Iterator[Column]:
    """``FL_n_DO`` columns: which children of each flow randomizer were shown, in order."""
    for node in survey.walk_flow():
        if not isinstance(node, RandomizerNode) or not node.id:
            continue
        keys = []
        for child in node.children:
            block = survey.blocks.get(child.block_id) if isinstance(child, BlockNode) else None
            keys.append(_squash(block.description) if block else child.id)
        yield from _display_order(node.id, f"{node.id}_DO", keys, options)


def response_columns(
    survey: Survey, *, include_embedded: bool = True, options: ColumnOptions | None = None
) -> list[Column]:
    """Every response column the survey can produce.

    Naming follows a Qualtrics CSV export with default settings; pass
    ``options`` to match exports made with other settings.
    """
    options = options or ColumnOptions()
    cols: list[Column] = []
    if options.include_metadata:
        for name, imp, dated in METADATA:
            extra: dict[str, str | int | bool] = {"timeZone": options.time_zone} if dated else {}
            cols.append(Column(name=name, import_id=imp, import_extra=extra, part="metadata"))
    wanted = set(options.question_ids) if options.question_ids is not None else None
    seen_blocks: set[str] = set()
    for bid in survey.flow_block_ids() + list(survey.blocks):
        if bid in seen_blocks or bid not in survey.blocks:
            continue
        seen_blocks.add(bid)
        block = survey.blocks[bid]
        questions = survey.block_questions(bid)
        for q in questions:
            if wanted is None or q.id in wanted:
                cols.extend(question_columns(survey, q, block, options))
        if block.randomization is not None and block.randomization.mode != "none":
            cols.extend(
                _display_order(
                    _squash(block.description) or bid,
                    f"{bid}_DO",
                    [q.export_tag for q in questions],
                    options,
                )
            )
    cols.extend(_flow_display_order(survey, options))
    for i, cat in enumerate(survey.scoring_categories):
        cols.append(Column(name=f"SC{i}", import_id=cat.id, part="score"))
    if include_embedded:
        names: dict[str, None] = {}
        for node in survey.walk_flow():
            if isinstance(node, EmbeddedDataNode):
                for f in node.fields:
                    names.setdefault(f.name, None)
        keep = set(options.embedded_data_ids) if options.embedded_data_ids is not None else None
        cols.extend(
            Column(name=n, import_id=n, part="embedded_data")
            for n in names
            if keep is None or n in keep
        )
    return cols


# --------------------------------------------------------------------------- labels

#: Second-header-row labels of the standard metadata columns. Start Date, End Date,
#: Progress, Duration (in seconds), Finished and Recorded Date were checked against
#: real exports; the others follow Qualtrics' export layout but are not checked.
METADATA_LABELS = {
    "StartDate": "Start Date",
    "EndDate": "End Date",
    "Status": "Response Type",
    "IPAddress": "IP Address",
    "RecordedDate": "Recorded Date",
    "ResponseId": "Response ID",
    "RecipientLastName": "Recipient Last Name",
    "RecipientFirstName": "Recipient First Name",
    "RecipientEmail": "Recipient Email",
    "ExternalReference": "External Data Reference",
    "LocationLatitude": "Location Latitude",
    "LocationLongitude": "Location Longitude",
    "DistributionChannel": "Distribution Channel",
    "UserLanguage": "User Language",
}
_PART_LABELS = {imp: label for label, imp in TIMING_PARTS + FILE_PARTS + META_PARTS}
_BR = re.compile(r"<br\s*/?>", re.I)
_TAG = re.compile(r"<[^>]*>")
_LABEL_PIPE = re.compile(r"\$\{(\w+)://([^}]*)\}")


def _label_text(source: str) -> str:
    """Question text as Qualtrics writes it into an export label: ``<br>`` becomes a
    newline, other tags are dropped (source newlines and spaces are kept), entities
    are decoded, ``&nbsp;`` is a space, and piped text ``${lm://Field/3}`` becomes
    ``[Field-3]`` (checked for ``lm://``; other schemes are assumed alike)."""
    t = _TAG.sub("", _BR.sub("\n", source or ""))
    t = _LABEL_PIPE.sub(lambda m: "[" + m.group(2).replace("/", "-") + "]", t)
    return _html.unescape(t).replace("\xa0", " ").strip()


def _sub_label(item: Any, *, flatten: bool = False) -> str | None:
    """A choice, row or field as it follows `` - `` in a label: its source text as is
    (Qualtrics does not strip markup here; matrix rows of response columns are the
    exception, ``flatten``), its id when it has no text, and nothing when its text is
    only whitespace or markup."""
    text: Text | None = getattr(item, "text", None)
    raw = text.html if text is not None else ""
    if not raw:
        return str(getattr(item, "id", ""))
    flat = _label_text(raw)
    if not flat:
        return None
    return flat if flatten else raw


def _find(items: Sequence[Any] | None, item_id: str | None) -> Any:
    return next((c for c in items or [] if c.id == item_id), None)


def _loop_value(survey: Survey, question_id: str, loop: str) -> str:
    """The loop's label in export labels: its first field's value, else the loop id."""
    for block in survey.blocks.values():
        if block.loop is None or not any(
            q.id == question_id for q in survey.block_questions(block.id)
        ):
            continue
        fields = block.loop.fields.get(loop) or {}
        if fields:
            first = min(fields, key=lambda k: (not k.isdigit(), int(k) if k.isdigit() else 0, k))
            return fields[first]
    return loop


def column_label(survey: Survey, column: Column) -> str:
    """The second-header-row label Qualtrics writes for ``column``.

    Question columns: the question text (markup stripped as in ``_label_text``, or
    the export tag when the text is empty), then `` - `` and the part: the choice,
    row, item or form field (``"Q - Choice"``), ``Selected Choice`` for a choice
    question with text-entry choices (``"Q - Selected Choice"``; their text columns
    are ``"Q - Choice - Text"``), ``Display Order`` (``"Q - Display Order - Choice"``),
    or the timing / meta-info / file part (``"Timing - First Click"``). Flow
    randomizer display order is ``"FL_5 - Block Randomizer - Display Order - <arm>"``,
    block display order ``"<block description> - Display Order - <tag>"``; embedded
    data and key columns are their name, score columns the scoring category name,
    metadata columns ``METADATA_LABELS``. Loop & Merge columns lead with the loop's
    first field value (``"TikTok - Q"``).

    Checked against 19 real exports (Twin-2K-500 mega-study; see
    tests/test_labels.py): metadata (the six names above), single-answer and
    split multi-select choice questions (with Selected Choice and text columns),
    single-answer matrix rows, text entry (single and form), slider, constant sum,
    timing, meta info, choice / matrix / block / flow display order, embedded
    data and score. Not checked: multi-answer and text matrix cells, side by side,
    rank order, drill down, file upload, signature, hot spot, heat map, highlight,
    pick-group-rank, graphic slider, single-column display order, the unchecked
    metadata labels, and piped text other than ``lm://``. Loop & Merge labels are
    checked only in part. Qualtrics is not consistent there: the value goes after
    the question text on text columns and form fields (``"Q - TikTok - Other -
    Text"``), and a choice question with text-entry choices repeats the question
    on its choice columns (``"Q - TikTok - Q - Selected Choice - Other"``).
    """
    if column.part == "metadata":
        return METADATA_LABELS.get(column.name, column.name)
    if column.part in ("embedded_data", "key"):
        return column.name
    if column.part == "score":
        cat = next((c for c in survey.scoring_categories if c.id == column.import_id), None)
        return cat.name if cat is not None and cat.name else column.name
    if column.question_id is None:
        if column.part != "display_order":
            return column.name
        source = column.import_id[: -len("_DO")]
        if source.startswith("FL_"):
            base = f"{source} - Block Randomizer - Display Order"
        else:
            block = survey.blocks.get(source)
            base = f"{block.description if block and block.description else source} - Display Order"
        return base if column.choice_id is None else f"{base} - {column.choice_id}"
    q = _label_question(survey, column.question_id)
    if q is None:
        return column.name
    parts = [_label_text(q.text.html) or q.export_tag]
    after: list[str] = []  # Loop & Merge: where the loop value goes (see the docstring)
    if column.part == "display_order":
        parts.append("Display Order")
        if column.choice_id is not None:
            item = None
            if isinstance(q, MatrixQuestion) and column.choice_id.isdigit():
                pos = int(column.choice_id) - 1
                item = q.rows[pos] if 0 <= pos < len(q.rows) else None
            else:
                item = _find(_label_items(q), column.choice_id)
            parts.append((_sub_label(item) if item is not None else None) or column.choice_id)
    elif column.part in ("timing", "meta", "file"):
        suffix = next((k for k in _PART_LABELS if column.import_id.endswith(f"_{k}")), None)
        if suffix is not None:
            parts.append(_PART_LABELS[suffix])
    elif isinstance(q, MatrixQuestion):
        row = _find(q.rows, column.row_id)
        parts.append((_sub_label(row, flatten=True) if row else None) or column.row_id or "")
        if column.answer_id is not None:
            ans = _find(q.columns, column.answer_id)
            parts.append((_sub_label(ans, flatten=True) if ans else None) or column.answer_id)
        if column.part == "text":
            parts.append("Text")
    elif isinstance(q, ChoiceQuestion):
        has_text = any(c.text_entry for c in q.choices)
        if column.part == "text":
            parts.append(_sub_label(_find(q.choices, column.choice_id)) or str(column.choice_id))
            parts.append("Text")
            after = ["text"]
        else:
            if has_text:
                parts.append("Selected Choice")
                after = ["repeat"]
            if column.choice_id is not None:
                label = _sub_label(_find(_label_items(q), column.choice_id))
                if label is not None:
                    parts.append(label)
    elif column.choice_id is not None:
        label = _sub_label(_find(_label_items(q), column.choice_id))
        if label is not None:
            parts.append(label)
        if column.part == "text":
            parts.append("Text")
        if isinstance(q, TextEntryQuestion):
            after = ["text"]
    if column.loop is not None:
        value = _loop_value(survey, column.question_id.split("#")[0], column.loop)
        if after == ["repeat"]:
            parts = [parts[0], value, *parts]
        elif after == ["text"]:
            parts.insert(1, value)
        else:
            parts.insert(0, value)
    return " - ".join(parts)


def _label_question(survey: Survey, question_id: str) -> Question | None:
    q = survey.questions.get(question_id)
    if q is not None:
        return q
    base = survey.questions.get(question_id.split("#")[0])
    if isinstance(base, SideBySideQuestion):
        return next((sub for sub in base.questions if sub.id == question_id), base)
    return base


def _label_items(q: Question) -> list[Any]:
    for attr in ("choices", "items", "fields", "levels", "regions", "words"):
        items = getattr(q, attr, None)
        if items:
            return list(items)
    return []


def response_header_rows(
    survey: Survey,
    columns: Sequence[Column] | None = None,
    *,
    options: ColumnOptions | None = None,
    key: Sequence[str] = (),
) -> list[list[str]]:
    """The three header rows of a Qualtrics CSV export: column names, labels
    (``column_label``) and the ImportId objects, as compact JSON as Qualtrics writes
    them. ``columns`` defaults to ``response_columns(survey, options=options)``;
    ``key`` names extra columns appended at the end (a respondent id to merge on,
    e.g. ``TWIN_ID``), labelled with their name and ``{"ImportId": name}``."""
    cols = list(columns) if columns is not None else response_columns(survey, options=options)
    cols += key_columns(key)
    return [
        [c.name for c in cols],
        [column_label(survey, c) for c in cols],
        [json.dumps(c.import_object, separators=(",", ":")) for c in cols],
    ]


def key_columns(names: Sequence[str] | Mapping[str, Any]) -> list[Column]:
    """Columns for external keys appended to an export (``part == "key"``)."""
    return [Column(name=n, import_id=n, part="key", evidence="inferred") for n in names]


# --------------------------------------------------------------------------- real exports

_TEXT_IQ = re.compile(
    r"_TEXT_[0-9a-f]{24}|[0-9a-f]{24}(Topics|ParTopics|Sentiment\w*|SenPol|"
    r"SenScore|TopicSen\w*|Actionability|Effort\w*|Emot\w*|TopicHierarchy\d*)$"
)


def unpredictable_from_qsf(name: str, import_object: dict[str, Any]) -> bool:
    """Columns no survey definition can predict: Text iQ analytics, export add-ons."""
    imp = str(import_object.get("ImportId", ""))
    return bool(
        _TEXT_IQ.search(imp)
        or name == "LastModifiedDate"
        or import_object.get("isLabelsColumn")
        or import_object.get("IsLabelsColumn")
    )


def read_header(lines: Sequence[str] | str) -> list[tuple[str, dict[str, Any]]]:
    """Parse the header rows of a Qualtrics CSV export into (name, import object) pairs.

    Accepts the raw text or a list of lines; returns [] for legacy (2-row) exports,
    which have no ImportId row.
    """
    import csv
    import io

    text = lines if isinstance(lines, str) else "\n".join(lines)
    rows = []
    for row in csv.reader(io.StringIO(text, newline="")):
        rows.append(row)
        if len(rows) == 3:
            break
    if len(rows) < 3 or not any("ImportId" in c for c in rows[2]):
        return []
    out = []
    for name, cell in zip(rows[0], rows[2], strict=False):
        try:
            obj = json.loads(cell)
        except json.JSONDecodeError:
            obj = {"ImportId": cell}
        out.append((name, obj if isinstance(obj, dict) else {"ImportId": str(obj)}))
    return out


def infer_options(header: Sequence[tuple[str, dict[str, Any]]]) -> ColumnOptions:
    """Recover the export settings that produced ``header`` (see ``read_header``).

    Split multi-value is visible only when the survey has a multi-select or
    highlight question; otherwise the default (split) is assumed. Slider naming
    cannot be read from the header and stays at its default.
    """
    objs = [o for _, o in header]

    def is_do(o: dict[str, Any]) -> bool:
        return str(o.get("ImportId", "")).endswith("_DO")

    split_cols = any("choiceId" in o and not is_do(o) for o in objs)
    do_cols = [o for o in objs if is_do(o)]
    if do_cols:
        do_layout = "split" if any("choiceId" in o for o in do_cols) else "single"
    else:
        do_layout = "auto"
    split = split_cols or do_layout == "split"
    date_tz = next((o["timeZone"] for o in objs if "timeZone" in o), "UTC")
    return ColumnOptions(
        split_multi_value=split,
        display_order=do_layout if do_layout != ("split" if split else "single") else "auto",
        include_metadata=any(o.get("ImportId") in {imp for _, imp, _ in METADATA} for o in objs),
        time_zone=str(date_tz),
    )
