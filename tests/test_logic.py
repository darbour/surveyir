from __future__ import annotations

from surveyir.describe import describe_condition
from surveyir.loaders.qualtrics._util import Diagnostics
from surveyir.loaders.qualtrics.logic import parse_logic, parse_question_locator
from surveyir.model import And, Comparison, Or


def expr(left: str, op: str = "Selected", conj: str | None = None, **kw) -> dict:
    e = {"LogicType": "Question", "LeftOperand": left, "Operator": op, "Type": "Expression"}
    if conj:
        e["Conjuction"] = conj
    e.update(kw)
    return e


def logic(*sets: dict) -> dict:
    out = {str(i): s for i, s in enumerate(sets)}
    out["Type"] = "BooleanExpression"
    return out


def leaves(cond) -> list[str]:
    if isinstance(cond, Comparison):
        return [cond.left.choice_id]
    return [x for t in cond.terms for x in leaves(t)]


def test_single_expression():
    cond = parse_logic(
        logic({"0": expr("q://QID1/SelectableChoice/2"), "Type": "If"}), Diagnostics(), "x"
    )
    assert isinstance(cond, Comparison)
    assert cond.operator == "selected"
    assert (cond.left.question_id, cond.left.selector, cond.left.choice_id) == (
        "QID1",
        "SelectableChoice",
        "2",
    )


def test_and_binds_tighter_than_or():
    # A or B and C  ==  A or (B and C)
    s = {
        "0": expr("q://QID1/SelectableChoice/1"),
        "1": expr("q://QID1/SelectableChoice/2", conj="Or"),
        "2": expr("q://QID1/SelectableChoice/3", conj="And"),
        "Type": "If",
    }
    cond = parse_logic(logic(s), Diagnostics(), "x")
    assert isinstance(cond, Or)
    assert isinstance(cond.terms[0], Comparison)
    assert isinstance(cond.terms[1], And)
    assert leaves(cond) == ["1", "2", "3"]


def test_logic_sets_combine_with_and_if_and_or_if():
    s1 = {"0": expr("q://QID1/SelectableChoice/1"), "Type": "If"}
    s2 = {"0": expr("q://QID1/SelectableChoice/2"), "Type": "AndIf"}
    s3 = {"0": expr("q://QID1/SelectableChoice/3"), "Type": "OrIf"}
    cond = parse_logic(logic(s1, s2, s3), Diagnostics(), "x")
    assert isinstance(cond, Or)
    assert isinstance(cond.terms[0], And)
    assert leaves(cond) == ["1", "2", "3"]


def test_sets_are_ordered_numerically_not_lexically():
    sets = [
        {"0": expr(f"q://QID1/SelectableChoice/{i}"), "Type": "If" if i == 0 else "OrIf"}
        for i in range(12)
    ]
    cond = parse_logic(logic(*sets), Diagnostics(), "x")
    assert leaves(cond) == [str(i) for i in range(12)]


def test_embedded_field_and_hidden_expression():
    s = {
        "0": {
            "LogicType": "EmbeddedField",
            "LeftOperand": "Condition",
            "Operator": "EqualTo",
            "RightOperand": "2",
            "Type": "Expression",
            "_HiddenExpression": False,
        },
        "1": {
            "LogicType": "EmbeddedField",
            "LeftOperand": "Other",
            "Operator": "EqualTo",
            "RightOperand": "1",
            "Type": "Expression",
            "_HiddenExpression": True,
            "Conjuction": "Or",
        },
        "Type": "If",
    }
    diags = Diagnostics()
    cond = parse_logic(logic(s), diags, "x")
    assert isinstance(cond, Comparison)
    assert (cond.left.kind, cond.left.name, cond.right) == ("embedded_data", "Condition", "2")
    assert [d.code for d in diags.items] == ["hidden-expression"]


def test_unknown_operator_is_reported_not_dropped():
    diags = Diagnostics()
    cond = parse_logic(
        logic({"0": expr("q://QID1/SelectableChoice/1", op="Frobnicates"), "Type": "If"}),
        diags,
        "x",
    )
    assert cond.operator == "other" and cond.source_operator == "Frobnicates"
    assert diags.items[0].code == "unknown-operator"


def test_loop_prefixed_locator():
    op = parse_question_locator("q://3_QID9/ChoiceTextEntryValue")
    assert (op.loop_iteration, op.question_id, op.selector, op.choice_id) == (
        3,
        "QID9",
        "ChoiceTextEntryValue",
        None,
    )


def test_matrix_locator():
    op = parse_question_locator("q://QID9/SelectableAnswer/2/5")
    assert (op.selector, op.choice_id, op.answer_id) == ("SelectableAnswer", "2", "5")


def test_describe():
    s = {
        "0": expr("q://QID1/SelectableChoice/1"),
        "1": {
            "LogicType": "EmbeddedField",
            "LeftOperand": "arm",
            "Operator": "EqualTo",
            "RightOperand": "2",
            "Type": "Expression",
            "Conjuction": "And",
        },
        "Type": "If",
    }
    text = describe_condition(parse_logic(logic(s), Diagnostics(), "x"))
    assert text == "QID1: choice 1 is selected AND embedded data `arm` = 2"


def test_empty_logic_is_none():
    assert parse_logic({}, Diagnostics(), "x") is None
    assert parse_logic(False, Diagnostics(), "x") is None
