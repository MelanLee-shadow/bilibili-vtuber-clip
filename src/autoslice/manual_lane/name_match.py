"""Names of the proper-noun table in a sentence (TASK-28; COACH-ADVICE e05 §1.1 第3/6条, §5.3, §5.5, §5.7).

The channel's table (channel.name_table("proper_nouns.json"), U117) and the session's participant groups give the names and their
aliases.  Three questions, all for detection and the record (no text is replaced here: CPA writes the name, U107; no
global string replacement, 41-semantic-repair 第11条):
- near_names: a run of the sentence that sounds like the Chinese part of a name (toneless pinyin, the initials and
  finals ASR confuses merged) without being written as that name;
- unknown_latin: Latin-letter words of the sentence (two letters or more, no common English short word) that are
  no name or alias of the table;
- unknown_spellings: of those, the ones that neither the table nor the clip's danmaku nor its title carries (the
  「未定专名」 list).

TASK-36: `sound_key` is that same pinyin comparison, exposed so the one-spelling-per-name pass
(name_consistency.py, e05 §5.6) groups two spellings of one name by exactly the rule this module already uses;
`table_rows` returns the table with each entry's note (the note is what decides the spelling where it has one).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, cast

from ..acoustic_pinyin import phrase_words, text_pinyin_tokens, valid_syllables
from ..channel_profile import ChannelProfileError
from . import channel
from .config import unknown_participant
from .lane_rules import rules

if TYPE_CHECKING:
    from src.autoslice.cpa_gemini_client import JsonValue
    from .lane_rules import DetectionRules


class NameRow(TypedDict):
    name: str
    aliases: tuple[str, ...]
    role: str
    note: str


class _NameInput(TypedDict, total=False):
    name: JsonValue
    aliases: list[JsonValue]
    role: JsonValue
    note: JsonValue
    detection_name: bool


class UnresolvedSpelling(TypedDict, total=False):
    spelling: str
    count: int | None
    note: str

PROPER_NOUNS = channel.name_table("proper_nouns.json")   # the active channel's table
SHARED_NODES = Path(__file__).resolve().parents[3] / "assets" / "manual_lane" / "knowledge" / "nodes.jsonl"
_HAN = re.compile(r"[一-鿿]+")
_MARKS = re.compile(r"[《》〈〉「」『』“”‘’\"']")      # marks that set a title or a quotation off inside a sentence
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9]*")
# initials and finals that ASR and speech blur: zh/z, ch/c, sh/s, n/l; in/ing, en/eng
_INITIALS = (("zh", "z"), ("ch", "c"), ("sh", "s"), ("n", "l"))
_FINALS = (("ing", "in"), ("eng", "en"))
# high-frequency function and basic characters (playbook_evidence reads this set: one definition)
COMMON_CHARS = frozenset(
    "的地得了着过是不在有我你他她它们这那就都也还很吗呢吧啊呀哦嗯么什个一说要会能可到去来上下里看想没对好把被给让和跟与而又再才只人家大小多少"
)


def _default_roster_path() -> Path | None:
    try:
        return channel.profile().asset_file("psplive_roster_sources")
    except ChannelProfileError:
        return None


ROSTER_SOURCES: Path | None = _default_roster_path()


@lru_cache(maxsize=8)
def _read_roster_cached(path_str: str, _mtime_ns: int, _size: int) -> tuple[tuple[str, tuple[str, ...]], ...]:
    path = Path(path_str)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid roster file {path}: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"invalid roster file {path}: root must be an object")
    overrides = doc.get("member_overrides")
    if not isinstance(overrides, dict):
        raise ValueError(f"invalid roster file {path}: member_overrides must be an object")
    out: list[tuple[str, tuple[str, ...]]] = []
    for key, entry in overrides.items():
        if not isinstance(entry, dict):
            raise ValueError(f"invalid roster file {path}: member_overrides[{key!r}] must be an object")
        canonical = entry.get("canonical")
        if not isinstance(canonical, str) or not canonical.strip():
            raise ValueError(f"invalid roster file {path}: member_overrides[{key!r}].canonical must be a non-empty string")
        aliases = entry.get("aliases", [])
        if not isinstance(aliases, list) or any(not isinstance(a, str) for a in aliases):
            raise ValueError(f"invalid roster file {path}: member_overrides[{key!r}].aliases must be a list of strings")
        out.append((canonical, tuple(aliases)))
    return tuple(out)


def _roster_entries() -> tuple[tuple[str, tuple[str, ...]], ...]:
    path = ROSTER_SOURCES
    if path is None or not path.exists():
        return ()
    if not path.is_file():
        raise ValueError(f"invalid roster file {path}: expected a file")
    st = path.stat()
    return _read_roster_cached(str(path), st.st_mtime_ns, st.st_size)


_roster_entries.cache_clear = _read_roster_cached.cache_clear  # type: ignore[attr-defined]


@lru_cache(maxsize=8)
def _read_shared_cached(path_str: str, _mtime_ns: int, _size: int) -> tuple[tuple[str, tuple[str, ...]], ...]:
    path = Path(path_str)
    out: list[tuple[str, tuple[str, ...]]] = []
    try:
        raw_text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"invalid shared terms file {path}: {exc}") from exc
    for lineno, line in enumerate(raw_text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            node = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid shared terms file {path} line {lineno}: {exc}") from exc
        if not isinstance(node, dict):
            raise ValueError(f"invalid shared terms file {path} line {lineno}: line must be an object")
        if node.get("status") == "retired":
            continue
        names = node.get("names")
        if not isinstance(names, dict) or not isinstance(names.get("display"), str) or not names["display"].strip():
            raise ValueError(f"invalid shared terms file {path} line {lineno}: names.display must be a non-empty string")
        if any(not isinstance(v, str) for v in names.values()):
            raise ValueError(f"invalid shared terms file {path} line {lineno}: names values must be strings")
        aliases = node.get("aliases", [])
        if not isinstance(aliases, list) or any(not isinstance(a, str) for a in aliases):
            raise ValueError(f"invalid shared terms file {path} line {lineno}: aliases must be a list of strings")
        display = names["display"]
        forms = [*names.values(), *aliases]
        extra = tuple(s for s in dict.fromkeys(forms) if s and s != display)
        out.append((display, extra))
    return tuple(out)


def _shared_entries() -> tuple[tuple[str, tuple[str, ...]], ...]:
    path = SHARED_NODES
    if not path.exists():
        return ()
    if not path.is_file():
        raise ValueError(f"invalid shared terms file {path}: expected a file")
    st = path.stat()
    return _read_shared_cached(str(path), st.st_mtime_ns, st.st_size)


_shared_entries.cache_clear = _read_shared_cached.cache_clear  # type: ignore[attr-defined]


def _single_han(text: str) -> str:
    """Single contiguous Chinese-character run of `text`, or '' when absent or split by non-Han characters."""
    runs = _HAN.findall(text)
    return runs[0] if len(runs) == 1 else ""


def _blur(syllable: str) -> str:
    for a, b in _INITIALS:
        if syllable.startswith(a):
            syllable = b + syllable[len(a):]
            break
    for a, b in _FINALS:
        if syllable.endswith(a):
            syllable = syllable[: -len(a)] + b
            break
    return syllable


def _pinyin(text: str) -> list[str]:
    return [_blur(t) for t in (text_pinyin_tokens(text) or [])]


@lru_cache(maxsize=4096)
def sound_key(text: str) -> tuple[str, ...] | None:
    """The blurred pinyin of a run of Chinese characters, one syllable per character; None when the run has no
    pinyin (punctuation) or its syllable count does not match its character count.  Two spellings of one name share
    this key, which is the comparison `near_names` and TASK-36's grouping both use."""
    han = "".join(_HAN.findall(text))
    if not han:
        return None
    key = tuple(_pinyin(han))
    return key if len(key) == len(han) else None


