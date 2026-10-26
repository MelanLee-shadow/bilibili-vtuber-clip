"""Private successful-cover checkpoints; no QC or publication authority."""
from __future__ import annotations

import fcntl
import hashlib
import inspect
import json
import marshal
import os
import re
import stat
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, is_dataclass
from pathlib import Path

from .qixi_transaction_core import (
    FileSnapshot,
    _runner_lock_open_flags, _safe_parent, _same_safe_runner_lock,
    create_staged_inode, install_staged_inode, stable_regular_snapshot,
)

SCHEMA = "normal-cover-generation-checkpoint.v1"


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def file_identity(path: Path) -> dict[str, object]:
    path = path.absolute()
    if ".." in path.parts:
        raise ValueError("COVER_CHECKPOINT_UNSAFE_PATH")
    # Optional assets may have missing ancestors. Inspect existing ancestors
    # without following links, then record absence rather than inventing bytes.
    cursor = Path(path.anchor)
    for part in path.parts[1:-1]:
        cursor /= part
        try:
            mode = cursor.lstat().st_mode
        except FileNotFoundError:
            return {"path": str(path), "missing": True}
        if not stat.S_ISDIR(mode):
            raise ValueError("COVER_CHECKPOINT_UNSAFE_PARENT")
    _safe_parent(path)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return {"path": str(path.absolute()), "missing": True}
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("COVER_CHECKPOINT_UNSAFE_FILE")
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("COVER_CHECKPOINT_FILE_CHANGED")
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_size, after.st_mtime_ns, after.st_ctime_ns
    ):
        raise ValueError("COVER_CHECKPOINT_FILE_CHANGED")
    named = path.lstat()
    if (named.st_dev, named.st_ino, named.st_size, named.st_mtime_ns, named.st_ctime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
    ) or not stat.S_ISREG(named.st_mode):
        raise ValueError("COVER_CHECKPOINT_FILE_CHANGED")
    return {"path": str(path.absolute()), "bytes": after.st_size,
            "sha256": "sha256:" + digest.hexdigest()}


def callable_identity(value: object) -> object:
    if value is None:
        return None
    # Canonical LLM adapters expose these receipt-safe identities explicitly.
    routing = getattr(value, "cpa_cache_identity", None)
    runtime = getattr(value, "provider_runtime_binding", None)
    function = value if inspect.isfunction(value) else getattr(value, "__call__", None)
    code = getattr(function, "__code__", None)
    if code is None:
        raise ValueError("COVER_CHECKPOINT_CALLABLE_IDENTITY_UNAVAILABLE")
    closure = getattr(function, "__closure__", None)
    wrapped = None
    if (inspect.isfunction(value) and isinstance(runtime, Mapping)
            and runtime.get("adapter") == "provider_slots.provider_transport"):
        callbacks = getattr(value, "_provider_transport_callables", None)
        if (not isinstance(callbacks, tuple) or len(callbacks) != 2
                or not inspect.isfunction(callbacks[0])
                or (callbacks[1] is not None and not inspect.isfunction(callbacks[1]))):
            raise ValueError("COVER_CHECKPOINT_CALLABLE_BINDING_REQUIRED")
        # Route metadata never substitutes for the original call/callback
        # identities, including the original rejection of opaque closures.
        wrapped = [callable_identity(callback) for callback in callbacks]
    elif closure and routing is None and runtime is None:
        # The standard provider-slot decorator captures only its underlying
        # function and timeout callback. Bind both, without serializing state
        # (which could contain credentials) from arbitrary injected adapters.
        if any(not inspect.isfunction(cell.cell_contents) or cell.cell_contents is value for cell in closure):
            raise ValueError("COVER_CHECKPOINT_CALLABLE_BINDING_REQUIRED")
        wrapped = [callable_identity(cell.cell_contents) for cell in closure]
    if inspect.ismethod(value) and routing is None and runtime is None:
        raise ValueError("COVER_CHECKPOINT_CALLABLE_BINDING_REQUIRED")
    return {"module": getattr(value, "__module__", None),
            "name": getattr(value, "__qualname__", None),
            # Reference-sharing flags in newer marshal formats can change
            # after executing a code object on CPython 3.11. Format 2 binds
            # the same code values without incidental object-sharing state.
            "code_sha256": hashlib.sha256(marshal.dumps(code, 2)).hexdigest(),
            "routing": routing, "runtime": runtime, "wrapped": wrapped}


