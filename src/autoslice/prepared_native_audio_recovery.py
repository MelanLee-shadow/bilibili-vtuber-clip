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


def _require_registered_acquisitions(*, parent: Path, account: Path, manifest: dict) -> None:
    """Registered metadata cannot become a newly qualified historical hole.

    A retained acquisition binds its original writer location through the
    audio_source record. Use that sealed record, not today's directory contents
    or provider/model match, to check an output being inherited again. Partial
    acquisitions already registered as unusable carry explicit absolute paths.
    """
    registered = {}
    for relative, digest in manifest.get('cache_preimages', {}).items():
        saved = account / relative
        if saved.name != 'acquisition.json':
            continue
        _require_bound(saved, digest)
        if parent == account:
            original = saved
        else:
            metadata = _read(saved)
            original = Path(metadata['paths']['audio_source']['path']).with_name('acquisition.json')
        registered[original] = digest
    for record in manifest.get('unusable_caches', {}).values():
        registered.update({Path(path): digest
                           for path, digest in record.get('unusable_acquisitions', {}).items()})
    for path, digest in registered.items():
        if parent != account and not path.is_relative_to(parent):
            continue
        _no_links(path)
        if not path.is_file():
            raise ValueError('PREPARED_NATIVE_RANGE_ACQUISITION_MISSING')
        if 'sha256:' + _sha256(path) != digest:
            raise ValueError('PREPARED_NATIVE_RANGE_ACQUISITION_DRIFT')


def _unavailable_cache(*, root: Path, relative: str, source_sha: str, budget: dict,
                       manifest: dict | None = None) -> dict | None:
    """Read eligibility emitted by inheritance; never infer it from absence."""
    path = root / 'parent.json'
    if manifest is None:
        manifest = _read(path) if _present(path) else {}
    record = manifest.get('unusable_caches', {}).get(relative)
    if record is None:
        return None
    if (manifest.get('schema_version') != 'prepared-native-audio-parents.v1'
            or manifest.get('source_media_sha256') != source_sha
            or not record.get('attempts')
            or not any(p.get('path') == record['parent']
                and p.get('budget_sha256') == record['budget_sha256']
                for p in manifest['parents'])
            or any(a not in budget['attempts'] or a['status'] not in {'OBSERVED', 'TEXT_UNLOCATED'}
                   for a in record['attempts'])):
        raise ValueError('PREPARED_NATIVE_UNAVAILABLE_CACHE_IDENTITY_MISSING')
    for original, digest in record['present'].items():
        _no_links(Path(original))
        if _present(Path(original)):
            _require_bound(Path(original), digest)
    return record



def _record_unavailable(*, unusable: dict | None, relative: str, parent: Path,
                        budget_sha: str, attempt: dict, present: dict[str, str],
                        acquisitions: dict[str, str] | None = None) -> None:
    if unusable is None:
        return
    record = unusable.setdefault(relative, {
        'parent': str(parent), 'budget_sha256': budget_sha, 'attempts': [],
        'present': {}, 'reason_code': 'NATIVE_CACHE_REACQUISITION_REQUIRED',
    })
    if record['parent'] != str(parent) or record['budget_sha256'] != budget_sha:
        raise ValueError('PREPARED_NATIVE_UNAVAILABLE_CACHE_IDENTITY_MISSING')
    if attempt not in record['attempts']:
        record['attempts'].append(dict(attempt))
    record['present'].update(present)
    if acquisitions is not None:
        record.setdefault('unusable_acquisitions', {}).update(acquisitions)


def _already_unavailable(*, root, relative, record, source_sha, budget, manifest):
    saved = _unavailable_cache(root=root, relative=relative, source_sha=source_sha,
                              budget=budget, manifest=manifest)
    return (saved is not None and all(a in saved['attempts'] for a in record['attempts'])
            and all(saved.get('unusable_acquisitions', {}).get(path) == digest
                    for path, digest in record.get('unusable_acquisitions', {}).items()))