def raw_pinyin(text: str) -> tuple[str, ...] | None:
    """The pinyin of a run of Chinese characters as it is spoken (no blur), one syllable per character; None when it does
    not line up.  A Latin spelling written from the initials (TASK-36, e05 §5.4) is measured on these: the letter is what
    was said, so 南 is n there even though n and l are one sound for the Chinese comparison."""
    han = "".join(_HAN.findall(text))
    if not han:
        return None
    syllables = tuple(t for t in (text_pinyin_tokens(han) or []))
    return syllables if len(syllables) == len(han) else None



@lru_cache(maxsize=1)
def table_rows() -> tuple[NameRow, ...]:
    """The proper-noun table as written: name, aliases, role, note, origin (the note decides the spelling where the
    table has one, e05 §5.1; it is read by name_consistency.py)."""
    rows = cast(list[_NameInput], json.loads(PROPER_NOUNS.read_text(encoding="utf-8")))
    return tuple({"name": str(r["name"]), "aliases": tuple(str(a) for a in r.get("aliases") or []),
                  "role": str(r.get("role") or ""), "note": str(r.get("note") or "")}
                 for r in rows if r.get("detection_name") is not False)


def _table() -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple((r["name"], tuple(r["aliases"])) for r in table_rows())



NameSource = Literal["table", "participant", "roster", "shared"]


