"""Derive scoped hints: python3.14 -m src.autoslice.manual_lane.mishearing.

Authorities remain read-only. A surface is a candidate, never authorization to
replace speech. Missing scope is not global authority. The JSON is a cache.
"""
from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast
from . import channel

if TYPE_CHECKING:
    from src.autoslice.cpa_gemini_client import JsonValue


class PairScope(TypedDict, total=False):
    kind: str
    description: str
    recording_basename: str
    source_start_ms: int | float
    source_end_ms: int | float
    truth_id: str
    context_patterns: list[str]


class PairEvidence(TypedDict):
    scope: PairScope
    source: NotRequired[str]
    pointer: NotRequired[str]
    origin: NotRequired[str]
    authority: NotRequired[str]


class FormalMatch(TypedDict):
    entity: str
    source: NotRequired[str]
    pointer: NotRequired[str]


class MemoryContext(TypedDict):
    source: str
    pointer: str
    scope: PairScope
    evidence: JsonValue
    action: JsonValue


class MemoryInput(TypedDict, total=False):
    candidate_canonicals: list[str]
    scope: PairScope
    evidence: JsonValue
    action: JsonValue


class LexiconSurface(TypedDict, total=False):
    misheard: str


class MishearingPair(LexiconSurface):
    standard: str
    evidence: list[PairEvidence]
    formal_matches: NotRequired[list[FormalMatch]]
    conflicts: NotRequired[list[FormalMatch]]
    memory_context: NotRequired[list[MemoryContext]]


class PairsDocument(TypedDict):
    schema_version: str
    generated_by: str
    source_files: list[str]
    entries: list[MishearingPair]

ROOT = Path(__file__).resolve().parents[3]
OUTPUT = channel.name_table("mishearing_pairs.json")     # the active channel's
# The active channel's own tables, by the asset keys its profile registers (a channel names its files itself).
SOURCES = {
    **{kind: channel.profile().asset_file(key).relative_to(ROOT).as_posix() for kind, key in (
        ("entities", "entity_confusables"), ("ledger", "subtitle_truth_ledger"), ("memory", "speech_memory_ledger"),
        ("roster", "psplive_roster_sources"))},
    "names": channel.name_table("proper_nouns.json").relative_to(ROOT).as_posix(),
}


def derive_pairs(root: Path = ROOT) -> list[MishearingPair]:
    data = {k: json.loads((root / p).read_text(encoding="utf-8")) for k, p in SOURCES.items()}
    rows: dict[tuple[str, str], MishearingPair] = {}

    def add(surface: str, standard: str, evidence: PairEvidence) -> None:
        if not surface or not standard or surface == standard:
            return
        row = rows.setdefault((surface, standard), {
            "misheard": surface, "standard": standard, "evidence": [],
        })
        row["evidence"].append(evidence)

    for gi, group in enumerate(data["entities"]["groups"]):
        for ei, entity in enumerate(group.get("entities", [])):
            for si, surface in enumerate(entity.get("surfaces", [])):
                add(surface, entity["canonical"], {
                    "source": SOURCES["entities"],
                    "pointer": f"/groups/{gi}/entities/{ei}/surfaces/{si}",
                    "origin": "entity_confusables",
                    "scope": cast(PairScope, group.get("scope") or {
                        "kind": "context", "description": group.get("reason", "实体语境候选"),
                    }),
                    "authority": group.get("reason", ""),
                })
    for i, entry in enumerate(data["names"]):
        standard = str(entry.get("canonical") or entry.get("name") or "")
        for ci, rule in enumerate(entry.get("context_mishearings") or []):
            patterns = [p for p in (rule.get("context_patterns") or []) if isinstance(p, str) and p]
            misheard = str(rule.get("misheard") or "")
            if not patterns or not misheard or not standard:
                continue
            add(misheard, standard, {
                "source": SOURCES["names"],
                "pointer": f"/{i}/context_mishearings/{ci}",
                "origin": "proper_nouns_context",
                "scope": {
                    "kind": "context",
                    "description": str(rule.get("description") or entry.get("condition") or "专名语境误听候选"),
                    "context_patterns": patterns,
                },
                "authority": str(entry.get("condition") or entry.get("source") or ""),
            })
    for i, entry in enumerate(data["ledger"]["entries"]):
        for ri, replacement in enumerate(entry.get("replacements") or []):
            add(replacement.get("surface", ""), replacement.get("canonical", ""), {
                "source": SOURCES["ledger"], "pointer": f"/entries/{i}/replacements/{ri}",
                "origin": "ledger", "authority": entry.get("authority", ""),
                "scope": cast(PairScope, {"kind": "recording_interval", **{
                    k: entry[k] for k in ("recording_basename", "source_start_ms", "source_end_ms", "truth_id")
                    if k in entry
                }}),
            })
    _attach_memory(rows, data["memory"]["entries"])
    registry: dict[str, list[FormalMatch]] = {}

    def register(name: str, aliases: list[str], source: str, pointer: str) -> None:
        for form in [name, *aliases]:
            registry.setdefault(form, []).append({"entity": name, "source": source, "pointer": pointer})

    for account, entry in data["roster"].get("member_overrides", {}).items():
        register(entry["canonical"], entry.get("aliases", []), SOURCES["roster"], f"/member_overrides/{account}")
    for i, entry in enumerate(data["names"]):
        if entry.get("detection_name") is not False:
            register(entry["name"], entry.get("aliases", []), SOURCES["names"], f"/{i}")
    for row in rows.values():
        row["formal_matches"] = registry.get(row["misheard"], [])
        row["conflicts"] = [m for m in row["formal_matches"] if m["entity"] != row["standard"]]
    return [rows[k] for k in sorted(rows)]


