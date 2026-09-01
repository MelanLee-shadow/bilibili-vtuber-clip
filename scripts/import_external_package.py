"""Import one externally produced slice package into free and reach review_ready.

wsl(workstation)/Mac 与 free 用同一份 repo、同一套 produce，产出的包是完整的；卡点只
在**跨主机导入**：上传凭据、``publication_registry``、upload 读的 state 都只在
free。此前这条链是六步手工活（路径规整 → state 绑定 → manifest → audit → QC →
make-manifest），于是 wsl 产的每条片都要人伺候。本脚本把前五步做成一条命令。

用法（在 free 的部署仓库根目录下跑）：

    python3 scripts/import_external_package.py \\
        --source /opt/bilive/autoslice/staging/2026-08-07/auto_220747_488_680/replacement_recuts \\
        --date --candidate auto_220747_488_680 --apply

已在 committed publication registry 逐字放行的 exact failed pick 只能加
``--adopt-failed-pick <同一 cid> --release-quote '<维护者 逐字原话>'``。它不与
``--allow-new-pick`` 共用，也不会替代 audit/QC/authorized-upload。

不带 ``--apply`` 是 dry-run：解析根、验证定位符契约、列出要搬的字节、并在**持
runner.lock 只读**的前提下预判 state 能不能绑——一个字节都不写。

做了什么 / 没做什么：

- ①路径规整、②state 绑定、③manifest、④audit、⑤联合质检 —— 做；
- ⑥``authorized_upload make-manifest`` —— **不做**（那是上传授权面，需要 维护者 的
  逐字引语）。回执里给出该跑的命令，人来接。

fail-closed：任何一步 REFUSE 就停，回执 ``steps[]`` 写明卡在哪、原因码、怎么修。
退出码 0=全绿，2=被拒（回执已落盘），3=用法/环境错误。

state 写入全程持 ``runner.lock``：runner 的 tick 把 state 读进内存、跑完（可长达
90 分钟）才写回，任何不持锁的带外手术都会被陈旧写回整体抹掉。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice import package_import as pi  # noqa: E402
from src.autoslice.publication_reconciliation import (  # noqa: E402
    project_publication_closure,
)
from src.autoslice.runner_state_writeback import (  # noqa: E402
    RunnerStateWritebackError,
    read_exact_state_preimage,
    write_exact_state_bytes_under_lease,
    write_state,
)
from src.autoslice.qixi_transaction_core import exclusive_runner_commit  # noqa: E402


DEFAULT_BASE = Path("/opt/bilive/autoslice")
PACKAGE_AUDIT_NAME = "package_audit.json"
PUBLICATION_REGISTRY_RELATIVE = Path(
    "assets/lidousha/publication_registry.v1.json"
)
STEP_ORDER = (
    "PREFLIGHT",
    "COPY",
    "RELOCATE",
    "STATE_BIND",
    "MANIFEST",
    "AUDIT",
    "TITLE_COVER_QC",
    "MAKE_MANIFEST",
)


@dataclass
class GateResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class StateBindRollback:
    """Exact byte pre/post-images needed to undo a gate-failed state bind."""

    state_path: Path
    runner_lock: Path
    backup_path: Path
    preimage_sha256: str
    postimage_sha256: str


class GateRunner:
    """Subprocess seam for the existing gate scripts (stubbed in tests)."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root

    def run(self, argv: Sequence[str]) -> GateResult:
        completed = subprocess.run(
            [str(value) for value in argv],
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
        )
        return GateResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


@dataclass
class Receipt:
    candidate_id: str
    date: str
    apply: bool
    started_at: str
    steps: list[dict[str, Any]] = field(default_factory=list)

    def add(self, step: str, status: str, **detail: Any) -> dict[str, Any]:
        entry = {"step": step, "status": status, **detail}
        self.steps.append(entry)
        return entry

    def as_dict(self, *, status: str, finished_at: str) -> dict[str, Any]:
        done = {row["step"] for row in self.steps}
        return {
            "schema_version": pi.IMPORT_RECEIPT_SCHEMA_VERSION,
            "generated_by": "import_external_package.v1",
            "candidate_id": self.candidate_id,
            "date": self.date,
            "run_mode": "APPLY" if self.apply else "DRY_RUN",
            "status": status,
            "started_at": self.started_at,
            "finished_at": finished_at,
            "steps": list(self.steps),
            "steps_not_reached": [
                step for step in STEP_ORDER if step not in done
            ],
            # 这个工具永远到不了上传：它最多把候选送到 review_ready。
            "upload_allowed": False,
        }


class Refusal(Exception):
    def __init__(self, step: str, error: pi.PackageImportError) -> None:
        super().__init__(str(error))
        self.step = step
        self.error = error