class NameCandidate(TypedDict):
    name: str
    source: NameSource
    weak: bool


class NameEntries(list[tuple[str, tuple[str, ...]]]):
    """`(name, aliases)` list carrying entry provenance (`sources`), primary (table + participant) rows for
    `unknown_latin`, and roster/shared-added spellings for weak-source filtering."""

    def __init__(
        self,
        rows: Iterable[tuple[str, tuple[str, ...]]],
        *,
        sources: dict[str, NameSource] | None = None,
        primary_rows: Sequence[tuple[str, tuple[str, ...]]] = (),
        weak_spellings: set[tuple[str, str]] | None = None,
    ) -> None:
        super().__init__(rows)
        self.sources: dict[str, NameSource] = dict(sources or {})
        self.primary_rows: tuple[tuple[str, tuple[str, ...]], ...] = tuple(primary_rows)
        self.weak_spellings: set[tuple[str, str]] = set(weak_spellings or ())


def entry_source(names: Sequence[tuple[str, tuple[str, ...]]], name: str) -> NameSource:
    """Return the source (`table`, `participant`, `roster`, or `shared`) recorded for `name`."""
    if isinstance(names, NameEntries):
        return names.sources.get(name, "table")
    return "table"


def entries(participants: Iterable[Sequence[str]] = ()) -> NameEntries:
    """(name, aliases) of the table, the session's participant groups (first = the name), and 3+ syllable
    contiguous-Han roster/shared entries."""
    out = list(_table())
    sources: dict[str, NameSource] = {n: "table" for n, _ in out}
    for g in participants:
        if g and not unknown_participant(str(g[0])) and str(g[0]) not in sources:
            pname = str(g[0])
            out.append((pname, tuple(str(a) for a in g[1:])))
            sources[pname] = "participant"
    primary_rows = tuple(out)
    owner_by_spelling: dict[str, int] = {}
    for idx, (n, als) in enumerate(out):
        for s in (n, *als):
            owner_by_spelling.setdefault(s, idx)
    weak_spellings: set[tuple[str, str]] = set()
    weak_groups: tuple[tuple[NameSource, tuple[tuple[str, tuple[str, ...]], ...]], ...] = (
        ("roster", _roster_entries()),
        ("shared", _shared_entries()),
    )
    for src, items in weak_groups:
        for canon, aliases in items:
            raw_forms = tuple(dict.fromkeys((canon, *aliases)))
            owner = next((owner_by_spelling[s] for s in raw_forms if s in owner_by_spelling), None)
            forms = tuple(s for s in raw_forms if len(_single_han(s)) >= 3 and s not in owner_by_spelling)
            if not forms:
                continue
            if owner is not None:
                cur_name, cur_aliases = out[owner]
                added = tuple(f for f in forms if f != cur_name and f not in cur_aliases)
                out[owner] = (cur_name, (*cur_aliases, *added))
                for f in forms:
                    owner_by_spelling.setdefault(f, owner)
                    weak_spellings.add((cur_name, f))
            elif len(_single_han(canon)) >= 3:
                idx = len(out)
                out.append((canon, tuple(f for f in forms if f != canon)))
                sources[canon] = src
                for f in forms:
                    owner_by_spelling.setdefault(f, idx)
                    weak_spellings.add((canon, f))
            else:
                for form in forms:
                    idx = len(out)
                    out.append((form, ()))
                    sources[form] = src
                    owner_by_spelling.setdefault(form, idx)
                    weak_spellings.add((form, form))
    return NameEntries(out, sources=sources, primary_rows=primary_rows, weak_spellings=weak_spellings)


def _spellings(names: Sequence[tuple[str, tuple[str, ...]]]) -> list[tuple[str, str]]:
    """(spelling, the name it belongs to) for the name and every alias."""
    return [(s, name) for name, aliases in names for s in (name, *aliases)]


