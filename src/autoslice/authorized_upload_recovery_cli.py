"""Crash-safe completion for manifest-bound uploads.

The uploader process can create a remote archive before local sidecars or the
terminal ledger row are durable.  This module keeps that recovery path out of
the already-large public CLI while preserving its adapters and policy gates.
"""

from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable


class UploadRecoveryError(RuntimeError):
    """An explicit-BVID recovery cannot prove the original upload binding."""


class UploaderNotDispatched(RuntimeError):
    def __init__(self, cause: OSError):
        self.cause = cause
        super().__init__(type(cause).__name__)


class UploaderOutcomeUnknown(RuntimeError):
    def __init__(self, cause: Exception):
        self.cause = cause
        super().__init__(type(cause).__name__)


def run_uploader(cmd: list[str], *, env: dict, timeout: float) -> subprocess.CompletedProcess:
    """Separate proven exec refusal from errors after the child exists."""
    try:
        child = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    except (FileNotFoundError, PermissionError) as exc:
        if exc.filename == cmd[0]:
            raise UploaderNotDispatched(exc) from exc
        raise UploaderOutcomeUnknown(exc) from exc
    except OSError as exc:
        raise UploaderOutcomeUnknown(exc) from exc
    try:
        stdout, stderr = child.communicate(timeout=timeout)
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        try:
            child.kill()
            child.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        raise UploaderOutcomeUnknown(exc) from exc
    return subprocess.CompletedProcess(cmd, child.returncode, stdout, stderr)


def valid_bvid(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"BV[A-Za-z0-9]{10}", value) is not None


def _complete_response_payload(payload: str) -> bool:
    """An inner closing brace or unterminated string cannot complete a frame."""
    stack = []
    quoted = escaped = False
    for index, character in enumerate(payload):
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "{([":
            stack.append(character)
        elif character in "})]":
            if not stack or stack.pop() != {"}": "{", ")": "(", "]": "["}[character]:
                return False
            if not stack and index != len(payload) - 1:
                return False
    return not quoted and not stack


def classify_uploader_result(stdout: str, stderr: str, rc: int, bvid: str | None) -> dict:
    """A process exit is not evidence that the archive was never created.

    The wrapper's final stdout line proves only whether biliup was spawned.
    A decoded submit ResponseData logged by the submit module can additionally
    prove explicit refusal. Transport errors and unrecognized output stay unknown.
    """
    if valid_bvid(bvid):
        return {"submission_outcome": "POSTED_UNVERIFIED"}
    if bvid:
        return {"submission_outcome": "UNKNOWN"}
    output = re.sub(r"\x1b\[[0-9;]*m", "", stdout + "\n" + stderr)
    lines = stdout.splitlines()
    if lines and lines[-1].startswith("AUTHORIZED_UPLOAD_RESULT="):
        try:
            protocol = json.loads(lines[-1].split("=", 1)[1])
        except (ValueError, TypeError):
            protocol = None
        if (isinstance(protocol, dict)
                and protocol.get("schema_version") == "authorized-upload-dispatch.v1"
                and type(protocol.get("rc")) is int and protocol["rc"] == rc
                and rc != 0 and protocol.get("dispatch_state") == "NOT_DISPATCHED"
                and output.count("AUTHORIZED_UPLOAD_RESULT=") == 1
                and "ResponseData" not in output):
            return {"submission_outcome": "NOT_DISPATCHED"}
    # Upstream submit_by_app logs the decoded ret before testing ret.code.
    # The returned error can print that same ret again. Account for an Error
    # echo only when its entire payload exactly matches a complete module frame.
    # Prefix/ANSI removal never normalizes payload fields or joins partial lines.
    frame = r"(ResponseData[ \t]*\{[ \t]*code:[ \t]*(-?\d+)[ \t]*,[^\r\n]*\})"
    frames = re.findall(
        r"^[ \t]*(?:\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?"
        r"(?:Z|[+-]\d{2}:?\d{2})?[ \t]+)?(?:(?:TRACE|DEBUG|INFO|WARN|ERROR)[ \t]+)?"
        r"biliup::uploader::bilibili:[ \t]*(?:crates/[\w./-]+\.rs:\d+:[ \t]*)?"
        + frame + r"[ \t]*$", output, re.MULTILINE,
    )
    echoes = re.findall(r"^[ \t]*Error:[ \t]*" + frame + r"[ \t]*$", output, re.MULTILINE)
    if (rc != 0 and frames and len(frames) + len(echoes) == output.count("ResponseData")
            and _complete_response_payload(frames[0][0])
            and all(payload == frames[0][0] for payload, _code in frames + echoes)
            and int(frames[0][1]) != 0):
        code = int(frames[0][1])
        return {"submission_outcome": "REJECTED_NO_ARCHIVE", "submit_rejection_code": code,
                "quota_frequency_code": code if code == 21566 else None}
    return {"submission_outcome": "UNKNOWN"}


