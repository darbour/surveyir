"""Question models.

Questions form a discriminated union on ``kind``. Each kind holds the typed
fields a simulator or analyst needs; anything platform-specific that has no
typed home is kept in ``extras``. Question types surveyir does not model yet
load as ``UnsupportedQuestion`` with the full source payload, never as a
silently-skipped descriptive block.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from .base import Extensible, IRModel
from .logic import Condition
from .text import Text

# --------------------------------------------------------------------------- parts


class Origin(IRModel):
    """Where a question came from, in the source platform's own terms."""

    format: str = Field(description="Source format, e.g. 'qualtrics'.")
    type: str = Field(description="Source question type, e.g. 'MC'.")
    selector: str | None = Field(default=None, description="Source layout selector, e.g. 'SAVR'.")
    sub_selector: str | None = Field(default=None, description="Source sub-selector, e.g. 'TX'.")


class Media(IRModel):
    id: str = Field(description="Platform media/library id, e.g. 'IM_abc123'.")
    description: str | None = None
    filename: str | None = None


class Choice(Extensible):
    """A selectable option (or a row/item, depending on the question kind)."""

    id: str = Field(description="Stable source id, always a string.")
    text: Text
    recode: int | float | str | None = Field(
        default=None, description="Value written to the data file when chosen."
    )
    variable_name: str | None = Field(
        default=None, description="Label used for this choice in exported data."
    )
    export_tag: str | None = Field(
        default=None, description="Per-choice export column tag override."
    )
    text_entry: bool = Field(default=False, description="Choice has an attached free-text box.")
    text_entry_required: bool = Field(
        default=False, description="The attached text box must be filled when chosen."
    )
    text_entry_validation: str | None = Field(
        default=None, description="Content check on the attached text box, e.g. 'ValidNumber'."
    )
    exclusive: bool = Field(default=False, description="Selecting it deselects all others.")
    display_logic: Condition | None = None
    image: Media | None = None


class ScalePoint(Extensible):
    """A column of a matrix question (Qualtrics calls these 'answers')."""

    id: str
    text: Text
    recode: int | float | str | None = None
    variable_name: str | None = None
    exclusive: bool = Field(default=False, description="Selecting it clears other columns.")


class ChoiceGroup(IRModel):
    """A labeled group of choices shown together (Qualtrics 'choice groups')."""

    id: str
    label: str
    choice_ids: list[str] = Field(default_factory=list)
    selection: str | None = Field(
        default=None, description="Source selection rule, e.g. 'SAWithinQuestion'."
    )


class Randomization(IRModel):
    """How the order of a list (choices, rows, questions, loops) is randomized."""

    mode: Literal["none", "all", "subset", "advanced"]
    subset_size: int | None = Field(
        default=None, description="Show only this many randomly selected elements."
    )
    even_presentation: bool = False
    slots: list[str] | None = Field(
        default=None,
        description=(
            "Advanced mode: the presentation order with fixed ids in place and '*' for each "
            "position filled from `randomized`."
        ),
    )
    randomized: list[str] = Field(default_factory=list)
    undisplayed: list[str] = Field(default_factory=list)
    per_page: int | None = Field(default=None, description="Questions per page (blocks only).")
    flip_scale: bool = Field(
        default=False, description="Randomly reverse the scale, consistently across questions."
    )


class CarryForward(IRModel):
    """Choices filled in at run time from an earlier question or a reference list."""

    source: Literal["question", "reference_list", "other"] = "question"
    question_id: str | None = Field(default=None, description="Source question (question source).")
    list_id: str | None = Field(default=None, description="Reference list id (reference_list).")
    mode: str = Field(
        description="Which choices are carried, as named by the source: 'SelectedChoices', "
        "'UnselectedChoices', 'DisplayedChoices', 'AllChoices', 'SelectedChoicesTextEntry', "
        "'EnteredChoicesTextEntry', 'UnselectedChoicesForAnswer', 'ActiveSelections', ..."
    )
    answer_id: str | None = Field(
        default=None,
        description="Scale point, for per-answer modes such as 'UnselectedChoicesForAnswer/1'.",
    )
    options: dict[str, str] = Field(
        default_factory=dict, description="Locator query options, e.g. {'displayLogic': '0'}."
    )
    raw: str


class NumberRange(IRModel):
    min: float | None = None
    max: float | None = None
    decimals: int | None = None


class Region(IRModel):
    """A clickable area of an image (hot spot and heat map questions)."""

    id: str | None = Field(default=None, description="Choice id the region reports as, if any.")
    label: str | None = None
    shape: str = Field(default="Polygon", description="Source shape type, e.g. 'Polygon'.")
    points: list[tuple[float, float]] = Field(
        default_factory=list, description="Outline vertices (x, y) in image pixels."
    )
    x: float | None = None
    y: float | None = None
    width: float | None = None
    height: float | None = None


