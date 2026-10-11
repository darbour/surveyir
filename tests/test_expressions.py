"""ExpressionScript: parsing, lowering to comparisons, and LimeSurvey's evaluation rules."""

from __future__ import annotations

import pytest

from surveyir.loaders.limesurvey import expressions as ex
from surveyir.model import (
    And,
    Binary,
    Call,
    ChoiceQuestion,
    Comparison,
    Const,
    Expression,
    Not,
    Or,
    Origin,
    SourceInfo,
    Survey,
    TextEntryQuestion,
    Unary,
    Var,
)
from surveyir.model.question import Choice
from surveyir.model.text import Text
from surveyir.runtime.expressions import evaluate_boolean, evaluate_value, loose_eq, strval
from surveyir.runtime.state import Answer, RespondentState

# --------------------------------------------------------------------------- parsing


def test_tokens_follow_expression_manager():
    toks = [(t.text, t.type) for t in ex.tokenize('Q1.NAOK eq "a\\"b" && !is_empty(Q2)')]
    assert toks == [
        ("Q1.NAOK", "WORD"),
        ("eq", "COMPARE"),
        ('a"b', "DQ_STRING"),
        ("&&", "AND_OR"),
        ("!", "NOT"),
        ("is_empty", "WORD"),
        ("(", "LP"),
        ("Q2", "WORD"),
        (")", "RP"),
    ]
    assert ex.tokenize("123X4X56SQ001.NAOK")[0].type == "SGQA"


def test_precedence_and_or_comparison_arithmetic():
    tree = ex.parse("a + 2 * b > 3 or c == 1 and d != 2")
    assert isinstance(tree, Binary) and tree.op == "or"
    left, right = tree.left, tree.right
    assert isinstance(left, Binary) and left.op == ">"
    assert isinstance(left.left, Binary) and left.left.op == "+"
    assert isinstance(left.left.right, Binary) and left.left.right.op == "*"
    assert isinstance(right, Binary) and right.op == "and"


def test_unary_binds_to_a_primary_only():
    tree = ex.parse("!is_empty(x)")
    assert isinstance(tree, Unary) and isinstance(tree.arg, Call)
    with pytest.raises(ex.ExpressionError):
        ex.parse("!!x")  # EM: a unary operator takes a primary, not another unary


@pytest.mark.parametrize("bad", ["", "(a", "a b", "f(a,", "a @ b", "x ++"])
def test_syntax_errors(bad):
    with pytest.raises(ex.ExpressionError):
        ex.parse(bad)


def test_sequences_and_assignments():
    seq = ex.parse("a, b, 3")
    assert seq.node == "sequence" and len(seq.items) == 3  # type: ignore[union-attr]
    assign = ex.parse("total = a + 1")
    assert assign.node == "assign"


def test_html_entities_in_markup_are_decoded():
    tree = ex.parse("Q1 &gt; 3")
    assert isinstance(tree, Binary) and tree.op == ">"


# --------------------------------------------------------------------------- text


@pytest.mark.parametrize(
    "text, parts",
    [
        ("Hi {Q1}!", [("Hi ", False), ("{Q1}", True), ("!", False)]),
        ("{ not an expression }", [("{ not an expression }", False)]),
        (
            "css {color:red} and { x}",
            [("css ", False), ("{color:red}", True), (" and { x}", False)],
        ),
        ("{if(Q1 == '}', 'a', 'b')}", [("{if(Q1 == '}', 'a', 'b')}", True)]),
        ("{a{b}c}", [("{a{b}c}", True)]),
        ("no braces", [("no braces", False)]),
    ],
)
def test_text_splits_like_limesurvey(text, parts):
    assert ex.split_text(text) == parts


def test_text_pipes_only_for_expressions_that_parse():
    names = {"Q1": ex.Target("Q1")}
    t = ex.make_text("You said {Q1.shown}. Style: {color:red}", ex.Scope(names))
    assert [p.raw for p in t.pipes] == ["{Q1.shown}"]
    assert t.syntax == "expression_script"
    rendered = t.render(lambda p: "yes" if p.raw == "{Q1.shown}" else None)
    assert rendered == "You said yes. Style: {color:red}"


