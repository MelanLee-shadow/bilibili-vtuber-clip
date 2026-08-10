"""F21（Ivan 2026-08-10 直令）：AGY 缺席时声学证人 fall back 到 Gemini key。

Ivan 8/10：「wsl 上有 gemini key，没有 AGY 当然要 fall back 到 gemini key」。
病灶是纯接线，不是缺 provider：``entity_audio_verifier`` 里 AGY→免费 3 key→
政策门控付费 backup 的链 7/25 起就在，但

1. ``producer_text_pipeline`` 的 host 门在 ``--ssh-host`` 非 localhost 时直接
   把 ``audio_entity_verifier`` 置 None（wsl 产线恒中），链根本没被构造；
2. 没有 next_verifier 时 ``read_aloud_llm_verifier._defer`` 下传裸 ``None``，
   ``microcue_acoustic_discovery`` 对它做 ``dict(None)`` 抛 TypeError。

两条金丝雀分别钉死这两点；第三组钉死 F21 新开的 typed UNCERTAIN 尾巴**不得**
继承既有的无声学改字权（8/8 F7 张力，默认关死待 Ivan 复裁）。

密闭性：AGY 一律走假 ``subprocess.run``/不存在的二进制，Gemini 一律 mock 在
``entity_audio_verifier._gemini_api_observe_witness``（使用方模块自己的 seam），
并显式清掉 ambient ``GEMINI_API_KEY*`` / ``GEMINI_KEY_BACKUP``——开发机 shell
带着真 key 时，未 mock 的用例会真打 Google。
"""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest

from src.autoslice import entity_audio_verifier as verifier_module
from src.autoslice import microcue_acoustic_discovery as microcue_module
from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice.acoustic_witness_adjudication import (
    adjudicate_with_witness,
    build_witness_request,
)
from src.autoslice.acoustic_witness_availability import (
    AUDIO_VERIFIER_UNAVAILABLE,
    witness_audio_locally_resolvable,
)
from src.autoslice.read_aloud_llm_verifier import build_cpa_read_aloud_verifier

WITNESS_SCHEMA = "subtitle-span-acoustic-witness.v1"


@pytest.fixture(autouse=True)
def _hermetic_gemini(monkeypatch):
    """开发机 shell 里的真 key 绝不能让本文件的用例走真网络。

    conftest 只封死了 CPA 命令通道；Gemini 走 ``urllib``，没人拦。这里双保险：
    清掉 ambient key，并把使用方模块自己的 urlopen seam 直接炸掉——任何漏 mock
    的用例会显式失败，而不是花着 Ivan 的配额变绿。
    """

    for name in (
        "GEMINI_API_KEY",
        "GEMINI_API_KEY_2",
        "GEMINI_API_KEY_3",
        "GEMINI_KEY_BACKUP",
        "GEMINI_PAID_BACKUP_DEV_EXCEPTION",
        "ENTITY_AUDIO_DISABLE_AGY",
    ):
        monkeypatch.delenv(name, raising=False)

    def _blocked(*args, **kwargs):
        raise AssertionError("TEST_HERMETIC_GEMINI_BLOCKED: 单测禁止真打 Gemini")

    monkeypatch.setattr(verifier_module.urllib.request, "urlopen", _blocked)


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _observed_blind_witness(pinyin: str = "hao piao liang o") -> str:
    return json.dumps(
        {
            "schema_version": WITNESS_SCHEMA,
            "status": "OBSERVED",
            "target_audible": True,
            "heard_pinyin": pinyin,
            "uncertain_positions": [],
            "syllable_count": len(pinyin.split()),
            "confidence": 0.94,
            "reason": "clear blind dictation",
        }
    )


def _witness_request(*, evidence_id: str = "c" * 64, start_ms: int = 2_000):
    return build_witness_request(
        {
            "evidence_id": evidence_id,
            "cue_indexes": [21],
            "matched_start_ms": start_ms,
            "matched_end_ms": start_ms + 1_121,
            "context_start_ms": max(0, start_ms - 1_500),
            "context_end_ms": start_ms + 2_621,
            "source_media_timeline_offset_ms": 0,
        }
    )