def _attach_memory(rows: dict[tuple[str, str], MishearingPair], entries: list[MemoryInput]) -> None:
    # Candidate-only memory does not authorize new surface/canonical cross products.
    for i, entry in enumerate(entries):
        for row in rows.values():
            if row["standard"] in entry.get("candidate_canonicals", []):
                row.setdefault("memory_context", []).append({
                    "source": SOURCES["memory"], "pointer": f"/entries/{i}",
                    "scope": entry.get("scope", {}), "evidence": entry.get("evidence", []),
                    "action": entry.get("action"),
                })


def generated_document(root: Path = ROOT) -> PairsDocument:
    return {
        "schema_version": "manual-lane-mishearing-pairs.v2",
        "generated_by": "python3.14 -m src.autoslice.manual_lane.mishearing",
        "source_files": list(SOURCES.values()),
        "entries": derive_pairs(root),
    }


def write_generated(path: Path = OUTPUT) -> None:
    path.write_text(json.dumps(generated_document(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def load_pairs(path: Path = OUTPUT) -> list[MishearingPair]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return cast(list[MishearingPair], data["entries"] if isinstance(data, dict) else data)


def _scope_text(evidence: PairEvidence) -> str:
    scope = evidence["scope"]
    if scope.get("kind") == "recording_interval":
        return (f"录音 {scope.get('recording_basename', '未注明')}，"
                f"{scope.get('source_start_ms', '?')}–{scope.get('source_end_ms', '?')} ms，"
                f"裁定 {scope.get('truth_id', '未注明')}（authority：{evidence.get('authority', '')}）")
    return str(scope.get("description") or scope)


def surface_matches(row: Mapping[str, object], text: str) -> bool:
    """Whether a misheard surface applies to `text` under any structured per-line `context_patterns` scope."""
    misheard = str(row.get("misheard") or "")
    if not misheard or misheard not in text:
        return False
    evidence = row.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return True
    lines = text.splitlines() or [text]
    for entry in evidence:
        scope = entry.get("scope") if isinstance(entry, Mapping) else None
        patterns = scope.get("context_patterns") if isinstance(scope, Mapping) else None
        if not isinstance(patterns, list) or not patterns:
            return True
        if any(re.search(str(pattern), line) for line in lines for pattern in patterns):
            return True
    return False


def _has_context_patterns(row: MishearingPair) -> bool:
    evidence = row.get("evidence") or []
    return bool(evidence) and all(bool(entry["scope"].get("context_patterns")) for entry in evidence)


def relevant_pairs(rows: list[MishearingPair], readings: list[str]) -> list[MishearingPair]:
    """Literal occurrence in an actual clip reading or participant form; context-scoped pairs require a pattern hit."""
    return [r for r in rows if any(
        surface_matches(r, text) if _has_context_patterns(r)
        else (r["standard"] and r["standard"] in text) or surface_matches(r, text)
        for text in readings
    )]


def render_lines(rows: list[MishearingPair]) -> str:
    lines = []
    scoped: dict[str, list[str]] = {}
    for row in rows:
        surface, standard = row["misheard"], row["standard"]
        conflicts = row.get("conflicts") or []
        if conflicts:
            owners = "、".join(dict.fromkeys(m["entity"] for m in conflicts))
            lines.append(f"- 两个实体易混：『{surface}』（『{owners}』的正式写法或别称）；『{standard}』。"
                         "不是替换规则，按本句音频和语境判断。")
            continue
        for evidence in row["evidence"]:
            if evidence["scope"].get("kind") == "global":
                lines.append(f"- {surface} → {standard}")
            else:
                scoped.setdefault(_scope_text(evidence), []).append(f"『{standard}』被误听/误写为『{surface}』")
    for scope, pairs in scoped.items():
        lines.append(f"- 曾在{scope}语境下，" + "；".join(dict.fromkeys(pairs))
                     + "；不是替换规则，按本句音频和语境判断。")
    return "\n".join(dict.fromkeys(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    write_generated(parser.parse_args().output)


if __name__ == "__main__":
    main()
