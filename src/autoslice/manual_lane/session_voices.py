"""Same-session voice samples: the lines of one session whose speaker is already known, as each person's voice in that
session (task speaker-truth-loop; the channel's request).

A session's own lines are each person's voice in that room, over that call, on that day: speaker-truth r1-r6 found the
same session's labels the one training data that helps, and mixing in one-person sessions hurts.  Here a clip's samples
come from the other clips of its session (same broadcast day, same participants; each clip's revisions are one clip):
- tier "owner": the lines the channel owner labelled himself, by name (`owner_samples`, read as step 4's lock and the
  guest names read them, guest_identity.state_owner_rows): an import `--decided-by owner` that brought
  speaker/speakers.json, and the owner tier of the channel's speaker truth store
  (`assets/<channel>/speaker_truth/<day>.*.json`, its newest set: one no other set supersedes) where the import has no
  label over the line (his pages before speakers.json existed reach the store only);
- tier "arrival": the lines whose arrival evidence is unambiguous (`arrival_high`), where that clip has no owner label
  over them: the owner wins wherever the two meet.
The clip itself is never its own sample.  Two consumers read the same list: step 4's voiceprint (`v8_env`: the samples
go to `assets/manual_lane/remote/speaker_truth.py session`, which fits the session classifier and its self-check;
`switch` is the rule over that check), and who_said through Person.voice (`who_said.voice.references` over the samples'
windows).  The rule: docs/pipeline/manual-lane-supervision.md (说话人真值与本场样本); numbers:
`assets/manual_lane/session_voices.json`."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

from . import arrival, channel
from .config import participant_names
from .layout import layout
from .speaker_v8 import EngineSegment
from .srt_text import parse_srt
from .state import ClipState

if TYPE_CHECKING:
    from .runner import Ctx

RULES_FILE = Path(__file__).resolve().parents[3] / "assets" / "manual_lane" / "session_voices.json"
REVISION = re.compile(r"_r\d+$")


class Sample(TypedDict):
    clip: str               # the clip the line is in (its live revision)
    remote_dir: str         # that clip's directory on the media host (its audio: <remote_dir>/cut/cut.mp4)
    start_ms: int           # the clip's clock
    end_ms: int
    text: str
    person: str             # a participant, or a shared path's label (arrival 「甲或乙」: host/guest level only)
    tier: str               # "owner" | "arrival"
    among: NotRequired[list[str]]     # a shared path's label: the participants it can be


def rules() -> dict[str, float]:
    return {k: float(v) for k, v in json.loads(RULES_FILE.read_text(encoding="utf-8")).items() if not k.startswith("_")}


def family(cid: str) -> str:
    """A clip and its revisions (<cid>_r2, _r3 ...) are one clip."""
    return REVISION.sub("", cid)


def _revision(cid: str) -> int:
    m = REVISION.search(cid)
    return int(m.group()[2:]) if m else 1


def truth_owner_rows(day: str) -> dict[str, list[dict[str, object]]]:
    """{clip family: rows} of the newest owner-tier set of `day` in the channel's speaker truth store."""
    from .channel import asset

    sets = [json.loads(f.read_text(encoding="utf-8")) for f in sorted(asset("speaker_truth").glob(f"{day}.*.json"))]
    sets = [d for d in sets if d.get("tier") == "owner"]
    replaced = {d.get("supersedes") for d in sets}
    live = [d for d in sets if d["set"] not in replaced]
    if len(live) > 1:
        raise ValueError(f"{day}: owner truth sets {[d['set'] for d in live]} — none supersedes the other")
    out: dict[str, list[dict[str, object]]] = {}
    for row in live[0]["rows"] if live else []:
        out.setdefault(family(str(row["clip"])), []).append(row)
    return out


