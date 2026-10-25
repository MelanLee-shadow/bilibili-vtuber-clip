"""Create-only preparation of the exact reviewed B2 v7/a8 recovery package.

This is the normal-source integration of the completed E428 implementation.
Input/output locations are explicit, while candidate, artifact and evidence
pins remain unchanged. It does not deploy, upload, or mutate production state.
Reuse an existing accepted package instead of preparing it a second time.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from importlib.machinery import ModuleSpec
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import types
from typing import Any, Mapping

from src.autoslice.review_evidence_store import review_from_bytes

REPO = Path(__file__).resolve().parents[2]
OUTPUT_NAME = 'release-v7-e428-a1'
CANDIDATE = 'auto_120029_1409_1578'
BVID = 'BV18dac6iEvJ'
MANIFEST_SHA = '266cc76c15d2bd6d9b980b2c763c3bb1074c01a59fe32190d579060d8f7946f1'
AUDIT_SHA = 'ca797e3296236abf9356f0309b0615156f996efebea3fd46762131f9cacbaa56'
TITLE = '【李豆沙】打嗝视频里怎么还藏着雷公电母？！'
PINS = {
    'video': '2c309f6a23846ef7d5ed45513b73455fd55b093f1b269caf04e35d87a93ac498',
    'subtitle_srt': 'a0856249250c46623468f5b7735027c7007efa0f88c6b838c5837fe65fdac57f',
    'ass_path': '8867bbdb5b79635dc839828ee9ed23664c291b81804c8db29defa47f3807b5c1',
    'cover': '338bf8327b3270a2ad1f23839fac4329c18ee24ce39a4ddb1aa674b485f62230',
    'record': '9a8ccd49ca9f3d0115c8b3696a49a845ffa6d7dcf029f6628039080de509a98f',
    'publish_json': 'ed3d8584cc90f5df155a474d251a882e1fd171c63eca2f1a637ef7849f3fa939',
    'chat_authority': '7f06c91fa2e6b24522b4554ae69e3b400594995dd3ea81d5b267df1a00ce5869',
    'clip_context': '28a9474214b6c4e90c43a432c4c2419faf3a754af950644920c70580f776321d',
}
LEGACY_HELPER_SHA = 'b9c56b9230882d399c1105a424d7d7c9df1ebaf052dc5097419524e0c17c62cb'
RAW_MAIN_NAME = CANDIDATE + '.recut.mp4'
RAW_MAIN_SHA = '9e67d859d819e37071cb4de759c1d2ffda7cc5e9868fb88e0ed6823307499fed'
SCHEMA = 'b2-v7-manifest-recovery-successor.v1'


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def safe_relative(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError('MISSING_MANIFEST_PATH')
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or str(path) != value or '\\' in value:
        raise ValueError('UNSAFE_MANIFEST_PATH')
    return value


def read_regular(path: Path) -> bytes:
    """Read one descriptor after checking the entire existing path chain."""
    p = path.absolute()
    current = Path(p.anchor)
    for part in p.parts[1:]:
        current = current / part
        if stat.S_ISLNK(current.lstat().st_mode):
            raise ValueError('SYMLINK_INPUT:' + str(current))
    if not stat.S_ISREG(p.lstat().st_mode):
        raise ValueError('NOT_REGULAR:' + str(p))
    # Reject known special files before open; do not block if a regular file
    # becomes a FIFO between the path check and descriptor verification.
    fd = os.open(p, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('NOT_REGULAR:' + str(p))
        chunks = []
        while True:
            data = os.read(fd, 1 << 20)
            if not data:
                break
            chunks.append(data)
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise ValueError('INPUT_CHANGED_DURING_READ:' + str(p))
        return b''.join(chunks)
    finally:
        os.close(fd)


def _decode_role_document(raw: bytes, *, role: str, directory: Path) -> dict[str, Any]:
    """Decode a pinned document only after its caller verifies the wire bytes."""

    try:
        value = (
            review_from_bytes(raw, directory=directory)
            if role in ('record', 'publish_json', 'chat_authority')
            else json.loads(raw)
        )
    except (OSError, TypeError, UnicodeError, ValueError) as exc:
        raise ValueError('DOCUMENT_INVALID:' + role) from exc
    if not isinstance(value, dict):
        raise ValueError('DOCUMENT_ROOT_NOT_OBJECT:' + role)
    return value


def binding(path: Path) -> dict[str, Any]:
    raw = read_regular(path)
    return {'path': str(path), 'sha256': digest(raw), 'bytes': len(raw)}


def json_bytes(obj: object) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode()


def new_file(path: Path, raw: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())


def new_json(path: Path, value: object) -> None:
    new_file(path, json_bytes(value))


def replace_owned(path: Path, raw: bytes, expected: str) -> None:
    if digest(read_regular(path)) != expected:
        raise ValueError('DESTINATION_PREIMAGE_DRIFT:' + str(path))
    tmp = path.with_name('.' + path.name + '.e428-write')
    new_file(tmp, raw)
    os.replace(tmp, path)


def changed_leaves(before: object, after: object, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    if isinstance(before, dict) and isinstance(after, dict):
        if set(before) != set(after):
            return {prefix}
        result = set()
        for key in before:
            result |= changed_leaves(before[key], after[key], prefix + (str(key),))
        return result
    if isinstance(before, list) and isinstance(after, list):
        if len(before) != len(after):
            return {prefix}
        result = set()
        for i, (a, b) in enumerate(zip(before, after)):
            result |= changed_leaves(a, b, prefix + (str(i),))
        return result
    return set() if before == after else {prefix}


def select_item(manifest: Mapping[str, Any]) -> dict[str, Any]:
    items = manifest.get('items')
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise ValueError('NOT_EXACTLY_ONE_MANIFEST_ITEM')
    item = items[0]
    if item.get('candidate_id') != CANDIDATE or item.get('title') != TITLE:
        raise ValueError('MANIFEST_CANDIDATE_OR_TITLE_DRIFT')
    if manifest.get('upload_allowed') is not False:
        raise ValueError('SOURCE_ALREADY_CLAIMS_UPLOAD')
    if (item.get('recovery_publication_authority') or {}).get('bvid') != BVID:
        raise ValueError('MANIFEST_TARGET_BV_DRIFT')
    for key in PINS:
        safe_relative(item.get(key))
    return deepcopy(item)


def _real_directory(path: Path) -> Path:
    """Require an existing lexical directory chain without following links."""
    absolute = path.absolute()
    if '..' in absolute.parts:
        raise ValueError('UNSAFE_MANIFEST_PATH')
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError('CREATE_ONLY_PRIVATE_DESTINATION_REQUIRED')
    return absolute


def _require_absent(path: Path) -> None:
    # Unlike exists/lexists, propagate permission and other I/O failures.
    try:
        path.lstat()
    except FileNotFoundError:
        return
    raise ValueError('CREATE_ONLY_PRIVATE_DESTINATION_REQUIRED')


def _source_inventory(source: Path) -> dict[str, dict[str, Any]]:
    """Inventory every source file or propagate the original enumeration failure."""
    def raise_walk_error(error: OSError) -> None:
        raise error

    inventory = {}
    for root, dirs, names in os.walk(source, followlinks=False, onerror=raise_walk_error):
        for name in dirs:
            if (Path(root) / name).is_symlink():
                raise ValueError('SOURCE_DIRECTORY_SYMLINK')
        for name in names:
            path = Path(root) / name
            b = binding(path)
            inventory[path.relative_to(source).as_posix()] = {k: b[k] for k in ('sha256', 'bytes')}
    return inventory


def preflight(source: Path) -> dict[str, Any]:
    source = _real_directory(source)
    # Location is portable; the exact a8 manifest/audit/artifact pins are not.
    manifest_raw = read_regular(source / 'review_manifest.json')
    audit_raw = read_regular(source / 'package-audit.json')
    if digest(manifest_raw) != MANIFEST_SHA or digest(audit_raw) != AUDIT_SHA:
        raise ValueError('SEALED_SOURCE_MANIFEST_OR_AUDIT_DRIFT')
    manifest, audit = json.loads(manifest_raw), json.loads(audit_raw)
    item = select_item(manifest)
    if audit.get('passed') is not True or audit.get('issues'):
        raise ValueError('PARENT_AUDIT_NOT_ACCEPTED')
    # Reuse the prior audit as lineage; actual destination still needs a fresh audit.
    indexed = {}
    for row in audit['audited_inputs']:
        relative = safe_relative(row['path'])
        if relative in indexed:
            raise ValueError('DUPLICATE_AUDITED_INPUT')
        raw = read_regular(source / relative)
        if digest(raw) != row['sha256'].removeprefix('sha256:') or len(raw) != row['bytes']:
            raise ValueError('AUDITED_INPUT_DRIFT:' + relative)
        indexed[relative] = {'sha256': digest(raw), 'bytes': len(raw)}
    docs = {}
    for key, expected in PINS.items():
        raw = read_regular(source / item[key])
        if digest(raw) != expected or indexed.get(item[key], {}).get('sha256') != expected:
            raise ValueError('CURRENT_V7_ARTIFACT_DRIFT:' + key)
        if key in ('record', 'publish_json', 'chat_authority', 'clip_context'):
            docs[key] = _decode_role_document(raw, role=key, directory=source)
    record, publish, chat = docs['record'], docs['publish_json'], docs['chat_authority']
    if record.get('candidate_id') != CANDIDATE or publish.get('candidate_id') != CANDIDATE:
        raise ValueError('DOCUMENT_CANDIDATE_DRIFT')
    if record.get('speaker_mode') != 'uniform_host' or chat.get('speaker_mode') != 'uniform_host':
        raise ValueError('SPEAKER_DISPLAY_CONTRACT_DRIFT')
    authority = item['recovery_publication_authority']
    if any(x != authority for x in (
        record.get('recovery_publication_authority'),
        record['publish_staging'].get('recovery_publication_authority'),
        publish.get('recovery_publication_authority'),
    )) or authority.get('cid') != 42154658915:
        raise ValueError('HISTORICAL_PUBLICATION_AUTHORITY_DRIFT')
    sf = record['publish_staging']['source_fact_review']
    if sf != record['story_contract']['source_fact_review'] or sf != publish['source_fact_review']:
        raise ValueError('SOURCE_FACT_MIRROR_DRIFT')
    if record['exact_final_review'] != chat['final_review_audit']:
        raise ValueError('FINAL_REVIEW_MIRROR_DRIFT')
    ending = record['boundary_audit']['final_delivery_boundary_semantic_review']
    if ending.get('next_topic_separated') is not False or ending.get('recommended_end_ms') != 151690:
        raise ValueError('USER_ENDPOINT_DRIFT')
    if record.get('duration_ms') != 152090:
        raise ValueError('DELIVERY_DURATION_DRIFT')
    if indexed.get(RAW_MAIN_NAME, {}).get('sha256') != RAW_MAIN_SHA:
        raise ValueError('HISTORICAL_RAW_MEDIA_ALIAS_NOT_AUDITED')
    legacy = REPO / 'scripts/finalize_b2_caption_same_bv_recovery_package.py'
    if digest(read_regular(legacy)) != LEGACY_HELPER_SHA:
        raise ValueError('LEGACY_PROJECTION_HELPER_DRIFT')
    inventory = _source_inventory(source)
    return {'manifest': manifest, 'item': item, 'docs': docs, 'inventory': inventory,
            'audited_inputs': indexed, 'source': str(source)}


def load_native() -> tuple[Any, ...]:
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(REPO))
    # Resolve the repository's scripts namespace; do not patch any validator.
    ns = types.ModuleType('scripts')
    ns.__path__ = [str(REPO / 'scripts')]
    ns.__spec__ = ModuleSpec('scripts', loader=None, is_package=True)
    ns.__spec__.submodule_search_locations = ns.__path__
    sys.modules['scripts'] = ns
    from PIL import Image, features
    if (Image.__version__, features.version_module('freetype2')) != ('11.3.0', '2.13.3'):
        raise ValueError('EXECUTION_RENDER_ENVIRONMENT_NOT_ACCEPTED')
    from scripts.finalize_b2_caption_same_bv_recovery_package import _project_cover_generation
    from src.autoslice.package_relocation_contract import set_value, is_mutable_pointer, validate_document_root_roles
    from scripts.build_recovery_review_manifest import build_manifest
    from scripts.audit_review_package import audit_package
    return _project_cover_generation, set_value, is_mutable_pointer, validate_document_root_roles, build_manifest, audit_package


def project_documents(frozen: dict, out: Path, native: tuple[Any, ...]) -> tuple[dict, dict, dict]:
    cover_projector, set_value, is_mutable, root_validator, _, _ = native
    item = frozen['item']
    record, publish = deepcopy(frozen['docs']['record']), deepcopy(frozen['docs']['publish_json'])
    source_record, source_publish = frozen['docs']['record'], frozen['docs']['publish_json']
    paths = {key: out / item[key] for key in PINS}
    paths.update(plain_srt=paths['subtitle_srt'], final_video=paths['video'], ass=paths['ass_path'],
                 chat=paths['chat_authority'], clip=paths['clip_context'], publish=paths['publish_json'],
                 raw_video=out / RAW_MAIN_NAME,
                 ai_background=out / item['cover_pre_overlay'],
                 title_mask=out / item['cover_title_mask'],
                 reference=out / item['host_only_v4_binding']['reference_path'],
                 builtin_provenance=out / item['cover_builtin_provenance'])
    record_targets = {
        ('media_path',): paths['raw_video'],
        ('subtitle_path',): paths['subtitle_srt'],
        ('subtitle_ass_path',): paths['ass_path'],
        ('chat_authority_audit_path',): paths['chat'],
        ('clip_context_path',): paths['clip'],
        ('burned_preview','path'): paths['video'],
        ('burned_preview','ass_path'): paths['ass_path'],
        ('publish_staging','cover_path'): paths['cover'],
        ('publish_staging','publish_json_path'): paths['publish'],
        ('publish_staging','video_path'): paths['video'],
        ('publish_staging','subtitle_path'): paths['subtitle_srt'],
        ('subtitle_audio_correspondence','output_dir'): out,
    }
    suffixes = {
        'witness_srt_path': 'subtitle-audio-witness.srt',
        'provenance_path': 'subtitle-audio-provenance.json',
        'correspondence_path': 'subtitle-audio-correspondence.json',
        'raw_result_path': 'subtitle-audio-bcut.raw.json',
    }
    stem = item['stem']
    for key, suffix in suffixes.items():
        record_targets[('subtitle_audio_correspondence', key)] = out / (stem + '.' + suffix)
    for pointer, path in record_targets.items():
        if path != out and not path.is_file():
            raise ValueError('PROJECTED_ARTIFACT_MISSING:' + str(path))
        set_value(record, pointer, str(path))
    publish['video_path'] = str(paths['video'])
    publish['cover_path'] = str(paths['cover'])
    for obj in (publish['cover_generation'], record['cover_generation'], record['publish_staging']['cover_generation']):
        cover_projector(obj, paths)
    # Preserve entire frozen model/BCUT verdicts, including historical locators.
    for pointer in [('story_contract',), ('boundary_audit',), ('exact_final_review',),
                    ('subtitle_audio_correspondence','receipt')]:
        a, b = source_record, record
        for key in pointer:
            a, b = a[key], b[key]
        if a != b:
            raise ValueError('SEMANTIC_EVIDENCE_MUTATED')
    if any(obj['source_composition_verification'] != source_publish['cover_generation']['source_composition_verification']
           or obj['final_host_identity_verification'] != source_publish['cover_generation']['final_host_identity_verification']
           for obj in (publish['cover_generation'], record['cover_generation'], record['publish_staging']['cover_generation'])):
        raise ValueError('FROZEN_VISUAL_WITNESS_MUTATED')
    delta = {}
    for kind, before, after in [('record',source_record,record),('publish',source_publish,publish)]:
        changes = changed_leaves(before, after)
        extras = {('publish_staging','video_path'), ('publish_staging','subtitle_path')} if kind == 'record' else set()
        for point in changes:
            # Top-level cover_generation is the exact mirror of the native
            # staging cover. The established B2 helper projects both mirrors.
            tested = ('publish_staging',) + point if kind == 'record' and point[0] == 'cover_generation' else point
            if not is_mutable(kind, tested) and point not in extras:
                raise ValueError('NON_LOCATOR_DELTA:' + kind + ':' + '/'.join(point))
        delta[kind] = sorted('/' + '/'.join(x) for x in changes)
    dummy_a, dummy_b = out / '.source-candidate-placeholder', out / '.destination-candidate-placeholder'
    mappings = ((frozen['source'],str(out)),(str(dummy_a),str(dummy_b)),(str(REPO),str(REPO)))
    root_validator(record, kind='record', mappings=mappings)
    root_validator(publish, kind='publish', mappings=mappings)
    return record, publish, delta


def preparation_capacity(frozen: Mapping[str, Any], out: Path) -> dict[str, Any]:
    """Budget this preparation through the existing configured reserve check.

    The single entry is an aggregate allocation budget on the destination
    filesystem, not a claim that a file of that size will be written. Include
    the four preserved history copies and retain E428's scratch allowance.
    The shared checker owns reserve parsing, disk probing and typed errors.
    """
    from src.autoslice.package_import_capacity import copy_capacity

    inventory = frozen['inventory']
    source_copy_bytes = sum(row['bytes'] for row in inventory.values())
    history_copy_bytes = sum(
        inventory[frozen['item'][key]]['bytes']
        for key in ('record', 'publish_json', 'chat_authority', 'clip_context')
    )
    scratch_allowance_bytes = 128 * 1024 * 1024
    allocation_budget = source_copy_bytes + history_copy_bytes + scratch_allowance_bytes
    volumes = copy_capacity([{
        'destination': str(out),
        'status': 'WOULD_COPY',
        'bytes': allocation_budget,
        'role': 'B2_PREPARATION_AGGREGATE_ALLOCATION_BUDGET',
    }])
    return {
        'source_copy_bytes': source_copy_bytes,
        'additional_history_copy_bytes': history_copy_bytes,
        'scratch_allowance_bytes': scratch_allowance_bytes,
        'allocation_budget_bytes': allocation_budget,
        'volumes': volumes,
        'scope': 'PREFLIGHT_ALLOCATION_BUDGET_NOT_RESERVED_SPACE_OR_ACTUAL_WRITTEN_BYTES',
    }


def prepare(source: Path, out: Path) -> dict:
    stamp = datetime.now(timezone.utc).isoformat()
    source = _real_directory(source)
    out = out.absolute()
    run_root = _real_directory(out.parent)
    # Keep the established output basename and separate receipt namespace. No
    # automatic retry, target replacement, directory cleanup or state adoption.
    if (out.name != OUTPUT_NAME or '..' in out.parts
            or out.is_relative_to(source) or source.is_relative_to(out)):
        raise ValueError('CREATE_ONLY_PRIVATE_DESTINATION_REQUIRED')
    for target in (out, run_root / 'SOURCE-PREFLIGHT.json', run_root / 'PREPARE-RESULT.json'):
        _require_absent(target)
    frozen = preflight(source)
    # Honor the same reserve as normal package imports before runtime loading
    # or the first preparation receipt/target write. Do not alter the environment.
    capacity = preparation_capacity(frozen, out)
    native = load_native()
    new_json(run_root / 'SOURCE-PREFLIGHT.json', {
        'schema_version': SCHEMA, 'created_at_utc': stamp, 'source_manifest_sha256': MANIFEST_SHA,
        'source_audit_sha256': AUDIT_SHA, 'candidate_id': CANDIDATE, 'target_bvid': BVID,
        'inventory': frozen['inventory'], 'manifest_item': frozen['item'],
        'capacity': capacity,
        'state_transition': 'NONE', 'production_adopted': False,
    })
    out.mkdir(mode=0o700)
    # Copy independent bytes, never hard-link mutable documents into the source.
    history = out / 'history/a8'
    current_gate_names = {'review_manifest.json', 'package-audit.json', 'title-cover-joint-qc.json',
                          CANDIDATE + '.title-cover-joint-qc.json'}
    for relative, meta in frozen['inventory'].items():
        p = PurePosixPath(relative)
        target = history / relative if p.parts[0] == 'verification' or relative in current_gate_names else out / relative
        raw = read_regular(source / relative)
        if digest(raw) != meta['sha256'] or len(raw) != meta['bytes']:
            raise ValueError('SOURCE_DRIFT_DURING_COPY:' + relative)
        new_file(target, raw)
    for key in ('record','publish_json','chat_authority','clip_context'):
        rel = frozen['item'][key]
        new_file(history / rel, read_regular(source / rel))
    record, publish, delta = project_documents(frozen, out, native)
    item = frozen['item']
    publish_raw = json_bytes(publish)
    record['artifact_hashes']['publish_draft_sha256'] = 'sha256:' + digest(publish_raw)
    delta['record'].append('/artifact_hashes/publish_draft_sha256')
    replace_owned(out / item['publish_json'], publish_raw, PINS['publish_json'])
    replace_owned(out / item['record'], json_bytes(record), PINS['record'])
    for key in ('chat_authority','clip_context'):
        if digest(read_regular(out / item[key])) != PINS[key]:
            raise ValueError('FROZEN_TEXT_EVIDENCE_BYTES_CHANGED')
    # The copied recovery-state is package construction context only. Never
    # read or mutate actual production state to turn published into review_ready.
    state = json.loads(read_regular(out / 'b2-caption-recovery-state.json'))
    build_manifest, audit_package = native[4:]
    manifest = build_manifest(package_root=out, state=state,
                              deployed_commit=frozen['manifest']['deployed_commit'],
                              created_at=stamp)
    manifest['producer_environment'] = {
        'scope': 'PRIVATE_V7_RECOVERY_SUCCESSOR_NOT_PRODUCTION_ADOPTION',
        'interpreter': sys.executable, 'local_code_deployed': False,
        'source_manifest_sha256': MANIFEST_SHA,
        'deployed_commit_field_is_preserved_historical_observation': True,
        'builder_sha256': digest(read_regular(Path(__file__))),
        'git_inspection_performed': False,
    }
    new_json(out / 'review_manifest.json', manifest)
    from src.autoslice.final_subtitle_audio_gate import validate_final_subtitle_audio_check
    from src.autoslice.final_review_contract import validate_final_review_release
    deterministic_audio = validate_final_subtitle_audio_check(record, out / item['subtitle_srt'], out / item['video'], out)
    validate_final_review_release(record['exact_final_review'],
                                  expected_srt_sha256='sha256:' + PINS['subtitle_srt'])
    review_ok = True  # The native validator raises on any incomplete/invalid evidence.
    transition = {
        'schema_version': SCHEMA, 'status':'LOCATORS_PROJECTED_NATIVE_AUDIT_PENDING',
        'candidate_id':CANDIDATE, 'target_bvid':BVID, 'state_transition':'NONE',
        'published_predecessor_chain_checked':False, 'production_adopted':False,
        'source_manifest_sha256':MANIFEST_SHA, 'source_audit_sha256':AUDIT_SHA,
        'parent_record_sha256':PINS['record'], 'parent_publish_sha256':PINS['publish_json'],
        'record_sha256':digest(read_regular(out/item['record'])),
        'publish_sha256':digest(read_regular(out/item['publish_json'])),
        'changed_json_pointers':delta, 'chat_and_context_bytes_unchanged':True,
        'frozen_semantic_and_bcut_and_visual_witnesses_unchanged':True,
        'new_provider_calls':0, 'media_renders':0, 'historical_registry_cid':42154658915,
        'historical_main_media_role':'UNCHANGED_PARENT_INPUT_NOT_CURRENT_DELIVERY',
        'source_inventory_receipt': binding(run_root/'SOURCE-PREFLIGHT.json'),
    }
    new_json(out / 'b2-v7-recovery-locator-successor.json', transition)
    audit = audit_package(out)
    new_json(out / 'verification/package-audit.json', audit)
    unchanged = all(binding(source/rel)['sha256'] == meta['sha256'] for rel,meta in frozen['inventory'].items())
    passed = audit.get('passed') is True and not audit.get('issues') and unchanged
    result = {
        'schema_version':SCHEMA, 'started_at_utc':stamp,
        'completed_at_utc':datetime.now(timezone.utc).isoformat(),
        'candidate_id':CANDIDATE, 'target_bvid':BVID,
        'status':'PRIVATE_PACKAGE_AUDIT_PASS_MECHANICAL_JQC_PENDING' if passed else 'PRIVATE_PACKAGE_AUDIT_BLOCKED',
        'output':str(out), 'audit':binding(out/'verification/package-audit.json'),
        'capacity':capacity,
        'manifest':binding(out/'review_manifest.json'),
        'transition':binding(out/'b2-v7-recovery-locator-successor.json'),
        'issues':audit.get('issues',[]), 'source_files_unchanged':unchanged,
        'current_audio_timing':deterministic_audio.get('status'), 'current_final_review_passed':review_ok,
        'production_adopted':False, 'uploaded':False,
        'operations':{'provider_calls':0,'image_generation_calls':0,'media_renders':0,'source_edits':0,'state_writes':0,'uploads':0,'new_bvids':0},
    }
    new_json(run_root / 'PREPARE-RESULT.json', result)
    return result