def finished_submission_outcome(row: dict) -> str:
    """Classify both current and legacy terminal rows without rewriting history."""
    if valid_bvid(row.get("bvid")):
        return "POSTED_UNVERIFIED"
    if row.get("bvid") or row.get("rc") == 0 or row.get("uploader_rc") == 0:
        return "UNKNOWN"
    if row.get("submission_outcome") in {"NOT_DISPATCHED", "REJECTED_NO_ARCHIVE"}:
        return "RETRYABLE"
    if _legacy_pre_spawn_not_dispatched(row):
        return "RETRYABLE"
    # A legacy flag is insufficient. Reclassify only an actual, hash-bound
    # process receipt for this exact attempt and manifest, when one exists.
    if row.get("uploader_output") and row.get("uploader_output_sha256"):
        path = Path(row["uploader_output"])
        try:
            if path.is_symlink() or not path.is_file() or _sha256_file(path) != row["uploader_output_sha256"]:
                return "UNKNOWN"
            receipt = json.loads(path.read_bytes())
            if (not isinstance(receipt, dict)
                    or receipt.get("schema_version") != "authorized-upload-process-result.v1"
                    or receipt.get("attempt_id") != row.get("attempt_id")
                    or receipt.get("manifest_sha256") != row.get("manifest_sha256")
                    or type(receipt.get("rc")) is not int
                    or receipt["rc"] != row.get("uploader_rc")
                    or not isinstance(receipt.get("stdout"), str)
                    or not isinstance(receipt.get("stderr"), str)):
                return "UNKNOWN"
            outcome = classify_uploader_result(receipt["stdout"], receipt["stderr"], receipt["rc"], row.get("bvid"))
            if outcome["submission_outcome"] in {"NOT_DISPATCHED", "REJECTED_NO_ARCHIVE"}:
                return "RETRYABLE"
        except (OSError, ValueError, TypeError):
            return "UNKNOWN"
    return "UNKNOWN"


