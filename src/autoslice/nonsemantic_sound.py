"""One comparison-only SUB-26 definition; never transcript/edit authority.

Particles/fillers come from the acceptability policy. ASR spellings and vocal
sound surfaces live in the adjacent asset. A foreign surface is mapped only
as a complete Unicode word: both sides must satisfy (?<![\\w]) / (?![\\w]).
Punctuation/whitespace delimit words; kana, Han, Hangul, digits and underscore
do not. Thus あの人, アイ/아이, uhuru and uh_1 remain lexical text.
"""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import re
import unicodedata

from .subtitle_acceptability_policy import load_policy


SURFACES_PATH = Path(__file__).resolve().parents[2] / "assets/lidousha/nonlexical_sound_surfaces.v1.json"


@lru_cache(maxsize=1)
def _surfaces():
    return json.loads(SURFACES_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=32)
def _alternatives(tokens):
    return "(?:" + "|".join(re.escape(t) for t in sorted(set(tokens), key=lambda t: (-len(t), t))) + ")"


def _hesitations(policy=None):
    features = (load_policy() if policy is None else policy)["features"]
    # An asset spelling cannot silently reintroduce a particle removed from
    # policy. Read the effective policy on each call, not an import-time copy.
    allowed = set(features["modal_particles"]) | set(features["fillers"])
    return {row["surface"]: row["spelling"] for row in _surfaces()["hesitations"]
            if row["spelling"] in allowed}


def _hesitation_rx(spellings):
    return re.compile(r"(?<![\w])" + _alternatives(tuple(spellings)) + r"(?![\w])", re.I)


def hesitation_spelling(text, *, policy=None):
    spellings = _hesitations(policy)
    if not spellings:
        return text
    return _hesitation_rx(spellings).sub(lambda m: spellings[m[0].lower()], text)


def _laughter_pattern():
    # Each alternative repeats ONE onomatopoeic syllable, not an arbitrary
    # mixture of laughter characters. Boundary binding is applied by caller.
    return "(?:" + "|".join("(?:" + re.escape(s) + "){2,}"
        for s in _surfaces()["laughter_syllables"]) + ")"


def nonlexical_pattern(*, policy=None, include_fillers=True, vocalization_role=None):
    """Unbounded grammar for whole-sound checks and discovery scoring only.

    Callers stripping part of a lexical cue must bind this grammar themselves;
    it must never be used to erase particles inside clear spoken words.
    """
    features = (load_policy() if policy is None else policy)["features"]
    vocalizations = _surfaces()["vocalizations"]
    if vocalization_role is not None:
        # A consumer role can reference a subset, never add sound members.
        references = _surfaces()["vocalization_roles"][vocalization_role]["members"]
        vocalizations = [sound for sound in references if sound in vocalizations]
    tokens = [*features["modal_particles"], *vocalizations]
    if include_fillers:
        tokens.extend(features["fillers"])
    return re.compile("(?:" + _laughter_pattern() + "|" + _alternatives(tuple(tokens)) + ")+", re.I)


def restatement_prefix_pattern(*, policy=None):
    """Policy-bound role subset for the existing unbounded prefix score.

    A pure cue can be a particle/filler without being a removable prefix of a
    content word: 了不起, 那个人 and 哈利 must not lose their first syllable.
    These roles constrain scoring only; they never classify a sound or allow
    its omission from subtitles. References cannot add words to the definition.
    """
    features = (load_policy() if policy is None else policy)["features"]
    roles = _surfaces()["restatement_prefix_roles"]
    particles = set(features["modal_particles"])
    vocalizations = set(_surfaces()["vocalizations"])
    prefixes = [re.escape(t) for t in roles["policy_particles"] if t in particles]
    prefixes.extend(re.escape(t) for t in roles["vocalization_singles"] if t in vocalizations)
    runs = tuple(t for t in roles["vocalization_runs"] if t in vocalizations)
    if runs:
        prefixes.append(_alternatives(runs) + "{2,}")
    for filler in roles["policy_fillers"]:
        if filler in features["fillers"]:
            suffix = roles["policy_filler_particle_suffixes"].get(filler)
            prefixes.append(re.escape(filler) + (re.escape(suffix) + "?" if suffix in particles else ""))
    return re.compile("^(?:" + "|".join(prefixes) + ")")


def substantive_text(text):
    """Only standalone repeated laughter may be absent from a word witness."""
    return re.sub(r"(?<![\w])" + _laughter_pattern() + r"(?![\w])", "", text, flags=re.I)


def clear_comparison(text, proposed, *, policy=None):
    """Sound spelling/omission equivalence, retaining every lexical word.

    Only foreign hesitation in the *heard* text enables omission of its
    policy-bound Chinese spellings; ordinary audible Chinese particles are
    still literal constraints. Punctuation is left for the ordinary matcher.
    """
    spellings = _hesitations(policy)
    has_foreign_hesitation = bool(spellings and _hesitation_rx(spellings).search(text))
    text, proposed = (substantive_text(hesitation_spelling(value, policy=policy))
                      for value in (text, proposed))
    if has_foreign_hesitation:
        hesitation = re.compile(r"(?<![\w])" + _alternatives(tuple(spellings.values())) + r"(?![\w])")
        text, proposed = hesitation.sub("", text), hesitation.sub("", proposed)
    return text, proposed


def sound_only(text, *, policy=None, vocalization_role=None):
    """Recognize a whole sound cue within the consumer's native-vocal role.

    The default uses the full definition. A role only narrows native surfaces;
    policy particles/fillers and boundary-bound ASR mapping stay shared.
    """
    normalized = hesitation_spelling(text, policy=policy)
    # Test each punctuation-delimited word before joining anything: removing
    # a boundary first could turn a foreign word into a hesitation, or vice versa.
    words = re.split(r"[\s" + re.escape("".join(
        c for c in normalized if unicodedata.category(c).startswith("P"))) + r"]+", normalized)
    words = [word for word in words if word]
    pattern = nonlexical_pattern(policy=policy, vocalization_role=vocalization_role)
    return bool(words) and all(pattern.fullmatch(word) is not None for word in words)
