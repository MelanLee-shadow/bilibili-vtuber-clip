"""Conservative deterministic lexical difference features; no model calls.

This is a bounded rule table, not a general Chinese semantic parser. Unknown
lexical changes remain meaning-carrying for review. Equivalence never authorizes
editing clear audio or dropping an audible fragment.
"""

from __future__ import annotations

from difflib import SequenceMatcher
import re
import unicodedata


def lexical(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return "".join(
        c
        for i, c in enumerate(text)
        if not c.isspace()
        and (
            unicodedata.category(c)[0] not in {"P", "Z"}
            or (c in {"-", "−"} and i + 1 < len(text) and text[i + 1].isdigit())
            or (
                c in {".", ":", ","}
                and 0 < i < len(text) - 1
                and text[i - 1].isdigit()
                and text[i + 1].isdigit()
            )
        )
    )


def _replace_groups(text, groups):
    alternatives = {word: group[0] for group in groups for word in group}
    if not alternatives:
        return text
    return re.sub(
        "|".join(map(re.escape, sorted(alternatives, key=len, reverse=True))),
        lambda m: alternatives[m[0]],
        text,
    )


def equivalence_key(text: str, policy: dict) -> str:
    """Narrow equivalences for refrain scoring; does not strip content words."""
    text = _replace_groups(lexical(text), policy["features"]["synonym_groups"])
    particles = "".join(policy["features"]["modal_particles"])
    return text.strip(particles)


def _repetitions(text, number_time_pattern):
    # Adjacent complete repeats only; never remove 不 from 能不能, etc.
    previous = None
    while previous != text:
        previous = text
        text = re.sub(
            r"(.{1,8}?)\1+", lambda m: m[0] if re.search(number_time_pattern, m[1]) else m[1], text
        )
    return text


def classify_difference(a: str, b: str, *, glossary_terms, policy: dict) -> dict:
    if not isinstance(a, str) or not isinstance(b, str):
        raise TypeError("candidate text must be str")
    f = policy["features"]
    features = []

    def add(name):
        if name not in features:
            features.append(name)

    x, y = lexical(a), lexical(b)
    for key in ('persona_action',):
        if any((word in x) != (word in y) for word in f[key]):
            add(key)
    if a != b and x == y:
        add("punctuation")
    # Names are checked before equivalences: a particle-looking name is a name.
    terms = sorted(
        {lexical(str(t)) for t in glossary_terms if lexical(str(t))}, key=len, reverse=True
    )
    for term in terms:
        if (term in x) != (term in y):
            add("proper_name")
    sx, sy = _replace_groups(x, f["synonym_groups"]), _replace_groups(y, f["synonym_groups"])
    if (sx, sy) != (x, y) and x != y:
        add("synonym")
    x, y = sx, sy
    particles = "".join(f["modal_particles"])
    sx, sy = x.strip(particles), y.strip(particles)
    if (sx, sy) != (x, y) and x != y:
        add("modal_particle")
    x, y = sx, sy
    # Standalone discourse fillers; not 那个 in 那个人/那个东西.
    for filler in f["fillers"]:
        pattern = r"^" + re.escape(filler) + r"(?=我|你|他|她|它|不|是|要|有|说)"
        sx, sy = re.sub(pattern, "", x), re.sub(pattern, "", y)
        if (sx, sy) != (x, y) and x != y:
            add("filler")
        x, y = sx, sy
    sx, sy = _repetitions(x, f["number_time_pattern"]), _repetitions(y, f["number_time_pattern"])
    if (sx, sy) != (x, y) and x != y:
        add("stutter_repetition")
    x, y = sx, sy
    # A terminal, incomplete heard quotation is a restart, not a completed
    # proposition. 完整的“不许他去/不是他去” never matches these patterns.
    for pattern in f["unfinished_restart_patterns"]:
        left, right = re.fullmatch(pattern, x), re.fullmatch(pattern, y)
        if left and right and left.group("stem") == right.group("stem"):
            add("unfinished_restart")
            x = y
            break
    for rule in f["shortening_rules"]:
        short, long = sorted((x, y), key=len)
        if (
            re.fullmatch(rule["core_pattern"], short)
            and long.startswith(short)
            and re.fullmatch(rule["tail_pattern"], long[len(short) :])
        ):
            add("shortening_core_retained")
            x = y
    for rule in f["word_order_rules"]:
        sx, sy = (
            re.sub(rule["pattern"], rule["replacement"], x),
            re.sub(rule["pattern"], rule["replacement"], y),
        )
        if sx == sy and x != y:
            add("word_order")
            x = y
    if x != y:
        # Compare whole-token occurrences as well as changed spans; 不喜欢 must
        # retain the changed 不 even if 喜欢 is shared.
        for key in ("negation_polarity", "referent", "action", "evaluation_stance", "derogatory",
                    "object_place", "syntax_role", "persona_action"):
            if any(x.count(word) != y.count(word) for word in f[key]):
                add(key)
        if re.findall(f["number_time_pattern"], x) != re.findall(f["number_time_pattern"], y):
            add("number_time")
        # Unclassified words may be an unknown entity/fact: never silently
        # label arbitrary near-homophones or a deletion as harmless.
        if any(
            tag != "equal" for tag, *_ in SequenceMatcher(None, x, y, autojunk=False).get_opcodes()
        ):
            add("unclassified_lexical")
    return {
        "meaning_carrying": any(name in f["meaning_carrying"] for name in features),
        "features": features,
    }
