import io
import hashlib
import json
from pathlib import Path

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
        "exact_text": "还没看，怎么有人说有母鸡卡的风险",
        "matched_audio_text": "还没看怎么有人说有母鸡卡的风险",
        "candidate_entities": [
            {"canonical": "梦限大", "surfaces": ["梦限大", "梦现代"], "readings": ["meng xian da"]},
            {"canonical": "Ave Mujica", "surfaces": ["Mujica", "母鸡卡"], "readings": ["mujica"]},
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
                    "canonical_entity": "梦限大",
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
    assert verdict["canonical_entity"] == "梦限大"
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
                    "canonical_entity": "梦限大",
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


class _FakeApiResponse:
    def __init__(self, observation):
        body = json.dumps(
            {"candidates": [{"content": {"parts": [{"text": json.dumps(observation, ensure_ascii=False)}]}}]}
        )
        self._stream = io.StringIO(body)

    def read(self, *args, **kwargs):
        return self._stream.read(*args, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeApiResponseRaw:
    def __init__(self, payload):
        self._stream = io.StringIO(json.dumps(payload, ensure_ascii=False))

    def read(self, *args, **kwargs):
        return self._stream.read(*args, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _resolved_observation():
    return {
        "schema_version": "entity-audio-observation.v1",
        "status": "RESOLVED",
        "canonical_entity": "梦限大",
        "heard_syllables": "meng xian da",
        "confidence": 0.97,
        "reason": "three clear syllables",
    }


def _isolate_policy_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "policy-base"))
    for name in (
        "GEMINI_API_KEY",
        "GEMINI_API_KEY_2",
        "GEMINI_API_KEY_3",
        "GEMINI_KEY_BACKUP",
        "GEMINI_PAID_BACKUP_DEV_EXCEPTION",
        "GEMINI_PAID_BACKUP_DAILY_CAP",
        "ENTITY_AUDIO_GEMINI_API_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_provider_failed_verdict_is_never_served_from_cache(tmp_path, monkeypatch):
    """2026-07-14 配额期中毒实证：断供期的 PROVIDER_FAILED 判决被 manifest
    固化后，重试必须重听而不是永远命中缓存。"""
    _isolate_policy_env(monkeypatch, tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "free-key-1")
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    monkeypatch.setattr(verifier_module.subprocess, "run", _agy_quota_run)

    api_calls = []

    def failing_urlopen(request, timeout=0):
        api_calls.append(1)
        raise TimeoutError("quota outage")

    monkeypatch.setattr(verifier_module.urllib.request, "urlopen", failing_urlopen)
    verify = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=tmp_path / "out",
        recording_date="2026-07-10",
        source_duration_ms=10_000,
        agy_bin="agy-test",
    )
    first = verify(_request())
    assert first["reason_code"] == "ENTITY_AUDIO_PROVIDER_FAILED"
    assert api_calls  # 断供也确实尝试过

    # 供应商恢复：同一请求必须重听并 RESOLVED，而不是回放缓存的失败判决
    monkeypatch.setattr(
        verifier_module.urllib.request,
        "urlopen",
        lambda request, timeout=0: _FakeApiResponse(_resolved_observation()),
    )
    second = verify(_request())
    assert second["status"] == "RESOLVED", second
    assert second["canonical_entity"] == "梦限大"

    # RESOLVED 判决可以缓存复用：第三次不再发起任何新调用
    calls_before = len(api_calls)
    monkeypatch.setattr(
        verifier_module.urllib.request,
        "urlopen",
        lambda request, timeout=0: (_ for _ in ()).throw(AssertionError("must hit cache")),
    )
    third = verify(_request())
    assert third["status"] == "RESOLVED"
    assert len(api_calls) == calls_before


def test_agy_quota_falls_back_to_gemini_api_free_key(tmp_path, monkeypatch):
    """Ivan 2026-07-14：付费/免费 API key 都能裁决音频——AGY 配额断供必须
    自动切到 Gemini API 直连，验收逻辑与 AGY 通道完全一致。"""
    _isolate_policy_env(monkeypatch, tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "free-key-1")
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    seen_requests = []

    def fake_urlopen(request, timeout=0):
        seen_requests.append(request)
        return _FakeApiResponse(_resolved_observation())

    monkeypatch.setattr(verifier_module.subprocess, "run", _agy_quota_run)
    monkeypatch.setattr(verifier_module.urllib.request, "urlopen", fake_urlopen)
    verify = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=tmp_path / "out",
        recording_date="2026-07-10",
        source_duration_ms=10_000,
        agy_bin="agy-test",
    )

    verdict = verify(_request())

    assert verdict["status"] == "RESOLVED"
    assert verdict["canonical_entity"] == "梦限大"
    assert verdict["provider"] == "gemini_api"
    assert verdict["model"] == "gemini-3.6-flash"
    assert verdict["key_tier"] == "free"
    assert seen_requests[0].headers.get("X-goog-api-key") == "free-key-1"
    job_dir = tmp_path / "out/entity_verdicts" / ("a" * 20)
    api_prompt = (job_dir / "prompt.gemini-api.md").read_text(encoding="utf-8")
    assert "attached audio clip" in api_prompt
    assert "还没看" not in api_prompt
    manifest = json.loads((job_dir / "verdict.manifest.json").read_text(encoding="utf-8"))
    assert manifest["provider"] == "gemini_api"
    assert any(
        row["category"] == "AGY_QUOTA_EXHAUSTED" for row in manifest["provider_failures"]
    )


