"""Extract and load the shared title-cover editorial method."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

SKILL_FILE = "SKILL.md"
EXAMPLES_FILE = "references/examples.md"
JUDGE_FILE = "references/judge-prompt.md"
SKILL_TITLE_SECTIONS = (("## 四、", "## 五、"), ("## 五、", "## 六、"))
JUDGE_RULES_START = "3. **意思**"
JUDGE_RULES_LAST = "**判官守则**"
JUDGE_TEMPLATE_SLOT = "@@RULES@@"
JUDGE_SOURCE_NAME = "lidousha-title-cover/references/judge-prompt.md"


def _method_path() -> Path:
    """The active channel's title method (its asset directory): a channel writes titles by its own method."""
    from src.autoslice.channel_profile import load_channel_profile

    return load_channel_profile(Path(__file__).resolve().parents[2]).asset_root / "title_editorial_method.json"


TITLE_EDITORIAL_METHOD = _method_path()


class AuthorityError(RuntimeError):
    """An authority file or one of its required sections is missing or malformed."""


class SourceRecord(TypedDict):
    name: str
    path: str
    sha256: str


class ProjectionData(TypedDict):
    sections: str
    judge_rules: str
    examples: str
    sources: list[SourceRecord]
    judge_template: str
    judge_source: SourceRecord


@dataclass(frozen=True)
class Source:
    name: str
    path: str
    sha256: str

    def as_dict(self) -> SourceRecord:
        return {"name": self.name, "path": self.path, "sha256": self.sha256}


def _read(name: str, path: Path) -> tuple[str, Source]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise AuthorityError(f"{name} unreadable: {path}: {exc}") from exc
    return data.decode("utf-8"), Source(name, str(path), hashlib.sha256(data).hexdigest())


def _between(text: str, start: str, end: str | None, *, where: str) -> str:
    """Extract complete lines from the start marker to just before the end marker."""
    lines = text.splitlines()
    try:
        i = next(n for n, ln in enumerate(lines) if ln.startswith(start))
    except StopIteration:
        raise AuthorityError(f"{where}: section marker {start!r} not found") from None
    j = len(lines)
    if end is not None:
        try:
            j = next(n for n in range(i + 1, len(lines)) if lines[n].startswith(end))
        except StopIteration:
            raise AuthorityError(f"{where}: section marker {end!r} not found after {start!r}") from None
    return "\n".join(lines[i:j]).strip()


def judge_rules(skill_text: str) -> str:
    """Section 八 item 3 through the 判官守则 paragraph, as used by the manual lane."""
    lines = skill_text.splitlines()
    try:
        i = next(n for n, ln in enumerate(lines) if ln.lstrip().startswith(JUDGE_RULES_START))
        j = next(n for n in range(i + 1, len(lines)) if lines[n].lstrip().startswith(JUDGE_RULES_LAST))
    except StopIteration:
        raise AuthorityError("SKILL.md: section 八 item 3 (judge rules) not found") from None
    return "\n".join(lines[i:j + 1]).strip()


def judge_template(judge_file_text: str) -> str:
    """The acceptance judge's template: judge-prompt.md after its first `---` line, which must carry @@RULES@@."""
    parts = judge_file_text.split("\n---\n", 1)
    if len(parts) != 2 or JUDGE_TEMPLATE_SLOT not in parts[1]:
        raise AuthorityError(f"judge-prompt.md: template after '---' with {JUDGE_TEMPLATE_SLOT} not found")
    return parts[1].strip()


@dataclass(frozen=True)
class EditorialMethod:
    sections: str                 # skill sections 四 and 五, joined by a blank line
    judge_rules: str
    examples: str
    sources: tuple[Source, Source]    # the files the ordinary title prompt carries: SKILL.md, examples.md
    judge_template: str           # references/judge-prompt.md after `---`
    judge_source: Source          # judge-prompt.md, recorded with the judge's prompt but not part of `sources`


def cover_text(method: EditorialMethod) -> str:
    """Skill section 五 (cover text) alone: the tail of `sections`."""
    marker = "\n\n" + SKILL_TITLE_SECTIONS[1][0]
    _, found, tail = method.sections.partition(marker)
    if not found:
        raise AuthorityError("title editorial method: section 五 not found in sections")
    return (SKILL_TITLE_SECTIONS[1][0] + tail).strip()