def qualify_existing_native_audio_account(*, cid, account, source, runtime_root):
    """SOURCE's inherited-account entry uses the same missing-cache constructor.

    Already qualified accounts stay read-only. Missing registered preimages keep
    the original strict consumer rejection; only unregistered historical cache
    holes can first acquire eligibility through ordinary inheritance.
    """
    manifest = _read(account / 'parent.json')
    if any(not (account / relative).is_file() or (account / relative).is_symlink()
           for relative in manifest['cache_preimages']):
        return
    source_sha = _sha256(source)
    missing = {}
    receipt, _ = _parent_inputs(parent=account, cid=cid, source=source,
        source_sha=source_sha, unusable=missing, allow_unavailable_cache=True)
    if any(not _already_unavailable(root=account, relative=relative, record=record,
            source_sha=source_sha, budget=receipt['budget'], manifest=manifest)
           for relative, record in missing.items()):
        inherit_prepared_native_audio(cid=cid, parent=account, source=source, runtime_root=runtime_root)


def _range_acquisition_inputs(
    *, parent: Path, source: Path, source_sha: str, attempt: dict,
    account_parent: Path | None = None, budget_sha: str = '',
    allow_unavailable_cache: bool = False, unusable: dict | None = None,
    origins: dict[str, Path] | None = None, budget: dict | None = None,
) -> dict[str, str]:
    """Validate original range acquisitions, independently of current cue projection."""
    from types import SimpleNamespace
    from src.autoslice.entity_audio_verifier import _source_stat_binding
    from src.autoslice.gemini_range_context import _cache_metadata_valid, _digest_json

    root = (parent / "gemini-range-acquisition" / source_sha[:20]
            / f"{attempt['start_ms']}-{attempt['end_ms']}")
    _no_links(root)
    account_parent = account_parent or parent
    relative = str(root.relative_to(account_parent))
    recorded = (_unavailable_cache(root=account_parent, relative=relative,
                source_sha=source_sha, budget=budget)
                if not allow_unavailable_cache and budget is not None else None)
    inventory = {}
    present = {}
    acquisitions = {}
    missing = False
    matched = False
    # Symlinks and non-regular occupied artifacts cannot be hidden by a missing
    # acquisition or by a different provider/model in the same window.
    for artifact in sorted(root.rglob('*')):
        _no_links(artifact)
        if artifact.is_file():
            if artifact.suffix != '.lock':
                present[str(artifact)] = 'sha256:' + _sha256(artifact)
        elif not artifact.is_dir():
            raise ValueError('PREPARED_NATIVE_RANGE_ACQUISITION_DRIFT')
    for path in sorted(root.glob("*/*/acquisition.json")):
        metadata = _read(path)
        original = dict(metadata)
        if original.pop("metadata_sha256", None) != _digest_json(original):
            raise ValueError("PREPARED_NATIVE_RANGE_ACQUISITION_DRIFT")
        if (metadata.get("provider"), metadata.get("model")) != (
                attempt["provider"], attempt["model"]):
            continue
        matched = True
        prompt_record = metadata["paths"]["prompt"]
        prompt_path = Path(prompt_record["path"])
        _no_links(prompt_path)
        prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.is_file() else None
        unavailable: set[str] = set()
        resolved: dict[str, Path] = {}
        valid = _cache_metadata_valid(path,
            verifier=SimpleNamespace(source_media=source, source_sha256=source_sha,
                source_stat_binding=_source_stat_binding(source), model=attempt["model"]),
            geometry={"source_target_start_ms": attempt["start_ms"],
                      "source_target_end_ms": attempt["end_ms"]},
            prompt=prompt, prompt_sha=prompt_record["sha256"],
            inherited_missing=unavailable, resolved=resolved,
            expected_pair=(attempt['provider'], attempt['model']), original_contract=True)
        if valid is None:
            raise ValueError("PREPARED_NATIVE_RANGE_ACQUISITION_DRIFT")
        for record in [*metadata['paths'].values(), metadata.get('response_source')]:
            if isinstance(record, dict) and isinstance(record.get('path'), str):
                artifact = Path(record['path'])
                if artifact.is_file():
                    _require_bound(artifact, record['sha256'])
                    present[str(artifact)] = 'sha256:' + _sha256(artifact)
        missing = missing or bool(unavailable)
        if unavailable:
            acquisitions[str(path)] = 'sha256:' + _sha256(path)
            continue  # retain other complete acquisitions in this same window
        for original, recovered in resolved.items():
            if origins is not None and Path(original).is_relative_to(parent):
                origins[str(Path(original).relative_to(account_parent))] = recovered
                inventory[str(Path(original).relative_to(parent))] = 'sha256:' + _sha256(recovered)
        for artifact in path.parent.rglob("*"):
            if artifact.is_file() and artifact.suffix != ".lock":
                _no_links(artifact)
                inventory[str(artifact.relative_to(parent))] = "sha256:" + _sha256(artifact)
    if not matched or missing:
        if not allow_unavailable_cache and (recorded is None or attempt not in recorded['attempts']):
            raise ValueError("PREPARED_NATIVE_RANGE_ACQUISITION_MISSING")
        _record_unavailable(unusable=unusable, relative=relative, parent=account_parent,
            budget_sha=budget_sha, attempt=attempt, present=present, acquisitions=acquisitions)
        return inventory
    # These bytes restore the original cache protocol. No current range,
    # acquisition, target consumption or subtitle decision is created here.
    return inventory


