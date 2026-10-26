"""Candidate placement behind the existing producer/commit interface.

Workers own execution only. The controller retains queue, delivery and upload
authority. SSH failures leave a durable assignment which is queried on resume.
"""
from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
import hashlib
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from src.autoslice import runtime_storage
from src.autoslice.overflow_install import active_repository

CONFIG_ENV = "AUTOSLICE_EXECUTION_CONFIG"
_RECOVERY_ITEM_METADATA = frozenset({
    "runtime_scratch_dir", "selected_repair", "retry_reason",
    "talk_repair_retry_count", "talk_transient_retry_count", "transient_retry_count",
    "talk_dependency_snapshot", "song_dependency_fingerprint",
})


def _json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _terminal_failure_matches(result: dict, assignment: dict) -> bool:
    state=result.get('remote_terminal')
    return (result.get('schema_version')=='autoslice-overflow-terminal-failure.v1'
        and result.get('status')=='failed' and isinstance(state,dict)
        and result.get('candidate_id')==assignment.get('item',{}).get('cid')
        and result.get('profile_binding')==assignment.get('profile_binding')
        and state.get('status')=='failed' and state.get('process_alive') is False
        and state.get('result_present') is False and state.get('bundle_present') is False
        and isinstance(state.get('worker_store_id'),str) and re.fullmatch(r'[0-9a-f]{32}',state['worker_store_id']) is not None
        and state.get('worker_store_id')==assignment.get('worker_authority',{}).get('worker_store_id')
        and all(state.get(k)==assignment.get(k) for k in ('job_id','attempt_id','release_sha256','input_sha256','lane','date'))
        and hashlib.sha256(_json(state)).hexdigest()==result.get('remote_terminal_sha256'))


def _hash(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"non-regular overflow input: {path}")
    digest = _evidence_file_sha256(path)
    return digest


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as handle:
        handle.write(_json(value))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _capture_profile(base: Path, profile_binding: object | None = None):
    """Load and freeze the selected channel profile for one backend run.

    The binding is deliberately resolved once by the backend constructor and
    then passed to every later profile load.  This keeps a changed ambient
    selector from changing the profile used by an already admitted task.
    """

    from src.autoslice.channel_profile import (
        binding_for_profile,
        load_channel_profile,
        require_profile_binding,
        profile_binding_from_environment,
    )

    repo = active_repository(base)
    parent_binding = profile_binding_from_environment()
    if parent_binding is not None and profile_binding is not None:
        profile_binding = require_profile_binding(task_binding=profile_binding,
            current_binding=parent_binding, repo_root=repo)
    profile = load_channel_profile(repo_root=repo, profile_binding=profile_binding)
    binding = binding_for_profile(profile)
    if profile_binding is not None:
        require_profile_binding(task_binding=profile_binding, current_binding=binding, repo_root=repo)
    return profile, binding


def _require_persisted_binding(
    actual: object, expected: object, *, label: str, repo_root: Path | None = None,
) -> dict[str, str]:
    """Turn a stale/missing durable binding into a probe failure.

    Durable assignment and cache files are owned by the controller.  Their
    bytes must stay untouched when the selection no longer matches, so this
    helper only validates and raises.
    """

    from src.autoslice.channel_profile import require_record_profile_binding

    try:
        return require_record_profile_binding(record_binding=actual, task_binding=expected, repo_root=repo_root)
    except (TypeError, ValueError) as exc:
        raise runtime_storage.CapacityProbeError(
            f"{label} is missing or differs from the selected channel profile binding; preserve durable bytes"
        ) from exc


def _source_inputs(
    base: Path,
    date: str,
    item: dict,
    *,
    lane: str | None = None,
    profile_binding: object | None = None,
) -> list[Path]:
    """Only declared source/context inputs, never a library/runtime mirror."""
    paths = {Path(str(item["segment_path"]))} if item.get("segment_path") else set()
    for piece in item.get("pieces", item.get("source_pieces", [])):
        if isinstance(piece, dict):
            raw = piece.get("remote_media", piece.get("segment_path"))
            if raw:
                paths.add(Path(str(raw)))
    if not paths:
        raise runtime_storage.CapacityBudgetError("overflow source input is missing")
    media = list(paths)
    for path in media:
        for suffix in (".xml", ".json", ".jsonl", ".chat.json", ".danmaku.json"):
            sidecar = path.with_suffix(suffix)
            if sidecar.is_file():
                paths.add(sidecar)
        transcript = base / "cache" / date / f"{path.stem}.bcut.srt"
        if transcript.is_file():
            paths.add(transcript)
    for key in ("xml", "bcut_srt_path", "chat_jsonl", "structured_chat_path", "structured_chat_manifest_path",
                "speaker_session_context", "speaker_session_context_path"):
        if item.get(key):
            paths.add(Path(str(item[key])))
    # Intro videos are runtime assets outside Git. Resolve the committed
    # profile policy, including every rotation member, before remote staging,
    # only when the candidate's declared clip form takes the intro.
    from src.autoslice.clip_profile import clip_profile_from_spec, requires_intro
    clip_form = clip_profile_from_spec(item, lane="song" if lane == "song" else "talk")
    if requires_intro(clip_form) and (active_repository(base) / "profiles").is_dir():
        from src.autoslice.branding_intro import require_branding_intro
        from src.autoslice.channel_profile import load_channel_profile
        repo = active_repository(base)
        profile = load_channel_profile(repo_root=repo, profile_binding=profile_binding)
        intro = require_branding_intro(repo,
            manifest_path=profile.asset_file("branding_intro_manifest", repo_root=repo))
        if intro:
            paths.update(Path(row["media_path"]) for row in intro["candidates"])
    for path in paths:
        if not path.is_absolute() or ".." in path.parts or path.is_symlink() or not path.is_file():
            raise runtime_storage.CapacityBudgetError(f"overflow input is unavailable or unsafe: {path}")
        # Workers bind these two roots onto their dedicated data disk. Other
        # source roots require explicit configuration, never root-disk copies.
        if not (path.is_relative_to(base) or path.is_relative_to(Path("/opt/bilive/autoslice"))
                or path.is_relative_to(Path("/path/to/clouddrive2"))):
            raise runtime_storage.CapacityBudgetError("overflow source root has no data-disk binding")
    return sorted(paths)


