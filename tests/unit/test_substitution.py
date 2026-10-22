"""Relevance substitution in action lines: {expr} is evaluated, {{ is a literal {."""

import pytest

from bigfix_actionscript_runner.substitution import SubstitutionError, find_expressions, substitute


def fake(answers):
    """An evaluator returning canned answer lists per expression."""

    def evaluate(expr):
        if expr not in answers:
            raise SubstitutionError(f"unexpected expression {expr!r}")
        return answers[expr]

    return evaluate


def test_plain_text_is_unchanged():
    assert substitute("copy a b", fake({})) == "copy a b"


def test_expression_is_replaced_by_its_answer():
    assert (
        substitute("echo {name of operating system}", fake({"name of operating system": ["macOS"]}))
        == "echo macOS"
    )


def test_several_expressions_on_one_line():
    ev = fake({"a": ["1"], "b": ["2"]})
    assert substitute("{a}-{b}", ev) == "1-2"


def test_double_left_brace_is_a_literal_brace():
    # BigFix: "{{" is an escaped "{"; a lone "}" needs no escape
    assert substitute("if [ x ]; then {{ echo; }", fake({})) == "if [ x ]; then { echo; }"


def test_expression_ends_at_first_right_brace_like_bigfix():
    ev = fake({'name of it whose(it = "': ["x"]})
    assert substitute('{name of it whose(it = "}")', ev) == 'x")'


def test_whitespace_inside_braces_is_trimmed():
    assert substitute("{ a }", fake({"a": ["1"]})) == "1"


def test_boolean_answers_are_substituted_as_bigfix_prints_them():
    assert substitute("{b}", fake({"b": ["True"]})) == "True"


def test_no_answer_is_an_error():
    with pytest.raises(SubstitutionError, match="no value"):
        substitute("{a}", fake({"a": []}))


def test_plural_answer_is_an_error():
    with pytest.raises(SubstitutionError, match="2 values"):
        substitute("{a}", fake({"a": ["1", "2"]}))


def test_unterminated_expression_is_an_error():
    with pytest.raises(SubstitutionError, match="unterminated"):
        substitute("copy {a b", fake({}))


def test_evaluator_errors_propagate_with_the_expression():
    def boom(expr):
        raise SubstitutionError('The operator "x" is not defined.')

    with pytest.raises(SubstitutionError, match="not defined"):
        substitute("{x}", boom)


# --- finding expressions without evaluating them (for the baseline pass) -----


def test_find_expressions_follows_the_same_rules():
    assert find_expressions('copy "{a}" "{ b }/x"') == ["a", "b"]
    assert find_expressions("if [ 1 ]; then {{ echo; } {c}") == ["c"]
    assert find_expressions('{name of it whose(it = "}")') == ['name of it whose(it = "']
    assert find_expressions("no braces") == []


def test_find_expressions_ignores_an_unterminated_one():
    assert find_expressions("{a} {b") == ["a"]
