#!/usr/bin/env python3
"""Build the one Qixi successor, its pending-human manifest, and current audit."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_lidousha_review_package import audit_package  # noqa: E402
from scripts.build_manual_review_manifest import DailyManifestError, build_manual  # noqa: E402
from src.autoslice.qixi_cover_successor_finalization import (  # noqa: E402
    PACKAGE_AUDIT,
    QixiCoverSuccessorError,
    _replace_json,
    finalize,
    seal_private_successor_tree,
    validate_current_successor_audit,
)


def build_review_package(
    *,
    preimage: Path,
    trial_root: Path,
    target: Path,
    punch_response: str,
    operator: str,
    note: str,
) -> dict[str, object]:
    """Create one package and bind only its newly generated audit to it."""

    result = finalize(
        repo_root=ROOT,
        preimage=preimage,
        trial_root=trial_root,
        target=target,
        apply=True,
        punch_response=punch_response,
    )
    package = target / "replacement_recuts"
    manifest = build_manual(
        package,
        operator=operator,
        note=note,
        qixi_repo_root=ROOT,
    )
    _replace_json(package / "review_manifest.json", manifest)
    audit = audit_package(package, qixi_repo_root=ROOT)
    if audit.get("passed") is not True or audit.get("issues") != []:
        raise QixiCoverSuccessorError("canonical successor package audit fails")
    _replace_json(package / PACKAGE_AUDIT, audit)
    seal_private_successor_tree(target)
    validated = validate_current_successor_audit(package_root=package)
    return {
        **result,
        "review_manifest": str(package / "review_manifest.json"),
        "review_manifest_sha256": "sha256:" + hashlib.sha256(
            (package / "review_manifest.json").read_bytes()
        ).hexdigest(),
        "package_audit": str(package / PACKAGE_AUDIT),
        "package_audit_sha256": "sha256:" + hashlib.sha256(
            (package / PACKAGE_AUDIT).read_bytes()
        ).hexdigest(),
        "audit_passed": validated["passed"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preimage", required=True, type=Path)
    parser.add_argument("--trial-root", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--punch-response", required=True, type=Path)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--note", required=True)
    args = parser.parse_args(argv)
    try:
        response = args.punch_response.read_text(encoding="utf-8")
        result = build_review_package(
            preimage=args.preimage,
            trial_root=args.trial_root,
            target=args.target,
            punch_response=response,
            operator=args.operator,
            note=args.note,
        )
    except (DailyManifestError, OSError, QixiCoverSuccessorError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
