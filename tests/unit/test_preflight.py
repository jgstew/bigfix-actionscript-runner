"""Static preflight with bigfix-relevance-analyzer before anything runs."""

from bigfix_actionscript_runner.preflight import preflight

BES = """<?xml version="1.0" encoding="UTF-8"?>
<BES xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="BES.xsd">
    <Task>
        <Title>T</Title>
        <Description>d</Description>
        <Relevance>{relevance}</Relevance>
        <DefaultAction ID="Action1">
            <Description><PreLink>a</PreLink><Link>b</Link><PostLink>c</PostLink></Description>
            <ActionScript MIMEType="application/x-Fixlet-Windows-Shell"><![CDATA[
parameter "p" = "{{pathname of client folder of current site}}"
copy "{{pathname of file whose(name of it = "x") of folder "/tmp"}}" "{{parameter "p"}}/x"
wait /bin/echo {action_expr}
]]></ActionScript>
        </DefaultAction>
    </Task>
</BES>
"""


def write_bes(tmp_path, relevance="true", action_expr="{id of action}"):
    p = tmp_path / "t.bes"
    p.write_text(BES.format(relevance=relevance, action_expr=action_expr))
    return p


def test_clean_bes_has_no_errors(tmp_path):
    result = preflight(write_bes(tmp_path))
    assert result.errors == []
    assert result.checked > 0


def test_singular_whose_is_a_warning_not_an_error(tmp_path):
    result = preflight(write_bes(tmp_path))
    assert any("non-unique-risk" in f.code for f in result.warnings)
    assert result.errors == []


def test_parse_error_in_action_substitution_is_an_error(tmp_path):
    result = preflight(write_bes(tmp_path, action_expr="{exists file whose (}"))
    assert result.errors
    assert any(f.line and f.line > 1 for f in result.errors)


def test_parse_error_in_fixlet_relevance_is_an_error(tmp_path):
    result = preflight(write_bes(tmp_path, relevance="name of (operating system"))
    assert result.errors


def test_plain_actionscript_file(tmp_path):
    p = tmp_path / "a.bas"
    p.write_text('parameter "x" = "{name of operating system}"\nwait /bin/echo {1 + "a"}\n')
    result = preflight(p)
    assert result.checked == 2
    assert len(result.errors) == 1
    assert result.errors[0].line == 2
    assert "not defined" in result.errors[0].message


def test_plain_actionscript_style_rules_are_warnings_not_errors(tmp_path):
    p = tmp_path / "a.bas"
    p.write_text('continue if {exists file "x" of folder (parameter "logs")}\n')
    result = preflight(p)
    assert result.errors == []
    assert [w.code for w in result.warnings] == ["plural-preferred"]


def test_plain_actionscript_parse_error(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("copy {pathname of (x} b\n")
    result = preflight(p)
    assert result.errors and result.errors[0].line == 1


def test_plain_actionscript_escaped_braces_are_not_relevance(tmp_path):
    p = tmp_path / "a.bas"
    p.write_text("createfile until END\nif [ 1 ]; then {{ echo; }\nEND\n")
    assert preflight(p).checked == 0


# --- pre-commit-bigfix ActionScript checks ----------------------------------


def _script(tmp_path, text, name="a.bas"):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_unbalanced_if_is_a_blocking_actionscript_error(tmp_path):
    result = preflight(_script(tmp_path, "if {true}\ndelete a\n"))
    assert [(i.code, i.line, i.source) for i in result.errors] == [("E500", 1, "pre-commit-bigfix")]


def test_unknown_command_is_a_blocking_error(tmp_path):
    result = preflight(_script(tmp_path, "bogusverb a b\n"))
    assert [i.code for i in result.errors] == ["E300"]


def test_actionscript_warnings_do_not_block(tmp_path):
    result = preflight(_script(tmp_path, "wait cmd echo hi\nexit 0\ndelete a\n"))
    assert result.errors == []
    assert sorted(i.code for i in result.warnings) == ["W501", "W505"]


def test_convention_only_error_codes_are_warnings(tmp_path):
    # E401: no sha256 on a prefetch item -- optional unless enhanced security is on
    text = (
        "begin prefetch block\n"
        f"add prefetch item name=a.zip sha1={'a' * 40} size=10 url=https://x/a.zip\n"
        "end prefetch block\n"
    )
    result = preflight(_script(tmp_path, text))
    assert result.errors == []
    assert [i.code for i in result.warnings] == ["E401"]


def test_preflight_never_modifies_the_file(tmp_path):
    p = _script(tmp_path, 'parameter "a" = 1 // unquoted, has an auto-fix\nwait cmd echo hi\n')
    before = p.read_bytes()
    preflight(p)
    assert p.read_bytes() == before


def test_relevance_findings_are_labelled_with_their_source(tmp_path):
    result = preflight(_script(tmp_path, "copy {pathname of (x} b\n"))
    assert {i.source for i in result.errors} >= {"bigfix-relevance-analyzer"}
