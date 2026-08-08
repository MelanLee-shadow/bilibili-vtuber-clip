#!/usr/bin/env python3
"""DEV PROBE — NOT production runtime. Not imported by any pipeline module.

Read-only mining probe for automatic misheard-direction (误听面) accumulation.
Written 2026-08-08 per Ivan's directive to quantify the mine before building
a real extractor. Run this on `free` where /opt/bilive/autoslice/out/ lives
(pypinyin must be importable there); it only reads under out/ and writes its
own JSONL/summary under --out-dir (default /tmp/mishear-probe), never back
into out/.

Pipeline:
  1. Inventory candidates that have both a BCUT draft SRT
     (padded_*.asr_draft.srt) and a delivered final SRT
     (replacement_recuts/<cid>.recut.srt), matched via the recut's
     provenance JSON (final_recut.source_path -> padded basename), since a
     candidate directory may hold multiple padded_*.asr_draft.srt from
     reprocessing attempts.
  2. Shift each recut cue's timestamps by final_recut.start_ms (from
     <cid>.recut.provenance.json) to map them back onto the padded/draft
     timeline, then build a time-overlap graph between draft cues and
     shifted recut cues; connected components handle 1:N / N:1 speaker-turn
     resplits (verified pattern: draft cue "入间人间那种背德百合" at
     00:00:10.000 == recut cue1 00:00:00.250 + 9750ms offset).
  3. Drop the first and last time-ordered component per candidate (recut
     boundaries trim cue text mid-span; that's boundary noise, not a
     mishear).
  4. Diff each component's concatenated, punctuation/whitespace-normalized
     draft text against its final text with difflib.SequenceMatcher; keep
     only 'replace' opcodes where both sides are 2-8 pure-CJK characters.
  5. Score toneless pinyin similarity (pypinyin lazy_pinyin, tone stripped)
     via SequenceMatcher.ratio() on the joined syllable strings, matching
     the codebase's near_homophone_gate convention
     (see docs/reviews/2026-08-07-zsm-mishear-forensics.md); keep >= 0.5.
  6. Record agy_concurs: whether the AGY second-listen text within the
     diffed component's own time span (+-2s pad) already contains the final
     surface (a cheap independent-corroboration signal for the
     promotion-threshold writeup; the window is intentionally the
     component's span, not an arbitrary tail, so recurring short surfaces
     elsewhere in the candidate cannot count as false corroboration).

song_* candidates are inventoried but excluded from direction aggregation
(LRC-lane lyric corrections, not ASR mishears).

Usage (on free):
    python3 probe_mishear_mining.py [--root /opt/bilive/autoslice/out] \
        [--out-dir /tmp/mishear-probe]
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

try:
    from pypinyin import Style, lazy_pinyin
except ImportError:  # pragma: no cover - probe fails loudly, no silent skip
    print("FATAL: pypinyin not importable in this interpreter", file=sys.stderr)
    raise

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SRT_BLOCK_RE = re.compile(
    r"(\d+)\s*\n(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*"
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*\n(.*?)(?=\n\s*\n\d+\s*\n|\Z)",
    re.S,
)
# Strip whitespace + common CJK/ASCII punctuation for diffing; keep CJK/alnum.
_PUNCT_CHARS = (
    "，。！？、；："  # ，。！？、；：
    "“”‘’"  # “ ” ‘ ’
    "「」『』"  # 「 」 『 』
    "（）()"  # （ ） ( )
    "《》【】"  # 《 》 【 】
    "…—-,.!?;:\"'[]<>~·"  # … — - , . ! ? ; : " ' [ ] < > ~ ·
)
_PUNCT_RE = re.compile("[\\s" + re.escape(_PUNCT_CHARS) + "]")
_CJK_ONLY_RE = re.compile(r"^[一-鿿]{2,8}$")


def parse_srt(path: Path) -> list[tuple[int, int, str]]:
    """Return [(start_ms, end_ms, text)], text with internal newlines joined."""
    cues: list[tuple[int, int, str]] = []
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError:
        return cues
    for m in _SRT_BLOCK_RE.finditer(raw):
        h1, m1, s1, ms1, h2, m2, s2, ms2, text = m.groups()[1:]
        start = ((int(h1) * 60 + int(m1)) * 60 + int(s1)) * 1000 + int(ms1)
        end = ((int(h2) * 60 + int(m2)) * 60 + int(s2)) * 1000 + int(ms2)
        cues.append((start, end, " ".join(text.split())))
    return cues


def normalize(text: str) -> str:
    return _PUNCT_RE.sub("", text)


def toneless_pinyin(text: str) -> str:
    syllables = lazy_pinyin(text, style=Style.TONE3, neutral_tone_with_five=True)
    return " ".join(s.rstrip("0123456789") for s in syllables)


def pinyin_similarity(a: str, b: str) -> float:
    pa, pb = toneless_pinyin(a), toneless_pinyin(b)
    if not pa or not pb:
        return 0.0
    return difflib.SequenceMatcher(None, pa, pb).ratio()


class UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start < b_end and b_start < a_end


def cluster_cues(
    draft: list[tuple[int, int, str]], final: list[tuple[int, int, str]]
) -> list[dict]:
    """Connected components over the draft/final time-overlap graph.

    Nodes: 0..len(draft)-1 are draft cues, len(draft).. are final cues.
    Returns time-ordered list of {draft_text, final_text, t0} for
    components containing at least one node of each type.
    """
    n_d, n_f = len(draft), len(final)
    uf = UnionFind(n_d + n_f)
    for i, (ds, de, _) in enumerate(draft):
        for j, (fs, fe, _) in enumerate(final):
            if overlaps(ds, de, fs, fe):
                uf.union(i, n_d + j)

    groups: dict[int, list[int]] = defaultdict(list)
    for idx in range(n_d + n_f):
        groups[uf.find(idx)].append(idx)

    components = []
    for members in groups.values():
        d_idx = sorted(i for i in members if i < n_d)
        f_idx = sorted(i - n_d for i in members if i >= n_d)
        if not d_idx or not f_idx:
            continue
        d_texts = [draft[i] for i in d_idx]
        f_texts = [final[i] for i in f_idx]
        t0 = min(d_texts[0][0], f_texts[0][0])
        t_end = max(d_texts[-1][1], f_texts[-1][1])
        components.append(
            {
                "t0": t0,
                "t_end": t_end,
                "draft_text": normalize("".join(c[2] for c in d_texts)),
                "final_text": normalize("".join(c[2] for c in f_texts)),
                "draft_cue_ix": d_idx[0],
            }
        )
    components.sort(key=lambda c: c["t0"])
    return components


def agy_text_for_window(
    agy: list[tuple[int, int, str]], start: int, end: int
) -> str:
    parts = [text for (s, e, text) in agy if overlaps(s, e, start, end)]
    return normalize("".join(parts))


def mine_pair(
    draft: list[tuple[int, int, str]],
    final: list[tuple[int, int, str]],
    agy: list[tuple[int, int, str]],
) -> list[dict]:
    components = cluster_cues(draft, final)
    if len(components) < 3:
        return []  # too little to safely drop boundary components
    interior = components[1:-1]
    instances = []
    for comp in interior:
        sm = difflib.SequenceMatcher(None, comp["draft_text"], comp["final_text"])
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag != "replace":
                continue
            a = comp["draft_text"][i1:i2]
            b = comp["final_text"][j1:j2]
            if not (_CJK_ONLY_RE.match(a) and _CJK_ONLY_RE.match(b)):
                continue
            sim = pinyin_similarity(a, b)
            if sim < 0.5:
                continue
            draft_cue = draft[comp["draft_cue_ix"]]
            # AGY corroboration window = this component's own time span
            # (+-2s pad for minor re-segmentation), not an arbitrary tail —
            # a wide window would let unrelated later recurrences of a
            # short 2-char surface (e.g. 豆沙) count as false corroboration.
            pad = 2000
            agy_concurs = b in agy_text_for_window(
                agy, comp["t0"] - pad, comp["t_end"] + pad
            )
            instances.append(
                {
                    "draft_surface": a,
                    "final_surface": b,
                    "pinyin_sim": round(sim, 3),
                    "cue_ms": draft_cue[0],
                    "agy_concurs": agy_concurs,
                    "draft_context": comp["draft_text"][:80],
                    "final_context": comp["final_text"][:80],
                }
            )
    return instances


def load_provenance_offset(prov_path: Path) -> tuple[int, str] | None:
    try:
        data = json.loads(prov_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    fr = data.get("final_recut")
    if not fr:
        return None
    start_ms = fr.get("start_ms")
    source_path = fr.get("source_path")
    if start_ms is None or not source_path:
        return None
    return int(start_ms), Path(source_path).stem  # e.g. padded_1075980_1148520


def find_agy_path(cand_dir: Path, padded_stem: str) -> Path:
    return cand_dir / f"{padded_stem}.agy_refined.srt"


def process_candidate(
    date: str, cand_dir: Path
) -> tuple[str, bool, list[dict], list[dict]]:
    """Returns (skip_reason_or_'', paired, instances, song_instances).

    ``paired`` is True whenever at least one draft/final SRT pair was
    successfully matched via provenance, independent of whether that pair
    yielded any mishear-direction instances (answers inventory step 1).
    """
    cand = cand_dir.name
    is_song = cand.startswith("song_")
    recut_dir = cand_dir / "replacement_recuts"
    recut_srts = sorted(recut_dir.glob("*.recut.srt")) if recut_dir.is_dir() else []
    if not recut_srts:
        return "no_recut", False, [], []

    all_instances: list[dict] = []
    matched_any = False
    for recut_srt in recut_srts:
        cid = recut_srt.name[: -len(".recut.srt")]
        prov_path = recut_dir / f"{cid}.recut.provenance.json"
        offset = load_provenance_offset(prov_path)
        if offset is None:
            continue
        start_ms, padded_stem = offset
        draft_path = cand_dir / f"{padded_stem}.asr_draft.srt"
        if not draft_path.is_file():
            continue
        agy_path = find_agy_path(cand_dir, padded_stem)

        draft = parse_srt(draft_path)
        final_raw = parse_srt(recut_srt)
        final = [(s + start_ms, e + start_ms, t) for (s, e, t) in final_raw]
        agy = parse_srt(agy_path) if agy_path.is_file() else []
        if not draft or not final:
            continue
        matched_any = True
        insts = mine_pair(draft, final, agy)
        for inst in insts:
            inst["date"] = date
            inst["candidate"] = cand
            inst["cid"] = cid
        all_instances.extend(insts)

    if not matched_any:
        return "provenance_or_draft_missing", False, [], []
    if is_song:
        return "", True, [], all_instances
    return "", True, all_instances, []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/opt/bilive/autoslice/out")
    ap.add_argument("--out-dir", default="/tmp/mishear-probe")
    args = ap.parse_args()

    root = Path(args.root)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    date_dirs = sorted(d for d in root.iterdir() if d.is_dir() and DATE_RE.match(d.name))

    per_date_counts: Counter[str] = Counter()
    paired_by_date: Counter[str] = Counter()  # has draft+final SRT, talk only
    song_paired_by_date: Counter[str] = Counter()
    skip_reasons: Counter[str] = Counter()
    total_candidates = 0
    total_song_candidates = 0
    all_instances: list[dict] = []
    all_song_instances: list[dict] = []

    for date_dir in date_dirs:
        date = date_dir.name
        for cand_dir in sorted(p for p in date_dir.iterdir() if p.is_dir()):
            total_candidates += 1
            is_song = cand_dir.name.startswith("song_")
            if is_song:
                total_song_candidates += 1
            reason, paired, instances, song_instances = process_candidate(
                date, cand_dir
            )
            if reason:
                skip_reasons[reason] += 1
                continue
            if paired:
                if is_song:
                    song_paired_by_date[date] += 1
                else:
                    paired_by_date[date] += 1
            if instances:
                per_date_counts[date] += 1
            all_instances.extend(instances)
            all_song_instances.extend(song_instances)

    with (out_dir / "instances.jsonl").open("w", encoding="utf-8") as f:
        for inst in all_instances:
            f.write(json.dumps(inst, ensure_ascii=False) + "\n")
    with (out_dir / "song_instances.jsonl").open("w", encoding="utf-8") as f:
        for inst in all_song_instances:
            f.write(json.dumps(inst, ensure_ascii=False) + "\n")

    summary = {
        "root": str(root),
        "dates_scanned": [d.name for d in date_dirs],
        "total_candidates": total_candidates,
        "total_song_candidates": total_song_candidates,
        "candidates_draft_and_final_paired_by_date_talk": dict(paired_by_date),
        "candidates_draft_and_final_paired_by_date_song": dict(song_paired_by_date),
        "candidates_draft_and_final_paired_total_talk": sum(paired_by_date.values()),
        "candidates_draft_and_final_paired_total_song": sum(
            song_paired_by_date.values()
        ),
        "candidates_with_pair_producing_instances_by_date": dict(per_date_counts),
        "skip_reasons": dict(skip_reasons),
        "total_instances": len(all_instances),
        "total_song_instances": len(all_song_instances),
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