class Validation(Extensible):
    """Response requirements. ``None`` on a question means no requirement."""

    required: Literal["off", "force", "request"] = "off"
    type: str | None = Field(
        default=None, description="Source validation type, e.g. 'ContentType', 'MinChar'."
    )
    content_type: str | None = Field(
        default=None, description="e.g. 'ValidNumber', 'ValidEmail', 'ValidZip'."
    )
    number: NumberRange | None = None
    min_chars: int | None = None
    max_chars: int | None = None
    min_choices: int | None = None
    max_choices: int | None = None
    file_type: str | None = Field(
        default=None, description="Allowed upload type for file-upload questions, e.g. 'Graphic'."
    )
    total: float | None = Field(default=None, description="Required sum (constant sum).")
    custom: Condition | None = None
    custom_message: str | None = None


class Translation(Extensible):
    text: Text | None = None
    choices: dict[str, Text] = Field(default_factory=dict)
    answers: dict[str, Text] = Field(default_factory=dict)


class Score(IRModel):
    """Points awarded for a choice (or choice/answer pair) in a scoring category."""

    choice_id: str
    answer_id: str | None = None
    category: str
    points: float


# --------------------------------------------------------------------------- kinds


class QuestionBase(Extensible):
    id: str = Field(description="Stable source id, e.g. 'QID12'.")
    export_tag: str = Field(description="Data export name, e.g. 'Q12' or 'age'.")
    origin: Origin
    text: Text
    description: str | None = Field(default=None, description="Short label shown in editors.")
    display_logic: Condition | None = None
    in_page_display_logic: Condition | None = Field(
        default=None,
        description=(
            "Display logic Qualtrics evaluates on the page itself, as earlier questions on "
            "the same page are answered (``InPageDisplayLogic``)."
        ),
    )
    validation: Validation | None = None
    translations: dict[str, Translation] = Field(default_factory=dict)
    media: list[Media] = Field(default_factory=list)
    scoring: list[Score] = Field(default_factory=list)
    default_value: Any = Field(default=None, description="Pre-filled response, source format.")
    javascript: str | None = Field(default=None, description="Custom question JavaScript.")


class ChoiceQuestion(QuestionBase):
    """Pick one or many options (radio buttons, checkboxes, dropdowns, NPS)."""

    kind: Literal["choice"] = "choice"
    multiple: bool = Field(description="True if more than one choice may be selected.")
    layout: Literal[
        "vertical", "horizontal", "columns", "dropdown", "select_box", "nps", "other"
    ] = "vertical"
    columns: int | None = None
    choices: list[Choice] = Field(description="In default presentation order.")
    groups: list[ChoiceGroup] = Field(default_factory=list)
    randomization: Randomization | None = None
    carry_forward: CarryForward | None = None


class MatrixQuestion(QuestionBase):
    """A grid of statements (rows) rated on a shared scale (columns)."""

    kind: Literal["matrix"] = "matrix"
    mode: Literal[
        "single",
        "multiple",
        "dropdown",
        "text",
        "bipolar",
        "rank_order",
        "constant_sum",
        "max_diff",
        "profile",
        "other",
    ]
    rows: list[Choice] = Field(description="Statements, in default presentation order.")
    columns: list[ScalePoint] = Field(description="Scale points, in presentation order.")
    row_columns: dict[str, list[ScalePoint]] = Field(
        default_factory=dict,
        description="Per-row scale points, when each row has its own (profile mode).",
    )
    randomization: Randomization | None = None
    column_randomization: Randomization | None = None
    carry_forward: CarryForward | None = None


class TextEntryQuestion(QuestionBase):
    kind: Literal["text_entry"] = "text_entry"
    mode: Literal["single_line", "multi_line", "essay", "form", "password", "autocomplete", "other"]
    fields: list[Choice] = Field(default_factory=list, description="Form fields (form mode).")


class SliderQuestion(QuestionBase):
    kind: Literal["slider"] = "slider"
    mode: Literal["slider", "bar", "star", "number_scale", "other"] = "slider"
    items: list[Choice]
    min: float | None = None
    max: float | None = None
    decimals: int | None = None
    grid_lines: int | None = None
    snap_to_grid: bool = False
    labels: list[str] = Field(default_factory=list, description="Scale labels along the slider.")
    show_value: bool = False
    start_positions: dict[str, float] = Field(
        default_factory=dict,
        description="Initial handle position per item, as a fraction (0-1) of the scale. "
        "Empty means handles start at the minimum / unset.",
    )
    not_applicable: str | None = Field(
        default=None, description="Label of an extra 'not applicable' option, if offered."
    )
    randomization: Randomization | None = None


class ConstantSumQuestion(QuestionBase):
    kind: Literal["constant_sum"] = "constant_sum"
    mode: Literal["text_boxes", "bars", "sliders", "other"] = "text_boxes"
    items: list[Choice]
    min: float | None = None
    max: float | None = None
    decimals: int | None = None
    unit: str | None = Field(default=None, description="Symbol shown with values, e.g. '%'.")
    unit_position: str | None = Field(default=None, description="'Before' or 'After'.")
    randomization: Randomization | None = None


class RankOrderQuestion(QuestionBase):
    kind: Literal["rank_order"] = "rank_order"
    mode: Literal["drag_and_drop", "radio_buttons", "text_box", "select_box", "other"]
    items: list[Choice]
    randomization: Randomization | None = None


