#!/usr/bin/env python3
"""Prepare or verify the original fastlane; never run ASR, reburn or upload."""

from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.fastlane_original_patch import OriginalPatchError, load_original_patch, _regular


def _original(candidate: str):
    result = load_original_patch(
        ROOT, load_channel_profile(ROOT).asset_directory("reviewed_subtitle_baselines"), candidate
    )
    if result is None:
        raise OriginalPatchError("no committed original-review patch for candidate")
    return result


def prepare_output(candidate, out, payload, receipt, *, reuse_existing=False) -> dict:
    """Create the original-based preparation, or verify its complete exact bytes.

    A partial/foreign directory is not a resumable success. Existing output is
    never edited, and reuse does not mint a new receipt or refresh its identity.
    """
    out = Path(out).absolute()
    if any(path.is_symlink() for path in (out, *out.parents)):
        raise OriginalPatchError("unsafe output directory")
    expected = {
        candidate + ".srt": payload,
        "original-patch-receipt.json": (
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n"
        ).encode(),
    }
    if reuse_existing and os.path.lexists(out):
        if not out.is_dir() or {p.name for p in out.iterdir()} != set(expected):
            raise OriginalPatchError(
                "existing preparation is incomplete or contains unrelated files"
            )
        if any(_regular(out / name) != data for name, data in expected.items()):
            raise OriginalPatchError(
                "existing original preparation differs from current sealed recipe"
            )
        status = "ORIGINAL_PATCH_REUSED_NO_UPLOAD"
    else:
        out.mkdir(mode=0o700, parents=False, exist_ok=False)
        for name, data in expected.items():
            with (out / name).open("xb") as f:
                os.chmod(out / name, 0o600)
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        fd = os.open(out, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        status = "ORIGINAL_PATCH_PREPARED_NO_UPLOAD"
    return {
        "status": status,
        "out": str(out),
        "release_srt_sha256": receipt["release_srt_sha256"],
        "provider_calls": 0,
        "media_regenerated": False,
        "upload_authorized": False,
    }


def check_existing_package(candidate: str, package_root: Path) -> dict:
    """Run the real original-package gate on existing media, without rewriting it."""
    from src.autoslice.original_patch_package import MANIFEST_SCHEMA, json_file, within
    from scripts.audit_review_package import audit_package

    package_root = package_root.absolute()
    review = json_file(package_root / "review_manifest.json")
    items = review.get("items")
    if (
        review.get("schema_version") != MANIFEST_SCHEMA
        or review.get("upload_allowed") is not False
        or not isinstance(items, list)
        or len(items) != 1
        or not isinstance(items[0], dict)
        or items[0].get("candidate_id") != candidate
    ):
        raise OriginalPatchError("not the requested original fastlane package")
    payload, receipt = _original(candidate)
    subtitle = within(package_root, items[0].get("subtitle_srt"))
    if _regular(subtitle) != payload:
        raise OriginalPatchError(
            "package subtitle differs from the committed original plus patches"
        )
    audit = audit_package(package_root)
    return {
        "status": "ORIGINAL_PACKAGE_VERIFIED_NO_UPLOAD"
        if audit.get("passed") is True
        else "ORIGINAL_PACKAGE_CHECK_FAILED",
        "candidate_id": candidate,
        "package_root": str(package_root),
        "release_srt_sha256": receipt["release_srt_sha256"],
        "audit": audit,
        "provider_calls": 0,
        "media_regenerated": False,
        "fresh_human_full_review_claimed": False,
        "upload_authorized": False,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--out", type=Path, help="private subtitle preparation directory")
    action.add_argument(
        "--check-package",
        type=Path,
        help="read-only current audit of an existing original same-BV package",
    )
    parser.add_argument(
        "--reuse-existing",
        action="store_true",
        help="accept only an existing complete byte-identical preparation",
    )
    args = parser.parse_args(argv)
    if args.check_package is not None:
        if args.reuse_existing:
            parser.error("--reuse-existing only applies to --out")
        result = check_existing_package(args.candidate, args.check_package)
    else:
        payload, receipt = _original(args.candidate)
        result = prepare_output(
            args.candidate, args.out, payload, receipt, reuse_existing=args.reuse_existing
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["status"] == "ORIGINAL_PACKAGE_CHECK_FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
