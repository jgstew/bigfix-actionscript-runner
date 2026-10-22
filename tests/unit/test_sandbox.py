"""Every path an action writes is mapped into the sandbox; nothing outside changes."""

from pathlib import Path

from bigfix_actionscript_runner.sandbox import Sandbox


def test_layout_is_created(tmp_path):
    sb = Sandbox(tmp_path / "sb")
    assert sb.site.is_dir()
    assert (sb.site / "__Download").is_dir()
    assert (sb.data / "__Global" / "Logs").is_dir()
    assert sb.data == tmp_path / "sb" / "__BESData"


def test_relative_paths_are_in_the_site_folder(tmp_path):
    sb = Sandbox(tmp_path)
    assert sb.resolve("__Download/a.zip") == sb.site / "__Download" / "a.zip"
    assert sb.resolve("__createfile") == sb.site / "__createfile"


def test_backslashes_are_separators(tmp_path):
    sb = Sandbox(tmp_path)
    assert sb.resolve("__Download\\a.zip") == sb.site / "__Download" / "a.zip"


def test_paths_already_inside_the_sandbox_stay(tmp_path):
    sb = Sandbox(tmp_path)
    inside = str(sb.data / "__Global" / "Logs" / "x.txt")
    assert sb.resolve(inside) == Path(inside)


def test_other_absolute_posix_paths_go_under_fs(tmp_path):
    sb = Sandbox(tmp_path)
    assert sb.resolve("/tmp/run_scan.sh") == sb.fs / "tmp" / "run_scan.sh"
    assert (
        sb.resolve("/Library/Application Support/x")
        == sb.fs / "Library" / "Application Support" / "x"
    )


def test_windows_drive_paths_go_under_fs(tmp_path):
    sb = Sandbox(tmp_path)
    assert sb.resolve("C:\\Program Files (x86)\\BigFix\\x.txt") == (
        sb.fs / "C" / "Program Files (x86)" / "BigFix" / "x.txt"
    )
    assert sb.resolve("c:/temp/a") == sb.fs / "C" / "temp" / "a"


def test_dotdot_cannot_escape_the_sandbox(tmp_path):
    sb = Sandbox(tmp_path)
    p = sb.resolve("../../../../etc/passwd")
    assert sb.contains(p)
    p = sb.resolve("/tmp/../../etc/passwd")
    assert sb.contains(p)


def test_reads_fall_back_to_the_real_file_outside(tmp_path):
    real = tmp_path / "outside.txt"
    real.write_text("hi")
    sb = Sandbox(tmp_path / "sb")
    assert sb.resolve_read(str(real)) == real  # not in the sandbox yet: read the real one
    mapped = sb.resolve(str(real))
    mapped.parent.mkdir(parents=True)
    mapped.write_text("sandbox copy")
    assert sb.resolve_read(str(real)) == mapped  # the sandbox copy wins once it exists


def test_reads_of_relative_paths_never_leave_the_sandbox(tmp_path):
    sb = Sandbox(tmp_path / "sb")
    assert sb.resolve_read("nope.txt") == sb.site / "nope.txt"


def test_display_shows_sandbox_relative_paths(tmp_path):
    sb = Sandbox(tmp_path)
    assert sb.display(sb.site / "__Download" / "a") == "<sandbox>/site/__Download/a"
    assert sb.display(Path("/etc/hosts")) == "/etc/hosts"