def _refuse(step: str, code: str, detail: str, hint: str = "") -> Refusal:
    return Refusal(step, pi.PackageImportError(code, detail, hint=hint))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_state(state_path: Path) -> dict[str, Any]:
    document, _ = pi.load_json_document(state_path, label="runner state")
    return document


def _backup_state(state_path: Path, stamp: str) -> Path:
    payload = state_path.read_bytes()
    digest = pi.sha256_bytes(payload)
    backup = state_path.with_name(
        f"{state_path.name}.pre-import-{stamp}-{digest[:12]}"
    )
    if backup.exists():
        existing = pi.require_regular_file(backup, label="state preimage backup")
        if existing.read_bytes() != payload:
            raise pi.PackageImportError(
                "STATE_BACKUP_COLLISION",
                f"state backup path already contains different bytes: {backup}",
            )
        return backup
    pi.atomic_write_bytes(backup, payload)
    return backup


def run_import(
    *,
    source: Path,
    date: str,
    candidate_id: str,
    base: Path,
    repo_root: Path,
    state_path: Path,
    runner_lock: Path,
    deployed_commit_file: Path,
    apply: bool,
    allow_new_pick: bool,
    skip_qc: bool,
    source_repo_root: str | None,
    source_workspace_root: str | None,
    gate_runner: GateRunner,
    closure_projector: Callable[[Any], Any] = project_publication_closure,
    adopt_failed_pick: str | None = None,
    release_quote: str | None = None,
) -> tuple[dict[str, Any], int]:
    started_at = pi.now_utc()
    receipt = Receipt(
        candidate_id=candidate_id, date=date, apply=apply, started_at=started_at
    )
    destination_package_root = (
        base / "out" / date / candidate_id / "replacement_recuts"
    )
    try:
        _validate_import_mode(
            candidate_id=candidate_id,
            allow_new_pick=allow_new_pick,
            adopt_failed_pick=adopt_failed_pick,
            release_quote=release_quote,
        )
        plan = _step_preflight(
            receipt=receipt,
            source=source,
            candidate_id=candidate_id,
            destination_package_root=destination_package_root,
            repo_root=repo_root,
            source_repo_root=source_repo_root,
            source_workspace_root=source_workspace_root,
        )
        failed_pick_authorization = _step_state_precheck(
            receipt=receipt,
            state_path=state_path,
            runner_lock=runner_lock,
            candidate_id=candidate_id,
            date=date,
            allow_new_pick=allow_new_pick,
            closure_projector=closure_projector,
            registry_path=repo_root / PUBLICATION_REGISTRY_RELATIVE,
            adopt_failed_pick=adopt_failed_pick,
            release_quote=release_quote,
        )
        _step_copy(
            receipt=receipt,
            plan=plan,
            apply=apply,
            destination_package_root=destination_package_root,
            candidate_id=candidate_id,
        )
        _step_relocate(
            receipt=receipt,
            plan=plan,
            destination_package_root=destination_package_root,
            candidate_id=candidate_id,
            apply=apply,
        )
        if not apply:
            for step in ("STATE_BIND", "MANIFEST", "AUDIT", "TITLE_COVER_QC"):
                receipt.add(
                    step,
                    "SKIPPED_DRY_RUN",
                    detail="dry run stops before any destination write",
                )
            _step_make_manifest_hint(
                receipt=receipt,
                destination_package_root=destination_package_root,
                title=None,
            )
            return (
                receipt.as_dict(status="DRY_RUN_OK", finished_at=pi.now_utc()),
                0,
            )

        bound, rollback = _step_state_bind(
            receipt=receipt,
            destination_package_root=destination_package_root,
            candidate_id=candidate_id,
            date=date,
            state_path=state_path,
            runner_lock=runner_lock,
            allow_new_pick=allow_new_pick,
            closure_projector=closure_projector,
            registry_path=repo_root / PUBLICATION_REGISTRY_RELATIVE,
            release_quote=release_quote,
            failed_pick_authorization=failed_pick_authorization,
        )
        try:
            title = _step_manifest(
                receipt=receipt,
                gate_runner=gate_runner,
                destination_package_root=destination_package_root,
                state_path=state_path,
                deployed_commit_file=deployed_commit_file,
                candidate_id=candidate_id,
            )
            package_audit_path = _step_audit(
                receipt=receipt,
                gate_runner=gate_runner,
                destination_package_root=destination_package_root,
            )
            _step_title_cover_qc(
                receipt=receipt,
                gate_runner=gate_runner,
                destination_package_root=destination_package_root,
                candidate_id=candidate_id,
                title=title,
                same_stem_cover=bound.same_stem_cover_path,
                skip_qc=skip_qc,
            )
            _step_make_manifest_hint(
                receipt=receipt,
                destination_package_root=destination_package_root,
                package_audit_path=package_audit_path,
                title=title,
            )
        except BaseException as gate_error:
            try:
                _rollback_state_bind(receipt=receipt, token=rollback)
            except pi.PackageImportError as rollback_error:
                raise Refusal("STATE_BIND", rollback_error) from gate_error
            raise
        status = (
            "REVIEW_READY"
            if not skip_qc
            else "REVIEW_READY_WITHOUT_JOINT_QC"
        )
        return receipt.as_dict(status=status, finished_at=pi.now_utc()), 0
    except Refusal as refusal:
        receipt.add(
            refusal.step,
            "REFUSED",
            **refusal.error.as_dict(),
        )
        return receipt.as_dict(status="REFUSED", finished_at=pi.now_utc()), 2
    except pi.PackageImportError as error:
        receipt.add("UNCLASSIFIED", "REFUSED", **error.as_dict())
        return receipt.as_dict(status="REFUSED", finished_at=pi.now_utc()), 2
    except Exception as error:  # noqa: BLE001 - a receipt must always exist
        receipt.add(
            "UNCLASSIFIED",
            "REFUSED",
            code="UNEXPECTED_ERROR",
            detail=f"{type(error).__name__}: {error}",
            hint="this is a bug in the importer, not an operator error; the "
            "chain stopped without reaching upload",
        )
        return receipt.as_dict(status="REFUSED", finished_at=pi.now_utc()), 2


