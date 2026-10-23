"""Client-context inspectors are simulated on the parse tree, not with regexes.

qna has no running client behind it, so an action's parameters, its id and
exit code, the current site's folder and the download folder all fail there
with "No inspector context". `simulate` replaces each with the literal a
running client would have produced, from a `ClientContext`, and reports what
it couldn't.
"""

from pathlib import Path

import pytest

from bigfix_actionscript_runner.client_context import (
    NO_CONTEXT_INSPECTORS,
    NO_OBJECT_GLOBALS,
    ClientContext,
    simulate,
)

DUMPS = Path(__file__).resolve().parent.parent / "data" / "client_context"


def ctx(**kw):
    defaults = {
        "parameters": {"temp": "/sb/tmp", "q": 'say "hi" 100%', "archive": "yr.tar.gz"},
        "action_id": 12345,
        "exit_code": 0,
        "site_folder": "/sb/site",
        "data_folder": "/sb/__BESData",
        "download_folder": "/sb/site/__Download",
    }
    defaults.update(kw)
    return ClientContext(**defaults)


def text(expr, context=None):
    return simulate(expr, context or ctx()).text


# -- parameters ---------------------------------------------------------------


@pytest.mark.parametrize(
    "expr",
    ['parameter "temp"', 'parameter "temp" of action', 'parameter "temp" of active action'],
)
def test_a_set_parameter_is_its_string(expr):
    assert text(expr) == '"/sb/tmp"'


def test_a_parameter_inside_a_larger_expression():
    assert text('folder (parameter "temp" & "/results")') == 'folder ("/sb/tmp" & "/results")'


def test_a_parameter_value_is_percent_escaped():
    assert text('parameter "q"') == '"say %22hi%22 100%25"'


def test_an_unset_parameter_is_left_as_written_and_reported():
    result = simulate('parameter "nope" & "x"', ctx())
    assert result.text == 'parameter "nope" & "x"'
    assert [u.text for u in result.unresolved] == ['parameter "nope"']
    assert "not set" in result.unresolved[0].reason


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ('exists parameter "temp"', "true"),
        ('exists parameter "nope"', "false"),
        ('not exists parameter "temp"', "false"),
        ('not exists parameter "nope"', "true"),
        ('exists parameter "nope" of action', "false"),
        ('not exists parameter "nope" of active action', "true"),
    ],
)
def test_exists_parameter_is_whether_it_is_set(expr, expected):
    result = simulate(expr, ctx())
    assert result.text == expected
    assert result.unresolved == ()


def test_exists_parameter_inside_a_condition():
    assert text('if exists parameter "nope" then parameter "nope" else "x"') == (
        'if false then parameter "nope" else "x"'
    )


# -- the action ---------------------------------------------------------------


def test_id_and_exit_code_of_the_action():
    assert text("id of action") == "12345"
    assert text("id of active action") == "12345"
    assert text("exit code of action = 0", ctx(exit_code=3)) == "3 = 0"
    assert text("exists (exit codes of active action) whose (it = 0)") == (
        "exists (0) whose (it = 0)"
    )


def test_a_negative_exit_code_is_parenthesized():
    assert text("exit code of action", ctx(exit_code=-1)) == "(-1)"


def test_unknown_action_values_are_left_and_reported():
    result = simulate("id of action", ctx(action_id=None))
    assert result.text == "id of action"
    assert len(result.unresolved) == 1


# -- folders ------------------------------------------------------------------


def test_client_folder_of_current_site():
    assert text('pathname of folder "__Download" of client folder of current site') == (
        'pathname of folder "__Download" of folder "/sb/site"'
    )
    assert text("client folders of current site") == 'folders "/sb/site"'


def test_data_folder_of_client():
    assert text("data folder of client") == 'folder "/sb/__BESData"'
    assert text('pathname of folders "Logs" of data folders of client') == (
        'pathname of folders "Logs" of folders "/sb/__BESData"'
    )


def test_unknown_folders_keep_the_real_inspectors():
    # a remote target's real client folders: left for the client to answer
    unknown = ctx(site_folder=None, data_folder=None)
    assert text("client folder of current site", unknown) == "client folder of current site"
    assert text("data folder of client", unknown) == "data folder of client"


def test_download_folder_and_files():
    assert text("pathname of download folder") == 'pathname of folder "/sb/site/__Download"'
    assert text('download file "a.zip"') == 'file "/sb/site/__Download/a.zip"'
    assert text('pathname of download file (parameter "archive")') == (
        'pathname of file "/sb/site/__Download/yr.tar.gz"'
    )


def test_download_file_of_an_unknown_name_is_reported():
    result = simulate('download file (parameter "nope")', ctx())
    assert result.text == 'download file (parameter "nope")'
    assert {u.text for u in result.unresolved} >= {'download file (parameter "nope")'}