def extract_title_editorial_method(root: Path) -> EditorialMethod:
    """Read the same source sections and byte hashes used by the manual lane."""
    skill, s_skill = _read("lidousha-title-cover/SKILL.md", root / SKILL_FILE)
    examples, s_examples = _read("lidousha-title-cover/references/examples.md", root / EXAMPLES_FILE)
    judge, s_judge = _read(JUDGE_SOURCE_NAME, root / JUDGE_FILE)
    return EditorialMethod(
        sections="\n\n".join(_between(skill, a, b, where="SKILL.md") for a, b in SKILL_TITLE_SECTIONS),
        judge_rules=judge_rules(skill),
        examples=examples.strip(),
        sources=(s_skill, s_examples),
        judge_template=judge_template(judge),
        judge_source=s_judge,
    )


def format_title_editorial_method(method: EditorialMethod) -> str:
    return "\n\n".join([
        "### lidousha-title-cover skill 第四、五节\n" + method.sections,
        "### skill 第八节第 3 条（事实核与修辞的判法，标题写完后会按它验收）\n" + method.judge_rules,
        "### skill 示范写法（references/examples.md）\n" + method.examples,
    ])


def projection_data(method: EditorialMethod) -> ProjectionData:
    """Make a portable, deterministic projection with source filenames and byte hashes."""
    sources: list[SourceRecord] = [
        {"name": source.name, "path": path, "sha256": source.sha256}
        for source, path in zip(method.sources, (SKILL_FILE, EXAMPLES_FILE), strict=True)
    ]
    judge_source: SourceRecord = {"name": method.judge_source.name, "path": JUDGE_FILE,
                                  "sha256": method.judge_source.sha256}
    return {"sections": method.sections, "judge_rules": method.judge_rules, "examples": method.examples,
            "sources": sources, "judge_template": method.judge_template, "judge_source": judge_source}


def _source_record(source: object, name: str, source_path: str, path: Path) -> Source:
    if (not isinstance(source, dict) or set(source) != {"name", "path", "sha256"}
            or source["name"] != name or source["path"] != source_path
            or not isinstance(source["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", source["sha256"]) is None):
        raise AuthorityError(f"title editorial method malformed: {path}: invalid source record")
    return Source(name, source_path, source["sha256"])


def load_title_editorial_method() -> EditorialMethod:
    """Load the deployed projection; missing or malformed content is an error."""
    path = TITLE_EDITORIAL_METHOD
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuthorityError(f"title editorial method unreadable: {path}: {exc}") from exc
    keys = {"sections", "judge_rules", "examples", "sources", "judge_template", "judge_source"}
    if not isinstance(payload, dict) or set(payload) != keys:
        raise AuthorityError(f"title editorial method malformed: {path}: expected {', '.join(sorted(keys))}")
    sections, rules, examples, template = (payload[key] for key in ("sections", "judge_rules", "examples",
                                                                     "judge_template"))
    if (not all(isinstance(value, str) and value.strip() for value in (sections, rules, examples, template))
            or not sections.startswith(SKILL_TITLE_SECTIONS[0][0])
            or f"\n\n{SKILL_TITLE_SECTIONS[1][0]}" not in sections
            or not rules.startswith(JUDGE_RULES_START) or JUDGE_RULES_LAST not in rules
            or JUDGE_TEMPLATE_SLOT not in template):
        raise AuthorityError(f"title editorial method malformed: {path}: missing required editorial text")
    raw_sources = payload["sources"]
    expected = (("lidousha-title-cover/SKILL.md", SKILL_FILE),
                ("lidousha-title-cover/references/examples.md", EXAMPLES_FILE))
    if not isinstance(raw_sources, list) or len(raw_sources) != len(expected):
        raise AuthorityError(f"title editorial method malformed: {path}: invalid sources")
    sources = [_source_record(source, name, source_path, path)
               for source, (name, source_path) in zip(raw_sources, expected, strict=True)]
    judge_source = _source_record(payload["judge_source"], JUDGE_SOURCE_NAME, JUDGE_FILE, path)
    return EditorialMethod(sections, rules, examples, (sources[0], sources[1]), template, judge_source)
