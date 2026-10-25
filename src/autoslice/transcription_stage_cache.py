"""Retry checkpoints for successful ordinary transcription observations.

These are fallible draft observations, never release approvals or human truth.
The caller reruns preparation, fidelity, strict parsers and downstream review. Legacy
SRT files without this binding are deliberately not imported into the cache.
"""

from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import inspect
import json
import os
from pathlib import Path
import stat
import tempfile
import time
from typing import Any, Callable

SCHEMA = "ordinary-transcription-stage-cache.v1"
MAX_BYTES = 8_000_000
ROOT = Path(__file__).resolve().parents[2]


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _file_sha(path: Path) -> str:
    return _evidence_file_sha256(path)


def _no_links(path: Path) -> None:
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise OSError("unsafe checkpoint path")


def _directory(path: Path) -> None:
    _no_links(path)
    path.mkdir(mode=0o700, exist_ok=True)
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o022:
        raise OSError("unsafe checkpoint directory")


def _read(path: Path) -> dict:
    _no_links(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_nlink != 1
            or st.st_uid != os.geteuid()
            or st.st_mode & 0o077
            or not 0 < st.st_size <= MAX_BYTES
        ):
            raise OSError("unsafe checkpoint file")
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = f.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES or len(data) != st.st_size:
            raise OSError("checkpoint changed while reading")
        return json.loads(data)
    finally:
        os.close(fd)


def _write(path: Path, payload: dict, *, replace: bool = False) -> bool:
    """Create one atomic success; existing corrupt cache entries are preserved."""
    temporary = None
    try:
        _no_links(path)
        if os.path.lexists(path):
            if not replace:
                return False
            _read(path)
        data = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode()
        if len(data) > MAX_BYTES:
            return False
        fd, name = tempfile.mkstemp(prefix=".stage-", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path, follow_symlinks=False)
            temporary.unlink()
        temporary = None
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return True
    except (OSError, ValueError, TypeError):
        return False
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def _locked(root: Path, stage: str, key: str):
    fd = None
    try:
        _directory(root)
        folder = root / stage
        _directory(folder)
        lock = folder / (key + ".lock")
        _no_links(lock)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        st = os.fstat(fd)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_nlink != 1
            or st.st_uid != os.geteuid()
            or st.st_mode & 0o077
            or st.st_size != 0
        ):
            raise OSError("unsafe checkpoint lock")
        deadline = time.monotonic() + 600
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OSError("checkpoint lock timeout")
                time.sleep(0.025)
        path = folder / (key + ".json")
    except OSError:
        if fd is not None:
            os.close(fd)
            fd = None
        path = None
    try:
        yield path
    finally:
        if fd is not None:
            os.close(fd)