def generation_identity(*, record: Mapping, media_path: Path, title: str,
                        cover_text: str, inputs: Mapping, options: Mapping,
                        profile: object, repo_root: Path, provider: Mapping,
                        generator: object) -> dict:
    """Bind actual inputs/options and the code/assets used by this callee."""
    normalized = {key: (callable_identity(value) if callable(value)
                         else asdict(value) if is_dataclass(value) else value)
                  for key, value in options.items() if key != "private_artifact_root"}
    code_names = (
        "publish_staging", "publish_staging_paths", "normal_cover_generation_checkpoint",
        "cover_generation", "fixed_cover_stage", "cover_candidate_direction", "cover_emote",
        "cover_punch_semantics", "cover_font_paths", "cover_text_pixel_evidence",
        "cover_identity_landmark", "cover_title_rendering", "cover_story_direction", "cover_title_reflow",
        "cover_title_reflow_binding",
        "cover_route_evidence", "cover_source_composition", "cover_host_identity_gate",
        "cover_scene_binding", "story_contract", "cpa_image_edit", "llm_client",
        "cover_frame_selection", "cover_screenshot_poster", "cpa_frame_witness", "agy_frame_witness",
        "game_context", "cover_polish_gate", "producer_final_review_transport", "cover_route_policy",
        "cover_package_media_lineage", "channel_profile", "surface_canon", "cover_text_layout",
        "cover_reference_authority", "qixi_transaction_core", "provider_slots", "provider_admission", "visual_witness",
        "agy_gemini_client", "title_cover_candidates", "title_cover_staging",
        "title_cover_quotes", "cover_style", "cover_private_font", "title_cover_package", "title_cover_shabutton", "title_cover_history",
    )
    paths = [repo_root / "src/autoslice" / (name + ".py") for name in code_names]
    code_paths = set(paths)
    paths.append(profile.manifest_path)
    if record.get("subtitle_path"):
        paths.append(Path(str(record["subtitle_path"])))
    paths.extend(profile.asset_file(key, repo_root=repo_root) for key in profile.asset_files
                 if "cover" in key or key in {"persona", "emote_library"})
    from .cover_font_paths import (
        cover_fallback_font_candidates, resolve_cover_fonts_dir, resolve_primary_cover_font,
    )
    font_directories = {
        resolve_cover_fonts_dir(media, channel_profile=profile, root=repo_root)
        for media in (None, media_path)
    }
    for directory in font_directories - {None}:
        _safe_parent(directory.absolute() / "input")
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise ValueError("COVER_CHECKPOINT_UNSAFE_ASSET_DIRECTORY")
            if not path.is_dir():
                paths.append(path)
    try:
        paths.append(resolve_primary_cover_font(
            channel_profile=profile, root=repo_root,
            fontsdir=resolve_cover_fonts_dir(None, channel_profile=profile, root=repo_root),
        ))
    except RuntimeError:
        # The original callee retains its ordinary missing-font rejection.
        pass
    paths.extend(cover_fallback_font_candidates(channel_profile=profile, root=repo_root))
    from .cover_private_font import resolve_private_cover_font, DEFAULT_BINDING
    private_binding = Path(os.environ.get("LIDOUSHA_COVER_FONT_BINDING") or DEFAULT_BINDING)
    paths.append(private_binding)
    try:
        private_font, _ = resolve_private_cover_font()
        paths.append(private_font)
    except ValueError:
        pass  # the producer retains the explicit missing/private-font blocker
    for choice in profile.delivery_root_for(repo_root).glob("**/choose.json"):
        paths.append(choice)
    paths.extend(profile.delivery_root_for(repo_root).glob("**/*.record.json"))
    from .cover_emote import emote_media_roots, load_emote_library
    library = load_emote_library(repo_root)
    for entry in library.entries:
        paths.extend(directory / entry.file for directory in emote_media_roots(
            repo_root, runtime_roots=library.runtime_roots,
        ))
        paths.extend(directory / entry.hd_file for directory in emote_media_roots(
            repo_root, runtime_roots=library.runtime_roots,
        ))
    from .title_cover_shabutton import manifest_path as shabutton_manifest_path, validate_manifest
    shabutton = shabutton_manifest_path()
    paths.append(shabutton)
    if shabutton.is_file():
        for entry in validate_manifest(json.loads(shabutton.read_text())):
            paths.append(shabutton.parent / entry["file"])
    context_path = os.environ.get("LIDOUSHA_SESSION_GAME_CONTEXT")
    if context_path:
        paths.append(Path(context_path))
    media = file_identity(media_path)
    if media.get("missing"):
        raise ValueError("COVER_CHECKPOINT_INPUT_MEDIA_MISSING")
    dependencies = [file_identity(path) for path in sorted(set(paths))]
    if any(row.get("missing") and Path(row["path"]) in code_paths for row in dependencies):
        raise ValueError("COVER_CHECKPOINT_DEPENDENCY_MISSING")
    return json.loads(canonical({
        "schema_version": SCHEMA, "editorial_input": dict(inputs),
        "title": title, "cover_text": cover_text,
        "full_story_contract": record.get("story_contract"),
        "burned_preview": record.get("burned_preview"),
        "media": media,
        "options": normalized, "provider": dict(provider),
        "generator": callable_identity(generator),
        "dependencies": dependencies,
    }))


