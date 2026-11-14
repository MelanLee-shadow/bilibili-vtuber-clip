"""The lane names no channel: who the host is lives in the profile (src/autoslice/manual_lane/channel.py).

Switching channel is switching profile.  That only holds while the lane's prompts, its rule numbers, the tools it runs on
the media host and its own code never spell a channel's names themselves; this test reads the active profile's names
and fails when one of them appears there again.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from src.autoslice.manual_lane import channel

REPO = Path(__file__).resolve().parents[2]
LANE = REPO / "src/autoslice/manual_lane"
# identifiers of persisted records and of files outside the lane: renaming them is not a change of channel
KEPT = ("lidousha-title-cover-route.v1", "audit_review_package.py", "lidousha-speaker-uniform-host-phase",
        "_write_sapphire_ass_from_srt")
DOC_KEYS = ("_doc", "source", "reason")


def _names() -> list[str]:
    profile = channel.profile()
    names = {profile.display_name, profile.short_name, *profile.self_reference_aliases, profile.profile_id,
             str(channel.text()["fans_name"])}
    return sorted(n for n in names if n)


def _hits(where: str, text: str, names: list[str]) -> list[str]:
    for kept in KEPT:
        text = text.replace(kept, "")
    return [f"{where}: {name}" for name in names if name.lower() in text.lower()]


def _code_text(path: Path) -> str:
    """Everything a module says that is not a comment or a docstring: its string constants and its identifiers."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = {id(node.body[0].value) for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body
            and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)}
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            out.append(node.value)
        elif isinstance(node, ast.Name):
            out.append(node.id)
        elif isinstance(node, ast.Attribute):
            out.append(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append(node.name)
        elif isinstance(node, ast.arg):
            out.append(node.arg)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.append(node.target.id)
    return "\n".join(out)


def _rule_text(value: object) -> str:
    """lane_rules.json without the notes on where a number came from."""
    if isinstance(value, dict):
        return "\n".join(f"{k}\n{_rule_text(v)}" for k, v in value.items()
                         if k not in DOC_KEYS and not k.endswith(("_source", "_basis", "_doc")))
    if isinstance(value, list):
        return "\n".join(_rule_text(v) for v in value)
    return str(value)


def test_the_lane_names_no_channel() -> None:
    names = _names()
    found: list[str] = []
    for path in sorted((REPO / "assets/manual_lane/prompts").glob("*.md")):
        found += _hits(f"prompts/{path.name}", path.read_text(encoding="utf-8"), names)
    for path in sorted((REPO / "assets/manual_lane/remote").glob("*.py")):
        found += _hits(f"remote/{path.name}", _code_text(path), names)
    for path in sorted(LANE.glob("*.py")):
        found += _hits(path.name, _code_text(path), names)
    found += _hits("lane_rules.json", _rule_text(json.loads((REPO / "assets/manual_lane/lane_rules.json").read_text("utf-8"))), names)
    found += _hits("scripts/manual_lane.py", _code_text(REPO / "scripts/manual_lane.py"), names)
    assert found == [], "the lane spells a channel's own name; put it in the profile and use the placeholder:\n" + "\n".join(found)


def test_a_prompt_gets_the_channel_from_the_profile() -> None:
    fields = channel.prompt_fields()
    assert fields["host"] == channel.profile().display_name
    for name in ("boundary.md", "title.md", "cover_judge.md", "subtitle_playbook_rules.md", "listen_blind.md"):
        text = channel.prompt(name)
        assert all("{" + key + "}" not in text for key in fields), name
    assert channel.profile().display_name in channel.prompt("boundary.md")


def test_the_template_profile_has_every_field() -> None:
    template = json.loads((REPO / "profiles/_template" / channel.TEXT_FILE).read_text(encoding="utf-8"))
    assert all(key in template for key in channel.TEXT_KEYS)
