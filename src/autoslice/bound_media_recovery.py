"""Resolve a missing derived medium without changing its immutable binding.

Existing unsafe or changed files are never repaired. A recovered file is only
usable if it hashes to the old digest; otherwise the observation needs a new
acquisition. Temporary reconstructions live for this process, outside the old
artifact tree. No provider or receipt writer is involved here.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import TypeAlias, cast

from .evidence_identity import regular_file_sha256, sha256_hex

Json: TypeAlias = 'None | bool | int | float | str | list[Json] | dict[str, Json]'
_MEDIA_SUFFIXES = frozenset({'.mp4', '.mp3', '.wav', '.m4a', '.flac', '.aac', '.mkv'})
_TEMP: list[tempfile.TemporaryDirectory[str]] = []
_RESOLVED: dict[tuple[Path, str], Path] = {}


class BoundMediaUnavailable(ValueError):
    """No original bytes: an old observation cannot authorize a decision."""

    def __init__(self, path: Path, digest: str) -> None:
        self.path, self.digest = path, digest
        super().__init__('BOUND_MEDIA_REACQUISITION_REQUIRED')


@dataclass(frozen=True)
class Source:
    path: Path
    sha256: str


@dataclass(frozen=True)
class AudioRecipe:
    source: Source
    start_ms: int
    end_ms: int
    kind: str  # the existing writer, never a guessed codec/interval


def _no_links(path: Path) -> bool:
    return not any(p.is_symlink() for p in (path, *path.parents))


def _read(path: Path) -> dict[str, Json]:
    if not _no_links(path) or regular_file_sha256(path) is None:
        return {}
    try:
        value = json.loads(path.read_text())
        return cast('dict[str, Json]', value) if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _source(value: Json) -> Source | None:
    if not isinstance(value, dict):
        return None
    path, digest = value.get('path'), value.get('sha256')
    if isinstance(path, str) and isinstance(digest, str):
        try:
            return Source(Path(path), sha256_hex(digest))
        except ValueError:
            pass
    return None


def audio_recipe(source: object, interval: object, kind: str) -> AudioRecipe | None:
    """Translate a writer's saved fields; callers still validate the document."""
    bound = _source(cast(Json, source))
    if (bound is not None and isinstance(interval, list) and len(interval) == 2
            and all(type(x) is int for x in interval)):
        start, end = cast('list[int]', interval)
        if 0 <= start < end:
            return AudioRecipe(bound, start, end, kind)
    return None


def runtime_root(path: Path) -> Path:
    """Search the actual runtime containing the reference, never a host mount."""
    configured = os.environ.get('AUTOSLICE_BASE')
    if configured:
        root = Path(configured).absolute()
        if path.is_relative_to(root):
            return root
    for parent in path.parents:
        if parent.name in {'private-runs', 'production-inputs', 'out', 'candidates', 'reports'}:
            return parent.parent
    return path.parent


def copies(path: Path, digest: str, root: Path) -> list[Path]:
    """Content-checked candidates; names/sidecars are discovery hints only."""
    result: list[Path] = []
    if not _no_links(root):
        return result
    for parent, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(n for n in dirs if n not in {
            '.git', 'venv', '.venv', 'node_modules', '__pycache__', 'versions',
            'recording', 'CloudFS',
        } and _no_links(Path(parent, n)))
        candidates = {Path(parent, n) for n in files if n == path.name}
        # A single-piece padded writer copies a piece under a different name.
        for name in files:
            if name.endswith('.provenance.json'):
                doc = _read(Path(parent, name))
                if doc.get('output_sha256') == digest and isinstance(doc.get('output_path'), str):
                    candidate = Path(cast(str, doc['output_path']))
                    if candidate.is_relative_to(root):
                        candidates.add(candidate)
        for candidate in sorted(candidates):
            if (candidate != path and _no_links(candidate)
                    and regular_file_sha256(candidate) == digest):
                result.append(candidate)
    return result


def _acquisition_recipe(path: Path, digest: str) -> AudioRecipe | None:
    doc = _read(path.parent/'acquisition.json')
    unsigned = dict(doc)
    declared = unsigned.pop('metadata_sha256', None)
    import hashlib
    if declared != hashlib.sha256(json.dumps(unsigned, ensure_ascii=False,
            sort_keys=True, separators=(',', ':')).encode()).hexdigest():
        return None
    paths = doc.get('paths')
    if not isinstance(paths, dict):
        return None
    if not any(_source(v) == Source(path, digest) for v in paths.values()):
        return None
    source = {'path': doc.get('source_media'), 'sha256': doc.get('source_media_sha256')}
    kind = 'black_frame' if path.name == 'input.mp4' else 'native_exact_mp3'
    if path.name == 'input.gemini-api.mp3':
        source = cast('dict[str, Json]', paths.get('rendered_audio_clip'))
        kind = 'gemini_api_mp3'
    elif path.name not in {'input.mp4', 'input.mp3'}:
        return None
    return audio_recipe(source, doc.get('source_target_ms'), kind)