def _atomic_json(path: Path, value: object) -> None:
    _safe_parent(path)
    if os.path.lexists(path) and not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("COVER_CHECKPOINT_UNSAFE_FILE")
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    create_staged_inode(temporary, payload=canonical(value), mode=0o600, label="cover checkpoint")
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_json(path: Path, *, expected_sha256: object = None) -> dict:
    snapshot = stable_regular_snapshot(path, label="cover checkpoint")
    if snapshot is None:
        raise ValueError("COVER_CHECKPOINT_MISSING")
    if expected_sha256 is not None and snapshot.sha256 != expected_sha256:
        raise ValueError("COVER_CHECKPOINT_RECEIPT_DRIFT")
    value = json.loads(snapshot.payload)
    if not isinstance(value, dict):
        raise ValueError("COVER_CHECKPOINT_INVALID")
    return value


def _is_control_file(path: Path, attempt: Path) -> bool:
    return path.parent == attempt and (
        path.name in {"result.json", "failure.json"}
        or re.fullmatch(r"\.(?:result|failure)\.json\.[0-9a-f]{32}\.tmp", path.name) is not None
    )


def _read_result_receipt(
    path: Path, *, expected_sha256: object = None,
) -> tuple[dict, FileSnapshot | None]:
    """Read a formal receipt or a single complete, still-uninstalled inode."""
    temporaries = sorted(path.parent.glob(".result.json.*.tmp"))
    if len(temporaries) > 1:
        raise ValueError("COVER_CHECKPOINT_AMBIGUOUS_STAGED")
    if temporaries and re.fullmatch(r"\.result\.json\.[0-9a-f]{32}\.tmp", temporaries[0].name) is None:
        raise ValueError("COVER_CHECKPOINT_INVALID_STAGED")
    staged = None
    if temporaries:
        staged = stable_regular_snapshot(temporaries[0], label="cover staged receipt")
        if staged is None:
            raise ValueError("COVER_CHECKPOINT_MISSING")
    if os.path.lexists(path):
        saved = _read_json(path, expected_sha256=expected_sha256)
        if staged is not None and file_identity(path)["sha256"] != staged.sha256:
            raise ValueError("COVER_CHECKPOINT_STAGED_DRIFT")
        return saved, None
    if staged is None:
        raise ValueError("COVER_CHECKPOINT_MISSING")
    if expected_sha256 is not None and staged.sha256 != expected_sha256:
        raise ValueError("COVER_CHECKPOINT_RECEIPT_DRIFT")
    saved = json.loads(staged.payload)
    if not isinstance(saved, dict):
        raise ValueError("COVER_CHECKPOINT_INVALID")
    return saved, staged