def arrival_high(lines: Sequence[arrival.TextSpan], evidence: arrival.Evidence) -> dict[int, str]:
    """{line index: label} for the lines the arrival evidence settles beyond doubt: placed by their words, one label,
    not mixed and not cut in two, and that label's stretches holding at least `arrival_high_share` of all the voice
    heard in the line's whole window (so a neighbour's or a second voice's sound in the window disqualifies it).
    The weak truth of 11-23 / 11-24 (scratchpad arrival-truth, set B) was chosen by this same rule."""
    share = rules()["arrival_high_share"]
    segments: Sequence[EngineSegment] = evidence["segments"]
    persons = evidence["persons"]
    out: dict[int, str] = {}
    for i, (line, row) in enumerate(zip(lines, arrival.line_words(lines, segments, persons, evidence.get("letters") or {}),
                                        strict=True)):
        who = row["person"]
        if row["by"] != "words" or row["mixed"] or row["parts"] or not who or who == arrival.UNNAMED:
            continue
        held: dict[str, int] = {}
        for seg in segments:
            ov = min(int(line["end_ms"]), int(seg["end_ms"])) - max(int(line["start_ms"]), int(seg["start_ms"]))
            if ov > 0:
                label = persons.get(str(seg["speaker"]), "")
                held[label] = held.get(label, 0) + ov
        if held and held.get(who, 0) >= share * sum(held.values()):
            out[i] = who
    return out


def _lines(clip_dir: Path) -> list[arrival.TextSpan]:
    return [{"start_ms": round(c["start_s"] * 1000), "end_ms": round(c["end_s"] * 1000), "text": str(c["text"])}
            for c in parse_srt((clip_dir / "subs" / "final.srt").read_text(encoding="utf-8"))]


def _overlaps(a: int, b: int, samples: Sequence[Sample]) -> bool:
    return any(min(b, s["end_ms"]) > max(a, s["start_ms"]) for s in samples if s["tier"] == "owner")


def owner_samples(state: ClipState) -> list[Sample]:
    """The lines of one clip the owner labelled by name (guest_identity.state_owner_rows: the import's speakers.json,
    then the truth store's), without his 未知."""
    from .guest_identity import state_owner_rows

    cid, remote = str(state.data["clip"]["cid"]), str(state.data["clip"]["remote_dir"])
    return [Sample(clip=cid, remote_dir=remote, start_ms=round(float(row["start_s"]) * 1000),
                   end_ms=round(float(row["end_s"]) * 1000), text=str(row.get("text") or ""),
                   person=str(row["speaker"]), tier="owner")
            for row in state_owner_rows(state) if row.get("speaker") and str(row["speaker"]) != "未知"]


def clip_samples(state: ClipState, clip_dir: Path) -> list[Sample]:
    """One clip's samples: the owner's labelled lines (owner_samples), then its arrival-high lines that no owner line
    overlaps."""
    cid, remote = str(state.data["clip"]["cid"]), str(state.data["clip"]["remote_dir"])
    out = owner_samples(state)
    evidence = arrival.load(clip_dir)
    if evidence is None or not (clip_dir / "subs" / "final.srt").is_file():
        return out
    lines = _lines(clip_dir)
    among = evidence.get("among") or {}
    for i, who in sorted(arrival_high(lines, evidence).items()):
        a, b = int(lines[i]["start_ms"]), int(lines[i]["end_ms"])
        if _overlaps(a, b, out):
            continue
        sample = Sample(clip=cid, remote_dir=remote, start_ms=a, end_ms=b, text=str(lines[i]["text"]), person=who,
                        tier="arrival")
        if who in among:
            sample["among"] = list(among[who])
        out.append(sample)
    return out


def session_clips(task_dir: Path, day: str, people: Sequence[str]) -> dict[str, tuple[str, ClipState]]:
    """{clip family: (live revision, its state)} of a session: the clips of broadcast day `day` whose participants are
    `people`, each family by its highest revision, superseded clips left out."""
    live: dict[str, tuple[str, ClipState]] = {}
    for path in sorted((task_dir / "state").glob("*.json")):
        state = ClipState.load(path)
        cid = str(state.data["clip"]["cid"])
        sess = state.data["session"]
        if (state.data["clip"].get("status") == "SUPERSEDED" or sess.get("recording_date") != day
                or sorted(participant_names(sess)) != sorted(people)):
            continue
        if family(cid) not in live or _revision(cid) > _revision(live[family(cid)][0]):
            live[family(cid)] = (cid, state)
    return live


