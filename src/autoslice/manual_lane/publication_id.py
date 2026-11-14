"""Which manuscript a clip id belongs to.

A clip made from a finished one is its successor and the same manuscript: the mid-clip cut (trim_run) names it `<cid>_trim`,
a revision of a published clip (revise_run) `<cid>_r<N>`, and the endings stack (`<cid>_trim_r2`).  The package directory name
(`fm-<date>-<slug>`), the owner's chosen title and the published-title exclusion all follow the manuscript, so all of them
ask this one function.  No project imports: title_history also runs on its own on the media host."""

from __future__ import annotations

import re

TRIM_SUFFIX = "_trim"      # the successor clip id's ending (trim_run.SUFFIX)
REVISE_SUFFIX = "_r"       # a revision's ending is this followed by its number, from 2 (revise_run)
_ENDINGS = re.compile(rf"(?:{TRIM_SUFFIX}|{REVISE_SUFFIX}\d+)+$")
_REVISION_ENDING = re.compile(rf"{REVISE_SUFFIX}\d+$")
_LAST_ENDING = re.compile(rf"(?:{TRIM_SUFFIX}|{REVISE_SUFFIX}\d+)$")


def publication_cid(cid: str) -> str:
    """`cid` without the successor endings: the clip id the manuscript is published under."""
    return _ENDINGS.sub("", cid)


def revision_base(cid: str) -> str:
    """`cid` without the last revision ending (a revision of a revision is numbered beside it, not under it)."""
    return _REVISION_ENDING.sub("", cid)


def lineage(cid: str) -> frozenset[str]:
    """`cid` and every clip it was made from, one ending at a time down to the manuscript (`a_trim_r2`: itself, `a_trim`, `a`)."""
    chain = {cid}
    while (earlier := _LAST_ENDING.sub("", cid)) != cid:
        chain.add(cid := earlier)
    return frozenset(chain)


def lineage_ending(cid: str) -> str | None:
    """What the last ending of `cid` is: "trim" (a cut picture), "revise" (a revision), None (no ending: not a successor)."""
    ending = _LAST_ENDING.search(cid)
    if ending is None:
        return None
    return "trim" if ending.group() == TRIM_SUFFIX else "revise"
