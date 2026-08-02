import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import entity_audio_verifier as verifier_module


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _request():
    return {
        "schema_version": "chat-entity-verification-request.v1",
        "request_sha256": "a" * 64,
        "evidence_id": "b" * 64,
        "matched_start_ms": 2_000,
        "matched_end_ms": 4_000,
        # These locate/audit the outer decision but must never enter AGY's prompt.
        "exact_text": "还没看，怎么有人说有示例乙的风险",
        "matched_audio_text": "还没看怎么有人说有示例乙的风险",
        "candidate_entities": [
            {"canonical": "示例甲", "surfaces": ["示例甲", "示例甲代"], "readings": ["meng xian da"]},
            {"canonical": "示例乙全名", "surfaces": ["示例乙", "示例乙全名"], "readings": ["mujica"]},
        ],
    }


def _seal_request(request):
    payload = dict(request)
    payload.pop("request_sha256", None)
    payload["request_sha256"] = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return payload


def _context_request(*, source_media_timeline_offset_ms=0):
    request = _request()
    request.update(
        {
            "schema_version": "subtitle-span-acoustic-check-request.v1",
            "source_media_timeline_offset_ms": source_media_timeline_offset_ms,
            "context_start_ms": 500,
            "context_end_ms": 7_500,
            "context_before": "一直在说欠了很多首歌",
            "context_after": "还欠两个",
            "candidate_entities": [
                {
                    "candidate_id": "CURRENT",
                    "canonical": "还没有歌杂呢",
                    "surfaces": [],
                    "readings": [],
                },
                {
                    "candidate_id": "PROPOSED",
                    "canonical": "还没有歌债呢",
                    "surfaces": [],
                    "readings": [],
                },
            ],
        }
    )
    return _seal_request(request)


