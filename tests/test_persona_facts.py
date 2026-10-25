"""B1 ledger contracts, including legacy bytes and authority rejection controls."""
from dataclasses import FrozenInstanceError, asdict, replace
import hashlib
import json
from pathlib import Path

import pytest
from src.autoslice import persona_facts as pf

ROOT = Path(__file__).resolve().parents[1]
STAMP = "2026-10-25T00:00:00Z"


def human():
    return pf.HumanAuthorityEvidence("维护者", "thread", "message", STAMP, "a" * 64, "收下这条")


def proposal():
    claim = pf.FactClaim("PREFERENCE", "LIKES", "唱歌", None, "AFFIRM", pf.TemporalScope(True, None, None))
    evidence = pf.SubtitleQuoteEvidence("candidate", "recording.mp4", "2026-10-24", "b" * 64, "c" * 64,
        1, 1, 0, 1000, "我喜欢唱歌", (), "ATTRIBUTED_HOST", "LIVE_ASSERTION", None, "extractor", "v1", "d" * 64)
    return pf.PersonaFactRevision("fact-1", "r1", "PROPOSED", claim, pf.claim_fingerprint(claim), "喜欢唱歌",
        pf.RenderSlot(pf.PROJECTION_GENERATED_SECTION_ZH, 100), (evidence,), "MACHINE_PROPOSAL", None, None, None, STAMP)


def ledger(*rows):
    return pf.PersonaFactLedger("lidousha-persona-facts.v1", "lidousha", rows, ())


def accept(original):
    return pf.apply_review_decisions(original, (pf.ReviewDecision("fact-1", "ACCEPT", None, None, human()),), recorded_at=STAMP)


def test_committed_projection_equals_persona_bytes():
    migrated = pf.load_persona_fact_ledger(ROOT / "assets/lidousha/persona_facts.v1.json", profile_id="lidousha")
    original = (ROOT / "assets/lidousha/persona.md").read_bytes()
    assert len(migrated.revisions) == 23
    assert len(pf.current_revisions(migrated)) == 23
    assert migrated.revisions[20].claim.kind == "EDITORIAL_DIRECTIVE"
    assert all(row.evidence_class == "LEGACY_PERSONA_MD_GRANDFATHERED" for row in migrated.revisions)
    rendered = pf.render_persona_projection(migrated, consumer="TITLE_COVER")
    assert rendered == original
    assert hashlib.sha256(rendered).hexdigest() == "66c9eb8780a80391175aa38ce289f230896d374d577d53c310f04281a0b2045a"
    # The same assertion rejects any manual edit, including an added blank line.
    with pytest.raises(AssertionError):
        assert rendered == original + b"\n"


@pytest.mark.parametrize("text", ["", "# 标题\n\n- 原文\n", "# A\r\n\r\n尾行", "只有一行", "\n\n"])
def test_migration_preserves_all_line_bytes(text):
    data = text.encode()
    blob = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
    result = pf.migrate_legacy_persona_md(text, git_blob_sha=blob, profile_id="lidousha", recorded_at=STAMP)
    pf.verify_append_only(ledger(), result)
    assert len(result.revisions) == len(text.splitlines(keepends=True))
    assert pf.render_persona_projection(result, consumer="TITLE_COVER") == data
    with pytest.raises(ValueError, match="blob"):
        pf.migrate_legacy_persona_md(text, git_blob_sha="0" * 40, profile_id="lidousha", recorded_at=STAMP)


def test_types_are_frozen():
    for value in (proposal(), proposal().claim, proposal().claim.temporal, proposal().render, proposal().evidence[0], human(), ledger()):
        with pytest.raises(FrozenInstanceError):
            value.forged = True


def test_claim_fingerprint_uses_canonical_and_nfkc_without_punctuation():
    claim = proposal().claim
    assert pf.claim_fingerprint(replace(claim, object_text="Ａ B，。")) == pf.claim_fingerprint(replace(claim, object_text="AB"))
    ref = pf.EntityRef("registry", "同一个人", "RESOLVED")
    assert pf.claim_fingerprint(replace(claim, object_entity=ref, object_text="别名一")) == pf.claim_fingerprint(replace(claim, object_entity=ref, object_text="别名二"))
    assert pf.claim_fingerprint(claim) != pf.claim_fingerprint(replace(claim, polarity="NEGATE"))