def _startup_retryable(state: dict) -> bool:
    """Only a failed gate/import attempt with no artifacts may restart."""
    return (
        state.get("status") == "failed"
        and state.get("error_code") == "runtime_prerequisite_failed"
        and state.get("startup_stage") in {"pre_executor", "producer_import"}
        and state.get("process_alive") is False
        and state.get("result_present") is False
        and state.get("bundle_present", False) is False
    )


def _read_imported_result(path: Path) -> dict | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise runtime_storage.CapacityProbeError(
            f"cached overflow result is unsafe; preserve cache bytes: {path}"
        )
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise runtime_storage.CapacityProbeError(
            f"cached overflow result is unreadable; preserve cache bytes: {path}"
        ) from exc
    if not isinstance(value, dict):
        raise runtime_storage.CapacityProbeError(
            f"cached overflow result is invalid; preserve cache bytes: {path}"
        )
    return value


@dataclass
class ExecutionLease:
    reservation: runtime_storage.Reservation
    node: dict | None = None
    assignment: dict | None = None
    node_slot_fd: int | None = None

    def release(self) -> None:
        try:
            self.reservation.release()
        finally:
            if self.node_slot_fd is not None:
                os.close(self.node_slot_fd)
                self.node_slot_fd = None


class LocalBackend:
    """An explicit adapter with the same capacity and producer behavior."""

    def reserve(self, capacity, *, date, item, admission_item, lane, prepare_only):
        reservation, details = capacity.reserve_for_item(
            admission_item, candidate_id=item.get("cid"), date=date
        )
        return (ExecutionLease(reservation) if reservation else None), details

    def execute(self, lease, produce_fn, date, item, kwargs):
        return produce_fn(date, item, **kwargs)


