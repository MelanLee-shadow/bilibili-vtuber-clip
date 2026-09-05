"""OSS stub for the private C3 v3 direct-closure seam."""
from __future__ import annotations

from pathlib import Path


CANDIDATE_ID = "__oss_private_c3_unavailable__"
RECORDING_DATE = ""
V3_RELATIVE_ROOT = Path("out") / "private-c3-unavailable"


class C3V3ClosureError(ValueError):
    """The private C3 v3 closure is unavailable in OSS."""


def _unavailable(*_args, **_kwargs):
    raise C3V3ClosureError("C3_PRIVATE_V3_CLOSURE_UNAVAILABLE")


build_c3_v3_closure = _unavailable
load_c3_v3_authority = _unavailable
consume_c3_successor_pass0 = _unavailable