def _legacy_pre_spawn_not_dispatched(row: dict) -> bool:
    """Replay the saved legacy spawn reconciliation, never trust its flag alone.

    This historical schema has fixed sibling log locators. Its actual traceback
    and manifest must still match the hashes persisted in the original terminal
    row. It grants no compatibility to an unproved legacy process exit.
    """
    if (row.get("failure_stage") != "SUBPROCESS_SPAWN"
            or row.get("error_code") != "UPLOADER_PATH_MISSING"
            or row.get("external_upload_started") is not False
            or row.get("bvid") is not None
            or type(row.get("rc")) is not int or row["rc"] == 0
            or row.get("uploader_rc") != 127):
        return False
    def read(path: Path) -> bytes:
        if path.is_symlink() or not path.is_file():
            raise ValueError("legacy spawn evidence is missing or unsafe")
        return path.read_bytes()
    try:
        manifest_path = Path(row["manifest"])
        manifest_bytes = read(manifest_path)
        if hashlib.sha256(manifest_bytes).hexdigest() != row.get("manifest_sha256"):
            return False
        manifest = json.loads(manifest_bytes)
        expected = manifest_ledger_binding(manifest, manifest_path)
        if any(row.get(key) != value for key, value in expected.items()):
            return False
        proof = json.loads(read(manifest_path.parent / "FAILED-SPAWN-RECONCILIATION.json"))
        if (not isinstance(proof, dict)
                or proof.get("schema_version") != "t06-failed-spawn-reconciliation.v1"
                or proof.get("status") != "PASS"
                or proof.get("external_upload_started") is not False
                or proof.get("failure_stage") != "SUBPROCESS_SPAWN"
                or proof.get("bvid") is not None
                or any(proof.get(key) != row.get(key) for key in
                       ("attempt_id", "manifest_sha256", "video_sha256"))
                or any(proof.get(f"failed_{stream}_sha256") != f"sha256:{row.get(f'failed_{stream}_sha256')}"
                       for stream in ("stdout", "stderr"))
                or proof.get("failed_uploader") != row.get("uploader")
                or any(type(proof.get(key)) is not int or proof[key] != 0
                       for key in ("network_mutations", "publication_calls", "upload_calls"))
                or proof.get("unknown_write_outcomes") != []):
            return False
        for stream in ("stdout", "stderr"):
            body = read(manifest_path.parent / f"UPLOAD-AUDIO.{stream}.txt")
            if hashlib.sha256(body).hexdigest() != row.get(f"failed_{stream}_sha256"):
                return False
            if stream == "stderr":
                stderr = body.decode("utf-8")
        uploader = row.get("uploader")
        if not isinstance(uploader, str) or not uploader:
            return False
        return ("Traceback (most recent call last):" in stderr
                and "in __init__" in stderr and "in _execute_child" in stderr
                and "in communicate" not in stderr
                and stderr.rstrip().endswith(
                    f"FileNotFoundError: [Errno 2] No such file or directory: {uploader!r}"))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


def _sha256_file(path: Path) -> str:
    digest = _evidence_file_sha256(path)
    return digest


