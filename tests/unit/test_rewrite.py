"""Action-only inspectors are rewritten before qna sees the expression.

qna has no action context, so `parameter "x"`, `id of action`, `exit code of
action` and the client/site folders are replaced with literals pointing into
the sandbox.
"""

from bigfix_actionscript_runner.rewrite import ActionContext, relevance_string, rewrite


def ctx(**kw):
    defaults = {
        "parameters": {"temp": "/sb/__BESData/__Global/yara_php_scan", "q": 'say "hi" 100%'},
        "action_id": 12345,
        "exit_code": 0,
        "site_folder": "/sb/site",
        "data_folder": "/sb/__BESData",
    }
    defaults.update(kw)
    return ActionContext(**defaults)


def test_relevance_string_escapes_percent_and_quote():
    assert relevance_string('a "b" 5%') == '"a %22b%22 5%25"'


def test_parameter_becomes_a_string_literal():
    assert rewrite('parameter "temp"', ctx()) == '"/sb/__BESData/__Global/yara_php_scan"'


def test_parameter_of_action_too():
    assert rewrite('parameter "temp" of action', ctx()) == '"/sb/__BESData/__Global/yara_php_scan"'


def test_parameter_inside_a_larger_expression():
    out = rewrite('folder (parameter "temp" & "/results")', ctx())
    assert out == 'folder ("/sb/__BESData/__Global/yara_php_scan" & "/results")'


def test_parameter_value_with_quotes_and_percent_is_escaped():
    assert rewrite('parameter "q"', ctx()) == '"say %22hi%22 100%25"'


def test_unknown_parameter_is_left_for_qna_to_report():
    assert rewrite('parameter "nope"', ctx()) == 'parameter "nope"'


def test_id_and_exit_code_of_action():
    assert rewrite("id of action", ctx()) == "12345"
    assert rewrite("exit code of action = 0", ctx(exit_code=3)) == "3 = 0"


def test_site_folders():
    assert rewrite('pathname of folder "__Download" of client folder of current site', ctx()) == (
        'pathname of folder "__Download" of folder "/sb/site"'
    )
    assert rewrite("client folders of current site", ctx()) == 'folders "/sb/site"'


def test_data_folders_of_client():
    assert (
        rewrite('pathname of folders "Logs" of folders "__Global" of data folders of client', ctx())
        == 'pathname of folders "Logs" of folders "__Global" of folders "/sb/__BESData"'
    )
    assert rewrite("data folder of client", ctx()) == 'folder "/sb/__BESData"'


def test_text_inside_string_literals_is_not_rewritten():
    assert rewrite('"id of action" & name of operating system', ctx()) == (
        '"id of action" & name of operating system'
    )


def test_folders_can_be_left_alone_for_remote_evaluation():
    expr = 'pathname of folders "Logs" of folders "__Global" of data folders of client & parameter "temp"'
    out = rewrite(expr, ctx(), folders=False)
    assert out == (
        'pathname of folders "Logs" of folders "__Global" of data folders of client'
        ' & "/sb/__BESData/__Global/yara_php_scan"'
    )


def test_case_insensitive_like_relevance():
    assert rewrite("ID OF ACTION", ctx()) == "12345"


def test_active_action_and_plural_forms():
    assert rewrite("exists (exit codes of active action) whose(it = 0)", ctx(exit_code=0)) == (
        "exists (0) whose(it = 0)"
    )
    assert rewrite("exit code of active action", ctx(exit_code=2)) == "2"
    assert rewrite("id of active action", ctx()) == "12345"
    assert rewrite('parameter "p" of active action', ctx(parameters={"p": "v"})) == '"v"'


def test_an_expression_the_analyzer_cannot_parse_still_gets_the_regex_rewrite():
    # an unfinished expression: qna will report the error, but with the value in it
    assert rewrite('parameter "temp" &', ctx()) == '"/sb/__BESData/__Global/yara_php_scan" &'


def test_unresolved_client_context_is_logged(caplog):
    with caplog.at_level("DEBUG", logger="bigfix_actionscript_runner.rewrite"):
        rewrite('parameter "nope"', ctx())
    assert 'parameter "nope"' in caplog.text
    assert "not set" in caplog.text
