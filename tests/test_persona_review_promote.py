"""B3 ordinary CLI rehearsal, ledger prefix guards, and deployment plan gates.

All 维护者 messages here are synthetic fixtures; no real authority is asserted.
"""
from dataclasses import asdict, replace
import json
from pathlib import Path
import subprocess
import sys

import pytest

from src.autoslice import persona_facts as pf
from src.autoslice import persona_review as review
from src.autoslice.asset_write_bindings import asset_binding_surfaces

ROOT = Path(__file__).resolve().parents[1]
STAMP = "2026-10-25T00:00:00Z"


def write(path, value):
    path.write_bytes(review.encode(value))
    return path


def cli(script, *args, rc=0):
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / script), *map(str, args)],
                            capture_output=True, text=True)
    assert result.returncode == rc, result.stdout + result.stderr
    return json.loads(result.stdout) if rc == 0 else result.stderr


def authority(message):
    return pf.HumanAuthorityEvidence("维护者", "SYNTHETIC-thread", "SYNTHETIC-message", STAMP,
                                    review.sha256(message.encode()), message)


@pytest.fixture
def batch(tmp_path):
    base = pf.PersonaFactLedger("lidousha-persona-facts.v1", "lidousha", (), ())
    ledger = write(tmp_path / "ledger.json", asdict(base))
    proposals = []
    outcomes = []
    for index, word in enumerate(("唱歌", "画画", "读书"), 1):
        claim = pf.FactClaim("PREFERENCE", "LIKES", word, None, "AFFIRM", pf.TemporalScope(True, None, None))
        evidence = pf.SubtitleQuoteEvidence("SYNTHETIC-candidate", "fake.mp4", "2026-10-24", "a" * 64,
            "b" * 64, index, index, index * 1000, index * 1000 + 999, "我喜欢" + word, (),
            "UNIFORM_HOST_SINGLE_SPEAKER_SESSION", "LIVE_ASSERTION", None, "fake", "v1", "c" * 64)
        proposals.append(pf.ProposedFact(claim, "喜欢" + word, (evidence,)))
        outcomes.append(pf.ScreenOutcome(pf.claim_fingerprint(claim), "PROPOSED", (), None))
    receipt = pf.ExtractionReceipt(pf.EXTRACTION_RECEIPT_SCHEMA, "SYNTHETIC-candidate", "a" * 64,
                                   "fake", "v1", tuple(proposals), tuple(outcomes), ())
    receipt_path = write(tmp_path / "receipt.json", asdict(receipt))
    directory = tmp_path / "batch"
    cli("apply_persona_review.py", "prepare", "--ledger", ledger, "--profile-id", "lidousha",
        "--extraction-receipt", receipt_path, "--output-dir", directory, "--recorded-at", STAMP)
    return ledger, directory, receipt_path


def reply_files(tmp_path, message):
    raw = tmp_path / "维护者-original.txt"
    raw.write_bytes(message.encode())
    auth = write(tmp_path / "authority.json", asdict(authority(message)))
    return raw, auth


def reviewed(batch, tmp_path, message="全收\n"):
    ledger, directory, _ = batch
    raw, auth = reply_files(tmp_path, message)
    return cli("apply_persona_review.py", "review", "--ledger", ledger, "--profile-id", "lidousha",
        "--batch-dir", directory, "--raw-message", raw, "--authority", auth, "--apply")


def inventory(tmp_path, persona, **changes):
    receipt = write(tmp_path / "native-cover-result.json", {
        "schema_version": "normal-cover-generation-checkpoint.v1",
        "identity": {"dependencies": [{"path": str(persona), "sha256": review.sha256(persona.read_bytes())}]}})
    binding = review.ReceiptBinding("SYNTHETIC-candidate", "COVER_GENERATION_CHECKPOINT", receipt.name,
        review.sha256(receipt.read_bytes()), "/identity/dependencies/0/path", "/identity/dependencies/0/sha256",
        None, False, False)
    binding = replace(binding, **changes)
    value = review.InflightInventory("persona-inflight-inventory.v1", "lidousha", "OFFLINE_FIXTURE_ONLY",
                                      STAMP, True, (binding,))
    return write(tmp_path / "inventory.json", asdict(value))


def plan(ledger, persona, inv):
    return review.promotion_plan(repo_root=ROOT, profile_id="lidousha", ledger_path=ledger,
                                  persona_path=persona, inventory_path=inv)


