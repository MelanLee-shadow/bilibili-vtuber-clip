"""The intro receipt step 5 writes (burn/intro_result.json), as its readers take it."""
from __future__ import annotations

from collections.abc import Mapping


def declared_no_intro(receipt: Mapping[str, object]) -> bool:
    """The receipt the lane writes when the channel's intro manifest says `enabled: false`
    (assets/manual_lane/remote/prepend_intro.py): no intro, offset 0.  Every reader of the intro receipt takes this one
    shape as a finished clip without an intro; whether the channel really has its intro off is the roster check's
    (FMR A6) to say."""
    return (receipt.get("status") == "NO_INTRO" and not receipt.get("intro_id")
            and type(receipt.get("intro_offset_ms")) is int and receipt.get("intro_offset_ms") == 0)