def _validate_import_mode(
    *,
    candidate_id: str,
    allow_new_pick: bool,
    adopt_failed_pick: str | None,
    release_quote: str | None,
) -> None:
    has_adoption = adopt_failed_pick is not None
    has_quote = release_quote is not None and release_quote != ""
    if has_adoption != has_quote:
        raise _refuse(
            "PREFLIGHT",
            "FAILED_PICK_IMPORT_FLAGS_INCOMPLETE",
            "--adopt-failed-pick and --release-quote must be supplied together",
        )
    if not has_adoption:
        return
    if adopt_failed_pick != candidate_id:
        raise _refuse(
            "PREFLIGHT",
            "FAILED_PICK_IMPORT_CANDIDATE_MISMATCH",
            f"--adopt-failed-pick={adopt_failed_pick!r} does not equal "
            f"--candidate={candidate_id!r}",
        )
    if allow_new_pick:
        raise _refuse(
            "PREFLIGHT",
            "FAILED_PICK_IMPORT_MODE_CONFLICT",
            "--adopt-failed-pick cannot be combined with --allow-new-pick",
        )


def _step_preflight(
    *,
    receipt: Receipt,
    source: Path,
    candidate_id: str,
    destination_package_root: Path,
    repo_root: Path,
    source_repo_root: str | None,
    source_workspace_root: str | None,
) -> pi.ImportPlan:
    try:
        plan = pi.plan_import(
            source_package_dir=source,
            destination_package_root=destination_package_root,
            destination_repo_root=repo_root,
            candidate_id=candidate_id,
            source_repo_root=source_repo_root,
            source_workspace_root=source_workspace_root,
        )
        verified = pi.verify_declared_artifacts(source, plan.documents)
    except pi.PackageImportError as error:
        raise Refusal("PREFLIGHT", error) from error
    receipt.add(
        "PREFLIGHT",
        "PASS",
        roots=plan.roots.as_dict(),
        uniform_host_fallback=plan.documents.uniform_fallback,
        upload_stem=plan.documents.upload_stem,
        planned_file_count=len(plan.copies),
        repo_assets=list(plan.repo_assets),
        declared_artifact_sha256=verified,
    )
    return plan


def _step_state_precheck(
    *,
    receipt: Receipt,
    state_path: Path,
    runner_lock: Path,
    candidate_id: str,
    date: str,
    allow_new_pick: bool,
    closure_projector,
    registry_path: Path,
    adopt_failed_pick: str | None,
    release_quote: str | None,
) -> pi.FailedPickImportAuthorization | None:
    """Answer "would the bind be accepted?" before moving any byte.

    Read-only, but still under ``runner.lock``: a tick that is mid-flight owns
    the state document, and a precheck against its stale bytes would be a lie.
    """

    try:
        runtime_root = state_path.parents[1]
        if runner_lock != runtime_root / "runner.lock":
            raise pi.PackageImportError(
                "RUNNER_LOCK_PATH_DRIFT",
                "state import must use the canonical runtime runner.lock",
            )
        with exclusive_runner_commit(runtime_root):
            state = _read_state(state_path)
            authorization = None
            if adopt_failed_pick is not None:
                assert release_quote is not None
                authorization = pi.load_failed_pick_import_authorization(
                    registry_path=registry_path,
                    candidate_id=adopt_failed_pick,
                    date=date,
                    release_quote=release_quote,
                )
            preconditions = pi.check_state_preconditions(
                state,
                candidate_id=candidate_id,
                date=date,
                allow_new_pick=allow_new_pick,
                project_closure=closure_projector,
                failed_pick_authorization=authorization,
            )
    except pi.PackageImportError as error:
        raise Refusal("PREFLIGHT", error) from error
    receipt.add(
        "PREFLIGHT",
        "STATE_PRECHECK_PASS",
        state_path=str(state_path),
        batch_status=preconditions.batch_status,
        pick_row_present=preconditions.pick_index is not None,
        will_create_pick_row=preconditions.will_create_pick_row,
        will_adopt_failed_pick=preconditions.will_adopt_failed_pick,
        failed_pick_import_authority=(
            authorization.receipt_binding()
            if authorization is not None
            else None
        ),
    )
    return authorization