def test_audio_verifier_uses_black_frame_clip_and_neutral_prompt(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source video pixels and audio")
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"black frame plus cropped audio")
            return _Completed()
        job_dir = Path(kwargs["cwd"])
        (job_dir / "verdict.json").write_text(
            json.dumps(
                {
                    "schema_version": "entity-audio-observation.v1",
                    "status": "RESOLVED",
                    "canonical_entity": "示例甲",
                    "heard_syllables": "meng xian da",
                    "confidence": 0.98,
                    "reason": "three distinct syllables",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return _Completed()

    monkeypatch.setattr(verifier_module.subprocess, "run", fake_run)
    verify = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=tmp_path / "out",
        recording_date="2026-07-10",
        source_duration_ms=10_000,
        agy_bin="agy-test",
    )

    verdict = verify(_request())

    assert verdict["status"] == "RESOLVED"
    assert verdict["canonical_entity"] == "示例甲"
    ffmpeg = commands[0]
    assert "color=c=black:s=320x240:r=10" in ffmpeg
    assert ffmpeg[ffmpeg.index("-map") + 1] == "1:v:0"
    job_dir = tmp_path / "out/entity_verdicts" / ("a" * 20)
    prompt = (job_dir / "prompt.md").read_text(encoding="utf-8")
    assert "还没看" not in prompt
    assert "怎么有人说" not in prompt
    assert "viewer chat" in prompt
    assert verdict["source_media_sha256"] != verdict["audio_clip_sha256"]


def test_audio_verifier_low_confidence_is_uncertain(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")

    def fake_run(command, **kwargs):
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"audio")
            return _Completed()
        Path(kwargs["cwd"], "verdict.json").write_text(
            json.dumps(
                {
                    "schema_version": "entity-audio-observation.v1",
                    "status": "RESOLVED",
                    "canonical_entity": "示例甲",
                    "heard_syllables": "unclear",
                    "confidence": 0.79,
                    "reason": "unclear",
                }
            ),
            encoding="utf-8",
        )
        return _Completed()

    monkeypatch.setattr(verifier_module.subprocess, "run", fake_run)
    verify = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=tmp_path / "out",
        recording_date="2026-07-10",
        source_duration_ms=10_000,
        agy_bin="agy-test",
    )

    assert verify(_request())["status"] == "UNCERTAIN"


def test_context_verifier_crops_adjacent_audio_and_exposes_bounded_discourse(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"context audio")
            return _Completed()
        Path(kwargs["cwd"], "verdict.json").write_text(
            json.dumps(
                {
                    "schema_version": "entity-audio-observation.v1",
                    "status": "OBSERVED",
                    "target_audible": True,
                    "heard_syllables": "hai mei you ge zhai ne",
                    "current_fit": "PLAUSIBLE",
                    "proposed_fit": "SUPPORTED",
                    "confidence_current": 0.71,
                    "confidence_proposed": 0.91,
                    "reason": "reduced final is acoustically ambiguous; repeated debt context breaks tie",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return _Completed()

    monkeypatch.setattr(verifier_module.subprocess, "run", fake_run)
    verify = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=tmp_path / "out",
        recording_date="2026-07-15",
        source_duration_ms=10_000,
        agy_bin="agy-test",
    )

    request = _context_request()
    verdict = verify(request)

    assert verdict["status"] == "OBSERVED"
    assert verdict["current_fit"] == "PLAUSIBLE"
    assert verdict["proposed_fit"] == "SUPPORTED"
    ffmpeg = commands[0]
    assert ffmpeg[ffmpeg.index("-ss") + 1] == "0.500"
    assert ffmpeg[ffmpeg.index("-t") + 1] == "7.000"
    prompt = (
        tmp_path
        / "out/entity_verdicts"
        / request["request_sha256"][:20]
        / "prompt.md"
    ).read_text(
        encoding="utf-8"
    )
    assert "一直在说欠了很多首歌" in prompt
    assert "还欠两个" in prompt
    assert "1500 ms" in prompt and "3500 ms" in prompt
    assert "Semantic plausibility must never" in prompt
    assert "INCOMPATIBLE" in prompt
    assert '"current_fit"' in prompt and '"proposed_fit"' in prompt
    assert '"candidate_id": "one exact candidate_id above, or null"' not in prompt
    assert '"canonical_entity": "one exact candidate sentence' not in prompt
    assert "Use RESOLVED only" not in prompt


def test_context_verifier_applies_hash_bound_source_media_timeline_offset(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"correct offset audio")
            return _Completed()
        Path(kwargs["cwd"], "verdict.json").write_text(
            json.dumps(
                {
                    "schema_version": "entity-audio-observation.v1",
                    "status": "OBSERVED",
                    "target_audible": True,
                    "heard_syllables": "jiu qing zuo zai zuo bian de tan",
                    "current_fit": "SUPPORTED",
                    "proposed_fit": "INCOMPATIBLE",
                    "confidence_current": 0.95,
                    "confidence_proposed": 0.05,
                    "reason": "target phrase is audible",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return _Completed()

    monkeypatch.setattr(verifier_module.subprocess, "run", fake_run)
    verify = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=tmp_path / "out",
        recording_date="2026-07-22",
        source_duration_ms=30_000,
        agy_bin="agy-test",
    )
    request = _context_request(source_media_timeline_offset_ms=9_770)
    request.update(
        {
            "matched_start_ms": 250,
            "matched_end_ms": 2_810,
            "context_start_ms": 0,
            "context_end_ms": 3_310,
        }
    )
    request = _seal_request(request)

    verdict = verify(request)

    assert verdict["status"] == "OBSERVED"
    ffmpeg = commands[0]
    assert ffmpeg[ffmpeg.index("-ss") + 1] == "9.770"
    assert ffmpeg[ffmpeg.index("-t") + 1] == "3.310"
    binding = verdict["timeline_binding"]
    assert binding["source_media_timeline_offset_ms"] == 9_770
    assert binding["delivery_local"] == {
        "target_start_ms": 250,
        "target_end_ms": 2_810,
        "context_start_ms": 0,
        "context_end_ms": 3_310,
    }
    assert binding["source_media"] == {
        "target_start_ms": 10_020,
        "target_end_ms": 12_580,
        "crop_start_ms": 9_770,
        "crop_end_ms": 13_080,
    }
    job_dir = (
        tmp_path / "out/entity_verdicts" / request["request_sha256"][:20]
    )
    manifest = json.loads(
        (job_dir / "verdict.manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["timeline_binding"] == binding
    assert manifest["verdict"]["timeline_binding"] == binding


def test_context_verifier_rejects_missing_negative_or_unbound_timeline_offset(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    monkeypatch.setattr(
        verifier_module.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("invalid request must not crop or call provider")
        ),
    )
    verify = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=tmp_path / "out",
        recording_date="2026-07-22",
        source_duration_ms=30_000,
        agy_bin="agy-test",
    )

    missing = _context_request()
    missing.pop("source_media_timeline_offset_ms")
    missing = _seal_request(missing)
    assert verify(missing)["reason_code"] == "ENTITY_AUDIO_TIMELINE_OFFSET_INVALID"

    negative = _context_request()
    negative["source_media_timeline_offset_ms"] = -1
    negative = _seal_request(negative)
    assert verify(negative)["reason_code"] == "ENTITY_AUDIO_TIMELINE_OFFSET_INVALID"

    tampered = _context_request()
    tampered["source_media_timeline_offset_ms"] = 9_770
    assert verify(tampered)["reason_code"] == "ENTITY_AUDIO_REQUEST_HASH_MISMATCH"


def _agy_quota_run(command, **kwargs):
    if command[0] == "ffmpeg":
        Path(command[-1]).write_bytes(b"fake media payload")
        return _Completed()
    return _Completed(returncode=1, stderr="Error: Individual quota reached. Resets in 1h.")


def test_provider_failed_verdict_is_never_served_from_cache(tmp_path, monkeypatch):
    """AGY failure is retried; only a later AGY success becomes cacheable."""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    state = {"agy_available": False, "agy_calls": 0}

    def fake_run(command, **kwargs):
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"fake media payload")
            return _Completed()
        state["agy_calls"] += 1
        if not state["agy_available"]:
            return _Completed(
                returncode=1,
                stderr="Error: temporary AGY backend failure.",
            )
        job_dir = Path(kwargs["cwd"])
        (job_dir / "verdict.json").write_text(
            json.dumps(
                {
                    "schema_version": "entity-audio-observation.v1",
                    "status": "RESOLVED",
                    "canonical_entity": "示例甲",
                    "heard_syllables": "meng xian da",
                    "confidence": 0.97,
                    "reason": "three clear syllables",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return _Completed()

    monkeypatch.setattr(verifier_module.subprocess, "run", fake_run)
    verify = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=tmp_path / "out",
        recording_date="2026-07-10",
        source_duration_ms=10_000,
        agy_bin="agy-test",
    )
    first = verify(_request())
    assert first["reason_code"] == "ENTITY_AUDIO_PROVIDER_FAILED"
    assert state["agy_calls"] == 1

    state["agy_available"] = True
    second = verify(_request())
    assert second["status"] == "RESOLVED", second
    assert second["canonical_entity"] == "示例甲"
    assert second["provider"] == "agy"
    assert state["agy_calls"] == 2

    # A legacy non-AGY manifest with otherwise matching hashes is rejected.
    manifest_path = (
        tmp_path / "out/entity_verdicts" / ("a" * 20) / "verdict.manifest.json"
    )
    legacy = json.loads(manifest_path.read_text(encoding="utf-8"))
    legacy["provider"] = "gemini_api"
    legacy["model"] = "gemini-3.6-flash"
    manifest_path.write_text(json.dumps(legacy), encoding="utf-8")
    third = verify(_request())
    assert third["status"] == "RESOLVED"
    assert third["provider"] == "agy"
    assert state["agy_calls"] == 3

    # The replacement AGY manifest is now reusable; no fourth provider call.
    fourth = verify(_request())
    assert fourth["status"] == "RESOLVED"
    assert state["agy_calls"] == 3


def _blind_witness_request(*, evidence_id: str, start_ms: int):
    from src.autoslice.acoustic_witness_adjudication import (
        build_witness_request,
    )

    return build_witness_request(
        {
            "evidence_id": evidence_id,
            "cue_indexes": [start_ms // 1_000],
            "matched_start_ms": start_ms,
            "matched_end_ms": start_ms + 1_100,
            "context_start_ms": max(0, start_ms - 500),
            "context_end_ms": start_ms + 1_600,
            "source_media_timeline_offset_ms": 0,
        }
    )


def _observed_blind_witness():
    return json.dumps(
        {
            "schema_version": verifier_module.WITNESS_SCHEMA,
            "status": "OBSERVED",
            "target_audible": True,
            "heard_pinyin": "hao piao liang o",
            "uncertain_positions": [],
            "syllable_count": 4,
            "confidence": 0.94,
            "reason": "clear blind dictation",
        }
    )


@pytest.mark.parametrize("quota_returncode", (0, 1))
def test_agy_quota_opens_run_circuit_and_later_cues_go_directly_to_api(
    tmp_path, monkeypatch, quota_returncode
):
    """One explicit per-run quota failure suppresses later per-cue AGY calls."""

    for name in ("GEMINI_API_KEY_2", "GEMINI_API_KEY_3", "GEMINI_KEY_BACKUP"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "free-key-1")
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    agy_calls = []
    api_calls = []

    def fake_run(command, **kwargs):
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(str(command[-1]).encode("utf-8"))
            return _Completed()
        agy_calls.append(command)
        return _Completed(
            returncode=quota_returncode,
            stderr="Error: Individual quota reached. Resets in 1h.",
        )

    def fake_api(**kwargs):
        api_calls.append(kwargs["audio_path"])
        return _observed_blind_witness()

    monkeypatch.setattr(verifier_module.subprocess, "run", fake_run)
    monkeypatch.setattr(verifier_module, "_gemini_api_observe_witness", fake_api)
    verify = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=tmp_path / "out",
        recording_date="2026-07-30",
        source_duration_ms=20_000,
        agy_bin="agy-test",
    )
    first_request = _blind_witness_request(evidence_id="c" * 64, start_ms=2_000)
    second_request = _blind_witness_request(evidence_id="d" * 64, start_ms=6_000)

    first = verify(first_request)
    second = verify(second_request)

    assert first["provider"] == "gemini_api"
    assert second["provider"] == "gemini_api"
    assert len(agy_calls) == 1
    assert len(api_calls) == 2
    first_manifest = json.loads(
        (
            tmp_path
            / "out/entity_verdicts"
            / first_request["request_sha256"][:20]
            / "verdict.manifest.json"
        ).read_text(encoding="utf-8")
    )
    second_manifest = json.loads(
        (
            tmp_path
            / "out/entity_verdicts"
            / second_request["request_sha256"][:20]
            / "verdict.manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert first_manifest["provider_failures"][0] == {
        "provider": "agy",
        "category": "AGY_QUOTA_EXHAUSTED",
        "circuit_breaker": "TRIPPED",
        "attempted": True,
    }
    assert second_manifest["provider_failures"][0] == {
        "provider": "agy",
        "category": "AGY_QUOTA_EXHAUSTED",
        "circuit_breaker": "OPEN",
        "attempted": False,
    }


@pytest.mark.parametrize(
    "first_failure",
    ("ordinary_error", "rate_limited", "timeout", "invalid_verdict"),
)
def test_agy_run_circuit_ignores_non_quota_failures(
    tmp_path, monkeypatch, first_failure
):
    """Only explicit quota exhaustion may suppress a later AGY attempt."""

    for name in ("GEMINI_API_KEY_2", "GEMINI_API_KEY_3", "GEMINI_KEY_BACKUP"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "free-key-1")
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    state = {"agy_calls": 0, "api_calls": 0}

    def fake_run(command, **kwargs):
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(str(command[-1]).encode("utf-8"))
            return _Completed()
        state["agy_calls"] += 1
        if state["agy_calls"] == 1:
            if first_failure == "ordinary_error":
                return _Completed(returncode=1, stderr="temporary backend crash")
            if first_failure == "rate_limited":
                return _Completed(returncode=1, stderr="HTTP 429 rate limit; retry later")
            if first_failure == "timeout":
                raise verifier_module.subprocess.TimeoutExpired(command, timeout=1)
            Path(kwargs["cwd"], "verdict.json").write_text("not-json", encoding="utf-8")
            return _Completed()
        Path(kwargs["cwd"], "verdict.json").write_text(
            _observed_blind_witness(), encoding="utf-8"
        )
        return _Completed()

    def fake_api(**kwargs):
        state["api_calls"] += 1
        return _observed_blind_witness()

    monkeypatch.setattr(verifier_module.subprocess, "run", fake_run)
    monkeypatch.setattr(verifier_module, "_gemini_api_observe_witness", fake_api)
    verify = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=tmp_path / "out",
        recording_date="2026-07-30",
        source_duration_ms=20_000,
        agy_bin="agy-test",
    )

    first = verify(_blind_witness_request(evidence_id="e" * 64, start_ms=2_000))
    second = verify(_blind_witness_request(evidence_id="f" * 64, start_ms=6_000))

    assert first["provider"] == "gemini_api"
    assert second["provider"] == "agy"
    assert state == {"agy_calls": 2, "api_calls": 1}


def test_agy_quota_never_falls_back_to_non_agy_audio_provider(tmp_path, monkeypatch):
    """Production audio is AGY-only; configured API keys must not create a bypass."""
    monkeypatch.setenv("GEMINI_API_KEY", "free-key-1")
    monkeypatch.setenv("GEMINI_KEY_BACKUP", "paid-key")
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    monkeypatch.setattr(verifier_module.subprocess, "run", _agy_quota_run)
    verify = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=tmp_path / "out",
        recording_date="2026-07-10",
        source_duration_ms=10_000,
        agy_bin="agy-test",
    )

    verdict = verify(_request())

    assert verdict["status"] == "UNCERTAIN"
    assert verdict["reason_code"] == "ENTITY_AUDIO_PROVIDER_FAILED"
    job_dir = tmp_path / "out/entity_verdicts" / ("a" * 20)
    failures = json.loads((job_dir / "provider-failures.json").read_text(encoding="utf-8"))
    assert {row["provider"] for row in failures["failures"]} == {"agy"}
    assert not list(job_dir.glob("*gemini-api*"))


def test_candidate_blind_witness_uses_direct_api_after_agy_quota(
    tmp_path, monkeypatch
):
    """The direct key is evidence-only and only reachable for blind pinyin."""

    from src.autoslice.acoustic_witness_adjudication import (
        build_witness_request,
    )

    for name in (
        "GEMINI_API_KEY_2",
        "GEMINI_API_KEY_3",
        "GEMINI_KEY_BACKUP",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "free-key-1")
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "base"))
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    monkeypatch.setattr(verifier_module.subprocess, "run", _agy_quota_run)
    monkeypatch.setattr(
        verifier_module,
        "_gemini_api_observe_witness",
        lambda **kwargs: json.dumps(
            {
                "schema_version": verifier_module.WITNESS_SCHEMA,
                "status": "OBSERVED",
                "target_audible": True,
                "heard_pinyin": "hao piao liang o",
                "uncertain_positions": [],
                "syllable_count": 4,
                "confidence": 0.94,
                "reason": "clear blind dictation",
            }
        ),
    )
    verify = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=tmp_path / "out",
        recording_date="2026-07-26",
        source_duration_ms=10_000,
        agy_bin="agy-test",
    )
    request = build_witness_request(
        {
            "evidence_id": "c" * 64,
            "cue_indexes": [21],
            "matched_start_ms": 2_000,
            "matched_end_ms": 3_121,
            "context_start_ms": 500,
            "context_end_ms": 4_621,
            "source_media_timeline_offset_ms": 0,
        }
    )

    verdict = verify(request)

    assert verdict["status"] == "OBSERVED"
    assert verdict["heard_pinyin"] == "hao piao liang o"
    assert verdict["provider"] == "gemini_api"
    assert verdict["key_tier"] == "free"
    prompt = next((tmp_path / "out/entity_verdicts").glob("*/prompt.gemini-api.md"))
    prompt_text = prompt.read_text(encoding="utf-8")
    assert "attached audio clip" in prompt_text
    assert "好爽哦" not in prompt_text


def test_witness_acoustic_cache_replays_same_audio_without_provider(tmp_path, monkeypatch):
    """成本裁定（Ivan 2026-07-27，3 天 $40 案）：同一段音频的纯听写答案
    与请求文本/几何标识无关——第二次（哪怕 request_sha 不同）必须直接
    命中内容寻址缓存，零 provider 调用；验证照常全跑。失败/UNCERTAIN
    永不入缓存（沿用既有规则）。"""

    from src.autoslice.acoustic_witness_adjudication import (
        build_witness_request,
    )

    source = tmp_path / "base" / "recordings" / "source.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source media bytes")
    agy_calls = []

    def fake_run(command, **kwargs):
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"identical witness clip bytes")
            return _Completed()
        agy_calls.append(command)
        job_dir = Path(kwargs["cwd"])
        (job_dir / "verdict.json").write_text(
            json.dumps(
                {
                    "schema_version": verifier_module.WITNESS_SCHEMA,
                    "status": "OBSERVED",
                    "target_audible": True,
                    "heard_pinyin": "hai mei you ge zhai ne",
                    "uncertain_positions": [],
                    "syllable_count": 6,
                    "confidence": 0.93,
                    "reason": "clear speech",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return _Completed()

    monkeypatch.setattr(verifier_module.subprocess, "run", fake_run)

    def check_request(evidence, start):
        return {
            "evidence_id": evidence,
            "cue_indexes": [3],
            "matched_start_ms": start,
            "matched_end_ms": start + 1_500,
            "context_start_ms": start - 1_000,
            "context_end_ms": start + 2_500,
            "source_media_timeline_offset_ms": 0,
        }

    output_dir = tmp_path / "base" / "out" / "2026-07-25" / "auto_x"
    verify = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=output_dir,
        recording_date="2026-07-25",
        source_duration_ms=600_000,
        agy_bin="agy-test",
    )

    first = verify(build_witness_request(check_request("e" * 64, 10_000)))
    assert first["status"] == "OBSERVED"
    assert first["heard_pinyin"] == "hai mei you ge zhai ne"
    assert len(agy_calls) == 1
    first_job = next((output_dir / "entity_verdicts").iterdir())
    prompt = (first_job / "prompt.md").read_text(encoding="utf-8")
    assert "zhe ge shi he tian yi de lian dong o" not in prompt
    assert "e.g." not in prompt

    # 不同 evidence/几何 → 不同 request_sha，但音频字节相同 → 缓存命中
    second = verify(build_witness_request(check_request("f" * 64, 10_040)))
    assert second["status"] == "OBSERVED"
    assert second["heard_pinyin"] == "hai mei you ge zhai ne"
    assert len(agy_calls) == 1  # 零新 provider 调用

    cache_root = tmp_path / "base" / "cache" / "witness-acoustic"
    cache_entry = next(
        path
        for path in cache_root.rglob("*.json")
        if not path.name.endswith((".prompt.json", ".response.json"))
    )
    cached = json.loads(cache_entry.read_text(encoding="utf-8"))
    assert cached["schema_version"] == "witness-acoustic-cache.v2"
    assert cached["prompt_contract"] == verifier_module.WITNESS_PROMPT_CONTRACT

    # A legacy Gemini API observation must not be replayed as AGY evidence.
    cached["provider"] = "gemini_api"
    cached["model"] = "gemini-3.6-flash"
    cache_entry.write_text(json.dumps(cached), encoding="utf-8")
    third = verify(build_witness_request(check_request("g" * 64, 10_080)))
    assert third["status"] == "OBSERVED"
    assert third["provider"] == "agy"
    assert len(agy_calls) == 2


def test_witness_rejects_legacy_prompt_copy_and_does_not_cache_it(
    tmp_path, monkeypatch
):
    """The exact example copied during the 2026-07-29 incident is poisoned
    evidence, even if it arrives outside the ordinary cache path."""

    from src.autoslice.acoustic_witness_adjudication import (
        build_witness_request,
    )

    source = tmp_path / "base" / "recordings" / "source.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source media bytes")

    def fake_run(command, **kwargs):
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"poison-test witness clip")
            return _Completed()
        job_dir = Path(kwargs["cwd"])
        (job_dir / "verdict.json").write_text(
            json.dumps(
                {
                    "schema_version": verifier_module.WITNESS_SCHEMA,
                    "status": "OBSERVED",
                    "target_audible": True,
                    "heard_pinyin": "zhe ge shi he tian yi de lian dong o",
                    "uncertain_positions": [],
                    "syllable_count": 10,
                    "confidence": 0.99,
                    "reason": "copied prompt sample",
                }
            ),
            encoding="utf-8",
        )
        return _Completed()

    monkeypatch.setattr(verifier_module.subprocess, "run", fake_run)
    output_dir = tmp_path / "base" / "out" / "2026-07-29" / "auto_z"
    verify = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=output_dir,
        recording_date="2026-07-29",
        source_duration_ms=60_000,
        agy_bin="agy-test",
    )
    verdict = verify(
        build_witness_request(
            {
                "evidence_id": "e" * 64,
                "cue_indexes": [64],
                "matched_start_ms": 10_000,
                "matched_end_ms": 11_400,
                "context_start_ms": 9_000,
                "context_end_ms": 12_000,
                "source_media_timeline_offset_ms": 0,
            }
        )
    )

    assert verdict["status"] == "UNCERTAIN"
    assert verdict["reason_code"] == "WITNESS_PROMPT_COPY_DETECTED"
    cache_root = tmp_path / "base" / "cache" / "witness-acoustic"
    assert not cache_root.exists() or not any(cache_root.rglob("*.json"))


