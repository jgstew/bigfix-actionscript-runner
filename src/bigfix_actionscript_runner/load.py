"""Load the ActionScript to simulate from a .bes file or a plain script file."""

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

ACTIONSCRIPT_MIME = "application/x-Fixlet-Windows-Shell"
_CONTENT = ("Task", "Fixlet", "SingleAction", "Baseline", "Analysis", "ComputerGroup")


class LoadError(Exception):
    pass


@dataclass
class LoadedAction:
    title: str
    script: str
    relevance: list[str] = field(default_factory=list)
    action_id: str | None = None
    first_line: int = 1
    """1-based line in the source file where `script` starts (for messages)."""


def _text(el: ET.Element | None) -> str:
    return (el.text or "") if el is not None else ""


def _first_line(raw: str, script: str) -> int:
    pos = raw.find(script) if script.strip() else -1
    return raw.count("\n", 0, pos) + 1 if pos >= 0 else 1


def _check_mime(el: ET.Element, what: str) -> None:
    mime = el.get("MIMEType", ACTIONSCRIPT_MIME)
    if mime != ACTIONSCRIPT_MIME:
        raise LoadError(
            f"{what} is a {mime} script, not ActionScript; only ActionScript is simulated"
        )


def is_bes_text(text: str) -> bool:
    """BES XML, whatever the file is called (exports are often .bes.xml or .xml)."""
    head = text.lstrip("\ufeff \t\r\n")[:200].lower()
    return head.startswith("<bes") or (head.startswith("<?xml") and "<bes" in text[:2000].lower())


def is_bes(path: str | Path) -> bool:
    path = Path(path)
    if path.suffix.lower() == ".bes":
        return True
    with open(path, "rb") as f:
        return is_bes_text(f.read(2048).decode("utf-8-sig", errors="replace"))


def load_action(path: str | Path, action_id: str | None = None) -> LoadedAction:
    path = Path(path)
    raw = path.read_bytes().decode("utf-8-sig").replace("\r\n", "\n")
    if path.suffix.lower() != ".bes" and not is_bes_text(raw):
        return LoadedAction(title=path.name, script=raw)

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise LoadError(f"{path.name} is not valid XML: {e}") from e
    item = next((el for el in root if el.tag in _CONTENT), None)
    if item is None:
        raise LoadError(f"{path.name} has no Task, Fixlet or SingleAction")
    title = _text(item.find("Title")).strip()
    relevance = [_text(r).strip() for r in item.findall("Relevance")]

    if item.tag == "SingleAction":
        script_el = item.find("ActionScript")
        if script_el is None:
            raise LoadError(f"{path.name}: the SingleAction has no ActionScript")
        _check_mime(script_el, "the SingleAction")
        script = _text(script_el)
        return LoadedAction(
            title, script.strip("\n"), relevance, None, _first_line(raw, script.strip("\n"))
        )

    actions = [el for el in item if el.tag in ("DefaultAction", "Action")]
    if not actions:
        raise LoadError(f"{path.name}: this {item.tag} has no action to simulate")
    if action_id is None:
        chosen = next((a for a in actions if a.tag == "DefaultAction"), actions[0])
    else:
        match = next((a for a in actions if a.get("ID") == action_id), None)
        if match is None:
            ids = ", ".join(a.get("ID", "?") for a in actions)
            raise LoadError(f"no action {action_id} in {path.name}; it has {ids}")
        chosen = match
    script_el = chosen.find("ActionScript")
    if script_el is None:
        raise LoadError(f"{chosen.get('ID')} has no ActionScript")
    _check_mime(script_el, chosen.get("ID", "the action"))
    script = _text(script_el).strip("\n")
    return LoadedAction(title, script, relevance, chosen.get("ID"), _first_line(raw, script))