class DescriptiveQuestion(QuestionBase):
    """Text or graphics shown to the respondent; collects no response."""

    kind: Literal["descriptive"] = "descriptive"
    mode: Literal["text", "graphic", "other"] = "text"


class TimingQuestion(QuestionBase):
    """Invisible page timer; records click/submit times."""

    kind: Literal["timing"] = "timing"
    min_seconds: float | None = Field(
        default=None, description="Next button hidden until this many seconds."
    )
    auto_advance_seconds: float | None = Field(
        default=None, description="Page auto-submits after this many seconds."
    )
    show_timer: bool = False
    countdown_seconds: float | None = Field(
        default=None, description="Visible countdown length (decrementing timers)."
    )


class MetaInfoQuestion(QuestionBase):
    """Invisible collection of browser, OS and screen resolution."""

    kind: Literal["meta_info"] = "meta_info"


class CaptchaQuestion(QuestionBase):
    kind: Literal["captcha"] = "captcha"


class SideBySideQuestion(QuestionBase):
    """Several questions about the same statements, shown as adjacent column groups.

    Each entry in ``questions`` is a full question (usually a matrix) whose rows
    repeat ``rows``. Sub-question ids look like ``QID5#1``.
    """

    kind: Literal["side_by_side"] = "side_by_side"
    rows: list[Choice]
    questions: list[Question]
    randomization: Randomization | None = None
    carry_forward: CarryForward | None = None


class DrillDownQuestion(QuestionBase):
    """Cascading dropdowns: each level's options depend on the previous selection."""

    kind: Literal["drill_down"] = "drill_down"
    levels: list[Choice] = Field(description="One entry per dropdown level.")
    options: list[ScalePoint] = Field(description="All selectable options across levels.")


class PickGroupRankQuestion(QuestionBase):
    """Drag items into named groups, optionally ranking within each group."""

    kind: Literal["pick_group_rank"] = "pick_group_rank"
    items: list[Choice]
    groups: list[str]
    columns: bool = Field(default=False, description="Groups laid out as columns.")
    randomization: Randomization | None = None
    carry_forward: CarryForward | None = None


class HighlightQuestion(QuestionBase):
    """Respondents mark words in a passage with one of several categories."""

    kind: Literal["highlight"] = "highlight"
    passage: str
    words: list[Choice] = Field(description="Selectable words; text is the word itself.")
    categories: list[ScalePoint] = Field(description="Highlight categories, e.g. like/dislike.")


class HotSpotQuestion(QuestionBase):
    """Respondents click predefined regions of an image to toggle or rate them."""

    kind: Literal["hot_spot"] = "hot_spot"
    mode: Literal["on_off", "like_dislike", "other"]
    image: Media | None = None
    regions: list[Region]
    states: list[ScalePoint] = Field(
        default_factory=list, description="Rating states (like/neutral/dislike)."
    )


class HeatMapQuestion(QuestionBase):
    """Respondents click anywhere on an image; clicks are reported by region."""

    kind: Literal["heat_map"] = "heat_map"
    image: Media | None = None
    regions: list[Region] = Field(default_factory=list)
    max_clicks: int | None = None


class GraphicSliderQuestion(QuestionBase):
    """A single rating shown as a graphic (bars, gauges, faces, ...)."""

    kind: Literal["graphic_slider"] = "graphic_slider"
    graphic: str | None = Field(default=None, description="Graphic family, e.g. 'Bars'.")
    scale: str | None = Field(default=None, description="Graphic preset, e.g. 'TenGauge'.")
    points: list[Choice] = Field(description="Scale points, low to high.")
    direction: str | None = None


class FileUploadQuestion(QuestionBase):
    """Respondent uploads a file (allowed type is in ``validation.file_type``)."""

    kind: Literal["file_upload"] = "file_upload"


class SignatureQuestion(QuestionBase):
    """Respondent draws a signature."""

    kind: Literal["signature"] = "signature"


class UnsupportedQuestion(QuestionBase):
    """A question type surveyir does not model yet. ``extras`` holds the full payload."""

    kind: Literal["unsupported"] = "unsupported"


Question = Annotated[
    ChoiceQuestion
    | MatrixQuestion
    | TextEntryQuestion
    | SliderQuestion
    | ConstantSumQuestion
    | RankOrderQuestion
    | DescriptiveQuestion
    | TimingQuestion
    | MetaInfoQuestion
    | CaptchaQuestion
    | SideBySideQuestion
    | DrillDownQuestion
    | PickGroupRankQuestion
    | HighlightQuestion
    | HotSpotQuestion
    | HeatMapQuestion
    | GraphicSliderQuestion
    | FileUploadQuestion
    | SignatureQuestion
    | UnsupportedQuestion,
    Field(discriminator="kind"),
]

SideBySideQuestion.model_rebuild()

#: Kinds that never collect a respondent answer.
NON_RESPONSE_KINDS = frozenset({"descriptive", "timing", "meta_info", "captcha"})