def _step_copy(
    *,
    receipt: Receipt,
    plan: pi.ImportPlan,
    apply: bool,
    destination_package_root: Path,
    candidate_id: str,
) -> None:
    try:
        protected = pi.relocated_document_guard(
            destination_package_root, candidate_id
        )
        summary = pi.execute_copy(plan, apply=apply, protected=protected)
    except pi.PackageImportError as error:
        raise Refusal("COPY", error) from error
    if not apply and summary["divergent_destination_count"]:
        raise _refuse(
            "COPY",
            "DESTINATION_DIVERGENT",
            f"{summary['divergent_destination_count']} destination file(s) "
            "differ from the source and would be overwritten",
            hint="archive the existing destination package "
            "(mv <pkg> <pkg>.pre-import-<stamp>) before importing, or confirm "
            "this is the same package and let --apply overwrite it",
        )
    receipt.add(
        "COPY",
        "PASS" if apply else "PLANNED",
        file_count=summary["file_count"],
        total_bytes=summary["total_bytes"],
        # 逐文件 sha256 前后比对是"字节没在传输中损坏"的唯一证明。
        files=summary["files"],
    )


def _step_relocate(
    *,
    receipt: Receipt,
    plan: pi.ImportPlan,
    destination_package_root: Path,
    candidate_id: str,
    apply: bool,
) -> None:
    if not apply:
        receipt.add(
            "RELOCATE",
            "SKIPPED_DRY_RUN",
            detail="relocation rewrites destination documents; nothing is "
            "projected during a dry run",
            would_project_roots=plan.roots.as_dict(),
        )
        return
    try:
        result = pi.relocate_package(
            package_root=destination_package_root,
            candidate_id=candidate_id,
            roots=plan.roots,
            apply=True,
        )
        documents = pi.read_package_documents(
            destination_package_root, candidate_id
        )
        cover = pi.materialize_same_stem_cover(
            package_root=destination_package_root,
            documents=documents,
            apply=True,
        )
    except pi.PackageImportError as error:
        raise Refusal("RELOCATE", error) from error
    receipt.add("RELOCATE", result.pop("status"), same_stem_cover=cover, **result)