def _spelled_out(text: str, spelling: str) -> bool:
    """A name written only in Latin letters (four or more) that the recogniser wrote as Chinese characters sounding it out:
    the run's toneless pinyin, joined, is the name (阿娃娃 a-wa-wa = awawa, 苏米 = Sumi)."""
    latin = spelling.lower()
    if len(latin) < 4 or not latin.isascii() or not latin.isalpha():
        return False
    for run in _HAN.findall(text):
        tokens = text_pinyin_tokens(run) or []
        if len(tokens) != len(run):
            continue
        for i in range(len(run) - 1):
            joined = ""
            for token in tokens[i:]:
                joined += token.lower()
                if joined == latin:
                    return True
                if not latin.startswith(joined):
                    break
    return False


@lru_cache(maxsize=4096)
def _drop_fuse_keys(han: str) -> frozenset[tuple[str, ...]]:
    """Blurred keys formed by dropping one syllable (interior only for 3-syllable names, any position for 4+) or
    fusing two adjacent syllables (initial(s1)+final(s2) when valid, or s1 alone when s2 starts with a vowel)."""
    raw = raw_pinyin(han)
    if raw is None or len(raw) < 3:
        return frozenset()
    blur = tuple(_blur(s) for s in raw)
    keys: set[tuple[str, ...]] = set()
    drop_positions = range(len(blur)) if len(blur) >= 4 else range(1, len(blur) - 1)
    for i in drop_positions:
        keys.add(blur[:i] + blur[i + 1:])
    valid = valid_syllables()
    for i in range(len(raw) - 1):
        ini1 = _initial(raw[i])
        ini2 = _initial(raw[i + 1])
        fin2 = raw[i + 1][len(ini2):]
        if ini1 and (ini1 + fin2) in valid:
            keys.add(blur[:i] + (_blur(ini1 + fin2),) + blur[i + 2:])
        if ini2 == "":
            keys.add(blur[:i] + (blur[i],) + blur[i + 2:])
    keys.discard(blur)
    return frozenset(keys)


def _is_common_word_span(span: str) -> bool:
    """True when `span` is a common lexical word, 2+ char single-char repetition, or dominated by basic function chars."""
    if len(span) >= 2 and len(set(span)) == 1:
        return True
    if span in phrase_words():
        return True
    if len(span) == 2 and any(ch in COMMON_CHARS for ch in span):
        return True
    return len(span) >= 3 and sum(ch in COMMON_CHARS for ch in span) >= 2


def _written_positions(run: str, words: Iterable[str]) -> dict[str, set[int]]:
    """Map each 2+ Han written word present in `run` to all character indices it covers."""
    positions: dict[str, set[int]] = {}
    for w_han in words:
        if len(w_han) < 2 or w_han in positions:
            continue
        start = 0
        hits: set[int] = set()
        while (pos := run.find(w_han, start)) != -1:
            hits.update(range(pos, pos + len(w_han)))
            start = pos + 1
        if hits:
            positions[w_han] = hits
    return positions


def _covered_indices(
    positions: dict[str, set[int]],
    written_owners: Sequence[tuple[str, str]],
    owner_name: str,
    han_len: int,
    extra_han: str = "",
) -> set[int]:
    """Indices in a run covered by written spellings, excluding shorter aliases of `owner_name` itself."""
    covered: set[int] = set()
    for w_han, w_owner in written_owners:
        if w_owner == owner_name and len(w_han) < han_len:
            continue
        if w_han in positions:
            covered.update(positions[w_han])
    if extra_han and extra_han in positions:
        covered.update(positions[extra_han])
    return covered


