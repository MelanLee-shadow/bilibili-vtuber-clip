#!/usr/bin/env python3
"""Parse the Li Dousha glossary into CPA terminology-QA inputs.

The CPA semantic QA ``terminology_ok`` gate is only as good as what it is
told to look for.  Historically the request was built with a single hard-coded
term (``applied_terms=("kmx",)``), so the gate could never notice that a
normalized subtitle still said 停放熊 / 沙特琳 / 一四二 / 苏丹 instead of the
canonical kmx / 沙豆李 / 142 / 奶油苏打.

This module reads the selected profile's ``glossary`` asset (for the default
profile, ``assets/lidousha/glossary.txt``) and extracts:

* ``canon``            — the approved proper-noun spellings the reviewer must
  keep (kmx, 142, 小室, Ado, 沙豆李, 掏兜, 倒反天罡, 奶油苏打, …).
* ``mishear_blacklist`` — the ASR mishearing / wrong-form variants that must NOT
  survive normalization (停放熊, 康姆叉, 沙特琳, 一四二, 苏丹, 阿朵, 小寺, …),
  harvested from the glossary's "不要写成 X"、"不要改成 X"、"听成 X 等" clauses.
* ``expected_value_pairs`` — only glossary bullets with one unambiguous
  canonical target contribute their explicitly listed wrong forms to the
  zero-CPA expected-value lane.  Ambiguous multi-name bullets contribute
  nothing, so registered proper names remain peers.

Everything here is best-effort and fail-safe: any IO/parse problem yields a
minimal ``GlossaryTerms(("kmx",), ())`` rather than raising, so a malformed or
missing glossary can never crash the QA pipeline (fail-safe, not fail-closed —
the terminology gate simply degrades to the old single-term behaviour).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.channel_profile import load_channel_profile  # noqa: E402


CHANNEL_PROFILE = load_channel_profile(ROOT)
# The repo-vendored glossary is the primary authority; the flat legacy copy is a
# fallback for older checkouts.
DEFAULT_GLOSSARY_PATHS: tuple[Path, ...] = (
    CHANNEL_PROFILE.asset_file("glossary"),
    ROOT / "lidousha" / "lidousha_glossary.txt",
)
# Never return an empty canon list — kmx is the one name that must always be
# enforced even if parsing yields nothing.
FALLBACK_CANON: tuple[str, ...] = ("kmx",)

# Characters stripped from the edge of any extracted token (mixed full/half
# width quotes, brackets and book-title marks all appear in the glossary).
_STRIP_EDGE = " \t　“”\"'‘’「」『』（）()《》【】"
# A canon token is a latin word (optionally with internal spaces, e.g.
# "cream soda") or a short CJK/alnum run (e.g. 李豆沙 / 142 / 打call / Ado).
_CANON_TOKEN_RE = re.compile(r"^(?:[A-Za-z][A-Za-z ]*[A-Za-z]|[A-Za-z0-9一-鿿]{1,8})$")
# A blacklist token: 1..10 chars, no whitespace, CJK/latin/digits only.
_BLACKLIST_TOKEN_RE = re.compile(r"^[A-Za-z0-9一-鿿]{1,10}$")

# Directive phrases that name the canonical form outright:
#   一律写小写 kmx / 一律写成 142 / 一律修正为“沙豆李”
_CANON_DIRECTIVE_RE = re.compile(
    r"一律(?:写成|写作|写小写|修正为)\s*[“\"‘'『「]?\s*([A-Za-z0-9一-鿿]{1,8})"
)
# A looser "…修正为“X”" catch (e.g. 一律按语境修正为“小李”) requiring a quote so
# it does not swallow surrounding prose.
_CANON_QUOTED_FIX_RE = re.compile(r"修正为\s*[“\"‘'『「]([A-Za-z0-9一-鿿]{1,8})")

# Mishearing markers. "不要改成/写成 …" runs to the sentence end; the "听成 …
# 等" forms stop at the first 等 so we never swallow the trailing prose.
_MISHEAR_DONT_RE = re.compile(r"不要(?:改成|写成)\s*([^。\n]+)")
_MISHEAR_HEARD_RE = re.compile(
    r"(?:误听成|误听|听成|听岔成)\s*([^。\n]+?)\s*等"
)
_MISHEAR_CANDIDATES_RE = re.compile(
    r"(?:常见)?误听候选包括\s*([^；。\n]+)"
)
_MISWRITE_QUOTED_RE = re.compile(
    r"(?:常)?误写成\s*[“\"‘'『「]([^”\"’'』」；。\n]+)[”\"’'』」]"
)

# Split an extracted span into individual terms.  Bare CJK "和" is not a safe
# delimiter: it is the first character of canonical names such as 和成天下.
_TOKEN_SPLIT_RE = re.compile(r"[、,，/｜|]|\s+(?:或|和)\s+")
_PAREN_ANNOTATION_RE = re.compile(r"[（(][^）)]*[）)]")


@dataclass(frozen=True)
class GlossaryTerms:
    """Canonical proper nouns and their known ASR mishearing variants."""

    canon: tuple[str, ...]
    mishear_blacklist: tuple[str, ...]


def _clean_token(token: str) -> str:
    token = token.strip(_STRIP_EDGE)
    # Drop a trailing 等 that clings to the last list item (…康姆叉等).
    if token.endswith("等") and len(token) > 1:
        token = token[:-1].strip(_STRIP_EDGE)
    return token


def _leading_terms(content: str) -> list[str]:
    """Canon terms from the leading "term、term、term" run of a bullet.

    The run ends at the first sentence break / parenthetical, so prose-first
    lines (沙豆李 / 小李 自称) contribute nothing here and are covered by the
    directive rules instead.
    """

    head = content
    for stop in ("（", "(", "。", "——", "—", "；", ";"):
        idx = head.find(stop)
        if idx != -1:
            head = head[:idx]
    out: list[str] = []
    for piece in _TOKEN_SPLIT_RE.split(head):
        token = _clean_token(piece)
        if token and _CANON_TOKEN_RE.match(token):
            out.append(token)
    return out


def _blacklist_terms(content: str) -> list[str]:
    spans: list[str] = []
    spans.extend(_MISHEAR_DONT_RE.findall(content))
    spans.extend(_MISHEAR_HEARD_RE.findall(content))
    spans.extend(_MISHEAR_CANDIDATES_RE.findall(content))
    spans.extend(_MISWRITE_QUOTED_RE.findall(content))
    out: list[str] = []
    for span in spans:
        # A dated provenance note is sometimes attached to the final variant,
        # e.g. ``下斗里（2026-07-27 ...）``.  It is metadata, not part of the
        # wrong surface; leaving it attached made that last variant silently
        # disappear from terminology QA and expected-value normalization.
        span = _PAREN_ANNOTATION_RE.sub("", span)
        for piece in _TOKEN_SPLIT_RE.split(span):
            token = _clean_token(piece)
            if token and _BLACKLIST_TOKEN_RE.match(token):
                out.append(token)
    return out


def parse_glossary_expected_value_pairs(
    text: str,
) -> tuple[tuple[str, str], ...]:
    """Return explicit wrong-surface -> canonical pairs from safe bullets.

    This does not fuzzily compare every transcript word against every glossary
    term.  A pair exists only when one glossary bullet both names exactly one
    canonical target and explicitly lists a wrong surface of at least three
    code points.  Short everyday words such as ``小时``/``留下`` have a poor
    unconditional prior and remain with CPA unless the profile explicitly
    promotes that exact pair.  Thus the high-precision majority is mechanical
    and free, while ambiguous names and common-word homophones leave the lane.
    """

    pairs: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("- "):
            continue
        line = line[2:]
        for colon in ("：", ":"):
            idx = line.find(colon)
            if idx != -1:
                content = line[idx + 1 :]
                break
        else:
            content = line

        explicit = list(
            dict.fromkeys(
                [
                    *_CANON_DIRECTIVE_RE.findall(content),
                    *_CANON_QUOTED_FIX_RE.findall(content),
                ]
            )
        )
        if len(explicit) == 1:
            canonical = explicit[0]
        else:
            leading = list(dict.fromkeys(_leading_terms(content)))
            if len(leading) != 1:
                continue
            canonical = leading[0]
        for surface in _blacklist_terms(content):
            if len(surface) >= 3 and surface != canonical:
                pairs.append((surface, canonical))
    return tuple(dict.fromkeys(pairs))


def load_glossary_expected_value_pairs(
    path: str | Path | None = None,
) -> tuple[tuple[str, str], ...]:
    """Load deterministic glossary pairs; malformed/missing assets disable it."""

    try:
        resolved = _resolve_glossary_path(path)
        if resolved is None:
            return ()
        return parse_glossary_expected_value_pairs(
            resolved.read_text(encoding="utf-8")
        )
    except Exception:  # noqa: BLE001 - conservative degradation
        return ()


def parse_glossary_terms(text: str) -> GlossaryTerms:
    """Extract canon proper nouns and the mishearing blacklist from glossary text."""

    canon: list[str] = []
    blacklist: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("- "):
            continue
        line = line[2:]
        # Everything after the first full/half-width colon is the payload; the
        # label (人名/ID, 主播名, …) is metadata we discard.
        for colon in ("：", ":"):
            idx = line.find(colon)
            if idx != -1:
                content = line[idx + 1 :]
                break
        else:
            content = line

        canon.extend(_leading_terms(content))
        canon.extend(match for match in _CANON_DIRECTIVE_RE.findall(content))
        canon.extend(match for match in _CANON_QUOTED_FIX_RE.findall(content))
        blacklist.extend(_blacklist_terms(content))

    canon_unique = tuple(dict.fromkeys(term for term in canon if term))
    canon_set = set(canon_unique)
    # A canonical form is never also a violation to flag.
    blacklist_unique = tuple(
        dict.fromkeys(term for term in blacklist if term and term not in canon_set)
    )
    return GlossaryTerms(canon=canon_unique, mishear_blacklist=blacklist_unique)


def _resolve_glossary_path(path: str | Path | None) -> Path | None:
    if path is not None:
        candidate = Path(path).expanduser()
        return candidate if candidate.is_file() else None
    for candidate in DEFAULT_GLOSSARY_PATHS:
        if candidate.is_file():
            return candidate
    return None


@lru_cache(maxsize=8)
def _load_cached(resolved: str) -> GlossaryTerms:
    text = Path(resolved).read_text(encoding="utf-8")
    parsed = parse_glossary_terms(text)
    if not parsed.canon:
        return GlossaryTerms(canon=FALLBACK_CANON, mishear_blacklist=parsed.mishear_blacklist)
    return parsed


def load_glossary_terms(path: str | Path | None = None) -> GlossaryTerms:
    """Load and parse the glossary, never raising.

    Returns ``GlossaryTerms(("kmx",), ())`` if the glossary is missing or
    unparseable, so the CPA terminology gate degrades gracefully instead of
    crashing the pipeline.
    """

    try:
        resolved = _resolve_glossary_path(path)
        if resolved is None:
            return GlossaryTerms(canon=FALLBACK_CANON, mishear_blacklist=())
        return _load_cached(str(resolved.resolve()))
    except Exception:  # noqa: BLE001 - fail-safe: any error degrades to fallback
        return GlossaryTerms(canon=FALLBACK_CANON, mishear_blacklist=())


if __name__ == "__main__":
    terms = load_glossary_terms()
    print(f"canon ({len(terms.canon)}): {'、'.join(terms.canon)}")
    print(f"blacklist ({len(terms.mishear_blacklist)}): {'、'.join(terms.mishear_blacklist)}")
