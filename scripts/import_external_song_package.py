"""Import one externally produced **song review package** into free.

谈话切走 ``scripts/import_external_package.py``；歌切的成品是压平改名的评审包，
形状不同（见 ``src.autoslice.song_package_import`` 的模块注释），所以走这里。

用法（在 free 的部署仓库根目录下跑）：

    python3 scripts/import_external_song_package.py \\
        --source /opt/bilive/autoslice/incoming/song_210131_1210-r1 \\
        --date 2026-08-08 --candidate song_210131_1210 --apply

不带 ``--apply`` 是 dry-run：验证包内整条歌切证据链、算出要搬的字节、并在**持
runner.lock 只读**的前提下预判 state 能不能绑——一个字节都不写。

做了什么 / 没做什么：

- ①两处落地（评审包根 + ``repo/lidousha/{date}/`` 扁平交付）、②落地字节复核、
  ③目的地重跑 canonical 审计器、④按上传面自己的判据做平价核验、⑤state 绑
  ``songs`` 行 —— 做；
- ⑥``authorized_upload make-manifest`` —— **不做**（上传授权面，需要 Ivan 的逐
  字引语 + 仓内出版登记）。回执里给出该跑的命令。

导入后这条片**默认仍不可上传**：``songs`` 行是 ``delivery_upload_enabled=false``，
包自己是 ``upload_allowed=false``，上传唯一授权仍是仓内
``assets/lidousha/publication_registry.v1.json``。

state 写入全程持 ``runner.lock``（2026-08-09 实事故：不持锁的带外手术会被 tick
的陈旧写回整体抹掉）。退出码 0=全绿，2=被拒（回执已落盘），3=用法/环境错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice import package_import as pi  # noqa: E402
from src.autoslice import song_package_import as spi  # noqa: E402
from src.autoslice.publication_reconciliation import (  # noqa: E402
    project_publication_closure,
)
from src.autoslice.runner_state_writeback import (  # noqa: E402
    RunnerStateWritebackError,
    write_state,
)


DEFAULT_BASE = Path("/opt/bilive/autoslice")
STEP_ORDER = ("PREFLIGHT", "COPY", "VERIFY", "AUDIT", "UPLOAD_GATE", "STATE_BIND")


class Refusal(RuntimeError):
    def __init__(self, step: str, error: pi.PackageImportError) -> None:
        super().__init__(f"{step}: {error}")
        self.step = step
        self.error = error


def _refuse(step: str, code: str, detail: str, hint: str = "") -> Refusal:
    return Refusal(step, pi.PackageImportError(code, detail, hint=hint))


class Receipt:
    def __init__(self, **head: Any) -> None:
        self.head = head
        self.steps: list[dict[str, Any]] = []

    def add(self, step: str, status: str, **detail: Any) -> None:
        self.steps.append({"step": step, "status": status, **detail})

    def as_dict(self, *, status: str) -> dict[str, Any]:
        done = {row["step"] for row in self.steps}
        return {
            "schema_version": spi.SONG_PACKAGE_IMPORT_SCHEMA_VERSION,
            "status": status,
            **self.head,
            "finished_at": pi.now_utc(),
            "steps": self.steps,
            "not_reached": [step for step in STEP_ORDER if step not in done],
        }


def _read_state(path: Path) -> dict[str, Any]:
    document, _ = pi.load_json_document(path, label="daily state")
    return document


def _backup_state(state_path: Path, stamp: str) -> Path:
    backup = state_path.with_name(f"{state_path.name}.pre-song-import-{stamp}")
    if state_path.is_file():
        pi.atomic_write_bytes(backup, state_path.read_bytes())
    return backup


def _regenerate_package_audit(package_root: Path, *, apply: bool) -> dict[str, Any]:
    """The producing host's audit names its own root; the destination re-runs it."""

    from scripts.build_lidousha_song_review_manifest import (
        SongReviewManifestError,
        _atomic_json,
        run_canonical_auditor,
    )

    try:
        audit = run_canonical_auditor(package_root)
    except SongReviewManifestError as error:
        raise _refuse(
            "AUDIT",
            "PACKAGE_AUDIT_BLOCKED",
            str(error)[-2000:],
            hint="every blocking issue must be repaired in the package or the "
            "code; the audit is never bypassed",
        ) from error
    if Path(str(audit.get("root") or "")).resolve() != package_root.resolve():
        raise _refuse(
            "AUDIT",
            "PACKAGE_AUDIT_ROOT_MISMATCH",
            f"auditor reports root={audit.get('root')!r} for {package_root}",
        )
    if apply:
        _atomic_json(package_root / spi.PACKAGE_AUDIT_NAME, audit)
    return audit