def _rebuild(path: Path, digest: str, output: Path, root: Path,
             recipe: AudioRecipe | None, active: frozenset[Path]) -> bool:
    recipe = recipe or _acquisition_recipe(path, digest)
    if recipe is not None:
        source = resolve_bound_media(recipe.source.path, recipe.source.sha256,
                                     root=root, active=active)
        if not _no_links(source) or regular_file_sha256(source) != recipe.source.sha256:
            return False
        if recipe.kind == 'black_frame':
            from .entity_audio_verifier import _crop_black_frame_audio
            ok = _crop_black_frame_audio(source_media=source, audio_path=output,
                start_ms=recipe.start_ms, end_ms=recipe.end_ms)[0]
            return ok and regular_file_sha256(source, fresh=True) == recipe.source.sha256
        if recipe.kind == 'native_exact_mp3':
            from .native_foreign_witness import _extract_exact_mp3
            _extract_exact_mp3(source, output, recipe.start_ms, recipe.end_ms)
            return regular_file_sha256(source, fresh=True) == recipe.source.sha256
        if recipe.kind == 'gemini_api_mp3':
            # entity_audio_verifier's API transport transcodes the complete
            # rendered MP4, rather than recutting the original recording.
            subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                '-i', str(source), '-vn', '-ac', '1', '-ar', '16000', '-b:a',
                '64k', str(output)], check=True, capture_output=True, timeout=180)
            return regular_file_sha256(source, fresh=True) == recipe.source.sha256
        return False
    doc = _read(path.with_suffix('.provenance.json'))
    if doc.get('output_path') != str(path) or doc.get('output_sha256') != digest:
        return False
    inputs = doc.get('inputs')
    if isinstance(inputs, list) and inputs:
        sources = [_source(row) for row in inputs]
        if any(row is None for row in sources):
            return False
        pieces = [resolve_bound_media(row.path, row.sha256, root=root, active=active)
                  for row in sources if row is not None]
        if any(regular_file_sha256(p) != row.sha256 for p, row in
               zip(pieces, (s for s in sources if s is not None), strict=True)):
            return False
        if len(pieces) == 1:
            shutil.copyfile(pieces[0], output)
            return True
        durations = doc.get('piece_durations_ms')
        if (doc.get('materialization') != 'independent-decoded-audio-copyts.v1'
                or not isinstance(durations, list) or len(durations) != len(pieces)
                or any(type(n) is not int or n <= 0 for n in durations)):
            return False
        from .producer_source_media import _concat_piece_command
        concat = output.with_suffix('.concat.txt')
        if any("'" in str(p) or '\n' in str(p) for p in pieces):
            return False
        times = cast('list[int]', durations)
        concat.write_text(''.join(f"file '{p}'\nduration {n/1000:.3f}\n"
                                 for p, n in zip(pieces, times, strict=True)))
        subprocess.run(_concat_piece_command(pieces, times, concat, output),
                       check=True, capture_output=True, timeout=3600)
        return True
    recipe = audio_recipe({'path':doc.get('source_path'), 'sha256':doc.get('source_sha256')},
                          [doc.get('start_ms'),doc.get('end_ms')], 'piece')
    if recipe is None:
        return False
    source = resolve_bound_media(recipe.source.path, recipe.source.sha256,
                                 root=root, active=active)
    if not _no_links(source) or regular_file_sha256(source) != recipe.source.sha256:
        return False
    from .recut_materialization import _accurate_reencode_recut_command
    subprocess.run(_accurate_reencode_recut_command(source_video=source, output_media=output,
        start_ms=recipe.start_ms, duration_ms=recipe.end_ms-recipe.start_ms),
        check=True, capture_output=True, timeout=3600)
    return regular_file_sha256(source, fresh=True) == recipe.source.sha256


def resolve_bound_media(path: Path, expected: str, *, root: Path | None = None,
                        recipe: AudioRecipe | None = None,
                        active: frozenset[Path] = frozenset()) -> Path:
    """Only missing media can recover. Every other input keeps its old guard.

    The physical read location can change; saved paths, digests and conclusions
    cannot. No negative lookup is cached: a subsequent ordinary collection can
    find newly materialized bytes without changing the historical record.
    """
    path = path.absolute()
    if not _no_links(path) or path.exists() or path.suffix not in _MEDIA_SUFFIXES:
        return path
    try:
        digest = sha256_hex(expected)
    except ValueError:
        return path  # malformed bindings retain the original consumer's error
    if path in active:
        raise BoundMediaUnavailable(path, digest)
    root = root or runtime_root(path)
    cached = _RESOLVED.get((path, digest))
    if cached is not None and _no_links(cached) and regular_file_sha256(cached) == digest:
        return cached
    found = copies(path, digest, root)
    if found:
        _RESOLVED[path, digest] = found[0]
        return found[0]
    temporary = tempfile.TemporaryDirectory(prefix='autoslice-bound-media-')
    _TEMP.append(temporary)
    output = Path(temporary.name)/path.name
    try:
        rebuilt = _rebuild(path, digest, output, root, recipe, active | {path})
        if rebuilt and regular_file_sha256(output, fresh=True) == digest:
            if path.exists() or path.is_symlink():
                return path  # a concurrently appeared original gets its own guard
            _RESOLVED[path, digest] = output
            return output
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        pass
    raise BoundMediaUnavailable(path, digest)
