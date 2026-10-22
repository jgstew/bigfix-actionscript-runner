"""Where every path an action names ends up, per sandbox mode.

full    (dry run)    client folders AND every other path are in the sandbox
client  (--execute)  only the client folders are in the sandbox; other paths are real
off     (--execute --no-sandbox)  the real client's folders; everything is real
"""

import os
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from bigfix_actionscript_runner.host import LocalHost
from bigfix_actionscript_runner.sandbox import Sandbox

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX paths on a POSIX LocalHost")


class PathOnlyHost:
    """Just enough of a Host for path mapping on another OS: records mkdir."""

    def __init__(self, pure) -> None:
        self.pure = pure
        self.windows = pure is PureWindowsPath
        self.made: list[str] = []

    def mkdir(self, path):
        self.made.append(path)

    def exists(self, path):
        return False


def j(*parts):
    return str(Path(*parts))


# --- full (dry run) --------------------------------------------------------


def test_layout_is_created(tmp_path):
    sb = Sandbox(tmp_path / "sb")
    assert Path(sb.site).is_dir()
    assert Path(sb.site, "__Download").is_dir()
    assert Path(sb.data, "__Global", "Logs").is_dir()
    assert sb.data == j(tmp_path.resolve(), "sb", "__BESData")
    assert sb.mode == "full"
    assert isinstance(sb.host, LocalHost)


def test_relative_paths_are_in_the_site_folder(tmp_path):
    sb = Sandbox(tmp_path)
    assert sb.resolve("__Download/a.zip") == j(sb.site, "__Download", "a.zip")
    assert sb.resolve("__createfile") == j(sb.site, "__createfile")


def test_backslashes_are_separators(tmp_path):
    sb = Sandbox(tmp_path)
    assert sb.resolve("__Download\\a.zip") == j(sb.site, "__Download", "a.zip")


def test_paths_already_inside_the_sandbox_stay(tmp_path):
    sb = Sandbox(tmp_path)
    inside = j(sb.data, "__Global", "Logs", "x.txt")
    assert sb.resolve(inside) == inside


@posix_only
def test_other_absolute_posix_paths_go_under_fs(tmp_path):
    sb = Sandbox(tmp_path)
    assert sb.resolve("/tmp/run_scan.sh") == j(sb.fs, "tmp", "run_scan.sh")
    assert sb.resolve("/Library/Application Support/x") == j(
        sb.fs, "Library", "Application Support", "x"
    )


def test_windows_drive_paths_go_under_fs(tmp_path):
    sb = Sandbox(tmp_path)
    assert sb.resolve("C:\\Program Files (x86)\\BigFix\\x.txt") == j(
        sb.fs, "C", "Program Files (x86)", "BigFix", "x.txt"
    )
    assert sb.resolve("c:/temp/a") == j(sb.fs, "C", "temp", "a")


def test_dotdot_cannot_escape_the_sandbox(tmp_path):
    sb = Sandbox(tmp_path)
    assert sb.contains(sb.resolve("../../../../etc/passwd"))
    assert sb.contains(sb.resolve("/tmp/../../etc/passwd"))


def test_reads_fall_back_to_the_real_file_outside(tmp_path):
    real = tmp_path / "outside.txt"
    real.write_text("hi")
    sb = Sandbox(tmp_path / "sb")
    assert sb.resolve_read(str(real)) == str(real)  # not in the sandbox yet: read the real one
    mapped = Path(sb.resolve(str(real)))
    mapped.parent.mkdir(parents=True)
    mapped.write_text("sandbox copy")
    assert sb.resolve_read(str(real)) == str(mapped)  # the sandbox copy wins once it exists


def test_reads_of_relative_paths_never_leave_the_sandbox(tmp_path):
    sb = Sandbox(tmp_path / "sb")
    assert sb.resolve_read("nope.txt") == j(sb.site, "nope.txt")


def test_display_shows_sandbox_relative_paths(tmp_path):
    sb = Sandbox(tmp_path)
    assert sb.display(j(sb.site, "__Download", "a")) == "<sandbox>/site/__Download/a"
    assert sb.display("/etc/hosts") == "/etc/hosts"


def test_protected_folders(tmp_path):
    sb = Sandbox(tmp_path)
    assert {sb.root, sb.site, sb.data, sb.fs} <= sb.protected


