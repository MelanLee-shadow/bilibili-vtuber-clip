"""Structured, expiring protection for bound evidence and inputs.

The digest comes from the recorded input binding or its declaring document
(named in purpose). Readers never reopen media to hash it: a drifted or missing
input still needs its owner's attention, not GC.
"""

from __future__ import annotations

from datetime import datetime, timezone
import ctypes
import json
import os
from pathlib import Path
import re
import tempfile
import time
import sys
from collections.abc import Callable, Iterator
from typing import TypeAlias, TypedDict, cast


class Lease(TypedDict):
    schema: str
    path: str
    sha256: str
    holder: str
    purpose: str
    until_utc: str


Leases: TypeAlias = dict[str, Lease]
JsonValue: TypeAlias = str | int | float | bool | None | list['JsonValue'] | dict[str, 'JsonValue']
Location: TypeAlias = tuple[str, ...]
Binding: TypeAlias = tuple[Location, str, JsonValue]

SCHEMA = "evidence-lease.v1"
FIELDS = frozenset({"schema", "path", "sha256", "holder", "purpose", "until_utc"})
INPUT_EXT = {'.mp4', '.m4s', '.mp3', '.wav', '.flac', '.m4a', '.mkv', '.ts', '.aac',
             '.png', '.jpg', '.jpeg', '.webp', '.ass', '.srt'}


def validate(lease: object) -> Lease:
    if not isinstance(lease, dict) or set(lease) != FIELDS or lease.get("schema") != SCHEMA:
        raise ValueError("unsupported evidence lease schema or fields")
    if any(not isinstance(lease[key], str) or not lease[key].strip() for key in FIELDS):
        raise ValueError("evidence lease fields must be nonempty strings")
    if not Path(lease["path"]).is_absolute():
        raise ValueError("evidence lease path must be absolute")
    if not re.fullmatch(r"[0-9a-f]{64}", lease["sha256"]):
        raise ValueError("evidence lease sha256 must be 64 lowercase hex digits")
    validated = cast(Lease, dict(lease))
    expires_at(validated)
    return validated


