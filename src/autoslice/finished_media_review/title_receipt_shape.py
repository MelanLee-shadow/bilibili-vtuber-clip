"""Shape-only check of a verified FMR title receipt, for a host that cannot read the first-publication evidence.

`--authority-shape-only` of the finished-media lane's `check` / `seal` skips the replay of root-only files for the repair
authority; this does the same for `--title-receipt`.  What is checked here needs only the receipt file itself and the
(already shape-validated) authority: the schema and the status, the receipt's own call counts and before hash, the identity of
the BV, the title chain (original -> final) and that the final title is the package's, the before/after surfaces differing in
the title alone, and the authorization fields being present.  What is skipped is exactly the part that reads root-only files:
rebuilding the receipt's binding from the first-publication manifest, record and subtitle and the registry
(`authorized_upload_title_repair.validate_title_repair_successor`).  The upload tool and the reconciliation replay that in
full on the record's `verified_title_repair`, so a receipt that passes here is not accepted without a complete check."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any


def validate_title_receipt_shape(
    receipt_path: Path, *, authority: Mapping[str, Any], candidate_id: str, title: str,
) -> None:
    """Raise `TitleRepairError` when the receipt at `receipt_path` cannot be the verified title edit that gave `title`."""
    from src.autoslice.authorized_upload_title_repair import (
        SCHEMA_VERSION, TitleRepairError, _binding_from_receipt, _normal_sha, _read_json, _required_text,
        _stable_without_title, _surface_identity, _validate_receipt,
    )
    from src.autoslice.part_name_contract import same_stable

    receipt = _read_json(receipt_path, "PREDECESSOR_TITLE_RECEIPT")
    binding = _binding_from_receipt(receipt)
    before, after = receipt.get("before"), receipt.get("after")
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        raise TitleRepairError("TITLE_REPAIR_SUCCESSOR_SNAPSHOTS_MISSING")
    if receipt.get("schema_version") != SCHEMA_VERSION:
        raise TitleRepairError("TITLE_REPAIR_RECEIPT_SCHEMA_INVALID")
    _validate_receipt(receipt, binding=binding, before=before)       # status in the edit states, call counts, before hash
    if receipt.get("status") != "VERIFIED_TITLE_ONLY":
        raise TitleRepairError("TITLE_REPAIR_SUCCESSOR_NOT_VERIFIED")
    invalid = "TITLE_REPAIR_PREDECESSOR_TITLE_RECEIPT_BINDING_INVALID"
    original, final = (_required_text(binding.get(key), invalid) for key in ("original_title", "final_title"))
    if binding.get("candidate_id") != candidate_id or any(
            binding.get(key) != authority.get(key) for key in ("bvid", "aid", "cid", "candidate_id")):
        raise TitleRepairError("TITLE_REPAIR_SUCCESSOR_IDENTITY_DRIFT")
    if final != title or final == original:
        raise TitleRepairError("TITLE_REPAIR_PREDECESSOR_TITLE_RECEIPT_TITLE_CHAIN_INVALID")
    if binding.get("predecessor_title_receipt") is None and binding.get("predecessor_repair_completed") is None \
            and original != authority.get("observed_public_title"):
        raise TitleRepairError("TITLE_REPAIR_PREDECESSOR_TITLE_RECEIPT_TITLE_CHAIN_INVALID")     # a first edit starts from the first title
    for key in ("authorized_by", "quote"):
        _required_text(binding.get(key), "TITLE_REPAIR_AUTHORIZATION_MISSING")
    if _normal_sha(binding.get("title_authority_sha256")) is None:
        raise TitleRepairError("TITLE_REPAIR_AUTHORIZATION_MISSING")
    section_id = binding.get("section_id")
    if not all(_surface_identity(snapshot, bvid=str(binding.get("bvid")), aid=int(binding["aid"]), cid=int(binding["cid"]),
                                 title=name) and snapshot["section"].get("section_id") == section_id
               for snapshot, name in ((before, original), (after, final))) \
            or not same_stable(before, after, _stable_without_title):
        raise TitleRepairError("TITLE_REPAIR_SUCCESSOR_NON_TITLE_DRIFT")