def test_witness_acoustic_cache_rejects_pre_contract_v1_entry(tmp_path):
    output_dir = tmp_path / "base" / "out" / "2026-07-29" / "auto_old"
    output_dir.mkdir(parents=True)
    job_dir = output_dir / "entity_verdicts" / "job"
    job_dir.mkdir(parents=True)
    clip_sha = "a" * 64
    entry_path = verifier_module._witness_acoustic_cache_path(
        output_dir, clip_sha
    )
    entry_path.parent.mkdir(parents=True)
    entry_path.write_text(
        json.dumps(
            {
                "schema_version": "witness-acoustic-cache.v1",
                "audio_clip_sha256": clip_sha,
                "observed": {
                    "schema_version": verifier_module.WITNESS_SCHEMA,
                    "status": "OBSERVED",
                    "target_audible": True,
                    "heard_pinyin": "zhe ge shi he tian yi de lian dong o",
                },
            }
        ),
        encoding="utf-8",
    )
    entry_path.with_suffix(".prompt.json").write_text("old prompt")
    entry_path.with_suffix(".response.json").write_text("old response")

    assert (
        verifier_module._serve_witness_acoustic_cache(
            output_dir=output_dir,
            clip_sha256=clip_sha,
            job_dir=job_dir,
            expected_model=verifier_module.ENTITY_AUDIO_MODEL,
        )
        is None
    )


def test_witness_acoustic_cache_never_stores_failures(tmp_path, monkeypatch):
    source = tmp_path / "base" / "recordings" / "source.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source media bytes")

    def fake_run(command, **kwargs):
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"same clip")
            return _Completed()
        return _Completed(returncode=1)

    monkeypatch.setattr(verifier_module.subprocess, "run", fake_run)

    from src.autoslice.acoustic_witness_adjudication import (
        build_witness_request,
    )

    output_dir = tmp_path / "base" / "out" / "2026-07-25" / "auto_y"
    verify = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=output_dir,
        recording_date="2026-07-25",
        source_duration_ms=600_000,
        agy_bin="agy-test",
    )
    verdict = verify(
        build_witness_request(
            {
                "evidence_id": "e" * 64,
                "cue_indexes": [1],
                "matched_start_ms": 5_000,
                "matched_end_ms": 6_000,
                "context_start_ms": 4_000,
                "context_end_ms": 7_000,
                "source_media_timeline_offset_ms": 0,
            }
        )
    )
    assert verdict.get("status") != "OBSERVED"
    cache_root = tmp_path / "base" / "cache" / "witness-acoustic"
    assert not cache_root.exists() or not any(cache_root.rglob("*.json"))