class SSHBackend(LocalBackend):
    def __init__(self, base: Path, config: dict, *, log: Callable[[str], None]):
        self.base, self.config, self.log = Path(base).absolute(), config, log
        # Capture the selected profile before validating or assigning any
        # remote work.  Every durable identity below carries this exact
        # manifest binding, including the raw manifest digest.
        self.profile, self.profile_binding = _capture_profile(
            self.base, config.get("profile_binding")
        )
        self.release = config["release_sha256"]
        if not isinstance(self.release, str) or not re.fullmatch(r"[0-9a-f]{64}", self.release):
            raise ValueError("overflow release is invalid")
        from src.autoslice.overflow_release import verify
        self.repo = active_repository(self.base)
        verify(self.repo, self.release)
        self._compatible_releases = {self.release}
        self._readable_releases = {self.release}
        self.nodes = config.get("nodes", [])
        if not isinstance(self.nodes, list) or not self.nodes:
            raise ValueError("overflow nodes are missing")
        for node in self.nodes:
            if not re.fullmatch(r"[A-Za-z0-9_.@-]+", node.get("host", "")):
                raise ValueError("overflow SSH host is invalid")
            if Path(node["runtime_root"]) != self.base:
                raise ValueError("overflow workers must share the controller logical runtime path")
        self.poll_seconds = float(config.get("poll_seconds", 5))
        self.timeout_seconds = float(config.get("timeout_seconds", 7200))
        if not 0.01 <= self.poll_seconds <= 60 or not 1 <= self.timeout_seconds <= 14400:
            raise ValueError("overflow wait bounds are invalid")

    def _ssh(self, node: dict) -> list[str]:
        args = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
                "-o", "ServerAliveCountMax=2"]
        if node.get("identity_file"):
            args += ["-i", node["identity_file"], "-o", "IdentitiesOnly=yes"]
        if node.get("known_hosts_file"):
            args += ["-o", "UserKnownHostsFile=" + node["known_hosts_file"]]
        return args + [node["host"]]

    def _rpc(self, node: dict, action: str, request: dict | None = None, identity: dict | None = None):
        release = (identity or request or {}).get('release_sha256',self.release)
        if release != self.release:
            if node.get('versioned') is not True:
                raise runtime_storage.CapacityProbeError('assigned overflow release changed; preserve assignment')
            self._verify_assigned_release(release,require_producer_compatibility=action not in {'status','probe','fetch-description'})
        worker_repo = (self.base / "versions" / release / "repo"
                       if node.get("versioned") is True else self.base / "repo")
        command = ["sudo", "-n", str(self.base / "venv-main/bin/python"),
                   str(worker_repo / "scripts/autoslice_overflow_worker.py"),
                   "--worker-root", node["worker_root"],
                   "--allowed-runtime-parent", str(self.base.parent),
                   "--runtime-root", str(self.base), action]
        if identity and action != 'probe':
            command += ["--job-id", identity["job_id"], "--attempt-id", identity["attempt_id"]]
        completed = subprocess.run(
            self._ssh(node) + [shlex.join(command)], input=_json(request) if request else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False,
        )
        if completed.returncode:
            try:
                failure = json.loads(completed.stdout)
            except (TypeError, UnicodeError, json.JSONDecodeError):
                failure = None
            if isinstance(failure, dict) and failure.get("error") == "runtime_prerequisite_failed":
                from src.autoslice.producer_request import ProducerRuntimePrerequisiteError

                raise ProducerRuntimePrerequisiteError(
                    "overflow_worker", "startup preflight failed; assignment retained"
                )
            raise ConnectionError(f"overflow {action} failed on {node['host']} (rc={completed.returncode})")
        value = json.loads(completed.stdout)
        if not isinstance(value, dict):
            raise ValueError("overflow response is not an object")
        return value

    def _verify_assigned_release(self, release: str, *, require_producer_compatibility: bool = True) -> None:
        """Resume original worker code after a controller-only transport fix."""
        if not isinstance(release,str) or re.fullmatch(r'[0-9a-f]{64}',release) is None:
            raise runtime_storage.CapacityProbeError('assigned overflow release changed; preserve assignment')
        if release in self._compatible_releases:
            return
        if not require_producer_compatibility and release in self._readable_releases:
            return
        from src.autoslice.overflow_release import verify
        original = None
        for marker in (self.base/'versions').glob('*/repo/OVERFLOW_RELEASE_SHA256'):
            if not marker.is_symlink() and marker.read_text().strip()==release:
                original=marker.parent
                break
        if original is None:
            raise runtime_storage.CapacityProbeError('assigned overflow release unavailable; preserve assignment')
        try:
            verify(original,release)
        except (OSError,ValueError) as exc:
            raise runtime_storage.CapacityProbeError('assigned overflow release changed; preserve assignment') from exc
        self._readable_releases.add(release)
        if not require_producer_compatibility:
            return
        before=json.loads((original/'OVERFLOW_RELEASE_MANIFEST.json').read_text())['files']
        after=json.loads((self.repo/'OVERFLOW_RELEASE_MANIFEST.json').read_text())['files']
        controller_only={'src/autoslice/execution_backend.py','src/autoslice/runner_batch_dispatch.py',
                         'src/autoslice/production_attempts.py','src/autoslice/reporting.py',
                         'scripts/prepare_slice_batch.py','DEPLOYED_COMMIT','DEPLOYED_AUTHORITY_MANIFEST.json'}
        changed={key for key in set(before)|set(after) if before.get(key)!=after.get(key)}
        if changed-controller_only:
            raise runtime_storage.CapacityProbeError('assigned overflow producer release changed; preserve assignment')
        self._compatible_releases.add(release)

    def _consume_terminal_failure(self, assignment: dict, node: dict, state: dict) -> dict:
        result={'schema_version':'autoslice-overflow-terminal-failure.v1','status':'failed','rc':-1,
            'candidate_id':assignment['item']['cid'],'profile_binding':assignment['profile_binding'],
            'failure_kind':state.get('error_code') or 'execution_failed','failure_recoverable':True,
            'result_source':'WORKER_TERMINAL_STATUS_WITHOUT_PRODUCER_RESULT',
            'remote_terminal':state,'remote_terminal_sha256':hashlib.sha256(_json(state)).hexdigest()}
        if not _terminal_failure_matches(result,assignment):
            raise runtime_storage.CapacityProbeError('overflow terminal failure is unbound or not safely finished; preserve assignment')
        path=self.base/'overflow-dispatch'/(assignment['job_id']+'.imported.json')
        _write(path,result)
        from src.autoslice.production_attempts import observed_production_attempt, record_production_result
        with observed_production_attempt(runtime_root=self.base,date=assignment['date'],candidate_id=result['candidate_id'],
                lane=assignment['lane'],execution_node=node['host'],
                trace=self.base/'reports/production-attempt-traces'/(uuid.uuid4().hex+'.jsonl')):
            record_production_result(result)
        return result

    def _identity(self, date: str, item: dict, lane: str) -> tuple[str, dict]:
        work = {key: value for key, value in item.items() if key != "runtime_scratch_dir"}
        body = {
            "date": date,
            "lane": lane,
            "item": work,
            "release_sha256": self.release,
            "profile_binding": dict(self.profile_binding),
        }
        return hashlib.sha256(_json(body)).hexdigest(), body

    def _recovery_lease(self, capacity, node: dict, assignment: dict):
        """Reserve no fresh peak for an already accepted remote attempt."""
        # Recovery itself does not allocate a fresh producer workspace.  The
        # remote completed path performs its actual return-capacity check just
        # before fetching, while a controller-local imported handle is read
        # directly.  Keep a normal releasable reservation object with zero
        # bytes so dispatcher cleanup remains uniform even at free=0.
        slot_fd = self._claim_node_slot(node)
        if slot_fd is None:
            return None, {'reason':'no-compatible-overflow-worker'}
        reservation = runtime_storage.Reservation(capacity, 0)
        return ExecutionLease(reservation, node, assignment, slot_fd), {
            "backend": "ssh", "node": node.get("host"), "recovery": True,
        }

    def _claim_node_slot(self, node: dict) -> int | None:
        """Hold controller admission from probe through submit and consumption.

        The worker's own slot remains authoritative for actual execution. This
        claim closes the gap before submit so a parallel candidate selects the
        next node instead of racing for the same still-free worker.
        """
        directory = self.base/'overflow-dispatch'/'.node-slots'
        directory.mkdir(parents=True,exist_ok=True)
        name = hashlib.sha256(node['host'].encode()).hexdigest()+'.lock'
        fd = os.open(directory/name,os.O_CREAT|os.O_WRONLY|getattr(os,'O_NOFOLLOW',0),0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise runtime_storage.CapacityProbeError('overflow node admission lock is not regular')
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                return None
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _verify_live_profile(self) -> None:
        """Re-read the bound manifest before any remote or promotion action."""

        from src.autoslice.channel_profile import binding_for_profile, require_profile_binding
        require_profile_binding(task_binding=self.profile_binding,
            current_binding=binding_for_profile(self.profile), repo_root=self.repo)
        profile, binding = _capture_profile(self.base, self.profile_binding)
        if binding != self.profile_binding:
            raise runtime_storage.CapacityProbeError(
                "selected channel profile binding changed; preserve durable assignment"
            )
        self.profile = profile

    def _validate_assignment(self, assignment: object, *, label: str) -> dict:
        if not isinstance(assignment, dict):
            raise runtime_storage.CapacityProbeError(
                f"{label} is invalid; preserve durable assignment bytes"
            )
        _require_persisted_binding(
            assignment.get("profile_binding"), self.profile_binding, label=label, repo_root=self.repo
        )
        from src.autoslice.overflow_worker import compute_job_id
        identity = compute_job_id(assignment)
        if assignment.get("job_id") != identity or assignment.get("input_sha256") != identity:
            raise runtime_storage.CapacityProbeError(f"{label} identity differs; preserve assignment bytes")
        return assignment

    def _validate_cached_result(self, cached: object, *, label: str, assignment: dict | None = None) -> dict:
        if not isinstance(cached, dict):
            raise runtime_storage.CapacityProbeError(
                f"{label} is invalid; preserve cached result bytes"
            )
        _require_persisted_binding(
            cached.get("profile_binding"), self.profile_binding, label=label, repo_root=self.repo
        )
        if (cached.get('schema_version')=='autoslice-overflow-terminal-failure.v1'
                and (assignment is None or not _terminal_failure_matches(cached,assignment))):
            raise runtime_storage.CapacityProbeError('overflow terminal failure binding differs; preserve assignment')
        return cached

    def _worker_authority(self, node: dict, store_id: str | None = None) -> dict:
        root = Path(node["worker_root"])
        if not root.is_absolute() or ".." in root.parts:
            raise runtime_storage.CapacityProbeError("overflow worker root is invalid")
        authority = {"node": node["host"], "worker_root": str(root), "runtime_root": str(self.base)}
        if store_id is not None:
            authority["worker_store_id"] = store_id
        return authority

    def _configured_authority_matches(self, assignment: dict, node: dict) -> bool:
        authority = assignment.get("worker_authority")
        return isinstance(authority, dict) and authority == self._worker_authority(
            node, authority.get("worker_store_id")
        )

    def _require_store_response(self, assignment: dict, response: dict) -> None:
        store_id = assignment.get("worker_authority", {}).get("worker_store_id")
        if (not isinstance(store_id, str) or not re.fullmatch(r"[0-9a-f]{32}", store_id)
                or response.get("worker_store_id") != store_id):
            raise runtime_storage.CapacityProbeError("overflow worker store binding missing or changed; preserve assignment")

    def _probe_authority(self, node: dict, probe: dict, *, expected_release: str | None = None) -> None:
        if probe.get("startup_ok") is not True:
            raise runtime_storage.CapacityProbeError("assigned overflow worker failed startup preflight; preserve assignment")
        authority = self._worker_authority(node)
        if (probe.get("release_sha256") != (expected_release or self.release)
                or any(probe.get(key) != authority[key] for key in ("worker_root", "runtime_root"))):
            raise runtime_storage.CapacityProbeError("overflow probe authority differs; preserve assignment")
        if not isinstance(probe.get("worker_store_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", probe["worker_store_id"]):
            raise runtime_storage.CapacityProbeError("overflow probe store binding missing; preserve assignment")

    def _saved_request(self, assignment: dict, node: dict, *, kwargs: dict | None = None) -> dict:
        from src.autoslice.overflow_worker import OverflowWorkerError, validate_request, request_sha256
        try:
            if not self._configured_authority_matches(assignment, node):
                raise ValueError("worker authority changed or is missing")
            store_id = assignment["worker_authority"].get("worker_store_id")
            self._require_store_response(assignment, {"worker_store_id": store_id})
            self._verify_assigned_release(assignment['release_sha256'])
            request = assignment["request"]
            checked = validate_request(request, allowed_runtime_parent=str(self.base.parent),
                                       expected_runtime_root=str(self.base))
            if request_sha256(checked) != assignment.get("request_sha256"):
                raise ValueError("saved request digest differs")
            if checked.get("worker_store_id") != store_id:
                raise ValueError("saved request store binding differs")
            for key in ("job_id", "attempt_id", "input_sha256", "release_sha256", "lane", "date", "item", "profile_binding"):
                if checked[key] != assignment[key]:
                    raise ValueError("saved request identity differs")
            if kwargs is not None and checked["produce_kwargs"] != kwargs:
                raise ValueError("producer arguments changed")
            for source in checked["source_files"]:
                path = Path(source["path"])
                if path.stat().st_size != source["bytes"] or _hash(path) != source["sha256"]:
                    raise ValueError("source preimage changed")
            return checked
        except (KeyError, OSError, TypeError, ValueError, OverflowWorkerError) as exc:
            raise runtime_storage.CapacityProbeError(
                "overflow saved request/authority/source differs; preserve assignment"
            ) from exc

    def _absent_recovery(self, assignment: dict, node: dict, state: dict, item: dict) -> None:
        if any(state.get(key) != assignment[key] for key in ("job_id", "attempt_id")):
            raise runtime_storage.CapacityProbeError("overflow absence identity differs; preserve assignment")
        if not self._configured_authority_matches(assignment, node):
            raise runtime_storage.CapacityProbeError("overflow worker authority changed or is missing; preserve assignment")
        self._require_store_response(assignment, state)
        self._verify_assigned_release(assignment['release_sha256'])
        def work(row):
            return {k: v for k, v in row.items() if k not in _RECOVERY_ITEM_METADATA}

        if work(item) != work(assignment["item"]):
            raise runtime_storage.CapacityProbeError("absent overflow input changed; preserve assignment")
        if assignment.get("phase") == "accepted":
            raise runtime_storage.CapacityProbeError("accepted overflow state disappeared; preserve assignment")
        if assignment.get("phase") == "submitting":
            self._saved_request(assignment, node)
        elif assignment.get("phase") != "reserved":
            raise runtime_storage.CapacityProbeError("overflow absence has no safe submission phase; preserve assignment")
        probe = self._rpc(node, "probe", identity=assignment)
        self._probe_authority(node, probe,expected_release=assignment['release_sha256'])
        self._require_store_response(assignment, probe)

    def reserve(self, capacity, *, date, item, admission_item, lane, prepare_only):
        if not prepare_only:
            return super().reserve(capacity, date=date, item=item, admission_item=admission_item,
                                   lane=lane, prepare_only=prepare_only)
        self._verify_live_profile()
        prepared_recovery = item.get("prepared_recovery")
        required_store = None
        if prepared_recovery is not None:
            if (lane != "talk" or not isinstance(prepared_recovery, dict)
                    or re.fullmatch(r"[0-9a-f]{32}", str(prepared_recovery.get("worker_store_id") or "")) is None):
                raise runtime_storage.CapacityBudgetError("prepared recovery requires its original Talk worker store")
            required_store = prepared_recovery["worker_store_id"]
        key, body = self._identity(date, item, lane)
        assignment_path = self.base / "overflow-dispatch" / f"{key}.json"
        recovery_assignment: dict | None = None
        recovery_assignment_path: Path | None = None
        recovery_node: dict | None = None
        recovery_state: dict | None = None
        cid = str(item.get("cid") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", cid) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise runtime_storage.CapacityBudgetError("overflow candidate/date is invalid")
        pointer = self.base / "overflow-dispatch" / "candidates" / f"{date}-{lane}-{cid}.json"
        if pointer.exists():
            try:
                pointer_value = json.loads(pointer.read_text())
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise runtime_storage.CapacityProbeError(
                    "overflow candidate pointer is unreadable; preserve pointer bytes"
                ) from exc
            if not isinstance(pointer_value, dict):
                raise runtime_storage.CapacityProbeError(
                    "overflow candidate pointer is invalid; preserve pointer bytes"
                )
            _require_persisted_binding(
                pointer_value.get("profile_binding"), self.profile_binding,
                label="overflow candidate pointer profile binding", repo_root=self.repo,
            )
        else:
            pointer_value = None
        if pointer_value is not None and pointer_value.get("job_id") != key:
            previous = pointer_value
            old_path = self.base / "overflow-dispatch" / f"{previous['job_id']}.json"
            try:
                old = json.loads(old_path.read_text())
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise runtime_storage.CapacityProbeError(
                    "previous overflow assignment is unreadable; preserve assignment bytes"
                ) from exc
            old = self._validate_assignment(old, label="previous overflow assignment")
            old_nodes = [node for node in self.nodes if node["host"] == old["node"]]
            if old_nodes and "worker_authority" in old and not self._configured_authority_matches(old, old_nodes[0]):
                raise runtime_storage.CapacityProbeError("previous worker authority changed; preserve assignment")
            imported = old_path.with_suffix(".imported.json")
            old_result = _read_imported_result(imported)
            if old_result is not None:
                old_result = self._validate_cached_result(
                    old_result, label="cached overflow result profile binding", assignment=old
                )
            if old_result is not None and old_result.get("status") == "delivery_prepared_no_target":
                # A controller-local imported result is already the durable
                # handoff.  It must be consumable without asking the worker to
                # start anything or requiring its current release to match.
                recovery_assignment = old
                recovery_assignment_path = old_path
                recovery_node = next(iter(old_nodes), {"host": old.get("node")})
                recovery_state = {"status": "completed", "result_present": True}
            else:
                if not old_nodes:
                    raise runtime_storage.CapacityProbeError("previous worker is missing; preserve candidate assignment")
                try:
                    old_state = self._rpc(old_nodes[0], "status", identity=old)
                except (ConnectionError, subprocess.TimeoutExpired) as exc:
                    raise runtime_storage.CapacityProbeError("previous candidate assignment is unreachable") from exc
                old_status = old_state.get("status")
                if "worker_store_id" in old.get("worker_authority", {}):
                    self._require_store_response(old, old_state)
                if old_status == "absent":
                    self._absent_recovery(old, old_nodes[0], old_state, item)
                    recovery_assignment = old
                    recovery_assignment_path = old_path
                    recovery_node = old_nodes[0]
                    recovery_state = old_state
                elif (old_status in {"submitted", "running", "unknown"}
                        or (old_status == "completed" and old_result is None)
                        or _startup_retryable(old_state)):
                    recovery_assignment = old
                    recovery_assignment_path = old_path
                    recovery_node = old_nodes[0]
                    recovery_state = old_state
                else:
                    confirmed_failure = old_status == "failed" or (old_status == "completed" and old_result is not None)
                    if not confirmed_failure:
                        raise runtime_storage.CapacityProbeError("candidate has a different durable overflow assignment; recover it before retry")
                    if old_state.get("error_code") == "runtime_prerequisite_failed":
                        raise runtime_storage.CapacityProbeError(
                            "previous candidate startup preflight failed after executor start; preserve assignment"
                        )
                    if old_status=='failed' and old_result is None:
                        self._consume_terminal_failure(old,old_nodes[0],old_state)
        existing = recovery_assignment
        if recovery_assignment is None and assignment_path.is_file():
            try:
                existing = json.loads(assignment_path.read_text())
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise runtime_storage.CapacityProbeError(
                    "overflow assignment is unreadable; preserve assignment bytes"
                ) from exc
            existing = self._validate_assignment(existing, label="overflow assignment")
        if recovery_assignment is not None:
            assignment_path = recovery_assignment_path or assignment_path
        if existing is not None and recovery_state is None:
            imported = _read_imported_result(assignment_path.with_suffix(".imported.json"))
            if imported is not None:
                imported = self._validate_cached_result(
                    imported, label="cached overflow result profile binding", assignment=existing
                )
            nodes = [node for node in self.nodes if node["host"] == existing.get("node")]
            if nodes and "worker_authority" in existing and not self._configured_authority_matches(existing, nodes[0]):
                raise runtime_storage.CapacityProbeError("assigned worker authority changed; preserve assignment")
            if imported is not None:
                recovery_node = next(iter(nodes), {"host": existing.get("node")})
                recovery_state = {"status": "completed", "result_present": True}
            else:
                if not nodes:
                    raise runtime_storage.CapacityProbeError("assigned worker is missing; preserve assignment")
                try:
                    exact_state = self._rpc(nodes[0], "status", identity=existing)
                except (ConnectionError, subprocess.TimeoutExpired) as exc:
                    raise runtime_storage.CapacityProbeError("assigned overflow worker is unreachable; preserve assignment") from exc
                if "worker_store_id" in existing.get("worker_authority", {}):
                    self._require_store_response(existing, exact_state)
                if exact_state.get("status") in {"submitted", "running", "completed", "unknown"} or _startup_retryable(exact_state):
                    recovery_node = nodes[0]
                    recovery_state = exact_state
                elif exact_state.get("status") == "absent":
                    self._absent_recovery(existing, nodes[0], exact_state, item)
                elif exact_state.get("error_code") == "runtime_prerequisite_failed":
                    raise runtime_storage.CapacityProbeError(
                        "assigned candidate startup preflight failed after executor start; preserve assignment"
                    )
        if recovery_state is not None and recovery_node is not None and existing is not None:
            return self._recovery_lease(capacity, recovery_node, existing)
        # An existing assignment must be recovered before considering local
        # execution; a disconnect never grants permission to run it twice.
        if existing is None and required_store is None and self.config.get("mode", "auto") == "auto":
            local, details = super().reserve(capacity, date=date, item=item,
                admission_item=admission_item, lane=lane, prepare_only=prepare_only)
            if local:
                return local, {**details, "backend": "local"}
        budget = runtime_storage.estimate_peak_allocation(admission_item,
            peak_bytes_per_second=capacity.peak_bytes_per_second)
        source_paths = _source_inputs(
            self.base, date, item, lane=lane,
            profile_binding=self.profile_binding,
        )
        source_copy_bytes = sum(path.stat().st_size for path in source_paths)
        worker_peak = budget.estimated_peak_bytes + source_copy_bytes
        if budget.duration_ms is None:
            raise runtime_storage.CapacityBudgetError("overflow return duration is unknown")
        # A conservative return estimate is separate from the larger workspace
        # estimate. Actual payload size is checked again before any fetch.
        rate = self.config.get("return_peak_bytes_per_second", 4 * 1024 * 1024)
        if isinstance(rate, bool) or not isinstance(rate, int) or rate <= 0:
            raise runtime_storage.CapacityBudgetError("overflow return peak rate is invalid")
        return_peak = ((budget.duration_ms + 999) // 1000) * rate + 128 * 1024 * 1024
        reservation = capacity.reserve_bytes(return_peak)
        if reservation is None:
            return None, {"reason": "insufficient-return-capacity", "required_bytes": return_peak + capacity.operational_reserve_bytes}
        try:
            nodes = self.nodes if existing is None else [node for node in self.nodes if node["host"] == existing["node"]]
            for node in nodes:
                slot_fd = self._claim_node_slot(node)
                if slot_fd is None:
                    continue
                try:
                    try:
                        probe = self._rpc(node, "probe")
                    except (ConnectionError, subprocess.TimeoutExpired):
                        if existing is not None:
                            raise runtime_storage.CapacityProbeError("assigned overflow worker is unreachable; preserve assignment")
                        continue
                    if probe.get("release_sha256") != self.release:
                        continue
                    # Marker/free/slot metadata is intentionally insufficient for
                    # admission.  The worker probe performs the same sealed
                    # runtime and adapter startup check that submit/run repeat.
                    # An existing assignment stays bound to its job and must be
                    # recovered instead of being silently reassigned or run local.
                    if probe.get("startup_ok") is not True:
                        if existing is not None:
                            raise runtime_storage.CapacityProbeError(
                                "assigned overflow worker failed startup preflight; preserve assignment"
                            )
                        continue
                    try:
                        self._probe_authority(node, probe)
                    except runtime_storage.CapacityProbeError:
                        if existing is not None:
                            raise
                        continue
                    if required_store is not None and probe.get("worker_store_id") != required_store:
                        continue
                    if existing is None and (probe.get("slot_busy") or probe.get("available_bytes", 0) < worker_peak + capacity.operational_reserve_bytes):
                        continue
                    assignment = existing or {"job_id": key, "attempt_id": uuid.uuid4().hex,
                        "node": node["host"], "input_sha256": hashlib.sha256(_json(body)).hexdigest(),
                        "release_sha256": self.release, "lane": lane, "date": date,
                        "item": body["item"], "estimated_peak_bytes": worker_peak,
                        "phase": "reserved",
                        "return_peak_bytes": return_peak,
                        "operational_reserve_bytes": capacity.operational_reserve_bytes,
                        "profile_binding": self.profile_binding,
                        "worker_authority": self._worker_authority(node, probe["worker_store_id"])}
                    if existing is None:
                        _write(assignment_path, assignment)
                        _write(pointer, {
                            "job_id": key,
                            "attempt_id": assignment["attempt_id"],
                            "node": node["host"],
                            "profile_binding": self.profile_binding,
                        })
                    self.log(f"overflow assigned {item.get('cid')} to {node['host']} ({key[:12]})")
                    lease = ExecutionLease(reservation, node, assignment, slot_fd)
                    slot_fd = None
                    return lease, {"backend": "ssh", "node": node["host"], "required_bytes": return_peak}
                finally:
                    if slot_fd is not None:
                        os.close(slot_fd)
            reservation.release()
            return None, {"reason": "no-compatible-overflow-worker"}
        except BaseException:
            reservation.release()
            raise

    def _copy(self, node: dict, source: str, target: str, *, fetch: bool = False) -> None:
        transport = shlex.join(self._ssh(node)[:-1])
        remote = node["host"] + ":" + (source if fetch else target)
        args = ["rsync", "-a", "--protect-args", "--checksum",
                "--rsync-path=sudo -n rsync", "-e", transport]
        if not fetch:
            path = Path(source)
            root = (self.base if path.is_relative_to(self.base)
                    else Path("/opt/bilive/autoslice") if path.is_relative_to(Path("/opt/bilive/autoslice"))
                    else Path("/path/to/clouddrive2"))
            if not path.is_relative_to(root):
                raise ValueError("overflow transfer escapes a bound worker input root")
            relative_source = str(root) + "/./" + str(path.relative_to(root))
            args += ["--ignore-existing", "--relative", "--", relative_source,
                     node["host"] + ":" + str(root) + "/"]
        else:
            args += ["--", remote, target]
        subprocess.run(args, check=True, timeout=3600, stdout=subprocess.DEVNULL)

    def _stage_sources(self, node: dict, date: str, item: dict, *, lane: str) -> list[dict]:
        rows = []
        for path in _source_inputs(
            self.base, date, item, lane=lane,
            profile_binding=self.profile_binding,
        ):
            before = _hash(path)
            self._copy(node, str(path), str(path))
            if _hash(path) != before:
                raise ValueError("overflow source changed during transfer")
            rows.append({"path": str(path), "bytes": path.stat().st_size, "sha256": before})
        return rows

    def execute(self, lease, produce_fn, date, item, kwargs):
        if lease.node is None:
            return super().execute(lease, produce_fn, date, item, kwargs)
        from src.autoslice.production_attempts import observed_production_attempt, record_production_result
        with observed_production_attempt(runtime_root=self.base,date=date,
                candidate_id=str(item.get('cid') or ''),lane=lease.assignment['lane'],
                trace=self.base/'reports/production-attempt-traces'/(uuid.uuid4().hex+'.jsonl'),
                execution_node=lease.node['host']):
            result=self._execute_remote(lease,produce_fn,date,item,kwargs)
            record_production_result(result)
            return result

    def _execute_remote(self, lease, produce_fn, date, item, kwargs):
        node, assignment = lease.node, lease.assignment
        self._verify_live_profile()
        assignment = self._validate_assignment(
            assignment, label="overflow assignment profile binding"
        )
        assignment_path = self.base / "overflow-dispatch" / f"{assignment['job_id']}.json"
        try:
            persisted_assignment = json.loads(assignment_path.read_text())
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise runtime_storage.CapacityProbeError(
                "overflow assignment is unreadable; preserve assignment bytes"
            ) from exc
        persisted_assignment = self._validate_assignment(
            persisted_assignment, label="persisted overflow assignment profile binding"
        )
        if (
            persisted_assignment.get("job_id") != assignment.get("job_id")
            or persisted_assignment.get("attempt_id") != assignment.get("attempt_id")
        ):
            raise runtime_storage.CapacityProbeError(
                "persisted overflow assignment identity differs; preserve assignment bytes"
            )
        imported_path = assignment_path.with_suffix(".imported.json")
        if imported_path.exists():
            cached = _read_imported_result(imported_path)
            if cached is None:
                raise runtime_storage.CapacityProbeError(
                    "cached overflow result is unsafe; preserve cache bytes"
                )
            cached = self._validate_cached_result(
                cached, label="cached overflow result profile binding", assignment=assignment
            )
            if cached.get("status") == "delivery_prepared_no_target":
                from src.autoslice.producer_batch_projection import prepared_batch_entry
                from src.autoslice.producer_delivery_transaction import load_prepared_delivery
                entry = prepared_batch_entry(result=cached, runtime_root=self.base,
                    lane=assignment["lane"], candidate_id=assignment["item"]["cid"])
                if entry is None:
                    raise RuntimeError("cached overflow result has no prepared handle")
                load_prepared_delivery(runtime_root=self.base, manifest_path=entry.handle.manifest_path)
            return cached

        def submit_attempt():
            saved = self._saved_request(assignment, node, kwargs=kwargs) if "request" in assignment else None
            sources = self._stage_sources(node, date, assignment["item"], lane=assignment["lane"])
            request = saved or {"schema_version": "autoslice-overflow-request.v2",
                       **{key: assignment[key] for key in ("job_id", "attempt_id", "release_sha256", "input_sha256", "lane", "date", "item", "estimated_peak_bytes", "operational_reserve_bytes")},
                       "profile_binding": assignment["profile_binding"],
                       "worker_store_id": assignment["worker_authority"]["worker_store_id"],
                       "runtime_root": str(self.base), "controller_root": str(self.base),
                       "source_files": sources, "produce_kwargs": kwargs}
            if saved is not None and sources != saved["source_files"]:
                raise runtime_storage.CapacityProbeError("overflow staged sources differ from saved request; preserve assignment")
            from src.autoslice.overflow_worker import request_sha256
            assignment["request"] = request
            assignment["request_sha256"] = request_sha256(request)
            assignment["phase"] = "submitting"
            _write(assignment_path, assignment)
            try:
                return self._rpc(node, "submit", request=request)
            except (ConnectionError, subprocess.TimeoutExpired):
                # Query the exact durable identity after ambiguous acceptance.
                return self._rpc(node, "status", identity=assignment)

        state = self._rpc(node, "status", identity=assignment)
        if "worker_store_id" in assignment.get("worker_authority", {}):
            self._require_store_response(assignment, state)
        if state.get("status") == "absent":
            self._absent_recovery(assignment, node, state, item)
            state = submit_attempt()
        elif _startup_retryable(state):
            state = submit_attempt()

        def record_acceptance(observed):
            if "worker_store_id" in assignment.get("worker_authority", {}):
                self._require_store_response(assignment, observed)
            if any(key in observed and observed[key] != assignment[key] for key in ("job_id", "attempt_id")):
                raise runtime_storage.CapacityProbeError("overflow response identity differs; preserve assignment")
            if (observed.get("status") != "absent"
                    and all(observed.get(key) == assignment[key] for key in ("job_id", "attempt_id"))
                    and assignment.get("phase") != "accepted"):
                assignment["phase"] = "accepted"
                _write(assignment_path, assignment)

        record_acceptance(state)
        started = time.monotonic()
        while state.get("status") in {"submitted", "running"}:
            if time.monotonic() - started > self.timeout_seconds:
                raise RuntimeError("overflow task still running; resume the preserved assignment")
            time.sleep(self.poll_seconds)
            state = self._rpc(node, "status", identity=assignment)
            record_acceptance(state)
        if state.get("status") != "completed":
            if state.get("error_code") == "runtime_prerequisite_failed":
                from src.autoslice.producer_request import ProducerRuntimePrerequisiteError

                raise ProducerRuntimePrerequisiteError(
                    "overflow_worker", "startup preflight failed; assignment retained"
                )
            if state.get('status')=='failed':
                return self._consume_terminal_failure(assignment,node,state)
            raise RuntimeError(f"overflow task {state.get('status', 'unknown')}; assignment retained")
        description = self._rpc(node, "fetch-description", identity=assignment)
        from src.autoslice.overflow_bundle import import_bundle
        staging = self.base / "overflow-import" / assignment["job_id"] / assignment["attempt_id"]
        staging.mkdir(parents=True, exist_ok=True)
        remote_bundle = description["bundle_path"]
        total = description["bundle_bytes"]
        # Import has one received copy and one fresh prepared copy. Reserved
        # bytes are an estimate; never fetch when the actual size exceeds free.
        free = shutil.disk_usage(self.base).free
        if free < 3 * total + assignment["operational_reserve_bytes"]:
            raise RuntimeError("overflow completed remotely; controller lacks actual return capacity")
        self._copy(node, remote_bundle.rstrip("/") + "/", str(staging) + "/", fetch=True)
        result = import_bundle(bundle_dir=staging, runtime_root=self.base,
            expected_job_id=assignment["job_id"], expected_attempt_id=assignment["attempt_id"],
            expected_release_sha256=assignment["release_sha256"], expected_input_sha256=assignment["input_sha256"],
            expected_profile_binding=self.profile_binding)
        result = self._validate_cached_result(
            result, label="imported overflow result profile binding"
        )
        _write(imported_path, result)
        return result


def _has_unconsumed_assignment(base: Path) -> bool:
    """A missing/unbound import never authorizes local replacement work."""
    dispatch = Path(base) / "overflow-dispatch"
    assignments = set(dispatch.glob("*.json"))
    for pointer in (dispatch / "candidates").glob("*.json"):
        try:
            identity = json.loads(pointer.read_text())
            job_id = identity["job_id"]
            if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{64}", job_id):
                raise ValueError("invalid pointer")
            assignment_path = dispatch / f"{job_id}.json"
            assignment = json.loads(assignment_path.read_text())
            _require_persisted_binding(identity.get("profile_binding"), assignment.get("profile_binding"), label="overflow pointer binding")
            assignments.add(assignment_path)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise runtime_storage.CapacityProbeError("unbound or invalid overflow pointer; preserve durable assignment") from exc
    for path in assignments:
        if path.name.endswith(".imported.json"):
            continue
        try:
            assignment = json.loads(path.read_text())
            cached = _read_imported_result(path.with_suffix(".imported.json"))
            if cached is None:
                return True
            # This is a consumed-state check, not promotion. Compare the
            # cached result with its own task, permitting other profile tasks.
            _require_persisted_binding(cached.get("profile_binding"), assignment.get("profile_binding"), label="cached overflow task binding")
            if cached.get('schema_version')=='autoslice-overflow-terminal-failure.v1' and not _terminal_failure_matches(cached,assignment):
                raise ValueError('overflow terminal failure binding differs')
        except (OSError, TypeError, ValueError) as exc:
            raise runtime_storage.CapacityProbeError("unbound or invalid overflow import; preserve durable assignment") from exc
    return False


def backend_from_environment(base: Path, *, log: Callable[[str], None]):
    path = os.environ.get(CONFIG_ENV)
    if path is None:
        default = Path(base) / "execution-backends.json"
        path = str(default) if default.is_file() else None
    if not path:
        if _has_unconsumed_assignment(base):
            raise runtime_storage.CapacityProbeError("overflow configuration removed with an unconsumed assignment; recover before local fallback")
        return None
    config = json.loads(Path(path).read_text())
    if config.get("mode") == "local":
        if _has_unconsumed_assignment(base):
            raise runtime_storage.CapacityProbeError("overflow rollback has an unconsumed assignment; recover before local fallback")
        return None
    if config.get("mode") not in {"auto", "remote"}:
        raise ValueError("overflow mode must be local, auto or remote")
    from src.autoslice.overflow_release import ReleaseMismatchError
    try:
        return SSHBackend(base, config, log=log)
    except ReleaseMismatchError:
        if config["mode"] != "auto":
            raise
        # A code update must not hide an accepted remote job and run the same
        # queued candidate locally. Imported results are already controller
        # owned; assignments without a durable import still need recovery.
        if _has_unconsumed_assignment(base):
            raise runtime_storage.CapacityProbeError("overflow release changed with an unconsumed assignment; recover before local fallback")
        log("overflow release changed; using local capacity admission until workers are synchronized")
        return None
