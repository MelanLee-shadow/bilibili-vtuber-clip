"""Score a clip's final subtitles against 维护者's truth labels (train/test scoring only; no rule lives here).

Alignment is by time: a truth window takes every pipeline cue that covers at least half of its own duration,
concatenates their texts in order, and takes the speaker of the largest overlap.  Pipeline cues that no truth
window claims are listed as uncovered.  Times: final.srt is cut-relative; truth is source-absolute; the offset is
boundary/decision.json adopted.start_ms.

The first measure for adopting a change is `cer_whole` (TASK-17): the whole output against the whole truth, every
output cue counted, so an inserted or repeated sentence costs as much as a missing one.  The per-sentence `exact` and
`exact_flat` only ask whether each truth sentence appears in the output and do not see insertions.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast
from . import channel

if TYPE_CHECKING:
    from .normalize import LoneChar
    from .overlap_join import OverlapInfo
    from .srt_text import TimedText
    from .subtitle_track import TrackCue

HOST = channel.HOST_LABEL
_LAUGH_RE = re.compile(r"[（(]\s*(笑|\?|？)\s*[)）]")
_SRT_TIME = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)")
_SPEAKER_PREFIX = re.compile(r"^\[([^\]]+)\]\s*")


class ParsedScoreCue(TypedDict):
    start: int
    end: int
    text: str
    speaker: str | None


class ScoreItem(TypedDict):
    abs_start_ms: int
    abs_end_ms: int
    text: str
    speaker: NotRequired[str | None]
    id: NotRequired[str]
    cid: NotRequired[str]


class ScoreRow(TypedDict):
    id: str
    abs_start_ms: int
    abs_end_ms: int
    truth_text: str
    pipe_text: str
    truth_speaker: str | None
    pred_speaker: str | None
    pipe_cues: list[int]


class WholeScore(TypedDict, total=False):
    cer_whole: float
    edits: int
    truth_chars: int
    output_chars: int
    truth_repeats_removed: int
    truth_repeat_candidates: int
    exact: int
    n: int
    exact_flat: int


class ScoreSummary(TypedDict, total=False):
    n: int
    scorer: str
    exact: int
    exact_rate: float | None
    cer_macro: float | None
    cer_micro: float
    speaker_correct: int
    speaker_all: int
    speaker_acc_all: float | None
    speaker_decided: int
    speaker_acc_decided: float | None
    cer_flat: float
    exact_flat: int
    cer_whole: float
    whole: WholeScore
    lone_chars: dict[str, list[str]]


class ScoreResult(TypedDict):
    cid: str
    offset_ms: int
    rows: list[ScoreRow]
    uncovered: list[ScoreItem]
    summary: ScoreSummary















SCORER_VERSIONS = ("v1", "v2", "v3", "v4")
DEFAULT_SCORER = "v3"
_NOTE_RE = re.compile(r"[（(][^（()）]{0,12}[)）]")
_LAUGH_SOUND_RE = re.compile(r"[哈呵嘿嘻]{2,}")
_NUMERALS = str.maketrans("〇零一二三四五六七八九", "00123456789")


def scorer_version() -> str:
    version = os.environ.get("MANUAL_LANE_SCORER", DEFAULT_SCORER)
    if version not in SCORER_VERSIONS:
        raise ValueError(f"MANUAL_LANE_SCORER must be one of {SCORER_VERSIONS}, got {version!r}")
    return version


def normalize(text: str) -> str:
    """Scoring normalisation.  v1: drop （笑）/（?）, NFKC (full-width -> half-width), drop punctuation/space/symbols.
    v2 additionally drops every short bracketed note and laughter written as a sound, and writes numerals as digits.
    v3 is v2 with the filler characters (doubt_policy.FILLERS) removed."""
    version = scorer_version()
    if version in ("v2", "v3", "v4"):
        text = _NOTE_RE.sub("", unicodedata.normalize("NFKC", text).replace("(", "（").replace(")", "）"))
        text = _LAUGH_SOUND_RE.sub("", text).translate(_NUMERALS)
    text = _LAUGH_RE.sub("", text)
    text = unicodedata.normalize("NFKC", text)
    if version in ("v3", "v4"):
        from .doubt_policy import FILLERS

        text = "".join(ch for ch in text if ch not in FILLERS)
    return "".join(ch for ch in text if unicodedata.category(ch)[0] not in "PZSC")


def edit_distance(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _ms(h: str, m: str, s: str, frac: str) -> int:
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(frac.ljust(3, "0")[:3])


def parse_srt(path: Path) -> list[ParsedScoreCue]:
    """Return [{start, end, text, speaker}] in cut-relative ms; a [名字] prefix becomes the speaker."""
    out: list[ParsedScoreCue] = []
    for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8").strip()):
        lines = [ln.rstrip() for ln in block.strip().splitlines()]
        if len(lines) < 2 or "-->" not in lines[1]:
            continue
        a, z = [x.strip() for x in lines[1].split("-->")]
        ta, tz = _SRT_TIME.match(a), _SRT_TIME.match(z)
        if not ta or not tz:
            continue
        text = " ".join(x.strip() for x in lines[2:] if x.strip())
        speaker = None
        m = _SPEAKER_PREFIX.match(text)
        if m:
            speaker, text = m.group(1), text[m.end():]
        out.append({"start": _ms(*ta.groups()), "end": _ms(*tz.groups()), "text": text, "speaker": speaker})
    return out


def _overlap(a0: int, a1: int, b0: int, b1: int) -> int:
    return max(0, min(a1, b1) - max(a0, b0))


def align(truth: Sequence[ScoreItem], pipe: Sequence[ScoreItem],
          speaker_of: Mapping[str, str | None] | None = None) -> tuple[list[ScoreRow], list[ScoreItem]]:
    """truth: [{id,abs_start_ms,abs_end_ms,text,speaker}] ; pipe: [{abs_start_ms,abs_end_ms,text,speaker}].

    Returns (rows per truth cue, uncovered pipeline cues).  speaker_of maps a truth id to a predicted speaker
    overriding the pipeline cue speaker (used for --speaker-from).
    """
    claimed: set[int] = set()
    rows: list[ScoreRow] = []
    owner: dict[int, int] = {}   # v2: pipeline index -> index of the truth window it overlaps most
    if scorer_version() in ("v2", "v3", "v4"):
        for i, p in enumerate(pipe):
            p0, p1 = int(p["abs_start_ms"]), int(p["abs_end_ms"])
            ovs = [(_overlap(int(t["abs_start_ms"]), int(t["abs_end_ms"]), p0, p1), -k) for k, t in enumerate(truth)]
            best = max(ovs, default=(0, 0))
            if best[0] > 0:
                owner[i] = -best[1]
    for k, t in enumerate(truth):
        t0, t1 = int(t["abs_start_ms"]), int(t["abs_end_ms"])
        hits: list[tuple[int, int]] = []  # (overlap, pipe index)
        for i, p in enumerate(pipe):
            p0, p1 = int(p["abs_start_ms"]), int(p["abs_end_ms"])
            ov = _overlap(t0, t1, p0, p1)
            mine = owner.get(i) == k if scorer_version() in ("v2", "v3", "v4") else (ov > 0 and ov >= 0.5 * max(1, p1 - p0))
            if mine:
                hits.append((ov, i))
                claimed.add(i)
        hits.sort(key=lambda h: pipe[h[1]]["abs_start_ms"])
        text = "".join(pipe[i]["text"] for _, i in hits)
        if hits:
            best_hit = max(hits, key=lambda h: h[0])[1]
            pred = pipe[best_hit].get("speaker")
        else:
            pred = None
        if speaker_of is not None and t["id"] in speaker_of:
            pred = speaker_of[t["id"]]
        rows.append({"id": t["id"], "abs_start_ms": t0, "abs_end_ms": t1, "truth_text": t["text"],
                     "pipe_text": text, "truth_speaker": t.get("speaker"), "pred_speaker": pred,
                     "pipe_cues": [pipe[i]["abs_start_ms"] for _, i in hits]})
    uncovered = [cast(ScoreItem, dict(pipe[i])) for i in range(len(pipe)) if i not in claimed]
    return rows, uncovered


def _is_host(name: str | None) -> bool | None:
    """Binary host/guest.  None = undecided (no prediction or 未知)."""
    if name is None or name in ("", "未知"):
        return None
    return name == HOST


def score_rows(rows: Sequence[ScoreRow]) -> ScoreSummary:
    n = len(rows)
    exact = 0
    dist_sum = 0
    truth_chars = 0
    cer_list: list[float] = []
    spk_all = spk_ok = spk_dec = spk_dec_ok = 0
    for r in rows:
        tn, pn = normalize(r["truth_text"]), normalize(r["pipe_text"])
        if tn == pn:
            exact += 1
        d = edit_distance(pn, tn)
        dist_sum += d
        truth_chars += len(tn)
        cer_list.append(d / max(1, len(tn)))
        want = _is_host(r["truth_speaker"])
        got = _is_host(r["pred_speaker"])
        spk_all += 1
        if want is not None and want == got:
            spk_ok += 1
            spk_dec_ok += 1
        if got is not None:
            spk_dec += 1
    return {
        "n": n,
        "scorer": scorer_version(),
        "exact": exact,
        "exact_rate": round(exact / n, 4) if n else None,
        "cer_macro": round(sum(cer_list) / n, 4) if n else None,
        "cer_micro": round(dist_sum / max(1, truth_chars), 4),
        "speaker_correct": spk_ok,
        "speaker_all": spk_all,
        "speaker_acc_all": round(spk_ok / spk_all, 4) if spk_all else None,
        "speaker_decided": spk_dec,
        "speaker_acc_decided": round(spk_dec_ok / spk_dec, 4) if spk_dec else None,
    }


def offset_for(clip_dir: Path) -> int:
    decision = json.loads((clip_dir / "boundary" / "decision.json").read_text(encoding="utf-8"))
    return int(decision["adopted"]["start_ms"])


def _explicit_truth_cid(value: object, location: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"truth cid at {location} must be a nonempty string")
    return value


def truth_clip_id(item: ScoreItem) -> str:
    """Prepared identity wins; legacy labels use their original single-suffix convention."""
    if "cid" in item:
        return item["cid"]
    return item["id"].rsplit("-", 1)[0]


def load_truth(paths: Sequence[Path]) -> dict[str, ScoreItem]:
    """Merge prepared labels. Identity may be in each label or the single-clip file's meta.cid.

    Legacy files without either retain their ID convention. Independent note fields are preserved metadata,
    not interpreted as transcript or speaker edits by the scorer.
    """
    merged: dict[str, ScoreItem] = {}
    for path in paths:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        labels = doc["labels"]
        meta = doc.get("meta")
        file_cid = _explicit_truth_cid(meta["cid"], f"meta.cid in {path}") if isinstance(meta, Mapping) and "cid" in meta else None
        for cue_id, v in labels.items():
            if cue_id in merged:
                raise ValueError(f"duplicate truth id {cue_id} in {path}")
            item = cast(ScoreItem, dict(v, id=cue_id))
            if "cid" in item:
                row_cid = _explicit_truth_cid(item["cid"], f"label {cue_id} in {path}")
                if file_cid is not None and row_cid != file_cid:
                    raise ValueError(f"conflicting truth cid for {cue_id} in {path}")
            elif file_cid is not None:
                item["cid"] = file_cid
            merged[cue_id] = item
    return merged


def speaker_map_from_file(path: Path) -> dict[str, str | None]:
    return {k: v for k, v in json.loads(Path(path).read_text(encoding="utf-8")).items()}


def exact_flat(truth_parts: Sequence[str], flat_pipe: str) -> int:
    """Sentences right regardless of where the output cuts its lines: the whole output text is aligned to the whole
    truth text, each truth sentence's span is carried over to the output through the alignment, and the sentence counts
    when the carried-over span equals it.  (Evaluation only.)"""
    from difflib import SequenceMatcher

    flat_t = "".join(truth_parts)
    mapping = [0] * (len(flat_t) + 1)
    for op, i1, i2, j1, j2 in SequenceMatcher(None, flat_t, flat_pipe, autojunk=False).get_opcodes():
        for i in range(i1, i2 + 1):
            mapping[i] = j1 + (i - i1) if op == "equal" else (j1 if i == i1 else (j2 if i == i2 else j1 + min(i - i1, j2 - j1)))
    count, pos = 0, 0
    for part in truth_parts:
        a, b = pos, pos + len(part)
        if part and flat_pipe[mapping[a]:mapping[b]] == part:
            count += 1
        pos = b
    return count


def clip_sources(clip_dir: Path) -> dict[str, list[TimedText]]:
    """The whole-clip readings of a clip ({source: [{start_s, end_s, text}]}, cut-relative): BCUT and jianying
    (asr/*.srt) and the whole-clip MAI / MOSS witnesses (witness/*.json, status OK).  Missing files are skipped."""
    out: dict[str, list[TimedText]] = {}
    for name in ("bcut", "jianying"):
        path = clip_dir / "asr" / f"{name}.srt"
        if path.exists():
            out[name] = [{"start_s": c["start"] / 1000, "end_s": c["end"] / 1000, "text": c["text"]}
                         for c in parse_srt(path)]
    for name in ("mai", "moss"):
        path = clip_dir / "witness" / f"{name}.json"
        doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if doc.get("status") == "OK":
            out[name] = [{"start_s": s["start_ms"] / 1000, "end_s": s["end_ms"] / 1000, "text": str(s.get("text", ""))}
                         for s in doc.get("segments", [])]
    return out


def dedup_truth(items: Sequence[ScoreItem], sources: Mapping[str, Sequence[TimedText]] | None = None,
                offset_ms: int = 0) -> tuple[list[ScoreItem], list[OverlapInfo]]:
    """Truth labels written per listening window may repeat the neighbour's words at the join (the window ran into the
    next sentence), or the speaker really said them again.  The join is decided by overlap_join.join_items (U159, the
    one function the pipeline uses too): the repeat counts once (removed from the earlier sentence) only when the
    clip's whole-clip readings (`sources`, cut-relative; truth times minus `offset_ms`) say it was said once; without
    readings nothing is removed.  Texts are returned normalised, in time order.  Returns (items, log of the joins that
    had a candidate; `k` is the length removed)."""
    from .overlap_join import join_items

    timed = [cast("TimedText", dict(t, start_s=(int(t["abs_start_ms"]) - offset_ms) / 1000,
                  end_s=(int(t["abs_end_ms"]) - offset_ms) / 1000)) for t in items]
    out, log = join_items(timed, sources or {}, norm=normalize)
    return [cast(ScoreItem, {k: v for k, v in t.items() if k not in ("start_s", "end_s")}) for t in out], log


def cer_whole(truth_items: Sequence[ScoreItem], pipe: Sequence[ScoreItem],
              sources: Mapping[str, Sequence[TimedText]] | None = None, offset_ms: int = 0) -> WholeScore:
    """Whole-clip character error rate (TASK-15 PREREG 整片拼接字错率, the first measure of TASK-17): the deduplicated
    truth (dedup_truth) joined in time order against every output cue joined in time order, edit distance over the
    truth's characters.  An output cue that no truth sentence claims is counted (an insertion), so a sentence carried
    into its neighbour, or said twice, is an error here although every truth sentence still appears in the output."""
    truth, log = dedup_truth(truth_items, sources, offset_ms)
    flat_t = "".join(t["text"] for t in truth)
    ordered = sorted(pipe, key=lambda p: (int(p["abs_start_ms"]), int(p["abs_end_ms"])))
    flat_p = normalize("".join(str(p["text"]) for p in ordered))
    dist = edit_distance(flat_p, flat_t)
    return {"cer_whole": round(dist / max(1, len(flat_t)), 4), "edits": dist, "truth_chars": len(flat_t),
            "output_chars": len(flat_p), "truth_repeats_removed": sum(1 for e in log if e["k"]),
            "truth_repeat_candidates": len(log)}


def score_clip(cid: str, clips_dir: Path, truth: Mapping[str, ScoreItem],
               speaker_from: Path | None = None, evidence_dirs: Sequence[Path] = ()) -> ScoreResult:
    """`evidence_dirs`: where else to look for the clip's whole-clip readings (clip_sources, for cer_whole's truth
    joins) when `clips_dir/cid` has none (a snapshot keeps only the subtitles); the first that has them is used."""
    clip_dir = clips_dir / cid
    offset = offset_for(clip_dir)
    srt_path = clip_dir / "subs" / "final.srt"
    speaker_path = clip_dir / "speaker" / "speaker.srt"
    speaker_cues = parse_srt(speaker_path) if speaker_path.exists() else []
    pipe: list[ScoreItem] = []
    for c in parse_srt(srt_path):
        speaker = c["speaker"]
        if speaker_cues:   # the pipeline's own speaker output: the speaker cue that overlaps this cue most
            best = max(speaker_cues, key=lambda sc: _overlap(c["start"], c["end"], sc["start"], sc["end"]))
            if _overlap(c["start"], c["end"], best["start"], best["end"]) > 0:
                speaker = best["speaker"]
        pipe.append({"abs_start_ms": offset + c["start"], "abs_end_ms": offset + c["end"],
                     "text": c["text"], "speaker": speaker})
    truth_items = [v for v in truth.values() if truth_clip_id(v) == cid]
    truth_items.sort(key=lambda v: int(v["abs_start_ms"]))
    lone: dict[str, list[LoneChar]] = {}
    if scorer_version() == "v4":
        # each side takes the writers' rule; a character either side leaves out is counted on neither side
        truth_items, lone["truth"] = lone_char_rule(truth_items)
        pipe, lone["pipe"] = lone_char_rule(pipe)
        recorded = clip_dir / "subs" / "lone_dropped.json"   # what the subtitle writers themselves left out
        written = cast("list[LoneChar]", json.loads(recorded.read_text(encoding="utf-8"))) if recorded.exists() else []
        for row in lone["pipe"] + [cast("LoneChar", dict(r, action="dropped", start_s=offset / 1000 + r["start_s"],
                                        end_s=offset / 1000 + r["end_s"])) for r in written]:
            if row["action"] == "dropped":
                truth_items = remove_char_at(truth_items, row)
        for row in lone["truth"]:
            if row["action"] == "dropped":
                pipe = remove_char_at(pipe, row)
    speaker_of = speaker_map_from_file(speaker_from) if speaker_from else None
    rows, uncovered = align(truth_items, pipe, speaker_of)
    summary = score_rows(rows)
    # segmentation-free text error: the whole truth against the whole output inside the truth span, both in time
    # order.  It does not depend on where either side cuts its lines (an output that merges or splits lines differently
    # from the truth is judged on its words only).
    if truth_items:
        lo, hi = int(truth_items[0]["abs_start_ms"]), int(truth_items[-1]["abs_end_ms"])
        flat_t = normalize("".join(t["text"] for t in truth_items))
        flat_p = normalize("".join(p["text"] for p in pipe if _overlap(lo, hi, p["abs_start_ms"], p["abs_end_ms"]) > 0))
        summary["cer_flat"] = round(edit_distance(flat_p, flat_t) / max(1, len(flat_t)), 4)
        summary["exact_flat"] = exact_flat([normalize(t["text"]) for t in truth_items], flat_p)
        sources: dict[str, list[TimedText]] = {}
        for d in [clip_dir, *(Path(e) / cid for e in evidence_dirs)]:
            sources = clip_sources(d)
            if sources:
                break
        whole = cer_whole(truth_items, pipe, sources, offset)
        summary["cer_whole"] = whole["cer_whole"]
        summary["whole"] = whole
    if lone:
        summary["lone_chars"] = {side: [f"{r['action']}:{r['text']}" for r in v] for side, v in lone.items()}
    return {"cid": cid, "offset_ms": offset, "rows": rows, "uncovered": uncovered, "summary": summary}


def remove_char_at(items: Sequence[ScoreItem], row: LoneChar) -> list[ScoreItem]:
    """The other side of a character one side leaves out (v4): in the item that overlaps the character's time most,
    the occurrence of that character nearest its time is not counted (removed).  Nothing happens when no overlapping
    item has the character."""
    char = normalize(str(row["text"]))
    if not char:
        return [cast(ScoreItem, dict(it)) for it in items]
    a, b = int(round(float(row["start_s"]) * 1000)), int(round(float(row.get("end_s", row["start_s"])) * 1000))
    out = [cast(ScoreItem, dict(it)) for it in items]
    ranked = sorted(range(len(out)), key=lambda i: -_overlap(a, b, int(out[i]["abs_start_ms"]), int(out[i]["abs_end_ms"])))
    for i in ranked[:1]:
        it = out[i]
        if _overlap(a, b, int(it["abs_start_ms"]), int(it["abs_end_ms"])) <= 0:
            break
        text = str(it["text"])
        spots = [k for k, ch in enumerate(text) if normalize(ch) == char]
        if not spots:
            break
        t0, t1 = int(it["abs_start_ms"]), int(it["abs_end_ms"])
        want = (((a + b) / 2) - t0) / max(1, t1 - t0) * max(1, len(text) - 1)
        k = min(spots, key=lambda j: abs(j - want))
        out[i] = cast(ScoreItem, dict(it, text=text[:k] + text[k + 1:]))
    return out


def lone_char_rule(items: Sequence[ScoreItem]) -> tuple[list[ScoreItem], list[LoneChar]]:
    """The subtitle writers' rule for isolated single characters applied to a scored side ({abs_start_ms, abs_end_ms,
    text, speaker}): normalize.join_lone_chars decides, the side follows its decision without re-cutting its lines (a
    joined character goes into the touching same-speaker item, a dropped one is not counted).  Items without a speaker
    are left as they are (the rule waits for the speaker).  Returns (items, the rule's rows)."""
    from .normalize import join_lone_chars

    out = [cast(ScoreItem, dict(it)) for it in items]
    cues: list[TrackCue] = [{"start_s": int(it["abs_start_ms"]) / 1000, "end_s": int(it["abs_end_ms"]) / 1000, "text": str(it["text"]),
             "speaker": it.get("speaker")} for it in out]
    _, rows = join_lone_chars(cues)
    for row in rows:
        if row["action"] == "deferred":
            continue
        k = next((i for i, it in enumerate(out) if abs(int(it["abs_start_ms"]) / 1000 - row["start_s"]) < 1e-3
                  and normalize(str(it["text"])) == normalize(row["text"])), None)
        if k is None:
            continue
        lone_item = out.pop(k)
        if row["action"] == "joined_prev" and k > 0:
            out[k - 1] = cast(ScoreItem, dict(out[k - 1], text=f"{out[k - 1]['text']}，{lone_item['text']}",
                              abs_end_ms=max(int(out[k - 1]["abs_end_ms"]), int(lone_item["abs_end_ms"]))))
        elif row["action"] == "joined_next" and k < len(out):
            out[k] = cast(ScoreItem, dict(out[k], text=f"{lone_item['text']}，{out[k]['text']}",
                          abs_start_ms=min(int(out[k]["abs_start_ms"]), int(lone_item["abs_start_ms"]))))
    return out, rows


def whole_total(results: Sequence[ScoreResult]) -> WholeScore:
    """cer_whole over several clips: edits summed over truth characters summed (each clip weighs by its length)."""
    edits = sum(int(r["summary"].get("whole", {}).get("edits", 0)) for r in results)
    chars = sum(int(r["summary"].get("whole", {}).get("truth_chars", 0)) for r in results)
    return {"cer_whole": round(edits / max(1, chars), 4), "edits": edits, "truth_chars": chars,
            "exact": sum(int(r["summary"]["exact"]) for r in results), "n": sum(int(r["summary"]["n"]) for r in results),
            "exact_flat": sum(int(r["summary"].get("exact_flat", 0)) for r in results)}


def format_table(results: Sequence[ScoreResult]) -> str:
    """One line per clip, the first measure first (cer_whole, with its edits and truth characters), then a total."""
    lines = []
    for res in results:
        s = res["summary"]
        w = s.get("whole") or {}
        lines.append(f"{res['cid']}: cer_whole={s.get('cer_whole')} ({w.get('edits')}/{w.get('truth_chars')}) "
                     f"n={s['n']} exact={s['exact']}/{s['n']} ({s['exact_rate']}) exact_flat={s.get('exact_flat')} "
                     f"cer_flat={s.get('cer_flat')} cer_macro={s['cer_macro']} cer_micro={s['cer_micro']} "
                     f"spk={s['speaker_correct']}/{s['speaker_all']} spk_decided={s['speaker_acc_decided']} "
                     f"({s['speaker_decided']} decided) uncovered={len(res['uncovered'])}")
    if len(results) > 1:
        t = whole_total(results)
        lines.append(f"TOTAL: cer_whole={t['cer_whole']} ({t['edits']}/{t['truth_chars']}) exact={t['exact']}/{t['n']} "
                     f"exact_flat={t['exact_flat']}")
    return "\n".join(lines)


TSV_COLUMNS = ("label", "cid", "scorer", "cer_whole", "edits", "truth_chars", "exact", "n", "exact_flat", "cer_flat",
               "cer_micro", "speaker_correct", "speaker_all")


def tsv_rows(results: Sequence[ScoreResult], label: str) -> list[str]:
    """Rows for a scores table (TASK-12 scores.tsv): one per clip and a TOTAL row; cer_whole is the first measure."""
    rows = []
    for res in results:
        s = res["summary"]
        w = s.get("whole") or {}
        vals = (label, res["cid"], s["scorer"], s.get("cer_whole"), w.get("edits"), w.get("truth_chars"), s["exact"],
                s["n"], s.get("exact_flat"), s.get("cer_flat"), s["cer_micro"], s["speaker_correct"], s["speaker_all"])
        rows.append("\t".join("" if v is None else str(v) for v in vals))
    t = whole_total(results)
    rows.append("\t".join(str(v) for v in (label, "TOTAL", scorer_version(), t["cer_whole"], t["edits"], t["truth_chars"],
                                            t["exact"], t["n"], t["exact_flat"], "", "", "", "")))
    return rows


def error_lines(results: Sequence[ScoreResult]) -> list[str]:
    out = []
    for res in results:
        for r in res["rows"]:
            tn, pn = normalize(r["truth_text"]), normalize(r["pipe_text"])
            want = _is_host(r["truth_speaker"])
            got = _is_host(r["pred_speaker"])
            text_bad = tn != pn
            spk_bad = want != got
            if not (text_bad or spk_bad):
                continue
            cer = edit_distance(pn, tn) / max(1, len(tn))
            out.append(f"{res['cid']} {r['abs_start_ms']}-{r['abs_end_ms']} "
                       f"T[{r['truth_speaker']}]={r['truth_text']!r} "
                       f"P[{r['pred_speaker']}]={r['pipe_text']!r} "
                       f"text={'BAD' if text_bad else 'ok'} spk={'BAD' if spk_bad else 'ok'} cer={cer:.2f}")
        for u in res["uncovered"]:
            out.append(f"{res['cid']} {u['abs_start_ms']}-{u['abs_end_ms']} UNCOVERED P={u['text']!r}")
    return out