def session_samples(task_dir: Path, cid: str) -> list[Sample]:
    """The samples of clip `cid`'s session from its other clips (each clip family by its live revision)."""
    me = ClipState.load(task_dir / "state" / f"{cid}.json").data["session"]
    day = me["recording_date"]
    out: list[Sample] = []
    for fam, (other, state) in sorted(session_clips(task_dir, day, participant_names(me)).items()):
        if fam != family(cid):
            out += clip_samples(state, task_dir / "clips" / other)
    return out


def classes_of(participants: Sequence[str], samples: Sequence[Sample]) -> list[str]:
    """What the session classifier tells apart: the host and every participant, except that participants the arrival
    hears on one shared voice path (samples with `among`) are that path's one label.  Every participant is a class
    whether or not the samples hold any of their voice, so one without samples shows as a class short of samples
    (`precheck`, `switch`) instead of dropping out of the classifier."""
    shared = {s["person"]: frozenset(s["among"]) for s in samples if s.get("among")}
    if len(shared) > 1:
        raise ValueError(f"more than one shared voice path {sorted(shared)} in one session")
    inside = next(iter(shared.values()), frozenset())
    return [channel.HOST, *sorted({p for p in participants if p != channel.HOST and p not in inside} | set(shared))]


def class_of(person: str, classes: Sequence[str], samples: Sequence[Sample]) -> str | None:
    """A sample's class: its person, the shared path that person is heard on, or None for a voice that is no
    participant's (his label 其他 on a video's voice): no one's sample."""
    if person in classes:
        return person
    return next((s["person"] for s in samples if person in s.get("among", ())), None)


def samples_doc(task_dir: Path, cid: str) -> dict[str, object]:
    """What `speaker_truth.py session` reads: the session, its classes, the samples (each with its class) and their
    clips' media."""
    me = ClipState.load(task_dir / "state" / f"{cid}.json").data["session"]
    people = sorted(participant_names(me))
    got = session_samples(task_dir, cid)
    classes = classes_of(people, got)
    return {"schema": "session-voices.v1", "for": cid,
            "session": {"recording_date": me["recording_date"], "participants": people,
                        "groups": len([g for g in me.get("participants") or [] if g])},
            "classes": classes, "samples": [dict(s, cls=c) for s in got if (c := class_of(s["person"], classes, got))],
            "media": {x["clip"]: f"{x['remote_dir']}/cut/cut.mp4" for x in got}}


def precheck(doc: Mapping[str, object]) -> list[str]:
    """The reasons a session cannot have its classifier, read from the samples alone (before any fit): too few sample
    clips to leave one out, a class with too little voice, too few owner-labelled lines to measure the switch on."""
    r = rules()
    samples = cast("list[dict[str, object]]", doc["samples"])
    why = []
    clips = {s["clip"] for s in samples}
    if len(clips) < r["min_clips"]:
        why.append(f"{len(clips)} sample clips < {r['min_clips']:g}")
    for c in cast("list[str]", doc["classes"]):
        mine = [s for s in samples if s["cls"] == c]
        secs = sum(cast("int", s["end_ms"]) - cast("int", s["start_ms"]) for s in mine) / 1000
        if len(mine) < r["per_class_lines"] or secs < r["per_class_seconds"]:
            why.append(f"{c}: {len(mine)} lines / {secs:g} s < {r['per_class_lines']:g} / {r['per_class_seconds']:g}")
    owner = sum(1 for s in samples if s["tier"] == "owner")
    if owner < r["owner_lines_min"]:
        why.append(f"{owner} owner-labelled lines < {r['owner_lines_min']:g} (the switch is measured on his lines only)")
    return why


