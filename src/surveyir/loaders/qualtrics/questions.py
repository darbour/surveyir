"""Build IR questions from Qualtrics ``SQ`` payloads."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, unquote

from ...model import (
    CaptchaQuestion,
    CarryForward,
    Choice,
    ChoiceGroup,
    ChoiceQuestion,
    ConstantSumQuestion,
    DescriptiveQuestion,
    DrillDownQuestion,
    FileUploadQuestion,
    GraphicSliderQuestion,
    HeatMapQuestion,
    HighlightQuestion,
    HotSpotQuestion,
    MatrixQuestion,
    Media,
    MetaInfoQuestion,
    NumberRange,
    Origin,
    PickGroupRankQuestion,
    Question,
    Randomization,
    RankOrderQuestion,
    Region,
    ScalePoint,
    Score,
    SideBySideQuestion,
    SignatureQuestion,
    SliderQuestion,
    TextEntryQuestion,
    TimingQuestion,
    Translation,
    UnsupportedQuestion,
    Validation,
)
from ...text import html_to_text, make_text
from ._util import (
    Diagnostics,
    Payload,
    as_mapping,
    first_str,
    recode,
    to_int,
    to_number,
    truthy,
)
from .logic import parse_logic

#: Keys that are editor bookkeeping or exact duplicates of mapped data.
DROPPED_QUESTION_KEYS = {
    "NextChoiceId",  # id counter for the editor
    "NextAnswerId",
    "QuestionText_Unsafe",  # duplicate of QuestionText
    "QuestionDescription_Unsafe",
    "QuestionText_Safe",  # sanitized duplicate of QuestionText
    "QuestionType",  # in `origin`
    "Selector",
    "SubSelector",
}

MC_LAYOUTS: dict[str, tuple[bool, str]] = {
    "SAVR": (False, "vertical"),
    "SAHR": (False, "horizontal"),
    "SACOL": (False, "columns"),
    "DL": (False, "dropdown"),
    "SB": (False, "select_box"),
    "NPS": (False, "nps"),
    "MAVR": (True, "vertical"),
    "MAHR": (True, "horizontal"),
    "MACOL": (True, "columns"),
    "MSB": (True, "select_box"),
}

MATRIX_MODES = {
    "Bipolar": "bipolar",
    "TE": "text",
    "RO": "rank_order",
    "CS": "constant_sum",
    "MaxDiff": "max_diff",
    "Profile": "profile",
}
LIKERT_SUB_MODES = {
    "SingleAnswer": "single",
    "SACH": "single",  # single answer, statements one at a time (carousel), horizontal
    "SACV": "single",  # ... vertical
    "MultipleAnswer": "multiple",
    "DL": "dropdown",
    "DND": "dropdown",
    "None": "single",  # old exports omit the sub-selector for single-answer matrices
}
TEXT_MODES = {
    "SL": "single_line",
    "ML": "multi_line",
    "ESTB": "essay",
    "FORM": "form",
    "PW": "password",
    "AUTO": "autocomplete",
}
SLIDER_MODES = {"HSLIDER": "slider", "HBAR": "bar", "STAR": "star", "NumberScale": "number_scale"}
CS_MODES = {
    "VRTL": "text_boxes",
    "VR": "text_boxes",
    "TB": "text_boxes",
    "HBAR": "bars",
    "HSLIDER": "sliders",
}
RANK_MODES = {"DND": "drag_and_drop", "RB": "radio_buttons", "TB": "text_box", "SB": "select_box"}
DB_MODES = {"TB": "text", "PTB": "text", "GRB": "graphic"}
HOTSPOT_MODES = {"OnOff": "on_off", "LikeDislike": "like_dislike"}

RANDOMIZED_SLOT = "{~Randomized~}"

#: Codes known from Qualtrics' survey-taking runtime and API spec, but with no public
#: payload to model against. They load as UnsupportedQuestion (verbatim) with a
#: diagnostic naming the probable question type, never as a lookalike kind.
KNOWN_UNMODELED: list[tuple[str, str | None, str]] = [
    ("TE", "Calendar", "Calendar"),  # runtime keys TECalendarSingle / TECalendarRange
    ("FileUpload", "ScreenCapture", "Screen Capture"),
    ("FileUpload", "VideoCapture", "Video Response"),  # runtime key ...VideoCaptureVideoToText
    ("TreeSelect", None, "Org Hierarchy"),
]


def _known_unmodeled(qtype: str, selector: str, sub: str) -> str | None:
    for t, sel_prefix, name in KNOWN_UNMODELED:
        if qtype == t and (sel_prefix is None or selector.startswith(sel_prefix)):
            return name
    if qtype == "TE" and selector == "AUTO" and sub == "SDS":
        return "Location Selector (probable)"
    return None


# --------------------------------------------------------------------------- parts


def parse_randomization(data: Any) -> Randomization | None:
    """Parse a choice/row ``Randomization`` dict. None means 'not randomized'."""
    if not isinstance(data, dict):
        return None
    rtype = str(data.get("Type", "None"))
    flip = truthy(data.get("ConsistentScaleReversal", False))
    even = truthy(data.get("EvenPresentation", False))
    subset = to_int(data.get("TotalRandSubset"))
    if rtype == "None":
        return Randomization(mode="none", flip_scale=True) if flip else None
    if rtype == "ScaleReversal":  # only the scale direction is randomized
        return Randomization(mode="none", flip_scale=True)
    if rtype == "All":
        return Randomization(mode="all", even_presentation=even, flip_scale=flip)
    if rtype in {"SubSet", "Subset", "RandomWithOnlyX"}:
        return Randomization(
            mode="subset", subset_size=subset, even_presentation=even, flip_scale=flip
        )
    if rtype == "Advanced":
        return parse_advanced(data.get("Advanced"), even=even, flip=flip)
    return None


def parse_advanced(adv: Any, *, even: bool = False, flip: bool = False) -> Randomization:
    adv = adv if isinstance(adv, dict) else {}
    slots = [("*" if str(s) == RANDOMIZED_SLOT else str(s)) for s in adv.get("FixedOrder") or []]
    subset = to_int(adv.get("TotalRandSubset")) or None
    per_page = to_int(adv.get("QuestionsPerPage")) or None
    return Randomization(
        mode="advanced",
        slots=slots or None,
        randomized=[str(x) for x in adv.get("RandomizeAll") or []],
        undisplayed=[str(x) for x in adv.get("Undisplayed") or []],
        subset_size=subset,
        per_page=per_page,
        even_presentation=even,
        flip_scale=flip or truthy(adv.get("ScaleReversal", False)),
    )


def parse_validation(data: Any, diags: Diagnostics, where: str) -> Validation | None:
    if not isinstance(data, dict):
        return None  # Qualtrics writes "None" (a string) on some question types
    settings = Payload(data.get("Settings"))
    force = str(settings.get("ForceResponse", "OFF"))
    force_type = str(settings.get("ForceResponseType", ""))
    if force == "RequestResponse" or (force == "ON" and force_type == "RequestResponse"):
        required = "request"
    elif force == "ON":
        required = "force"
    else:
        required = "off"

    vtype = settings.get("Type")
    vtype = None if vtype in (None, "", "None") else str(vtype)
    number = None
    vn = settings.get("ValidNumber")
    if isinstance(vn, dict):
        number = NumberRange(
            min=to_number(vn.get("Min")),
            max=to_number(vn.get("Max")),
            decimals=to_int(vn.get("NumDecimals")),
        )
        if number == NumberRange():
            number = None

    custom = None
    custom_message = None
    cv = settings.get("CustomValidation")
    if isinstance(cv, dict):
        custom = parse_logic(cv.get("Logic"), diags, f"{where}.Validation")
        msg = cv.get("Message")
        if isinstance(msg, dict):
            custom_message = msg.get("description")

    # Settings carry stale values from earlier validation types, so only read the
    # fields the active type uses.
    content = vtype in ("ContentType", "ValidNumber")
    chars = vtype in ("MinChar", "TotalChar", "CharRange")
    choices = vtype in ("MinChoices", "ChoiceRange", "SelectMany")
    validation = Validation(
        required=required,
        type=vtype,
        content_type=settings.get("ContentType") if content else None,
        number=number if content and settings.data.get("ContentType") == "ValidNumber" else None,
        min_chars=to_int(settings.get("MinChars")) if vtype in ("MinChar", "CharRange") else None,
        max_chars=to_int(settings.get("TotalChars"))
        if vtype in ("TotalChar", "CharRange")
        else to_int(settings.get("MaxChars"))
        if chars
        else None,
        min_choices=to_int(settings.get("MinChoices")) if choices else None,
        max_choices=to_int(settings.get("MaxChoices")) if choices else None,
        total=to_number(settings.get("ChoiceTotal")) if vtype == "ChoicesTotal" else None,
        custom=custom,
        custom_message=custom_message,
        extras=settings.rest(),
    )
    if required == "off" and vtype is None and custom is None:
        return None
    return validation


def parse_carry_forward(dyn: Any) -> CarryForward | None:
    """Parse ``DynamicChoices`` (carry forward / reference-list choices)."""
    if not isinstance(dyn, dict) or not dyn.get("Locator"):
        return None
    raw = str(dyn["Locator"])
    path, _, query = raw.partition("?")
    options = dict(parse_qsl(query))
    if path.startswith("refl://"):
        parts = path.removeprefix("refl://").split("/")
        return CarryForward(
            source="reference_list",
            list_id=parts[0],
            mode="/".join(parts[1:]),
            options=options,
            raw=raw,
        )
    m = re.match(r"^q://([^/]+)/ChoiceGroup/([^/]+)(?:/(.+))?$", path)
    if not m:
        return CarryForward(source="other", mode=path, options=options, raw=raw)
    return CarryForward(
        source="question",
        question_id=unquote(m.group(1)),
        mode=m.group(2),
        answer_id=m.group(3),
        options=options,
        raw=raw,
    )


def _image(data: Any) -> Media | None:
    if isinstance(data, dict) and data.get("ImageLocation"):
        return Media(id=str(data["ImageLocation"]), filename=data.get("Display"))
    return None


def _regions(raw: Any, labels: dict[str, str]) -> list[Region]:
    regions = []
    for r in raw if isinstance(raw, list) else []:
        if not isinstance(r, dict):
            continue
        shapes = r.get("Shapes") or []
        outline = shapes[0] if shapes and isinstance(shapes[0], list) else []
        cid = r.get("ChoiceID")
        regions.append(
            Region(
                id=str(cid) if cid is not None else None,
                label=labels.get(str(cid)) or r.get("Description"),
                shape=str(r.get("Type") or "Polygon"),
                points=[
                    (float(pt["X"]), float(pt["Y"]))
                    for pt in outline
                    if isinstance(pt, dict) and "X" in pt and "Y" in pt
                ],
                x=to_number(r.get("X")),
                y=to_number(r.get("Y")),
                width=to_number(r.get("Width")),
                height=to_number(r.get("Height")),
            )
        )
    return regions


def _labels(value: Any) -> dict[str, str]:
    """An id -> label map; labels are occasionally numbers in the source."""
    return {k: str(v) for k, v in as_mapping(value).items() if v not in (None, "", False)}


class _QuestionReader:
    """Holds one SQ payload while its parts are mapped."""

    def __init__(self, payload: dict[str, Any], diags: Diagnostics) -> None:
        self.p = Payload(payload)
        self.diags = diags
        self.qid = str(self.p.get("QuestionID", ""))
        self.p.drop(*DROPPED_QUESTION_KEYS)
        self.config = self.p.sub("Configuration")
        self.variable_naming = _labels(self.p.get("VariableNaming"))
        tags = self.p.get("ChoiceDataExportTags")
        self.choice_tags = _labels(tags) if tags else {}
        self.recodes: dict[str, Any] = as_mapping(self.p.get("RecodeValues"))
        self.naming_used = False
        self.recodes_used = False

    # ---------------------------------------------------------------- choices

    def _order(self, items: dict[str, Any], order: Any, what: str) -> list[str]:
        keys = list(items)
        if not isinstance(order, list) or not order:
            return keys
        ordered = [str(x) for x in order]
        missing = [k for k in ordered if k not in items]
        if missing:
            self.diags.warn(
                "dangling-order",
                f"{what} order references ids with no definition: {missing}",
                self.qid,
            )
        result = [k for k in ordered if k in items]
        result += [k for k in keys if k not in result]
        return result

    def choices(self, *, naming: bool = True, recodes: bool = True) -> list[Choice]:
        raw = as_mapping(self.p.get("Choices"))
        order = self.p.get("ChoiceOrder")
        if not raw:
            return []
        self.naming_used |= naming
        self.recodes_used |= recodes
        result = []
        for cid in self._order(raw, order, "Choice"):
            c = Payload(raw[cid])
            display_logic = parse_logic(c.get("DisplayLogic"), self.diags, f"{self.qid}/{cid}")
            display = c.get("Display", "")
            if not c.has("Display") and c.has("display"):
                display = c.get("display")
            validation = c.get("TextEntryValidation")
            result.append(
                Choice(
                    id=cid,
                    text=make_text("" if display is None else str(display)),
                    recode=recode(self.recodes.get(cid)) if recodes else None,
                    variable_name=self.variable_naming.get(cid) if naming else None,
                    export_tag=self.choice_tags.get(cid) or None,
                    text_entry=truthy(c.get("TextEntry", False)),
                    text_entry_required=truthy(c.get("TextEntryForceResponse", False)),
                    text_entry_validation=str(validation) if validation else None,
                    exclusive=truthy(c.get("ExclusiveAnswer", False)),
                    display_logic=display_logic,
                    image=_image(c.get("Image")),
                    extras=c.rest(),
                )
            )
        return result

    def _scale_points(self, raw: dict[str, Any], order: Any) -> list[ScalePoint]:
        result = []
        for aid in self._order(raw, order, "Answer"):
            a = Payload(raw[aid])
            display = a.get("Display", "")
            result.append(
                ScalePoint(
                    id=aid,
                    text=make_text("" if display is None else str(display)),
                    recode=recode(self.recodes.get(aid)),
                    variable_name=self.variable_naming.get(aid),
                    exclusive=truthy(a.get("ExclusiveAnswer", False)),
                    extras=a.rest(),
                )
            )
        return result

    def scale_points(self) -> list[ScalePoint]:
        raw = as_mapping(self.p.get("Answers"))
        order = self.p.get("AnswerOrder")
        self.naming_used = self.recodes_used = True
        return self._scale_points(raw, order)

    def profile_scale_points(self) -> tuple[list[ScalePoint], dict[str, list[ScalePoint]]]:
        """Profile matrices nest answers per row: {row: {answer: {...}}}."""
        raw = as_mapping(self.p.get("Answers"))
        order = self.p.get("AnswerOrder")
        self.naming_used = self.recodes_used = True
        nested = raw and all(
            isinstance(v, dict)
            and "Display" not in v
            and v
            and all(isinstance(x, dict) for x in v.values())
            for v in raw.values()
        )
        if not nested:
            return self._scale_points(raw, order), {}
        per_row = {row: self._scale_points(as_mapping(v), order) for row, v in raw.items()}
        return [], per_row

    def choice_groups(self) -> list[ChoiceGroup]:
        groups = []
        for gid, g in as_mapping(self.p.get("ChoiceGroups")).items():
            if not isinstance(g, dict):
                continue
            options = as_mapping(g.get("Options"))
            groups.append(
                ChoiceGroup(
                    id=gid,
                    label=html_to_text(str(g.get("GroupLabel") or "")),
                    choice_ids=[str(c) for c in g.get("ChoiceGroupOrder") or []],
                    selection=options.get("Selection"),
                )
            )
        if not groups:
            self.p.drop("ChoiceGroupOrder")
        return groups

    def carry_forward(self) -> CarryForward | None:
        cf = parse_carry_forward(self.p.get("DynamicChoices"))
        self.p.drop("DynamicChoicesData")  # editor cache of the carried choices
        return cf

    # ---------------------------------------------------------------- common

    def common(self, kind_type: str) -> dict[str, Any]:
        p = self.p
        translations: dict[str, Translation] = {}
        for lang, tdata in as_mapping(p.get("Language")).items():
            t = Payload(tdata)
            translations[lang] = Translation(
                text=make_text(t.get("QuestionText")) if t.has("QuestionText") else None,
                choices={
                    k: make_text(str(v.get("Display") or ""))
                    for k, v in as_mapping(t.get("Choices")).items()
                    if isinstance(v, dict)
                },
                answers={
                    k: make_text(str(v.get("Display") or ""))
                    for k, v in as_mapping(t.get("Answers")).items()
                    if isinstance(v, dict)
                },
                extras=t.rest(),
            )
        media = []
        if p.get("Graphics"):
            media.append(
                Media(id=str(p.get("Graphics")), description=p.get("GraphicsDescription") or None)
            )
        else:
            p.drop("GraphicsDescription")

        scores = []
        grading = p.get("GradingData")
        if isinstance(grading, list):
            for g in grading:
                if not isinstance(g, dict):
                    continue
                for cat, pts in as_mapping(g.get("Grades")).items():
                    n = to_number(pts)
                    if n is not None:
                        scores.append(
                            Score(
                                choice_id=str(g.get("ChoiceID")),
                                answer_id=str(g["AnswerID"])
                                if g.get("AnswerID") is not None
                                else None,
                                category=cat,
                                points=n,
                            )
                        )
        default = p.get("DefaultChoices")
        js = p.get("QuestionJS")
        description = p.get("QuestionDescription")
        text = p.get("QuestionText", "")
        selector = p.data.get("Selector")
        sub = p.data.get("SubSelector")
        return {
            "id": self.qid,
            "export_tag": str(p.get("DataExportTag") or self.qid),
            "origin": Origin(
                format="qualtrics",
                type=kind_type,
                selector=first_str(selector) or None,
                sub_selector=first_str(sub) or None,
            ),
            "text": make_text("" if text is None else str(text)),
            "description": html_to_text(description) if isinstance(description, str) else None,
            "display_logic": parse_logic(p.get("DisplayLogic"), self.diags, self.qid),
            "validation": parse_validation(p.get("Validation"), self.diags, self.qid),
            "translations": translations,
            "media": media,
            "scoring": scores,
            "default_value": default or None,
            "javascript": js if isinstance(js, str) and js.strip() else None,
        }

    def finish(self, fields: dict[str, Any]) -> dict[str, Any]:
        extras = self.p.rest()
        config_rest = self.config.rest()
        if config_rest:
            extras["Configuration"] = config_rest
        else:
            extras.pop("Configuration", None)
        if self.variable_naming and not self.naming_used:
            extras["VariableNaming"] = self.variable_naming
        if self.recodes and not self.recodes_used:
            extras["RecodeValues"] = self.recodes
        in_page = self.p.data.get("InPageDisplayLogic")
        if in_page and not fields.get("display_logic"):
            # Logic evaluated on the same page as its source question.
            fields["display_logic"] = parse_logic(in_page, self.diags, self.qid)
            extras.pop("InPageDisplayLogic", None)
        fields["extras"] = extras
        return fields


# --------------------------------------------------------------------------- dispatch


def build_question(payload: dict[str, Any], diags: Diagnostics) -> Question:
    qtype = first_str(payload.get("QuestionType"))
    selector = first_str(payload.get("Selector"))
    sub = first_str(payload.get("SubSelector")) or "None"
    r = _QuestionReader(payload, diags)

    def build(cls, **fields):
        return cls(**r.finish({**r.common(qtype), **fields}))

    def unsupported(code: str, message: str) -> Question:
        diags.warn(code, message, r.qid)
        return UnsupportedQuestion(**{**r.common(qtype), "extras": dict(payload)})

    plugin = payload.get("QuestionTypePluginProperties")
    if isinstance(plugin, dict) and plugin.get("ID"):
        return unsupported(
            "plugin-question",
            f"Question-type plugin {plugin.get('ID')!r} (base {qtype}/{selector}) is not "
            "modeled; kept verbatim in extras",
        )
    probable = _known_unmodeled(qtype, selector, sub)
    if probable is not None:
        return unsupported(
            "unsupported-question",
            f"{probable} question ({qtype}/{selector}/{sub}) has no public example to model; "
            "kept verbatim in extras",
        )

    if qtype == "MC":
        multiple, layout = MC_LAYOUTS.get(selector, (None, "other"))
        if multiple is None:
            multiple = selector.startswith("MA") or sub == "MultipleAnswer"
            diags.info("unknown-selector", f"MC selector {selector!r} mapped to 'other'", r.qid)
        return build(
            ChoiceQuestion,
            multiple=multiple,
            layout=layout,
            columns=to_int(r.config.get("NumColumns")),
            choices=r.choices(),
            groups=r.choice_groups(),
            randomization=parse_randomization(r.p.get("Randomization")),
            carry_forward=r.carry_forward(),
        )

    if qtype == "Matrix":
        mode = MATRIX_MODES.get(selector) or (
            LIKERT_SUB_MODES.get(sub, "other") if selector == "Likert" else "other"
        )
        if mode == "other":
            diags.info("unknown-selector", f"Matrix {selector}/{sub} mapped to mode 'other'", r.qid)
        rand = r.p.get("Randomization")
        answer_rand = rand.get("Answers") if isinstance(rand, dict) else None
        rows = r.choices(naming=False, recodes=False)
        columns, row_columns = (
            r.profile_scale_points() if mode == "profile" else (r.scale_points(), {})
        )
        return build(
            MatrixQuestion,
            mode=mode,
            rows=rows,
            columns=columns,
            row_columns=row_columns,
            randomization=parse_randomization(rand),
            column_randomization=parse_randomization(answer_rand),
            carry_forward=r.carry_forward(),
        )

    if qtype == "TE":
        if not selector:  # some exports omit the selector; Qualtrics' default is single line
            diags.info("default-selector", "Text entry without selector read as single line", r.qid)
            selector = "SL"
        mode = TEXT_MODES.get(selector, "other")
        if mode == "other":
            diags.warn(
                "unknown-selector",
                f"Text entry selector {selector}/{sub} is not modeled; read as mode 'other'",
                r.qid,
            )
        return build(TextEntryQuestion, mode=mode, fields=r.choices() if mode == "form" else [])

    if qtype == "Slider":
        labels_raw = as_mapping(r.p.get("Labels"))
        labels = [
            html_to_text(str((labels_raw[k] or {}).get("Display") or ""))
            for k in sorted(labels_raw, key=lambda k: to_int(k) or 0)
            if isinstance(labels_raw[k], dict)
        ]
        mode = SLIDER_MODES.get(selector, "other")
        lo, hi = to_number(r.config.get("CSSliderMin")), to_number(r.config.get("CSSliderMax"))
        if mode == "number_scale" and hi is None:
            points = to_int(r.config.get("NumScalePoints"))
            lo, hi = (0, points - 1) if points else (lo, hi)
        if mode == "star":
            lo, hi = 0, to_number(r.config.get("StarCount")) or hi
        starts = as_mapping(r.config.get("SliderStartPositions"))
        custom_start = truthy(r.config.get("CustomStart", False))
        na_label = r.config.get("NotApplicableText")
        not_applicable = (
            (str(na_label or "N/A")) if truthy(r.config.get("NotApplicable", False)) else None
        )
        return build(
            SliderQuestion,
            mode=mode,
            items=r.choices(),
            min=lo,
            max=hi,
            decimals=to_int(r.config.get("NumDecimals")),
            grid_lines=to_int(r.config.get("GridLines")),
            snap_to_grid=truthy(r.config.get("SnapToGrid", False)),
            labels=labels,
            show_value=truthy(r.config.get("ShowValue", False)),
            start_positions={k: float(v) for k, v in starts.items() if to_number(v) is not None}
            if custom_start
            else {},
            not_applicable=not_applicable,
            randomization=parse_randomization(r.p.get("Randomization")),
        )

    if qtype == "CS":
        return build(
            ConstantSumQuestion,
            mode=CS_MODES.get(selector, "other"),
            items=r.choices(),
            min=to_number(r.config.get("CSSliderMin")),
            max=to_number(r.config.get("CSSliderMax")),
            decimals=to_int(r.config.get("NumDecimals")),
            unit=r.p.get("ClarifyingSymbol") or None,
            unit_position=r.p.get("ClarifyingSymbolType") or None,
            randomization=parse_randomization(r.p.get("Randomization")),
        )

    if qtype == "RO":
        return build(
            RankOrderQuestion,
            mode=RANK_MODES.get(selector, "other"),
            items=r.choices(),
            randomization=parse_randomization(r.p.get("Randomization")),
        )

    if qtype == "DB":
        if not as_mapping(r.p.data.get("Choices")):
            r.p.drop("ChoiceOrder", "Choices")  # always empty on descriptive blocks
        return build(DescriptiveQuestion, mode=DB_MODES.get(selector, "other"))

    if qtype == "Timing":
        r.p.drop("Choices", "ChoiceOrder")  # fixed labels of the four timing columns
        min_s = to_number(r.config.get("MinSeconds"))
        max_s = to_number(r.config.get("MaxSeconds"))
        countdown = to_number(r.config.get("Seconds"))
        decrementing = r.config.get("CountDirection") == "Decrementing" or selector == "D"
        return build(
            TimingQuestion,
            min_seconds=min_s or None,
            auto_advance_seconds=max_s or None,
            show_timer=truthy(r.config.get("ShowTimer", False)),
            countdown_seconds=(countdown or min_s or None) if decrementing else None,
        )

    if qtype == "Meta":
        r.p.drop("Choices", "ChoiceOrder")
        return build(MetaInfoQuestion)

    if qtype == "Captcha":
        return build(CaptchaQuestion)

    if qtype == "SBS":
        subs = []
        for key, sub_payload in sorted(
            as_mapping(r.p.get("AdditionalQuestions")).items(), key=lambda kv: to_int(kv[0]) or 0
        ):
            if not isinstance(sub_payload, dict):
                continue
            sub_payload = dict(sub_payload)
            sub_payload.setdefault("QuestionID", f"{r.qid}#{key}")
            subs.append(build_question(sub_payload, diags))
        r.p.drop("NumberOfQuestions")
        return build(
            SideBySideQuestion,
            rows=r.choices(),
            questions=subs,
            randomization=parse_randomization(r.p.get("Randomization")),
            carry_forward=r.carry_forward(),
        )

    if qtype == "DD":
        levels = r.choices(naming=False, recodes=False)
        return build(DrillDownQuestion, levels=levels, options=r.scale_points())

    if qtype == "PGR":
        groups = r.p.get("Groups")
        r.p.drop("NumberOfGroups")
        return build(
            PickGroupRankQuestion,
            items=r.choices(),
            groups=[html_to_text(str(g)) for g in (groups if isinstance(groups, list) else [])],
            columns=sub == "Columns",
            randomization=parse_randomization(r.p.get("Randomization")),
            carry_forward=r.carry_forward(),
        )

    if qtype == "HL":
        words = r.choices()
        for w in words:  # Display is "<n>: <word>"; the word itself is in extras
            if "Word" in w.extras:
                w.text = make_text(str(w.extras.pop("Word")))
        return build(
            HighlightQuestion,
            passage=str(r.p.get("HighlightText") or ""),
            words=words,
            categories=r.scale_points(),
        )

    if qtype == "HotSpot":
        labels = {c.id: c.text.plain for c in r.choices()}
        image = r.p.get("GraphicID")
        return build(
            HotSpotQuestion,
            mode=HOTSPOT_MODES.get(selector, "other"),
            image=Media(id=str(image)) if image else None,
            regions=_regions(r.p.get("Regions"), labels),
            states=r.scale_points(),
        )

    if qtype == "HeatMap":
        image = r.p.get("GraphicID")
        return build(
            HeatMapQuestion,
            image=Media(id=str(image)) if image else None,
            regions=_regions(r.p.get("Regions"), {}),
            max_clicks=to_int(r.p.get("Clicks")),
        )

    if qtype == "SS":
        return build(
            GraphicSliderQuestion,
            graphic=r.p.get("Category"),
            scale=r.p.get("Scale"),
            points=r.choices(),
            direction=r.p.get("Direction"),
        )

    if qtype == "FileUpload":
        fields = r.common(qtype)
        v = fields["validation"]
        if v is not None and v.content_type:
            v.file_type, v.content_type = v.content_type, None
        if selector not in ("", "FileUpload"):
            diags.warn(
                "unknown-selector",
                f"File upload selector {selector}/{sub} is not modeled; read as plain upload",
                r.qid,
            )
        return FileUploadQuestion(**r.finish(fields))

    if qtype == "Draw":
        return build(SignatureQuestion)

    if not qtype:
        return unsupported("question-without-type", "Question has no QuestionType; kept verbatim")
    return unsupported(
        "unsupported-question",
        f"Question type {qtype}/{selector}/{sub} is not modeled; kept verbatim in extras",
    )