def test_human_acceptance_and_projection_do_not_mutate_original():
    original = ledger(proposal())
    updated = accept(original)
    pf.verify_append_only(original, updated)
    assert original.revisions[0].status == "PROPOSED"
    assert len(updated.revisions) == 2
    assert updated.revisions[-1].supersedes == "r1"
    assert pf.render_persona_projection(original, consumer="TITLE_COVER") == b""
    assert pf.render_persona_projection(updated, consumer="TITLE_COVER").decode() == "## 切片中积累的已审定特质\n- 喜欢唱歌 <!-- pf:fact-1@r2 -->\n"


@pytest.mark.parametrize("authority", [None, replace(human(), reviewer="ADMIN"), replace(human(), raw_user_message_sha256=""), proposal().evidence[0]])
def test_acceptance_without_real_reviewer_evidence_fails(authority):
    row = replace(proposal(), status="ACCEPTED", decided_by=authority)
    with pytest.raises(ValueError):
        pf.current_revisions(ledger(row))


@pytest.mark.parametrize("prefix", ["身份：虚拟主播", "形象：白发", "- 身份：主播", "  ## 形象：熊猫", "普通文字\n身份：伪造", "普通文字<!-- pf:fake -->"])
def test_machine_reserved_prefix_and_multiline_are_rejected(prefix):
    with pytest.raises(ValueError, match="projection"):
        pf.current_revisions(ledger(replace(proposal(), statement_zh=prefix)))


@pytest.mark.parametrize("kind", ["IDENTITY_BACKGROUND", "APPEARANCE", "EDITORIAL_DIRECTIVE", "LEGACY_TEXT"])
def test_machine_cannot_propose_human_only_kinds(kind):
    claim = replace(proposal().claim, kind=kind)
    with pytest.raises(ValueError, match="Machine origin"):
        pf.current_revisions(ledger(replace(proposal(), claim=claim, claim_fingerprint=pf.claim_fingerprint(claim))))


@pytest.mark.parametrize("change", ["edit", "delete", "reorder", "identity", "suppression"])
def test_append_only_rejects_rewriting_history(change):
    original = accept(ledger(proposal()))
    if change == "edit":
        current = replace(original, revisions=(replace(original.revisions[0], statement_zh="改写"), original.revisions[1]))
    elif change == "delete":
        current = replace(original, revisions=original.revisions[:1])
    elif change == "reorder":
        current = replace(original, revisions=tuple(reversed(original.revisions)))
    elif change == "identity":
        current = replace(original, profile_id="other")
    else:
        rule = pf.SuppressionRule("rule", "PREFERENCE", "LIKES", "object", human(), STAMP)
        original = replace(original, suppressions=(rule,))
        current = replace(original, suppressions=())
    with pytest.raises(ValueError):
        pf.verify_append_only(original, current)


def test_reject_defer_and_reproposal_require_new_evidence():
    original = ledger(proposal())
    deferred = pf.apply_review_decisions(original, (pf.ReviewDecision("fact-1", "DEFER", None, None, human()),), recorded_at=STAMP)
    assert deferred.revisions[-1].status == "DEFERRED"
    assert accept(deferred).revisions[-1].status == "ACCEPTED"
    for scope in ("CLAIM", "CLAIM_UNLESS_HUMAN_EVIDENCE", "EVIDENCE_ONLY"):
        rejected = pf.apply_review_decisions(original, (pf.ReviewDecision("fact-1", "REJECT", None, scope, human()),), recorded_at=STAMP)
        head = rejected.revisions[-1]
        row = replace(head, revision="r3", supersedes="r2", status="PROPOSED", rejection_scope=None)
        with pytest.raises(ValueError):
            pf.verify_append_only(rejected, replace(rejected, revisions=(*rejected.revisions, row)))
        row = replace(row, evidence=(*row.evidence, human()))
        if scope == "CLAIM":
            with pytest.raises(ValueError):
                pf.current_revisions(replace(rejected, revisions=(*rejected.revisions, row)))
        else:
            pf.verify_append_only(rejected, replace(rejected, revisions=(*rejected.revisions, row)))


