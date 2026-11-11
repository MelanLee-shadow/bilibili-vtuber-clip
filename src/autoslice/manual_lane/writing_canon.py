"""One word, one written form: the proper-noun table's `canonical` writing (维护者 / 11-09).

assets/manual_lane/proper_nouns.json gives each word a `canonical` writing, the other writings of the SAME word in
`variants`, and a `condition` (when a text is that word).  `aliases` are other names of the same object and are never
a writing.  `variants` may still hold such a second name, so a hit is rewritten only when it is the same word:

- `find` (deterministic, no model): the non-canonical `variants` written in a text, the longest first and without
  overlap; a place that is itself part of a canonical writing is not a hit;
- a hit that differs from the canonical writing only in letter case is rewritten without asking;
- every other hit is put to the model once per call (purpose `writing-canon`, prompts/writing_canon.md): is this the
  word, written another way (rewrite), or another name / another word (keep);
- every verdict is recorded in subs/writing_canon.json.  Step 3 writes the subtitle verdicts, step 6 adds the title's
  and the cover text's (a writing the subtitles already decided keeps that verdict), step 8 refuses a package whose
  final subtitles, speaker subtitles, title or cover text still hold a hit that has no `kept` verdict
  (WRITING_NOT_CANONICAL).  A failed call stops the step (WRITING_CANON_FAILED).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

from . import normalize
from .name_match import PROPER_NOUNS
from .runner import Ctx, StepOutcome
from .srt_text import _json_from_text
from .step_io import _ask_cpa, _cpa_ok, _fail, _log, _render

if TYPE_CHECKING:
    from .state import CpaEvent

PURPOSE = "writing-canon"
FILE = "subs/writing_canon.json"
SCHEMA = "writing-canon.v1"
CONTEXT = 2                       # sentences shown before and after the one holding the hit
SUBTITLE, TITLE, COVER, APPROVED = "subtitle", "title", "cover", "approved_title"
KNOWN_WHERE = (SUBTITLE, APPROVED)    # verdicts a title or cover text takes over instead of asking again


class _RawRow(TypedDict, total=False):
    name: str
    canonical: str
    variants: list[str]
    condition: str


_PromptRow = TypedDict("_PromptRow", {"id": int, "位置": str, "前两句": list[str], "这一句": str, "后两句": list[str],
                                      "写法": str, "规范写法": str, "条件": str})


class _Answer(TypedDict, total=False):
    id: object
    same_word: object
    why: object


class Conclusion(TypedDict):
    """The verdict on one hit, as stored in subs/writing_canon.json."""
    where: str            # subtitle | title | cover | approved_title (the owner's picked title body)
    cue: int | None       # final.srt number (subtitle), cover line number (cover); None for a title
    row: str              # the table row
    written: str          # the writing found
    canonical: str
    verdict: str          # rewritten | kept
    basis: str            # case_only | model
    why: str
    sentence: str         # the text as it stands after this step (the check matches kept verdicts on it)
    reused: NotRequired[bool]            # taken over from an earlier verdict on the same writing
    inherited: NotRequired[bool]         # taken over from the clip this one was cut from


@dataclass(frozen=True)
class Hit:
    row: str
    written: str
    canonical: str
    start: int
    end: int

    @property
    def case_only(self) -> bool:
        return self.written.lower() == self.canonical.lower()


@dataclass(frozen=True)
class Unit:
    """One text to settle: a subtitle sentence, the title, or a cover line."""
    where: str
    number: int | None
    text: str


@dataclass(frozen=True)
class _Row:
    name: str
    canonical: str
    variants: tuple[str, ...]
    condition: str


class WritingCanonFailed(Exception):
    """The model could not be had, or never answered in the contract.  `cpa` holds the records of the calls."""

    def __init__(self, detail: str, cpa: Sequence[CpaEvent] = ()) -> None:
        super().__init__(detail)
        self.detail = detail
        self.cpa = list(cpa)


@dataclass
class Resolved:
    texts: list[str]
    conclusions: list[Conclusion]
    cpa: list[CpaEvent] = field(default_factory=list)


# ----- the table and the deterministic scan ---------------------------------------------------------------------
@lru_cache(maxsize=1)
def _table() -> tuple[_Row, ...]:
    rows = cast("list[_RawRow]", json.loads(PROPER_NOUNS.read_text(encoding="utf-8")))
    return tuple(_Row(r["name"], r["canonical"], tuple(r.get("variants") or ()), r.get("condition") or "")
                 for r in rows)


@lru_cache(maxsize=1)
def _canonicals() -> tuple[str, ...]:
    return tuple(sorted({r.canonical for r in _table() if r.canonical}, key=len, reverse=True))


def condition_of(row: str) -> str:
    return next((r.condition for r in _table() if r.name == row), "")


def _word_char(ch: str) -> bool:
    return bool(ch) and ch.isascii() and ch.isalnum()


def _inside_canonical(text: str, written: str, start: int) -> bool:
    """The place is part of a longer canonical writing that holds this writing at this very offset."""
    for canonical in _canonicals():
        if len(canonical) <= len(written) or written not in canonical:
            continue
        offset = canonical.find(written)
        while offset != -1:
            if start >= offset and text[start - offset:start - offset + len(canonical)] == canonical:
                return True
            offset = canonical.find(written, offset + 1)
    return False


def find(text: str) -> list[Hit]:
    """The non-canonical writings of the table's rows in `text`, in text order.  Longer writings win and hits never
    overlap; an ASCII writing is matched as a whole word; a place inside a canonical writing is no hit; `aliases` and
    `rejected_variants` are not scanned."""
    found: list[Hit] = []
    for row in _table():
        for written in dict.fromkeys(v for v in row.variants if v and v != row.canonical):
            for match in re.finditer(re.escape(written), text):
                start, end = match.span()
                if _word_char(written[0]) and start and _word_char(text[start - 1]):
                    continue
                if _word_char(written[-1]) and end < len(text) and _word_char(text[end]):
                    continue
                if not _inside_canonical(text, written, start):
                    found.append(Hit(row.name, written, row.canonical, start, end))
    taken: list[Hit] = []
    for hit in sorted(found, key=lambda h: (-(h.end - h.start), h.start)):
        if all(hit.end <= t.start or hit.start >= t.end for t in taken):
            taken.append(hit)
    return sorted(taken, key=lambda h: h.start)


def canonical_form(text: str) -> str:
    """`text` with every hit written canonically, no judgement: the key two texts are compared on."""
    for hit in reversed(find(text)):
        text = text[:hit.start] + hit.canonical + text[hit.end:]
    return text


def _flat(text: str) -> str:
    return " ".join(text.split())


_BREAK = re.compile(r"[ \t]*\n[ \t]*")


def join_lines(text: str) -> str:
    """A sentence on one line: a line break between two ASCII letters/digits becomes one space, any other break is
    removed (no space is put between Chinese characters).  Writings are found on this text, not on the display lines."""
    return _BREAK.sub(lambda m: " " if _word_char(text[m.start() - 1:m.start()]) and _word_char(text[m.end():m.end() + 1])
                      else "", text)


# ----- the model's verdicts -------------------------------------------------------------------------------------
class _Pending(TypedDict):
    unit: int
    hit: Hit


def _accepts(ids: set[int]) -> Callable[[object], str]:
    def check(value: object) -> str:
        if not isinstance(value, list) or not all(isinstance(r, dict) for r in value):
            return "WRITING_CANON_ANSWER_INVALID: not a list of objects"
        rows = cast("list[_Answer]", value)
        got = [r.get("id") for r in rows]
        if len(set(map(str, got))) != len(got) or {g for g in got if isinstance(g, int)} != ids:
            return "WRITING_CANON_ANSWER_INVALID: ids do not match the asked ones"
        if not all(isinstance(r.get("same_word"), bool) for r in rows):
            return "WRITING_CANON_ANSWER_INVALID: same_word is not true/false"
        return ""
    return check


def _prompt_rows(units: Sequence[Unit], pending: Sequence[_Pending]) -> str:
    out: list[_PromptRow] = []
    for number, item in enumerate(pending, 1):
        unit = units[item["unit"]]
        group = [u for u in units if u.where == unit.where]
        at = group.index(unit)
        out.append({"id": number, "位置": {SUBTITLE: "字幕句", TITLE: "标题", COVER: "封面字", APPROVED: "标题正文"}[unit.where]
                    + (f" {unit.number}" if unit.number is not None else ""),
                    "前两句": [_flat(u.text) for u in group[max(0, at - CONTEXT):at]],
                    "这一句": _flat(unit.text),
                    "后两句": [_flat(u.text) for u in group[at + 1:at + 1 + CONTEXT]],
                    "写法": item["hit"].written, "规范写法": item["hit"].canonical,
                    "条件": condition_of(item["hit"].row)})
    return json.dumps(out, ensure_ascii=False, indent=1)


def _ask(ctx: Ctx, call_id: str, units: Sequence[Unit], pending: Sequence[_Pending]
         ) -> tuple[dict[int, tuple[bool, str]], CpaEvent]:
    record = _ask_cpa(ctx, call_id, _render("writing_canon.md", rows=_prompt_rows(units, pending)), purpose=PURPOSE,
                      accepts=_accepts(set(range(1, len(pending) + 1))))
    if not _cpa_ok(record):
        raise WritingCanonFailed(record.get("stderr_tail", "") or "no answer", [_log(record)])
    try:
        rows = cast("list[_Answer]", _json_from_text(record["response_text"]))
        verdicts = {cast(int, r["id"]): (cast(bool, r["same_word"]), str(r.get("why") or "").strip()) for r in rows}
        if set(verdicts) != set(range(1, len(pending) + 1)):
            raise ValueError("ids")
    except (ValueError, TypeError, KeyError):
        Path(str(record.get("cache_path", ""))).unlink(missing_ok=True)
        raise WritingCanonFailed(f"the answer cannot be read: {record.get('file')}", [_log(record)]) from None
    return verdicts, _log(record)


def resolve(ctx: Ctx, units: Sequence[Unit], *, call_id: str, known: Sequence[Conclusion] = ()) -> Resolved:
    """Every hit of every unit settled: case-only hits and hits whose writing `known` already decided (one verdict for
    the writing, no disagreement) without a call, the rest in one call.  Returns the texts as they are to stand and
    the verdicts.  Raises WritingCanonFailed when the call fails or its answer never fits."""
    taken: dict[tuple[str, str], set[str]] = {}
    for c in known:
        taken.setdefault((c["row"], c["written"]), set()).add(c["verdict"])
    prior = {(c["row"], c["written"]): c for c in known}
    hits = [find(u.text) for u in units]
    pending = [_Pending(unit=i, hit=h) for i, found in enumerate(hits) for h in found
               if not h.case_only and len(taken.get((h.row, h.written), ())) != 1]
    answers: dict[int, tuple[bool, str]] = {}
    cpa: list[CpaEvent] = []
    if pending:
        answers, event = _ask(ctx, call_id, units, pending)
        cpa.append(event)
    asked = {(p["unit"], p["hit"].start): n for n, p in enumerate(pending, 1)}
    texts: list[str] = []
    drafts: list[tuple[int, Hit, str, str, str, bool]] = []
    for i, unit in enumerate(units):
        text = unit.text
        for hit in reversed(hits[i]):
            if hit.case_only:
                verdict, basis, why, reused = "rewritten", "case_only", "只差大小写", False
            elif (i, hit.start) in asked:
                same, why = answers[asked[(i, hit.start)]]
                verdict, basis, reused = ("rewritten" if same else "kept"), "model", False
            else:
                old = prior[(hit.row, hit.written)]
                verdict, basis, why, reused = old["verdict"], old["basis"], old["why"], True
            drafts.append((i, hit, verdict, basis, why, reused))
            if verdict == "rewritten":
                text = text[:hit.start] + hit.canonical + text[hit.end:]
        texts.append(text)
    conclusions: list[Conclusion] = []
    for i, hit, verdict, basis, why, reused in sorted(drafts, key=lambda d: (d[0], d[1].start)):
        unit = units[i]
        row: Conclusion = {"where": unit.where, "cue": unit.number, "row": hit.row, "written": hit.written,
                           "canonical": hit.canonical, "verdict": verdict, "basis": basis, "why": why,
                           "sentence": _flat(texts[i])}
        if reused:
            row["reused"] = True
        conclusions.append(row)
    return Resolved(texts, conclusions, cpa)


# ----- the record file ------------------------------------------------------------------------------------------
def load(clip_dir: Path) -> list[Conclusion]:
    path = clip_dir / FILE
    if not path.is_file():
        return []
    return cast("list[Conclusion]", json.loads(path.read_text(encoding="utf-8"))["conclusions"])


def save(clip_dir: Path, conclusions: Sequence[Conclusion]) -> Path:
    path = clip_dir / FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": SCHEMA, "conclusions": list(conclusions)}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def _replace(clip_dir: Path, wheres: set[str], new: Sequence[Conclusion]) -> None:
    save(clip_dir, [*(c for c in load(clip_dir) if c["where"] not in wheres), *new])


def process_line(conclusions: Sequence[Conclusion], *, note: str = "") -> str:
    """PROCESS: `规范写法：改 N 处（原写法→规范形 及句号），保留 M 处`."""
    label = {SUBTITLE: "句", COVER: "封面第", TITLE: "标题", APPROVED: "选定标题"}
    changed: dict[str, list[str]] = {}
    for c in conclusions:
        if c["verdict"] == "rewritten":
            where = label[c["where"]] + (str(c["cue"]) if c["cue"] is not None else "")
            changed.setdefault(f"{c['written']}→{c['canonical']}", []).append(where)
    n_changed = sum(len(v) for v in changed.values())
    kept = sum(1 for c in conclusions if c["verdict"] == "kept")
    listed = "；".join(f"{pair}（{'、'.join(where)}）" for pair, where in changed.items())
    return f"规范写法{note}：改 {n_changed} 处" + (f"（{listed}）" if listed else "") + f"，保留 {kept} 处"


# ----- step 3 ---------------------------------------------------------------------------------------------------
def _inherited(ctx: Ctx, cues: Sequence[str]) -> list[Conclusion]:
    """The verdicts of the clip this one was cut from (import/writing_canon.json), for the sentences this clip still
    holds word for word, numbered by this clip's own sentences."""
    path = ctx.clip_dir / "import" / "writing_canon.json"
    if not path.is_file():
        return []
    where: dict[str, list[int]] = {}
    for number, text in enumerate(cues, 1):
        where.setdefault(_flat(text), []).append(number)
    out: list[Conclusion] = []
    for c in cast("list[Conclusion]", json.loads(path.read_text(encoding="utf-8"))["conclusions"]):
        free = where.get(c["sentence"]) if c["where"] == SUBTITLE else None
        if free:
            out.append(cast(Conclusion, dict(c, cue=free.pop(0), inherited=True)))
    return out