# --------------------------------------------------------------------------- resolution


def _scope() -> ex.Scope:
    names = {
        "Q1": ex.Target("Q1"),
        "Q2_SQ001": ex.Target("Q2", "SQ001"),
        "Q2_SQ002": ex.Target("Q2", "SQ002"),
        "Q2_other": ex.Target("Q2", "other"),
        "111X2X3": ex.Target("Q1"),
    }
    variables = {
        "Q2": [("Q2_SQ001", "SQ001"), ("Q2_SQ002", "SQ002"), ("Q2_other", "other")],
    }
    return ex.Scope(names, variables, owner="Q2", statics={"QID": "3"})


def test_names_resolve_to_questions_and_parts():
    tree = ex.compile_expression("Q2_SQ001.NAOK == 'Y' and 111X2X3 == 1 and TOKEN:AGE", _scope())
    vars_ = [n for n in ex.iter_nodes(tree) if isinstance(n, Var)]
    assert [(v.kind, v.question_id, v.part, v.attribute, v.field) for v in vars_] == [
        ("question", "Q2", "SQ001", "NAOK", None),
        ("question", "Q1", None, None, None),
        ("participant", None, None, None, "AGE"),
    ]


def test_self_and_that_expand_like_expand_this_var():
    scope = _scope()
    assert ex.expand_this("count(self.NAOK)", scope) == (
        "count(Q2_SQ001.NAOK,Q2_SQ002.NAOK,Q2_other.NAOK)"
    )
    assert ex.expand_this("sum(that.Q2.sq_SQ00)", scope) == "sum(Q2_SQ001,Q2_SQ002)"
    assert ex.expand_this("sum(that.Q2.nosq_other)", scope) == "sum(Q2_SQ001,Q2_SQ002)"
    tree = ex.compile_expression("QID == 3", scope)
    assert isinstance(tree, Binary) and tree.left == Const(value="3", quoted=True)


# --------------------------------------------------------------------------- lowering

SHAPES = {
    "Q1": "choice/single",
    "Q2": "choice/multiple",
    "Q3": "matrix/single",
    "Q4": "text_entry/form",
}


def lowered(source: str) -> object:
    names = {
        "Q1": ex.Target("Q1"),
        "Q2_SQ001": ex.Target("Q2", "SQ001"),
        "Q3_R1": ex.Target("Q3", "R1"),
        "Q4_F1": ex.Target("Q4", "F1"),
        "Q1_other": ex.Target("Q1", "other"),
    }
    tree = ex.compile_expression(source, ex.Scope(names))
    return ex.lower(tree, source, SHAPES)


def test_equality_with_a_constant_is_a_comparison():
    c = lowered('Q1 == "A1"')
    assert isinstance(c, Comparison) and (c.operator, c.right) == ("equal", "A1")


def test_inequality_requires_relevance():
    c = lowered('Q1 != "A1"')
    assert isinstance(c, And)
    guard, test = c.terms
    assert isinstance(guard, Comparison) and guard.operator == "relevant"
    assert isinstance(test, Comparison) and test.operator == "not_equal"


def test_naok_needs_no_guard():
    c = lowered('Q1.NAOK != "A1"')
    assert isinstance(c, Comparison) and c.operator == "not_equal"


def test_parts_choose_selectors():
    c = lowered('Q2_SQ001 == "Y"')
    assert isinstance(c, Comparison) and (c.operator, c.left.choice_id) == ("selected", "SQ001")
    c = lowered('Q3_R1 == "A2"')
    assert isinstance(c, Comparison) and (c.operator, c.left.answer_id) == ("selected", "A2")
    c = lowered('Q4_F1.NAOK == "x"')
    assert isinstance(c, Comparison) and c.left.selector == "ChoiceTextEntryValue"
    c = lowered("!is_empty(Q1_other.NAOK)")
    assert isinstance(c, Comparison) and c.left.choice_id == "-oth-"


