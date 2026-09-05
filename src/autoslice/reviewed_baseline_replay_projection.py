"""Small private-projection retention primitives for reviewed-baseline replay."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


def canonical_talk_delivery_basename(
    hook: str, candidate_id: str, *, error: Callable[[str], Exception],
) -> str:
    """Use the ordinary Talk destination rule without exposing a replay knob."""

    from src.autoslice.producer_prepare_result import talk_delivery_basename

    try:
        return talk_delivery_basename(hook, candidate_id)
    except ValueError as exc:
        raise error("REPLAY_DELIVERY_NAME_INVALID") from exc


def exact_candidate_sidecar_target(
    candidate_id: str, package_root: Path, *, role: str, target: Path,
    error: Callable[[str], Exception],
) -> Path:
    """Accept only record-owned sidecars directly below their candidate root."""

    expected = {
        "candidate-chat-authority": f"{candidate_id}.chat-authority.json",
        "candidate-clip-context": f"{candidate_id}.clip-context.json",
    }.get(role)
    if expected is None or target.parent != package_root or target.name != expected:
        raise error("REPLAY_RECORD_MIRROR_BINDING_INVALID")
    return target


@dataclass(frozen=True, slots=True)
class PreparedReplayAfterImage:
    """A staged transaction paired with its sealed private-to-live projection."""

    after: object
    projection: object


def validate_retained_projection(
    projection_root: Path, bindings: Iterable[tuple[str, object]], *,
    safe_directory: Callable[[Path], Path],
    regular_binding: Callable[..., object],
    error: Callable[[str], Exception],
) -> None:
    """Fail closed if a retained create-only projection is replaced or moved."""

    safe_directory(projection_root)
    for name, binding in bindings:
        path = getattr(binding, "path", None)
        if not isinstance(path, Path) or path.parent != projection_root:
            raise error("REPLAY_LIVE_PROJECTION_INVALID")
        try:
            current = regular_binding(path, label=name)
        except Exception as exc:
            raise error("REPLAY_LIVE_PROJECTION_DRIFT") from exc
        if current != binding:
            raise error("REPLAY_LIVE_PROJECTION_DRIFT")