def _wrap(text: str, number: int) -> list[str]:
    """A rewritten sentence on display lines again, within the lane's own limits (normalize.wrap_lines)."""
    audit = normalize._audit()
    max_lines, max_chars = audit.DEFAULT_MAX_VISUAL_LINES, audit.DEFAULT_MAX_VISUAL_LINE_CHARS
    lines = normalize.wrap_lines(text, max_lines, max_chars)
    if len(lines) > max_lines or any(len(line) > max_chars for line in lines):
        raise WritingCanonFailed(f"句{number} 改成规范写法后放不进 {max_lines} 行 × {max_chars} 字：{text}")
    return lines


def settle_step3(ctx: Ctx, outcome: StepOutcome, *, verbatim: bool) -> StepOutcome:
    """Step 3's decided subtitles (subs/final.srt) written canonically.  Only the words change: the sentences, their
    times and speakers stay.  A `verbatim` push (a clip cut out of a finished one) is already canonical and is not
    asked again: it takes over the verdicts of the clip it was cut from."""
    from . import subtitle_track as st

    if not outcome.ok:
        return outcome
    path = ctx.clip_dir / "subs/final.srt"
    cues = st.parse_blocks(path.read_text(encoding="utf-8"))
    joined = [join_lines(str(c["text"])) for c in cues]
    if verbatim:
        done = _inherited(ctx, joined)
        note = "（沿用原片结论）"
    else:
        try:
            res = resolve(ctx, [Unit(SUBTITLE, n, t) for n, t in enumerate(joined, 1)], call_id=PURPOSE)
        except WritingCanonFailed as exc:
            return failed(exc, outcome.cpa, outcome.process)
        changed = [n for n, (was, now) in enumerate(zip(joined, res.texts, strict=True), 1) if was != now]
        try:
            wrapped = {n: _wrap(res.texts[n - 1], n) for n in changed}
        except WritingCanonFailed as exc:
            return failed(exc, [*outcome.cpa, *res.cpa], outcome.process)
        done, note = res.conclusions, ""
        if changed:
            for n, lines in wrapped.items():      # only a rewritten sentence is wrapped again; the others keep their lines
                cues[n - 1]["text"] = "\n".join(lines)
            path.write_text(st.format_blocks(cues, labels=True), encoding="utf-8")
        outcome.cpa.extend(res.cpa)
    outcome.products["writing_canon"] = str(save(ctx.clip_dir, done))
    outcome.process.append(process_line(done, note=note))
    return outcome