def _step_state_bind(
    *,
    receipt: Receipt,
    destination_package_root: Path,
    candidate_id: str,
    date: str,
    state_path: Path,
    runner_lock: Path,
    allow_new_pick: bool,
    closure_projector,
    registry_path: Path,
    release_quote: str | None,
    failed_pick_authorization: pi.FailedPickImportAuthorization | None,
) -> tuple[pi.BoundPackage, StateBindRollback]:
    bound_at = pi.now_utc()
    stamp = bound_at.replace(":", "").replace("-", "")
    try:
        package = pi.read_bound_package(
            package_root=destination_package_root, candidate_id=candidate_id
        )
        # 持锁贯穿读—改—写：runner 的 tick 可能已经把 state 读进内存并要跑 90
        # 分钟才写回，不持锁的带外手术会被那次陈旧写回整体抹掉。
        runtime_root = state_path.parents[1]
        if runner_lock != runtime_root / "runner.lock":
            raise pi.PackageImportError(
                "RUNNER_LOCK_PATH_DRIFT",
                "state import must use the canonical runtime runner.lock",
            )
        with exclusive_runner_commit(runtime_root) as lease:
            before_state = _read_state(state_path)
            current_authorization = None
            if failed_pick_authorization is not None:
                assert release_quote is not None
                current_authorization = (
                    pi.load_failed_pick_import_authorization(
                        registry_path=registry_path,
                        candidate_id=candidate_id,
                        date=date,
                        release_quote=release_quote,
                    )
                )
                if current_authorization != failed_pick_authorization:
                    raise pi.PackageImportError(
                        "FAILED_PICK_REGISTRY_DRIFT_AFTER_PREFLIGHT",
                        "committed failed-pick release authority changed after "
                        "preflight; re-run from a fresh dry run",
                    )
            after_state, delta = pi.build_bound_state(
                before_state,
                package=package,
                date=date,
                bound_at=bound_at,
                allow_new_pick=allow_new_pick,
                project_closure=closure_projector,
                failed_pick_authorization=current_authorization,
            )
            state_preimage_sha256 = pi.sha256_file(state_path)
            backup = _backup_state(state_path, stamp)
            if pi.sha256_file(backup) != state_preimage_sha256:
                raise pi.PackageImportError(
                    "STATE_BACKUP_VERIFY_FAILED",
                    "state preimage backup does not match the locked source bytes",
                )
            try:
                if after_state != before_state:
                    write_state(
                        state_path,
                        dict(after_state),
                        runtime_root=runtime_root,
                        updated_at=bound_at,
                        log=lambda message: print(message, file=sys.stderr),
                        lease=lease,
                    )
                verified = _read_state(state_path)
                state_postimage_sha256 = pi.sha256_file(state_path)
                if verified != after_state:
                    raise pi.PackageImportError(
                        "STATE_READBACK_FAILED",
                        "the persisted state is not the exact computed postimage",
                    )
                row = next(
                    (
                        item
                        for item in verified.get("picks") or []
                        if isinstance(item, dict)
                        and item.get("candidate_id") == candidate_id
                    ),
                    None,
                )
                if row is None or row.get("status") != "review_ready" or row.get("rc") != 0:
                    raise pi.PackageImportError(
                        "STATE_READBACK_FAILED",
                        "the written state lacks the exact review_ready/rc=0 row",
                    )
                if delta["adopted_failed_pick"] and (
                    verified.get("status") != "ready_unpublished_with_failures"
                    or not isinstance(verified.get("publication_closure"), dict)
                    or verified["publication_closure"].get("status")
                    != "ready_unpublished_with_failures"
                    or not isinstance(
                        (row.get("external_package_import") or {}).get(
                            "failed_pick_adoption"
                        ),
                        dict,
                    )
                    or row["external_package_import"]["failed_pick_adoption"].get(
                        "status"
                    )
                    != "CONSUMED"
                ):
                    raise pi.PackageImportError(
                        "FAILED_PICK_ADOPTION_READBACK_FAILED",
                        "state readback lacks the exact batch/closure/consumption postimage",
                    )
            except BaseException as bind_error:
                try:
                    _restore_state_preimage_locked(
                        state_path=state_path,
                        backup_path=backup,
                        preimage_sha256=state_preimage_sha256,
                        runtime_root=runtime_root,
                        lease=lease,
                    )
                except pi.PackageImportError as rollback_error:
                    raise pi.PackageImportError(
                        "STATE_BIND_ROLLBACK_FAILED",
                        f"state bind failed and exact preimage restore also failed: {rollback_error}",
                    ) from bind_error
                if isinstance(bind_error, (OSError, RunnerStateWritebackError)):
                    raise pi.PackageImportError(
                        "STATE_WRITE_FAILED",
                        f"{type(bind_error).__name__}: {bind_error}",
                    ) from bind_error
                raise
    except pi.PackageImportError as error:
        raise Refusal("STATE_BIND", error) from error
    receipt.add(
        "STATE_BIND",
        "PASS",
        state_path=str(state_path),
        state_backup_path=str(backup),
        state_preimage_sha256="sha256:" + state_preimage_sha256,
        state_postimage_sha256="sha256:" + state_postimage_sha256,
        created_pick_row=delta["created_pick_row"],
        adopted_failed_pick=delta["adopted_failed_pick"],
        failed_pick_import_authority=delta[
            "failed_pick_import_authority"
        ],
        removed_superseded_keys=delta["removed_superseded_keys"],
        closure_before=delta["closure_before"],
        closure_after=delta["closure_after"],
        cover_path=row.get("cover_path"),
        cover_sha256=row.get("cover_sha256"),
        video_sha256=row.get("video_sha256"),
        title=row.get("title"),
    )
    return package, StateBindRollback(
        state_path=state_path,
        runner_lock=runner_lock,
        backup_path=backup,
        preimage_sha256=state_preimage_sha256,
        postimage_sha256=state_postimage_sha256,
    )


def _rollback_state_bind(*, receipt: Receipt, token: StateBindRollback) -> None:
    """Restore exact state bytes when any post-bind review gate refuses."""

    runtime_root = token.state_path.parents[1]
    if token.runner_lock != runtime_root / "runner.lock":
        raise pi.PackageImportError(
            "RUNNER_LOCK_PATH_DRIFT",
            "state rollback must use the canonical runtime runner.lock",
        )
    with exclusive_runner_commit(runtime_root) as lease:
        current = read_exact_state_preimage(token.state_path, runtime_root=runtime_root)
        current_sha256 = pi.sha256_bytes(current or b"")
        if current_sha256 != token.postimage_sha256:
            raise pi.PackageImportError(
                "STATE_ROLLBACK_POSTIMAGE_DRIFT",
                "state changed after import bind; automatic rollback refused",
                hint=f"compare {token.backup_path} with {token.state_path} while "
                "holding runner.lock before any further production",
            )
        _restore_state_preimage_locked(
            state_path=token.state_path,
            backup_path=token.backup_path,
            preimage_sha256=token.preimage_sha256,
            runtime_root=runtime_root,
            lease=lease,
        )
    receipt.add(
        "STATE_BIND",
        "ROLLED_BACK_AFTER_GATE_FAILURE",
        state_path=str(token.state_path),
        state_backup_path=str(token.backup_path),
        restored_sha256="sha256:" + token.preimage_sha256,
    )


