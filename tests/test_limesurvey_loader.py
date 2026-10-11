"""The LimeSurvey loader: every question type, every file generation, logic and flow."""

from __future__ import annotations

from pathlib import Path

import pytest

import surveyir
from surveyir.describe import describe_condition
from surveyir.loaders.limesurvey import LimeSurveyLoader, load_lss
from surveyir.loaders.qualtrics import LoadError
from surveyir.model import (
    And,
    BlockNode,
    BranchNode,
    ChoiceQuestion,
    Comparison,
    EmbeddedDataNode,
    EquationQuestion,
    Expression,
    MatrixQuestion,
    PageBreak,
    QuotaNode,
    RandomizerNode,
    RankOrderQuestion,
    SideBySideQuestion,
    TextEntryQuestion,
)

FIX = Path(__file__).parent / "fixtures" / "limesurvey"
CITRIC = FIX / "citric"


@pytest.fixture(scope="module")
def types():
    return load_lss(FIX / "types_v6.lss", strict=True)


@pytest.fixture(scope="module")
def logic():
    return load_lss(FIX / "logic.lss", strict=True)


def test_dispatch_by_extension_and_format():
    assert surveyir.load(FIX / "types_v6.lss").source.format == "limesurvey.lss"
    assert surveyir.load(FIX / "responses.lsa").source.format == "limesurvey.lss"
    assert "limesurvey" in surveyir.available_loaders()


def test_survey_level(types):
    assert (types.id, types.name, types.language, types.languages) == (
        "111111",
        "Question types",
        "en",
        ["de"],
    )
    assert types.source.format_version == "498"
    assert list(types.blocks) == ["welcome", "G1", "G2", "G3"]
    assert [b.description for b in types.blocks.values()] == [
        "Welcome",
        "Choices",
        "Arrays",
        "Entry",
    ]
    assert types.diagnostics == []


@pytest.mark.parametrize(
    "qid, kind",
    [
        ("colour", "choice"),
        ("country", "choice"),
        ("brand", "choice"),
        ("five", "choice"),
        ("gender", "choice"),
        ("agree", "choice"),
        ("pets", "choice"),
        ("media", "choice"),
        ("rate", "matrix"),
        ("bycol", "matrix"),
        ("a5", "matrix"),
        ("b10", "matrix"),
        ("ync", "matrix"),
        ("isd", "matrix"),
        ("dual", "side_by_side"),
        ("nums", "matrix"),
        ("texts", "matrix"),
        ("alloc", "text_entry"),
        ("names", "text_entry"),
        ("short", "text_entry"),
        ("long", "text_entry"),
        ("huge", "text_entry"),
        ("age", "text_entry"),
        ("when", "text_entry"),
        ("rank", "rank_order"),
        ("info", "descriptive"),
        ("score", "equation"),
        ("upload", "file_upload"),
    ],
)
def test_every_type_maps_to_a_kind(types, qid, kind):
    assert types.questions[qid].kind == kind


def test_lists(types):
    q = types.questions["colour"]
    assert isinstance(q, ChoiceQuestion) and not q.multiple
    assert [c.id for c in q.choices] == ["A1", "A2", "A3", "-oth-"]  # mandatory: no "No answer"
    assert q.choices[-1].text_entry and q.validation and q.validation.required == "force"
    assert "Pick one." in q.text.plain  # help text is shown with the question
    assert q.translations["de"].choices["A2"].plain == "Grün"
    country = types.questions["country"]
    assert isinstance(country, ChoiceQuestion) and country.layout == "dropdown"
    assert [c.id for c in country.choices] == ["FR", "DE", ""]  # optional: "No answer"
    five = types.questions["five"]
    assert isinstance(five, ChoiceQuestion)
    assert [c.id for c in five.choices] == ["1", "2", "3", "4", "5", ""]


def test_multiple_choice(types):
    q = types.questions["pets"]
    assert isinstance(q, ChoiceQuestion) and q.multiple
    assert [c.id for c in q.choices] == ["SQ001", "SQ002", "SQ003", "SQ004", "other"]
    assert [c.exclusive for c in q.choices] == [False, False, False, True, False]
    media = types.questions["media"]
    assert isinstance(media, ChoiceQuestion)
    assert all(c.text_entry for c in media.choices)  # P: a comment per option


def test_arrays(types):
    rate = types.questions["rate"]
    assert isinstance(rate, MatrixQuestion) and rate.mode == "single"
    assert [r.id for r in rate.rows] == ["SQ001", "SQ002"]
    assert [c.id for c in rate.columns] == ["A1", "A2", "A3", ""]
    assert rate.columns[0].extras == {"assessment_value": "1"}
    ync = types.questions["ync"]
    assert isinstance(ync, MatrixQuestion)
    assert [c.text.plain for c in ync.columns] == ["Yes", "Uncertain", "No", "No answer"]
    nums = types.questions["nums"]
    assert isinstance(nums, MatrixQuestion) and nums.mode == "text"
    assert [r.id for r in nums.rows] == ["R1", "R2"]
    assert [c.id for c in nums.columns] == ["C1", "C2"]
    assert nums.validation and nums.validation.content_type == "ValidNumber"
    dual = types.questions["dual"]
    assert isinstance(dual, SideBySideQuestion)
    assert [s.id for s in dual.questions] == ["dual#0", "dual#1"]
    assert [s.text.plain for s in dual.questions] == ["Now", "Before"]
    assert [[c.id for c in s.columns] for s in dual.questions] == [
        ["A1", "A2", ""],
        ["B1", "B2", ""],
    ]