def test_unknown_mode_is_refused(tmp_path):
    with pytest.raises(ValueError, match="mode"):
        Sandbox(tmp_path, mode="partial")


# --- client (--execute): only the client folders are sandboxed ----------------


def test_client_mode_keeps_client_folders_in_the_sandbox(tmp_path):
    sb = Sandbox(tmp_path / "sb", mode="client")
    assert sb.resolve("__createfile") == j(sb.site, "__createfile")
    assert sb.data == j(tmp_path.resolve(), "sb", "__BESData")
    assert Path(sb.data, "__Global", "Logs").is_dir()


@posix_only
def test_client_mode_leaves_other_absolute_paths_real(tmp_path):
    sb = Sandbox(tmp_path / "sb", mode="client")
    real = j(tmp_path, "real", "x.sh")
    assert sb.resolve(real) == real
    assert sb.resolve("/usr/local/bin/../bin/php") == "/usr/local/bin/php"
    assert not sb.contains(real)


@posix_only
def test_client_mode_reads_are_real_too(tmp_path):
    sb = Sandbox(tmp_path / "sb", mode="client")
    assert sb.resolve_read("/etc/hosts") == "/etc/hosts"


def test_client_mode_has_no_fs_folder(tmp_path):
    sb = Sandbox(tmp_path / "sb", mode="client")
    assert sb.fs is None
    assert not (tmp_path / "sb" / "fs").exists()


# --- off (--execute --no-sandbox): the real client folders ----------------------


@posix_only
def test_off_mode_uses_the_real_client_folders(tmp_path):
    site, data = tmp_path / "BES" / "__BESData" / "MySite", tmp_path / "BES" / "__BESData"
    site.mkdir(parents=True)
    sb = Sandbox(mode="off", site_folder=str(site), data_folder=str(data))
    assert sb.root is None
    assert sb.site == str(site) and sb.data == str(data)
    assert sb.resolve("__createfile") == j(site, "__createfile")
    assert sb.resolve("/etc/x") == "/etc/x"
    assert sb.display(j(site, "a")) == j(site, "a")


def test_off_mode_needs_both_folders(tmp_path):
    with pytest.raises(ValueError, match="site_folder"):
        Sandbox(mode="off", data_folder=str(tmp_path))


def test_full_and_client_modes_need_a_root():
    with pytest.raises(ValueError, match="root"):
        Sandbox(mode="client")


# --- a Windows target ------------------------------------------------------------


def test_windows_target_full_mode():
    host = PathOnlyHost(PureWindowsPath)
    sb = Sandbox("C:\\Users\\u\\AppData\\Local\\Temp\\sb", host=host)
    assert sb.site == "C:\\Users\\u\\AppData\\Local\\Temp\\sb\\site"
    assert sb.resolve("__Download\\a.zip") == sb.site + "\\__Download\\a.zip"
    assert sb.resolve("__Download/a.zip") == sb.site + "\\__Download\\a.zip"
    assert sb.resolve("C:\\Program Files\\x\\y.txt") == f"{sb.fs}\\C\\Program Files\\x\\y.txt"
    assert sb.resolve(sb.data + "\\__Global\\Logs\\r.txt") == sb.data + "\\__Global\\Logs\\r.txt"
    assert sb.display(sb.site + "\\__Download\\a") == "<sandbox>/site/__Download/a"
    assert sb.site + "\\__Download" in host.made


def test_windows_target_client_mode_leaves_drive_paths_real():
    sb = Sandbox("C:\\T\\sb", host=PathOnlyHost(PureWindowsPath), mode="client")
    assert sb.resolve("C:\\Windows\\Temp\\x.ps1") == "C:\\Windows\\Temp\\x.ps1"
    assert sb.resolve("c:/temp/../a") == "c:\\a"
    assert sb.resolve("x.txt") == "C:\\T\\sb\\site\\x.txt"


def test_windows_sandbox_paths_are_case_insensitive():
    sb = Sandbox("C:\\T\\sb", host=PathOnlyHost(PureWindowsPath))
    assert sb.contains("c:\\t\\SB\\site\\a")


def test_posix_target_from_any_machine():
    host = PathOnlyHost(PurePosixPath)
    sb = Sandbox("/tmp/sb", host=host, mode="client")
    assert sb.resolve("a\\b") == "/tmp/sb/site/a/b"
    assert sb.resolve("/opt/x") == "/opt/x"