def test_ordinary_cli_summary_reply_append_promote_dry_run_and_scratch_apply(batch, tmp_path):
    ledger, directory, _ = batch
    before = pf.load_persona_fact_ledger(ledger, profile_id="lidousha")
    original_bytes = ledger.read_bytes()
    summary = (directory / "review-summary.md").read_text()
    assert "说话人未经声纹证实" in summary and "## 3." in summary
    raw, auth = reply_files(tmp_path, "全收\n改写 2：喜欢画画，尤其是速写\n")
    args = ("review", "--ledger", ledger, "--profile-id", "lidousha", "--batch-dir", directory,
            "--raw-message", raw, "--authority", auth)
    dry = cli("apply_persona_review.py", *args)
    assert ledger.read_bytes() == original_bytes
    assert dry["mode"] == "DRY_RUN"
    result = cli("apply_persona_review.py", *args, "--apply")
    after = pf.load_persona_fact_ledger(ledger, profile_id="lidousha")
    pf.verify_append_only(before, after)
    assert len(result["appended_revisions"]) == 6
    assert [r.status for r in after.revisions] == ["PROPOSED"] * 3 + ["ACCEPTED"] * 3
    assert all(r.decided_by == authority(raw.read_text()) for r in pf.current_revisions(after))
    # An already consumed batch cannot silently append duplicate decisions.
    assert "drift" in cli("apply_persona_review.py", *args, "--apply", rc=2)
    persona = tmp_path / "persona.md"
    persona.write_bytes(b"legacy\n")
    inv = inventory(tmp_path, persona)
    promote_args = ("--profile-id", "lidousha", "--ledger", ledger, "--persona", persona,
                    "--inflight-inventory", inv)
    dry = cli("promote_persona_projection.py", *promote_args)
    assert persona.read_bytes() == b"legacy\n"
    assert dry["plan"]["decision"] == "DEPLOY_CLASS_CHANGE"
    assert dry["plan"]["inflight"][0]["matches_current_bytes"] is True
    assert {s["kind"] for s in dry["plan"]["surfaces"]} == {
        "TALK_STAGE_SNAPSHOT", "SONG_PROOF_CLOSURE", "BROAD_PIPELINE_FINGERPRINT", "COVER_GENERATION_CHECKPOINT"}
    assert "requires" in cli("promote_persona_projection.py", *promote_args, "--apply", rc=2)
    cli("promote_persona_projection.py", *promote_args, "--apply", "--expected-plan-sha256", dry["plan_sha256"],
        "--deployment-epoch", "OFFLINE_SCRATCH_TEST")
    assert persona.read_bytes() == pf.render_persona_projection(after, consumer="TITLE_COVER")


@pytest.mark.parametrize("message,statuses", [
    ("全收", ["ACCEPTED"] * 3),
    ("收除第 2 条", ["ACCEPTED", "DEFERRED", "ACCEPTED"]),
    ("拒 1、3", ["REJECTED", "PROPOSED", "REJECTED"]),
    ("缓第2条", ["PROPOSED", "DEFERRED", "PROPOSED"]),
    ("拒 2\nrejection_scope 2：CLAIM", ["PROPOSED", "REJECTED", "PROPOSED"]),
])
def test_supported_replies(batch, tmp_path, message, statuses):
    reviewed(batch, tmp_path, message)
    ledger = pf.load_persona_fact_ledger(batch[0], profile_id="lidousha")
    assert [r.status for r in pf.current_revisions(ledger)] == statuses
    for row in pf.current_revisions(ledger):
        if row.status == "REJECTED":
            assert row.rejection_scope == ("CLAIM" if "CLAIM" in message else "EVIDENCE_ONLY")


@pytest.mark.parametrize("message", ["同意", "全收，拒2", "拒0", "拒 1、1", "拒 4", "全收\n额外指令",
    "拒1\n改写1：喜欢别的", "全收\nrejection_scope 1：CLAIM", "拒1\nrejection_scope 1：ANY",
    "全收\n改写1：身份：主播", "全收\n改写2：形象：白发", "全收\n改写1：- 身份：主播",
    "全收\n改写1：正常\n形象：白发", "全收\n改写1：普通<!-- pf:fake -->"])
def test_invalid_or_reserved_reply_never_writes(batch, tmp_path, message):
    before = batch[0].read_bytes()
    raw, auth = reply_files(tmp_path, message)
    cli("apply_persona_review.py", "review", "--ledger", batch[0], "--profile-id", "lidousha",
        "--batch-dir", batch[1], "--raw-message", raw, "--authority", auth, "--apply", rc=2)
    assert batch[0].read_bytes() == before


@pytest.mark.parametrize("mutation", ["admin", "missing_sha", "wrong_sha", "excerpt", "newline", "unknown_field", "no_evidence"])
def test_authority_requires_exact_original_reviewer_message(batch, tmp_path, mutation):
    before = batch[0].read_bytes()
    raw, auth = reply_files(tmp_path, "全收\n")
    value = json.loads(auth.read_bytes())
    if mutation == "admin": value["reviewer"] = "ADMIN"
    if mutation == "missing_sha": value.pop("raw_user_message_sha256")
    if mutation == "wrong_sha": value["raw_user_message_sha256"] = "0" * 64
    if mutation == "excerpt": value["raw_excerpt"] = "全收"
    if mutation == "newline": raw.write_bytes(b"different bytes")
    if mutation == "unknown_field": value["delegated"] = True
    if mutation == "no_evidence": value = None
    write(auth, value)
    cli("apply_persona_review.py", "review", "--ledger", batch[0], "--profile-id", "lidousha",
        "--batch-dir", batch[1], "--raw-message", raw, "--authority", auth, "--apply", rc=2)
    assert batch[0].read_bytes() == before