def retained_failed_generation(*, path: Path, expected_sha256: str, expected: Mapping,
                               artifact_root: Path, candidate_id: str) -> dict:
    return retained_generation(path=path, expected_sha256=expected_sha256, expected=expected,
        artifact_root=artifact_root, candidate_id=candidate_id, successful=False)


def retained_editorial_identity(*, path: Path, expected_sha256: str,
        artifact_root: Path, candidate_id: str, successful: bool) -> dict:
    """Read a pinned completed stage only after proving its original files."""
    saved = _read_json(path.absolute(), expected_sha256='sha256:' + expected_sha256.removeprefix('sha256:'))
    old = saved.get('identity')
    if not isinstance(old, Mapping):
        raise ValueError('COVER_REVIEW_ORIGIN_INVALID')
    retained_generation(path=path, expected_sha256=expected_sha256, expected=old,
        artifact_root=artifact_root, candidate_id=candidate_id, successful=successful)
    return json.loads(canonical(old))


def retained_generation(*, path: Path, expected_sha256: str, expected: Mapping,
                        artifact_root: Path, candidate_id: str, successful: bool) -> dict:
    """Load explicitly bound pixels without inheriting obsolete code identity."""
    path = path.absolute()
    candidate = hashlib.sha256(candidate_id.encode()).hexdigest()[:24]
    if (path.name != ('result.json' if successful else 'failure.json') or path.parent.parent.parent !=
            artifact_root.absolute() / '.normal-cover-generations' / candidate
            or re.fullmatch(r'[0-9a-f]{32}', path.parent.name) is None):
        raise ValueError('COVER_REVIEW_ORIGIN_INVALID')
    saved = _read_json(path, expected_sha256='sha256:' + expected_sha256.removeprefix('sha256:'))
    old = saved.get('identity')
    result = saved.get('result')
    if (saved.get('schema_version') != SCHEMA
            or not isinstance(old, Mapping) or not isinstance(result, dict)
            or (not successful and (saved.get('status') != 'REJECTED_NOT_REUSABLE'
                or result.get('reason_codes') != ['COVER_FINAL_HOST_IDENTITY_UNVERIFIED']))
            or (successful and result.get('status') != 'AI_COVER_READY')):
        raise ValueError('COVER_REVIEW_ORIGIN_INVALID')
    for key in ('editorial_input', 'title', 'cover_text', 'full_story_contract', 'media', 'burned_preview'):
        if canonical(old.get(key)) != canonical(expected.get(key)):
            raise ValueError('COVER_REVIEW_CURRENT_INPUT_DRIFT')
    def material_options(identity):
        adapters = {'art_direction_llm_call', 'image_edit', 'final_participant_verifier',
                    'final_host_identity_verifier', 'source_composition_verifier'}
        return {key: value for key, value in identity.get('options', {}).items()
                if key not in adapters and not key.startswith('retained_')}
    if canonical(material_options(old)) != canonical(material_options(expected)):
        raise ValueError('COVER_REVIEW_CURRENT_INPUT_DRIFT')
    def material_dependencies(identity):
        values = []
        for row in identity.get('dependencies', []):
            path = Path(row['path'])
            if path.suffix == '.py':
                continue  # Current consumers revalidate the retained pixels.
            parts = path.parts
            section = next((i for i, part in enumerate(parts) if part in {'assets', 'profiles', 'config'}), None)
            key = str(Path(*parts[section:])) if section is not None else str(path)
            values.append((key, canonical({k: v for k, v in row.items() if k != 'path'})))
        return sorted(values)
    if material_dependencies(old) != material_dependencies(expected):
        raise ValueError('COVER_REVIEW_CURRENT_ASSET_DRIFT')
    dependencies = old.get('dependencies')
    if not isinstance(dependencies, list) or not dependencies:
        raise ValueError('COVER_REVIEW_ORIGINAL_DEPENDENCY_DRIFT')
    for dependency in dependencies:
        if not isinstance(dependency, Mapping) or file_identity(Path(str(dependency.get('path') or ''))) != dependency:
            raise ValueError('COVER_REVIEW_ORIGINAL_DEPENDENCY_DRIFT')
    generation = result.get('cover_generation') or {}
    if (not isinstance(generation, Mapping) or generation.get('method') != 'images.edit' or generation.get('image_generation_used') is not True
            or generation.get('input_binding') != old.get('editorial_input')):
        raise ValueError('COVER_REVIEW_ORIGIN_INVALID')
    # A path projection lets the unchanged output validator inspect a rejected
    # pixel set. No status or original receipt is changed by this inspection.
    inspected = {**result, 'cover_path': generation.get('final_cover')}
    outputs = _outputs(inspected, path.parent)
    if successful and outputs != saved.get('outputs'):
        raise ValueError('COVER_REVIEW_ORIGINAL_OUTPUT_DRIFT')
    return {**result, 'cover_generation': {**generation, 'retained_generation': {
        'path': str(path), 'sha256': file_identity(path)['sha256'], 'outputs': outputs}}}