def _restore_state_preimage_locked(
    *, state_path: Path, backup_path: Path, preimage_sha256: str,
    runtime_root: Path, lease,
) -> None:
    """Restore and verify exact bytes while the caller holds runner.lock."""

    backup = pi.require_regular_file(backup_path, label="state preimage backup")
    payload = backup.read_bytes()
    if pi.sha256_bytes(payload) != preimage_sha256:
        raise pi.PackageImportError(
            "STATE_ROLLBACK_BACKUP_DRIFT",
            f"state preimage backup changed: {backup}",
        )
    current = read_exact_state_preimage(state_path, runtime_root=runtime_root)
    write_exact_state_bytes_under_lease(
        state_path, runtime_root=runtime_root, lease=lease,
        expected_before=current, after_bytes=payload,
    )
    if pi.sha256_file(state_path) != preimage_sha256:
        raise pi.PackageImportError(
            "STATE_ROLLBACK_VERIFY_FAILED",
            "restored state bytes do not match the frozen preimage",
        )


def _step_manifest(
    *,
    receipt: Receipt,
    gate_runner: GateRunner,
    destination_package_root: Path,
    state_path: Path,
    deployed_commit_file: Path,
    candidate_id: str,
) -> str:
    manifest_path = destination_package_root / "review_manifest.json"
    result = gate_runner.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "build_daily_review_manifest.py"),
            str(destination_package_root),
            "--state",
            str(state_path),
            "--deployed-commit-file",
            str(deployed_commit_file),
            "--candidate",
            candidate_id,
        ]
    )
    if result.returncode != 0:
        raise _refuse(
            "MANIFEST",
            "MANIFEST_BUILDER_REFUSED",
            (result.stdout + result.stderr).strip()[-2000:],
            hint="fix the package/state binding the builder names; do not "
            "hand-edit review_manifest.json",
        )
    try:
        manifest, _ = pi.load_json_document(manifest_path, label="review manifest")
    except pi.PackageImportError as error:
        raise Refusal("MANIFEST", error) from error
    items = manifest.get("items")
    if not isinstance(items, list) or len(items) != 1:
        raise _refuse(
            "MANIFEST",
            "MANIFEST_ITEM_COUNT_UNEXPECTED",
            f"review_manifest carries {len(items or [])} item(s); expected 1",
        )
    title = items[0].get("title")
    if not isinstance(title, str) or not title.strip():
        raise _refuse(
            "MANIFEST", "MANIFEST_TITLE_MISSING", "manifest item lacks a title"
        )
    receipt.add(
        "MANIFEST",
        "PASS",
        path=str(manifest_path),
        sha256=pi.sha256_file(manifest_path),
        title=title,
        title_sha256="sha256:" + _sha256_text(title),
        cover=items[0].get("cover"),
        video=items[0].get("video"),
    )
    return title


def _step_audit(
    *,
    receipt: Receipt,
    gate_runner: GateRunner,
    destination_package_root: Path,
) -> Path:
    result = gate_runner.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "audit_review_package.py"),
            "--json",
            str(destination_package_root),
        ]
    )
    try:
        report = json.loads(result.stdout)
    except ValueError as error:
        raise _refuse(
            "AUDIT",
            "AUDIT_OUTPUT_UNPARSEABLE",
            (result.stdout + result.stderr).strip()[-2000:],
        ) from error
    blocking = [
        issue
        for issue in report.get("issues") or []
        if str(issue.get("severity") or "BLOCK") == "BLOCK"
    ]
    if not report.get("passed") or report.get("blocking_issue_count"):
        raise _refuse(
            "AUDIT",
            "PACKAGE_AUDIT_BLOCKED",
            "; ".join(
                f"{issue.get('code')} {issue.get('stem', '')} "
                f"{issue.get('detail', '')}".strip()
                for issue in blocking[:20]
            )
            or "package audit did not pass",
            hint="every blocking issue must be repaired in the package or the "
            "code; the audit is never bypassed",
        )
    audit_path = destination_package_root / PACKAGE_AUDIT_NAME
    audit_payload = result.stdout.encode("utf-8")
    audit_write_status = _create_or_verify_audit_report(
        path=audit_path,
        payload=audit_payload,
    )
    receipt.add(
        "AUDIT",
        "PASS",
        path=str(audit_path),
        sha256=pi.sha256_file(audit_path),
        write_status=audit_write_status,
        blocking_issue_count=int(report.get("blocking_issue_count") or 0),
        issue_count=int(report.get("issue_count") or 0),
    )
    return audit_path


