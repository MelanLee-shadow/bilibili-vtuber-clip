"""F20 真值全所有权快路径（维护者 立项）。

真值盲纪律：这里所有 fixture 都是合成的。真实真值工件只当交付输入，永远
不当软件测试的断言 oracle。
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest

from scripts.materialize_reviewed_speaker_truth_delivery import compile_delivery
from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice.delivery_fast_path import (
    OPERATOR_TEXT_FULL_OWNERSHIP_SCHEMA,
    TRUTH_FULL_OWNERSHIP_SCHEMA,
    discover_priority_findings,
    resolve_operator_text_full_ownership,
    resolve_truth_full_ownership,
    skipped_final_review_audit,
    verify_transcript_entities,
)
from src.autoslice.final_review_auditor import audit_correction_mutation_authority
from src.autoslice.reviewed_subtitle_baseline_registry import (
    load_candidate_reviewed_subtitle_baseline,
)
from tests.test_materialize_reviewed_speaker_truth_delivery import (
    CANDIDATE,
    _fixture,
)


BASELINE_SHA = "ab" * 32
TRUTH_SHA = "cd" * 32
ARBITRATION_SHA = "ef" * 32
SOURCE_SHA = "12" * 32


def _owned_spec() -> dict:
    return {
        "subtitle_redelivery_baseline": {
            "schema_version": "subtitle-redelivery-baseline.v2",
            "mode": "preserve_text_outside_source_truth",
            "exact_interval_replay": True,
            "path": "/tmp/synthetic.reviewed.srt",
            "sha256": BASELINE_SHA,
            "authority": "维护者 synthetic reviewed truth",
            "source_recording_basename": "synthetic-recording.mp4",
            "source_sha256": SOURCE_SHA,
            "absolute_source_start_ms": 1_323_000,
            "absolute_source_end_ms": 1_603_000,
            "truth_full_ownership": {
                "schema_version": "truth-full-ownership-pin.v1",
                "authority": "维护者 synthetic reviewed truth",
                "truth_input": {
                    "path": "reports/synthetic/truth.json",
                    "sha256": TRUTH_SHA,
                },
                "baseline_sha256": BASELINE_SHA,
                "arbitration_receipt_sha256": ARBITRATION_SHA,
                "cue_count": 5,
                "reviewed_override_count": 3,
                "machine_cues": [2, 4],
            },
        }
    }


def _operator_text_owned_spec() -> dict:
    baseline = _owned_spec()["subtitle_redelivery_baseline"]
    baseline.pop("truth_full_ownership")
    baseline["operator_text_full_ownership"] = {
        "schema_version": "operator-reviewed-text-full-ownership-pin.v1",
        "authority": "维护者 exhaustive reviewed subtitle truth",
        "baseline_sha256": BASELINE_SHA,
        "source_srt_sha256": TRUTH_SHA,
        "cue_count": 5,
        "changed_cue_count": 2,
        "speaker_authority": "NOT_CLAIMED_TEXT_ONLY",
    }
    return {"subtitle_redelivery_baseline": baseline}


def test_operator_text_ownership_skips_rewriters_without_claiming_speaker() -> None:
    receipt = resolve_operator_text_full_ownership(_operator_text_owned_spec())

    assert receipt is not None
    assert receipt["schema_version"] == OPERATOR_TEXT_FULL_OWNERSHIP_SCHEMA
    assert receipt["status"] == "OPERATOR_TEXT_FULL_OWNERSHIP"
    coverage = receipt["coverage"]
    assert coverage["reviewed_text_cue_count"] == coverage["cue_count"] == 5
    assert coverage["changed_text_cue_count"] == 2
    assert coverage["speaker_ownership"] == "NOT_CLAIMED_TEXT_ONLY"


@pytest.mark.parametrize(
    "field,value",
    [
        ("baseline_sha256", "99" * 32),
        ("source_srt_sha256", "bad"),
        ("cue_count", 0),
        ("changed_cue_count", 6),
        ("speaker_authority", "UNIFORM_HOST"),
    ],
)
def test_broken_operator_text_pin_falls_back_to_full_chain(field, value) -> None:
    spec = _operator_text_owned_spec()
    spec["subtitle_redelivery_baseline"]["operator_text_full_ownership"][field] = value

    assert resolve_operator_text_full_ownership(spec) is None


def test_full_ownership_receipt_carries_coverage_skips_and_conditions() -> None:
    receipt = resolve_truth_full_ownership(_owned_spec())
    assert receipt is not None
    assert receipt["schema_version"] == TRUTH_FULL_OWNERSHIP_SCHEMA == "truth_full_ownership.v1"
    assert receipt["status"] == "TRUTH_FULL_OWNERSHIP"

    coverage = receipt["coverage"]
    assert coverage["cue_count"] == 5
    assert coverage["reviewed_override_count"] == 3
    assert coverage["machine_speaker_cues"] == [2, 4]
    assert (
        coverage["reviewed_override_count"] + coverage["machine_speaker_cue_count"]
        == coverage["cue_count"]
    )
    proof = coverage["proof"]
    assert proof["truth_input"] == {
        "path": "reports/synthetic/truth.json",
        "sha256": TRUTH_SHA,
    }
    assert proof["baseline_sha256"] == BASELINE_SHA
    assert proof["arbitration_receipt_sha256"] == ARBITRATION_SHA
    assert proof["source_sha256"] == SOURCE_SHA
    assert proof["absolute_source_start_ms"] == 1_323_000
    assert proof["absolute_source_end_ms"] == 1_603_000

    skipped = {row["stage"] for row in receipt["skipped_stages"]}
    assert skipped == {
        "producer_text_pipeline._run_final_review",
        "chat_repair.apply_audio_entity_verification",
        "microcue_acoustic_discovery.discover_microcue_findings",
        "restatement_recall.merge_restatement_priority_findings",
    }
    assert all(row["reason_code"] for row in receipt["skipped_stages"])
    # 把关侧必须原样列在保留清单里：终审、边界评审、owner 校验、烧录。
    preserved = set(receipt["preserved_stages"])
    assert "producer_text_pipeline._run_exact_final_release_review" in preserved
    assert "boundary_semantic_review.review_final_boundary_semantics" in preserved
    assert "producer_package_finalization._verify_final_authority" in preserved
    assert len(receipt["effective_conditions"]) >= 4


def test_fully_labelled_truth_uses_typed_no_arbitration_disposition() -> None:
    spec = _owned_spec()
    pin = spec["subtitle_redelivery_baseline"]["truth_full_ownership"]
    pin["reviewed_override_count"] = pin["cue_count"]
    pin["machine_cues"] = []
    pin.pop("arbitration_receipt_sha256")
    pin["arbitration_disposition"] = {
        "schema_version": "reviewed-speaker-arbitration-disposition.v1",
        "status": "NOT_REQUIRED_FULLY_LABELLED",
        "truth_input_sha256": TRUTH_SHA,
        "automatic_labelled_srt_sha256": "ee" * 32,
    }

    receipt = resolve_truth_full_ownership(spec)

    assert receipt is not None
    proof = receipt["coverage"]["proof"]
    assert proof["arbitration_receipt_sha256"] is None
    assert proof["arbitration_disposition"]["status"] == ("NOT_REQUIRED_FULLY_LABELLED")


@pytest.mark.parametrize("field", ["truth_input_sha256", "automatic_labelled_srt_sha256"])
def test_fully_labelled_no_arbitration_disposition_is_hash_bound(field) -> None:
    spec = _owned_spec()
    pin = spec["subtitle_redelivery_baseline"]["truth_full_ownership"]
    pin["reviewed_override_count"] = pin["cue_count"]
    pin["machine_cues"] = []
    pin.pop("arbitration_receipt_sha256")
    pin["arbitration_disposition"] = {
        "schema_version": "reviewed-speaker-arbitration-disposition.v1",
        "status": "NOT_REQUIRED_FULLY_LABELLED",
        "truth_input_sha256": TRUTH_SHA,
        "automatic_labelled_srt_sha256": "ee" * 32,
    }
    pin["arbitration_disposition"][field] = "bad"

    assert resolve_truth_full_ownership(spec) is None


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda spec: spec.pop("subtitle_redelivery_baseline"), id="no_baseline"),
        pytest.param(
            lambda spec: spec["subtitle_redelivery_baseline"].pop("truth_full_ownership"),
            id="no_pin",
        ),
        pytest.param(
            lambda spec: spec["subtitle_redelivery_baseline"].update(exact_interval_replay=False),
            id="not_exact_replay",
        ),
        pytest.param(
            lambda spec: spec["subtitle_redelivery_baseline"].update(
                schema_version="subtitle-redelivery-baseline.v1"
            ),
            id="v1_baseline",
        ),
        pytest.param(
            lambda spec: spec["subtitle_redelivery_baseline"].update(authority=""),
            id="baseline_authority_blank",
        ),
        pytest.param(
            lambda spec: spec.update(subtitle_text_overrides="overrides.json"),
            id="text_override_present",
        ),
        pytest.param(
            lambda spec: spec["subtitle_redelivery_baseline"]["truth_full_ownership"].update(
                schema_version="truth-full-ownership-pin.v2"
            ),
            id="pin_schema_drift",
        ),
        pytest.param(
            lambda spec: spec["subtitle_redelivery_baseline"]["truth_full_ownership"].update(
                baseline_sha256="99" * 32
            ),
            id="pin_baseline_sha_drift",
        ),
        pytest.param(
            lambda spec: spec["subtitle_redelivery_baseline"]["truth_full_ownership"].update(
                reviewed_override_count=2
            ),
            id="coverage_gap",
        ),
        pytest.param(
            lambda spec: spec["subtitle_redelivery_baseline"]["truth_full_ownership"].update(
                machine_cues=[2, 2]
            ),
            id="duplicate_machine_cue",
        ),
        pytest.param(
            lambda spec: spec["subtitle_redelivery_baseline"]["truth_full_ownership"].update(
                machine_cues=[2, 99]
            ),
            id="machine_cue_out_of_range",
        ),
        pytest.param(
            lambda spec: spec["subtitle_redelivery_baseline"]["truth_full_ownership"].update(
                truth_input={"path": "reports/t.json", "sha256": "short"}
            ),
            id="truth_sha_invalid",
        ),
        pytest.param(
            lambda spec: spec["subtitle_redelivery_baseline"]["truth_full_ownership"].update(
                truth_input={"path": "", "sha256": TRUTH_SHA}
            ),
            id="truth_path_blank",
        ),
        pytest.param(
            lambda spec: spec["subtitle_redelivery_baseline"]["truth_full_ownership"].update(
                arbitration_receipt_sha256=""
            ),
            id="arbitration_receipt_missing",
        ),
        pytest.param(
            lambda spec: spec["subtitle_redelivery_baseline"]["truth_full_ownership"].update(
                authority="  "
            ),
            id="pin_authority_blank",
        ),
    ],
)
def test_any_broken_pin_falls_back_to_the_full_chain(mutate) -> None:
    spec = _owned_spec()
    mutate(spec)
    assert resolve_truth_full_ownership(spec) is None


def test_compiled_manifest_pin_is_accepted_through_the_registry(
    tmp_path: Path,
) -> None:
    """编译器盖的 pin 必须能一路走到生产判定点（合成真值，非真实工件）。"""

    compiled = compile_delivery(**_fixture(tmp_path))
    root = tmp_path / "reviewed_subtitle_baselines"
    root.mkdir()
    (root / f"{CANDIDATE}.reviewed.srt").write_text(str(compiled["baseline_srt"]), encoding="utf-8")
    manifest = dict(compiled["baseline_manifest"])
    (root / f"{CANDIDATE}.subtitle-baseline.v1.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )

    loaded = load_candidate_reviewed_subtitle_baseline(root, CANDIDATE)
    assert loaded is not None
    receipt = resolve_truth_full_ownership({"subtitle_redelivery_baseline": loaded.config})
    assert receipt is not None
    coverage = receipt["coverage"]
    assert coverage["cue_count"] == 4
    assert coverage["machine_speaker_cues"] == [3]
    assert coverage["proof"]["baseline_sha256"] == loaded.config["sha256"]


def test_v1_compiled_manifest_stays_on_the_full_chain(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["baseline_schema_version"] = "subtitle-redelivery-baseline.v1"
    manifest = dict(compile_delivery(**fixture)["baseline_manifest"])
    manifest["path"] = "/tmp/whatever.srt"
    assert resolve_truth_full_ownership({"subtitle_redelivery_baseline": manifest}) is None


def _exploding_verifier(_request):
    raise AssertionError("acoustic witness must not be requested under truth ownership")


def test_transcript_entity_witness_is_skipped_and_disclosed() -> None:
    receipt = resolve_truth_full_ownership(_owned_spec())
    srt = "1\n00:00:00,000 --> 00:00:01,000\n李豆沙说话\n"
    text, audit = verify_transcript_entities(
        srt,
        referent_groups=[],
        entity_verifier=_exploding_verifier,
        truth_full_ownership=receipt,
    )
    assert text == srt
    assert audit["status"] == "SKIPPED_TRUTH_FULL_OWNERSHIP"
    assert audit["reason_code"] == "TRANSCRIPT_ENTITY_SURFACE_OWNED_BY_TRUTH"
    assert audit["repairs"] == [] and audit["entity_verdict_required"] == []
    assert audit["truth_full_ownership"]["schema_version"] == TRUTH_FULL_OWNERSHIP_SCHEMA
    # 硬拦口径不变：跳过后的状态绝不能是「实体裁决缺失」。
    assert audit["status"] != "ENTITY_VERDICT_REQUIRED"
    # 键面必须与真实仲裁 audit 同构，否则下游读者要认识这条快路径。
    _text, real_audit = verify_transcript_entities(
        srt, referent_groups=[], entity_verifier=None, truth_full_ownership=None
    )
    assert set(real_audit) - set(audit) == set()
    assert audit["schema_version"] == real_audit["schema_version"]


def test_transcript_entity_witness_still_runs_without_ownership() -> None:
    srt = "1\n00:00:00,000 --> 00:00:01,000\n李豆沙说话\n"
    _text, audit = verify_transcript_entities(
        srt, referent_groups=[], entity_verifier=None, truth_full_ownership=None
    )
    assert audit["status"] != "SKIPPED_TRUTH_FULL_OWNERSHIP"


def test_priority_discovery_is_skipped_without_touching_the_witness(
    tmp_path: Path,
) -> None:
    receipt = resolve_truth_full_ownership(_owned_spec())
    srt = "1\n00:00:00,000 --> 00:00:00,400\n哦\n\n2\n00:00:00,400 --> 00:00:02,000\n行吧\n"
    findings, audit = discover_priority_findings(
        srt,
        timeline_offset_ms=1_323_000,
        entity_verifier=_exploding_verifier,
        out_root=tmp_path,
        cid="synthetic",
        truth_full_ownership=receipt,
    )
    assert findings == []
    assert audit["status"] == "PASS"
    assert audit["reason_code"] == "PRIORITY_DISCOVERY_SKIPPED_TRUTH_FULL_OWNERSHIP"
    assert audit["mutation_authorized"] is False
    assert audit["truth_full_ownership"]["schema_version"] == TRUTH_FULL_OWNERSHIP_SCHEMA
    # 重述候选注入也随之停手：它的 sidecar 回执不该被写出来。
    assert not (tmp_path / "synthetic.restatement-candidates.json").exists()


def test_priority_discovery_still_runs_without_ownership(tmp_path: Path) -> None:
    srt = "1\n00:00:00,000 --> 00:00:00,400\n哦\n\n2\n00:00:00,400 --> 00:00:02,000\n行吧\n"
    _findings, audit = discover_priority_findings(
        srt,
        timeline_offset_ms=0,
        entity_verifier=None,
        out_root=tmp_path,
        cid="synthetic",
        truth_full_ownership=None,
    )
    assert audit["reason_code"] != "PRIORITY_DISCOVERY_SKIPPED_TRUTH_FULL_OWNERSHIP"
    assert (tmp_path / "synthetic.restatement-candidates.json").exists()


def test_skipped_final_review_audit_keeps_the_reviewer_contract_shape() -> None:
    audit = skipped_final_review_audit(
        "SKIPPED_TRUTH_FULL_OWNERSHIP", truth_full_ownership={"a": 1}
    )
    assert audit["schema_version"] == "final-review-audit.v1"
    assert audit["findings"] == []
    assert audit["applied_count"] == 0
    assert audit["truth_full_ownership"] == {"a": 1}


@pytest.mark.parametrize(
    "resolver,spec_factory",
    [
        (resolve_truth_full_ownership, _owned_spec),
        (resolve_operator_text_full_ownership, _operator_text_owned_spec),
    ],
)
def test_exact_release_accepts_zero_mutation_full_ownership_skip(resolver, spec_factory) -> None:
    ownership = resolver(spec_factory())
    assert ownership is not None
    correction = skipped_final_review_audit(
        "SKIPPED_TRUTH_FULL_OWNERSHIP", truth_full_ownership=ownership
    )

    mutation = audit_correction_mutation_authority(correction)

    assert mutation == {
        "schema_version": "subtitle-correction-mutation-audit.v1",
        "status": "PASS",
        "applied_count": 0,
        "validated_mutation_count": 0,
        "failures": [],
        "truth_full_ownership_skip": ownership,
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda audit: audit.update(applied_count=1),
        lambda audit: audit.update(findings=[{"routed": "disclosure"}]),
        lambda audit: audit["truth_full_ownership"].pop("coverage"),
        lambda audit: audit["truth_full_ownership"]["coverage"].update(cue_count=0),
        lambda audit: audit["truth_full_ownership"]["coverage"].update(
            cue_count=1,
            reviewed_text_cue_count=True,
            changed_text_cue_count=1,
        ),
        lambda audit: audit["truth_full_ownership"]["coverage"]["proof"].update(
            baseline_sha256="bad"
        ),
        lambda audit: audit["truth_full_ownership"]["skipped_stages"].append(
            dict(audit["truth_full_ownership"]["skipped_stages"][0])
        ),
    ],
)
def test_exact_release_rejects_unbound_or_nonzero_full_ownership_skip(mutate) -> None:
    ownership = resolve_operator_text_full_ownership(_operator_text_owned_spec())
    assert ownership is not None
    correction = skipped_final_review_audit(
        "SKIPPED_TRUTH_FULL_OWNERSHIP", truth_full_ownership=ownership
    )
    mutate(correction)

    mutation = audit_correction_mutation_authority(correction)

    assert mutation["status"] == "BLOCK"


def test_exact_release_malformed_machine_cues_fail_closed_without_crashing() -> None:
    ownership = resolve_truth_full_ownership(_owned_spec())
    assert ownership is not None
    ownership["coverage"]["machine_speaker_cues"] = [{}]
    correction = skipped_final_review_audit(
        "SKIPPED_TRUTH_FULL_OWNERSHIP", truth_full_ownership=ownership
    )

    mutation = audit_correction_mutation_authority(correction)

    assert mutation["status"] == "BLOCK"


def test_truth_ownership_branch_skips_reviewer_without_touching_gates() -> None:
    source = inspect.getsource(pipeline.run_text_pipeline)
    tree = ast.parse(source)
    branch = None
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "truth_ownership is not None" in ast.unparse(node.test):
            branch = node
            break
    assert branch is not None, "F20 branch missing from run_text_pipeline"
    then_src = "\n".join(ast.unparse(row) for row in branch.body)
    else_src = "\n".join(ast.unparse(row) for row in branch.orelse)
    assert "_run_final_review" not in then_src
    assert "SKIPPED_TRUTH_FULL_OWNERSHIP" in then_src
    assert "truth_ownership" in then_src
    assert "_run_final_review" in else_src
    # 快路径不得越权：终审与边界评审必须留在无条件路径上。
    assert "_run_exact_final_release_review" not in then_src
    assert "review_final_boundary_semantics" not in then_src


def _keyword_argument_names(source: str, callee: str) -> dict[str, str]:
    """Return ``{keyword: unparsed value}`` for one call inside ``source``."""

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and ast.unparse(node.func) == callee:
            return {
                str(keyword.arg): ast.unparse(keyword.value)
                for keyword in node.keywords
                if keyword.arg
            }
    return {}


def test_ownership_reaches_every_wired_skip_site() -> None:
    """接线金丝雀：判定值必须真的到达每个跳过点，否则快路径只是装饰。"""

    pipeline_source = inspect.getsource(pipeline.run_text_pipeline)
    pipeline_tree = ast.parse(pipeline_source)
    ownership_assignments = [
        node
        for node in ast.walk(pipeline_tree)
        if isinstance(node, ast.Assign)
        and any(ast.unparse(target) == "truth_ownership" for target in node.targets)
    ]
    assert len(ownership_assignments) == 1
    assert ast.unparse(ownership_assignments[0].value) == (
        "resolve_truth_full_ownership(spec) or resolve_operator_text_full_ownership(spec)"
    )
    assert (
        _keyword_argument_names(pipeline_source, "_apply_entity_authority").get(
            "truth_full_ownership"
        )
        == "truth_ownership"
    )
    assert (
        _keyword_argument_names(pipeline_source, "discover_priority_findings").get(
            "truth_full_ownership"
        )
        == "truth_ownership"
    )
    authority_source = inspect.getsource(pipeline._apply_entity_authority)
    assert (
        _keyword_argument_names(authority_source, "verify_transcript_entities").get(
            "truth_full_ownership"
        )
        == "truth_full_ownership"
    )
