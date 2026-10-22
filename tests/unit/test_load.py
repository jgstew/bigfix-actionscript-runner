"""Loading the ActionScript (and relevance) from a .bes file or a plain script file."""

import pytest

from bigfix_actionscript_runner.load import LoadError, load_action

TASK = """<?xml version="1.0" encoding="UTF-8"?>
<BES xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="BES.xsd">
    <Task>
        <Title>My Task</Title>
        <Description>d</Description>
        <Relevance>windows of operating system</Relevance>
        <Relevance><![CDATA[x64 of operating system]]></Relevance>
        <DefaultAction ID="Action1">
            <Description><PreLink>a</PreLink><Link>b</Link><PostLink>c</PostLink></Description>
            <ActionScript MIMEType="application/x-Fixlet-Windows-Shell"><![CDATA[
// default
wait cmd /c echo one
]]></ActionScript>
        </DefaultAction>
        <Action ID="Action2">
            <Description><PreLink>a</PreLink><Link>b</Link><PostLink>c</PostLink></Description>
            <ActionScript MIMEType="application/x-Fixlet-Windows-Shell"><![CDATA[wait cmd /c echo two]]></ActionScript>
        </Action>
        <Action ID="Action3">
            <Description><PreLink>a</PreLink><Link>b</Link><PostLink>c</PostLink></Description>
            <ActionScript MIMEType="application/x-sh"><![CDATA[#!/bin/sh
echo hi]]></ActionScript>
        </Action>
    </Task>
</BES>
"""

SINGLE = """<?xml version="1.0" encoding="UTF-8"?>
<BES xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="BES.xsd">
    <SingleAction>
        <Title>One Off</Title>
        <Relevance>true</Relevance>
        <ActionScript MIMEType="application/x-Fixlet-Windows-Shell">delete __createfile</ActionScript>
    </SingleAction>
</BES>
"""


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_default_action_is_used(tmp_path):
    a = load_action(write(tmp_path, "t.bes", TASK))
    assert a.title == "My Task"
    assert a.action_id == "Action1"
    assert "echo one" in a.script
    assert a.relevance == ["windows of operating system", "x64 of operating system"]


def test_pick_an_action_by_id(tmp_path):
    a = load_action(write(tmp_path, "t.bes", TASK), action_id="Action2")
    assert a.script == "wait cmd /c echo two"


def test_line_offset_points_into_the_file(tmp_path):
    p = write(tmp_path, "t.bes", TASK)
    a = load_action(p)
    lines = p.read_text().splitlines()
    idx = a.script.splitlines().index("wait cmd /c echo one")
    assert lines[a.first_line - 1 + idx] == "wait cmd /c echo one"


def test_unknown_action_id(tmp_path):
    with pytest.raises(LoadError, match="Action9.*Action1, Action2, Action3"):
        load_action(write(tmp_path, "t.bes", TASK), action_id="Action9")


def test_non_actionscript_mime_type_is_refused(tmp_path):
    with pytest.raises(LoadError, match="application/x-sh"):
        load_action(write(tmp_path, "t.bes", TASK), action_id="Action3")


def test_single_action(tmp_path):
    a = load_action(write(tmp_path, "s.bes", SINGLE))
    assert (a.title, a.action_id, a.script, a.relevance) == (
        "One Off",
        None,
        "delete __createfile",
        ["true"],
    )


def test_analysis_has_no_action(tmp_path):
    analysis = '<?xml version="1.0"?><BES><Analysis><Title>A</Title><Relevance>true</Relevance></Analysis></BES>'
    with pytest.raises(LoadError, match="no action"):
        load_action(write(tmp_path, "a.bes", analysis))


def test_plain_actionscript_file(tmp_path):
    a = load_action(write(tmp_path, "x.bas", "delete __createfile\n"))
    assert (a.title, a.script, a.relevance, a.first_line) == (
        "x.bas",
        "delete __createfile\n",
        [],
        1,
    )


def test_crlf_is_normalized(tmp_path):
    p = tmp_path / "x.bas"
    p.write_bytes(b"delete a\r\ndelete b\r\n")
    assert load_action(p).script == "delete a\ndelete b\n"