def test_entry_types(types):
    age = types.questions["age"]
    assert isinstance(age, TextEntryQuestion) and age.validation
    assert age.validation.content_type == "ValidNumber"
    assert age.validation.number and (age.validation.number.min, age.validation.number.max) == (
        18,
        99,
    )
    assert age.validation.number.decimals == 0
    short = types.questions["short"]
    assert short.validation and short.validation.max_chars == 10
    when = types.questions["when"]
    assert when.validation and when.validation.content_type == "ValidDate"
    alloc = types.questions["alloc"]
    assert isinstance(alloc, TextEntryQuestion) and alloc.mode == "form"
    assert alloc.validation and alloc.validation.expression is not None
    assert alloc.validation.custom_message == "The sum must equal 100."
    rank = types.questions["rank"]
    assert isinstance(rank, RankOrderQuestion) and [i.id for i in rank.items] == ["R1", "R2", "R3"]
    score = types.questions["score"]
    assert isinstance(score, EquationQuestion) and score.hidden
    assert score.expression.pipes[0].raw == "{sum(age, 1)}"
    upload = types.questions["upload"]
    assert upload.validation and upload.validation.file_type == "png, jpg"


def test_piping_resolves_codes(types):
    info = types.questions["info"]
    (pipe,) = info.text.pipes
    assert pipe.kind == "expression" and pipe.question_id == "colour"
    assert info.text.syntax == "expression_script"


def _strip(survey):
    d = survey.model_dump(mode="json", exclude_defaults=True)
    for key in ("source", "extras", "options"):
        d.pop(key, None)

    def clean(x):
        if isinstance(x, dict):
            return {k: clean(v) for k, v in x.items() if k != "extras"}
        if isinstance(x, list):
            return [clean(v) for v in x]
        return x

    return clean(d)


def test_three_generations_load_alike(types):
    v3 = load_lss(FIX / "types_v3.lss", strict=True)
    v7 = load_lss(FIX / "types_v7.lss", strict=True)
    assert _strip(v3) == _strip(types) == _strip(v7)
    assert (v3.extras["era"], types.extras["era"], v7.extras["era"]) == ("legacy", "l10n", "l10n")


def test_relevance_lowered_and_placed(logic):
    dogname = logic.questions["dogname"]
    assert isinstance(dogname.display_logic, Comparison)
    assert dogname.display_logic.operator == "selected"
    why = logic.questions["why"]  # depends on a question on its own page
    assert why.display_logic is None and isinstance(why.in_page_display_logic, Comparison)
    hello = logic.questions["hello"]
    assert isinstance(hello.display_logic, Expression) and hello.display_logic.source == (
        "age >= 18"
    )


def test_array_filter_and_subquestion_relevance(logic):
    rows = {r.id: r for r in logic.questions["petrate"].rows}  # type: ignore[attr-defined]
    assert describe_condition(rows["SQ001"].display_logic, logic) == "pets is selected"
    sq3 = rows["SQ003"].display_logic
    assert isinstance(sq3, And) and isinstance(sq3.terms[0], Expression)


def test_flow(logic):
    kinds = [type(n).__name__ for n in logic.flow]
    assert kinds == [
        "EmbeddedDataNode",
        "BlockNode",  # the welcome page
        "BlockNode",
        "BranchNode",
        "RandomizerNode",
        "BlockNode",
    ]
    participant = logic.flow[0]
    assert isinstance(participant, EmbeddedDataNode)
    assert [(f.name, f.source) for f in participant.fields] == [("TOKEN:FIRSTNAME", "recipient")]
    branch = logic.flow[3]
    assert isinstance(branch, BranchNode)
    assert describe_condition(branch.condition, logic) == "consent = Y"
    assert [type(c).__name__ for c in branch.children] == ["BlockNode", "QuotaNode"]
    assert isinstance(branch.children[1], QuotaNode)
    rnd = logic.flow[4]
    assert isinstance(rnd, RandomizerNode)
    assert [c.block_id for c in rnd.children if isinstance(c, BlockNode)] == ["G12", "G13"]


def test_question_random_groups(logic):
    r = logic.blocks["G14"].randomization
    assert r is not None and (r.mode, r.randomized) == ("advanced", ["o1", "o2", "o3"])


def test_quotas(logic):
    (quota,) = logic.quotas
    assert (quota.name, quota.limit, quota.action) == ("Few dislikers", 1, "end_survey")
    assert describe_condition(quota.condition, logic) == "likes = A2"


