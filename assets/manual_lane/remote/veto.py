"""One rule for every model judge whose "no" costs something: a free cover candidate sent away, a picture drawn again,
a clip held back.

One reading of a borderline case says no where the next says yes (2026-11-21: the same thumbnail, read seven times, was
called unreadable twice; six stored cover judgements read again gave two other answers).  So a reading that is not a
clean pass is asked again until READINGS are taken, and an axis fails only when most of the judged readings fail it.
A single no with nothing to confirm it is no verdict (the caller decides what no verdict means for it); a single pass
among failed calls stands, since a pass costs nothing to accept.

Standard library only: the cover tools on the media host import it as a sibling (cover_confirm.py, cover_thumb.py); a
lane-side judge that needs it loads this same file.  Which judgements go through it is registered in lane_rules `veto`."""

READINGS = 3
FAILED = (ValueError, OSError)       # a call that did not give an answer (urllib's errors are OSError)


def settle(seen):
    """The verdict of the readings taken.  `seen` holds each reading's {axis: bool}, or None where it could not be
    judged.  Returns {axis: {"pass", "fails", "judged", "from": index of a reading that says so}}, or None."""
    judged = [(i, axes) for i, axes in enumerate(seen) if axes is not None]
    if not judged:
        return None
    if len(judged) == 1:
        i, axes = judged[0]
        if len(seen) > 1 and not all(axes.values()):
            return None                                   # one no, nothing to confirm it
        return {axis: {"pass": ok, "fails": int(not ok), "judged": 1, "from": i} for axis, ok in axes.items()}
    out = {}
    for axis in judged[0][1]:
        votes = [(i, axes[axis]) for i, axes in judged if axis in axes]
        fails = sum(1 for _, ok in votes if not ok)
        ok = fails * 2 <= len(votes)
        out[axis] = {"pass": ok, "fails": fails, "judged": len(votes), "from": next(i for i, v in votes if v is ok)}
    return out


def confirm(read, axes, readings=READINGS):
    """`read(n)` gives the n-th answer (n from 1; every asking must be its own request, so no cache hands the first
    answer back) and may raise FAILED; `axes(answer)` gives {axis: bool}, or None when the answer cannot be judged.
    The first call's failure is the caller's (nothing was read at all).  Returns (taken, verdict): `taken` is every
    reading as {"answer", "axes"} (a failed one {"answer": None, "axes": None, "error"}), `verdict` is settle()'s."""
    first = read(1)
    taken = [{"answer": first, "axes": axes(first)}]
    if taken[0]["axes"] is None or not all(taken[0]["axes"].values()):
        for n in range(2, readings + 1):
            try:
                again = read(n)
            except FAILED as exc:
                taken.append({"answer": None, "axes": None, "error": f"{type(exc).__name__}: {exc}"})
                continue
            taken.append({"answer": again, "axes": axes(again)})
    return taken, settle([t["axes"] for t in taken])