def write_json_sidecar(path: Path, payload: dict) -> None:
    """Durably replace an idempotent sidecar without exposing partial JSON."""

    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    body = (
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        try:
            view = memoryview(body)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short write while replacing upload sidecar")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    parent_descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)


def manifest_ledger_binding(manifest: dict, manifest_path: Path) -> dict:
    """Project the immutable manifest fields stored by UPLOAD_ATTEMPT_STARTED."""

    attestation = manifest.get("package_attestation") or {}
    binding = {
        "artifact_id": manifest.get("artifact_id"),
        "video_sha256": (manifest.get("video") or {}).get("sha256"),
        "cover_sha256": (manifest.get("cover") or {}).get("sha256"),
        "manifest": str(manifest_path.resolve()),
        "manifest_sha256": _sha256_file(manifest_path.resolve()),
        "manifest_version": manifest.get("manifest_version"),
        "package_audit_sha256": (attestation.get("package_audit") or {}).get("sha256"),
        "record_sha256": (attestation.get("record") or {}).get("sha256"),
        "subtitle_sha256": (attestation.get("subtitle") or {}).get("sha256"),
        "review_manifest_sha256": (attestation.get("review_manifest") or {}).get("sha256"),
        "title_cover_qc_sha256": (attestation.get("title_cover_qc") or {}).get("sha256"),
        "title": manifest.get("title"),
        "authorized_by": (manifest.get("authorization") or {}).get("by"),
        "authorization_quote": (manifest.get("authorization") or {}).get("quote"),
        "tags": ",".join(manifest.get("tags") or []),
    }
    if "content_identity" in manifest:
        binding["content_identity"] = manifest["content_identity"]
    return binding


def _recoverable_started_row(
    ledger: Path,
    manifest: dict,
    manifest_path: Path,
    *,
    read_ledger: Callable[[Path], tuple[list[dict], list[str]]],
    attempt_id: str | None = None,
) -> dict:
    entries, problems = read_ledger(ledger)
    if problems:
        raise UploadRecoveryError("; ".join(problems))
    started: dict[str, dict] = {}
    finished: set[str] = set()
    terminal: dict[str, dict] = {}
    reconciled: set[str] = set()
    legacy_unknown = 0
    for entry in entries:
        entry_attempt = str(entry.get("attempt_id"))
        if entry.get("event") == "UPLOAD_ATTEMPT_STARTED":
            started[entry_attempt] = entry
        elif entry.get("event") == "UPLOAD_ATTEMPT_FINISHED":
            finished.add(entry_attempt)
            terminal[entry_attempt] = entry
        elif entry.get("event") in {"UPLOAD_ATTEMPT_BVID_BOUND", "UPLOAD_PUBLICATION_VERIFIED"}:
            reconciled.add(entry_attempt)
        elif entry.get("event") is None and finished_submission_outcome(entry) == "UNKNOWN":
            legacy_unknown += 1
    unresolved = [row for key, row in started.items()
                  if (attempt_id is None or key == attempt_id)
                  and (key not in finished or (key not in reconciled
                      and finished_submission_outcome(terminal[key]) == "UNKNOWN"))]
    if len(unresolved) != 1 or (attempt_id is None and legacy_unknown):
        raise UploadRecoveryError(
            f"explicit recovery requires exactly one unresolved upload intent, found {len(unresolved) + (legacy_unknown if attempt_id is None else 0)}"
        )
    row = unresolved[0]
    expected = manifest_ledger_binding(manifest, manifest_path)
    changed = [key for key, value in expected.items() if row.get(key) != value]
    if changed:
        raise UploadRecoveryError(
            f"unresolved upload intent does not match this manifest: {changed}"
        )
    return row


def _selected_attempt(
    entries: list[dict], attempt_id: str, manifest: dict, manifest_path: Path, bvid: str,
) -> tuple[str, dict]:
    """Project one attempt only after the caller validates the entire ledger."""
    rows = [entry for entry in entries if entry.get("attempt_id") == attempt_id]
    origins = [entry for entry in rows if entry.get("event") == "UPLOAD_ATTEMPT_STARTED"]
    if len(origins) != 1:
        raise UploadRecoveryError("selected attempt has no unique STARTED row")
    expected = manifest_ledger_binding(manifest, manifest_path)
    changed = [key for key, value in expected.items() if origins[0].get(key) != value]
    if changed:
        raise UploadRecoveryError(f"selected attempt does not match this manifest: {changed}")
    if not valid_bvid(bvid):
        raise UploadRecoveryError("explicit recovery BVID has an invalid shape")
    terminal = next((entry for entry in rows
                     if entry.get("event") == "UPLOAD_ATTEMPT_FINISHED"), None)
    if terminal is not None and finished_submission_outcome(terminal) == "RETRYABLE":
        raise UploadRecoveryError("selected attempt was proven not to create an archive")
    bound = [entry for entry in rows if entry.get("event") in
             {"UPLOAD_ATTEMPT_BVID_BOUND", "UPLOAD_PUBLICATION_VERIFIED"}]
    identities = [entry for entry in ([terminal] if terminal else []) + bound if valid_bvid(entry.get("bvid"))]
    if any(entry["bvid"] != bvid for entry in identities):
        raise UploadRecoveryError("explicit BVID does not match the selected attempt")
    verified = next((entry for entry in bound if entry["event"] == "UPLOAD_PUBLICATION_VERIFIED"), None)
    if verified:
        return "uploaded", verified
    if bound:
        return "posted_unverified", bound[0]
    if terminal and finished_submission_outcome(terminal) == "POSTED_UNVERIFIED" and terminal.get("bvid"):
        return "posted_unverified", terminal
    return "unresolved", origins[0]


def _started_row_sha256(row: dict) -> str:
    body = json.dumps(
        row,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


_LEDGER_STABLE_KEYS = (
    "attempt_id",
    "artifact_id",
    "video_sha256",
    "cover_sha256",
    "manifest",
    "manifest_sha256",
    "manifest_version",
    "uploader",
    "package_audit_sha256",
    "record_sha256",
    "subtitle_sha256",
    "review_manifest_sha256",
    "title_cover_qc_sha256",
    "content_identity",
    "title",
    "authorized_by",
    "authorization_quote",
    "tags",
)


def append_publication_verified(
    ledger: Path,
    guard_row: dict,
    *,
    bvid: str,
    result: dict | None,
    manifest_path: Path,
    append_ledger: Callable[[Path, dict], None],
    now: Callable[[], str],
    public_verify_sidecar_path: Callable[[Path], Path],
) -> None:
    public_view = (result or {}).get("public_view") or {}
    stable = {
        key: guard_row.get(key)
        for key in _LEDGER_STABLE_KEYS
        if guard_row.get(key) is not None
    }
    append_ledger(
        ledger,
        {
            "event": "UPLOAD_PUBLICATION_VERIFIED",
            "at": now(),
            **stable,
            "rc": 0,
            "bvid": bvid,
            "aid": public_view.get("aid"),
            "cid": public_view.get("cid"),
            "public_verify_status": (result or {}).get("status"),
            "public_verify_sha256": _sha256_file(
                Path((result or {}).get("public_verify_path") or public_verify_sidecar_path(manifest_path))
            ),
        },
    )


def _verify_recovery_bvid_binding(
    manifest: dict,
    bvid: str,
    *,
    http: Callable[..., dict],
    view_api: str,
    tags_api: str,
    member_archive_view_api: str,
    expected_tid: int,
    expected_copyright: int,
    expected_source: str,
    normalise_tags: Callable[[object], list[str]],
) -> dict:
    from .publish_policy import checked_policy
    policy = checked_policy(manifest.get("publish_policy") or {
        "tid": expected_tid, "copyright": expected_copyright, "source": expected_source,
    })
    expected_tid, expected_copyright, expected_source = (policy[k] for k in ("tid", "copyright", "source"))
    problems: list[str] = []
    if not valid_bvid(bvid):
        raise UploadRecoveryError("explicit recovery BVID has an invalid shape")
    public = http(view_api.format(bvid=bvid))
    public_data = public.get("data") or {}
    tags_payload = http(tags_api.format(bvid=bvid))
    public_tags = [
        str(row.get("tag_name") or "").strip()
        for row in (tags_payload.get("data") or [])
        if isinstance(row, dict) and str(row.get("tag_name") or "").strip()
    ]
    member = http(member_archive_view_api.format(bvid=bvid))
    archive = (member.get("data") or {}).get("archive") or {}
    aid, cid = public_data.get("aid"), public_data.get("cid")
    if public.get("code") != 0 or public_data.get("state") != 0:
        problems.append("public archive is not available")
    if not aid or not cid:
        problems.append("public archive has no stable aid/cid")
    for field, expected in (
        ("title", manifest.get("title")),
        ("desc", manifest.get("description")),
        ("tid", expected_tid),
        ("copyright", expected_copyright),
    ):
        if public_data.get(field) != expected:
            problems.append(f"public {field} mismatch")
    expected_tags = list(manifest.get("tags") or [])
    if public_tags != expected_tags and set(public_tags) != set(expected_tags):
        problems.append("public tags mismatch")
    if member.get("code") != 0 or not isinstance(archive, dict):
        problems.append("Creator archive is unavailable")
    if archive.get("bvid") != bvid:
        problems.append("Creator archive BVID mismatch")
    if archive.get("aid") != aid:
        problems.append("Creator/public aid mismatch")
    for field, expected in (
        ("title", manifest.get("title")),
        ("desc", manifest.get("description")),
        ("tid", expected_tid),
        ("copyright", expected_copyright),
        ("source", expected_source),
    ):
        if archive.get(field) != expected:
            problems.append(f"Creator archive {field} mismatch")
    member_tags = normalise_tags(archive.get("tag"))
    if member_tags != expected_tags and set(member_tags) != set(expected_tags):
        problems.append("Creator archive tags mismatch")
    if problems:
        raise UploadRecoveryError("; ".join(problems))
    return {"bvid": bvid, "aid": aid, "cid": cid}


def season_add(
    args: Any,
    *,
    default_upload_lock: Path,
    exclusive_upload_lock: Callable[..., Any],
    load_and_verify: Callable[..., tuple[dict | None, list[str]]],
    ledger_guard: Callable[..., tuple[str | None, dict | None, list[str]]],
    read_ledger: Callable[[Path], tuple[list[dict], list[str]]],
    append_ledger: Callable[[Path, dict], None],
    build_season_http: Callable[[Path], tuple[Callable[..., dict], str]],
    run_postpublish_verification: Callable[..., tuple[int, dict | None]],
    run_season_step: Callable[..., int],
    public_verify_sidecar_path: Callable[[Path], Path],
    normalise_tags: Callable[[object], list[str]],
    now: Callable[[], str],
    view_api: str,
    tags_api: str,
    member_archive_view_api: str,
    expected_tid: int,
    expected_copyright: int,
    expected_source: str,
) -> int:
    """Finish one posted manifest, including explicit-BVID crash recovery."""

    manifest_path = Path(args.manifest)
    ledger = Path(args.ledger)
    selected_attempt = getattr(args, "attempt_id", None)
    if selected_attempt and not args.bvid:
        print("REFUSE: --attempt-id requires explicit --bvid", file=sys.stderr)
        return 2
    with exclusive_upload_lock(default_upload_lock):
        manifest, problems = load_and_verify(manifest_path)
        if (manifest is not None and manifest.get("manifest_version") == 3
                and manifest.get("recovery_publication_authority") is None
                and any("PUBLISH_POLICY_LANE_DRIFT" in p for p in problems)):
            manifest, problems = load_and_verify(manifest_path, posted_policy_ledger=ledger)
        if problems:
            for problem in problems:
                print(f"WARN (season-add continues): {problem}", file=sys.stderr)
            if manifest is None or manifest.get("manifest_version") == 3:
                return 2
        status, row, ledger_problems = ledger_guard(
            ledger, manifest["video"]["sha256"]
        )
        if ledger_problems:
            for problem in ledger_problems:
                print(f"REFUSE: {problem}", file=sys.stderr)
            return 5

        bvid = str(args.bvid) if args.bvid else None
        if selected_attempt:
            if manifest.get("manifest_version") != 3:
                print("REFUSE: attempt selection requires manifest v3", file=sys.stderr)
                return 5
            try:
                status, row = _selected_attempt(read_ledger(ledger)[0], selected_attempt,
                                                manifest, manifest_path, bvid)
            except UploadRecoveryError as exc:
                print(f"REFUSE: {exc}", file=sys.stderr)
                return 5
        guard_row: dict | None = None
        if bvid and status == "unresolved":
            if manifest.get("manifest_version") != 3:
                print("REFUSE: explicit unresolved recovery requires manifest v3", file=sys.stderr)
                return 5
            try:
                started_row = _recoverable_started_row(
                    ledger,
                    manifest,
                    manifest_path,
                    read_ledger=read_ledger,
                    attempt_id=selected_attempt,
                )
                http, _csrf = build_season_http(Path(args.cookie_json))
                binding = _verify_recovery_bvid_binding(
                    manifest,
                    bvid,
                    http=http,
                    view_api=view_api,
                    tags_api=tags_api,
                    member_archive_view_api=member_archive_view_api,
                    expected_tid=expected_tid,
                    expected_copyright=expected_copyright,
                    expected_source=expected_source,
                    normalise_tags=normalise_tags,
                )
            except UploadRecoveryError as exc:
                print(f"REFUSE: {exc}", file=sys.stderr)
                return 5
            except Exception as exc:
                print(
                    f"REFUSE: recovery readback failed: {type(exc).__name__}",
                    file=sys.stderr,
                )
                return 5
            previous_terminal = next((entry for entry in read_ledger(ledger)[0]
                if entry.get("event") == "UPLOAD_ATTEMPT_FINISHED"
                and entry.get("attempt_id") == started_row["attempt_id"]), None)
            guard_row = {
                "event": "UPLOAD_ATTEMPT_BVID_BOUND" if previous_terminal else "UPLOAD_ATTEMPT_FINISHED",
                "at": now(),
                **{
                    key: value
                    for key, value in started_row.items()
                    if key not in {"event", "at"}
                },
                "uploader_rc": previous_terminal.get("uploader_rc") if previous_terminal else 0,
                "rc": 6,
                "bvid": bvid,
                "aid": binding["aid"],
                "cid": binding["cid"],
                "public_verify_status": "RECOVERY_BOUND_READONLY",
                "recovery_source": "season-add-explicit-bvid",
                "recovery_started_row_sha256": _started_row_sha256(started_row),
            }
            append_ledger(ledger, guard_row)
        elif bvid:
            if status not in {"uploaded", "posted_unverified"} or not row:
                if manifest.get("manifest_version") == 3:
                    print(
                        "REFUSE: manifest v3 explicit BVID has no matching upload ledger row",
                        file=sys.stderr,
                    )
                    return 5
            elif str(row.get("bvid") or "") != bvid:
                print("REFUSE: explicit BVID does not match the upload ledger", file=sys.stderr)
                return 5
            else:
                guard_row = row
        else:
            if status not in {"uploaded", "posted_unverified"} or not row or not row.get("bvid"):
                print(
                    "REFUSE: ledger has no successful upload with a bvid for this manifest's video; "
                    "pass --bvid explicitly if the post exists",
                    file=sys.stderr,
                )
                return 5
            bvid = str(row["bvid"])
            guard_row = row

        if manifest.get("manifest_version") == 3 and guard_row:
            expected = manifest_ledger_binding(manifest, manifest_path)
            changed = [key for key, value in expected.items() if guard_row.get(key) != value]
            if changed:
                print(f"REFUSE: recovery ledger does not match this manifest: {changed}", file=sys.stderr)
                return 5

        if manifest.get("manifest_version") == 3:
            rc, result = run_postpublish_verification(
                manifest, manifest_path, bvid, args,
                resume_local_only=bool(guard_row and guard_row.get("event") != "UPLOAD_PUBLICATION_VERIFIED"
                                       and guard_row.get("event") is not None),
            )
            if rc == 0 and guard_row and guard_row.get("event") in {"UPLOAD_ATTEMPT_FINISHED", "UPLOAD_ATTEMPT_BVID_BOUND"}:
                append_publication_verified(
                    ledger,
                    guard_row,
                    bvid=bvid,
                    result=result,
                    manifest_path=manifest_path,
                    append_ledger=append_ledger,
                    now=now,
                    public_verify_sidecar_path=public_verify_sidecar_path,
                )
            return rc
        return run_season_step(manifest, manifest_path, bvid, args)

def assert_posted_policy_ledger(
    manifest: dict, manifest_path: Path, ledger: Path, *,
    read_ledger: Callable[[Path], tuple[list[dict], list[str]]],
) -> None:
    """Permit old policy only for byte-identical already-dispatched/unknown intents.

    This grants no upload capability. The season recovery then resolves the exact
    BVID and replays all existing per-attempt gates before any collection write.
    """
    entries, problems = read_ledger(ledger)
    if problems:
        raise UploadRecoveryError("; ".join(problems))
    expected = manifest_ledger_binding(manifest, manifest_path)
    for row in entries:
        if row.get("event") != "UPLOAD_ATTEMPT_STARTED" or not row.get("attempt_id"):
            continue
        if any(row.get(key) != value for key, value in expected.items()):
            continue
        terminal = [entry for entry in entries if entry.get("event") == "UPLOAD_ATTEMPT_FINISHED"
                    and entry.get("attempt_id") == row["attempt_id"]]
        if not terminal or all(finished_submission_outcome(entry) != "RETRYABLE" for entry in terminal):
            return
    raise UploadRecoveryError("historical policy has no exact posted/unknown ledger intent")