def near_name_candidates(text: str, names: Sequence[tuple[str, tuple[str, ...]]]) -> list[NameCandidate]:
    """Candidate records for `near_names`, carrying source (`table`/`participant`/`roster`/`shared`) and weakness."""
    hits: list[NameCandidate] = []
    seen_hits: set[str] = set()
    weak_set = names.weak_spellings if isinstance(names, NameEntries) else set()
    spellings = _spellings(names)
    written_owners = list(
        dict.fromkeys(
            (w_han, owner)
            for s, owner in spellings
            if s in text and len(w_han := "".join(_HAN.findall(s))) >= 2
        )
    )
    written_words = {w_han for w_han, _ in written_owners}
    raw_runs = list(dict.fromkeys((*_HAN.findall(text), *_HAN.findall(_MARKS.sub("", text)))))
    runs: list[tuple[str, list[str]]] = []
    for run in raw_runs:
        got = _pinyin(run)
        if len(got) == len(run):
            runs.append((run, got))
    run_positions = [(_written_positions(run, written_words) if written_words else {}) for run, _ in runs]

    def _record(name: str, is_weak_spelling: bool) -> None:
        seen_hits.add(name)
        src = entry_source(names, name)
        hits.append({"name": name, "source": src, "weak": is_weak_spelling or src in ("roster", "shared")})

    for spelling, name in spellings:
        if name in seen_hits:
            continue
        is_weak = (name, spelling) in weak_set
        if _spelled_out(text, spelling):
            _record(name, is_weak)
            continue
        han = "".join(_HAN.findall(spelling))
        if len(han) < 2:
            continue
        want = _pinyin(han)
        if len(want) != len(han):
            continue
        for (run, got), positions in zip(runs, run_positions, strict=True):   # a name may run across 《》 or quotes
            covered = _covered_indices(positions, written_owners, name, len(han)) if positions else set()
            for i in range(len(run) - len(han) + 1):
                span = run[i:i + len(han)]
                if span == han or got[i:i + len(han)] != want:
                    continue
                if covered and any(pos in covered for pos in range(i, i + len(han))):
                    continue
                if is_weak and _is_common_word_span(span):
                    continue
                _record(name, is_weak)
                break
            if name in seen_hits:
                break
    for spelling, name in spellings:
        if name in seen_hits:
            continue
        han = _single_han(spelling)
        if len(han) < 3:
            continue
        keys = _drop_fuse_keys(han)
        if not keys:
            continue
        is_weak = (name, spelling) in weak_set
        width = len(han) - 1
        for (run, got), positions in zip(runs, run_positions, strict=True):
            if len(run) < width:
                continue
            pos_map = positions
            if han in run and han not in pos_map:
                pos_map = {**positions, **_written_positions(run, (han,))}
            cov = _covered_indices(pos_map, written_owners, name, len(han), extra_han=han) if pos_map else set()
            for i in range(len(run) - width + 1):
                if tuple(got[i:i + width]) not in keys:
                    continue
                if cov and any(pos in cov for pos in range(i, i + width)):
                    continue
                span = run[i:i + width]
                if _is_common_word_span(span):
                    continue
                _record(name, is_weak)
                break
            if name in seen_hits:
                break
    return hits


def near_names(text: str, names: Sequence[tuple[str, tuple[str, ...]]]) -> list[str]:
    """Names whose Chinese part (two or more characters) the sentence says in other characters, 3+ syllable Chinese
    names with one dropped or two adjacent fused syllables, and Latin-only names sounded out in Chinese characters."""
    return [c["name"] for c in near_name_candidates(text, names)]


_PINYIN_INITIALS = ("zh", "ch", "sh", "b", "p", "m", "f", "d", "t", "n", "l", "g", "k", "h", "j", "q", "x", "r", "z",
                    "c", "s", "y", "w")
_PART = re.compile(r"[一-鿿]+|[A-Za-z]+")


def _initial(syllable: str) -> str:
    return next((i for i in _PINYIN_INITIALS if syllable.startswith(i)), "")


def _loosely(got: Sequence[str], want: Sequence[str]) -> bool:
    """Two blurred syllable runs that a recogniser can confuse for a name: every initial the same and the first
    syllable the same (礼墨 li-mo heard as 你们 ni-men: n/l one sound, m kept)."""
    return (len(got) == len(want) > 0 and got[0] == want[0]
            and all(_initial(a) == _initial(b) for a, b in zip(got, want)))


def near_full_names(text: str, names: Sequence[tuple[str, tuple[str, ...]]]) -> list[str]:
    """Names written as a Chinese part next to a Latin part (礼墨Sumi) that the sentence says with the Chinese part in
    other, loosely alike characters right beside the Latin part, or beside a character whose syllable begins the Latin
    part (你们苏菲, 你们Sumi).  The Latin neighbour is what ties the run to the name: the loose sound alone (你们) is an
    ordinary word."""
    hits: list[str] = []
    for spelling, name in _spellings(names):
        parts = _PART.findall(spelling)
        if name in hits or spelling in text or len(parts) != 2 or {bool(_HAN.fullmatch(p)) for p in parts} != {True, False}:
            continue
        han, latin = (parts[0], parts[1]) if _HAN.fullmatch(parts[0]) else (parts[1], parts[0])
        han_first = parts[0] == han
        want = _pinyin(han)
        if len(han) < 2 or len(want) != len(han):
            continue
        for i in range(len(text) - len(han) + 1):
            window = text[i:i + len(han)]
            got = _pinyin(window) if _HAN.fullmatch(window) else []
            if window == han or len(got) != len(han) or not _loosely(got, want):
                continue
            beside = text[i + len(han):].lstrip() if han_first else text[:i].rstrip()
            if han_first:
                tie = beside[:len(latin)].lower() == latin.lower() or (
                    bool(beside) and _HAN.fullmatch(beside[0]) is not None
                    and latin.lower().startswith(((text_pinyin_tokens(beside[0]) or [""])[0]).lower() or "\0"))
            else:
                tie = beside[-len(latin):].lower() == latin.lower()
            if tie:
                hits.append(name)
                break
    return hits


