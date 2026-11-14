"""Imported subtitles (U130: the training pair is pushed by hand; the push is a formal input of step 3).

Split out of stage_impl.py (TASK-13); stage_impl re-exports every name, so callers and tests keep
`stage_impl.<name>`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, MutableMapping, Sequence
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

from . import text_overlap
from . import (
    doubt_policy,
    normalize,
    speaker_v8,
)
from .runner import Ctx, StepOutcome
from .srt_text import TimedText, _json_from_text
from .step3_adjudicate import hold_line, lone_char_line, record_lone
from .step_io import _ask_cpa, _cpa_ok, _fail, _local, _log, _render, linked_session

if TYPE_CHECKING:
    from src.autoslice.cpa_gemini_client import JsonValue
    from .state import ClipState, CpaEvent

# ----- imported subtitles (U130: the training pair is pushed by hand; the push is a formal input of step 3) --------
IMPORT_DIR = "import"


IMPORT_FILES = {"final.srt": "subs/final.srt", "speaker.srt": "speaker/speaker.srt", "review.tsv": "subs/review.tsv",
                "doubts.json": "subs/doubts.json", "writing_canon.json": "subs/writing_canon.json",
                "cut.srt": "subs/cut.srt",      # cut.srt: the sentences the owner marked 删 (cut-clock times); trim_run cuts them from the video
                "baseline.srt": "subs/baseline.srt"}    # baseline.srt: the subtitles as the owner saw them before he edited (owner_edits)
# what an import replaces: step 3 and everything after it is produced again from the import (ordinary path)
IMPORT_REDO = ("subtitles", "speaker", "burn", "title_cover", "tags", "package", "review", "upload")


ImportRecord = TypedDict("ImportRecord", {"source": str, "from": str, "authority": str, "files": dict[str, str], "at": str,
                                          "verbatim": NotRequired[bool], "decided_by": NotRequired[str],
                                          "owner_edited": NotRequired[list[int] | None]})


class ImportedDoubt(doubt_policy.DecisionView, total=False):
    cue_id: None
    start_s: float
    end_s: float
    current: str
    source: str
    basis: str
    alternatives: list[str]
    speaker: str | None
    speaker_confidence: str | None
    meaning_basis: str


class MeaningResult(TypedDict):
    cpa: list[CpaEvent]
    process: list[str]


def imported(ctx: Ctx) -> ImportRecord | None:
    """The import recorded by `import-subtitles` (state.imported), or None."""
    return ctx.state.data.get("imported")


def owner_decided(ctx: Ctx) -> bool:
    """True when the import was decided sentence by sentence by the channel owner himself (`import-subtitles
    --decided-by owner`, imported["decided_by"]): after step 3 no machine step may change the words or speakers he
    edited (imported["owner_edited"]; review_owner), and the review's objections to them need a disposition."""
    rec = imported(ctx)
    return rec is not None and rec.get("decided_by") == "owner"


def import_redo(state: ClipState, clip_dir: Path, src: Path, *, verbatim: bool = False,
                decided_by: str | None = None) -> str:
    """What `record_import` does with the steps: "review_only" when the clip already holds an import whose files are
    byte for byte those of `src` (the same set), with the same `verbatim`, and only `decided_by` differs (the mark is
    added or removed: nothing but the review depends on it); otherwise "from_step_3"."""
    prior = state.data.get("imported")
    if prior is None or prior.get("decided_by") == decided_by or bool(prior.get("verbatim")) != verbatim:
        return "from_step_3"
    names = {n for n, rel in IMPORT_FILES.items() if (src / rel).exists()}
    same = set(prior["files"]) == names and all(
        (clip_dir / IMPORT_DIR / n).is_file() and (clip_dir / IMPORT_DIR / n).read_bytes() == (src / IMPORT_FILES[n]).read_bytes()
        for n in names)
    return "review_only" if same else "from_step_3"


def forget_steps(state: ClipState, *, review_only: bool = False) -> None:
    """Forget what an import (or an owner-authorised edit of it) invalidates: step 3 and everything after it, or with
    `review_only` just the review and the upload; plus the review's rewind and revision keys."""
    for key in ("review", "upload") if review_only else IMPORT_REDO:
        state.data["steps"].pop(key, None)
    for key in ("review", "rewind", "revisions") if review_only else ("review", "rewind", "revisions", "manual_import"):
        cast("MutableMapping[str, JsonValue]", state.data).pop(key, None)


