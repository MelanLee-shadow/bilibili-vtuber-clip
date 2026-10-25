"""Source-scoped native ledger/cache inheritance for private prepared recovery."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from src.autoslice.native_audio_budget_receipt import (
    _no_links,
    _write_atomic,
    BudgetReceiptPersistenceError,
    _require_budget_history_extension,
    _validate_budget_history,
    _validate_existing,
    exclusive_native_audio_budget,
    resume_native_audio_budget,
)
from src.autoslice.producer_media import _write_json_atomic
from src.autoslice.package_import import atomic_write_bytes
from src.autoslice.shadow_review import _sha256
from src.autoslice.subtitle_audio_evidence import CONTRACT, _cached_secondary
from src.autoslice.supplement_audio_budget import ensure_budget


@dataclass(frozen=True)
class PreparedNativeAudioState:
    cache_root: Path
    budget_receipt: Path
    history_cache_roots: tuple[Path, ...] = ()


def _read(path: Path) -> dict:
    _no_links(path)
    if not path.is_file():
        raise ValueError("PREPARED_NATIVE_PARENT_MISSING")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("PREPARED_NATIVE_PARENT_INVALID")
    return value


def _present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _require_bound(path: Path, digest: str) -> None:
    _no_links(path)
    if not path.is_file() or _sha256(path) != digest.removeprefix("sha256:"):
        raise ValueError("PREPARED_NATIVE_PARENT_DRIFT")



def _range_acquisition_inputs(
    *, parent: Path, source: Path, source_sha: str, attempt: dict
) -> dict[str, str]:
    """Validate original AGY acquisitions, independently of current cue projection."""
    from types import SimpleNamespace
    from src.autoslice.entity_audio_verifier import _source_stat_binding
    from src.autoslice.gemini_range_context import _cache_metadata_valid, _digest_json

    root = (parent / "gemini-range-acquisition" / source_sha[:20]
            / f"{attempt['start_ms']}-{attempt['end_ms']}")
    _no_links(root)
    inventory = {}
    for path in sorted(root.glob("*/*/acquisition.json")):
        metadata = _read(path)
        original = dict(metadata)
        if original.pop("metadata_sha256", None) != _digest_json(original):
            raise ValueError("PREPARED_NATIVE_RANGE_ACQUISITION_DRIFT")
        if (metadata.get("provider"), metadata.get("model")) != (
                attempt["provider"], attempt["model"]):
            continue
        prompt_record = metadata["paths"]["prompt"]
        prompt_path = Path(prompt_record["path"])
        _require_bound(prompt_path, prompt_record["sha256"])
        prompt = prompt_path.read_text(encoding="utf-8")
        valid = _cache_metadata_valid(path,
            verifier=SimpleNamespace(source_media=source, source_sha256=source_sha,
                source_stat_binding=_source_stat_binding(source), model=attempt["model"]),
            geometry={"source_target_start_ms": attempt["start_ms"],
                      "source_target_end_ms": attempt["end_ms"]},
            prompt=prompt, prompt_sha=prompt_record["sha256"])
        if valid is None:
            raise ValueError("PREPARED_NATIVE_RANGE_ACQUISITION_DRIFT")
        for artifact in path.parent.rglob("*"):
            if artifact.is_file() and artifact.suffix != ".lock":
                _no_links(artifact)
                inventory[str(artifact.relative_to(parent))] = "sha256:" + _sha256(artifact)
    if not inventory:
        raise ValueError("PREPARED_NATIVE_RANGE_ACQUISITION_MISSING")
    # These bytes restore the original cache protocol. No current range,
    # acquisition, target consumption or subtitle decision is created here.
    return inventory


def _parent_inputs(
    *, parent: Path, cid: str, source: Path, source_sha: str
) -> tuple[dict | None, dict[str, str]]:
    budget_path = parent / "native-audio-budget.json"
    checkpoint_path = parent / f"{cid}.partial-source-text-checkpoint.json"
    checkpoint = _read(checkpoint_path) if _present(checkpoint_path) else None
    frozen = checkpoint.get("budget") if checkpoint else None
    if not _present(budget_path):
        if frozen or _present(parent / "native-exact"):
            raise ValueError("PREPARED_NATIVE_BUDGET_MISSING_WITH_OBSERVATIONS")
        return None, {}
    budget = _read(budget_path)
    _validate_existing(budget, source_media_sha256=source_sha)
    _validate_budget_history(budget["budget"])
    if budget["budget"]["source_media"] != str(source):
        raise ValueError("PREPARED_NATIVE_SOURCE_DRIFT")
    if frozen:
        if (
            checkpoint.get("candidate_id") != cid
            or checkpoint.get("release_authorized") is not False
            or checkpoint.get("padded") != {"path": str(source), "sha256": "sha256:" + source_sha}
        ):
            raise ValueError("PREPARED_NATIVE_CHECKPOINT_SCOPE_DRIFT")
        path = Path(frozen["path"])
        _require_bound(path, frozen["sha256"])
        previous = _read(path)
        _validate_existing(previous, source_media_sha256=source_sha)
        _require_budget_history_extension(previous, budget)
    inventory = {"native-audio-budget.json": "sha256:" + _sha256(budget_path)}
    for attempt in budget["budget"]["attempts"]:
        if attempt["status"] not in {"OBSERVED", "TEXT_UNLOCATED"}:
            continue
        provider, model = attempt["provider"], attempt["model"]
        cache_parent = parent
        history = budget["budget"].get("account_history")
        if history and attempt.get("history_branch_sha256") == history["incoming"]["receipt_sha256"]:
            cache_parent = parent / "history-cache" / history["plan"]["incoming"]["sha256"][7:]
        if provider == "agy":
            acquired = _range_acquisition_inputs(
                parent=cache_parent, source=source, source_sha=source_sha, attempt=attempt)
            inventory.update({str((cache_parent / relative).relative_to(parent)): digest
                              for relative, digest in acquired.items()})
            continue
        job = (
            Path("native-exact")
            / provider
            / f"{source_sha[:20]}-{attempt['start_ms']}-{attempt['end_ms']}"
        )
        audio = cache_parent / job / "input.mp3"
        receipt = audio.with_suffix(f".audio-evidence.{provider}.json")
        _no_links(audio)
        _no_links(receipt)
        if not audio.is_file() or not receipt.is_file():
            raise ValueError("PREPARED_NATIVE_COMPLETED_CACHE_MISSING")
        binding = {
            "contract": CONTRACT,
            "provider": provider,
            "model": model,
            "input_audio_sha256": _sha256(audio),
            "duration_ms": attempt["duration_ms"],
            "candidate_exposure": "none",
        }
        if _cached_secondary(path=receipt, binding=binding, exact_cue=True) is None:
            raise ValueError("PREPARED_NATIVE_COMPLETED_CACHE_DRIFT")
        inventory[str(audio.relative_to(parent))] = "sha256:" + _sha256(audio)
        inventory[str(receipt.relative_to(parent))] = "sha256:" + _sha256(receipt)
    return budget, inventory


def _reconcile_registered_fork(
    fork_error: BudgetReceiptPersistenceError, *, account: Path, manifest: dict,
    inventory: dict[str, str], current: dict, incoming: dict, receipt_path: Path,
    parent: Path, cid: str, source_sha: str,
) -> dict:
    """Automatic fork reconciliation from the account's own registered ancestry.

    The ordinary entry derives the byte-bound plan from ``parent.json`` and the
    archived parent receipts; nobody hand-writes a plan into the SPEC. When no
    sealed registered ancestor proves the fork, the original prefix rejection
    is raised unchanged and every file staged by this attempt is withdrawn.
    """
    from src.autoslice.native_audio_history_reconciliation import (
        build_reconciliation, discover_registered_ancestor, stage_discovered_plan,
    )

    discovered = discover_registered_ancestor(
        account=account, manifest=manifest, current=current, incoming=incoming,
        receipt_path=receipt_path, incoming_path=parent / "native-audio-budget.json",
        cid=cid, source_sha=source_sha)
    if discovered is None:
        raise fork_error
    created: list[Path] = []
    try:
        reference, created = stage_discovered_plan(account=account, discovered=discovered)
        return build_reconciliation(
            reference=reference, current=current, incoming=incoming, account=account,
            manifest=manifest, inventory=inventory, cid=cid, source_sha=source_sha,
            parent=parent)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise fork_error from exc


def inherit_prepared_native_audio(
    *, cid: str, parent: Path, source: Path, runtime_root: Path, reconciliation: dict | None = None
) -> PreparedNativeAudioState:
    """Join one canonical account across output roots; never write the parent.

    The runtime base selects the state namespace just as it selects production
    attempt accounting. Changing the producer output root does not select a new
    ledger. All parents and all retries must extend the same source history.
    """
    _no_links(source)
    source_sha = _sha256(source)
    original, inventory = _parent_inputs(
        parent=parent, cid=cid, source=source, source_sha=source_sha,
    )
    base = Path(os.environ.get("AUTOSLICE_BASE", str(runtime_root)))
    if not base.is_absolute():
        raise ValueError("PREPARED_NATIVE_RUNTIME_ROOT_INVALID")
    account = base / ".native-audio-resume" / cid / source_sha
    _no_links(account)
    account.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt_path = account / "native-audio-budget.json"
    manifest_path = account / "parent.json"
    limits = original["budget"] if original else {}
    budget = ensure_budget(
        source,
        max_windows=limits.get("max_windows"),
        max_audio_ms=limits.get("max_audio_ms"),
        enforce_limits=limits.get("enforce_limits"),
    )
    with budget.lock, exclusive_native_audio_budget(receipt_path):
        manifest = (
            _read(manifest_path)
            if _present(manifest_path)
            else {
                "schema_version": "prepared-native-audio-parents.v1",
                "candidate_id": cid,
                "source_media_sha256": source_sha,
                "parents": [],
                "cache_preimages": {},
            }
        )
        if (
            manifest.get("schema_version") != "prepared-native-audio-parents.v1"
            or manifest.get("candidate_id") != cid
            or manifest.get("source_media_sha256") != source_sha
        ):
            raise ValueError("PREPARED_NATIVE_ACCOUNT_SCOPE_DRIFT")
        for relative, digest in manifest["cache_preimages"].items():
            _require_bound(account / relative, digest)
        previous_parent = next(
            (row for row in manifest["parents"] if row["path"] == str(parent)), None
        )
        if previous_parent and previous_parent["budget_sha256"] != inventory.get(
            "native-audio-budget.json"
        ):
            # A real extended parent is checked against its frozen predecessor;
            # a missing or truncated parent may not silently become a new seed.
            previous_path = account / "parents" / (previous_parent["budget_sha256"][7:] + ".json")
            _require_bound(previous_path, previous_parent["budget_sha256"])
            previous = _read(previous_path)
            if original is None:
                raise ValueError("PREPARED_NATIVE_PARENT_BUDGET_DISAPPEARED")
            _require_budget_history_extension(previous, original)
        current = _read(receipt_path) if _present(receipt_path) else None
        planned = None
        if current is not None:
            _validate_existing(current, source_media_sha256=source_sha)
            _validate_budget_history(current["budget"])
            existing_history = current["budget"].get("account_history")
            if reconciliation is not None and existing_history:
                from src.autoslice.native_audio_history_reconciliation import read_bound
                _, _, plan = read_bound(reconciliation)
                if plan != existing_history["plan"]:
                    raise ValueError("PREPARED_NATIVE_PARENT_DRIFT")
            if original is not None:
                try:
                    if current["revision"] >= original["revision"]:
                        _require_budget_history_extension(original, current)
                    else:
                        _require_budget_history_extension(current, original)
                        planned = original
                except BudgetReceiptPersistenceError as fork_error:
                    if existing_history:
                        raise
                    from src.autoslice.native_audio_history_reconciliation import build_reconciliation
                    if reconciliation is not None:
                        planned = build_reconciliation(
                            reference=reconciliation,current=current,incoming=original,
                            account=account,manifest=manifest,inventory=inventory,cid=cid,
                            source_sha=source_sha,parent=parent)
                    else:
                        planned = _reconcile_registered_fork(
                            fork_error, account=account, manifest=manifest,
                            inventory=inventory, current=current, incoming=original,
                            receipt_path=receipt_path, parent=parent, cid=cid,
                            source_sha=source_sha)
        elif original is not None:
            if manifest["parents"]:
                raise ValueError("PREPARED_NATIVE_ACCOUNT_LEDGER_DISAPPEARED")
            if reconciliation is not None:
                raise ValueError("PREPARED_NATIVE_ACCOUNT_SCOPE_DRIFT")
            planned = original
        history = (planned or current or {}).get("budget", {}).get("account_history")
        # Validate every occupied destination before any migration write.
        # Different observations on a fork are retained under their own root;
        # they never overwrite the canonical observation at the same window.
        fork_root = (account / "history-cache" / history["plan"]["incoming"]["sha256"][7:]
                     if history else None)
        fork_parent = bool(history and original == history["incoming"])
        destinations = []
        for relative, digest in inventory.items():
            if relative == "native-audio-budget.json":
                continue
            target = account / relative
            _no_links(target)
            if target.exists() and "sha256:" + _sha256(target) != digest:
                if not fork_parent:
                    raise ValueError("PREPARED_NATIVE_PARENT_DRIFT")
            else:
                destinations.append((target, parent / relative, digest))
            if fork_parent:
                destinations.append((fork_root / relative, parent / relative, digest))
        for target, origin, digest in destinations:
            _no_links(target)
            if target.exists():
                _require_bound(target, digest)
        if planned and history and not current["budget"].get("account_history"):
            # Keep exact original JSON bytes before installing the v3 view.
            for name in ("canonical", "incoming", "ancestor"):
                from src.autoslice.native_audio_history_reconciliation import read_bound
                _, raw, parsed = read_bound(history["plan"][name])
                frozen_digest = history["plan"][name]["sha256"]
                archive = account / "parents" / (frozen_digest[7:] + ".json")
                _no_links(archive)
                archive.parent.mkdir(exist_ok=True)
                if archive.exists():
                    _require_bound(archive, frozen_digest)
                else:
                    atomic_write_bytes(archive, raw)
                manifest["cache_preimages"][str(archive.relative_to(account))] = frozen_digest
        for target, origin, digest in destinations:
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                atomic_write_bytes(target, origin.read_bytes())
                _require_bound(target, digest)
            manifest["cache_preimages"][str(target.relative_to(account))] = digest
        if original is not None:
            digest = inventory["native-audio-budget.json"]
            archive = account / "parents" / (digest[7:] + ".json")
            archive.parent.mkdir(exist_ok=True)
            _no_links(archive)
            if archive.exists():
                _require_bound(archive, digest)
            else:
                atomic_write_bytes(archive, (parent / "native-audio-budget.json").read_bytes())
                _require_bound(archive, digest)
            parent_binding = {"path": str(parent), "budget_sha256": digest}
            if parent_binding not in manifest["parents"]:
                manifest["parents"].append(parent_binding)
        if not _present(manifest_path) or _read(manifest_path) != manifest:
            _write_json_atomic(manifest_path, manifest)
        if planned is not None:
            _write_atomic(receipt_path, planned)
        resume_native_audio_budget(
            source_media=source,
            source_media_sha256=source_sha,
            receipt_path=receipt_path,
            require_existing=original is not None or current is not None,
        )
    roots = (fork_root,) if fork_root is not None else ()
    return PreparedNativeAudioState(account, receipt_path, roots)


def join_own_interrupted_account(
    state: PreparedNativeAudioState, *, cid: str, out_root: Path, source: Path, runtime_root: Path,
) -> tuple[PreparedNativeAudioState, dict | None]:
    """Join this output root's own earlier account (an interrupted attempt).

    41:1540-1541 / 40:57: occupation and real observations of the stopped run
    are never re-bought, refunded or reset. The ordinary inheritance is the
    proof: the account must extend the joined ledger as an exact prefix (or a
    registered fork) and every OBSERVED/TEXT_UNLOCATED attempt must still have
    its complete original audio/receipt/range files. Nothing is written to the
    stopped run's files. When the proof does not hold, nothing is joined and the
    account stays exactly as inherited before (the original refusal).
    """
    own = Path(out_root) / "native-audio-budget.json"
    if (not own.is_file() or own.is_symlink()
            or own.absolute() == Path(state.budget_receipt).absolute()):
        return state, None
    try:
        joined = inherit_prepared_native_audio(
            cid=cid, parent=Path(out_root), source=source, runtime_root=runtime_root)
    except (ValueError, OSError, KeyError, TypeError, BudgetReceiptPersistenceError) as exc:
        return state, {"schema_version": "own-interrupted-native-account.v1", "status": "NOT_JOINED",
                       "path": str(own), "reason_code": (str(exc) or type(exc).__name__)[:200]}
    return joined, {"schema_version": "own-interrupted-native-account.v1", "status": "JOINED",
                    "path": str(own), "budget_sha256": "sha256:" + _sha256(own)}