def _outputs(result: Mapping, attempt: Path) -> list[dict]:
    paths: set[Path] = set()
    generation = result.get('cover_generation') or {}
    retained = generation.get('retained_generation')
    origin = None
    origin_paths: set[Path] = set()
    if isinstance(retained, Mapping):
        receipt = Path(str(retained.get('path') or ''))
        if (receipt.name not in {'failure.json', 'result.json'}
                or receipt.parent.parent.parent.name != attempt.parent.parent.name
                or receipt.parent.parent.parent.parent.name != '.normal-cover-generations'
                or re.fullmatch(r'[0-9a-f]{32}', receipt.parent.name) is None
                or file_identity(receipt).get('sha256') != retained.get('sha256')):
            raise ValueError('COVER_REVIEW_ORIGIN_INVALID')
        origin = receipt.parent
        if [file_identity(Path(row['path'])) for row in retained.get('outputs', [])] != retained.get('outputs'):
            raise ValueError('COVER_REVIEW_ORIGINAL_OUTPUT_DRIFT')
        origin_paths = {receipt, *(Path(row['path']) for row in retained.get('outputs', []))}
    def owned(raw):
        path = Path(str(raw))
        if (not path.is_absolute() or ".." in path.parts
                or not (path.is_relative_to(attempt) or path in origin_paths)):
            raise ValueError("COVER_CHECKPOINT_OUTPUT_NOT_OWNED")
        if _is_control_file(path, attempt):
            raise ValueError("COVER_CHECKPOINT_OUTPUT_NOT_OWNED")
        return path
    def visit(value):
        if isinstance(value, Mapping):
            for key, child in value.items():
                if key in {"story_contract", "input_binding"}:
                    continue
                if isinstance(child, str) and Path(child).is_absolute():
                    path = Path(child)
                    if path.is_relative_to(attempt) or (origin is not None and path.is_relative_to(origin)):
                        paths.add(owned(child))
                elif isinstance(child, (Mapping, list)):
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(result)
    generation = result.get("cover_generation") or {}
    required = [result.get("cover_path"), generation.get("final_cover")]
    local = generation.get("method") == "screenshot_direct" and generation.get("image_generation_used") is False
    if not local:
        required.extend(generation.get(key) for key in ("request_path", "response_path"))
    for raw in required:
        if not raw:
            raise ValueError("COVER_CHECKPOINT_OUTPUT_NOT_OWNED")
        paths.add(owned(raw))
    # Bind references, raw responses, comparisons and rejected intermediate
    # artifacts too, including retained files not exposed as metadata paths.
    for path in sorted(attempt.rglob("*")):
        if path.is_symlink():
            raise ValueError("COVER_CHECKPOINT_UNSAFE_FILE")
        if not path.is_dir() and not _is_control_file(path, attempt):
            paths.add(path)
    paths = {path for path in paths if not path.is_dir()}
    rows = [file_identity(path) for path in sorted(paths)]
    if any(row.get("missing") for row in rows):
        raise ValueError("COVER_CHECKPOINT_OUTPUT_MISSING")
    expected = generation.get("final_cover_sha256")
    observed = {row["path"]: row["sha256"] for row in rows}
    if expected != observed.get(str(result["cover_path"])) or expected != observed.get(str(generation["final_cover"])):
        raise ValueError("COVER_CHECKPOINT_FINAL_BYTES_DRIFT")
    if not local and any(
        generation.get(key + "_sha256") != observed.get(str(generation[key + "_path"]))
        for key in ("request", "response")
    ):
        raise ValueError("COVER_CHECKPOINT_NETWORK_BYTES_DRIFT")
    return rows