def test_gemini_api_salvages_first_balanced_json_object_before_trailing_echo(
    tmp_path, monkeypatch
):
    """2026-07-16 夸夸怪实案：付费 Gemini 给出完整合法对象后又回声了几个
    片段；结构化响应不能因对象后的垃圾字符被误判为供应商整体失败。"""

    _isolate_policy_env(monkeypatch, tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "free-key-1")
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    observation = json.dumps(_resolved_observation(), ensure_ascii=False) + '\n de)."'
    response_payload = {
        "candidates": [{"content": {"parts": [{"text": observation}]}}]
    }
    monkeypatch.setattr(verifier_module.subprocess, "run", _agy_quota_run)
    monkeypatch.setattr(
        verifier_module.urllib.request,
        "urlopen",
        lambda request, timeout=0: _FakeApiResponseRaw(response_payload),
    )
    verify = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=tmp_path / "out",
        recording_date="2026-07-10",
        source_duration_ms=10_000,
        agy_bin="agy-test",
    )

    verdict = verify(_request())

    assert verdict["status"] == "RESOLVED"
    assert verdict["canonical_entity"] == "梦限大"


def test_api_paid_gate_blocks_without_dev_exception(tmp_path, monkeypatch):
    """免费 key 未配置且无 DEV_EXCEPTION：付费 key 在 3 strike 前必须被门拦，
    整体退 UNCERTAIN(PROVIDER_FAILED)，不允许任何未入帐付费调用。"""
    _isolate_policy_env(monkeypatch, tmp_path)
    monkeypatch.setenv("GEMINI_KEY_BACKUP", "paid-key")
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    api_calls = []
    monkeypatch.setattr(verifier_module.subprocess, "run", _agy_quota_run)
    monkeypatch.setattr(
        verifier_module.urllib.request,
        "urlopen",
        lambda *a, **k: api_calls.append(1) or (_ for _ in ()).throw(AssertionError("no api call allowed")),
    )
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
    assert api_calls == []
    job_dir = tmp_path / "out/entity_verdicts" / ("a" * 20)
    failures = json.loads((job_dir / "provider-failures.json").read_text(encoding="utf-8"))
    # 2026-07-19 起 strike 含本次运行已记录的轮次（付费触发时 ledger 必须已有
    # ≥3 轮完整失败证据）；免费 key 未配置只记 1 轮即停。
    assert any(
        str(row.get("category", "")).startswith("PAID_BACKUP_SKIPPED:FREE_CHAIN_STRIKES_1")
        for row in failures["failures"]
    )
    ledger_root = tmp_path / "policy-base/state/gemini-paid-backup"
    assert list(ledger_root.glob("strikes/*.json"))
    assert not list(ledger_root.glob("usage-*.jsonl"))