def test_empty_array_row_keeps_its_guard():
    c = lowered("is_empty(Q3_R1)")
    assert isinstance(c, And)
    guard, test = c.terms
    assert isinstance(guard, Comparison) and guard.operator == "relevant"
    assert isinstance(test, Comparison) and test.left.selector == "SelectedAnswerCount"


def test_undefined_names_keep_the_expression_whole():
    for source in ('Q1 == "A1" or nosuch == 1', 'Q1 == "A1" or myplugin(Q1)'):
        c = lowered(source)
        assert isinstance(c, Expression) and c.source == source


def test_is_empty_and_not():
    c = lowered("is_empty(Q1.NAOK)")
    assert isinstance(c, Comparison) and (c.operator, c.right) == ("equal", "")
    c = lowered('!(Q1.NAOK == "A1")')
    assert isinstance(c, Not)


def test_ordering_comparisons_stay_expressions():
    c = lowered('Q1.NAOK == "A1" or Q1.NAOK > 3')
    assert isinstance(c, Or)
    assert isinstance(c.terms[0], Comparison) and isinstance(c.terms[1], Expression)
    assert c.terms[1].source == "Q1.NAOK > 3"


def test_unlowerable_whole_expression_keeps_its_source():
    c = lowered("sum(Q1, 2) > 3")
    assert isinstance(c, Expression) and c.source == "sum(Q1, 2) > 3" and c.tree is not None


# --------------------------------------------------------------------------- evaluation


def survey() -> Survey:
    origin = Origin(format="limesurvey", type="L")
    return Survey(
        id="1",
        name="t",
        source=SourceInfo(format="limesurvey.lss", loader="test"),
        questions={
            "Q1": ChoiceQuestion(
                id="Q1",
                export_tag="Q1",
                origin=origin,
                text=Text(plain="Pick"),
                multiple=False,
                choices=[
                    Choice(id="A1", text=Text(plain="One")),
                    Choice(id="A2", text=Text(plain="Two")),
                ],
            ),
            "N1": TextEntryQuestion(
                id="N1",
                export_tag="N1",
                origin=Origin(format="limesurvey", type="N"),
                text=Text(plain="Num"),
                mode="single_line",
            ),
            "S1": TextEntryQuestion(
                id="S1",
                export_tag="S1",
                origin=Origin(format="limesurvey", type="S"),
                text=Text(plain="Text"),
                mode="single_line",
            ),
        },
    )


def run(source: str, answers: dict[str, object], irrelevant: set[str] = frozenset()) -> bool:
    names = {q: ex.Target(q) for q in ("Q1", "N1", "S1")}
    tree = ex.compile_expression(source, ex.Scope(names))
    state = RespondentState(
        answers={(k, None): Answer(value=v) for k, v in answers.items()},
        irrelevant={(k, None) for k in irrelevant},
    )
    expr = Expression(language="expression_script", source=source, tree=tree)
    return evaluate_boolean(expr, state, survey())