def _create_or_verify_audit_report(*, path: Path, payload: bytes) -> str:
    """Atomically create immutable audit evidence, or accept identical bytes.

    Re-import is intentionally idempotent, so an identical existing report is
    reused.  A symlink, non-regular path, or different existing report is a
    hard refusal: the importer never overwrites governed audit evidence.
    """

    try:
        pi.require_directory(path.parent, label="package audit parent")
    except pi.PackageImportError as error:
        raise Refusal("AUDIT", error) from error

    if path.is_symlink():
        raise _refuse(
            "AUDIT",
            "AUDIT_REPORT_PATH_UNSAFE",
            f"package audit output may not be a symlink: {path}",
        )
    if path.exists():
        try:
            existing = pi.require_regular_file(path, label="package audit output")
        except pi.PackageImportError as error:
            raise Refusal("AUDIT", error) from error
        if existing.read_bytes() != payload:
            raise _refuse(
                "AUDIT",
                "AUDIT_REPORT_OVERWRITE_REFUSED",
                f"different governed audit evidence already exists: {path}",
                hint="preserve the existing report and investigate why the "
                "same package now produces different audit bytes",
            )
        return "ALREADY_IDENTICAL"

    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.tmp-"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # A hard link publishes the already-fsynced complete file and, like
            # O_EXCL, refuses to replace anything that won the destination race.
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            if path.is_symlink():
                raise _refuse(
                    "AUDIT",
                    "AUDIT_REPORT_PATH_UNSAFE",
                    f"package audit output became a symlink: {path}",
                )
            try:
                existing = pi.require_regular_file(
                    path, label="package audit output"
                )
            except pi.PackageImportError as error:
                raise Refusal("AUDIT", error) from error
            if existing.read_bytes() != payload:
                raise _refuse(
                    "AUDIT",
                    "AUDIT_REPORT_OVERWRITE_REFUSED",
                    f"different governed audit evidence won the create race: {path}",
                )
            return "ALREADY_IDENTICAL"
        directory_fd = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Refusal:
        raise
    except OSError as error:
        raise _refuse(
            "AUDIT",
            "AUDIT_REPORT_CREATE_FAILED",
            f"{type(error).__name__}: {error}",
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)

    try:
        written = pi.require_regular_file(path, label="package audit output")
    except pi.PackageImportError as error:
        raise Refusal("AUDIT", error) from error
    if written.read_bytes() != payload:
        raise _refuse(
            "AUDIT",
            "AUDIT_REPORT_WRITE_DRIFT",
            f"persisted package audit bytes differ from auditor stdout: {path}",
        )
    return "CREATED"


def _step_title_cover_qc(
    *,
    receipt: Receipt,
    gate_runner: GateRunner,
    destination_package_root: Path,
    candidate_id: str,
    title: str,
    same_stem_cover: Path,
    skip_qc: bool,
) -> None:
    if skip_qc:
        receipt.add(
            "TITLE_COVER_QC",
            "SKIPPED_BY_OPERATOR",
            detail="--skip-qc was passed; the joint QC receipt is still a hard "
            "requirement for make-manifest",
        )
        return
    out_path = (
        destination_package_root / f"{candidate_id}.title-cover-joint-qc.json"
    )
    # 标题**必须**程序化地从 review_manifest 取：手抄会把全角引号打成 ASCII，
    # QC 回执的 title_sha256 就与 manifest 不符（rerun1 实拒）。
    result = gate_runner.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "run_title_cover_joint_qc.py"),
            str(destination_package_root),
            title,
            str(out_path),
        ]
    )
    if result.returncode != 0 or not out_path.is_file():
        raise _refuse(
            "TITLE_COVER_QC",
            "JOINT_QC_FAILED",
            (result.stdout + result.stderr).strip()[-2000:],
            hint="a FAIL verdict is a real judgement: repair the cover/title, "
            "then re-run; never re-roll the same bytes hoping for green",
        )
    try:
        qc, _ = pi.load_json_document(out_path, label="joint QC receipt")
    except pi.PackageImportError as error:
        raise Refusal("TITLE_COVER_QC", error) from error
    expected_title_sha = "sha256:" + _sha256_text(title)
    if str(qc.get("title_sha256") or "") != expected_title_sha:
        raise _refuse(
            "TITLE_COVER_QC",
            "QC_TITLE_SHA_MISMATCH",
            f"receipt={qc.get('title_sha256')!r} manifest={expected_title_sha}",
            hint="the QC receipt was produced for a different title string",
        )
    if str(qc.get("cover_path") or "") != str(same_stem_cover):
        raise _refuse(
            "TITLE_COVER_QC",
            "QC_COVER_NOT_SAME_STEM",
            f"receipt={qc.get('cover_path')!r} expected={same_stem_cover}",
            hint="the QC verdict must bind the same-stem delivery alias the "
            "upload manifest carries (2026-08-09 B2 defect)",
        )
    if qc.get("status") != "PASS" or qc.get("pass") is not True:
        raise _refuse(
            "TITLE_COVER_QC",
            "JOINT_QC_VERDICT_FAIL",
            json.dumps(qc.get("verdict"), ensure_ascii=False)[:1000],
        )
    receipt.add(
        "TITLE_COVER_QC",
        "PASS",
        path=str(out_path),
        sha256=pi.sha256_file(out_path),
        title_sha256=qc.get("title_sha256"),
        cover_path=qc.get("cover_path"),
        cover_sha256=qc.get("cover_sha256"),
    )