# ----- step 6 ---------------------------------------------------------------------------------------------------
def _settle_units(ctx: Ctx, units: Sequence[Unit], call_id: str, cpa_calls: list[CpaEvent]) -> Resolved:
    known = [c for c in load(ctx.clip_dir) if c["where"] in KNOWN_WHERE]
    res = resolve(ctx, units, call_id=call_id, known=known)
    cpa_calls.extend(res.cpa)
    _replace(ctx.clip_dir, {u.where for u in units}, res.conclusions)
    return res


def settle_approved(ctx: Ctx, body: str, cpa_calls: list[CpaEvent]) -> str:
    """The title body the owner picked, written canonically."""
    return _settle_units(ctx, [Unit(APPROVED, None, body)], "writing-canon-approved", cpa_calls).texts[0]


def settle_title_cover(ctx: Ctx, title: str, cover_lines: Sequence[str], cpa_calls: list[CpaEvent]
                       ) -> tuple[str, list[str], str]:
    """The adopted title and cover text written canonically (a writing the subtitles already decided keeps that
    verdict).  Returns (title, cover lines, PROCESS line).  Raises WritingCanonFailed."""
    res = _settle_units(ctx, [Unit(TITLE, None, title), *(Unit(COVER, n, t) for n, t in enumerate(cover_lines, 1))],
                        "writing-canon-title", cpa_calls)
    return res.texts[0], res.texts[1:], process_line(res.conclusions, note="（标题与封面字）")