def switch(report: Mapping[str, object]) -> tuple[bool, list[str]]:
    """Whether a session's clips may take the session classifier's voiceprint instead of the frozen v8's, from its
    self-check (`speaker_truth.py session` session.json): each sample clip with owner-labelled lines is left out, the
    session classifier is fitted without it, and both classifiers are scored on that clip's owner lines only.  The
    arrival-high lines train the session classifier but are never what it is measured on: step 4 already labels them
    by the arrival (speaker_v8.DECISIVE), and they are the easy lines.  All must hold (numbers: RULES_FILE):
    - at least `min_clips` sample clips with audio; every class at least `per_class_lines` lines and
      `per_class_seconds` seconds of samples; at least `owner_lines_min` owner lines measured;
    - at each classifier's host threshold (step 4's for its classes), fewer host/guest errors than the frozen v8
      (strictly);
    - no more person errors, and no higher error share among the lines either one is sure of (top >= 0.9).
    Returns (switch, the reasons it does not)."""
    r = rules()
    why: list[str] = []
    clips = cast("list[str]", report["clips"])
    if len(clips) < r["min_clips"]:
        why.append(f"{len(clips)} sample clips with audio < {r['min_clips']:g}")
    for c, n in cast("dict[str, dict[str, float]]", report["counts"]).items():
        if n["lines"] < r["per_class_lines"] or n["seconds"] < r["per_class_seconds"]:
            why.append(f"{c}: {n['lines']:g} lines / {n['seconds']:g} s < "
                       f"{r['per_class_lines']:g} / {r['per_class_seconds']:g}")
    check = cast("dict[str, dict[str, int]] | None", report["self_check"])
    if check is None:
        return False, why or ["no self-check"]
    old, new = check["frozen"], check["session"]
    if new["n"] < r["owner_lines_min"]:
        why.append(f"{new['n']} owner lines measured < {r['owner_lines_min']:g}")
    if not why:
        if new["hg_err"] >= old["hg_err"]:
            why.append(f"host/guest errors {new['hg_err']} not below the frozen v8's {old['hg_err']}")
        if new["person_err"] > old["person_err"]:
            why.append(f"person errors {new['person_err']} above the frozen v8's {old['person_err']}")
        if new["hi_err"] * max(old["hi_n"], 1) > old["hi_err"] * max(new["hi_n"], 1):
            why.append(f"sure-line errors {new['hi_err']}/{new['hi_n']} above the frozen v8's {old['hi_err']}/{old['hi_n']}")
    return not why, why


# ----- step 4: which voiceprint a linked clip's runs use (step4_speaker.voice_chunks / _v8_predictions) -----------
RECORD = "speaker/session-voices.json"    # the clip's choice: the samples' key, the decision and why, the classifier


class SessionVoicesError(RuntimeError):
    """The session classifier's fit on the media host did not finish."""


def env_line(paths: Mapping[str, str]) -> str:
    return "".join(f"SPEAKER_V8_{k.upper()}={v} " for k, v in paths.items())


def v8_env(ctx: Ctx) -> str:
    """The SPEAKER_V8_* settings of this clip's voiceprint runs: the frozen v8's (layout speaker_v8), or, in a linked
    session whose other clips give samples that pass `precheck` and a self-check that passes `switch`, the session
    classifier's version directory (`choose`)."""
    from .step_io import linked_session

    if not linked_session(ctx.session):
        return env_line(layout().speaker_v8)
    return env_line(cast("dict[str, str]", choose(ctx)["v8"]))


