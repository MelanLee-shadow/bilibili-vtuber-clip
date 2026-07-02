from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Mapping, Sequence

TERM_LEXICON_SCHEMA_VERSION = "lidousha-term-lexicon.v1"
_TERM_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_-])([A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*)(?![A-Za-z0-9_-])")


@dataclass(frozen=True)
class SeedTerm:
    text: str
    count: int
    sources: tuple[str, ...]


@dataclass(frozen=True)
class TermOverride:
    canonical: str
    display: str | None = None
    aliases: tuple[str, ...] = ()

    def target_for(self, variant: str) -> str:
        # `display` is still final user-facing text.  Developer aliases such as
        # kimo熊 must not leak into subtitles; they normalize to canonical.
        return self.canonical


@dataclass(frozen=True)
class TermLexicon:
    path: Path
    schema_version: str
    seed_terms: Mapping[str, SeedTerm]
    overrides: tuple[TermOverride, ...]


def normalize_text(text: str, *, lexicon: TermLexicon | None, variant: str = "canonical") -> str:
    if lexicon is None or not text:
        return text
    normalized = text
    for override in lexicon.overrides:
        target = override.target_for(variant)
        candidates = [alias for alias in override.aliases if alias]
        if variant == "canonical":
            if override.display and override.display != target:
                candidates.append(override.display)
        elif override.canonical != target:
            candidates.append(override.canonical)
        seen: set[str] = set()
        for source in sorted(candidates, key=len, reverse=True):
            if source == target or source in seen:
                continue
            seen.add(source)
            normalized = normalized.replace(source, target)
    return normalized


@lru_cache(maxsize=16)
def load_term_lexicon(path: str | Path) -> TermLexicon:
    lexicon_path = Path(path).expanduser().resolve()
    payload = json.loads(lexicon_path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"term lexicon must be a JSON object: {lexicon_path}")
    schema_version = str(payload.get("schema_version") or TERM_LEXICON_SCHEMA_VERSION)
    sources = _load_sources(payload.get("sources"), base_dir=lexicon_path.parent)
    overrides = _load_overrides(payload.get("overrides"))
    return TermLexicon(
        path=lexicon_path,
        schema_version=schema_version,
        seed_terms=sources,
        overrides=overrides,
    )


def discover_term_lexicon(start_path: str | Path) -> Path | None:
    env_path = os.environ.get("VTUBER_SLICE_TERM_LEXICON")
    if env_path:
        candidate = Path(env_path).expanduser()
        if candidate.is_file():
            return candidate.resolve()
    start = Path(start_path).expanduser().resolve()
    anchors: Iterable[Path] = (start.parent, *start.parents)
    for anchor in anchors:
        direct = anchor / "term_lexicon.json"
        if direct.is_file():
            return direct.resolve()
        nested = anchor / "lidousha" / "term_lexicon.json"
        if nested.is_file():
            return nested.resolve()
    return None


def load_discovered_term_lexicon(start_path: str | Path) -> TermLexicon | None:
    lexicon_path = discover_term_lexicon(start_path)
    if lexicon_path is None:
        return None
    return load_term_lexicon(lexicon_path)


def _load_sources(value: object, *, base_dir: Path) -> dict[str, SeedTerm]:
    counts: dict[str, int] = {}
    sources: dict[str, set[str]] = {}
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return {}
    for item in value:
        if not isinstance(item, Mapping):
            continue
        raw_path = item.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            continue
        source_path = Path(raw_path)
        if not source_path.is_absolute():
            source_path = (base_dir / source_path).resolve()
        if not source_path.is_file():
            continue
        for term in _extract_seed_terms_from_srt(source_path):
            counts[term] = counts.get(term, 0) + 1
            sources.setdefault(term, set()).add(str(source_path))
    return {
        term: SeedTerm(text=term, count=count, sources=tuple(sorted(sources.get(term, ()))))
        for term, count in sorted(counts.items())
    }


def _extract_seed_terms_from_srt(path: Path) -> list[str]:
    raw = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    extracted: list[str] = []
    for block in raw.split("\n\n"):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        text_lines = lines[1:] if "-->" in lines[0] else lines[2:]
        for text in text_lines:
            extracted.extend(match.group(1) for match in _TERM_TOKEN_RE.finditer(text))
    return extracted


def _load_overrides(value: object) -> tuple[TermOverride, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    overrides: list[TermOverride] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        canonical = item.get("canonical")
        if not isinstance(canonical, str) or not canonical.strip():
            continue
        display = item.get("display")
        aliases = item.get("aliases")
        overrides.append(
            TermOverride(
                canonical=canonical.strip(),
                display=display.strip() if isinstance(display, str) and display.strip() else None,
                aliases=tuple(
                    alias.strip()
                    for alias in (aliases if isinstance(aliases, Sequence) and not isinstance(aliases, (str, bytes, bytearray)) else ())
                    if isinstance(alias, str) and alias.strip()
                ),
            )
        )
    return tuple(overrides)