def _parent_inputs(
    *, parent: Path, cid: str, source: Path, source_sha: str,
    origins: dict[str, Path] | None = None, unusable: dict | None = None,
    allow_unavailable_cache: bool = False,
) -> tuple[dict | None, dict[str, str]]:
    budget_path = parent / "native-audio-budget.json"
    checkpoint_path = parent / f"{cid}.partial-source-text-checkpoint.json"
    checkpoint = _read(checkpoint_path) if _present(checkpoint_path) else None
    frozen = checkpoint.get("budget") if checkpoint else None
    if not _present(budget_path):
        if frozen or _present(parent / "native-exact") or _present(parent / 'gemini-range-acquisition'):
            raise ValueError("PREPARED_NATIVE_BUDGET_MISSING_WITH_OBSERVATIONS")
        return None, {}
    budget = _read(budget_path)
    _validate_existing(budget, source_media_sha256=source_sha)
    _validate_budget_history(budget["budget"])
    if _present(parent / 'parent.json'):
        _require_registered_acquisitions(parent=parent, account=parent,
                                        manifest=_read(parent / 'parent.json'))
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
        from src.autoslice.native_audio_history_reconciliation import cache_branches
        cache_digest = cache_branches(history).get(attempt.get("history_branch_sha256"))
        if cache_digest:
            cache_parent = parent / "history-cache" / cache_digest[7:]
        if provider in {"agy", "gemini_api"}:
            acquired = _range_acquisition_inputs(
                parent=cache_parent, source=source, source_sha=source_sha, attempt=attempt,
                account_parent=parent, budget_sha=inventory['native-audio-budget.json'],
                allow_unavailable_cache=allow_unavailable_cache, unusable=unusable,
                origins=origins, budget=budget['budget'])
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
        if _present(audio) and not audio.is_file():
            raise ValueError("PREPARED_NATIVE_COMPLETED_CACHE_DRIFT")
        # Only inheritance can first declare a cache unavailable. A later
        # read-only checkpoint may consume that explicit declaration, but
        # cannot use absence to excuse a newly damaged canonical cache.
        if not allow_unavailable_cache and (not audio.is_file() or not receipt.is_file()):
            recorded = _unavailable_cache(root=parent, relative=str(audio.parent.relative_to(parent)),
                                         source_sha=source_sha, budget=budget['budget'])
            if recorded is None or attempt not in recorded['attempts']:
                raise ValueError("PREPARED_NATIVE_COMPLETED_CACHE_MISSING")
        # A dispatch remains historical fact when retention removed its cache.
        # Validate every remaining receipt before treating absence as recoverable;
        # missing media must never hide changed non-media evidence.
        try:
            saved = _read(receipt) if _present(receipt) else None
        except (ValueError, TypeError) as exc:
            raise ValueError("PREPARED_NATIVE_COMPLETED_CACHE_DRIFT") from exc
        saved_binding = saved.get('binding') if saved is not None else None
        audio_sha = saved_binding.get('input_audio_sha256') if isinstance(saved_binding, dict) else None
        if saved is not None and (not isinstance(audio_sha, str) or len(audio_sha) != 64
                                  or any(c not in "0123456789abcdef" for c in audio_sha)):
            raise ValueError("PREPARED_NATIVE_COMPLETED_CACHE_DRIFT")
        binding = {
            "contract": CONTRACT,
            "provider": provider,
            "model": model,
            "input_audio_sha256": audio_sha,
            "duration_ms": attempt["duration_ms"],
            "candidate_exposure": "none",
        }
        if saved is not None and _cached_secondary(path=receipt, binding=binding, exact_cue=True) is None:
            raise ValueError("PREPARED_NATIVE_COMPLETED_CACHE_DRIFT")
        resolved = audio
        if saved is not None and not audio.exists():
            from src.autoslice.bound_media_recovery import (
                AudioRecipe, BoundMediaUnavailable, Source, resolve_bound_media,
            )
            try:
                resolved = resolve_bound_media(audio, audio_sha,
                    root=Path(os.environ['AUTOSLICE_BASE']) if os.environ.get('AUTOSLICE_BASE') else None,
                    recipe=AudioRecipe(Source(source, source_sha), attempt['start_ms'],
                                       attempt['end_ms'], 'native_exact_mp3'))
            except BoundMediaUnavailable:
                resolved = audio
        if audio.exists() and saved is not None and _sha256(audio) != audio_sha:
            raise ValueError("PREPARED_NATIVE_COMPLETED_CACHE_DRIFT")
        if saved is None or not resolved.is_file():
            _record_unavailable(unusable=unusable, relative=str(audio.parent.relative_to(parent)),
                parent=parent, budget_sha=inventory['native-audio-budget.json'], attempt=attempt,
                present={str(p): 'sha256:' + _sha256(p) for p in (audio, receipt) if p.is_file()})
            continue
        _no_links(resolved)
        if _sha256(resolved) != audio_sha:
            raise ValueError("PREPARED_NATIVE_COMPLETED_CACHE_DRIFT")
        audio_relative = str(audio.relative_to(parent))
        inventory[audio_relative] = "sha256:" + audio_sha
        if origins is not None:
            origins[audio_relative] = resolved
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
    origins: dict[str, Path] = {}
    unusable: dict = {}
    original, inventory = _parent_inputs(
        parent=parent, cid=cid, source=source, source_sha=source_sha,
        origins=origins, unusable=unusable, allow_unavailable_cache=True,
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
        _require_registered_acquisitions(parent=parent, account=account, manifest=manifest)
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
        current_unusable: dict = {}
        if current is not None:
            _validate_existing(current, source_media_sha256=source_sha)
            _validate_budget_history(current["budget"])
            for relative in manifest.get('unusable_caches', {}):
                _unavailable_cache(root=account, relative=relative, source_sha=source_sha,
                    budget=current['budget'], manifest=manifest)
            if parent != account:
                # Joining an output also inherits the destination's historical
                # attempts. Qualify its unregistered holes under the same
                # constructor, after enforcing all registered byte preimages.
                # A cold reader still cannot infer eligibility from absence.
                _parent_inputs(parent=account, cid=cid, source=source, source_sha=source_sha,
                    unusable=current_unusable, allow_unavailable_cache=True)
                current_unusable = {relative: record for relative, record in current_unusable.items()
                    if not _already_unavailable(root=account, relative=relative, record=record,
                        source_sha=source_sha, budget=current['budget'], manifest=manifest)}
            if parent == account:
                # A re-read of an already declared absence is not a new parent
                # or a reason to rewrite provenance/forget surviving old bytes.
                unusable = {relative:record for relative, record in unusable.items()
                    if not _already_unavailable(root=account, relative=relative, record=record,
                        source_sha=source_sha, budget=current['budget'], manifest=manifest)}
            existing_history = current["budget"].get("account_history")
            if reconciliation is not None and existing_history:
                from src.autoslice.native_audio_history_reconciliation import read_bound
                _, _, plan = read_bound(reconciliation)
                if plan != existing_history["plan"]:
                    raise ValueError("PREPARED_NATIVE_PARENT_DRIFT")
            if original is not None:
                try:
                    if existing_history:
                        # Containment, rather than total revision, orders a
                        # composed account. A larger active parent may omit an
                        # already retained fork; the fallback keeps both.
                        try:
                            _require_budget_history_extension(original, current)
                        except BudgetReceiptPersistenceError:
                            _require_budget_history_extension(current, original)
                            planned = original
                    elif current["revision"] >= original["revision"]:
                        _require_budget_history_extension(original, current)
                    else:
                        _require_budget_history_extension(current, original)
                        planned = original
                except BudgetReceiptPersistenceError as fork_error:
                    if existing_history:
                        from src.autoslice.native_audio_history_reconciliation import reconcile_active_fork
                        planned = reconcile_active_fork(
                            account=account, current=current, incoming=original,
                            receipt_path=receipt_path, incoming_path=parent / "native-audio-budget.json")
                    elif reconciliation is not None:
                        from src.autoslice.native_audio_history_reconciliation import build_reconciliation
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
        active_fork = None
        if history and original:
            for fork in history.get("active_forks", []):
                if original["receipt_sha256"] == fork["incoming"]["receipt_sha256"]:
                    fork_root = account / "history-cache" / fork["incoming"]["sha256"][7:]
                    fork_parent = True
                    active_fork = fork
        new_cache_jobs = []
        if active_fork:
            for attempt in active_fork['active_budget']['attempts'][active_fork['shared_prefix']['attempts']:]:
                if attempt['status'] in {'OBSERVED', 'TEXT_UNLOCATED'}:
                    new_cache_jobs.append(_attempt_cache_job(source_sha, attempt))
        destinations = []
        for relative, digest in inventory.items():
            if relative == "native-audio-budget.json":
                continue
            target = account / relative
            _no_links(target)
            if target.exists() and "sha256:" + _sha256(target) != digest:
                if (not fork_parent or active_fork and not any(
                        Path(relative).is_relative_to(job) for job in new_cache_jobs)):
                    raise ValueError("PREPARED_NATIVE_PARENT_DRIFT")
            else:
                destinations.append((target, origins.get(relative, parent / relative), digest))
            if fork_parent:
                destinations.append((fork_root / relative, origins.get(relative, parent / relative), digest))
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
        archives = []
        if original is not None and (parent != account or unusable):
            archives.append((parent, inventory['native-audio-budget.json']))
        if current_unusable:
            archives.append((account, 'sha256:' + _sha256(receipt_path)))
        for origin_parent, digest in archives:
            archive = account / "parents" / (digest[7:] + ".json")
            archive.parent.mkdir(exist_ok=True)
            _no_links(archive)
            if archive.exists():
                _require_bound(archive, digest)
            else:
                atomic_write_bytes(archive, (origin_parent / "native-audio-budget.json").read_bytes())
                _require_bound(archive, digest)
            parent_binding = {"path": str(origin_parent), "budget_sha256": digest}
            if parent_binding not in manifest["parents"]:
                manifest["parents"].append(parent_binding)
        for holes, remap in ((current_unusable, False), (unusable, fork_parent)):
            if not holes:
                continue
            updated = {}
            for relative, record in holes.items():
                # Keep accounting immutable. This manifest describes only cache
                # eligibility; it is never an OBSERVED receipt or a refund.
                if remap:
                    # The v3 view adds the existing branch namespace to original
                    # v2 IDs. Mirror that representation in cache eligibility,
                    # without changing either immutable branch receipt.
                    from src.autoslice.native_audio_history_reconciliation import remap_incoming_attempt
                    record = {**record, 'attempts': [remap_incoming_attempt(a, original, history)
                                                   for a in record['attempts']]}
                elif history:
                    record = {**record, 'attempts': [a if 'history_branch_sha256' in a else
                        {**a, 'history_branch_sha256': history['canonical']['receipt_sha256']}
                        for a in record['attempts']]}
                from src.autoslice.native_audio_history_reconciliation import cache_branches
                for unavailable_attempt in record['attempts']:
                    # Existing immutable branch caches keep their original
                    # namespace. Only a newly retained active suffix is remapped;
                    # nesting every old namespace under the new fork loses its
                    # eligibility at the ordinary cold reader.
                    job = _attempt_cache_job(source_sha, unavailable_attempt)
                    digest = cache_branches(history).get(unavailable_attempt.get('history_branch_sha256'))
                    key = str(Path('history-cache') / digest[7:] / job) if digest else str(job)
                    for cache_key in dict.fromkeys((key, str(job))):
                        files = (str(Path(cache_key) / 'input.mp3'),
                                 str(Path(cache_key) / f"input.audio-evidence.{unavailable_attempt['provider']}.json"))
                        if unavailable_attempt['provider'] not in {'agy', 'gemini_api'} and all(
                                name in manifest['cache_preimages'] and (account / name).is_file() for name in files):
                            continue  # retain the account's byte-bound original copy
                        _record_unavailable(unusable=updated, relative=cache_key,
                            parent=Path(record['parent']), budget_sha=record['budget_sha256'],
                            attempt=unavailable_attempt, present=record['present'],
                            acquisitions=record.get('unusable_acquisitions'))
            retained = manifest.setdefault('unusable_caches', {})
            final_budget = (planned or current or original)['budget']
            for key, record in updated.items():
                old = retained.get(key)
                if old:
                    attempts = []
                    for attempt in [*old['attempts'], *record['attempts']]:
                        if attempt not in final_budget['attempts'] and history and 'history_branch_sha256' not in attempt:
                            attempt = {**attempt, 'history_branch_sha256': history['canonical']['receipt_sha256']}
                        if attempt not in final_budget['attempts']:
                            raise ValueError('PREPARED_NATIVE_UNAVAILABLE_CACHE_IDENTITY_MISSING')
                        if attempt not in attempts:
                            attempts.append(attempt)
                    record['attempts'] = attempts
                    for field in ('present', 'unusable_acquisitions'):
                        values = dict(old.get(field, {}))
                        if any(path in values and values[path] != digest
                               for path, digest in record.get(field, {}).items()):
                            raise ValueError('PREPARED_NATIVE_PARENT_DRIFT')
                        values.update(record.get(field, {}))
                        if values:
                            record[field] = values
                retained[key] = record
        # A first v2 -> v3 JOIN changes the representation of every canonical
        # identity, including already qualified holes untouched by this parent.
        final_budget = (planned or current or original or {}).get('budget', {})
        if history:
            for record in manifest.get('unusable_caches', {}).values():
                record['attempts'] = [a if a in final_budget['attempts'] else
                    {**a, 'history_branch_sha256': history['canonical']['receipt_sha256']}
                    for a in record['attempts']]
                if any(a not in final_budget['attempts'] for a in record['attempts']):
                    raise ValueError('PREPARED_NATIVE_UNAVAILABLE_CACHE_IDENTITY_MISSING')
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
    from src.autoslice.native_audio_history_reconciliation import history_cache_roots
    roots = history_cache_roots(account, history)
    return PreparedNativeAudioState(account, receipt_path, roots)


def _attempt_cache_job(source_sha: str, attempt: dict) -> Path:
    if attempt['provider'] in {'agy', 'gemini_api'}:
        return Path('gemini-range-acquisition') / source_sha[:20] / f"{attempt['start_ms']}-{attempt['end_ms']}"
    return Path('native-exact') / attempt['provider'] / f"{source_sha[:20]}-{attempt['start_ms']}-{attempt['end_ms']}"


def join_own_interrupted_account(
    state: PreparedNativeAudioState, *, cid: str, out_root: Path, source: Path, runtime_root: Path,
) -> tuple[PreparedNativeAudioState, dict | None]:
    """Join this output root's own earlier account (an interrupted attempt).

    Occupation and real observations of the stopped run are never refunded or
    reset. The ordinary inheritance is the
    proof: the account must extend the joined ledger as an exact prefix (or a
    registered fork). Missing caches retain their attempts but cannot
    serve observations; ordinary collection must acquire them again. Nothing is written to the
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
