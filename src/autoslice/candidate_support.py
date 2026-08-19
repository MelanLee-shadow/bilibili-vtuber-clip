"""Pure structured-support predicates shared by final review and acoustics."""

from __future__ import annotations

import re
from typing import Mapping

try:
    from pypinyin import lazy_pinyin as _lazy_pinyin
except Exception:  # pragma: no cover - optional production dependency
    _lazy_pinyin = None


_LATIN_PRONUNCIATION = {
    "a": "ei", "b": "bi", "c": "xi", "d": "di", "e": "yi",
    "f": "ai fu", "g": "ji", "h": "ei chi", "i": "ai", "j": "jie",
    "k": "kei", "l": "ai le", "m": "ai mu", "n": "en", "o": "ou",
    "p": "pi", "q": "kiu", "r": "a er", "s": "ai si", "t": "ti",
    "u": "you", "v": "wei", "w": "da bu liu", "x": "ai ke si",
    "y": "wai", "z": "zei",
}


def orthography_pronunciation_key(text: str) -> tuple[str, ...]:
    """Conservative toneless key, including spoken Latin letter names."""

    if not text or _lazy_pinyin is None:
        return ()
    tokens: list[str] = []
    han: list[str] = []

    def flush() -> None:
        if han:
            tokens.extend(str(value).casefold() for value in _lazy_pinyin("".join(han)))
            han.clear()

    for char in text:
        if char.isascii() and char.isalpha():
            flush()
            tokens.extend(_LATIN_PRONUNCIATION[char.casefold()].split())
        elif char.isalnum():
            han.append(char)
        else:
            flush()
    flush()
    collapsed: list[str] = []
    for token in tokens:
        normalized = re.sub(r"[^a-z0-9üv]", "", token.casefold())
        if normalized and (not collapsed or collapsed[-1] != normalized):
            collapsed.append(normalized)
    return tuple(collapsed)


def bound_structured_chat_surface(
    clip_context: Mapping[str, object] | None,
    surface: str,
) -> dict[str, object] | None:
    """Return only an exact surface hit carried by a sha256-bound chat row."""

    rows = clip_context.get("structured_chat") if isinstance(clip_context, Mapping) else None
    if not isinstance(rows, list) or not surface:
        return None
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(surface)}(?![A-Za-z0-9_])", re.IGNORECASE
    )
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        sha256 = str(row.get("source_sha256") or "")
        text = str(row.get("text") or row.get("message") or "")
        if re.fullmatch(r"[0-9a-f]{64}", sha256) and pattern.search(text):
            return {
                "kind": "structured_chat_bound",
                "surface": surface,
                "source_sha256": sha256,
                "source_event_id": str(row.get("source_event_id") or "") or None,
            }
    return None


def _declared_respell_edit(current_cue: str, proposed_cue: str) -> bool:
    try:
        from src.autoslice.term_authority import respell_pairs

        return any(
            surface and canonical and surface in current_cue
            and current_cue.replace(surface, canonical, 1) == proposed_cue
            for surface, canonical in respell_pairs()
        )
    except Exception:
        return False


def orthography_ambiguous(
    *, current_cue: str, proposed_cue: str, suspect: str, replacement: str
) -> bool:
    """Whether audio alone cannot distinguish the written alternatives."""

    if _declared_respell_edit(current_cue, proposed_cue):
        return True
    try:
        from src.autoslice.subtitle_fidelity import _homophone_equal

        if suspect and replacement and _homophone_equal(suspect, replacement):
            return True
    except Exception:
        pass
    current_key = orthography_pronunciation_key(current_cue)
    return bool(current_key and current_key == orthography_pronunciation_key(proposed_cue))


def registered_misheard_direction(*, suspect: str, replacement: str) -> bool:
    if not suspect or not replacement:
        return False
    try:
        from src.autoslice.term_authority import expected_value_respell_pairs, respell_pairs

        pair = (suspect, replacement)
        return pair in respell_pairs() or pair in expected_value_respell_pairs()
    except Exception:
        return False


def structured_text_support(
    *, candidate_provenance: object, replacement: str, proposed_cue: str,
    clip_context: Mapping[str, object] | None,
) -> bool:
    provenance = candidate_provenance if isinstance(candidate_provenance, Mapping) else {}
    if provenance.get("kind") == "session_restatement":
        return True
    for surface in (str(provenance.get("surface") or ""), replacement):
        if surface and bound_structured_chat_surface(clip_context, surface) is not None:
            return True
    try:
        from src.autoslice.term_authority import exact_cue_canons

        return bool(proposed_cue and proposed_cue in exact_cue_canons())
    except Exception:
        return False