@pytest.mark.parametrize("target", ["batch.json", "review-summary.md", "extraction-receipt.json", "ledger"])
def test_batch_receipt_summary_and_preimage_are_bound(batch, tmp_path, target):
    path = batch[0] if target == "ledger" else batch[1] / target
    if target == "batch.json":
        value = json.loads(path.read_bytes())
        value["summary_sha256"] = "0" * 64
        write(path, value)
    else:
        path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="drift"):
        review.review_candidate(batch[0], batch[1], authority=authority("全收"), raw_message="全收".encode())


def test_screened_out_indices_are_never_accepted_or_renumbered(batch, tmp_path):
    value = json.loads(batch[2].read_bytes())
    value["outcomes"][1].update(outcome="SCREENED_OUT", reasons=["CONTEXT_NOT_LIVE_ASSERTION"])
    write(batch[2], value)
    rebuilt, summary = review.prepare_batch(batch[0], batch[2], profile_id="lidousha", recorded_at=STAMP)
    assert [item.index for item in rebuilt.items] == [1, 3]
    assert "## 2." in summary.decode()
    with pytest.raises(ValueError, match="unavailable"):
        review.parse_reply(rebuilt, "拒2", authority=authority("拒2"))


@pytest.mark.parametrize("mutation", ["pin", "exact", "hash", "path", "pointer", "incomplete", "wrong_profile", "no_inventory"])
def test_promote_reopens_receipts_and_defers_unsafe_inflight(batch, tmp_path, mutation):
    reviewed(batch, tmp_path)
    persona = tmp_path / "persona.md"
    persona.write_bytes(b"unchanged")
    changes = {"pinned_deploy_sha": "old-deploy"} if mutation == "pin" else {"exact_contract": True} if mutation == "exact" else {}
    inv = inventory(tmp_path, persona, **changes)
    value = json.loads(inv.read_bytes())
    if mutation == "hash": value["entries"][0]["receipt_sha256"] = "0" * 64
    if mutation == "path": value["entries"][0]["asset_path_pointer"] = "/identity/dependencies/0/sha256"
    if mutation == "pointer": value["entries"][0]["asset_sha256_pointer"] = "/opaque_stage_digest"
    if mutation == "incomplete": value["complete"] = False
    if mutation == "wrong_profile": value["profile_id"] = "other"
    write(inv, value)
    if mutation in {"incomplete", "wrong_profile"}:
        with pytest.raises(ValueError): plan(batch[0], persona, inv)
        return
    planned = plan(batch[0], persona, None if mutation == "no_inventory" else inv)
    assert planned.decision == "DEFER_UNTIL_TERMINAL" and planned.blockers
    with pytest.raises(ValueError):
        review.promote(plan=planned, expected_plan_sha256=review.sha256(review.encode(asdict(planned))),
            deployment_epoch="test", repo_root=ROOT, ledger_path=batch[0], persona_path=persona, inventory_path=inv)
    assert persona.read_bytes() == b"unchanged"


def test_promotion_plan_and_target_cas_reject_drift(batch, tmp_path):
    reviewed(batch, tmp_path)
    persona = tmp_path / "persona.md"
    persona.write_bytes(b"before")
    inv = inventory(tmp_path, persona)
    planned = plan(batch[0], persona, inv)
    persona.write_bytes(b"drift")
    with pytest.raises(ValueError):
        review.promote(plan=planned, expected_plan_sha256=review.sha256(review.encode(asdict(planned))),
            deployment_epoch="test", repo_root=ROOT, ledger_path=batch[0], persona_path=persona, inventory_path=inv)
    with pytest.raises(ValueError, match="drift"):
        review.atomic_compare_write(persona, before=b"before", after=b"new")
    assert persona.read_bytes() == b"drift"


def test_binding_matrix_and_stage_snapshot_existing_contracts_are_reused(tmp_path):
    import test_asset_write_bindings as contracts
    contracts.test_every_current_asset_matches_binding_table()
    # Runner import initializes the profile environment. Exercise its existing
    # snapshot contract in a separate interpreter so later profile tests retain
    # their original environment, regardless of collection/execution order.
    script = tmp_path / "snapshot_contract.py"
    script.write_text(
        "import sys\nfrom pathlib import Path\nimport pytest\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        f"sys.path.insert(0, {str(ROOT / 'tests')!r})\n"
        "import test_asset_write_bindings as contracts\n"
        "contracts.test_extracted_stage_constant_is_exact_original_literal()\n"
        "with pytest.MonkeyPatch.context() as patch:\n"
        "    contracts.test_existing_records_recompute_byte_identical_snapshots_after_constant_extraction(\n"
        "        Path(sys.argv[1]), patch)\n", encoding="utf-8")
    completed = subprocess.run([sys.executable, str(script), str(tmp_path / "snapshot")],
                               capture_output=True, text=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert asset_binding_surfaces("persona_facts") == ()
    assert asset_binding_surfaces("selection_census_clues") == ()