def test_accepted_evidence_append_preserves_projection_and_authority():
    original = accept(ledger(proposal()))
    head = original.revisions[-1]
    row = replace(head, revision="r3", supersedes="r2", evidence=(*head.evidence, human()))
    updated = replace(original, revisions=(*original.revisions, row))
    pf.verify_append_only(original, updated)
    assert pf.current_revisions(updated)[0].statement_zh == head.statement_zh
    for changes in ({"statement_zh": "改写"}, {"decided_by": None}, {"evidence": (human(),)}):
        with pytest.raises(ValueError):
            pf.current_revisions(replace(original, revisions=(*original.revisions, replace(row, **changes))))


def test_stale_and_retired_heads_leave_projection_and_cannot_be_revived_without_review():
    original = accept(ledger(proposal()))
    head = original.revisions[-1]
    for status in ("STALE_EVIDENCE", "RETIRED"):
        row = replace(head, revision="r3", supersedes="r2", status=status, decided_by=None)
        current = replace(original, revisions=(*original.revisions, row))
        pf.verify_append_only(original, current)
        assert pf.render_persona_projection(current, consumer="TITLE_COVER") == b""
        if status == "STALE_EVIDENCE":
            assert accept(current).revisions[-1].status == "ACCEPTED"
        else:
            with pytest.raises(ValueError, match="transition"):
                accept(current)


@pytest.mark.parametrize("mutation", ["unknown", "status", "profile", "tuple", "bool", "evidence", "fingerprint", "fork", "duplicate"])
def test_loading_rejects_invalid_json_and_revision_chains(tmp_path, mutation):
    data = asdict(accept(ledger(proposal())))
    if mutation == "unknown": data["extra"] = True
    if mutation == "status": data["revisions"][0]["status"] = "MADE_UP"
    if mutation == "profile": data["profile_id"] = "other"
    if mutation == "tuple": data["revisions"] = {}
    if mutation == "bool": data["revisions"][0]["render"]["order"] = True
    if mutation == "evidence": data["revisions"][0]["evidence"][0]["provenance_kind"] = "CORRECTION_RECORD"
    if mutation == "fingerprint": data["revisions"][0]["claim_fingerprint"] = "bad"
    if mutation == "fork": data["revisions"][1]["supersedes"] = "missing"
    path = tmp_path / "ledger.json"
    text = json.dumps(data)
    if mutation == "duplicate": text = text.replace('"profile_id": "lidousha"', '"profile_id": "lidousha", "profile_id": "lidousha"')
    path.write_text(text)
    with pytest.raises(ValueError):
        pf.load_persona_fact_ledger(path, profile_id="lidousha")


@pytest.mark.parametrize("change", ["ledger_list", "evidence_list", "findings_list", "bad_human", "unsafe_id", "bad_predicate"])
def test_typed_boundaries_cannot_hide_mutability_or_forged_evidence(change):
    row = proposal()
    if change == "ledger_list":
        current = replace(ledger(row), revisions=[row])
    else:
        if change == "evidence_list": row = replace(row, evidence=list(row.evidence))
        if change == "findings_list": row = replace(row, evidence=(replace(row.evidence[0], overlapping_adjudicated_findings=[]),))
        if change == "bad_human": row = replace(row, evidence=(*row.evidence, replace(human(), reviewer="ADMIN")))
        if change == "unsafe_id": row = replace(row, fact_id="fact\n身份：伪造")
        if change == "bad_predicate":
            claim = replace(row.claim, predicate="FRIEND")
            row = replace(row, claim=claim, claim_fingerprint=pf.claim_fingerprint(claim))
        current = ledger(row)
    with pytest.raises(ValueError):
        pf.current_revisions(current)