def _latin_known(names: Sequence[tuple[str, tuple[str, ...]]]) -> set[str]:
    base = names.primary_rows if isinstance(names, NameEntries) else names
    return {w.lower() for s, _ in _spellings(base) for w in _LATIN.findall(s)}


def unknown_latin(text: str, names: Sequence[tuple[str, tuple[str, ...]]]) -> list[str]:
    """Latin-letter words of the sentence that could be an unresolved name: at least lane_rules detection
    latin_min_letters letters, no name or alias of the table, not a common English short word (latin_common_words;
    TASK-29 ruling 6: ok, no, get, my, oh ... were false doubts)."""
    cfg = rules("detection")
    least = int(cfg.get("latin_min_letters") or 1)
    skip = _latin_known(names) | {str(w).lower() for w in cfg.get("latin_common_words") or []}
    return sorted({w for w in _LATIN.findall(text)
                   if sum(ch.isalpha() for ch in w) >= least and w.lower() not in skip})


def unknown_spellings(texts: Iterable[str], names: Sequence[tuple[str, tuple[str, ...]]],
                      sources: Iterable[str]) -> list[UnresolvedSpelling]:
    """Latin-letter spellings in the subtitles that the table, the danmaku and the title (`sources`) do not carry:
    [{"spelling", "count"}] (the 未定专名 list for PROCESS; a spelling must exist in a written source, §5.4)."""
    pool = " ".join(sources).lower()
    counts: dict[str, int] = {}
    for t in texts:
        for w in unknown_latin(t, names):
            if w.lower() not in pool:
                counts[w] = counts.get(w, 0) + 1
    return [{"spelling": w, "count": n} for w, n in sorted(counts.items())]


def unknown_line(texts: Iterable[str], participants: Iterable[Sequence[str]], sources: Iterable[str],
                 extra: Iterable[UnresolvedSpelling] = ()) -> str:
    """The PROCESS line of the 未定专名 list (COACH e05 §5.4: a spelling must exist in a written source).  `extra` are
    rows another producer could not settle (TASK-36: the name-consistency groups CPA left open, each spelling with how
    often it stands and why), shown in the same list so the reader has one place to look."""
    rows = unknown_spellings(texts, entries(participants), sources)
    seen = {r["spelling"] for r in rows}
    rows += [cast(UnresolvedSpelling, {**dict(r), "count": r.get("count")})
             for r in extra if str(r.get("spelling")) not in seen]
    return ("未定专名（字母写法在名字表、弹幕和选题钩子里都没有，e05 §5.4；只记录）："
            + ("、".join(f"{r['spelling']}×{r['count']}" for r in rows) if rows else "无"))


def detection_reasons(text: str, names: Sequence[tuple[str, tuple[str, ...]]], cfg: DetectionRules) -> list[str]:
    """The name doubts of one sentence (lane_rules detection.near_name, detection.latin_unknown)."""
    out = []
    if cfg.get("near_name"):
        seen_near = set()
        for c in near_name_candidates(text, names):
            n, src = c["name"], c["source"]
            seen_near.add(n)
            if src in ("roster", "shared"):
                out.append(f"近音候选（{src}）：本句可能提到「{n}」，请结合音频和语境判断")
            else:
                out.append(f"近音名字：本句可能说的是名字表里的「{n}」（拼音相近、写法不同）")
        for n in near_full_names(text, names):
            if n in seen_near:
                continue
            src = entry_source(names, n)
            if src in ("roster", "shared"):
                out.append(f"近音候选（{src}）：本句可能提到全名「{n}」，请结合音频和语境判断")
            else:
                out.append(f"近音全名：本句紧挨着名字的字母部分，前面几个字和「{n}」的汉字部分声母相同，可能说的是全名「{n}」")
    if cfg.get("latin_unknown"):
        latin = unknown_latin(text, names)
        if latin:
            out.append("含拉丁字母「" + "、".join(latin) + "」，名字表里查不到")
    return out
