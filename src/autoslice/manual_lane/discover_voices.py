"""`discover`: which lines of a recording's candidates sound like the host, from her voiceprint, for the labelling call.

The labelling call (core_draws) says where the host stands in each candidate, and before step 4 no line has a speaker.
In a call of three the model then credits her with a colleague's line: 11-23 21:41 「一个破石头到底有什么好吵的」 was a
guest's, and the clip, in which she has two incidental lines, was labelled hers and made (the channel's request); told to name a line that only she can be meant by, it also dropped 20:11, where her own 「我们是
团队」「我们不是敌人啊」 are the point and nobody says her name (with the marks, a highlight marked hers is such a line).
The frozen v8 classifier step 4 uses (speaker_v8_run.sh,
~1.2 s chunks, compute on the media host, no paid model) is run once over every line of the recording's candidates, and
each line is marked in the prompt: hers (mean host probability >= speaker_v8.V8_MID_MIN_CONF), someone else's
(<= speaker_v8.GUEST_MAX_HOST_PROB), or unmarked.  Measured on the 15 candidates of 11-23 it took one to two and a half
minutes of host time per half-hour recording.

The voiceprint reads the recording's mixed audio and sometimes hears a guest as the host (11-24: in the lag group 礼墨
and 安晚 shared in 礼墨's room, 28% of the chunks were classed as hers), so a mark alone never makes her a party
(core_draws.parse `heard_from` also needs the model's own reading of who spoke).  Whether two guests share one sound
path is known only at step 4, after the selection (collab_guests `shared_path`), so it cannot be read here.

A channel without a voiceprint workspace labels without marks and says so (`Heard.note`, kept in recall.json
`voiceprint`).  On a channel that has one, a run that fails raises (`VOICEPRINT_FAILED`): the labels are kept by the
asset's version and never asked again, so labelling the recording without its marks would keep that for good; the
recording is left for the next run, like a failed recall call, and its recall is kept.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TypedDict

from . import speaker_v8
from .adapters import Remote
from .discover_media import DiscoverError, Prepared, write_json
from .layout import layout


class Heard(TypedDict):
    host_prob: dict[str, float] | None      # cue id -> mean host probability of its chunks; None: no reading
    note: str


Hear = Callable[[Prepared, Path, Mapping[str, tuple[int, int]]], Heard]
WORKSPACE = "voiceprint"                     # under the recording's directory, and its remote directory
VOICEPRINT_FAILED = "VOICEPRINT_FAILED"


def hearer(remote: Remote) -> Hear:
    """The voiceprint reading of a recording's lines on the media host `remote` (the lane's Remote)."""
    from .step4_speaker import SPEAKER_V8_ENV, SPEAKER_V8_ROOT, SPEAKER_V8_SCRIPT   # step 4 owns the v8 run

    def hear(prepared: Prepared, directory: Path, spans: Mapping[str, tuple[int, int]]) -> Heard:
        if not all(layout().speaker_v8.values()):
            return {"host_prob": None, "note": "没有配置声纹工作区，字幕不标说话人"}
        name = prepared["segment"]
        units = [u for cue, (start, end) in sorted(spans.items(), key=lambda kv: kv[1])
                 for u in speaker_v8.cue_chunks(int(cue), start, end, name)]
        units_doc = {"units": [dict(u, seg=name) for u in units]}
        local = directory / WORKSPACE
        units_local, pred_local = local / "units.json", local / "pred.json"
        same = units_local.exists() and json.loads(units_local.read_text(encoding="utf-8")) == units_doc
        if not (same and pred_local.exists()):
            pred_local.unlink(missing_ok=True)       # never paired with other units: a failed run leaves no reading
            write_json(units_local, units_doc)
            rd = f"{prepared['remote_dir']}/{WORKSPACE}"
            workdir = f"{SPEAKER_V8_ROOT}/discover-{name}"
            remote.put(units_local, f"{rd}/units.json")
            remote.put(SPEAKER_V8_SCRIPT, f"{rd}/speaker_v8_run.sh")
            res = remote.run_script(name=f"discover-voiceprint-{name}", timeout=3600,
                                    body=f"{SPEAKER_V8_ENV}bash {rd}/speaker_v8_run.sh {name} {prepared['path']} {workdir} {rd}/units.json\n")
            if "SPEAKER_V8_DONE" not in res.stdout:
                raise DiscoverError(VOICEPRINT_FAILED, f"{name}: rc={res.rc} {res.stdout[-120:]} {res.stderr[-120:]}")
            try:
                remote.get(f"{workdir}/out/pred.json", pred_local)
            except RuntimeError as exc:
                raise DiscoverError(VOICEPRINT_FAILED, f"{name}: the reading was not fetched: {exc}") from exc
        return {"host_prob": host_prob(units, json.loads(pred_local.read_text(encoding="utf-8")).get("predictions", [])),
                "note": "按声纹标了说话人"}

    return hear


def host_prob(units: Sequence[speaker_v8.SpeakerChunk], predictions: Sequence[Mapping[str, object]]) -> dict[str, float]:
    """Cue id -> the mean host probability of its chunks that the classifier scored."""
    by_id = {str(p["id"]): p for p in predictions}
    per_cue: dict[str, list[float]] = {}
    for unit in units:
        p = by_id.get(unit["id"])
        if p and p.get("ok"):
            per_cue.setdefault(str(unit["cue"]), []).append(float(dict(p.get("probs") or {}).get(speaker_v8.HOST, 0.0)))  # type: ignore[call-overload]
    return {cue: round(sum(v) / len(v), 3) for cue, v in per_cue.items()}


def mark(prob: float | None) -> str:
    """The mark a line carries in the labelling prompt: 她, 别人, or nothing when the voiceprint is not sure."""
    if prob is None:
        return ""
    return "她" if prob >= speaker_v8.V8_MID_MIN_CONF else "别人" if prob <= speaker_v8.GUEST_MAX_HOST_PROB else ""
