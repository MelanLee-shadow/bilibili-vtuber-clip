"""B2 end-to-end review output and independent rejection controls (no provider)."""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from src.autoslice import persona_facts as pf
from src.autoslice import persona_fact_extraction as ex

ROOT = Path(__file__).resolve().parents[1]
STAMP = "2026-10-25T00:00:00Z"


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def sample(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    srt = source / "final.srt"
    srt.write_bytes("1\n00:00:01,000 --> 00:00:02,000\n我喜欢唱歌\n\n2\n00:00:03,000 --> 00:00:04,000\n我喜欢画画\n".encode())
    sha = ex.digest(srt.read_bytes())
    audit = write(source / "audit.json", {"schema_version": "final-review-audit.v2", "status": "CLEAN",
        "release_gate": "PASS", "reviewed_srt_sha256": "sha256:" + sha, "findings": []})
    sealed = pf.SealedFinalSubtitle("candidate", "recording.mp4", "2026-10-24", srt, sha,
        audit, ex.digest(audit.read_bytes()), None, None, None)
    evidence = pf.SubtitleQuoteEvidence("candidate", "recording.mp4", "2026-10-24", sha,
        sealed.final_review_audit_sha256, 1, 1, 999, 9999, "我喜欢唱歌", (), "ATTRIBUTED_HOST",
        "LIVE_ASSERTION", "BV_fake_model_supplied", "fake", "v1", "d" * 64)
    claim = pf.FactClaim("PREFERENCE", "LIKES", "唱歌", None, "AFFIRM", pf.TemporalScope(True, None, None))
    proposal = pf.ProposedFact(claim, "喜欢唱歌", (evidence,))
    ledger = pf.PersonaFactLedger("example-persona-facts.v1", "example", (), ())
    relations = write(source / "relations.json", {"schema_version": "session-relation-ledger.v1", "sessions": []})
    registry = write(source / "publication.json", {"schema_version": "publication-registry.v1", "entries": [
        {"candidate_id": "candidate", "recording_date": "2026-10-24", "status": "published", "bvid": "BV_test"}]})
    sources = ex.ScreeningSources(relations, registry)
    return sealed, proposal, ledger, sources


def run(sample, **changes):
    sealed, proposal, ledger, sources = sample
    return ex.screen((changes.get("proposal", proposal),), sealed=changes.get("sealed", sealed),
                     ledger=changes.get("ledger", ledger), sources=changes.get("sources", sources))


def update_audit(sealed, **changes):
    data = json.loads(sealed.final_review_audit_path.read_bytes())
    data.update(changes)
    write(sealed.final_review_audit_path, data)
    return replace(sealed, final_review_audit_sha256=ex.digest(sealed.final_review_audit_path.read_bytes()))


def assert_reject(result, reason):
    assert result.outcomes[0].outcome == "SCREENED_OUT"
    assert reason in result.outcomes[0].reasons


def test_solo_is_explicit_unverified_and_rebuilds_evidence(sample):
    result = run(sample, sources=replace(sample[3], source_offset_ms=20000))
    assert result.outcomes[0].outcome == "PROPOSED"
    e = result.proposals[0].evidence[0]
    assert e.speaker_basis == "UNIFORM_HOST_SINGLE_SPEAKER_SESSION"
    assert (e.source_start_ms, e.source_end_ms, e.bvid) == (21000, 22000, "BV_test")
    assert pf.screen_proposals((sample[1],), ledger=sample[2], sealed=sample[0])[0].outcome == "SCREENED_OUT"


def test_cpa_adapter_accepts_fake_callable_and_only_prompts_final_srt(sample):
    seen = []
    def llm_call(prompt):
        seen.append(prompt)
        return json.dumps({"facts": [{"claim": asdict(sample[1].claim), "statement_zh": "喜欢唱歌",
            "cue_index_start": 1, "cue_index_end": 1, "verbatim_quote": "我喜欢唱歌", "context_kind": "LIVE_ASSERTION"}]})
    adapter = ex.CpaPersonaFactExtractor(llm_call)
    proposals = adapter(sample[0])
    result = ex.screen(proposals, sealed=sample[0], ledger=sample[2], sources=sample[3])
    assert result.outcomes[0].outcome == "PROPOSED"
    assert proposals[0].evidence[0].extractor_prompt_sha256 == ex.digest(seen[0].encode())
    assert sample[0].final_srt_path.read_text() in seen[0]
    assert "BV_fake_model_supplied" not in seen[0]


@pytest.mark.parametrize("mutation", ["text", "index", "range", "empty"])
def test_quote_not_in_sealed_srt(sample, mutation):
    e = sample[1].evidence[0]
    changes = {"text": {"verbatim_quote": "标题说她很勇敢"}, "index": {"cue_index_start": 3, "cue_index_end": 3},
        "range": {"cue_index_start": 2, "cue_index_end": 1}, "empty": {"verbatim_quote": ""}}[mutation]
    assert_reject(run(sample, proposal=replace(sample[1], evidence=(replace(e, **changes),))), "QUOTE_NOT_IN_SEALED_SRT")


@pytest.mark.parametrize("mutation", ["raw", "expected", "audit", "v1", "blocked", "identity"])
def test_sealed_sha_mismatch(sample, mutation):
    sealed, p, _, _ = sample
    if mutation == "raw": sealed.final_srt_path.write_bytes(sealed.final_srt_path.read_bytes() + b"\n")
    if mutation == "expected": sealed = replace(sealed, final_srt_sha256="0" * 64)
    if mutation == "audit": sealed = replace(sealed, final_review_audit_sha256="0" * 64)
    if mutation == "v1": sealed = update_audit(sealed, schema_version="final-review-audit.v1")
    if mutation == "blocked": sealed = update_audit(sealed, release_gate="BLOCK")
    if mutation == "identity": p = replace(p, evidence=(replace(p.evidence[0], candidate_id="other"),))
    assert_reject(run(sample, sealed=sealed, proposal=p), "SEALED_SRT_SHA_MISMATCH")


@pytest.mark.parametrize("grade,choice,reject", [
    ("A", "CURRENT", False), ("A", "PROPOSED", False), ("B", "CURRENT", True),
    ("C", "CURRENT", True), ("D", "UNRESOLVABLE", True), ("A", "CONTEXT_FIT", True),
    ("A", "UNRESOLVABLE", True), (None, None, True)])
def test_each_uncertain_adjudication_and_clean_cue(sample, grade, choice, reject):
    sealed = update_audit(sample[0], findings=[{"cue_index": 1, "finding_id": "finding-1",
        "terminal_review": {"grade": grade, "choice": choice}}])
    # Extractor's empty findings cannot hide the audit finding.
    p = replace(sample[1], evidence=(replace(sample[1].evidence[0], final_review_audit_sha256=sealed.final_review_audit_sha256),))
    result = run(sample, sealed=sealed, proposal=p)
    assert ("CUE_OVERLAPS_UNCERTAIN_ADJUDICATION" in result.outcomes[0].reasons) is reject
    assert len(result.proposals[0].evidence[0].overlapping_adjudicated_findings) == 1
    p2 = replace(p, evidence=(replace(p.evidence[0], cue_index_start=2, cue_index_end=2, verbatim_quote="我喜欢画画"),))
    assert run(sample, sealed=sealed, proposal=p2).outcomes[0].outcome == "PROPOSED"


@pytest.mark.parametrize("grade,choice,rejected", [
    ("A", "CURRENT", False), ("B", "CURRENT", True), ("C", "CURRENT", True),
    ("D", "UNRESOLVABLE", True), ("A", "CONTEXT_FIT", True)])
def test_native_nested_terminal_adjudication(sample, grade, choice, rejected):
    sealed = update_audit(sample[0], resolved_findings=[{"cue_index": 1, "finding_id": "native-finding",
        "exact_release_adjudication": {"rebuilt_finding": {"cue_index": 1},
            "terminal_review": {"grade": grade, "choice": choice, "request": {"context": [{"cue_index": 2}]}}}}])
    p = replace(sample[1], evidence=(replace(sample[1].evidence[0], final_review_audit_sha256=sealed.final_review_audit_sha256),))
    result = run(sample, sealed=sealed, proposal=p)
    assert ("CUE_OVERLAPS_UNCERTAIN_ADJUDICATION" in result.outcomes[0].reasons) is rejected
    assert len(result.proposals[0].evidence[0].overlapping_adjudicated_findings) == 1


def multi(sample):
    path = sample[3].session_relation_ledger
    write(path, {"schema_version": "session-relation-ledger.v1", "sessions": [{
        "date": "2026-10-24", "recording_basenames": ["recording.mp4"], "source_sha256s": [],
        "relation_id": "session-1", "relation_type": "collab", "state": "CLAIMED",
        "participants": [{"name": "host"}, {"name": "guest"}], "evidence": [{"source": "fixture"}]}]})


@pytest.mark.parametrize("label", [None, "HOST", "OTHER", "NON_HOST", "UNKNOWN", "MEDIA"])
def test_multispeaker_requires_attributed_host(sample, label):
    multi(sample)
    sealed = sample[0]
    if label:
        path = write(sealed.final_srt_path.parent / "speaker.json", {"schema_version": "speaker-attribution.v1",
            "srt_sha256": "sha256:" + sealed.final_srt_sha256, "cues": [{"cue_index": 1, "speaker": label}]})
        sealed = replace(sealed, speaker_attribution_path=path)
    result = run(sample, sealed=sealed)
    assert (result.outcomes[0].outcome == "PROPOSED") is (label == "HOST")
    if label != "HOST": assert_reject(result, "SPEAKER_NOT_ATTRIBUTED_HOST")


def test_absent_session_ledger_does_not_infer_solo(sample):
    assert_reject(run(sample, sources=replace(sample[3], session_relation_ledger=None)), "SPEAKER_NOT_ATTRIBUTED_HOST")


@pytest.mark.parametrize("context", ["QUOTED", "READ_ALOUD", "ROLE_PLAY", "MEDIA_PLAYBACK", "HYPOTHETICAL", "GAME_OR_FICTION", "UNKNOWN"])
def test_every_non_live_context(sample, context):
    p = replace(sample[1], evidence=(replace(sample[1].evidence[0], context_kind=context),))
    assert_reject(run(sample, proposal=p), "CONTEXT_NOT_LIVE_ASSERTION")


@pytest.mark.parametrize("kind", ["read", "MEDIA", "UNKNOWN", "LIVE"])
def test_context_artifacts_override_model_live_assertion(sample, kind):
    sealed = sample[0]
    path = sealed.final_srt_path.parent / "context.json"
    if kind == "read":
        write(path, {"srt_sha256": sealed.final_srt_sha256, "candidates": [{"cue_indexes": [1]}]})
        sources = replace(sample[3], read_aloud_candidates=path)
    else:
        write(path, {"srt_sha256": sealed.final_srt_sha256, "segments": [{"start_cue": 1, "end_cue": 1, "kind": kind}]})
        sources = replace(sample[3], media_context=path)
    result = run(sample, sources=sources)
    if kind == "LIVE": assert result.outcomes[0].outcome == "PROPOSED"
    else: assert_reject(result, "CONTEXT_NOT_LIVE_ASSERTION")


def test_native_chat_authority_rows_override_model_with_source_clock_offset(sample):
    sealed = sample[0]
    path = write(sealed.final_srt_path.parent / "chat-authority.json", {
        "schema_version": "chat-authority-audit.v2", "output_srt_sha256": sealed.final_srt_sha256,
        "applied": [{"matched_start_ms": 1000, "matched_end_ms": 2000, "kind": "danmaku"}],
        "superseded_chat_proposals": [], "read_aloud_arbitrations": []})
    assert_reject(run(sample, sources=replace(sample[3], read_aloud_candidates=path, source_offset_ms=20000)),
                  "CONTEXT_NOT_LIVE_ASSERTION")


def test_stale_optional_artifact_cannot_override_current_evidence(sample):
    path = write(sample[0].final_srt_path.parent / "old-context.json", {
        "srt_sha256": "0" * 64, "segments": [{"start_cue": 1, "end_cue": 1, "kind": "LIVE"}]})
    with pytest.raises(ValueError, match="binding mismatch"):
        run(sample, sources=replace(sample[3], media_context=path))


@pytest.mark.parametrize("kind", ["IDENTITY_BACKGROUND", "APPEARANCE", "EDITORIAL_DIRECTIVE", "LEGACY_TEXT"])
def test_kind_not_machine_proposable(sample, kind):
    assert_reject(run(sample, proposal=replace(sample[1], claim=replace(sample[1].claim, kind=kind))), "KIND_NOT_MACHINE_PROPOSABLE")


@pytest.mark.parametrize("state", ["missing", "uncertain", "keep", "resolved"])
def test_entity_unresolved_owner_canonical(sample, state):
    sealed, p, _, _ = sample
    ref = pf.EntityRef("streamer_registry_sources", "小甲", "UNCERTAIN" if state == "uncertain" else "RESOLVED")
    p = replace(p, claim=replace(p.claim, object_entity=ref))
    if state != "missing":
        path = write(sealed.final_srt_path.parent / "entities.json", {"entities": [
            {"owner_key": ref.owner_key, "canonical": ref.canonical,
             "uncertain_keep_canonicals": [ref.canonical] if state == "keep" else []}]})
        sealed = replace(sealed, entity_projection_path=path)
    result = run(sample, sealed=sealed, proposal=p)
    if state == "resolved": assert result.outcomes[0].outcome == "PROPOSED"
    else: assert_reject(result, "ENTITY_UNRESOLVED")


def human():
    return pf.HumanAuthorityEvidence("维护者", "thread", "message", STAMP, "a" * 64, "拒绝这条")


def proposed_ledger(sample):
    result = run(sample)
    return pf.append_screened(sample[2], result.proposals, result.outcomes, recorded_at=STAMP)


@pytest.mark.parametrize("scope", ["CLAIM", "CLAIM_UNLESS_HUMAN_EVIDENCE", "EVIDENCE_ONLY"])
def test_rejected_fingerprint_match_even_reworded(sample, scope):
    ledger = pf.apply_review_decisions(proposed_ledger(sample), (pf.ReviewDecision(
        "pf-" + pf.claim_fingerprint(sample[1].claim), "REJECT", None, scope, human()),), recorded_at=STAMP)
    p = replace(sample[1], claim=replace(sample[1].claim, object_text=" 唱，歌！"), statement_zh="享受歌唱")
    assert_reject(run(sample, ledger=ledger, proposal=p), "REJECTED_FINGERPRINT_MATCH")
    if scope == "EVIDENCE_ONLY":
        p = replace(p, evidence=(replace(p.evidence[0], cue_index_start=2, cue_index_end=2, verbatim_quote="我喜欢画画"),))
        assert run(sample, ledger=ledger, proposal=p).outcomes[0].outcome == "PROPOSED"


def test_suppression_and_polarity_conflict(sample):
    ledger = replace(sample[2], suppressions=(pf.SuppressionRule("rule", "PREFERENCE", "LIKES", "唱", human(), STAMP),))
    assert_reject(run(sample, ledger=ledger), "REJECTED_FINGERPRINT_MATCH")
    base = proposed_ledger(sample)
    fact = base.revisions[-1]
    accepted = pf.apply_review_decisions(base, (pf.ReviewDecision(fact.fact_id, "ACCEPT", None, None, human()),), recorded_at=STAMP)
    p = replace(sample[1], claim=replace(sample[1].claim, polarity="NEGATE"))
    result = run(sample, ledger=accepted, proposal=p)
    assert result.outcomes[0].outcome == "CONFLICT_PROPOSED"
    updated = pf.append_screened(accepted, result.proposals, result.outcomes, recorded_at=STAMP)
    assert pf.current_revisions(updated)[-1].status == "PROPOSED"
    assert pf.current_revisions(updated)[0] == accepted.revisions[-1]
    # Same accepted fact only appends evidence; it never changes accepted prose.
    same = run(sample, ledger=accepted)
    assert same.outcomes[0].outcome == "EVIDENCE_APPENDED"
    assert pf.append_screened(accepted, same.proposals, same.outcomes, recorded_at=STAMP) == accepted


def test_source_srt_superseded(sample):
    sealed = sample[0]
    path = write(sealed.final_srt_path.parent / "current.json", {"schema_version": "persona-published-source-bindings.v1",
        "entries": [{"candidate_id": "replacement", "bvid": "BV_test", "final_srt_sha256": "0" * 64}]})
    assert_reject(run(sample, sources=replace(sample[3], current_source_bindings=path)), "SOURCE_SRT_SUPERSEDED")


@pytest.mark.parametrize("statement", ["身份：主播", "形象：白发", "第一行\n第二行", "注入<!-- pf:fake -->"])
def test_reserved_projection_prefix(sample, statement):
    assert_reject(run(sample, proposal=replace(sample[1], statement_zh=statement)), "RESERVED_PROJECTION_PREFIX")


def test_corrections_only_make_directional_owner_review(sample, tmp_path):
    sealed = update_audit(sample[0], sealed_corrections=[{"heard_surface": "小家", "resolved_canonical": "小甲",
        "source_start_ms": 1000, "source_end_ms": 2000, "adjudication_ref": "correction-1"}])
    observations = pf.observations_from_corrections(sealed)
    assert len(observations) == 1
    assert (observations[0].direction, observations[0].scope) == ("HEARD_AS_TO_RESOLVED", "SOURCE_INTERVAL")
    calls = []
    def fake(s):
        calls.append(s)
        return ()
    batch = ex.run_extraction(sealed=sealed, ledger=sample[2], sources=sample[3], extractor=fake,
        output_dir=tmp_path / "review", extractor_id="fake", extractor_version="v1")
    receipt = json.loads((batch / "extraction-receipt.json").read_bytes())
    assert receipt["proposals"] == []
    assert receipt["owner_proposals"][0]["target"] == "entity_confusables"
    assert receipt["owner_proposals"][0]["resulting_owner_revision"] is None
    # Correction provenance cannot deserialize as subtitle fact evidence.
    value = asdict(sample[1])
    value["evidence"][0]["provenance_kind"] = "CORRECTION_RECORD"
    with pytest.raises(ValueError): pf._decode(value, pf.ProposedFact)


def test_native_corrections_keep_direction_source_interval_and_only_final_surface(sample):
    sealed = update_audit(sample[0], exact_final_cpa_self_heal={"passes": [{"repairs": [
        {"action": "REPLACE_CUE_TEXT", "before": "我喜欢山歌", "after": "我喜欢唱歌", "cue_index": 1,
         "matched_start_ms": 1000, "matched_end_ms": 2000, "finding_sha256": "f" * 64},
        {"action": "REPLACE_CUE_TEXT", "before": "中间误修", "after": "已经不在最终字幕", "cue_index": 1,
         "matched_start_ms": 1000, "matched_end_ms": 2000, "finding_sha256": "e" * 64}]}]})
    observations = ex.observations_from_corrections(sealed, source_offset_ms=20000)
    assert len(observations) == 1
    assert (observations[0].source_start_ms, observations[0].source_end_ms) == (21000, 22000)
    assert observations[0].heard_surface == "我喜欢山歌" and observations[0].resolved_canonical == "我喜欢唱歌"


def test_idempotent_readonly_and_summary(sample, tmp_path):
    sealed, p, ledger, sources = sample
    before = {f: f.read_bytes() for f in sealed.final_srt_path.parent.iterdir()}
    count = []
    def fake(s):
        count.append(s)
        return (p,)
    args = dict(sealed=sealed, ledger=ledger, sources=sources, extractor=fake,
                output_dir=tmp_path / "review", extractor_id="fake", extractor_version="v1")
    batch = ex.run_extraction(**args)
    snapshots = {f: (f.read_bytes(), f.stat().st_mtime_ns) for f in batch.iterdir()}
    assert ex.run_extraction(**args) == batch
    assert len(count) == 1
    assert snapshots == {f: (f.read_bytes(), f.stat().st_mtime_ns) for f in batch.iterdir()}
    assert before == {f: f.read_bytes() for f in sealed.final_srt_path.parent.iterdir()}
    summary = (batch / "review-summary.md").read_text()
    assert "说话人未经声纹证实" in summary and "BV_test" in summary and "我喜欢唱歌" in summary
    queue = [json.loads(line) for line in (batch / "channel_knowledge_review_queue.jsonl").read_text().splitlines()]
    assert {row["status"] for row in queue} == {"PROPOSED"}
    assert not list(tmp_path.rglob("runner.lock"))
    with pytest.raises(ValueError, match="overlaps"):
        ex.run_extraction(**dict(args, output_dir=sealed.final_srt_path.parent))


def test_cli_normal_entry_and_whitelist(sample, tmp_path):
    sealed, p, ledger, sources = sample
    raw_sealed = asdict(sealed)
    raw_sources = asdict(sources)
    for raw in (raw_sealed, raw_sources):
        for k, value in raw.items():
            if isinstance(value, Path): raw[k] = str(value)
    bundle = write(tmp_path / "bundle.json", {"schema_version": "persona-extraction-input.v1", "sealed": raw_sealed, "sources": raw_sources})
    ledger_path = write(tmp_path / "ledger.json", asdict(ledger))
    fake_path = write(tmp_path / "fake.json", [asdict(p)])
    command = [sys.executable, "-B", str(ROOT / "scripts/extract_persona_facts.py"), "--bundle", str(bundle),
        "--ledger", str(ledger_path), "--profile-id", "example", "--output-dir", str(tmp_path / "review"),
        "--proposals-json", str(fake_path)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).name == sealed.final_srt_sha256
    assert subprocess.run(command, capture_output=True).returncode == 0
    data = json.loads(bundle.read_bytes())
    data["sealed"]["hook"] = "不该进入抽取器"
    write(bundle, data)
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 2 and "non-whitelisted" in result.stderr


def test_changed_context_rescreens_cached_extraction(sample, tmp_path):
    calls = []
    def fake(s):
        calls.append(s)
        return (sample[1],)
    args = dict(sealed=sample[0], ledger=sample[2], sources=sample[3], extractor=fake,
        output_dir=tmp_path / "review", extractor_id="fake", extractor_version="v1")
    batch = ex.run_extraction(**args)
    multi(sample)
    ex.run_extraction(**args)
    assert len(calls) == 1
    receipt = json.loads((batch / "extraction-receipt.json").read_bytes())
    assert receipt["outcomes"][0]["outcome"] == "SCREENED_OUT"
    assert not (batch / "channel_knowledge_review_queue.jsonl").read_text()
    # Missing output files are reconstructed from the cache as well.
    (batch / "review-summary.md").unlink()
    ex.run_extraction(**args)
    assert (batch / "review-summary.md").exists() and len(calls) == 1


def test_changed_source_removes_cached_proposals_without_calling_provider(sample, tmp_path):
    calls = []
    def fake(s):
        calls.append(s)
        return (sample[1],)
    args = dict(sealed=sample[0], ledger=sample[2], sources=sample[3], extractor=fake,
        output_dir=tmp_path / "review", extractor_id="fake", extractor_version="v1")
    batch = ex.run_extraction(**args)
    sample[0].final_srt_path.write_bytes(sample[0].final_srt_path.read_bytes() + b"\n")
    ex.run_extraction(**args)
    assert len(calls) == 1
    receipt = json.loads((batch / "extraction-receipt.json").read_bytes())
    assert "SEALED_SRT_SHA_MISMATCH" in receipt["outcomes"][0]["reasons"]
    assert not (batch / "channel_knowledge_review_queue.jsonl").read_text()





def test_real_published_sealed_fixture_via_cli(tmp_path):
    fixture = ROOT / "tests/fixtures/persona_facts"
    bundle = fixture / "published-bundle.json"
    if not bundle.exists():
        pytest.skip("Channel-private historical fixture is stripped from OSS")
    provenance = json.loads((fixture / "provenance.json").read_bytes())
    assert ex.digest((fixture / "published.srt").read_bytes()) == provenance["srt_sha256"]
    assert ex.digest((fixture / "published-final-review.json").read_bytes()) == provenance["audit_sha256"]
    assert ex.digest((fixture / "source-geometry.json").read_bytes()) == provenance["source_geometry_sha256"]
    command = [sys.executable, "-B", str(ROOT / "scripts/extract_persona_facts.py"), "--bundle", str(bundle),
        "--ledger", str(ROOT / "assets/lidousha/persona_facts.v1.json"), "--profile-id", "lidousha",
        "--output-dir", str(tmp_path / "review"), "--proposals-json", str(fixture / "fake-proposals.json")]
    before = {p: p.read_bytes() for p in fixture.iterdir() if p.is_file()}
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    batch = Path(result.stdout.strip())
    receipt = json.loads((batch / "extraction-receipt.json").read_bytes())
    assert receipt["outcomes"][0]["outcome"] == "PROPOSED", receipt["outcomes"]
    assert receipt["proposals"][0]["evidence"][0]["bvid"] == "BV1PXeT6SEd3"
    geometry = json.loads((fixture / "source-geometry.json").read_bytes())
    assert receipt["proposals"][0]["evidence"][0]["source_start_ms"] == geometry["final_recut"]["absolute_source_start_ms"] + 7650
    assert "说话人未经声纹证实" in (batch / "review-summary.md").read_text()
    queue = [json.loads(line) for line in (batch / "channel_knowledge_review_queue.jsonl").read_text().splitlines()]
    assert len([row for row in queue if row.get("target") == "persona_facts"]) == 1
    assert all(row["status"] == "PROPOSED" for row in queue)
    assert before == {p: p.read_bytes() for p in fixture.iterdir() if p.is_file()}


def test_new_module_strict_typecheck(tmp_path):
    python = Path("/home/user/vtuber-ci/venv-typecheck/bin/python")
    if not python.exists():
        pytest.skip("Strict type-check environment is installed on OCI3")
    result = subprocess.run([str(python), "-m", "mypy", "--config-file=", "--python-version=3.11",
        "--strict", "--follow-imports=silent", "--cache-dir", str(tmp_path / "mypy-cache"),
        "src/autoslice/persona_facts.py", "src/autoslice/persona_fact_extraction.py"],
        cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