def successful_generation(*, artifact_root: Path, candidate_id: str,
                          identity: Callable[[], dict], produce: Callable[[Path], dict]) -> dict:
    """Serialize only this generation identity; preserve all immutable attempts.

    The local flock is nonblocking. It never acquires the runner/global lease.
    Drifted outputs regenerate in a new sibling; unsafe/partial receipts block.
    """
    expected = identity()
    digest = hashlib.sha256(canonical(expected)).hexdigest()
    candidate = hashlib.sha256(candidate_id.encode()).hexdigest()[:24]
    if ".." in artifact_root.parts:
        raise ValueError("COVER_CHECKPOINT_UNSAFE_DIRECTORY")
    root = artifact_root.absolute() / ".normal-cover-generations" / candidate / digest
    cursor = Path(root.anchor)
    for part in root.parts[1:]:
        cursor /= part
        try:
            cursor.mkdir(mode=0o700)
        except FileExistsError:
            pass
        if not stat.S_ISDIR(cursor.lstat().st_mode):
            raise ValueError("COVER_CHECKPOINT_UNSAFE_DIRECTORY")
    _safe_parent(root / "lock")
    descriptor = os.open(root / "lock", _runner_lock_open_flags(), 0o600)
    try:
        opened = os.fstat(descriptor)
        _same_safe_runner_lock(root / "lock", descriptor, opened)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("COVER_GENERATION_CHECKPOINT_BUSY") from exc
        _same_safe_runner_lock(root / "lock", descriptor, opened)
        if identity() != expected:
            raise ValueError("COVER_GENERATION_CHECKPOINT_INPUT_DRIFT")
        pointer = root / "CURRENT.json"
        receipts = []
        if os.path.lexists(pointer):
            locator = _read_json(pointer)
            name = locator.get("receipt")
            if not isinstance(name, str) or Path(name).is_absolute() or len(Path(name).parts) != 2 or Path(name).parts[1] != "result.json" or ".." in Path(name).parts:
                raise ValueError("COVER_CHECKPOINT_INVALID")
            receipt = root / name
            if not isinstance(locator.get("sha256"), str):
                raise ValueError("COVER_CHECKPOINT_INVALID")
            receipts = [(receipt, locator["sha256"])]
        else:
            # Include the fsynced inode left just before result rename.
            paths = set(root.glob("*/result.json"))
            paths.update(path.parent / "result.json" for path in root.glob("*/.result.json.*.tmp"))
            receipts = [(path, None) for path in sorted(paths)]
        valid = []
        for receipt, receipt_sha256 in receipts:
            if re.fullmatch(r"[0-9a-f]{32}", receipt.parent.name) is None:
                raise ValueError("COVER_CHECKPOINT_INVALID")
            saved, staged = _read_result_receipt(receipt, expected_sha256=receipt_sha256)
            if saved.get("schema_version") != SCHEMA or saved.get("identity") != expected:
                raise ValueError("COVER_CHECKPOINT_IDENTITY_DRIFT")
            result = saved.get("result")
            if not isinstance(result, dict) or result.get("status") != "AI_COVER_READY":
                raise ValueError("COVER_CHECKPOINT_NOT_SUCCESSFUL")
            try:
                actual = _outputs(result, receipt.parent)
            except ValueError as exc:
                if staged is not None:
                    raise
                if str(exc) not in {"COVER_CHECKPOINT_OUTPUT_MISSING", "COVER_CHECKPOINT_FINAL_BYTES_DRIFT", "COVER_CHECKPOINT_NETWORK_BYTES_DRIFT"}:
                    raise
                continue
            if actual == saved.get("outputs"):
                valid.append((receipt, result, actual, staged))
            elif staged is not None:
                raise ValueError("COVER_CHECKPOINT_STAGED_OUTPUT_DRIFT")
        if len(valid) > 1:
            raise ValueError("COVER_CHECKPOINT_AMBIGUOUS")
        if valid:
            from .cpa_runtime import record_cpa_event
            receipt, result, saved_outputs, staged = valid[0]
            if identity() != expected:
                raise ValueError("COVER_GENERATION_CHECKPOINT_INPUT_DRIFT")
            if _outputs(result, receipt.parent) != saved_outputs:
                raise ValueError("COVER_CHECKPOINT_OUTPUT_CHANGED_DURING_REUSE")
            if staged is not None:
                # Validate every receipt and output before mutating any locator.
                installed = install_staged_inode(
                    staged, target=receipt, expected_before=None,
                    label="cover checkpoint staged recovery",
                )
                if installed.sha256 != staged.sha256:
                    raise ValueError("COVER_CHECKPOINT_STAGED_DRIFT")
                if identity() != expected:
                    raise ValueError("COVER_GENERATION_CHECKPOINT_INPUT_DRIFT")
                if _outputs(result, receipt.parent) != saved_outputs:
                    raise ValueError("COVER_CHECKPOINT_OUTPUT_CHANGED_DURING_REUSE")
            if not pointer.exists():
                _atomic_json(pointer, {"receipt": str(receipt.relative_to(root)),
                                       "sha256": file_identity(receipt)["sha256"]})
            record_cpa_event('cache_hit',{'kind':'cover_generation','stage':'normal_cover'})
            return result
        attempt = root / uuid.uuid4().hex
        attempt.mkdir(mode=0o700)
        result = produce(attempt)
        if result.get("status") != "AI_COVER_READY":
            _atomic_json(attempt / "failure.json", {
                "schema_version": SCHEMA, "identity": expected, "result": result,
                "status": "REJECTED_NOT_REUSABLE",
            })
            return result
        if identity() != expected:
            _atomic_json(attempt / "failure.json", {
                "schema_version": SCHEMA, "identity": expected, "result": result,
                "status": "INPUT_DRIFT_NOT_REUSABLE",
            })
            raise ValueError("COVER_GENERATION_CHECKPOINT_INPUT_DRIFT")
        outputs = _outputs(result, attempt)
        receipt = attempt / "result.json"
        _atomic_json(receipt, {"schema_version": SCHEMA, "identity": expected,
                               "result": result, "outputs": outputs})
        _atomic_json(pointer, {"receipt": str(receipt.relative_to(root)),
                               "sha256": file_identity(receipt)["sha256"]})
        return result
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
