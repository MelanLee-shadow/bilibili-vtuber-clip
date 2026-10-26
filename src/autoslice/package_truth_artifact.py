"""Resolve current/frozen truth by trusted bytes and the actual package scope."""

from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256, sha256_ref
import hashlib
import json
from pathlib import Path

from .clip_context import validate_clip_context
from .runtime_code_lease import immutable_version, repository_runtime_root
from .source_subtitle_truth import _entry_local_windows, _load_source_aliases
from .source_truth_governance import assertion_state, validate_ledger_governance


def _sha(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _registered_asset(repo: Path, relative: Path) -> bool:
    """Retained profile bytes must still match their completed deployment seal."""
    try:
        path = repo / relative
        manifest = repo / "DEPLOYED_AUTHORITY_MANIFEST.json"
        marker = repo / "DEPLOYED_COMMIT"
        if any(p.is_symlink() or not p.is_file() for p in (path, manifest, marker)):
            return False
        document = json.loads(manifest.read_bytes())
        body = dict(document)
        declared = body.pop("manifest_sha256")
        raw = path.read_bytes()
        return (
            body.get("schema_version") == "deployed-authority-manifest.v1"
            and body.get("deployed_commit") == marker.read_text().strip()
            and _sha(
                json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
            )
            == declared
            and body["entries"].get(relative.as_posix()) == {"bytes": len(raw), "sha256": _sha(raw)}
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def profile_ledgers(canonical: Path, repo_root: Path):
    """Only the selected profile asset in registered retained code authorities."""
    deployed = (repo_root / "DEPLOYED_AUTHORITY_MANIFEST.json").exists()
    try:
        relative = canonical.relative_to(repo_root)
    except ValueError:
        if not deployed:
            yield canonical
        return
    if not deployed or _registered_asset(repo_root, relative):
        yield canonical
    base = repository_runtime_root(repo_root)
    if _registered_asset(base / "repo", relative):
        yield base / "repo" / relative
    versions = base / "versions"
    if versions.is_dir():
        for version in sorted(versions.iterdir()):
            prior = version / "repo"
            try:
                if immutable_version(prior) and _registered_asset(prior, relative):
                    yield prior / relative
            except (OSError, RuntimeError, ValueError):
                continue


def _document(raw):
    doc = json.loads(raw)
    if doc.get("schema_version") != "source-subtitle-truth-ledger.v1":
        raise ValueError("unrecognized truth ledger")
    validate_ledger_governance(doc, doc["entries"])
    return doc


def _pieces(provenance, *, record=None, context=None):
    raw = provenance["source_piece"]
    rows = raw if isinstance(raw, list) else [raw]
    pieces = []
    for row in rows:
        source = row["source_path"]
        digest = str(row.get("source_media_binding") or row.get("source_sha256") or "")
        digest = digest.removeprefix("sha256:")
        start, end = row["start_ms"], row["end_ms"]
        if (
            len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
            or type(start) is not int
            or type(end) is not int
            or not 0 <= start < end
        ):
            raise ValueError("unbound source piece")
        pieces.append(
            {
                "remote_media": source,
                "source_media_sha256": "sha256:" + digest,
                "start_ms": start,
                "end_ms": end,
            }
        )
    if context is not None:
        expected = [
            (p["recording_basename"], p["source_media_sha256"], p["start_ms"], p["end_ms"])
            for p in context["pieces"]
        ]
        actual = [
            (Path(p["remote_media"]).name, p["source_media_sha256"], p["start_ms"], p["end_ms"])
            for p in pieces
        ]
        if actual != expected:
            raise ValueError("package source/context scope drift")
    recut = provenance.get("final_recut")
    if isinstance(recut, dict):
        if len(pieces) != 1:
            raise ValueError("ambiguous absolute recut scope")
        piece = pieces[0]
        start, end = recut["absolute_source_start_ms"], recut["absolute_source_end_ms"]
        if (
            type(start) is not int
            or type(end) is not int
            or not piece["start_ms"] <= start < end <= piece["end_ms"]
            or start != piece["start_ms"] + recut["start_ms"]
            or end != piece["start_ms"] + recut["end_ms"]
        ):
            raise ValueError("final source window drift")
        if record is not None and (
            record.get("start_ms") != 0 or record.get("end_ms") != end - start
        ):
            raise ValueError("record/provenance source window drift")
        piece.update(start_ms=start, end_ms=end)
    return pieces


def _scope(doc, pieces):
    aliases = _load_source_aliases(doc)
    durations = [p["end_ms"] - p["start_ms"] for p in pieces]
    result = {}
    for entry in doc["entries"]:
        if entry.get("knowledge_type") != "SOURCE_INTERVAL_TRUTH":
            continue
        if assertion_state(entry) in {"PROPOSED", "REJECTED", "SUPERSEDED"}:
            continue
        windows = _entry_local_windows(
            entry, pieces=pieces, durations=durations, source_aliases=aliases
        )
        if windows:
            if assertion_state(entry) == "CONFLICTED":
                raise ValueError("conflicted applicable truth")
            identity = entry["truth_id"]
            if identity in result:
                raise ValueError("ambiguous truth history")
            result[identity] = _sha(
                json.dumps(
                    {"entry": entry, "local_windows": windows},
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
            )
    return result


def source_truth_artifact_valid(
    truth, *, canonical, repo_root, provenance, authority, record, record_path
):
    """One content identity, explicit frozen ownership, and scoped progression.

    A portable consumer may resolve identical bytes at the selected profile
    asset instead of chasing a missing producer locator. A present frozen file
    must still be regular and exact; an unregistered historical digest cannot
    manufacture a ledger that the committed profile never contained.
    """
    try:
        if (repo_root / "DEPLOYED_AUTHORITY_MANIFEST.json").exists() and not _registered_asset(
            repo_root, canonical.relative_to(repo_root)
        ):
            return False
        declared = Path(truth["ledger_path"])
        digest = truth["ledger_sha256"]
        trusted = [
            p for p in profile_ledgers(canonical, repo_root) if p.is_file() and not p.is_symlink()
        ]
        matching = [p for p in trusted if sha256_ref(_evidence_file_sha256(p)) == digest]
        if not matching:
            return False
        frozen = declared.name != canonical.name
        context = None
        if frozen:
            attempt = authority.get("source_truth_attempt") or {}
            binding = attempt.get("ledger") or {}
            candidate = record.get("candidate_id")
            producer = Path(record["chat_authority_audit_path"]).parent
            if (
                attempt.get("schema_version") != "producer-source-truth-attempt.v1"
                or binding != {"path": str(declared), "sha256": digest}
                or producer.name != candidate
                or declared.parent != producer / "source-truth-input"
                or declared.name != "ledger." + digest.removeprefix("sha256:") + ".json"
            ):
                return False
            if declared.exists() or declared.is_symlink():
                if (
                    declared.is_symlink()
                    or not declared.is_file()
                    or declared.resolve() != declared
                    or sha256_ref(_evidence_file_sha256(declared)) != digest
                ):
                    return False
            local = (
                record_path.with_name(
                    record_path.name.removesuffix(".record.json") + ".clip-context.json"
                )
                if record_path is not None
                else None
            )
            context_path = (
                local if local is not None and local.exists() else Path(record["clip_context_path"])
            )
            if context_path.is_symlink():
                return False
            context = json.loads(context_path.read_bytes())
            validate_clip_context(context, candidate_id=candidate)
            if record.get("clip_context_payload_sha256") != context["context_sha256"]:
                return False
        elif declared not in trusted:
            return False
        elif digest == sha256_ref(_evidence_file_sha256(canonical)):
            # Existing canonical audits already carry their producer owner contract.
            # Scope replay is required when resolving a frozen locator or old bytes.
            _document(canonical.read_bytes())
            return True
        pieces = _pieces(provenance, record=record if frozen else None, context=context)
        previous = _scope(_document(matching[0].read_bytes()), pieces)
        current = _scope(_document(canonical.read_bytes()), pieces)
        if previous != current:
            return False
        known = {row["truth_id"] for key in ("applied", "satisfied") for row in truth[key]}
        if truth["status"] == "NO_RELEVANT_INTERVAL":
            return not current
        return set(current) <= known
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, AttributeError):
        return False