def test_windows_download_folder_uses_its_separator():
    windows = ctx(download_folder="C:\\sb\\site\\__Download")
    assert text('download file "a.zip"', windows) == 'file "C:\\sb\\site\\__Download\\a.zip"'


# -- what is not touched ------------------------------------------------------


def test_string_literals_are_not_rewritten():
    assert text('"id of action" & name of operating system') == (
        '"id of action" & name of operating system'
    )


def test_matching_is_case_insensitive():
    assert text("ID OF ACTION") == "12345"
    assert text('EXISTS PARAMETER "nope"') == "false"


def test_a_parse_error_leaves_the_text_and_says_so():
    result = simulate('exists (parameter "x"', ctx())
    assert result.text == 'exists (parameter "x"'
    assert result.parse_error


def test_an_unsimulated_client_context_inspector_is_reported():
    result = simulate("pending restart", ctx())
    assert result.text == "pending restart"
    assert [u.text for u in result.unresolved] == ["pending restart"]
    assert "running BigFix client" in result.unresolved[0].reason


# -- the evidence -------------------------------------------------------------


def _dump_rows():
    for dump in sorted(DUMPS.glob("qna_*.tsv")):
        for line in dump.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                signature, _probe, outcome, detail = (line.split("\t") + [""])[:4]
                yield signature, outcome, detail


def _dump_outcomes():
    outcomes: dict[str, set[str]] = {}
    for signature, outcome, _detail in _dump_rows():
        outcomes.setdefault(signature, set()).add(outcome)
    return outcomes


def test_the_table_is_exactly_what_qna_reported_as_needing_a_client():
    outcomes = _dump_outcomes()
    assert outcomes, f"no qna dumps in {DUMPS}"
    reported = {sig for sig, seen in outcomes.items() if "no-context" in seen}
    assert set(NO_CONTEXT_INSPECTORS) == reported


# -- from review ----------------------------------------------------------------


def test_a_very_long_expression_does_not_overflow_the_stack():
    # the analyzer parses long flat chains; the walk over them must not recurse per level
    result = simulate(" or ".join(['parameter "temp" = "x"'] * 3000), ctx())
    assert "parameter" not in result.text
    assert result.text.startswith('"/sb/tmp" = "x" or ')


def test_a_grouped_property_is_a_global():
    # `(prop) of obj` evaluates prop against the world, so it is the global parameter
    assert text('(parameter "temp") of file "x"') == '("/sb/tmp") of file "x"'


def test_a_numbered_action_is_not_the_running_one():
    for expr in ("id of action 3", "exit code of action 3", 'parameter "temp" of action 3'):
        result = simulate(expr, ctx())
        assert result.text == expr
        assert result.unresolved, expr


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ('exists parameter "nope" whose (it = "y")', "false"),
        ('not exists parameter "nope" whose (it = "y")', "true"),
        ('exists parameter "nope" as lowercase', "false"),
        ('exists parameter (parameter "archive")', "false"),
        ('exists parameter "temp" whose (it = "y")', 'exists "/sb/tmp" whose (it = "y")'),
        ('exists parameters "nope"', "false"),
        ('parameters "temp"', '"/sb/tmp"'),
    ],
)
def test_exists_over_a_filtered_or_cast_parameter(expr, expected):
    assert text(expr) == expected


def test_a_parameter_name_is_matched_as_written_first():
    # the executor stores names as the script wrote them, escapes and all
    assert text('parameter "x%20y"', ctx(parameters={"x%20y": "RAW"})) == '"RAW"'
    assert text('parameter "x%20y"', ctx(parameters={"x y": "DECODED"})) == '"DECODED"'


def test_the_current_site_is_reported_when_not_simulated():
    # qna has no current site ("nonexistent object"), so leaving it is a failure to come
    unknown = ctx(site_folder=None)
    for expr in ("client folder of current site", "name of current site"):
        result = simulate(expr, unknown)
        assert result.text == expr
        assert [u.text for u in result.unresolved] == ["current site"], expr


def test_the_untaken_branch_keeps_an_unset_parameter():
    # qna evaluates lazily: `if false then parameter "nope" else ...` answers the
    # else branch (checked against qna 10.0.16.61 on macOS), so it is left alone
    result = simulate('if exists parameter "nope" then parameter "nope" else "d"', ctx())
    assert result.text == 'if false then parameter "nope" else "d"'


def test_the_absent_objects_are_exactly_what_qna_had_no_object_for():
    # "no-object" rows name their object path; one that is itself no-context
    # is already covered by the table above
    absent = {
        detail.split(":")[0] for _sig, outcome, detail in _dump_rows() if outcome == "no-object"
    }
    assert absent - set(NO_CONTEXT_INSPECTORS) == set(NO_OBJECT_GLOBALS)