def expires_at(lease: Lease) -> float:
    stamp = datetime.fromisoformat(lease["until_utc"].replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() != timezone.utc.utcoffset(stamp):
        raise ValueError("evidence lease until_utc must specify UTC")
    return stamp.timestamp()


def lease_dir(base: Path) -> Path:
    return base / "state" / "evidence-leases"


def write_lease(base: Path, lease_id: str, *, path: Path, sha256: str,
                holder: str, purpose: str, until_utc: str) -> Path:
    """Atomically create/renew a lease; ID is a filename, never a path."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", lease_id):
        raise ValueError("invalid evidence lease id")
    lease = validate({"schema": SCHEMA, "path": str(path.resolve()), "sha256": sha256,
                      "holder": holder, "purpose": purpose, "until_utc": until_utc})
    directory = lease_dir(base)
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise ValueError("evidence lease directory must not be a symlink")
    destination = directory / f"{lease_id}.json"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         prefix=".lease-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(lease, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


def read_leases(base: Path, *, now: float | None = None) -> Leases:
    """Read a validated active snapshot. Invalid records stop collection/apply."""
    now = time.time() if now is None else now
    directory = lease_dir(base)
    if directory.is_symlink():
        raise ValueError("evidence lease directory must not be a symlink")
    if not directory.exists():
        return {}
    if not directory.is_dir():
        raise ValueError("evidence lease directory must be a directory")
    leases: Leases = {}
    for path in sorted(path for path in directory.iterdir() if path.suffix == '.json'):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"evidence lease must be a regular JSON file: {path}")
        lease = validate(json.loads(path.read_text(encoding="utf-8")))
        if expires_at(lease) > now:
            leases[path.stem] = lease
    return leases


def covers(lease: Lease, path: Path) -> bool:
    """Protect the exact path and descendants of a bound directory, not siblings."""
    bound = Path(os.path.abspath(lease["path"]))
    path = Path(os.path.abspath(path))
    return path == bound or bound in path.parents


def protected(path: Path, leases: Leases) -> bool:
    return bool(LeaseIndex(leases).covering(path))


class LeaseIndex:
    """Lexical ancestor lookup; GC never walks symlinks. No per-media stat/hash."""

    def __init__(self, leases: Leases) -> None:
        self.by_path: dict[Path, list[str]] = {}
        for key, lease in leases.items():
            self.by_path.setdefault(Path(os.path.abspath(lease['path'])), []).append(key)

    def covering(self, path: Path) -> list[str]:
        path = Path(os.path.abspath(path))
        return [key for ancestor in (path, *path.parents) for key in self.by_path.get(ancestor, ())]


class SnapshotGuard:
    """Revalidate before every unlink without rereading N leases N times.

    Watch both the directory and each inode (including hard-linked aliases).
    Changes, expiry or unavailable event coverage fall back to full reads.
    A watcher is local to one apply and is always closed by its caller.
    """

    def __init__(self, base: Path, expected: Leases) -> None:
        self.base, self.expected = base, expected
        self.fd: int | None = None
        self.directory = lease_dir(base)
        self.stamp: tuple[int, int] | None = None
        self.expiry = min((expires_at(row) for row in expected.values()), default=float('inf'))
        if sys.platform == 'linux' and self.directory.is_dir() and not self.directory.is_symlink():
            try:
                libc = ctypes.CDLL(None, use_errno=True)
                libc.inotify_init1.argtypes = [ctypes.c_int]
                libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
                descriptor = int(libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC))
                if descriptor >= 0:
                    self.fd = descriptor
                    # MODIFY/ATTRIB/CLOSE_WRITE/MOVED_FROM/TO/CREATE/DELETE/
                    # DELETE_SELF/MOVE_SELF; overflow and IGNORED are automatic.
                    mask = 0xFCE
                    info = self.directory.lstat()
                    self.stamp = (info.st_dev, info.st_ino)
                    for path in (self.directory, *self.directory.glob('*.json')):
                        if libc.inotify_add_watch(descriptor, os.fsencode(path), mask) < 0:
                            self.close()
                            break
            except (AttributeError, OSError):
                self.close()
        try:
            if read_leases(base) != expected:
                raise RuntimeError('evidence leases changed before apply; review a new plan')
        except Exception:
            self.close()
            raise

    def unchanged(self) -> bool:
        if self.fd is not None:
            try:
                info = self.directory.lstat()
                if ((info.st_dev, info.st_ino) == self.stamp and time.time() < self.expiry):
                    try:
                        changed = bool(os.read(self.fd, 65536))
                    except BlockingIOError:
                        return True
                    if not changed:
                        self.close()
                else:
                    self.close()
            except OSError:
                self.close()
            # On any event, stop trusting this watcher (its directory or inode
            # may have been replaced). The ordinary validated reader is exact.
            self.close()
        return read_leases(self.base) == self.expected

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def derive(base: Path, sources: list[Path], *, holder: str, purpose: str,
           until_utc: str) -> Leases:
    """Issue a snapshot from selected live SPEC/authority chains, never prose.

    Follow explicit input/path fields and SOURCE parent/provenance siblings.
    COMMANDS uses only JSON fences or explicit source_media/inputs fields.
    Hashes identify the declaring document when no input digest was recorded;
    they never claim a measured media digest. GC needs the binding, not its bytes.
    The caller supplies the owner and expiry; no inferred perpetual ownership.
    """
    import hashlib
    from src.autoslice.source_checkpoint_parent import checkpoint_parent_paths
    from src.autoslice.review_evidence_store import evidence_store_root
    store_root = cast(Callable[[Path], Path], evidence_store_root)

    base = base.absolute()
    result: Leases = {}
    declaration_digests: dict[Path, str] = {}

    def declaration_digest(path: Path | str, data: bytes | None = None) -> str:
        # Many media/text bindings share one large authority document. Hash
        # its declaration once per issuance, never reopen it for each input.
        path = Path(path)
        if data is not None:
            # A different selected source may observe a later atomic rewrite.
            # Bind the bytes just parsed, while still avoiding per-input reads.
            declaration_digests[path] = hashlib.sha256(data).hexdigest()
        elif path not in declaration_digests:
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            declaration_digests[path] = digest.hexdigest()
        return declaration_digests[path]
    for source in sources:
        source = source.absolute()
        if source.suffix.lower() not in {'.json', '.jsonl'} and source.name != 'COMMANDS.md':
            raise ValueError(f'evidence source must be a structured declaration: {source}')
        if source.is_symlink() or not source.is_file():
            raise ValueError(f'evidence binding source unavailable: {source}')
        queue: list[tuple[Path, JsonValue, str]] = [(source, None, str(source))]
        seen: set[Path] = set()
        ids: set[str] = set()
        while queue:
            path, recorded_digest, binding = queue.pop()
            path = Path(os.path.abspath(path))
            if path in seen:
                continue
            seen.add(path)
            if path != source and base not in path.parents:
                # Explicit bindings outside the runtime still protect their
                # named file/directory (GC also has home/old-clone roots).
                # Representation of that binding does not authorize opening
                # arbitrary external JSON/media to expand its dependencies.
                digest = recorded_digest.removeprefix('sha256:') if isinstance(recorded_digest, str) else None
                key = hashlib.sha256((str(source) + '\0' + str(path)).encode()).hexdigest()
                result[key] = validate({'schema': SCHEMA, 'path': str(path),
                                        'sha256': digest or declaration_digest(binding), 'holder': holder,
                                        'purpose': f'{purpose}; binding={binding}', 'until_utc': until_utc})
                continue
            if path.is_symlink():
                raise ValueError(f'evidence binding must not be a symlink: {path}')
            digest = recorded_digest.removeprefix('sha256:') if isinstance(recorded_digest, str) else None
            documents: list[JsonValue] = []
            if path.suffix.lower() in {'.json', '.jsonl'} or path.name == 'COMMANDS.md':
                if path.is_file():
                    data = path.read_bytes()
                    declaring_hash = declaration_digest(path, data)
                    digest = digest or declaring_hash
                    text = data.decode('utf-8')
                    try:
                        documents = (list(command_declarations(text)) if path.name == 'COMMANDS.md' else
                                     [cast(JsonValue, json.loads(line)) for line in text.splitlines() if line.strip()]
                                     if path.suffix.lower() == '.jsonl' else [cast(JsonValue, json.loads(text))])
                    except ValueError:
                        # A raw provider response is still a protected input;
                        # it does not declare further structured dependencies.
                        if path == source:
                            raise
            # A scalar path with no recorded hash binds to its declaring JSON,
            # not to a fabricated digest of missing/unread media.
            digest = digest or declaration_digest(binding)
            key = hashlib.sha256((str(source) + '\0' + str(path)).encode()).hexdigest()
            result[key] = validate({'schema': SCHEMA, 'path': str(path), 'sha256': digest,
                                    'holder': holder, 'purpose': f'{purpose}; binding={binding}',
                                    'until_utc': until_utc})
            if path.suffix.lower() not in {'.json', '.jsonl'}:
                provenance = path.with_suffix('.provenance.json')
                if provenance.is_file():
                    queue.append((provenance, None, str(path if documents else binding)))
            if path.parent.name in ids:
                queue.extend((parent, None, str(path if documents else binding))
                             for parent in checkpoint_parent_paths(path.parent, path.parent.name).values()
                             if parent.is_file())
            if path.name.endswith('.chat-authority.json'):
                store = store_root(path.parent)
                if store.is_dir() and not store.is_symlink():
                    queue.extend((child, None, str(path)) for child in store.rglob('*')
                                 if child.is_file() and not child.is_symlink())
            for document in documents:
                if isinstance(document, dict):
                    ids.update(str(document[k]) for k in ('candidate_id', 'cid') if document.get(k))
                    if document.get('output_root') and document.get('candidate_id'):
                        output = Path(str(document['output_root'])) / str(document['candidate_id'])
                        queue.extend((parent, None, str(path)) for parent in
                                     checkpoint_parent_paths(output, str(document['candidate_id'])).values()
                                     if base in parent.parents and parent.is_file())
                for _location, value, sha256 in structural_bindings(document):
                    bound = Path(value)
                    bound = bound if bound.is_absolute() else path.parent / bound
                    queue.append((bound, sha256, str(path)))
    return result


def issue_from_sources(base: Path, sources: list[Path], *, lease_base: Path | None = None,
                       holder: str, purpose: str, until_utc: str) -> Leases:
    leases = derive(base, sources, holder=holder, purpose=purpose, until_utc=until_utc)
    for key, lease in leases.items():
        write_lease(lease_base or base, key, path=Path(lease['path']), sha256=lease['sha256'],
                    holder=lease['holder'], purpose=lease['purpose'], until_utc=lease['until_utc'])
    return leases


def structural_bindings(node: JsonValue, location: Location = ()) -> Iterator[Binding]:
    """SPEC/parent readers name paths, never arbitrary prose or container roots."""
    if isinstance(node, dict):
        for key, value in node.items():
            branch = location + (str(key),)
            if key in {'source_media', 'inputs'}:
                for subpath, bound, digest in declared_paths(value, branch):
                    yield subpath, bound, digest or node.get(key + '_sha256')
                continue
            if (key == 'path' or key.endswith(('_path', '_paths'))
                    or key in {'remote_media', 'danmaku_xml_local', 'chat_jsonl_local', 'speaker_session_context'}):
                if isinstance(value, str):
                    yield branch, value, node.get('sha256') if key == 'path' else node.get(key + '_sha256')
                elif isinstance(value, (list, dict)):
                    yield from declared_paths(value, branch)
            elif isinstance(value, (dict, list)):
                yield from structural_bindings(value, branch)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from structural_bindings(value, location + (str(index),))


def command_declarations(text: str) -> Iterator[JsonValue]:
    """Read JSON blocks or explicit source_media/inputs declarations, never prose paths."""
    for block in re.findall(r"^```(?:json)?\s*\n(.*?)^```\s*$", text, re.M | re.S):
        try:
            yield cast(JsonValue, json.loads(block))
        except ValueError:
            continue
    # Explicit fields may be quoted/backticked; JSON arrays can span lines.
    declaration = r'''^\s*(?:[-*]\s+)?[`"']?(source_media|inputs)[`"']?\s*[:=]\s*(.+)$'''
    for match in re.finditer(declaration, text, re.M):
        key, raw = match.groups()
        raw = raw.strip()
        encoded = text[match.start(2):].lstrip()
        if raw.startswith("`") and raw.endswith("`"):
            raw = raw[1:-1]
            encoded = raw
        try:
            decoded, _ = json.JSONDecoder().raw_decode(encoded)
            value: JsonValue = cast(JsonValue, decoded)
        except ValueError:
            value = raw if raw.startswith(("/", "./", "../")) else None
        if value is not None:
            yield {key: value}


def declared_paths(node: JsonValue, location: Location = ()) -> Iterator[Binding]:
    """Extract paths only inside an explicitly declared input field."""
    if isinstance(node, dict):
        bound = node.get('path')
        if isinstance(bound, str):
            yield location + ("path",), bound, node.get("sha256")
        for key, value in node.items():
            if key not in {"path", "sha256", "schema", "schema_version", "purpose", "holder"}:
                yield from declared_paths(value, location + (str(key),))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from declared_paths(value, location + (str(index),))
    elif isinstance(node, str) and (node.startswith(("/", "./", "../")) or Path(node).suffix.lower() in INPUT_EXT):
        yield location, node, None


def input_bindings(node: JsonValue, location: Location = ()) -> Iterator[Binding]:
    if isinstance(node, dict):
        for key, value in node.items():
            branch = location + (str(key),)
            if key in {"source_media", "inputs"}:
                for path, bound, sha256 in declared_paths(value, branch):
                    # Native-audio RESULT records keep the digest beside the
                    # scalar source_media field rather than inside a binding.
                    yield path, bound, sha256 or node.get(f"{key}_sha256")
            else:
                yield from input_bindings(value, branch)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from input_bindings(value, location + (str(index),))


def stage_bindings(node: JsonValue, location: Location = ()) -> Iterator[Binding]:
    if isinstance(node, dict):
        bound, digest = node.get('path'), node.get('sha256')
        if isinstance(bound, str) and isinstance(digest, str):
            yield location + ("path",), bound, digest
        for key, value in node.items():
            yield from stage_bindings(value, location + (str(key),))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from stage_bindings(value, location + (str(index),))
