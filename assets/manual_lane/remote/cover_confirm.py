"""The cover tools' model judgements whose "no" drops something, asked through veto.confirm (lane_rules `veto`):
the judge of a rendered candidate (a veto sends a free candidate away or has a drawn one drawn again) and the readings
that locate the host's face, head or the people's figures (not found: the route is not made).  `ask(n)` is the n-th
asking, (answer text, reused); each asking has its own cache name (`nth`)."""
import json
import re

import veto

JUDGE_FLAGS = ("clean_layers", "identity", "source_faithful", "no_fabrication")


def nth(n):
    """The cache-name suffix of the n-th asking: the first keeps the plain name."""
    return "" if n == 1 else f".{n}"


def judge_axes(verdict, host, needed=()):
    """What in one judge answer vetoes the candidate (title_cover_scoring, step6 cover_guests.roster_vetoes): a flag that
    is not a pass, the host not among the people present, a guest the cover must show (`needed`) not among them.  None
    when the answer is not an object."""
    if not isinstance(verdict, dict):
        return None
    people = verdict.get("people") if isinstance(verdict.get("people"), dict) else {}
    present = people.get("present") if isinstance(people.get("present"), list) else []
    return {**{k: isinstance(verdict.get(k), dict) and verdict[k].get("pass") is True for k in JUDGE_FLAGS},
            "host_present": host in present, **({"needed_present": set(needed) <= set(present)} if needed else {})}


PEOPLE_AXES = ("host_present", "needed_present")     # the axes read off the verdict's `people`


def settled_verdict(verdicts, settled, host, needed=()):
    """One judge verdict from the readings taken: the first reading that agrees with every settled axis as it is (its
    boxes, people and score belong together), else the first judged one with each axis from a reading that says so."""
    seen = [judge_axes(v, host, needed) for v in verdicts]
    want = {axis: vote["pass"] for axis, vote in settled.items()}
    whole = next((v for v, axes in zip(verdicts, seen) if axes == want), None)
    if whole is not None:
        return whole
    merged = dict(next(v for v, axes in zip(verdicts, seen) if axes is not None))
    for axis, vote in settled.items():
        key = "people" if axis in PEOPLE_AXES else axis
        merged[key] = verdicts[vote["from"]].get(key)
    return merged


def confirm_judge(ask, parse, host, needed=()):
    """(answer text, reused, votes): the text of the settled verdict when it took more than one reading, and each axis's
    {pass, fails, judged}.  A single clean reading, or no verdict (then the first answer is kept: a picture nobody
    judged is not accepted either), has votes None.  `needed`: the guests the cover must show; whether the picture
    shows them is the judge's reading of the pixels, confirmed like a veto (who stands where is the tool's own word)."""
    taken, settled = veto.confirm(ask, lambda got: judge_axes(parse(got[0]), host, needed))
    first, reused = taken[0]["answer"]
    if len(taken) == 1 or settled is None:
        return first, reused, None
    verdicts = [parse(t["answer"][0]) if t["answer"] else None for t in taken]
    votes = {axis: {k: vote[k] for k in ("pass", "fails", "judged")} for axis, vote in settled.items()}
    return json.dumps(settled_verdict(verdicts, settled, host, needed), ensure_ascii=False), reused, votes


def confirm_found(ask, found, strict=False):
    """What a reading located (`found(answer)`, None = not there), or None when the first call failed or most readings
    found nothing.  The first asking keeps its name, so a picture that was located once gives the same answer again.
    `strict`: a failed call raises (veto.FAILED) instead, when nothing was located: "not there" is then the readings'
    word, never a call that gave no answer (a guest's pack picture whose face is "not there" leaves her off the cover)."""
    try:
        taken, settled = veto.confirm(ask, lambda got: {"found": found(got[0]) is not None})
    except veto.FAILED:
        if strict:
            raise
        return None
    if settled is None or not settled["found"]["pass"]:
        if strict and (errors := [t["error"] for t in taken if t.get("error")]):
            raise ValueError(f"locating readings failed: {'; '.join(errors)}")
        return None
    return found(taken[settled["found"]["from"]]["answer"][0])


def box_in(answer, key, w, h):
    """The [x0, y0, x1, y1] an answer gives under `key`, inside a w x h picture; None when absent or out of it."""
    match = re.search(r"\{.*\}", answer, re.DOTALL)
    try:
        box = json.loads(match.group(0)).get(key) if match else None
    except (ValueError, AttributeError):
        return None
    if isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box):
        box = [int(round(v)) for v in box]
        if 0 <= box[0] < box[2] <= w and 0 <= box[1] < box[3] <= h:
            return box
    return None


def with_face(head):
    """A (face box, head top) reading counts as found only with its face."""
    return head if head[0] is not None else None
