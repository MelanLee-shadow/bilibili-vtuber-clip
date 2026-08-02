from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path("/opt/bilive/autoslice/recovery/2026-07-22/full-rerun")
PACKAGE = ROOT / "repo/lidousha/2026-07-22"
OUT_SRT = (
    ROOT
    / "out/2026-07-22/auto_193450_670_909/replacement_recuts/"
    "auto_193450_670_909.recut.srt"
)
STEM = "联动对象当面追问“最最最最喜欢”是真"
DELIVERY_SRT = PACKAGE / f"{STEM}.srt"
RECORD = PACKAGE / f"{STEM}.record.json"
MANIFEST = PACKAGE / "review_manifest.json"
LEDGER = ROOT / "reports/upload_ledger.jsonl"
OP_ROOT = (
    ROOT
    / "reports/operator/2026-07-22-package-sidecar-finalization-6e458c2"
)
STAGED_MANIFEST = OP_ROOT / "review_manifest.next.json"
RECEIPT = OP_ROOT / "receipt.json"

EXPECTED_CANONICAL_SRT_SHA = (
    "86aa64f90559dc213f088b95df7325bb94947a481601a31b1b890a04454f9f6e"
)
EXPECTED_RECORD_SHA = (
    "e2246ccc4bc9cb6dbd2a390510bd56c17fa02073d8d4f687f7d00f1756decfa3"
)
EXPECTED_OLD_MANIFEST_SHA = (
    "2051043e46782792db41045f3f44ca28465fbbd13f853a29dbd83660890dc990"
)
EXPECTED_NEW_MANIFEST_SHA = (
    "bd2fb6ae6dc65c641212381ace0734cbccfe13acc95916e13ce6aaac34f24a9f"
)
EXPECTED_LEDGER_SHA = (
    "f3775e2e56cf78707fc249a159a0e97ad394c15854076e7f9315f445535ab225"
)
EXPECTED_OLD_LINE = (
    "这不是主播最最最最喜欢的南町nightin吗，llnnhhb"
)
EXPECTED_NEW_LINES = [
    "这不是主播最最最最喜欢的南町nightin吗，",
    "llnnhhb",
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bytes_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def atomic_write(path: Path, payload: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp-review-sidecar")
    tmp.write_bytes(payload)
    os.replace(tmp, path)


def fail(message: str) -> None:
    raise RuntimeError(message)


def main() -> None:
    if RECEIPT.exists():
        existing = json.loads(RECEIPT.read_text(encoding="utf-8"))
        if existing.get("status") == "COMPLETED":
            print(json.dumps(existing, ensure_ascii=False, indent=2))
            return
        fail("operator receipt already exists but is not COMPLETED")

    if not (ROOT / "DISABLED").exists():
        fail("isolated runner kill switch is missing")
    if sha256(LEDGER) != EXPECTED_LEDGER_SHA:
        fail("upload ledger drifted before sidecar finalization")
    if sha256(OUT_SRT) != EXPECTED_CANONICAL_SRT_SHA:
        fail("canonical subtitle authority drifted")
    if sha256(DELIVERY_SRT) != EXPECTED_CANONICAL_SRT_SHA:
        fail("delivery subtitle is not the expected adopted baseline")
    if sha256(RECORD) != EXPECTED_RECORD_SHA:
        fail("record drifted")
    if sha256(MANIFEST) != EXPECTED_OLD_MANIFEST_SHA:
        fail("old review manifest drifted")
    if sha256(STAGED_MANIFEST) != EXPECTED_NEW_MANIFEST_SHA:
        fail("staged review manifest is not the reviewed local manifest")

    adoption = json.loads(
        (
            ROOT
            / "reports/operator/2026-07-22-official-replay-target-6e458c2-adopt-clean/receipt.json"
        ).read_text(encoding="utf-8")
    )
    if adoption.get("status") != "COMPLETED":
        fail("verified adoption is not complete")
    rebind = json.loads(
        (
            ROOT
            / "reports/operator/2026-07-22-official-replay-target-6e458c2-adopt-clean/"
            "reviewed-cover-rebind-receipt.json"
        ).read_text(encoding="utf-8")
    )
    if rebind.get("status") != "FINALIZED":
        fail("reviewed cover rebind is not finalized")

    before_text = DELIVERY_SRT.read_text(encoding="utf-8")
    if before_text.count(EXPECTED_OLD_LINE) != 1:
        fail("expected long review-sidecar line is not unique")
    replacement = "\n".join(EXPECTED_NEW_LINES)
    after_text = before_text.replace(EXPECTED_OLD_LINE, replacement, 1)
    if after_text.replace(replacement, EXPECTED_OLD_LINE, 1) != before_text:
        fail("sidecar transformation changed more than the intended line wrap")
    if "".join(EXPECTED_NEW_LINES) != EXPECTED_OLD_LINE:
        fail("sidecar line wrap changed the subtitle payload")
    after_srt_bytes = after_text.encode("utf-8")
    after_srt_sha = bytes_sha256(after_srt_bytes)

    record = json.loads(RECORD.read_text(encoding="utf-8"))
    hashes = record.get("artifact_hashes")
    if not isinstance(hashes, dict):
        fail("record artifact_hashes is missing")
    expected_prefixed = "sha256:" + EXPECTED_CANONICAL_SRT_SHA
    if hashes.get("subtitle_sha256") != expected_prefixed:
        fail("record canonical subtitle hash drifted")
    hashes["delivery_subtitle_sha256"] = "sha256:" + after_srt_sha
    record["review_sidecar_layout"] = {
        "schema_version": "review-sidecar-layout.v1",
        "status": "APPLIED",
        "operation": "line_wrap_only",
        "cue_index": 3,
        "canonical_subtitle_path": str(OUT_SRT),
        "canonical_subtitle_sha256": expected_prefixed,
        "delivery_subtitle_path": str(DELIVERY_SRT),
        "delivery_subtitle_sha256_before": expected_prefixed,
        "delivery_subtitle_sha256_after": "sha256:" + after_srt_sha,
        "before_lines": [EXPECTED_OLD_LINE],
        "after_lines": EXPECTED_NEW_LINES,
        "payload_unchanged": True,
        "max_chars_per_line": 28,
        "upload_enabled": False,
        "applied_at": datetime.now(timezone.utc).isoformat(),
    }
    record_bytes = (
        json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")

    OP_ROOT.mkdir(parents=True, exist_ok=True)
    backups = OP_ROOT / "before"
    backups.mkdir(exist_ok=False)
    shutil.copy2(DELIVERY_SRT, backups / DELIVERY_SRT.name)
    shutil.copy2(RECORD, backups / RECORD.name)
    shutil.copy2(MANIFEST, backups / MANIFEST.name)

    try:
        atomic_write(DELIVERY_SRT, after_srt_bytes)
        atomic_write(RECORD, record_bytes)
        atomic_write(MANIFEST, STAGED_MANIFEST.read_bytes())

        if sha256(DELIVERY_SRT) != after_srt_sha:
            fail("delivery subtitle write verification failed")
        if sha256(OUT_SRT) != EXPECTED_CANONICAL_SRT_SHA:
            fail("canonical subtitle changed during packaging")
        if sha256(MANIFEST) != EXPECTED_NEW_MANIFEST_SHA:
            fail("review manifest write verification failed")
        if sha256(LEDGER) != EXPECTED_LEDGER_SHA:
            fail("upload ledger changed during sidecar finalization")

        receipt = {
            "schema_version": "review-sidecar-finalization-receipt.v1",
            "status": "COMPLETED",
            "commit": "6e458c292f63f14da3e96a273930d713b9e66ac1",
            "candidate_id": "auto_193450_670_909",
            "canonical_subtitle_sha256": "sha256:" + EXPECTED_CANONICAL_SRT_SHA,
            "delivery_subtitle_sha256_before": "sha256:" + EXPECTED_CANONICAL_SRT_SHA,
            "delivery_subtitle_sha256_after": "sha256:" + after_srt_sha,
            "record_sha256_after": "sha256:" + sha256(RECORD),
            "review_manifest_sha256": "sha256:" + sha256(MANIFEST),
            "payload_unchanged": True,
            "line_lengths_after": [len(line) for line in EXPECTED_NEW_LINES],
            "upload_enabled": False,
            "upload_ledger_sha256": "sha256:" + sha256(LEDGER),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        atomic_write(
            RECEIPT,
            (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
    except Exception:
        shutil.copy2(backups / DELIVERY_SRT.name, DELIVERY_SRT)
        shutil.copy2(backups / RECORD.name, RECORD)
        shutil.copy2(backups / MANIFEST.name, MANIFEST)
        raise


if __name__ == "__main__":
    main()
