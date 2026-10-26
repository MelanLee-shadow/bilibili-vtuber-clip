"""Freeze one attempt's truth input and replay old phase previews explicitly."""

from __future__ import annotations
from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256, sha256_ref
import copy
import hashlib
import json
from itertools import chain
from pathlib import Path
from .producer_text_checkpoint import _frozen_binding, _frozen_bytes
from .source_subtitle_truth import apply_source_subtitle_truth, build_source_truth_preview_receipt


def freeze_ledger(path: Path | None, out_root: Path) -> Path | None:
    if path is None:
        return None
    _check_frozen(path)
    return Path(_frozen_binding(out_root / "source-truth-input", "ledger", path)["path"])


class FrozenTruthAssets:
    def __init__(self, delegate, out_root: Path, *, withheld: bool):
        self.delegate = delegate
        self.source_truth_origin_path = (
            None if withheld else delegate.profile_asset_file("subtitle_truth_ledger")
        )
        self.ledger = freeze_ledger(self.source_truth_origin_path, out_root)

    def profile_asset_file(self, name):
        return (
            self.ledger
            if name == "subtitle_truth_ledger"
            else self.delegate.profile_asset_file(name)
        )

    def __getattr__(self, name):
        return getattr(self.delegate, name)


def _sha(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _check_frozen(path):
    if path.parent.name == "source-truth-input" and (
        path.is_symlink()
        or not path.is_file()
        or path.stem != "ledger." + _evidence_file_sha256(path)
    ):
        raise ValueError("SOURCE_TRUTH_FROZEN_LEDGER_DRIFT")


def _preview(text, *, stage, path, spec, durations):
    _, audit = apply_source_subtitle_truth(text, spec=spec, durations=durations, ledger_path=path)
    return build_source_truth_preview_receipt(
        input_srt_text=text, source_truth_audit=audit, stage=stage
    )


def added_local_term_review_required(authority, *, spec, durations, ledger_path, current_text):
    """A withheld checkpoint may acquire narrow human terms only with a new review.

    This selects a review lane, never a completed truth or release verdict.
    The original phase receipts are still fully replayed by the consumer.
    """
    previews = authority.get("source_truth_preview_receipts")
    if not isinstance(previews, dict) or set(previews) != {
        "pre_entity_arbitration", "pre_correction_review"
    } or ledger_path is None:
        return False
    if any(not isinstance(row, dict) or row.get("ledger_sha256") is not None
           or row.get("source_truth_audit_status") != "SKIPPED_NO_LEDGER"
           or row.get("protected_cue_indexes") != [] for row in previews.values()):
        return False
    from .final_review_meaning_policy import digest
    from .source_truth_term_alignment import _row_owner
    document = json.loads(ledger_path.read_bytes())
    entries = {digest(entry): entry for entry in document["entries"]}
    _, audit = apply_source_subtitle_truth(current_text, spec=spec, durations=durations,
                                         ledger_path=ledger_path)
    rows = [row for key in ("applied", "satisfied", "failures") for row in audit[key]]
    return bool(rows) and all(
        row.get("entry_sha256") in entries
        and _row_owner(row, entries[row["entry_sha256"]]) is not None
        for row in rows
    )


def _previous_version_ledgers(origin_path, expected_sha):
    """Locate the same asset at retained runtime authority locators.

    A legacy preview hashes the ledger's original locator into its audit.
    Matching bytes merely select a candidate; the caller still replays the
    entire old receipt, including its exact input, window and locator.
    """
    from .runtime_code_lease import immutable_version, repository_runtime_root

    if origin_path is None:
        return
    repo = Path(__file__).resolve().parents[2]
    try:
        relative = Path(origin_path).resolve().relative_to(repo)
    except ValueError:
        return
    versions = repository_runtime_root(repo) / "versions"
    if not versions.is_dir():
        return
    # Pre-versioned previews named the registered legacy repo directly. Its
    # retained asset is a locator candidate only: exact digest and full receipt
    # replay below still bind the old input, ledger and original path.
    legacy = versions.parent / "repo" / relative
    if legacy.is_file() and not legacy.is_symlink() and sha256_ref(_evidence_file_sha256(legacy)) == expected_sha:
        yield legacy
    for version in sorted(versions.iterdir()):
        prior_repo = version / "repo"
        try:
            if not immutable_version(prior_repo):
                continue
        except (OSError, RuntimeError):
            continue
        path = prior_repo / relative
        if path.is_file() and not path.is_symlink() and sha256_ref(_evidence_file_sha256(path)) == expected_sha:
            yield path


def rebase_pending_previews(
    authority,
    *,
    spec,
    durations,
    ledger_path,
    origin_path,
    clip_context,
    name_reference,
    current_text,
    out_root,
    allow_added_local_terms=False,
):
    """Regenerate both active previews from one frozen ledger, keep old proof.

    Every old preview must replay against its own actual ledger and exact
    phase input. Only unchanged candidate-specific protection can reuse an
    already completed review after an unrelated ledger edit.
    """
    previews = authority.get("source_truth_preview_receipts")
    if ledger_path is not None:
        _check_frozen(ledger_path)
    if not isinstance(previews, dict):
        raise ValueError("SOURCE_TRUTH_PREVIEW_RECEIPTS_MISSING")
    root = out_root / "source-truth-input"
    inputs = [str(clip_context.get("whole_clip_draft_srt") or ""), name_reference, current_text]
    old_formal = authority.get("source_subtitle_truth_audit") or {}
    candidates = [None, ledger_path, origin_path]
    if old_formal.get("ledger_path"):
        candidates.append(Path(old_formal["ledger_path"]))
    previous = authority.get("source_truth_attempt") or {}
    saved_history = authority.get("source_truth_preview_history") or []
    if not isinstance(saved_history, list) or any(not isinstance(h, dict) for h in saved_history):
        raise ValueError("SOURCE_TRUTH_PREVIEW_HISTORY_INVALID")
    for binding in [
        previous.get("ledger"),
        *(h.get("ledger") for h in saved_history),
        *(h.get("original_ledger") for h in saved_history),
    ]:
        if isinstance(binding, dict) and binding.get("path"):
            p = Path(binding["path"])
            if p.is_symlink() or not p.is_file() or sha256_ref(_evidence_file_sha256(p)) != binding.get("sha256"):
                raise ValueError("SOURCE_TRUTH_FROZEN_LEDGER_DRIFT")
            candidates.append(p)
    new = {}
    history = list(saved_history)
    added_terms = allow_added_local_terms and added_local_term_review_required(
        authority, spec=spec, durations=durations, ledger_path=ledger_path,
        current_text=current_text,
    )
    changed_stages = []

    def original_ledger(text, stage, receipt):
        for candidate in chain(
            dict.fromkeys(candidates),
            _previous_version_ledgers(origin_path, receipt.get("ledger_sha256")),
        ):
            if candidate is None:
                if receipt.get("ledger_sha256") is not None:
                    continue
            elif (
                candidate.is_symlink()
                or not candidate.is_file()
                or sha256_ref(_evidence_file_sha256(candidate)) != receipt.get("ledger_sha256")
            ):
                continue
            if (
                _preview(text, stage=stage, path=candidate, spec=spec, durations=durations)
                == receipt
            ):
                return candidate
        raise ValueError("SOURCE_TRUTH_PREVIEW_ORIGINAL_REPLAY_DRIFT")

    phase_inputs = []
    for row in history:
        if not isinstance(row, dict):
            raise ValueError("SOURCE_TRUTH_PREVIEW_HISTORY_INVALID")
        stage, receipt, binding = row.get("stage"), row.get("preview"), row.get("input")
        if (
            stage not in {"pre_entity_arbitration", "pre_correction_review"}
            or not isinstance(receipt, dict)
            or receipt.get("stage") != stage
            or not isinstance(binding, dict)
            or not isinstance(binding.get("path"), str)
            or binding.get("sha256") != receipt.get("input_srt_sha256")
        ):
            raise ValueError("SOURCE_TRUTH_PREVIEW_HISTORY_INVALID")
        ledger = row.get("ledger")
        if (receipt.get("ledger_sha256") is None and ledger is not None) or (
            receipt.get("ledger_sha256") is not None
            and (not isinstance(ledger, dict) or ledger.get("sha256") != receipt["ledger_sha256"])
        ):
            raise ValueError("SOURCE_TRUTH_PREVIEW_HISTORY_INVALID")
        original = row.get("original_ledger")
        if original is not None and (
            not isinstance(original, dict) or original.get("sha256") != receipt.get("ledger_sha256")
        ):
            raise ValueError("SOURCE_TRUTH_PREVIEW_HISTORY_INVALID")
        path = Path(binding["path"])
        if (
            path.is_symlink()
            or not path.is_file()
            or path.resolve().parent != root.resolve()
            or path.name != stage + "." + binding["sha256"].removeprefix("sha256:") + ".srt"
            or sha256_ref(_evidence_file_sha256(path)) != binding["sha256"]
        ):
            raise ValueError("SOURCE_TRUTH_PREVIEW_INPUT_DRIFT")
        text = path.read_bytes().decode("utf-8")
        # Historical input is useful only with its own complete receipt and
        # ledger replay. It never becomes the current phase's text or a PASS.
        original_ledger(text, stage, receipt)
        phase_inputs.append((stage, text))

    for stage in ("pre_entity_arbitration", "pre_correction_review"):
        old = previews.get(stage)
        if not isinstance(old, dict):
            raise ValueError("SOURCE_TRUTH_PREVIEW_RECEIPTS_MISSING")
        retained_inputs = []
        # A completed source checkpoint need not still use its pre-review
        # name reference. The ordinary partial emitter retained those exact
        # bytes before finalization; they are only an old receipt's preimage.
        for role in ('name_reference', 'language_witness'):
            digest = str(old.get('input_srt_sha256') or '').removeprefix('sha256:')
            if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                raise ValueError('SOURCE_TRUTH_PREVIEW_INPUT_DRIFT')
            path = out_root/'partial-source-text-input'/f'{role}.{digest}.srt'
            if path.exists() or path.is_symlink():
                if path.is_symlink() or not path.is_file():
                    raise ValueError('SOURCE_TRUTH_PREVIEW_INPUT_DRIFT')
                raw = path.read_bytes()
                if _sha(raw) != old['input_srt_sha256']:
                    raise ValueError('SOURCE_TRUTH_PREVIEW_INPUT_DRIFT')
                retained_inputs.append(raw.decode('utf-8'))
        texts = {
            text
            for text in [*inputs, *retained_inputs, *(t for s, t in phase_inputs if s == stage)]
            if text and _sha(text.encode()) == old.get("input_srt_sha256")
        }
        if len(texts) != 1:
            raise ValueError("SOURCE_TRUTH_PREVIEW_INPUT_DRIFT")
        text = texts.pop()
        matched = original_ledger(text, stage, old)
        refreshed = _preview(text, stage=stage, path=ledger_path, spec=spec, durations=durations)
        identity_keys = {"ledger_sha256", "source_truth_audit_sha256", "receipt_sha256"}
        if {k: v for k, v in old.items() if k not in identity_keys} != {
            k: v for k, v in refreshed.items() if k not in identity_keys
        }:
            if not added_terms:
                raise ValueError("SOURCE_TRUTH_CHANGED_PROTECTION_REVIEW_REQUIRED")
            changed_stages.append(stage)
        if refreshed != old:
            evidence = {
                "stage": stage,
                "preview": copy.deepcopy(old),
                "input": _frozen_bytes(root, stage, text.encode(), ".srt"),
                "ledger": _frozen_binding(root, "ledger", matched) if matched is not None else None,
                "original_ledger": (
                    {"path": str(matched), "sha256": sha256_ref(_evidence_file_sha256(matched))}
                    if matched is not None
                    else None
                ),
            }
            if evidence not in history:
                history.append(evidence)
        new[stage] = refreshed
    authority["source_truth_preview_history"] = history
    authority["source_truth_preview_receipts"] = new
    authority["source_truth_attempt"] = {
        "schema_version": "producer-source-truth-attempt.v1",
        "ledger": _frozen_binding(root, "ledger", ledger_path) if ledger_path is not None else None,
    }
    if changed_stages:
        authority["source_truth_protection_transition"] = {
            "schema_version": "source-truth-protection-transition.v1",
            "status": "CURRENT_REVIEW_REQUIRED", "release_authorized": False,
            "changed_stages": changed_stages,
            "current_srt_sha256": _sha(current_text.encode()),
            "ledger_sha256": sha256_ref(_evidence_file_sha256(ledger_path)),
        }
    return ledger_path