def record_import(state: ClipState, clip_dir: Path, src: Path, *, source: str, authority: str,
                  verbatim: bool = False, decided_by: str | None = None, owner_edited: Sequence[int] | None = None) -> ImportRecord:
    """`import-subtitles`: a push's decided subtitles (and speakers) become this clip's step 3 input.  The push must have
    been made on this clip's cut (its boundary decision, when it has one, adopts the same start and end).  The files are
    copied into clips/<cid>/import/; step 3 and every later step are forgotten so the next run produces them from the
    import; the review lineage of the old products is dropped.  Returns the state record.  Exception (import_redo):
    the same files imported again with only `decided_by` changed update the record and forget just the review, the
    upload and the review's state keys; steps 3-8 and their products stay.

    `verbatim` (recorded as imported["verbatim"]): the push's text is already the decided, shaped subtitles of this
    lane (a clip cut out of a finished one), so step 3 writes it as it is, with no normalisation.  A push that holds
    subs/doubts.json brings its own doubt table (ready-made import-style rows) instead of review.tsv.

    `decided_by` (recorded as imported["decided_by"]; only "owner" is known): see owner_decided.  With it the record also
    holds imported["owner_edited"], the numbers of the push cues the owner edited (owner_edits; None without a baseline).
    `owner_edited`: for a successor's push (`trim`, `revise`) the numbers the predecessor's sentences carry over (owner_edits.carried),
    used instead of the baseline, which such a push has none of; None (no carried answer): the baseline decides, as for any push."""
    import shutil

    from .state import StateError, utc_now

    if decided_by not in (None, "owner"):
        raise StateError(f"decided_by must be 'owner', not {decided_by!r}")
    clip = state.data["clip"]
    decision = src / "boundary/decision.json"
    if decision.exists():
        adopted = json.loads(decision.read_text(encoding="utf-8")).get("adopted") or {}
        if (adopted.get("start_ms"), adopted.get("end_ms")) != (clip["start_ms"], clip["end_ms"]):
            raise StateError(f"the push {src} was made on another cut {adopted.get('start_ms')}-{adopted.get('end_ms')} "
                             f"(this clip {clip['start_ms']}-{clip['end_ms']})")
    if not (src / IMPORT_FILES["final.srt"]).exists():
        raise StateError(f"{src / IMPORT_FILES['final.srt']} is missing")
    dest = clip_dir / IMPORT_DIR
    review_only = import_redo(state, clip_dir, src, verbatim=verbatim, decided_by=decided_by) == "review_only"
    if decided_by == "owner" and not review_only:       # how the pipeline's sentences turned out, before they are replaced
        from . import outcomes

        outcomes.record(state, clip_dir, src)
    dest.mkdir(parents=True, exist_ok=True)
    files = {}
    for name, rel in IMPORT_FILES.items():
        if not review_only and not (src / rel).exists():
            (dest / name).unlink(missing_ok=True)       # the import is this push, whole: nothing of an earlier one stays
        if (src / rel).exists():
            if not review_only:
                shutil.copyfile(src / rel, dest / name)
            files[name] = str(dest / name)
    rec: ImportRecord = {"source": source, "from": str(src), "authority": authority, "files": files, "at": utc_now()}
    if verbatim:
        rec["verbatim"] = True
    if decided_by is not None:
        rec["decided_by"] = decided_by
    if decided_by == "owner":      # the push cues the owner edited (None: no baseline to compare, all of them count)
        from .owner_edits import pushed_edits

        rec["owner_edited"] = list(owner_edited) if owner_edited is not None else pushed_edits(
            src / IMPORT_FILES["final.srt"], src / IMPORT_FILES["speaker.srt"], src / IMPORT_FILES["baseline.srt"])
    forget_steps(state, review_only=review_only)
    state.data.pop("dissent", None)     # the review's objections to the old text are not the new import's
    state.data["imported"] = rec
    state.save()
    return rec


def _import_doubts(rows: Sequence[Mapping[str, str]], source: str) -> list[ImportedDoubt]:
    """review.tsv rows of a hand push -> doubt rows of the canonical contract.  Each row carries its own time (cut
    clock); the push gives its confidence but not whether the alternatives change the meaning, so `affects_meaning` is
    None (unknown), which doubt_policy.HOLD_RULE treats as affecting it."""
    out: list[ImportedDoubt] = []
    for r in rows:
        text, bcut_text = str(r.get("text") or ""), str(r.get("bcut_text") or "")
        out.append({"id": f"imp-{r.get('id') or r.get('no')}", "cue_id": None,
                    "start_s": float(r["start"]), "end_s": float(r["end"]), "current": bcut_text,
                    "state": "KEEP" if text_overlap.key(text) == text_overlap.key(bcut_text) else "ADOPT", "final_text": text,
                    "confidence": "低" if str(r.get("text_conf") or "") == "低" else "高", "affects_meaning": None,
                    "source": source, "basis": str(r.get("basis") or "").strip() or f"{source} 手推",
                    "alternatives": [a.strip() for a in str(r.get("alt") or "").split(" / ") if a.strip()],
                    "speaker": r.get("speaker"), "speaker_confidence": r.get("speaker_conf")})
    return out