def settle_cover(ctx: Ctx, cover_lines: Sequence[str], cpa_calls: list[CpaEvent], *, call_id: str
                 ) -> tuple[list[str], str]:
    """Cover text that was written again (shortened), written canonically.  Returns (lines, PROCESS line)."""
    res = _settle_units(ctx, [Unit(COVER, n, t) for n, t in enumerate(cover_lines, 1)], call_id, cpa_calls)
    return res.texts, process_line(res.conclusions, note="（封面字）")


def failed(exc: WritingCanonFailed, cpa_calls: Sequence[CpaEvent], process: Sequence[str] = ()) -> StepOutcome:
    return _fail("WRITING_CANON_FAILED", exc.detail, cpa=[*cpa_calls, *exc.cpa], process=process)


# ----- step 8 ---------------------------------------------------------------------------------------------------
def not_canonical(ctx: Ctx) -> str:
    """"" when the final subtitles, the speaker subtitles, the title and the cover text hold no hit that lacks a
    `kept` verdict; else the places and writings, for WRITING_NOT_CANONICAL."""
    from . import subtitle_track as st

    clip = ctx.clip_dir
    kept = Counter((c["where"], c["sentence"], c["written"]) for c in load(clip) if c["verdict"] == "kept")
    texts: list[tuple[str, str, str]] = []     # (place label, where, text)
    for name in ("subs/final.srt", "speaker/speaker.srt"):
        texts += [(f"{name} 句{n}", SUBTITLE, join_lines(str(c["text"])))
                  for n, c in enumerate(st.parse_blocks((clip / name).read_text(encoding="utf-8")), 1)]
    texts.append(("标题", TITLE, (clip / "title/title.txt").read_text(encoding="utf-8").strip()))
    candidates = json.loads((clip / "title/candidates.json").read_text(encoding="utf-8"))
    texts += [(f"封面字第{n}行", COVER, str(t)) for n, t in enumerate(candidates.get("cover_lines") or [], 1)]
    left: list[str] = []
    used: Counter[tuple[str, str, str, str]] = Counter()
    for place, where, text in texts:
        for hit in find(text):
            key = (where, _flat(text), hit.written)
            used[(place.split(" ")[0], *key)] += 1
            if used[(place.split(" ")[0], *key)] > kept[key]:
                left.append(f"{place}：{hit.written}（应为 {hit.canonical}）")
    return "；".join(left)[:600]
