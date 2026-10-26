"""Audited native account forks, without renumbering branch-local events.

v2 remains a single strict prefix ledger. A v3 snapshot retains that active
ledger and immutable original receipts; its accounting view qualifies event
identity by branch receipt hash. Byte-bound reconciliation retains the original
branches; later active forks are archived and composed by the ordinary join.
It confers no subtitle/release authority.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
from pathlib import Path

HISTORY_KEY = "account_history"
SCHEMA = "native-audio-budget.v3"
PLAN_SCHEMA = "native-audio-history-reconciliation-plan.v1"


def _error(message):
    from src.autoslice.native_audio_budget_receipt import _history_error
    _history_error("reconciliation " + message)


def active_snapshot(snapshot):
    history = snapshot.get(HISTORY_KEY)
    return history["active_budget"] if isinstance(history, Mapping) else snapshot


def history_identity(history):
    return {k: v for k, v in history.items() if k != "active_budget"}


def _original_history(history):
    """The first reconciliation is immutable across later active forks."""
    return {k: v for k, v in history.items()
            if k not in {"active_budget", "active_forks", "schema_version"}}


def cache_branches(history):
    """Namespace -> original cache namespace, shared by inventory and consumers."""
    if not history:
        return {}
    return {history["incoming"]["receipt_sha256"]: history["plan"]["incoming"]["sha256"],
            **{row["incoming"]["receipt_sha256"]: row["incoming"]["sha256"]
               for row in history.get("active_forks", [])}}


def history_cache_roots(account, history):
    return tuple(account / "history-cache" / digest[7:]
                 for digest in cache_branches(history).values())


def remap_incoming_attempt(attempt, original, history):
    """Map the original active ID into the union; never edit the old receipt."""
    old_h = original["budget"].get(HISTORY_KEY)
    if not old_h:
        return {**attempt, "history_branch_sha256": history["incoming"]["receipt_sha256"]}
    if attempt.get("history_branch_sha256") != old_h["canonical"]["receipt_sha256"]:
        return dict(attempt)
    for fork in history.get("active_forks", []):
        if fork["incoming"]["receipt_sha256"] == original["receipt_sha256"]:
            if attempt["attempt_id"] > fork["shared_prefix"]["attempts"]:
                return {**attempt, "history_branch_sha256": original["receipt_sha256"]}
            # A shared prefix can itself belong to an earlier retained fork.
            plain = {k: v for k, v in attempt.items() if k != "history_branch_sha256"}
            for row in pack_snapshot(history["active_budget"], history)["attempts"]:
                if {k: v for k, v in row.items() if k != "history_branch_sha256"} == plain:
                    return row
    return dict(attempt)


def pack_snapshot(active, history):
    """Derive totals, preserving the original IDs within both branch namespaces."""
    active = deepcopy(dict(active))
    active.setdefault("enforce_limits", False)
    h = deepcopy(history_identity(history))
    h["active_budget"] = deepcopy(active)
    ancestor = h["ancestor"]["budget"]
    incoming = h["incoming"]["budget"]
    rows = {}
    for name in ("attempts", "cache_hits", "refusals"):
        rows[name] = [
            {**row, "history_branch_sha256": h["canonical"]["receipt_sha256"]}
            for row in active[name]
        ] + [
            {**row, "history_branch_sha256": h["incoming"]["receipt_sha256"]}
            for row in incoming[name][len(ancestor[name]):]
        ]
        for fork in h.get("active_forks", []):
            rows[name].extend(
                {**row, "history_branch_sha256": fork["incoming"]["receipt_sha256"]}
                for row in fork["active_budget"][name][fork["shared_prefix"][name]:])
    attempts = rows["attempts"]
    windows = {}
    for row in attempts:
        key = row["start_ms"], row["end_ms"]
        windows[key] = windows.get(key, 0) + 1
    total = sum(row["duration_ms"] for row in attempts)
    sealed = sum(row["status"] != "DISPATCHED" for row in attempts)
    result = deepcopy(active)
    result.update(rows)
    result.update(
        revision=len(attempts) + sealed + len(rows["cache_hits"]) + len(rows["refusals"]),
        attempt_count=len(attempts), sealed_attempt_count=sealed,
        pending_attempt_count=len(attempts) - sealed,
        failed_attempt_count=sum(row["status"] in {"FAILED", "RESPONSE_REJECTED"} for row in attempts),
        cache_hit_count=len(rows["cache_hits"]), refusal_count=len(rows["refusals"]),
        distinct_window_count=len(windows), total_audio_ms=total,
        remaining_windows=max(0, active["max_windows"] - len(windows)),
        remaining_audio_ms=max(0, active["max_audio_ms"] - total),
        windows=[{"start_ms":a,"end_ms":b,"duration_ms":b-a,"attempt_count":n}
                 for (a,b),n in sorted(windows.items())],
    )
    result[HISTORY_KEY] = h
    return result


def validate_snapshot(snapshot):
    from src.autoslice.native_audio_budget_receipt import (
        _validate_budget_history, _validate_existing, _require_budget_history_extension, _providers,
    )
    h = snapshot.get(HISTORY_KEY)
    base_keys = {
        "schema_version", "canonical", "incoming", "ancestor", "plan", "plan_reference", "active_budget"
    }
    if (not isinstance(h, Mapping) or not (
            set(h) == base_keys and h["schema_version"] == "native-audio-account-history.v1"
            or set(h) == base_keys | {"active_forks"}
            and h["schema_version"] == "native-audio-account-history.v2")):
        _error("structure is invalid")
    source_sha = h["canonical"].get("source_media_sha256")
    for name in ("canonical", "incoming", "ancestor"):
        receipt = h[name]
        if not isinstance(receipt, Mapping) or HISTORY_KEY in receipt.get("budget", {}):
            _error("nested or invalid original branch")
        _validate_existing(receipt, source_media_sha256=source_sha)
        _validate_budget_history(receipt["budget"])
        if receipt.get("providers") != _providers(receipt["budget"]):
            _error("original provider summary changed")
    ancestor = h["ancestor"]
    # A common pending reservation whose branches sealed differently is not
    # enough evidence to reconcile outcomes. Keep it unresolved for explicit review.
    if ancestor["budget"]["pending_attempt_count"]:
        _error("shared pending ancestry needs outcome reconciliation")
    for name in ("canonical", "incoming"):
        _require_budget_history_extension(ancestor, h[name])
    active = h["active_budget"]
    _validate_budget_history(active)
    _require_budget_history_extension(h["canonical"], {"budget":active,"providers":_providers(active)})
    plan = h["plan"]
    if (not isinstance(plan, Mapping) or plan.get("schema_version") != PLAN_SCHEMA
            or plan.get("source_media_sha256") != source_sha
            or plan.get("mutation_authorized") is not False
            or plan.get("release_authorized") is not False):
        _error("plan scope is invalid")
    _, _, bound_plan = read_bound(h["plan_reference"])
    if bound_plan != plan:
        _error("plan bytes differ on cold restore")
    for name in ("canonical", "incoming", "ancestor"):
        # Raw JSON hashes are checked at entry. The receipt hash also binds
        # the embedded, unmodified original identity on every cold restoration.
        if plan.get(name, {}).get("receipt_sha256") != h[name]["receipt_sha256"]:
            _error("plan branch binding changed")
    _validate_active_forks(h, source_sha)
    if pack_snapshot(active, h) != dict(snapshot):
        _error("derived accounting changed")
    if active.get("enforce_limits", False) and (
        snapshot["distinct_window_count"] > active["max_windows"]
        or snapshot["total_audio_ms"] > active["max_audio_ms"]
    ):
        _error("combined history exceeds explicitly enforced limits")


def require_extension(old, new):
    """Containment of immutable branches plus strict extension of the active branch."""
    from src.autoslice.native_audio_budget_receipt import (
        _validate_budget_history, _require_budget_history_extension, _providers,
        BudgetReceiptPersistenceError,
    )
    before, after = old["budget"], new["budget"]
    _validate_budget_history(before)
    _validate_budget_history(after)
    if old.get("providers") != _providers(before) or new.get("providers") != _providers(after):
        _error("provider summary changed")
    old_h, new_h = before.get(HISTORY_KEY), after.get(HISTORY_KEY)
    if not new_h:
        _error("branch history was removed")
    if old_h:
        if (_original_history(old_h) != _original_history(new_h)
                or new_h.get("active_forks", [])[:len(old_h.get("active_forks", []))]
                != old_h.get("active_forks", [])):
            _error("original branch identity changed")
        # The old receipt may be either the writable branch or an original
        # active fork now retained by the union. Original IDs remain resolvable.
        for component in [new_h["active_budget"],
                          *(row["active_budget"] for row in new_h.get("active_forks", []))]:
            if _is_prefix({"budget": old_h["active_budget"], "providers": _providers(old_h["active_budget"])},
                          {"budget": component, "providers": _providers(component)}):
                return
        _error("prior attempt identity changed")
    for component in (new_h["active_budget"], new_h["incoming"]["budget"]):
        try:
            _require_budget_history_extension(old, {"budget":component,"providers":_providers(component)})
            return
        except BudgetReceiptPersistenceError:
            pass
    _error("receipt is not a retained branch prefix")


def read_bound(reference):
    from src.autoslice.native_audio_budget_receipt import _no_links
    if not isinstance(reference, Mapping) or not isinstance(reference.get("path"), str):
        _error("reference is invalid")
    path = Path(reference["path"])
    _no_links(path)
    if not path.is_absolute() or not path.is_file():
        _error("reference is missing")
    raw = path.read_bytes()
    if "sha256:" + hashlib.sha256(raw).hexdigest() != reference.get("sha256"):
        _error("reference bytes changed")
    value = json.loads(raw)
    if not isinstance(value, dict):
        _error("reference object is invalid")
    return path, raw, value


def _shared_prefix_counts(canonical, incoming):
    """Compare complete event prefixes, never deduplicate by window or text."""
    h = canonical[HISTORY_KEY]
    active = incoming[HISTORY_KEY]["active_budget"]
    bases = [h["active_budget"], *(row["active_budget"] for row in h.get("active_forks", []))]
    for key in ("source_media", "max_windows", "max_window_ms", "max_audio_ms"):
        if active[key] != h["active_budget"][key]:
            _error("source or limits changed across active forks")
    if active.get("enforce_limits", False) != h["active_budget"].get("enforce_limits", False):
        _error("enforcement mode changed across active forks")
    result = {}
    for name in ("attempts", "cache_hits", "refusals"):
        count = 0
        best = None
        for base in bases:
            shared = 0
            for before, after in zip(base[name], active[name]):
                if before != after:
                    break
                shared += 1
            if best is None or shared > count:
                count, best = shared, base
        if name == "attempts":
            # A pending reservation is one call whose outcome is unknown, not
            # proof of two distinct calls. Neither split nor refund it.
            if any(row["status"] == "DISPATCHED" for row in active[name][:count]):
                _error("shared pending ancestry needs outcome reconciliation")
            if count < min(len(best[name]), len(active[name])):
                keys = {"status", "reason_code", "http_status"}
                before, after = best[name][count], active[name][count]
                if {k: v for k, v in before.items() if k not in keys} == {
                        k: v for k, v in after.items() if k not in keys}:
                    _error("same active attempt has conflicting outcomes")
        result[name] = count
    return result


def _validate_active_forks(history, source_sha):
    from src.autoslice.native_audio_budget_receipt import _validate_existing, _validate_budget_history, _providers
    forks = history.get("active_forks", [])
    if not isinstance(forks, list):
        _error("active forks are invalid")
    identities = {history[name]["receipt_sha256"] for name in ("canonical", "incoming", "ancestor")}
    for index, fork in enumerate(forks):
        if not isinstance(fork, Mapping) or set(fork) != {"canonical", "incoming", "shared_prefix", "active_budget"}:
            _error("active fork fields are invalid")
        originals = {}
        for name in ("canonical", "incoming"):
            _, _, original = read_bound(fork[name])
            _validate_existing(original, source_media_sha256=source_sha)
            _validate_budget_history(original["budget"])
            if (original["receipt_sha256"] != fork[name].get("receipt_sha256")
                    or original.get("providers") != _providers(original["budget"])):
                _error("active fork receipt identity changed")
            originals[name] = original["budget"]
        ch, ih = (originals[name].get(HISTORY_KEY) for name in ("canonical", "incoming"))
        if (not ch or not ih or _original_history(ch) != _original_history(history)
                or _original_history(ih) != _original_history(history)
                or ch.get("active_forks", []) != forks[:index]
                or forks[:len(ih.get("active_forks", []))] != ih.get("active_forks", [])
                or len(ih.get("active_forks", [])) > index):
            _error("original branch identity changed")
        if fork["active_budget"] != ih["active_budget"]:
            _error("active fork snapshot changed")
        if (fork["shared_prefix"] != _shared_prefix_counts(originals["canonical"], originals["incoming"])
                or any(type(value) is not int for value in fork["shared_prefix"].values())):
            _error("active fork shared prefix changed")
        if not _is_prefix({"budget": ch["active_budget"], "providers": _providers(ch["active_budget"])},
                          {"budget": history["active_budget"], "providers": _providers(history["active_budget"])}):
            _error("active canonical history changed")
        identity = fork["incoming"]["receipt_sha256"]
        if identity in identities:
            _error("duplicate active fork identity")
        identities.add(identity)


def reconcile_active_fork(*, account, current, incoming, receipt_path, incoming_path):
    """Advance active history or append a byte-bound active branch exactly once.

    Earlier reconciliation receipts and plans remain immutable. Both original
    v3 receipts are archived verbatim; only the new derived revision is signed.
    The writable branch keeps its local IDs, while retained suffixes use their
    original receipt SHA as namespace. Cache namespaces use original raw SHA.
    """
    from src.autoslice.native_audio_budget_receipt import _providers, receipt_sha256, _no_links
    from src.autoslice.package_import import atomic_write_bytes
    if any(value.get("providers") != _providers(value["budget"]) for value in (current, incoming)):
        _error("provider summary changed")
    ch, ih = (value["budget"].get(HISTORY_KEY) for value in (current, incoming))
    if (not ch or not ih or _original_history(ch) != _original_history(ih)
            or ch.get("active_forks", [])[:len(ih.get("active_forks", []))] != ih.get("active_forks", [])
            or len(ih.get("active_forks", [])) > len(ch.get("active_forks", []))):
        _error("original branch identity changed")
    # Global revision includes frozen branches, so it cannot order two active
    # prefixes. A parent may extend active while lacking later frozen forks.
    # Preserve those forks and advance active normally, without inventing a fork.
    if _is_prefix({"budget": ch["active_budget"], "providers": _providers(ch["active_budget"])},
                  {"budget": ih["active_budget"], "providers": _providers(ih["active_budget"])}):
        snapshot = pack_snapshot(ih["active_budget"], ch)
        validate_snapshot(snapshot)
        payload = {"schema_version": SCHEMA, "source_media_sha256": current["source_media_sha256"],
                   "revision": snapshot["revision"], "providers": _providers(snapshot), "budget": snapshot}
        payload["receipt_sha256"] = receipt_sha256(payload)
        return payload
    counts = _shared_prefix_counts(current["budget"], incoming["budget"])
    record = {"shared_prefix": counts, "active_budget": deepcopy(ih["active_budget"])}
    archives = []
    for name, value, path in (("canonical", current, receipt_path), ("incoming", incoming, incoming_path)):
        _no_links(path)
        raw = path.read_bytes()
        if json.loads(raw) != value:
            _error("active fork changed before archive")
        digest = _sha_prefixed(raw)
        target = account / "parents" / (digest[7:] + ".json")
        record[name] = {"path": str(target), "sha256": digest, "receipt_sha256": value["receipt_sha256"]}
        archives.append((target, raw))
    history = deepcopy(ch)
    history["schema_version"] = "native-audio-account-history.v2"
    history.setdefault("active_forks", []).append(record)
    snapshot = pack_snapshot(ch["active_budget"], history)
    payload = {"schema_version": SCHEMA, "source_media_sha256": current["source_media_sha256"],
               "revision": snapshot["revision"], "providers": _providers(snapshot), "budget": snapshot}
    payload["receipt_sha256"] = receipt_sha256(payload)
    created = []
    try:
        for path, raw in archives:
            _no_links(path)
            if path.exists():
                if path.read_bytes() != raw:
                    _error("active archive bytes changed")
            else:
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                atomic_write_bytes(path, raw)
                created.append(path)
        validate_snapshot(snapshot)
    except (OSError, ValueError, KeyError, TypeError):
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise
    return payload


def build_reconciliation(*, reference, current, incoming, account, manifest, inventory,
                         cid, source_sha, parent):
    """Validate the explicit migration against the occupied account before writes."""
    from src.autoslice.native_audio_budget_receipt import _providers, receipt_sha256
    from src.autoslice.prepared_native_audio_recovery import _parent_inputs
    _, _, plan = read_bound(reference)
    if (plan.get("schema_version") != PLAN_SCHEMA
            or plan.get("candidate_id") != cid
            or plan.get("source_media_sha256") != source_sha):
        _error("plan source or candidate differs")
    parsed = {}
    for name in ("canonical", "incoming", "ancestor"):
        path, raw, parsed[name] = read_bound(plan.get(name))
        if parsed[name].get("receipt_sha256") != plan[name].get("receipt_sha256"):
            _error("raw receipt identity differs")
        if name == "canonical" and parsed[name] != current:
            _error("occupied canonical differs from plan")
        if name == "incoming" and (parsed[name] != incoming or path != parent/"native-audio-budget.json"):
            _error("incoming parent differs from plan")
    ancestor_sha = plan["ancestor"]["sha256"]
    archive = account / "parents" / (ancestor_sha[7:] + ".json")
    _, _, archived = read_bound({"path":str(archive), "sha256":ancestor_sha})
    if archived != parsed["ancestor"] or not any(
        row.get("budget_sha256") == ancestor_sha for row in manifest["parents"]
    ):
        _error("common ancestor is not archived account ancestry")
    ancestor_source = Path(parsed["ancestor"]["budget"]["source_media"])
    # Validate the original observations of the common ancestor in both
    # branches. Same geometry alone is never a shared-dispatch witness.
    ancestor_root = Path(plan["ancestor"]["path"]).parent
    if ancestor_root.name == "parents":
        matches = [r for r in manifest["parents"] if r.get("budget_sha256") == ancestor_sha]
        ancestor_root = Path(matches[0]["path"])
    original_ancestor, ancestor_inventory = _parent_inputs(
        parent=ancestor_root,cid=cid,source=ancestor_source,source_sha=source_sha)
    if original_ancestor != parsed["ancestor"]:
        _error("original common ancestor drifted")
    for relative, digest in ancestor_inventory.items():
        if relative == "native-audio-budget.json":
            continue
        read_bound({"path":str(account/relative),"sha256":digest}) if relative.endswith(".json") else _check_file(account/relative,digest)
        if inventory.get(relative) != digest:
            _error("shared ancestor observation differs")
    history = {
        "schema_version":"native-audio-account-history.v1",
        **parsed, "plan":plan, "plan_reference":dict(reference),
    }
    snapshot = pack_snapshot(current["budget"],history)
    validate_snapshot(snapshot)
    payload = {"schema_version":SCHEMA,"source_media_sha256":source_sha,
               "revision":snapshot["revision"],"providers":_providers(snapshot),"budget":snapshot}
    payload["receipt_sha256"] = receipt_sha256(payload)
    return payload


def _check_file(path, digest):
    from src.autoslice.native_audio_budget_receipt import _no_links
    from .evidence_identity import file_sha256
    _no_links(path)
    if not path.is_file() or "sha256:" + file_sha256(path) != digest:
        _error("cache bytes changed")


DERIVATION_SCHEMA = "native-audio-history-reconciliation-derivation.v1"


def _sha_prefixed(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _is_prefix(old, new) -> bool:
    from src.autoslice.native_audio_budget_receipt import (
        BudgetReceiptPersistenceError, _require_budget_history_extension,
    )
    try:
        _require_budget_history_extension(old, new)
    except (BudgetReceiptPersistenceError, KeyError, TypeError, ValueError):
        return False
    return True


def discover_registered_ancestor(*, account: Path, manifest, current, incoming,
                                 receipt_path: Path, incoming_path: Path,
                                 cid: str, source_sha: str):
    """Derive the fork plan from the account's own registered parent archives.

    The normal entry has no manual plan channel. The only admissible common
    ancestor is a parent the account itself registered (``parent.json``) and
    archived byte-for-byte under ``parents/``; it must be sealed and a strict
    history prefix of both the occupied canonical receipt and the incoming
    parent. Anything else returns ``None`` so the caller keeps its original
    rejection. No account file is written here.
    """
    if not isinstance(manifest, Mapping) or not isinstance(manifest.get("parents"), list):
        return None
    canonical_raw = receipt_path.read_bytes()
    incoming_raw = incoming_path.read_bytes()
    if json.loads(canonical_raw) != current or json.loads(incoming_raw) != incoming:
        return None
    if HISTORY_KEY in current.get("budget", {}) or HISTORY_KEY in incoming.get("budget", {}):
        return None
    candidates = {}
    for row in manifest["parents"]:
        if isinstance(row, Mapping) and row.get('path') == str(account):
            # Self snapshots bind cache qualification, not an independently
            # retained source directory for the original common-ancestor proof.
            continue
        digest = row.get("budget_sha256") if isinstance(row, Mapping) else None
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            continue
        archive = account / "parents" / (digest[7:] + ".json")
        try:
            _, _, archived = read_bound({"path": str(archive), "sha256": digest})
        except (OSError, ValueError):
            continue
        budget = archived.get("budget") if isinstance(archived, Mapping) else None
        if (not isinstance(budget, Mapping) or HISTORY_KEY in budget
                or budget.get("pending_attempt_count") != 0
                or archived == current or archived == incoming
                or not _is_prefix(archived, current) or not _is_prefix(archived, incoming)):
            continue
        candidates[digest] = (archive, archived, row)
    if not candidates:
        return None
    ordered = sorted(candidates.items(), key=lambda item: (
        item[1][1]["budget"]["attempt_count"], item[1][1]["revision"]))
    digest, (archive, ancestor, row) = ordered[-1]
    # Registered ancestors of one history are nested prefixes. Two distinct
    # maximal ancestors mean the registration itself is ambiguous.
    if any(other != digest and not _is_prefix(value[1], ancestor)
           for other, value in candidates.items()):
        return None
    plan = {
        "schema_version": PLAN_SCHEMA,
        "candidate_id": cid,
        "source_media_sha256": source_sha,
        "canonical": {"path": str(account / "parents" / (_sha_prefixed(canonical_raw)[7:] + ".json")),
                      "sha256": _sha_prefixed(canonical_raw),
                      "receipt_sha256": current.get("receipt_sha256")},
        "incoming": {"path": str(incoming_path), "sha256": _sha_prefixed(incoming_raw),
                     "receipt_sha256": incoming.get("receipt_sha256")},
        "ancestor": {"path": str(archive), "sha256": digest,
                     "receipt_sha256": ancestor.get("receipt_sha256")},
        "derivation": {"schema_version": DERIVATION_SCHEMA,
                       "method": "REGISTERED_PARENT_ARCHIVE_COMMON_SEALED_ANCESTOR",
                       "registered_parent_path": row.get("path"),
                       "registered_parent_budget_sha256": digest},
        "mutation_authorized": False,
        "release_authorized": False,
    }
    return {"plan": plan, "canonical_raw": canonical_raw}


def stage_discovered_plan(*, account: Path, discovered) -> tuple[dict, list[Path]]:
    """Persist the derived plan and the canonical original, create-only.

    Returns the plan reference and the paths this call newly created, so a
    caller whose later proof fails can withdraw exactly those files.
    """
    from src.autoslice.native_audio_budget_receipt import _no_links
    from src.autoslice.package_import import atomic_write_bytes
    created = []
    canonical_raw = discovered["canonical_raw"]
    plan = discovered["plan"]
    targets = [(Path(plan["canonical"]["path"]), canonical_raw)]
    plan_raw = (json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    plan_path = account / "history-plans" / (hashlib.sha256(plan_raw).hexdigest() + ".json")
    targets.append((plan_path, plan_raw))
    for path, raw in targets:
        _no_links(path)
        if path.exists():
            if path.read_bytes() != raw:
                _error("derived plan target differs")
            continue
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write_bytes(path, raw)
        created.append(path)
    return {"path": str(plan_path), "sha256": _sha_prefixed(plan_raw)}, created
