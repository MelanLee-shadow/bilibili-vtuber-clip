"""Public boundary: private candidate replay projections are unavailable."""
from __future__ import annotations
import copy
from dataclasses import dataclass
from pathlib import Path
from src.autoslice.candidate_public_text_surface_authority import CandidatePublicTextSurfaceAuthorityError

class C6ReplayPublicTextProjectionError(CandidatePublicTextSurfaceAuthorityError):
    pass

@dataclass(frozen=True, slots=True)
class C6ReplayPublicTextConsumption:
    """Immutable typed result carrying the derived receipt into staging."""
    receipt: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return copy.deepcopy(self.receipt)

def _unavailable(*_args, **_kwargs):
    raise C6ReplayPublicTextProjectionError("C6_REPLAY_PRIVATE_AUTHORITY_UNAVAILABLE")

validate_c6_replay_public_text_consumption_for_fresh_story = _unavailable
validate_c6_replay_public_text_consumption = _unavailable
build_c6_replay_public_text_resolver = _unavailable