def _push_doubts(base: Path, rec: ImportRecord) -> list[ImportedDoubt] | StepOutcome:
    """The doubt table of the push: its own subs/doubts.json when it brought one (ready-made import-style rows whose
    `affects_meaning` is already true or false, so nothing is left to ask), else the rows of review.tsv."""
    ready = base / "doubts.json"
    if ready.exists():
        rows = json.loads(ready.read_text(encoding="utf-8"))
        if not isinstance(rows, list) or not all(isinstance(r, dict) and r.get("id") for r in rows):
            return _fail("IMPORT_DOUBTS_INVALID", f"{ready} must be a list of doubt rows that each carry an id")
        unsettled = [r["id"] for r in rows if not isinstance(r.get("affects_meaning"), bool)]
        if unsettled:
            return _fail("IMPORT_DOUBTS_INVALID", f"{ready}: affects_meaning is not true/false in {unsettled}")
        return cast("list[ImportedDoubt]", rows)
    tsv = base / "review.tsv"
    rows_tsv: list[dict[str, str]] = []
    if tsv.exists():
        import csv

        with tsv.open(encoding="utf-8") as fh:
            rows_tsv = list(csv.DictReader(fh, delimiter="\t"))
    return _import_doubts(rows_tsv, str(rec.get("source")))


def _step3_import_verbatim(ctx: Ctx, rec: ImportRecord, cues: Sequence[TimedText]) -> StepOutcome:
    """Step 3 from a push that is already this lane's finished subtitles (record_import(verbatim=True)): the text is
    written as it was given, with no rewrite at all (no laughter strip, canon, numerals, re-splitting, lone-character
    joins, timing change).  Only the speaker labels are read."""
    base = ctx.clip_dir / IMPORT_DIR
    final_file, spk_file = base / "final.srt", base / "speaker.srt"
    from . import subtitle_track as st

    final_text = final_file.read_text(encoding="utf-8")
    if len(st.parse_blocks(final_text)) != len(cues):
        return _fail("IMPORT_SPEAKER_MISMATCH", f"{spk_file} and {final_file} do not hold the same number of cues")
    final_path = _local(ctx, "subs/final.srt")
    final_path.write_text(final_text, encoding="utf-8")
    doubts = _push_doubts(base, rec)
    if isinstance(doubts, StepOutcome):
        return doubts
    meaning = _import_meaning(ctx, doubts, cues)
    if isinstance(meaning, StepOutcome):
        return meaning
    doubts_path = _local(ctx, "subs/doubts.json")
    doubts_path.write_text(json.dumps(doubts, ensure_ascii=False, indent=2), encoding="utf-8")
    products = {"doubts": str(doubts_path), "final_srt": str(final_path)}
    sidecar = _local(ctx, "subs/import_speakers.json")
    sidecar.unlink(missing_ok=True)          # an import without speakers leaves none of an earlier import's for step 4
    if spk_file.exists():
        sidecar.write_text(json.dumps([c.get("speaker") for c in cues], ensure_ascii=False), encoding="utf-8")
        products["import_speakers"] = str(sidecar)
    record_lone(ctx.clip_dir, [], fresh=True)
    process = [f"字幕原样导入自 {rec.get('source')}（{rec.get('from')}，{rec.get('authority', '')}）：{len(cues)} 句，"
               f"文字、拆条、时间一律不改；未听写、未重新裁决；疑点表 {len(doubts)} 条来自推送",
               *meaning["process"], hold_line(doubts)]
    return StepOutcome(True, products=products, note=f"原样导入 {rec.get('source')}：{len(cues)} 句", process=process,
                       cpa=meaning["cpa"])