def test_api_paid_used_under_dev_exception_and_ledgered(tmp_path, monkeypatch):
    _isolate_policy_env(monkeypatch, tmp_path)
    monkeypatch.setenv("GEMINI_KEY_BACKUP", "paid-key")
    monkeypatch.setenv("GEMINI_PAID_BACKUP_DEV_EXCEPTION", "1")
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    monkeypatch.setattr(verifier_module.subprocess, "run", _agy_quota_run)
    monkeypatch.setattr(
        verifier_module.urllib.request,
        "urlopen",
        lambda request, timeout=0: _FakeApiResponse(_resolved_observation()),
    )
    verify = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=tmp_path / "out",
        recording_date="2026-07-10",
        source_duration_ms=10_000,
        agy_bin="agy-test",
    )

    verdict = verify(_request())

    assert verdict["status"] == "RESOLVED"
    assert verdict["key_tier"] == "paid_backup"
    ledger_root = tmp_path / "policy-base/state/gemini-paid-backup"
    usage_files = list(ledger_root.glob("usage-*.jsonl"))
    assert len(usage_files) == 1
    rows = [json.loads(line) for line in usage_files[0].read_text(encoding="utf-8").splitlines()]
    assert rows and rows[0]["purpose"] == "entity_audio_verdict"
    assert "paid-key" not in usage_files[0].read_text(encoding="utf-8")
    job_dir = tmp_path / "out/entity_verdicts" / ("a" * 20)
    manifest = json.loads((job_dir / "verdict.manifest.json").read_text(encoding="utf-8"))
    assert manifest["paid_backup_policy"]


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

    # 不同 evidence/几何 → 不同 request_sha，但音频字节相同 → 缓存命中
    second = verify(build_witness_request(check_request("f" * 64, 10_040)))
    assert second["status"] == "OBSERVED"
    assert second["heard_pinyin"] == "hai mei you ge zhai ne"
    assert len(agy_calls) == 1  # 零新 provider 调用

    cache_root = tmp_path / "base" / "cache" / "witness-acoustic"
    assert any(cache_root.rglob("*.json"))


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


def test_free_key_quota_rotates_model_before_next_key(tmp_path, monkeypatch):
    """Ivan 2026-07-27：免费层 RPD 按模型独立计（每 key 每模型 20），主模型
    429 时同 key 轮换 3.5 兜配额；接受的模型串如实钉进 verdict/manifest。
    非 429 失败不轮换（换模型不产生新信息）。"""

    import urllib.error

    _isolate_policy_env(monkeypatch, tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "free-key-1")
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    attempts = []

    def fake_urlopen(request, timeout=0):
        url = request.full_url
        attempts.append(url)
        if "gemini-3.6-flash" in url:
            raise urllib.error.HTTPError(url, 429, "quota", None, None)
        return _FakeApiResponse(_resolved_observation())

    monkeypatch.setattr(verifier_module.subprocess, "run", _agy_quota_run)
    monkeypatch.setattr(verifier_module.urllib.request, "urlopen", fake_urlopen)
    verify = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=tmp_path / "out",
        recording_date="2026-07-10",
        source_duration_ms=10_000,
        agy_bin="agy-test",
    )

    verdict = verify(_request())

    assert verdict["status"] == "RESOLVED"
    assert verdict["provider"] == "gemini_api"
    assert verdict["model"] == "gemini-3.5-flash"
    assert verdict["key_tier"] == "free"
    assert any("gemini-3.6-flash" in url for url in attempts)
    assert any("gemini-3.5-flash" in url for url in attempts)
    job_dir = tmp_path / "out/entity_verdicts" / ("a" * 20)
    manifest = json.loads(
        (job_dir / "verdict.manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["model"] == "gemini-3.5-flash"
    assert any(
        row.get("model") == "gemini-3.6-flash"
        and row.get("category") == "GEMINI_API_QUOTA_EXHAUSTED"
        for row in manifest["provider_failures"]
    )