def _upload_gate_parity(package_root: Path, plan: spi.SongImportPlan) -> None:
    """Let the upload gate itself be the arbiter — parity by construction.

    ``authorized_upload._strict_verified_song_package`` is what decides at
    upload time whether this is a canonical verified-Song envelope.  Asserting
    it here means the import cannot land something the upload lane would later
    refuse, and it cannot drift away from that predicate either.
    """

    from scripts import authorized_upload

    audit_path = package_root / spi.PACKAGE_AUDIT_NAME
    review, _ = pi.load_json_document(
        package_root / spi.REVIEW_MANIFEST_NAME, label="landed review manifest"
    )
    audit, _ = pi.load_json_document(audit_path, label="landed package audit")
    record, _ = pi.load_json_document(
        package_root / f"{plan.documents.stem}.record.json",
        label="landed song record",
    )
    if not authorized_upload._strict_verified_song_package(
        root=package_root.resolve(),
        review=review,
        review_item=dict(review["items"][0]),
        record=record,
        audit=audit,
    ):
        raise _refuse(
            "UPLOAD_GATE",
            "UPLOAD_GATE_REJECTS_LANDED_PACKAGE",
            "the landed package is not recognized as a canonical verified-Song "
            "authority envelope by the upload gate",
            hint="do not import it; the upload lane would refuse it later",
        )