@pytest.mark.parametrize(
    "source, answers, expected",
    [
        ('Q1 == "A1"', {"Q1": "A1"}, True),
        ("Q1 == 'A2'", {"Q1": "A1"}, False),
        ("N1 > 3", {"N1": "10"}, True),
        ("N1 > 30", {"N1": "4"}, False),  # numbers, not strings: "4" > "30" as text
        ('S1 > "30"', {"S1": "4"}, True),  # a quoted literal forces a string comparison
        ("N1 < 3", {}, False),  # empty vs number: a type mismatch is false
        ("N1 == 0", {}, False),  # PHP 8: "" == "0" is false
        ("N1 == 5", {"N1": "5.0"}, True),  # numeric strings compare as numbers
        ("is_empty(N1)", {}, True),
        ("N1 + 1 == 6", {"N1": "5"}, True),
        ('S1 + "x" == "ax"', {"S1": "a"}, True),  # + concatenates non-numbers
        ("count(Q1, N1, S1) == 2", {"Q1": "A1", "S1": "x"}, True),
        ('if(N1 > 1, "big", "small") == "big"', {"N1": "2"}, True),
        ('regexMatch("/^[a-z]+$/", S1)', {"S1": "abc"}, True),
        ('regexMatch("/^[a-z]+$/i", S1)', {"S1": "ABC"}, True),
        ("sum(N1, 2) == 7", {"N1": "5"}, True),
        ('Q1.shown == "Two"', {"Q1": "A2"}, True),
        ("strlen(S1) == 3", {"S1": "abc"}, True),
        ("!is_empty(S1) and S1 == 'x'", {"S1": "x"}, True),
        ('in_array("a", S1)', {"S1": "a"}, False),  # not a LimeSurvey function: fails
    ],
)
def test_evaluation_follows_limesurvey(source, answers, expected):
    assert run(source, answers) is expected


def test_an_irrelevant_variable_fails_the_whole_expression():
    assert run('Q1 == "A1" or N1 != 5', {"Q1": "A1"}, irrelevant={"N1"}) is False
    assert run('Q1 == "A1" or N1.NAOK != 5', {"Q1": "A1"}, irrelevant={"N1"}) is True
    assert run("N1.relevanceStatus == 0", {}, irrelevant={"N1"}) is True


def test_irrelevant_array_row_is_not_empty_but_failed():
    from surveyir.model import MatrixQuestion, ScalePoint

    s = survey()
    s.questions["M1"] = MatrixQuestion(
        id="M1",
        export_tag="M1",
        origin=Origin(format="limesurvey", type="F"),
        text=Text(plain="Grid"),
        mode="single",
        rows=[Choice(id="R1", text=Text(plain="Row"))],
        columns=[ScalePoint(id="A1", text=Text(plain="Yes"))],
    )
    names = {"M1_R1": ex.Target("M1", "R1")}
    tree = ex.compile_expression("is_empty(M1_R1)", ex.Scope(names))
    cond = ex.lower(tree, "is_empty(M1_R1)", {"M1": "matrix/single"})
    from surveyir.runtime.logic import evaluate

    hidden = RespondentState(irrelevant={("M1", None)})
    assert evaluate(cond, hidden, s) is False  # LimeSurvey: the expression fails
    assert evaluate(cond, RespondentState(), s) is True  # relevant and unanswered


def test_unknown_functions_and_variables_are_recorded():
    names = {"Q1": ex.Target("Q1")}
    tree = ex.compile_expression("myplugin(Q1) or nosuch == 1", ex.Scope(names))
    state = RespondentState()
    expr = Expression(language="expression_script", source="x", tree=tree, location="Q9")
    assert evaluate_boolean(expr, state, survey()) is False
    assert [a.code for a in state.approximations] == ["expression.unknown_function"]


def test_values_render_as_text():
    names = {"N1": ex.Target("N1")}
    state = RespondentState(answers={("N1", None): Answer(value="4")})
    tree = ex.compile_expression("N1 / 8", ex.Scope(names))
    assert evaluate_value(tree, state, survey(), "x") == "0.5"
    tree = ex.compile_expression("N1 * 2", ex.Scope(names))
    assert evaluate_value(tree, state, survey(), "x") == "8"
    tree = ex.compile_expression("N1 > 3", ex.Scope(names))
    assert evaluate_value(tree, state, survey(), "x") == "1"


@pytest.mark.parametrize(
    "a, b, eq",
    [
        ("1", "01", True),
        ("abc", "ABC", False),
        ("", "0", False),
        ("1e3", "1000", True),
        (0, "a", False),
        (None, "", True),
        (True, "x", True),
    ],
)
def test_php_loose_equality(a, b, eq):
    assert loose_eq(a, b) is eq


def test_php_float_strings():
    assert [strval(x) for x in (3.0, 0.5, 1e25, -2.0)] == ["3", "0.5", "1.0E+25", "-2"]