def _adapters() -> pipeline.TextPipelineAdapters:
    def unused(*args, **kwargs):
        return None

    return pipeline.TextPipelineAdapters(
        build_aggregate_transcriber=unused,
        build_agy_transcriber=unused,
        load_term_boundary_surfaces=unused,
        profile_asset_file=lambda _name: Path("/__vtuber_slice_missing_asset__"),
        review_glossary=lambda: "",
        topic_graph_disabled=lambda: True,
        topic_graph_path=unused,
        topic_graph_expected_sha256=lambda: "",
    )


# --------------------------------------------------------------------------
# 金丝雀 ①：AGY 缺席 + mock Gemini → 证词产出，法官可跑
# --------------------------------------------------------------------------


def test_missing_agy_binary_falls_back_to_gemini_key_witness(tmp_path, monkeypatch):
    """真·二进制缺席（FileNotFoundError）不再是链的终点，只是第一环失败。

    revert ``AGY_BINARY_ABSENT`` 分类不会让本例变红（回执措辞而已），但把
    Gemini fallback 拆掉会——本例证明「没有 agy 也能拿到证词」。
    """

    monkeypatch.setenv("GEMINI_API_KEY", "free-key-1")
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media bytes")
    api_calls: list[dict] = []

    real_run = verifier_module.subprocess.run

    def fake_run(command, **kwargs):
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"cropped witness clip")
            return _Completed()
        # 真实缺席姿势：内核抛 FileNotFoundError，与 wsl 上没有 agy 一致。
        raise FileNotFoundError(2, "No such file or directory", command[0])

    def fake_api(**kwargs):
        api_calls.append(kwargs)
        return _observed_blind_witness()

    monkeypatch.setattr(verifier_module.subprocess, "run", fake_run)
    monkeypatch.setattr(verifier_module, "_gemini_api_observe_witness", fake_api)
    assert real_run is not fake_run

    verify = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=tmp_path / "out",
        recording_date="2026-08-10",
        source_duration_ms=20_000,
        agy_bin=str(tmp_path / "definitely-absent-agy"),
    )

    verdict = verify(_witness_request())

    assert verdict["status"] == "OBSERVED"
    assert verdict["heard_pinyin"] == "hao piao liang o"
    # provider 出处必须能区分 agy / gemini-api / key tier。
    assert verdict["provider"] == "gemini_api"
    assert verdict["key_tier"] == "free"
    assert verdict["model"] == verifier_module.ENTITY_AUDIO_API_MODEL_DEFAULT
    assert len(api_calls) == 1

    failures = json.loads(
        next((tmp_path / "out/entity_verdicts").glob("*/verdict.manifest.json")).read_text(
            encoding="utf-8"
        )
    )["provider_failures"]
    assert failures[0]["provider"] == "agy"
    assert failures[0]["category"] == "AGY_BINARY_ABSENT"

    # 法官拿到的是合法 OBSERVED 证词，可以真的跑闭集裁决。
    repaired, branch, audit = adjudicate_with_witness(
        check_request={
            "schema_version": "subtitle-span-acoustic-check-request.v1",
            "request_sha256": "a" * 64,
            "current_cue": "好漂亮哦",
            "proposed_cue": "好漂亮喔",
        },
        witness=verdict,
        llm_call=lambda prompt: json.dumps(
            {"choice": "CURRENT", "reason": "拼音与当前文本一致"}
        ),
    )
    assert audit["witness_status"] == "OBSERVED"
    assert (repaired, branch) == (False, "JUDGE_KEEPS_CURRENT")