def run_song_import(
    *,
    source: Path,
    date: str,
    candidate_id: str,
    base: Path,
    state_path: Path,
    runner_lock: Path,
    apply: bool,
    supersede_existing_row: bool,
    registry_path: Path | None = None,
    closure_projector: Callable[[Any], Any] = project_publication_closure,
) -> tuple[dict[str, Any], int]:
    receipt = Receipt(
        candidate_id=candidate_id,
        date=date,
        apply=apply,
        started_at=pi.now_utc(),
        source=str(source),
    )
    package_root = base / "review_packages" / date / f"{candidate_id}-r1"
    delivery_root = base / "repo" / "lidousha" / date
    try:
        try:
            plan = spi.plan_song_import(
                source_package_dir=source,
                destination_package_root=package_root,
                destination_delivery_root=delivery_root,
                candidate_id=candidate_id,
            )
        except pi.PackageImportError as error:
            raise Refusal("PREFLIGHT", error) from error
        if plan.date != date:
            raise _refuse(
                "PREFLIGHT",
                "PACKAGE_DATE_MISMATCH",
                f"the package declares date={plan.date!r}, import was asked "
                f"for {date!r}",
            )
        receipt.add(
            "PREFLIGHT",
            "PASS",
            stem=plan.documents.stem,
            title=plan.documents.title,
            selector_record_candidate_id=plan.documents.source_candidate_id,
            destination_package_root=str(package_root),
            destination_delivery_root=str(delivery_root),
            file_count=len(plan.copies),
            not_copied=list(plan.skipped),
        )

        # state 预判在搬字节之前：能不能绑要先知道，别先落 265MB 再被拒。
        try:
            with pi.exclusive_lock(runner_lock, label="runner.lock"):
                spi.check_song_state_preconditions(
                    _read_state(state_path),
                    candidate_id=candidate_id,
                    date=date,
                    project_closure=closure_projector,
                    allow_supersede_blocked=supersede_existing_row,
                    registry_path=registry_path,
                )
        except pi.PackageImportError as error:
            raise Refusal("PREFLIGHT", error) from error

        try:
            copied = pi.execute_copy(plan, apply=apply)
        except pi.PackageImportError as error:
            raise Refusal("COPY", error) from error
        if copied["divergent_destination_count"]:
            raise _refuse(
                "COPY",
                "DESTINATION_DIVERGENT",
                f"{copied['divergent_destination_count']} destination file(s) "
                "hold different bytes than the package",
                hint="archive the destination and import again from scratch",
            )
        receipt.add(
            "COPY",
            "PASS" if apply else "DRY_RUN",
            file_count=copied["file_count"],
            total_bytes=copied["total_bytes"],
        )
        if not apply:
            receipt.add(
                "VERIFY",
                "SKIPPED_DRY_RUN",
                detail="landed-byte verification, audit regeneration, upload-gate "
                "parity and the state bind all need --apply",
            )
            return receipt.as_dict(status="DRY_RUN_OK"), 0

        try:
            verified = spi.verify_landed_song_delivery(plan)
        except pi.PackageImportError as error:
            raise Refusal("VERIFY", error) from error
        receipt.add("VERIFY", "PASS", verified_roles=sorted(verified))

        audit = _regenerate_package_audit(package_root, apply=True)
        receipt.add(
            "AUDIT",
            "PASS",
            package_audit_path=str(package_root / spi.PACKAGE_AUDIT_NAME),
            issue_count=int(audit.get("issue_count") or 0),
            blocking_issue_count=int(audit.get("blocking_issue_count") or 0),
        )

        _upload_gate_parity(package_root, plan)
        receipt.add("UPLOAD_GATE", "PASS")

        bound_at = pi.now_utc()
        stamp = bound_at.replace(":", "").replace("-", "")
        try:
            with pi.exclusive_lock(runner_lock, label="runner.lock"):
                before_state = _read_state(state_path)
                after_state, delta = spi.build_bound_song_state(
                    before_state,
                    plan=plan,
                    date=date,
                    bound_at=bound_at,
                    project_closure=closure_projector,
                    allow_supersede_blocked=supersede_existing_row,
                    registry_path=registry_path,
                )
                backup = _backup_state(state_path, stamp)
                try:
                    if after_state != before_state:
                        write_state(
                            state_path,
                            dict(after_state),
                            updated_at=bound_at,
                            log=lambda message: print(message, file=sys.stderr),
                        )
                except (OSError, RunnerStateWritebackError) as error:
                    raise pi.PackageImportError(
                        "STATE_WRITE_FAILED",
                        f"{type(error).__name__}: {error}",
                        hint=f"restore {backup} over {state_path} while holding "
                        "runner.lock before anything else touches the day",
                    ) from error
                readback = _read_state(state_path)
        except pi.PackageImportError as error:
            raise Refusal("STATE_BIND", error) from error
        row = next(
            (
                item
                for item in readback.get("songs") or []
                if isinstance(item, dict)
                and item.get("candidate_id") == candidate_id
            ),
            None,
        )
        if (
            row is None
            or row.get("status") != "review_ready"
            or row.get("rc") != 0
            or row.get("delivery_upload_enabled") is not False
        ):
            raise _refuse(
                "STATE_BIND",
                "STATE_READBACK_FAILED",
                "the written state does not carry a review_ready/rc=0/no-upload "
                f"song row for {candidate_id}",
                hint=f"restore {backup} over {state_path} while holding runner.lock",
            )
        receipt.add(
            "STATE_BIND",
            "PASS",
            state_path=str(state_path),
            state_backup_path=str(backup),
            created_song_row=delta["created_song_row"],
            superseded_existing_row=delta["superseded_existing_row"],
            carried_selection_keys=delta["carried_selection_keys"],
            closure_after=delta["closure_after"]["status"],
            delivered=row.get("delivered"),
            cover_path=row.get("cover_path"),
            title=row.get("title"),
        )
    except Refusal as refusal:
        receipt.add(refusal.step, "REFUSE", **refusal.error.as_dict())
        return receipt.as_dict(status="REFUSED"), 2
    receipt.head["next_step"] = {
        "detail": "上传授权面不在本脚本内：需要 Ivan 的逐字放行 + 仓内出版登记",
        "upload_authority": "assets/lidousha/publication_registry.v1.json",
        "command": (
            "python3 scripts/authorized_upload.py make-manifest "
            f"--package-root {package_root} "
            f"--video {plan.delivery_path('video')}"
        ),
    }
    return receipt.as_dict(status="IMPORTED_NO_UPLOAD"), 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--date", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--base", type=Path, default=DEFAULT_BASE)
    parser.add_argument("--state", type=Path, default=None)
    parser.add_argument("--runner-lock", type=Path, default=None)
    parser.add_argument("--registry", type=Path, default=None)
    parser.add_argument("--receipt", type=Path, default=None)
    parser.add_argument(
        "--supersede-existing-row",
        action="store_true",
        help="move this host's own song row verbatim into "
        "song_superseded_attempts (history kept) before binding the import",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    state_path = args.state or (args.base / "state" / f"{args.date}.json")
    runner_lock = args.runner_lock or (args.base / "runner.lock")
    receipt, code = run_song_import(
        source=args.source,
        date=args.date,
        candidate_id=args.candidate,
        base=args.base,
        state_path=state_path,
        runner_lock=runner_lock,
        apply=args.apply,
        supersede_existing_row=args.supersede_existing_row,
        registry_path=args.registry,
    )
    body = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True)
    if args.receipt is not None:
        pi.atomic_write_bytes(args.receipt, (body + "\n").encode("utf-8"))
    else:
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        default = (
            args.base
            / "reports"
            / "song_imports"
            / f"{args.date}-{args.candidate}-{stamp}.json"
        )
        try:
            pi.atomic_write_bytes(default, (body + "\n").encode("utf-8"))
        except OSError:
            pass
    print(body)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