def test_question_by_question_pages():
    s = load_lss(FIX / "conditions.lss", strict=True)
    block = s.blocks["G20"]
    questions = [q for q in block.question_ids if not q.endswith("_info")]
    assert sum(isinstance(e, PageBreak) for e in block.elements) == len(questions) - 1


def test_welcome_and_group_info(logic):
    welcome = logic.questions["welcome"]
    assert welcome.kind == "descriptive" and welcome.text.plain == "Welcome"
    info = logic.questions["G10_info"]
    assert info.kind == "descriptive" and info.text.plain == "Screen"
    assert logic.blocks["G10"].question_ids[0] == "G10_info"
    assert "G14_info" in logic.questions  # every group with a visible question
    assert not any(c.question_id == "G10_info" for c in surveyir.response_columns(logic))


@pytest.mark.parametrize(
    "qid, relevance",
    [
        ("t1", '((c1.NAOK == "A1")) or ((333333X20X902SQ001.NAOK == "Y"))'),
        ("t2", "((is_empty(c1.NAOK)))"),
        (
            "t3",
            '(((!is_empty(c3.NAOK) && (c3.NAOK >= 5))) and (regexMatch("/^[a-z]+$/",c4.NAOK)))',
        ),
        ("t4", '((TOKEN:ATTRIBUTE_1 == "gold"))'),
    ],
)
def test_conditions_convert_as_limesurvey_does(qid, relevance):
    s = load_lss(FIX / "conditions.lss", strict=True)
    q = s.questions[qid]
    assert q.extras["limesurvey"]["relevance_from_conditions"] == relevance
    assert q.display_logic is not None


def test_conditions_lower_to_comparisons():
    s = load_lss(FIX / "conditions.lss", strict=True)
    assert describe_condition(s.questions["t1"].display_logic, s) == "c1 = A1 OR c2 is selected"
    assert describe_condition(s.questions["t4"].display_logic, s) == (
        "embedded data `TOKEN:ATTRIBUTE_1` = gold"
    )


@pytest.mark.parametrize("path", sorted(CITRIC.glob("*.ls?")), ids=lambda p: p.name)
def test_citric_examples_load_strictly(path):
    s = LimeSurveyLoader(strict=True).load(path)
    assert s.questions and not s.diagnostics


def test_group_and_question_exports():
    group = load_lss(CITRIC / "group.lsg")
    assert group.extras["doctype"] == "Group" and len(group.blocks) == 1
    question = load_lss(CITRIC / "free_text.lsq")
    assert question.extras["doctype"] == "Question" and len(question.questions) == 1


def test_tab_separated_export(tmp_path):
    rows = [
        [
            "class",
            "type/scale",
            "name",
            "relevance",
            "text",
            "help",
            "language",
            "validation",
            "mandatory",
            "other",
            "default",
            "same_default",
        ],
        ["S", "", "language", "", "en", "", "", "", "", "", "", ""],
        ["S", "", "format", "", "G", "", "", "", "", "", "", ""],
        ["SL", "", "surveyls_title", "", "TSV survey", "", "en", "", "", "", "", ""],
        ["G", "", "Group one", "", "", "", "en", "", "", "", "", ""],
        ["Q", "L", "q1", "1", "Pick one", "", "en", "", "Y", "N", "", "0"],
        ["A", "0", "A1", "", "Yes", "", "en", "", "", "", "", ""],
        ["A", "0", "A2", "", "No", "", "en", "", "", "", "", ""],
        ["Q", "S", "q2", 'q1 == "A1"', "Why?", "", "en", "", "N", "N", "", "0"],
    ]
    path = tmp_path / "survey.txt"
    path.write_text("\n".join("\t".join(r) for r in rows) + "\n", encoding="utf-8")
    s = surveyir.load(path, format="limesurvey")
    assert s.name == "TSV survey"
    q1 = s.questions["q1"]
    assert isinstance(q1, ChoiceQuestion) and [c.id for c in q1.choices] == ["A1", "A2"]
    # q1 is on q2's page, so q2 appears as q1 is answered
    assert describe_condition(s.questions["q2"].in_page_display_logic, s) == "q1 = A1"


def test_not_limesurvey(tmp_path):
    bad = tmp_path / "x.lss"
    bad.write_text("<html></html>")
    with pytest.raises(LoadError):
        load_lss(bad)


def test_strict_rejects_unexpected_warnings(tmp_path):
    src = (FIX / "logic.lss").read_text()
    broken = src.replace("pets_SQ001 == &quot;Y&quot;", "nosuch == 1").replace(
        'pets_SQ001 == "Y"', "nosuch == 1"
    )
    path = tmp_path / "broken.lss"
    path.write_text(broken)
    s = load_lss(path)  # an unknown variable is a defect of the file: tolerated
    assert any(d.code == "limesurvey-unknown-variable" for d in s.diagnostics)
    load_lss(path, strict=True)


def test_json_round_trip_and_exports(logic, types):
    for s in (logic, types):
        assert surveyir.Survey.from_json(s.to_json()) == s
        for fmt in ("markdown", "codebook", "json"):
            assert surveyir.export(s, fmt)
