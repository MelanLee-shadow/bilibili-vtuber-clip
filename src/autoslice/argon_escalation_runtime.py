"""Opt-in ordinary runtime admission for the reviewed Argon engine.

At most one new inference reservation per invocation. A route that returned a
definitive zero-inference refusal (complete 429, or HTTP 400 FAILED_PRECONDITION
with only the provider error object) cannot serve this request: it keeps a
create-only marker and the ladder moves AGY -> free key -> paid key (COST-01).
A lane already terminal before this invocation is passed in the same call; a
refusal received now stops this call. Key tiers whose provider tier table
excludes Argon are never selected (``argon_model_capability.TIER_INFERENCE``).
INVALID_ARGUMENT/body errors, auth/permission failures and unknown outcomes
never rotate. Secrets are loaded lazily through the normal loader, never put
in an artifact.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import time

import hashlib
import hmac
import os
from pathlib import Path
from typing import Callable, Mapping

from . import argon_escalation_storage as store
from . import gemini_backup_policy as paid
from .argon_escalation_contract import (
    AudioSpec, KeyPolicy, ROUTE_UNSERVABLE, classify_provider_rejection, require, sha,
)
from . import argon_model_capability as capability
from .argon_escalation_transport import ArgonHttpTransport
from .argon_escalation_witness import ArgonEscalationWitness
from .provider_slots import runtime_provider_slot, ProviderSlotBusy, provider_wait_for_call

_retrieval_deadline = ContextVar('argon_inrun_retrieval_deadline', default=None)


@contextmanager
def bounded_retrieval(seconds):
    token = _retrieval_deadline.set(time.monotonic() + max(0, seconds))
    try:
        yield
    finally:
        _retrieval_deadline.reset(token)


def _remaining_retrieval():
    deadline = _retrieval_deadline.get()
    return None if deadline is None else max(0.001, deadline - time.monotonic())

FREE_NAMES = ("GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3")
UNSERVABLE_SCHEMA = "argon-route-unservable.v1"
NEXT_ROUTE_STATUSES = frozenset({"FREE_QUOTA_NEXT_ROUTE_PENDING", "ROUTE_UNSERVABLE_NEXT_ROUTE_PENDING"})


def _dispatched_route(snapshot):
    for event in snapshot["events"]:
        if event["state"] == "SUBMIT_DISPATCHED":
            return event["metadata"].get("route") or {}
    return None


def lane_rejection(engine, task, manifest, inner):
    """The lane's retained terminal API refusal, read only from its ledgers.

    Returns None unless the lane's last dispatched API request FAILED with a
    complete retained response; otherwise the shared classification, the
    mode, the dispatched route and the raw response digest.
    """
    rows = []
    for mode in ("batch", "realtime"):
        ledger = engine._ledger(task, manifest, mode, engine._body(task.name, inner, mode))
        if not ledger.path.exists():
            continue
        snap = engine._read_snapshot(ledger)
        route = _dispatched_route(snap)
        if route is None:
            continue
        rows.append((mode, snap, route))
    if not rows:
        return None
    mode, snap, route = rows[-1]
    event = snap["events"][-1]
    if snap["state"] != "FAILED" or event["reason_code"] != "SUBMIT_HTTP_REJECTED":
        return None
    meta = event["metadata"]
    raw = store.get_blob(task, meta["response_sha256"])
    verdict = classify_provider_rejection(meta.get("http_status"), raw,
                                          response_complete=meta.get("response_complete", True))
    return {"mode": mode, "route": dict(route), "response_sha256": meta["response_sha256"],
            "http_status": meta.get("http_status"), "classification": verdict,
            "ledger_request_id": snap["request_id"],
            "realtime_tried": any(m == "realtime" for m, _, _ in rows)}


def load_credentials() -> dict[str, str]:
    # The ordinary launcher owns this loader. It is called only after opt-in,
    # source/trigger validation and provider permission. Tests inject a fixture.
    from scripts.session_autoslice import load_gemini_credentials

    loaded = load_gemini_credentials()
    return {
        name: os.environ.get(name) or loaded.get(name, "")
        for name in (*FREE_NAMES, "GEMINI_KEY_BACKUP")
    }


def _has_prior_api_dispatch(root, spec):
    """Only an actual retained API reservation permits legacy API-only recovery."""
    for handle_path in root.glob("attempt-*/handle.json"):
        engine = ArgonEscalationWitness(handle_path.parent / "engine", read_only=True)
        handle = store.get_json(handle_path)
        task, manifest, inner = engine._load(handle["task_id"], spec)
        for mode in ("batch", "realtime"):
            ledger = engine._ledger(task, manifest, mode, engine._body(task.name, inner, mode))
            if ledger.path.exists() and engine._dispatch_metadata(ledger.snapshot()):
                return True
    return False


class ArgonRuntime:
    def __init__(
        self,
        *,
        runtime_root: Path,
        config: Mapping,
        credential_loader: Callable | None = None,
        transport_factory: Callable | None = None,
    ):
        self.base = Path(runtime_root).absolute()
        self.config = dict(config)
        self.loader = credential_loader or load_credentials
        self.transport_factory = transport_factory or ArgonHttpTransport

    def advance(self, *, root: Path, spec: AudioSpec) -> dict:
        store.directory(root)
        with store.locked(root):
            # Bounded: one pass per configured route plus the paid route. Only
            # an iteration that reserved no new inference may continue, so a
            # refusal received in this call still ends this call.
            outcome, dispatches = None, 0
            for _ in range(len(FREE_NAMES) + 2):
                outcome = self._advance_locked(root, spec)
                dispatches += outcome["dispatches_this_advance"]
                if (outcome["result"]["status"] not in NEXT_ROUTE_STATUSES
                        or outcome["dispatches_this_advance"]):
                    break
            return {**outcome, "dispatches_this_advance": dispatches}

    def _advance_locked(self, root: Path, spec: AudioSpec) -> dict:
        if True:
            # Only the normal runtime config owns production scope. A planner
            # cannot lift a comparison grant, and cold recovery cannot turn an
            # old API task into a new AGY attempt.
            policy_path = root / "routing-policy.json"
            comparison = bool(self.config.get("comparison_plan"))
            identity = {k: self.config.get(k) for k in
                        ("comparison_plan", "comparison_grant", "comparison_case_ids")}
            if policy_path.exists():
                routing = store.get_json(policy_path)
                require(routing["comparison"] == comparison and routing["comparison_identity"] == identity,
                        "ARGON_ROUTING_POLICY_DRIFT")
            else:
                routing = {"schema_version": ("argon-routing-policy.v1"
                    if _has_prior_api_dispatch(root, spec) else "argon-routing-policy.v2"),
                    "comparison": comparison, "comparison_identity": identity,
                    "scope": "comparison" if comparison else (
                        "legacy_api_only" if _has_prior_api_dispatch(root, spec) else "production")}
                store.put_json(policy_path, routing)
            require(routing["scope"] == ("comparison" if comparison else "production")
                    or (not comparison and routing["scope"] == "legacy_api_only"
                        and _has_prior_api_dispatch(root, spec)), "ARGON_ROUTING_POLICY_DRIFT")
            seal = {"schema_version": "argon-routing-policy-seal.v1",
                    "routing_policy_sha256": sha(store.read(policy_path))}
            seal_path = root / "routing-policy-seal.json"
            if seal_path.exists():
                require(store.get_json(seal_path) == seal, "ARGON_ROUTING_POLICY_DRIFT")
            else:
                store.put_json(seal_path, seal)
            production = routing["scope"] == "production"
            # Pin slots by a private, salted one-way identity. A changed or
            # reordered secret may not query somebody else's original job.
            creds = None

            def credentials():
                nonlocal creds
                if creds is None:
                    try:
                        raw = self.loader()
                    except Exception:
                        raise ValueError("ARGON_CREDENTIAL_LOADER_UNAVAILABLE") from None
                    values = list(dict.fromkeys(raw.get(n) for n in FREE_NAMES if raw.get(n)))
                    require(1 <= len(values) <= 3, "ARGON_FREE_CREDENTIALS_NOT_CONFIGURED")
                    creds = (values, raw.get("GEMINI_KEY_BACKUP") or None)
                    chain_path = root / "free-chain-identity.json"
                    if chain_path.exists():
                        chain = store.get_json(chain_path)
                    else:
                        chain = {"salt": os.urandom(32).hex()}
                    identities = [
                        hmac.new(
                            bytes.fromhex(chain["salt"]), v.encode(), hashlib.sha256
                        ).hexdigest()
                        for v in values
                    ]
                    if "keys" in chain:
                        require(identities == chain["keys"], "ARGON_FREE_CHAIN_IDENTITY_DRIFT")
                    else:
                        store.put_json(chain_path, {**chain, "keys": identities})
                return creds

            def key_for(route):
                free, backup = credentials()
                require(len(free) == route["configured_key_count"], "ARGON_CREDENTIAL_COUNT_DRIFT")
                key = (
                    backup
                    if route["accepted_key_tier"] == "paid_backup"
                    else free[route["accepted_key_ordinal"] - 1]
                )
                require(isinstance(key, str) and bool(key), "ARGON_CREDENTIAL_UNAVAILABLE")
                identity_file = root / (
                    "credential-" + str(route["accepted_key_ordinal"]) + ".json"
                )
                if identity_file.exists():
                    identity = store.get_json(identity_file)
                else:
                    identity = {"salt": os.urandom(32).hex()}
                signature = hmac.new(
                    bytes.fromhex(identity["salt"]), key.encode(), hashlib.sha256
                ).hexdigest()
                if "binding" in identity:
                    require(
                        hmac.compare_digest(identity["binding"], signature),
                        "ARGON_CREDENTIAL_IDENTITY_DRIFT",
                    )
                else:
                    store.put_json(identity_file, {**identity, "binding": signature})
                return key

            class Slotted:
                def request(inner, method, path, body, *, route, timeout_s):
                    # Credential drift is checked BEFORE slot acquisition/HTTP.
                    key = key_for(route)
                    transport = self.transport_factory(lambda _: key)
                    remaining = _remaining_retrieval()
                    if remaining is not None:
                        require(method == 'GET', 'ARGON_INRUN_RETRIEVAL_READ_ONLY')
                        timeout_s = min(timeout_s, remaining)
                    return transport.request(method, path, body, route=route, timeout_s=timeout_s)

            # Per-route engines keep all immutable attempts. Switching is allowed
            # only after a definitive zero-inference refusal (complete 429 or
            # FAILED_PRECONDITION), never an unknown/pending job or auth/body error.
            index = 1
            prior_attempts = []
            consumed_routes = []
            while True:
                lane_dir = root / f"attempt-{index}"
                quota_marker = lane_dir / "quota-rejected.json"
                unservable_marker = lane_dir / "route-unservable.json"
                if not store.route_refused(lane_dir):  # Shared with consumer lane location.
                    break
                marker = store.get_json(quota_marker if quota_marker.exists() else unservable_marker)
                engine = ArgonEscalationWitness(lane_dir / "engine")
                task, manifest, body = engine._load(marker["task_id"], spec)
                rejection = lane_rejection(engine, task, manifest, body)
                require(rejection is not None
                        and rejection["classification"]["class"] == ROUTE_UNSERVABLE
                        and rejection["response_sha256"] == marker["response_sha256"],
                        "ARGON_QUOTA_HISTORY_INVALID" if quota_marker.exists()
                        else "ARGON_ROUTE_UNSERVABLE_HISTORY_INVALID")
                if quota_marker.exists():
                    require(rejection["http_status"] == 429 and rejection["mode"] == "batch",
                            "ARGON_QUOTA_HISTORY_INVALID")
                else:
                    require(marker == {**marker, "schema_version": UNSERVABLE_SCHEMA,
                                       "route": rejection["route"], "mode": rejection["mode"],
                                       "classification": rejection["classification"]},
                            "ARGON_ROUTE_UNSERVABLE_HISTORY_INVALID")
                ledger = engine._ledger(task, manifest, rejection["mode"],
                                        engine._body(task.name, body, rejection["mode"]))
                snap = ledger.snapshot()
                consumed_routes.append(rejection["route"])
                prior_attempts.append(
                    {
                        "engine_root": str(engine.root),
                        "task_id": task.name,
                        "ledger": snap,
                        "response_sha256": marker["response_sha256"],
                        "route": rejection["route"],
                        "classification": rejection["classification"],
                    }
                )
                index += 1
            lane = root / f"attempt-{index}"
            store.directory(lane)
            engine = ArgonEscalationWitness(lane / "engine")
            handle_path = lane / "handle.json"
            if handle_path.exists():
                handle = store.get_json(handle_path)
            else:
                handle = engine.prepare(spec)
                store.put_json(handle_path, handle)
            task_id = handle["task_id"]
            task, manifest, inner_body = engine._load(task_id, spec)
            before_accounting = engine._accounting(task, manifest, inner_body)
            batch_ledger = engine._ledger(task, manifest, "batch", engine._body(task_id, inner_body, "batch"))

            def lane_current_route():
                return _dispatched_route(batch_ledger.snapshot())

            # The same-key realtime method is only a route on a tier that may
            # run Argon inference at all (production tier table).
            lane_route = lane_current_route()
            realtime_allowed = bool(self.config.get("allow_realtime_fallback", False)) and not (
                production and lane_route is not None
                and not capability.tier_available(lane_route.get("accepted_key_tier")))
            if production and index == 1 and batch_ledger.snapshot()["state"] == "PREPARED":
                result = engine.advance_agy(task_id, expected=spec,
                    request_timeout_s=self.config.get("request_timeout_s", 60))
                # Only retained definite failure/unavailability permits API.
                # UNKNOWN, slot refusal and a real uncertain hearing all stop.
                definitive_fallback = (
                    result["status"] == "FAILED_PRE_DISPATCH"
                    and result["reason"] in {"AGY_BINARY_ABSENT", "AGY_MODEL_UNSUPPORTED"}
                    or result["status"] == "FAILED"
                    and result["reason"] in {"AGY_QUOTA_EXHAUSTED", "AGY_RATE_LIMITED"})
                if definitive_fallback and result["status"] == "FAILED_PRE_DISPATCH":
                    from .argon_escalation_agy import validate_capability
                    native_snapshot = engine._ledger(task, manifest, "agy",
                        engine._body(task_id, inner_body, "agy")).snapshot()
                    proof = validate_capability(task, inner_body, expected_sha=
                        native_snapshot["events"][-1]["metadata"].get("capability_receipt_sha256"))
                    require(proof["status"] == "UNSUPPORTED" and proof["reason"] == result["reason"],
                            "ARGON_AGY_FALLBACK_CAPABILITY_INVALID")
                if not definitive_fallback:
                    return {"engine_root": str(engine.root), "result": result,
                        "prior_key_attempts": prior_attempts,
                        "dispatches_this_advance": max(0, result["accounting"]["inference_reservations"]
                                                       - before_accounting["inference_reservations"]),
                        "new_usable_observations_this_advance": int(result["status"] == "OBSERVED"
                            and not any(h["state"] == "COMPLETED" for h in before_accounting["histories"]))}

            def preflight_api(route):
                if routing["comparison"]:
                    return
                key = key_for(route)
                current_task, current_manifest, body = engine._load(task_id, spec)
                rt = engine._ledger(current_task, current_manifest, "realtime", body)
                mode = "realtime" if rt.path.exists() and rt.snapshot()["state"] == "PREPARED" else "batch"
                binding = capability.api_binding(current_task,
                    sha(engine._body(task_id, body, mode)), mode, route)
                transport = self.transport_factory(lambda _: key)
                receipt = capability.ensure(current_task, "api-" + mode, binding,
                    lambda: capability.api_probe(transport, self.config.get("request_timeout_s", 60)))
                if receipt["status"] != "SUPPORTED":
                    raise capability.CapabilityBlocked(receipt)

            class Policy:
                def capability_receipt(inner, mode, route, request_sha256):
                    if routing["comparison"]:
                        return None
                    receipt = capability.validated_api_receipt(task, request_sha256, mode, route)
                    require(receipt["status"] == "SUPPORTED", "ARGON_API_CAPABILITY_REQUIRED")
                    return receipt["receipt_sha256"]

                def select(inner, item_key):
                    if routing["comparison"]:
                        from .argon_regression_comparison import SubmissionBudget
                        require(index == 1 and not self.config.get("allow_paid")
                                and not self.config.get("allow_realtime_fallback"), "COMPARISON_ROUTE_FORBIDDEN")
                        _, _, body = engine._load(task_id, spec)
                        SubmissionBudget(Path(routing["comparison_identity"]["comparison_plan"]),
                            Path(routing["comparison_identity"]["comparison_grant"]),
                            allowed_case_ids=routing["comparison_identity"]["comparison_case_ids"]).reserve(spec, item_key, body)
                    free, backup = credentials()
                    n = len(free)
                    require(index <= n + 1, "ARGON_ROUTE_HISTORY_INVALID")
                    current = lane_current_route()
                    if current is not None:
                        # Same lane, next method (batch -> realtime): same key.
                        if current["accepted_key_tier"] != "paid_backup":
                            route = KeyPolicy(n, tuple(i for i in range(1, n + 1)
                                if i != current["accepted_key_ordinal"])).select(item_key)
                            key_for(route)
                            preflight_api(route)
                            return route
                    used = {r["accepted_key_ordinal"] for r in consumed_routes
                            if r.get("accepted_key_tier") == "free"}
                    require(not any(r.get("accepted_key_tier") == "paid_backup"
                                    for r in consumed_routes), "ARGON_ALL_ROUTES_UNSERVABLE")
                    # A free tier the provider excludes from this model is not a
                    # route; models.get visibility is no inference entitlement.
                    tier_closed = (set(range(1, n + 1)) if production
                                   and not capability.tier_available("free") else set())
                    unavailable = tuple(sorted(used | tier_closed))
                    if current is None and len(unavailable) < n:
                        route = KeyPolicy(n, unavailable).select(item_key)
                        key_for(route)  # Pin the chosen actual credential before reservation.
                        preflight_api(route)
                        return route
                    # Proven free-chain conditions: the provider tier table, or
                    # definitive zero-inference FAILED_PRECONDITION refusals.
                    # An all-429 chain keeps the retained strikes gate (unchanged).
                    precondition_refused = any(
                        a["classification"]["basis"] == "ZERO_INFERENCE_FAILED_PRECONDITION"
                        for a in prior_attempts)
                    basis = ("FREE_TIER_UNAVAILABLE_FOR_MODEL" if tier_closed
                             else "FREE_CHAIN_DEFINITIVE_REJECTIONS")
                    unservable_gate = production and (bool(tier_closed) or precondition_refused)
                    require(
                        os.environ.get("AUTOSLICE_BASE") == str(self.base),
                        "ARGON_PAID_RUNTIME_DRIFT",
                    )
                    round_path = root / "free-round.json"
                    if not tier_closed and not round_path.exists():
                        require(
                            not (root / "free-round.intent.json").exists(),
                            "ARGON_FREE_ROUND_RECORDING_UNKNOWN",
                        )
                        store.put_json(root / "free-round.intent.json", {"item_key": item_key})
                        prior_strikes = paid.free_chain_strikes(item_key)
                        strikes = paid.record_free_chain_failure(item_key)
                        require(
                            paid.free_chain_strikes(item_key) >= strikes,
                            "ARGON_POLICY_NOT_PERSISTED",
                        )
                        store.put_json(
                            round_path,
                            {
                                "item_key": item_key,
                                "prior_strikes": prior_strikes,
                                "strikes": strikes,
                            },
                        )
                    require(self.config.get("allow_paid") is True, "ARGON_PAID_NOT_AUTHORIZED")
                    require(
                        os.environ.get("AUTOSLICE_BASE") == str(self.base),
                        "ARGON_PAID_RUNTIME_DRIFT",
                    )
                    # A metadata failure cannot create a paid-use record.
                    preflight_api({"configured_key_count": n, "accepted_key_ordinal": n + 1,
                                   "accepted_key_tier": "paid_backup"})
                    task, manifest, inner_body = engine._load(task_id, spec)
                    realtime = engine._ledger(task, manifest, "realtime", inner_body)
                    mode = (
                        "realtime"
                        if realtime.path.exists() and realtime.snapshot()["state"] == "PREPARED"
                        else "batch"
                    )
                    policy_path = lane / ("paid-policy-" + mode + ".json")
                    with store.locked(self.base / "state" / "argon-paid-admission"):
                        if policy_path.exists():
                            stamp = store.get_json(policy_path)
                        else:
                            if unservable_gate:
                                # COST-01: free chain proven unable to serve ->
                                # paid in this round, every use ledgered, no cooldown.
                                allowed, reason = paid.unservable_free_chain_paid_gate(
                                    item_key, basis=basis, key_configured=bool(backup),
                                    uncapped=True,
                                )
                            else:
                                allowed, reason = paid.paid_attempt_allowed(
                                    item_key,
                                    key_configured=bool(backup),
                                    prior_strikes=store.get_json(round_path)["prior_strikes"],
                                    uncapped=production,
                                )
                            require(allowed, "ARGON_PAID_POLICY_" + reason)
                            require(
                                not (lane / ("paid-policy-" + mode + ".intent.json")).exists(),
                                "ARGON_PAID_RECORDING_UNKNOWN",
                            )
                            evidence = {"item_key": item_key, "basis": basis if unservable_gate else None,
                                        "gate_reason": reason,
                                        "tier_inference_basis": (capability.TIER_INFERENCE_BASIS
                                            if basis == "FREE_TIER_UNAVAILABLE_FOR_MODEL" else None),
                                        "prior_route_refusals": [
                                            {"route": a["route"], "response_sha256": a["response_sha256"],
                                             "classification": a["classification"]}
                                            for a in prior_attempts]}
                            store.put_json(lane / ("paid-policy-" + mode + ".intent.json"), evidence)
                            stamp = paid.record_paid_use(
                                item_key, purpose=paid.CANDIDATE_BLIND_AUDIO_WITNESS_PURPOSE,
                                uncapped=production, admission=reason if unservable_gate else None,
                                unservable_basis=basis if unservable_gate else None,
                            )
                            store.put_json(policy_path, stamp)
                    route = KeyPolicy(n, tuple(range(1, n + 1)), True, stamp).select(item_key)
                    key_for(route)
                    return route

            task, manifest, inner_body = engine._load(task_id, spec)
            ledger = engine._ledger(
                task, manifest, "batch", engine._body(task.name, inner_body, "batch")
            )
            snapshot = ledger.snapshot()

            def advance_engine():
                pricing = None
                if self.config.get("comparison_plan"):
                    from .argon_escalation_contract import Pricing
                    # These are the quoted Batch rates, already discounted. No
                    # cache savings or inferred account credit is assumed.
                    pricing = Pricing("gemini-4-argon", 1.0, 5.0, 1.0,
                        "https://ai.google.dev/gemini-api/docs/pricing#gemini-4-argon; checked 2026-10-16; Batch; grant-scoped")
                try:
                    return engine.advance(
                        task_id,
                        expected=spec,
                        provider=Slotted(),
                        key_policy=Policy(),
                        allow_realtime_fallback=realtime_allowed,
                        request_timeout_s=self.config.get("request_timeout_s", 60),
                        max_recovery_pages=1,
                        pricing=pricing,
                    )
                except capability.CapabilityBlocked as blocked:
                    mode = blocked.receipt["binding"].get("mode", "batch")
                    blocked_ledger = engine._ledger(task, manifest, mode,
                        engine._body(task_id, inner_body, mode))
                    return {**engine._view(task, manifest, inner_body, blocked_ledger, mode,
                        blocked.status, str(blocked)), "capability": blocked.receipt}

            slot_wait_s = self.config.get('slot_wait_s')
            if slot_wait_s is None:
                slot_wait_s = provider_wait_for_call(self.config.get('request_timeout_s', 60))
            # A retained batch still uses the common provider slot. The
            # bounded in-run GET phase caps queue+transport by its own deadline.
            retrieving = bool(engine._dispatch_metadata(snapshot) and engine._job_name(snapshot)
                and snapshot['state'] not in {'FAILED', 'FAILED_PRE_DISPATCH', 'PREPARED'})
            remaining = _remaining_retrieval()
            if remaining is not None:
                slot_wait_s = min(slot_wait_s, remaining)
            if snapshot["state"] == "COMPLETED" or snapshot["events"][-1]["metadata"].get(
                "result_sha256"
            ):
                result = advance_engine()
            else:
                try:
                    with runtime_provider_slot(
                        runtime_root=self.base, timeout_seconds=slot_wait_s
                    ) as lease:
                        require(lease is not None, "ARGON_PROVIDER_SLOT_REQUIRED")
                        result = advance_engine()
                except ProviderSlotBusy:
                    result = engine._view(
                        task,
                        manifest,
                        inner_body,
                        ledger,
                        "batch",
                        "ADMISSION_PENDING",
                        "ARGON_PROVIDER_SLOT_BUSY",
                    )
            if result["status"] == "FAILED":
                rejection = lane_rejection(engine, task, manifest, inner_body)
                if rejection is not None and rejection["classification"]["class"] == ROUTE_UNSERVABLE:
                    tier = rejection["route"].get("accepted_key_tier")
                    if (rejection["mode"] == "batch" and not rejection["realtime_tried"]
                            and realtime_allowed
                            and rejection["classification"]["basis"] == "ZERO_INFERENCE_FAILED_PRECONDITION"):
                        # Possibly a batch-only limit: the same key's realtime
                        # method is this route's next step (engine fallback).
                        result = {**result, "status": "ROUTE_UNSERVABLE_SAME_ROUTE_REALTIME_PENDING",
                                  "route_refusal": rejection["classification"]}
                    elif tier == "free" and rejection["http_status"] == 429 and rejection["mode"] == "batch":
                        store.put_json(
                            lane / "quota-rejected.json",
                            {"task_id": task_id, "response_sha256": rejection["response_sha256"]},
                        )
                        result = {**result, "status": "FREE_QUOTA_NEXT_ROUTE_PENDING"}
                    elif tier == "free":
                        store.put_json(lane / "route-unservable.json", {
                            "schema_version": UNSERVABLE_SCHEMA, "task_id": task_id,
                            "mode": rejection["mode"], "route": rejection["route"],
                            "response_sha256": rejection["response_sha256"],
                            "classification": rejection["classification"]})
                        result = {**result, "status": "ROUTE_UNSERVABLE_NEXT_ROUTE_PENDING",
                                  "route_refusal": rejection["classification"]}
                    else:
                        # The paid key is the last route: the refusal stays a
                        # terminal FAILED receipt; nothing is replayed.
                        result = {**result, "route_refusal": rejection["classification"]}
            return {
                "engine_root": str(engine.root),
                "result": result,
                "prior_key_attempts": prior_attempts,
                "dispatches_this_advance": max(0, result["accounting"]["inference_reservations"]
                                               - before_accounting["inference_reservations"]),
                "new_usable_observations_this_advance": int(result["status"] == "OBSERVED"
                    and not any(h["state"] == "COMPLETED" for h in before_accounting["histories"])),
                'advance_operation': 'RETRIEVE_RETAINED_BATCH' if retrieving else 'ADVANCE_ROUTE',
                'provider_slot_wait_s': slot_wait_s,
            }