def choose(ctx: Ctx) -> dict[str, object]:
    """The clip's classifier choice, recorded in RECORD and reused while the session's samples are unchanged: a changed
    sample list (he labelled another clip of the session, a sibling clip got its arrival) decides again.  Raises
    SessionVoicesError when the fit on the media host fails."""
    doc = samples_doc(ctx.clip_dir.parent.parent, str(ctx.state.data["clip"]["cid"]))
    key = hashlib.sha256(json.dumps(doc, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    path = ctx.clip_dir / RECORD
    if path.is_file():
        old = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
        if old["key"] == key:
            return old
    frozen = dict(layout().speaker_v8)
    samples = cast("list[Sample]", doc["samples"])
    rec: dict[str, object] = {"key": key, "samples": len(samples), "owner_lines": sum(s["tier"] == "owner" for s in samples),
                              "classes": doc["classes"], "decision": "KEEP_FROZEN", "v8": frozen}
    why = precheck(doc)
    if not why:
        report = _fit(ctx, doc, key)
        ok, why = switch(report)
        rec.update(self_check=report["self_check"], counts=report["counts"], report=report["remote"])
        if ok:
            version = str(report["version"])
            rec.update(decision="SWITCH", v8=dict(frozen, code_dir=version, score=f"{version}/score.py"))
    rec["why"] = why
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return rec


def note(ctx: Ctx) -> str:
    """How PROCESS names the classifier step 4 used (RECORD; a clip without one used the frozen v8)."""
    path = ctx.clip_dir / RECORD
    if not path.is_file():
        return "分类器 v8 冻结"
    rec = json.loads(path.read_text(encoding="utf-8"))
    if rec["decision"] == "SWITCH":
        return (f"分类器：本场分类器（冻结 v8 底库加本场其他片 {rec['samples']} 句样本，其中频道主标的 {rec['owner_lines']} 句；"
                f"按他标的句自检过线，session_voices）")
    return (f"分类器 v8 冻结（本场其他片样本 {rec['samples']} 句、其中频道主标的 {rec['owner_lines']} 句，不换本场分类器："
            f"{'；'.join(rec['why'])}）")


def _fit(ctx: Ctx, doc: Mapping[str, object], key: str) -> dict[str, object]:
    """speaker_truth.py session on the media host, in a workspace of its own per sample list (kept and reused)."""
    from . import speaker_v8
    from .lane_rules import rules as lane_rules
    from .step_io import REMOTE_TOOLS, _run_remote

    rd, cid = ctx.remote_dir, str(ctx.state.data["clip"]["cid"])
    v8 = layout().speaker_v8
    local = ctx.clip_dir / "speaker" / "session-samples.json"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    ctx.adapters.remote.put(local, f"{rd}/speaker/session-samples.json")
    for tool in ("speaker_truth.py", "speaker_score.py", "speaker_v8_run.sh"):
        ctx.adapters.remote.put(REMOTE_TOOLS / tool, f"{rd}/speaker/{tool}")
    cfg, n = lane_rules("speaker"), speaker_v8.people(ctx.session)
    session = cast("dict[str, object]", doc["session"])
    # speaker_v8.known_participants, split in two: what the session allows here, the classes on the host
    could_know = cast("int", session["groups"]) >= 2 and len(cast("list[str]", session["participants"])) == session["groups"]
    work = f"{layout().speaker_v8_root}/{cid}-session-{key}"
    body = (f"if [ ! -s {work}/session.json ]; then nice -n 10 {v8['python']} {rd}/speaker/speaker_truth.py session "
            f"--samples {rd}/speaker/session-samples.json --base {v8['code_dir']} --out {work} --version classifier "
            f"--host {channel.HOST} --host-min {speaker_v8.host_threshold(n, cfg, known=False)} "
            f"--host-min-known {speaker_v8.host_threshold(n, cfg, known=could_know)} "
            f"--v8-code {v8['code_dir']} --v8-python {v8['python']} --v8-model {v8['model']} --v8-score {v8['score']} "
            f"--run-script {rd}/speaker/speaker_v8_run.sh || exit 1; rm -rf {work}/clips/*/work; fi\n"
            f"echo SESSION_VOICES_DONE\n")
    res = _run_remote(ctx, "s4-session-voices", body, timeout=3600)
    if "SESSION_VOICES_DONE" not in res.stdout:
        raise SessionVoicesError(f"rc={res.rc} stdout={res.stdout[-200:]} stderr={res.stderr[-300:]}")
    got = ctx.clip_dir / "speaker" / "session.json"
    ctx.adapters.remote.get(f"{work}/session.json", got)
    report = cast("dict[str, object]", json.loads(got.read_text(encoding="utf-8")))
    report["remote"] = f"{work}/session.json"
    return report


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("samples", help="the session samples of one clip, for speaker_truth.py session")
    s.add_argument("--task-dir", type=Path, required=True)
    s.add_argument("--cid", required=True)
    s.add_argument("--out", type=Path, required=True)
    d = sub.add_parser("decide", help="the switch rule over a speaker_truth.py session report")
    d.add_argument("--report", type=Path, required=True)
    a = ap.parse_args(argv)
    if a.cmd == "samples":
        if a.out.exists():
            raise SystemExit(f"{a.out} exists")
        doc = samples_doc(a.task_dir, a.cid)
        a.out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{len(cast('list[Sample]', doc['samples']))} samples from {len(cast('dict[str, str]', doc['media']))} "
              f"clips -> {a.out}", *precheck(doc), sep="\n")
        return 0
    ok, why = switch(json.loads(a.report.read_text(encoding="utf-8")))
    print("SWITCH" if ok else "KEEP_FROZEN", *why, sep="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