def _step3_import(ctx: Ctx, rec: ImportRecord) -> StepOutcome:
    """Step 3 from an imported push: its text and speakers are the decided subtitles.  The same 维护者 rules as step 3's
    own assembly apply (no laughter, canon, shape, no overlap: normalize_cues); nothing is listened
    to and nothing is re-decided.  Speakers go to step 4 through subs/import_speakers.json (one label per final cue).
    A verbatim push (`rec["verbatim"]`) skips those rules and is written as it is."""
    from . import subtitle_track as st

    base = ctx.clip_dir / IMPORT_DIR
    final_file, spk_file = base / "final.srt", base / "speaker.srt"
    if not final_file.exists():
        return _fail("IMPORT_INPUT_MISSING", f"{final_file} is missing")
    cues = st.parse_blocks((spk_file if spk_file.exists() else final_file).read_text(encoding="utf-8"))
    if rec.get("verbatim"):
        return _step3_import_verbatim(ctx, rec, cues)
    overlaps = sum(1 for a, b in pairwise(cues) if b["start_s"] < a["end_s"] - 1e-9)
    for c in cues:
        c["text"] = normalize.strip_laughter(st.plain_text(c["text"]))
        if c.get("speaker"):
            c["speaker"] = speaker_v8.HOST if c["speaker"] == speaker_v8.HOST else speaker_v8.GUEST_LABEL
    shaped, log = normalize.normalize_cues(cues, one_speaker=not linked_session(ctx.session))
    st.write_track(ctx.clip_dir, shaped, speakers=False)
    doubts = _push_doubts(base, rec)
    if isinstance(doubts, StepOutcome):
        return doubts
    meaning = _import_meaning(ctx, doubts, shaped)
    if isinstance(meaning, StepOutcome):
        return meaning
    doubts_path = _local(ctx, "subs/doubts.json")
    doubts_path.write_text(json.dumps(doubts, ensure_ascii=False, indent=2), encoding="utf-8")
    products = {"doubts": str(doubts_path), "final_srt": str(ctx.clip_dir / "subs/final.srt")}
    sidecar = _local(ctx, "subs/import_speakers.json")
    sidecar.unlink(missing_ok=True)          # an import without speakers leaves none of an earlier import's for step 4
    if spk_file.exists():
        sidecar.write_text(json.dumps([c.get("speaker") for c in shaped], ensure_ascii=False), encoding="utf-8")
        products["import_speakers"] = str(sidecar)
    source_of_doubts = "subs/doubts.json" if (base / "doubts.json").exists() else f"review.tsv {len(doubts)} 行"
    process = [f"字幕导入自 {rec.get('source')}（{rec.get('from')}，{rec.get('authority', '')}）：{len(cues)} 句，"
               f"规范化后 {len(shaped)} 句（拆条 {log['extra_cues_from_split']}，时间重叠改为先后 {overlaps} 处，"
               f"规范表替换 {len(log['canon_replacements'])} 处，相邻句开头重复 {len(log['adjacent_repeats'])} 处只记录）；"
               f"未听写、未重新裁决；疑点表来自 {source_of_doubts}",
               lone_char_line(log["lone_chars"]), *meaning["process"], hold_line(doubts)]
    record_lone(ctx.clip_dir, log["lone_chars"], fresh=True)
    return StepOutcome(True, products=products, note=f"导入 {rec.get('source')}：{len(shaped)} 句", process=process,
                       cpa=meaning["cpa"])


def _import_meaning(ctx: Ctx, doubts: list[ImportedDoubt], cues: Sequence[TimedText]
                    ) -> MeaningResult | StepOutcome:
    """The canonical doubt row needs affects_meaning (doubt_policy.HOLD_RULE); a hand push records its confidence but
    not that.  CPA is asked once, only for the push's low-confidence rows, whether another reading would change the
    meaning; its answer is adopted (an unanswered row stays unknown, which holds).  Returns {"cpa", "process"}, or the
    failure outcome when the call fails."""
    from . import subtitle_track as st

    asked = [d for d in doubts if d.get("confidence") == "低" and d.get("affects_meaning") is None]
    if not asked:
        return {"cpa": [], "process": []}

    def context(d: ImportedDoubt) -> list[str]:
        near = [c for c in cues if float(c["end_s"]) > float(d["start_s"]) - 8 and float(c["start_s"]) < float(d["end_s"]) + 8]
        return [st.plain_text(str(c["text"])) for c in near]
    rows = [{"id": d["id"], "text": d.get("final_text"), "alternatives": d.get("alternatives") or [],
             "context": context(d)} for d in asked]
    record = _ask_cpa(ctx, "import-meaning", _render("import_meaning.md", rows=json.dumps(rows, ensure_ascii=False,
                                                                                        indent=1)),
                       purpose="import-meaning")
    if not _cpa_ok(record):
        return _fail("CPA_CALL_FAILED", record.get("stderr_tail", ""), cpa=[record])
    try:
        answers = {str(r.get("id")): r for r in _json_from_text(record["response_text"]) if isinstance(r, dict)}
    except (ValueError, TypeError, AttributeError):
        return _fail("CPA_RESPONSE_UNPARSEABLE", record["file"], cpa=[record])
    settled = 0
    for d in asked:
        answer = answers.get(str(d["id"]))
        if answer is not None and isinstance(answer.get("affects_meaning"), bool):
            d["affects_meaning"] = answer["affects_meaning"]
            d["meaning_basis"] = f"CPA（导入时）：{str(answer.get('reason') or '').strip()}"
            settled += 1
    changes = sum(1 for d in asked if d.get("affects_meaning") is True)
    return {"cpa": [_log(record)],
            "process": [f"低把握 {len(asked)} 句的「是否影响意思」由 CPA 判（导入的手推没有这一项）：影响 {changes} 句，"
                        f"不影响 {settled - changes} 句，未答 {len(asked) - settled} 句（未答视同影响）"]}