class TranscriptionStageCache:
    def __init__(self, media_path: Path, audio: bytes):
        self.root = media_path.absolute().parent / ".transcription-stage-cache"
        self.receipt = media_path.with_suffix(".transcription-reuse.json").absolute()
        self.audio_sha = hashlib.sha256(audio).hexdigest()
        self.audit = {
            "schema_version": SCHEMA,
            "audio_sha256": self.audio_sha,
            "final_release_approved": False,
        }

    def _record(self, stage: str, **fields):
        self.audit[stage] = fields
        # Optional cache diagnostics never replace the producer's quality gates.
        _write(self.receipt, self.audit, replace=True)

    def _run(
        self,
        stage: str,
        identity: dict | None,
        fetch: Callable,
        validate: Callable,
        cacheable: Callable = lambda _: True,
        invalid_cache_errors: tuple[type[Exception], ...] = (),
        omit_uncacheable_digest: bool = False,
    ):
        started = time.monotonic()
        key = _digest(identity) if identity is not None else None
        context = _locked(self.root, stage, key) if key else _disabled()
        with context as path:
            cached = None
            state = "MISS" if path is not None else "DISABLED"
            if path is not None and os.path.lexists(path):
                try:
                    entry = _read(path)
                    if (
                        entry["schema_version"] != SCHEMA
                        or entry["identity"] != identity
                        or not cacheable(entry["payload"])
                        or entry["payload_sha256"] != _digest(entry["payload"])
                        or entry["status"] != "SUCCESS"
                    ):
                        raise ValueError("checkpoint identity mismatch")
                    raw = entry["payload"]
                    if isinstance(raw, str) and isinstance(identity.get('model_identity'), dict):
                        from .llm_client import cached_completion
                        raw = cached_completion(raw, identity['model_identity'])
                    validated = validate(raw)
                    cached = (entry, validated)
                except (OSError, ValueError, KeyError, TypeError) + invalid_cache_errors:
                    state = "INVALID_PRESERVED"
            if cached is not None:
                entry, validated = cached
                self._record(
                    stage,
                    served_from_cache=True,
                    provider_calls=0,
                    cache_key=key,
                    payload_sha256=entry["payload_sha256"],
                    elapsed_seconds=time.monotonic() - started,
                    status="HIT",
                )
                return validated
            try:
                from .completed_review_checkpoint import ruling_material_scope
                with ruling_material_scope(identity, cache_allowed=identity is not None, retain=cacheable):
                    payload = fetch()
                validated = validate(payload)
            except Exception:
                self._record(
                    stage,
                    served_from_cache=False,
                    provider_calls=1,
                    cache_key=key,
                    elapsed_seconds=time.monotonic() - started,
                    status="FAILED_NOT_CACHED",
                )
                raise
            persisted = False
            # Read under the requested model, write under the actual answer.
            from src.autoslice.llm_client import completion_identity
            if identity is not None and getattr(payload, 'answering_model', None):
                identity = {**identity, 'model_identity': completion_identity(identity.get('model_identity'), payload)}
                actual_key = _digest(identity)
                if actual_key != key:
                    key = actual_key
                    path = path.parent / (key + '.json') if path is not None else None
            if path is not None and cacheable(payload):
                persisted = _write(
                    path,
                    {
                        "schema_version": SCHEMA,
                        "identity": identity,
                        "status": "SUCCESS",
                        "payload": payload,
                        "payload_sha256": _digest(payload),
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
            self._record(
                stage,
                served_from_cache=bool(getattr(payload, 'ruling_cache_hit', False)),
                provider_calls=0 if getattr(payload, 'ruling_cache_hit', False) else 1,
                cache_key=key,
                payload_sha256=(None if omit_uncacheable_digest and not cacheable(payload)
                                else _digest(payload)),
                elapsed_seconds=time.monotonic() - started,
                status=state,
                persisted=persisted,
            )
            return validated

    def bcut(self, fetch: Callable[[], dict]) -> dict:
        from scripts.free_asr_client import BCUT_MODEL_ID

        identity = {
            "schema_version": SCHEMA,
            "stage": "bcut",
            "audio_sha256": self.audio_sha,
            "model": BCUT_MODEL_ID,
            "client_sha256": _file_sha(ROOT / "scripts/free_asr_client.py"),
        }

        def cacheable(result):
            if not isinstance(result, dict) or result.get("provider") != "bcut":
                return False
            rows = result.get("utterances")
            return (
                isinstance(rows, list)
                and bool(rows)
                and all(
                    isinstance(r, dict)
                    and type(r.get("start_time")) is int
                    and type(r.get("end_time")) is int
                    and 0 <= r["start_time"] < r["end_time"]
                    and isinstance(r.get("transcript"), str)
                    and bool(r["transcript"].strip())
                    for r in rows
                )
            )

        return self._run("bcut", identity, fetch, lambda x: x, cacheable)

    def review(
        self,
        *,
        prompt: str,
        source_srt: str,
        cue_count: int,
        llm_call: Callable,
        validate: Callable,
    ):
        model = getattr(llm_call, "cpa_cache_identity", None)
        identity = None
        if isinstance(model, dict) and model.get("models") and model.get("effort"):
            identity = {
                "schema_version": SCHEMA,
                "stage": "complete-cpa-review",
                "audio_sha256": self.audio_sha,
                "source_srt_sha256": hashlib.sha256(source_srt.encode()).hexdigest(),
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "cue_count": cue_count,
                "model_identity": model,
                "cpa_endpoint_sha256": hashlib.sha256(
                    os.environ.get("CPA_BASE_URL", "").encode()
                ).hexdigest(),
                "implementation": {
                    name: _file_sha(ROOT / name)
                    for name in (
                        "src/autoslice/subtitle_draft_preparation.py",
                        "src/autoslice/full_session_transcription.py",
                        "src/autoslice/llm_client.py",
                        "src/autoslice/transcription_stage_cache.py",
                        "scripts/llm_via_cpa.sh",
                    )
                },
            }
        return self._run("cpa", identity, lambda: llm_call(prompt), validate)

    def pronoun(
        self, *, prompt: str, source_srt: str, llm_call: Callable,
        fetch: Callable, validate: Callable, implementation: tuple[Callable, ...],
        retain: Callable[[object], bool],
    ) -> str:
        """Reuse a complete raw answer, then apply the current occurrence contract."""
        from src.autoslice.llm_client import LlmJsonParseError
        from src.autoslice.source_context_executor import AgyRunnerError

        declared = getattr(llm_call, "cpa_cache_identity", None)
        identity = None
        if isinstance(declared, dict):
            model = {key: declared[key] for key in (
                "transport", "models", "effort", "api_mode", "max_tokens",
                "temperature", "api_base_sha256",
            ) if key in declared}
            known = (
                model.get("transport") in {"direct", "cpa_command", "ssh_runtime_cpa"}
                and isinstance(model.get("models"), list) and bool(model["models"])
                and all(isinstance(v, str) and v.strip() for v in model["models"])
                and isinstance(model.get("effort"), str) and bool(model["effort"].strip())
            )
            try:
                if known and all(retain(v) for v in (prompt, source_srt, json.dumps(model))):
                    identity = {
                        "schema_version": SCHEMA, "stage": "complete-pronoun-review",
                        "audio_sha256": self.audio_sha,
                        "source_srt_sha256": hashlib.sha256(source_srt.encode()).hexdigest(),
                        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                        "model_identity": model,
                        "cpa_endpoint_sha256": hashlib.sha256(
                            os.environ.get("CPA_BASE_URL", "").encode()).hexdigest(),
                        "implementation": {
                            function.__qualname__: hashlib.sha256(
                                inspect.getsource(function).encode()).hexdigest()
                            for function in implementation
                        },
                        "transport_implementation": {
                            name: _file_sha(ROOT / name) for name in (
                                "src/autoslice/llm_client.py", "scripts/llm_via_cpa.sh",
                                "src/autoslice/jingting_chunker.py",
                            )
                        },
                    }
            except (OSError, TypeError, ValueError, UnicodeError):
                identity = None
        return self._run(
            "pronoun", identity, fetch, validate, cacheable=retain,
            invalid_cache_errors=(AgyRunnerError, LlmJsonParseError), omit_uncacheable_digest=True,
        )


@contextmanager
def _disabled():
    yield None