def _step_make_manifest_hint(
    *,
    receipt: Receipt,
    destination_package_root: Path,
    package_audit_path: Path | None = None,
    title: str | None,
) -> None:
    stem = "<video stem>"
    audit_path = package_audit_path or (
        destination_package_root / PACKAGE_AUDIT_NAME
    )
    receipt.add(
        "MAKE_MANIFEST",
        "SKIPPED_OUT_OF_SCOPE",
        detail="upload authorization is out of this tool's scope: "
        "authorized_upload make-manifest binds 维护者's verbatim quote and the "
        "publication registry is the sole upload authority",
        next_command=[
            "python3",
            "scripts/authorized_upload.py",
            "make-manifest",
            "--video",
            f"{destination_package_root}/{stem}.mp4",
            "--cover",
            f"{destination_package_root}/{stem}.cover.png",
            "--package-audit",
            str(audit_path),
            "--title",
            title if title is not None else "<title from review_manifest>",
            "--authorized-by",
            "维护者",
            "--quote",
            "<维护者 的逐字授权引语>",
            "--title-cover-qc",
            f"{destination_package_root}/<candidate>.title-cover-joint-qc.json",
            "--season",
            "auto",
        ],
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source",
        required=True,
        type=Path,
        help="local directory holding the external package's replacement_recuts "
        "(rsync/scp it here first; cross-host transfer is out of scope)",
    )
    parser.add_argument("--date", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--base", type=Path, default=DEFAULT_BASE)
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--state", type=Path, default=None)
    parser.add_argument("--runner-lock", type=Path, default=None)
    parser.add_argument("--deployed-commit-file", type=Path, default=None)
    parser.add_argument("--receipt", type=Path, default=None)
    parser.add_argument("--source-repo-root", default=None)
    parser.add_argument("--source-workspace-root", default=None)
    parser.add_argument(
        "--allow-new-pick",
        action="store_true",
        help="create a picks row for a candidate free never produced itself "
        "(fields are read out of the package's frozen documents)",
    )
    parser.add_argument(
        "--adopt-failed-pick",
        metavar="CID",
        help="one-shot adoption of the exact failed picks row authorized by "
        "the committed publication registry; requires --release-quote, must "
        "equal --candidate, and cannot be combined with --allow-new-pick",
    )
    parser.add_argument(
        "--release-quote",
        help="维护者's verbatim release quote; only valid together with "
        "--adopt-failed-pick and must exactly match the committed registry",
    )
    parser.add_argument(
        "--skip-qc",
        action="store_true",
        help="skip the real-CPA title+cover joint QC (it still gates upload)",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    base = args.base
    repo_root = args.repo_root or base / "repo"
    state_path = args.state or base / "state" / f"{args.date}.json"
    runner_lock = args.runner_lock or base / "runner.lock"
    deployed_commit_file = args.deployed_commit_file or repo_root / "DEPLOYED_COMMIT"
    package_root = base / "out" / args.date / args.candidate / "replacement_recuts"
    receipt_path = args.receipt or (
        package_root / f"{args.candidate}.external-import-receipt.json"
    )

    document, code = run_import(
        source=args.source,
        date=args.date,
        candidate_id=args.candidate,
        base=base,
        repo_root=repo_root,
        state_path=state_path,
        runner_lock=runner_lock,
        deployed_commit_file=deployed_commit_file,
        apply=args.apply,
        allow_new_pick=args.allow_new_pick,
        skip_qc=args.skip_qc,
        source_repo_root=args.source_repo_root,
        source_workspace_root=args.source_workspace_root,
        gate_runner=GateRunner(ROOT),
        adopt_failed_pick=args.adopt_failed_pick,
        release_quote=args.release_quote,
    )
    # 回执落包内（要求：每步结果 typed 存档）。包目录还不存在时——dry-run，或
    # PREFLIGHT 就被拒——不为了写回执去凭空创建产线目录：退回 stdout。
    written = ""
    if args.receipt is not None or receipt_path.parent.is_dir():
        pi.atomic_write_json(receipt_path, document)
        written = str(receipt_path)
    print(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True))
    if written:
        print(f"receipt: {written}", file=sys.stderr)
    print(f"status: {document['status']}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
