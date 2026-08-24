"""C2-only private authority adapter for the reviewed-baseline replay lane.

The predecessor record is immutable and intentionally retains its canonical
delivery locator strings.  A private runtime cannot rewrite those bytes
without breaking the record SHA join.  This adapter supplies those strings
only as comparison expectations while denying every non-private file read.
"""
from __future__ import annotations

from dataclasses import is_dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping
from types import SimpleNamespace

from src.autoslice.reviewed_baseline_replay_authority import (
    RecordBoundFinalizerAuthority,
    resolve_record_bound_finalizer_authority,
)


C2_CANDIDATE_ID = "auto_203011_328_389"
C2_RECORDING_DATE = "2026-08-13"


def _canonical_locator_root(*, plan: Any, record: Mapping[str, object]) -> Path:
    """Extract the two immutable old-record locator strings without opening them."""

    chat = record.get("chat_authority_audit_path")
    clip = record.get("clip_context_path")
    if not isinstance(chat, str) or not isinstance(clip, str):
        raise ValueError("C2_PRIVATE_AUTHORITY_CANONICAL_LOCATOR_MISSING")
    chat_path, clip_path = Path(chat), Path(clip)
    root = chat_path.parent
    if (
        not chat_path.is_absolute()
        or not clip_path.is_absolute()
        or clip_path.parent != root
        or chat_path.name != f"{plan.candidate_id}.chat-authority.json"
        or clip_path.name != f"{plan.candidate_id}.clip-context.json"
    ):
        raise ValueError("C2_PRIVATE_AUTHORITY_CANONICAL_LOCATOR_DRIFT")
    return root


def resolve_c2_private_record_authority(
    *, plan: Any, record_binding: object, record: Mapping[str, object],
    runtime_authority_root: Path, source_media_sha256: str,
    regular_binding: Callable[..., object], safe_directory: Callable[[Path], Path],
    load_json: Callable[..., dict[str, Any]], error: Callable[[str], Exception],
) -> RecordBoundFinalizerAuthority:
    """Resolve C2 sidecars without ever dereferencing its canonical `/opt` strings."""

    if plan.candidate_id != C2_CANDIDATE_ID or plan.date != C2_RECORDING_DATE:
        raise error("C2_PRIVATE_AUTHORITY_CANDIDATE_SCOPE_INVALID")
    runtime = safe_directory(Path(runtime_authority_root))
    story = record.get("story_contract")
    if not isinstance(story, Mapping) or story.get("candidate_id") != plan.candidate_id:
        raise error("C2_PRIVATE_AUTHORITY_RECORD_CANDIDATE_DRIFT")
    try:
        canonical_root = _canonical_locator_root(plan=plan, record=record)
    except ValueError as exc:
        raise error(str(exc)) from exc
    if canonical_root == plan.package_root or canonical_root.is_relative_to(runtime):
        raise error("C2_PRIVATE_AUTHORITY_CANONICAL_LOCATOR_DRIFT")

    def private_binding(path: Path, *, label: str) -> object:
        candidate = Path(path).absolute()
        if not candidate.is_relative_to(runtime):
            raise error("C2_PRIVATE_AUTHORITY_READ_OUTSIDE_RUNTIME")
        return regular_binding(candidate, label=label)

    def private_directory(path: Path) -> Path:
        candidate = safe_directory(Path(path))
        if not candidate.is_relative_to(runtime):
            raise error("C2_PRIVATE_AUTHORITY_READ_OUTSIDE_RUNTIME")
        return candidate

    # The resolver's portable-record check compares immutable locator strings
    # against `plan.package_root`.  Only that comparison gets a proxy; all
    # paths actually passed to `private_binding` remain under `runtime`.
    proxy = (
        replace(plan, package_root=canonical_root)
        if is_dataclass(plan)
        else SimpleNamespace(
            candidate_id=plan.candidate_id, date=plan.date,
            package_root=canonical_root,
        )
    )
    authority = resolve_record_bound_finalizer_authority(
        plan=proxy, record_binding=record_binding, record=record,
        runtime_authority_root=runtime, source_media_sha256=source_media_sha256,
        regular_binding=private_binding, safe_directory=private_directory,
        load_json=load_json, error=error,
    )
    for binding in (authority.chat, authority.clip_context):
        path = Path(getattr(binding, "path"))
        if not path.is_relative_to(runtime):
            raise error("C2_PRIVATE_AUTHORITY_RETURN_OUTSIDE_RUNTIME")
    return authority