def test_explicit_env_switch_skips_agy_and_uses_gemini_key(tmp_path, monkeypatch):
    """显式关掉 AGY 那一环：链直接从免费 key 起跑，且回执写明未尝试。"""

    monkeypatch.setenv("GEMINI_API_KEY", "free-key-1")
    monkeypatch.setenv(verifier_module.ENTITY_AUDIO_DISABLE_AGY_ENV, "1")
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media bytes")
    agy_calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"cropped witness clip")
            return _Completed()
        agy_calls.append(command)
        return _Completed()

    monkeypatch.setattr(verifier_module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        verifier_module,
        "_gemini_api_observe_witness",
        lambda **kwargs: _observed_blind_witness(),
    )

    verify = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=tmp_path / "out",
        recording_date="2026-08-10",
        source_duration_ms=20_000,
        agy_bin="agy-test",
    )

    verdict = verify(_witness_request(evidence_id="d" * 64))

    assert verdict["provider"] == "gemini_api"
    assert agy_calls == []
    failures = json.loads(
        next((tmp_path / "out/entity_verdicts").glob("*/verdict.manifest.json")).read_text(
            encoding="utf-8"
        )
    )["provider_failures"]
    assert failures[0] == {
        "provider": "agy",
        "category": "AGY_DISABLED_BY_ENV",
        "attempted": False,
    }


def test_remote_host_with_local_padded_still_builds_the_audio_witness(
    tmp_path, monkeypatch
):
    """host 门金丝雀：wsl 姿势（host=free）+ 本地 padded → 证人链必须建起来。

    revert ``witness_audio_locally_resolvable``（改回 ``host in {localhost,
    127.0.0.1}``）本例立刻变红：verify 返回 None 而不是 typed 证词。
    """

    padded = tmp_path / "padded.mp4"
    padded.write_bytes(b"locally pulled padded media")
    out_root = tmp_path / "out"
    out_root.mkdir()
    built: list[dict] = []

    class _FakeVerifier:
        def __call__(self, request):
            built.append(dict(request))
            return {
                "schema_version": WITNESS_SCHEMA,
                "witness_protocol": "blind_pinyin",
                "request_sha256": request["request_sha256"],
                "status": "OBSERVED",
                "target_audible": True,
                "heard_pinyin": "hao piao liang o",
                "uncertain_positions": [],
                "syllable_count": 4,
                "confidence": 0.9,
                "provider": "gemini_api",
                "key_tier": "free",
            }

    monkeypatch.setattr(
        verifier_module,
        "build_local_audio_entity_verifier",
        lambda **kwargs: _FakeVerifier(),
    )

    context = pipeline._build_entity_verification_context(
        spec={"date": "2026-08-10"},
        padded=padded,
        padded_dur=20_000,
        host="free",
        text_override_path=None,
        cid="auto_f21",
        out_root=out_root,
        srt_text="1\n00:00:01,000 --> 00:00:02,000\n好漂亮哦\n",
        authoritative_chat=[],
        adapters=_adapters(),
    )

    verdict = context.verify_confusable_entity(_witness_request(evidence_id="e" * 64))

    assert built, "远端 host + 本地 padded 时声学证人必须被构造并被调用"
    assert verdict["status"] == "OBSERVED"


@pytest.mark.parametrize(
    ("host", "exists", "expected"),
    [
        ("localhost", False, True),
        ("127.0.0.1", False, True),
        ("free", True, True),
        ("free", False, False),
    ],
)
def test_host_gate_predicate_asks_about_local_audio_not_about_ssh(
    tmp_path, host, exists, expected
):
    padded = tmp_path / "padded.mp4"
    if exists:
        padded.write_bytes(b"media")
    assert witness_audio_locally_resolvable(padded, host=host) is expected


def test_host_gate_rejects_zero_byte_padded(tmp_path):
    padded = tmp_path / "padded.mp4"
    padded.write_bytes(b"")
    assert witness_audio_locally_resolvable(padded, host="free") is False


def test_agy_absent_posture_keeps_the_free_then_paid_key_order(tmp_path, monkeypatch):
    """AGY 缺席不改 key 顺序（7/19 裁定）：免费 3 key 轮换满 3 轮 → 付费 backup。

    429 是对已耗尽免费链的确定性快败，同一 run 内连补到政策线；付费那一笔
    必须带 ``free_chain_strikes >= 3`` 的记账戳。
    """

    for ordinal in (1, 2, 3):
        name = "GEMINI_API_KEY" if ordinal == 1 else f"GEMINI_API_KEY_{ordinal}"
        monkeypatch.setenv(name, f"free-key-{ordinal}")
    monkeypatch.setenv("GEMINI_KEY_BACKUP", "paid-key")
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "base"))
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media bytes")
    keys: list[str] = []

    def fake_run(command, **kwargs):
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"cropped witness clip")
            return _Completed()
        raise FileNotFoundError(2, "No such file or directory", command[0])

    def fake_api(**kwargs):
        keys.append(kwargs["key"])
        if kwargs["key"] != "paid-key":
            raise urllib.error.HTTPError(
                "https://example.invalid", 429, "quota", None, None
            )
        return _observed_blind_witness()

    monkeypatch.setattr(verifier_module.subprocess, "run", fake_run)
    monkeypatch.setattr(verifier_module, "_gemini_api_observe_witness", fake_api)

    verify = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=tmp_path / "out",
        recording_date="2026-08-10",
        source_duration_ms=20_000,
        agy_bin=str(tmp_path / "definitely-absent-agy"),
    )
    request = _witness_request(evidence_id="2" * 64)

    verdict = verify(request)

    assert verdict["status"] == "OBSERVED"
    assert keys == ["free-key-1", "free-key-2", "free-key-3"] * 3 + ["paid-key"]
    manifest = json.loads(
        (
            tmp_path
            / "out/entity_verdicts"
            / request["request_sha256"][:20]
            / "verdict.manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["key_tier"] == "paid_backup"
    assert manifest["paid_backup_policy"]["free_chain_strikes"] >= 3
    assert manifest["provider_failures"][0]["category"] == "AGY_BINARY_ABSENT"
    ledger = (
        tmp_path / "base" / "state" / "gemini-paid-backup"
    ).rglob("usage-*.jsonl")
    entries = [
        json.loads(line)
        for path in ledger
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(entries) == 1
    assert entries[0]["purpose"] == "candidate_blind_audio_witness"
    assert "paid-key" not in json.dumps(entries[0])


# --------------------------------------------------------------------------
# 金丝雀 ②：全 provider 竭 → typed UNCERTAIN，绝不裸 None
# --------------------------------------------------------------------------


def test_no_acoustic_provider_yields_typed_uncertain_not_none():
    """revert ``_defer`` 的 typed 尾巴（改回 ``return None``）本例立刻变红。"""

    verify = build_cpa_read_aloud_verifier(None, next_verifier=None)
    request = _witness_request(evidence_id="f" * 64)

    verdict = verify(request)

    assert verdict is not None
    assert verdict["schema_version"] == WITNESS_SCHEMA
    assert verdict["status"] == "UNCERTAIN"
    assert verdict["reason_code"] == AUDIO_VERIFIER_UNAVAILABLE
    assert verdict["witness_protocol"] == "blind_pinyin"
    assert verdict["request_sha256"] == request["request_sha256"]


def test_microcue_discovery_survives_a_none_returning_verifier():
    """``dict(None)`` 曾把「根本没有证人」伪装成 MICROCUE_AUDIO_VERIFIER_ERROR。"""

    srt = "1\n00:00:01,000 --> 00:00:01,900\n好漂亮哦\n"

    findings, receipt = microcue_module.discover_microcue_findings(
        srt,
        timeline_offset_ms=0,
        entity_verifier=lambda request: None,
    )

    assert findings == []
    witnesses = [row["witness"] for row in receipt["eligible"]]
    assert witnesses, "至少要有一条被检查的微线索"
    for witness in witnesses:
        assert witness["reason_code"] == AUDIO_VERIFIER_UNAVAILABLE
        assert witness["schema_version"] == WITNESS_SCHEMA
    assert receipt["uncertain_count"] == len(witnesses)


def test_all_gemini_keys_exhausted_still_returns_typed_uncertain(
    tmp_path, monkeypatch
):
    """AGY 缺席 + 一把 key 都没有 → schema 合法的 UNCERTAIN，含 request_sha256。"""

    source = tmp_path / "source.mp4"
    source.write_bytes(b"media bytes")

    def fake_run(command, **kwargs):
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"cropped witness clip")
            return _Completed()
        raise FileNotFoundError(2, "No such file or directory", command[0])

    def never_called(**kwargs):  # pragma: no cover - 无 key 时不该被调用
        raise AssertionError("no configured key may reach the network")

    monkeypatch.setattr(verifier_module.subprocess, "run", fake_run)
    monkeypatch.setattr(verifier_module, "_gemini_api_observe_witness", never_called)

    verify = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=tmp_path / "out",
        recording_date="2026-08-10",
        source_duration_ms=20_000,
        agy_bin=str(tmp_path / "definitely-absent-agy"),
    )
    request = _witness_request(evidence_id="1" * 64)

    verdict = verify(request)

    assert verdict["schema_version"] == WITNESS_SCHEMA
    assert verdict["status"] == "UNCERTAIN"
    assert verdict["request_sha256"] == request["request_sha256"]
    assert verdict["reason_code"] == "ENTITY_AUDIO_PROVIDER_FAILED"


# --------------------------------------------------------------------------
# 张力封口：新尾巴不得继承既有的无声学改字权（8/8 F7），默认关死
# --------------------------------------------------------------------------


def test_never_attempted_witness_cannot_authorize_a_mutation():
    check_request = {
        "schema_version": "subtitle-span-acoustic-check-request.v1",
        "request_sha256": "b" * 64,
        "current_cue": "好漂亮哦",
        "proposed_cue": "好漂亮喔",
    }
    witness = {
        "schema_version": WITNESS_SCHEMA,
        "witness_protocol": "blind_pinyin",
        "request_sha256": "b" * 64,
        "status": "UNCERTAIN",
        "reason_code": AUDIO_VERIFIER_UNAVAILABLE,
    }

    repaired, branch, audit = adjudicate_with_witness(
        check_request=check_request,
        witness=witness,
        llm_call=lambda prompt: json.dumps(
            {"choice": "PROPOSED", "reason": "语境更顺"}
        ),
    )

    assert repaired is False
    assert branch == "WITNESS_NEVER_ATTEMPTED_KEEP_CURRENT_DISCLOSED"
    assert audit["acoustic_witness_never_attempted"] is True
    # 法官仍然真的跑了（披露口径完整），只是不授权改字。
    assert audit["judge"]["choice"] == "PROPOSED"
    assert audit["witness_unavailable_reason"] == AUDIO_VERIFIER_UNAVAILABLE


def test_never_attempted_witness_still_reaches_the_disclosed_keep_current_exit():
    """端到端：没有任何声学 provider 时，法官仍然真的跑完并给出 CURRENT，
    这条 finding 走 ``is_keep_current_disclosed`` 披露出口而不是永久拦死；
    法官若改口 PROPOSED 则退回 blocker（同一测试里对照）。"""

    from src.autoslice.final_review_auditor import adjudicate_context_finding
    from src.autoslice.final_review_contract import is_keep_current_disclosed

    srt = (
        "1\n00:00:01,000 --> 00:00:02,000\n前一句\n\n"
        "2\n00:00:02,000 --> 00:00:03,200\n好漂亮哦\n\n"
        "3\n00:00:03,200 --> 00:00:04,000\n后一句\n"
    )
    finding = {
        "cue_index": 2,
        "kind": "context",
        "suspect": "漂亮",
        "suggestion": "飘亮",
        "proposed_full_cue": "好飘亮哦",
        "repair_class": "phonetic",
        "why": "巡检怀疑",
    }
    no_provider_verifier = build_cpa_read_aloud_verifier(None, next_verifier=None)

    def judge(choice):
        return lambda prompt: json.dumps(
            {
                "choice": choice,
                "ranking": [
                    {"canonical": "好漂亮哦", "p": 0.9},
                    {"canonical": "好飘亮哦", "p": 0.1},
                ],
                "reason": "语境判断",
            }
        )

    _out, kept = adjudicate_context_finding(
        srt,
        finding,
        entity_verifier=no_provider_verifier,
        judge_llm_call=judge("CURRENT"),
    )
    assert kept["verdict"]["reason_code"] == AUDIO_VERIFIER_UNAVAILABLE
    assert kept["status"] == "OBSERVED", "法官必须真的跑完，而不是证词 invalid"
    assert kept["policy_branch"] == "JUDGE_KEEPS_CURRENT"
    assert kept["repaired"] is False
    assert is_keep_current_disclosed({"exact_release_adjudication": kept}) is True

    out, blocked = adjudicate_context_finding(
        srt,
        finding,
        entity_verifier=no_provider_verifier,
        judge_llm_call=judge("PROPOSED"),
    )
    assert "好飘亮哦" not in out, "无声学证人时绝不改字"
    assert blocked["repaired"] is False
    assert blocked["policy_branch"] == "WITNESS_NEVER_ATTEMPTED_KEEP_CURRENT_DISCLOSED"
    assert is_keep_current_disclosed({"exact_release_adjudication": blocked}) is False


def test_provider_really_failed_keeps_the_preexisting_apply_path():
    """既有出口不动：provider 真被调用过并失败，语境证据仍可定夺（7/25 起）。"""

    repaired, branch, _audit = adjudicate_with_witness(
        check_request={
            "schema_version": "subtitle-span-acoustic-check-request.v1",
            "request_sha256": "b" * 64,
            "current_cue": "好漂亮哦",
            "proposed_cue": "好漂亮喔",
        },
        witness={
            "schema_version": WITNESS_SCHEMA,
            "witness_protocol": "blind_pinyin",
            "request_sha256": "b" * 64,
            "status": "UNCERTAIN",
            "reason_code": "ENTITY_AUDIO_PROVIDER_FAILED",
            "detail": "GEMINI_API_QUOTA_EXHAUSTED",
        },
        llm_call=lambda prompt: json.dumps(
            {"choice": "PROPOSED", "reason": "语境更顺"}
        ),
    )

    assert repaired is True
    assert branch == "CPA_JUDGE_APPLY_PROPOSED_WITHOUT_AUDIO_WITNESS"


def test_entity_receipt_shape_survives_the_unified_client_extraction(
    tmp_path, monkeypatch
):
    """④ 逐字节形状回归：抽出 agy_gemini_client 之后回执面必须一模一样。

    真值盲——只断言**形状**（键集合 + provider/model/key_tier 三元组 +
    provider_failures 行的键集合），不对任何转写内容做断言。这条 lane 是
    F21 用 98 份真回执验证过的蓝本，接口重构不许改动它的回执契约。
    """

    monkeypatch.setenv("GEMINI_API_KEY", "free-key-1")
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media bytes")

    def fake_run(command, **_kwargs):
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"cropped witness clip")
            return _Completed()
        raise FileNotFoundError(2, "No such file or directory", command[0])

    monkeypatch.setattr(verifier_module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        verifier_module,
        "_gemini_api_observe_witness",
        lambda **_kw: _observed_blind_witness(),
    )

    verify = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=tmp_path / "out",
        recording_date="2026-08-10",
        source_duration_ms=20_000,
        agy_bin=str(tmp_path / "definitely-absent-agy"),
    )
    verdict = verify(_witness_request())
    manifest = json.loads(
        next((tmp_path / "out/entity_verdicts").glob("*/verdict.manifest.json")).read_text(
            encoding="utf-8"
        )
    )

    # 三元组回执契约（字段名不许改）。
    assert {"provider", "model", "key_tier"} <= verdict.keys()
    assert {"provider", "model", "key_tier", "provider_failures"} <= manifest.keys()
    # AGY 缺席那一行的形状。
    absent_row = manifest["provider_failures"][0]
    assert set(absent_row) == {"provider", "category", "error_type"}
    assert isinstance(manifest["provider_failures"], list)
    # 证词本体的键集合（F21 蓝本形状；只比形状，不看任何转写内容）。
    assert set(verdict) == {
        "audio_clip_sha256",
        "audio_end_ms",
        "audio_start_ms",
        "confidence",
        "heard_pinyin",
        "key_tier",
        "model",
        "prompt_sha256",
        "provider",
        "reason",
        "request_sha256",
        "response_sha256",
        "schema_version",
        "self_count_mismatch",
        "source_media_sha256",
        "status",
        "syllable_count",
        "target_audible",
        "timeline_binding",
        "uncertain_positions",
        "witness_protocol",
    }
