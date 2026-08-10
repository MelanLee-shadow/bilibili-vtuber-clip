"""统一 AGY->Gemini 客户端的金丝雀。

Ivan 2026-08-10 逐字：「需要调用AGY->gemini 这条链的，全都复用一种接口才好」，
外加长期指示「不要在 wsl 上安装 AGY」——所以 **AGY 缺席是一种正常部署形态**，
每条腿都必须能在没有 AGY 的机器上靠 Gemini 走通。

四组金丝雀，每组都写明「摘掉什么会让它变红」：
①  AGY 缺席 + mock Gemini：各入口拿到结果、回执标 AGY_BINARY_ABSENT。
②  AGY 在场：仍优先 AGY，不碰 Gemini。
③  key 顺序：免费 3 key 轮换 → 全 429 才动付费，且记一笔账。
④  路径解析：不得出现写死的 /root/...（本机腿）。
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
from pathlib import Path

import pytest

from src.autoslice import agy_gemini_client, gemini_backup_policy


# --------------------------------------------------------------------------
# ④ 路径解析
# --------------------------------------------------------------------------


def test_local_binary_resolution_never_hardcodes_root(monkeypatch, tmp_path):
    """本机腿不许写死 /root/...；解析不到 = 正常降级，不是异常。"""

    monkeypatch.delenv("AGY_BIN", raising=False)
    monkeypatch.delenv("AUTOSLICE_AGY_BIN", raising=False)
    monkeypatch.setattr(agy_gemini_client.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(agy_gemini_client.shutil, "which", lambda _name: None)

    resolved = agy_gemini_client.resolve_local_agy_binary()

    assert not resolved.startswith("/root/")
    assert agy_gemini_client.local_agy_available() is False


def test_both_env_aliases_are_accepted(monkeypatch, tmp_path):
    """AGY_BIN 与 AUTOSLICE_AGY_BIN 两个历史名都要继续认。"""

    monkeypatch.delenv("AGY_BIN", raising=False)
    monkeypatch.setenv("AUTOSLICE_AGY_BIN", str(tmp_path / "vs-agy"))
    assert agy_gemini_client.resolve_local_agy_binary(
        env_names=("AUTOSLICE_AGY_BIN", "AGY_BIN")
    ) == str(tmp_path / "vs-agy")

    monkeypatch.setenv("AGY_BIN", str(tmp_path / "plain-agy"))
    assert agy_gemini_client.resolve_local_agy_binary() == str(tmp_path / "plain-agy")


def test_remote_binary_is_env_overridable_with_free_host_default(monkeypatch):
    """SSH 腿的 /root/... 是**远端 free 主机**的真实路径，保留为默认值，
    但必须收在一个可覆盖的名字后面，而不是散在四处字符串字面量里。"""

    monkeypatch.delenv(agy_gemini_client.REMOTE_AGY_ENV, raising=False)
    assert agy_gemini_client.resolve_remote_agy_binary() == "/root/.local/bin/agy"
    monkeypatch.setenv(agy_gemini_client.REMOTE_AGY_ENV, "/usr/local/bin/agy")
    assert agy_gemini_client.resolve_remote_agy_binary() == "/usr/local/bin/agy"


# --------------------------------------------------------------------------
# ① AGY 缺席 → Gemini 接管，回执标 AGY_BINARY_ABSENT
# --------------------------------------------------------------------------


def test_missing_binary_is_classified_absent_not_crashed(tmp_path):
    """FileNotFoundError = 这一环不存在，不是「子进程炸了」。"""

    run = agy_gemini_client.run_local_agy(
        [str(tmp_path / "definitely-missing-agy"), "--sandbox"],
        cwd=tmp_path,
        timeout=5,
    )

    assert run.completed is None
    assert run.failure_category == agy_gemini_client.AGY_BINARY_ABSENT
    assert run.launch_error_type == "FileNotFoundError"


def test_timeout_and_other_oserror_stay_distinct(tmp_path, monkeypatch):
    def boom_timeout(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd="agy", timeout=1, output="partial")

    monkeypatch.setattr(subprocess, "run", boom_timeout)
    timed_out = agy_gemini_client.run_local_agy(["agy"], cwd=tmp_path, timeout=1)
    assert timed_out.failure_category == agy_gemini_client.AGY_TIMEOUT
    assert timed_out.partial_output()[0] == "partial"

    def boom_perm(*_a, **_kw):
        raise PermissionError("nope")

    monkeypatch.setattr(subprocess, "run", boom_perm)
    denied = agy_gemini_client.run_local_agy(["agy"], cwd=tmp_path, timeout=1)
    assert denied.failure_category == agy_gemini_client.AGY_SUBPROCESS_ERROR


def test_remote_rc_127_means_absent_not_generic_failure():
    """SSH 腿上 rc=127 = 那台机器没装 agy，等价于本机的 BINARY_ABSENT。"""

    assert agy_gemini_client.parse_remote_rc_line("rc=127") == 127
    assert (
        agy_gemini_client.classify_remote_agy_rc(127)
        == agy_gemini_client.AGY_BINARY_ABSENT
    )
    assert agy_gemini_client.classify_remote_agy_rc(1) == "AGY_FAILED_RC_1"
    assert (
        agy_gemini_client.classify_remote_agy_rc(None)
        == agy_gemini_client.AGY_TIMEOUT
    )
    assert agy_gemini_client.parse_remote_rc_line("") is None


# --------------------------------------------------------------------------
# ③ key 顺序：免费 3 key → 全 429 才动付费，且记账
# --------------------------------------------------------------------------


@pytest.fixture
def paid_ledger(monkeypatch, tmp_path):
    """付费闸门要求账本可写；conftest 会清掉 ambient AUTOSLICE_BASE。"""

    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "base"))
    return tmp_path / "base" / "state" / "gemini-paid-backup"


def _configure_free_keys(monkeypatch, *keys: str) -> None:
    for name in agy_gemini_client.FREE_KEY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name, value in zip(agy_gemini_client.FREE_KEY_ENV_NAMES, keys):
        monkeypatch.setenv(name, value)


class _Quota(urllib.error.HTTPError):
    def __init__(self) -> None:
        super().__init__("https://x", 429, "quota", {}, None)


def test_free_keys_are_tried_in_order_before_any_paid_use(monkeypatch, paid_ledger):
    _configure_free_keys(monkeypatch, "free-1", "free-2", "free-3")
    monkeypatch.setenv(gemini_backup_policy.PAID_KEY_ENV, "paid-key")
    monkeypatch.setenv(gemini_backup_policy.DEV_EXCEPTION_ENV, "1")
    order: list[str] = []

    def observe(key: str) -> str:
        order.append(key)
        if key != "paid-key":
            raise _Quota()
        return "accepted"

    outcome = agy_gemini_client.run_gemini_key_ladder(
        item_key="item-a", observe=observe, purpose="canary"
    )

    # 顺序打乱必红：免费三把按序在前，付费永远最后一个。
    assert order[:3] == ["free-1", "free-2", "free-3"]
    assert order[-1] == "paid-key"
    assert outcome.accepted_key_tier == gemini_backup_policy.PAID_KEY_TIER
    assert outcome.accepted_key_ordinal == 4
    assert outcome.configured_key_count == 3
    # 每笔付费都要入账。
    ledgered = list(paid_ledger.glob("usage-*.jsonl"))
    assert len(ledgered) == 1
    stamp = json.loads(ledgered[0].read_text(encoding="utf-8").strip())
    assert stamp["key_tier"] == gemini_backup_policy.PAID_KEY_TIER
    assert stamp["purpose"] == "canary"
    assert outcome.paid_policy_stamp is not None


def test_paid_key_waits_for_three_complete_free_rounds(monkeypatch, paid_ledger):
    """7/13 + 7/19 合起来的实际语义。

    付费只在**同项免费链失败满 3 轮之后**才允许；而纯额度类失败可以在同一次
    run 内把这 3 轮连补齐（否则 429 全耗尽时策略线永远凑不满，正确修复会被
    卡死成 UNCERTAIN）。所以：3 轮免费在前，付费在最后一位。
    """

    _configure_free_keys(monkeypatch, "free-1")
    monkeypatch.setenv(gemini_backup_policy.PAID_KEY_ENV, "paid-key")
    monkeypatch.delenv(gemini_backup_policy.DEV_EXCEPTION_ENV, raising=False)
    tried: list[str] = []

    def observe(key: str) -> str:
        tried.append(key)
        raise _Quota()

    agy_gemini_client.run_gemini_key_ladder(
        item_key="item-b", observe=observe, purpose="canary"
    )

    free_rounds = gemini_backup_policy.MIN_FREE_CHAIN_STRIKES
    assert tried == ["free-1"] * free_rounds + ["paid-key"]
    assert gemini_backup_policy.free_chain_strikes("item-b") == free_rounds


def test_non_quota_failure_stops_after_one_free_round_and_withholds_paid(
    monkeypatch, paid_ledger
):
    """认证/坏输出类失败不连补轮次，付费闸门也因此挡住——只有额度类才是
    确定性快败。把 quota_exhausted_round 判据换成宽松的 429 文本嗅探，
    这一例必红。"""

    _configure_free_keys(monkeypatch, "free-1", "free-2")
    monkeypatch.setenv(gemini_backup_policy.PAID_KEY_ENV, "paid-key")
    monkeypatch.delenv(gemini_backup_policy.DEV_EXCEPTION_ENV, raising=False)
    tried: list[str] = []
    skipped: list[str] = []

    def observe(key: str) -> str:
        tried.append(key)
        raise ValueError("garbled output")

    outcome = agy_gemini_client.run_gemini_key_ladder(
        item_key="item-c",
        observe=observe,
        purpose="canary",
        record_paid_skipped=lambda _o, _r, reason: skipped.append(reason),
    )

    assert tried == ["free-1", "free-2"]
    assert "paid-key" not in tried
    assert not outcome.accepted
    assert skipped and skipped[0].startswith("FREE_CHAIN_STRIKES_1_BELOW_3")
    assert not list(paid_ledger.glob("usage-*.jsonl"))


def test_quota_fastpath_lets_paid_fire_in_the_same_round(monkeypatch, paid_ledger):
    """foreign_span lane 的 7/20 裁定：全 429 的免费链即确定性耗尽。

    把 quota_fastpath 去掉，这一例必红（付费会被 strike 闸门挡住）。
    """

    _configure_free_keys(monkeypatch, "free-1", "free-2")
    monkeypatch.setenv(gemini_backup_policy.PAID_KEY_ENV, "paid-key")
    monkeypatch.delenv(gemini_backup_policy.DEV_EXCEPTION_ENV, raising=False)
    order: list[str] = []

    def observe(key: str) -> str:
        order.append(key)
        if key != "paid-key":
            raise _Quota()
        return "accepted"

    outcome = agy_gemini_client.run_gemini_key_ladder(
        item_key="item-d",
        observe=observe,
        purpose="canary",
        quota_fastpath=True,
    )

    assert order == ["free-1", "free-2", "paid-key"]
    assert outcome.accepted_key_tier == gemini_backup_policy.PAID_KEY_TIER
    assert outcome.paid_gate_reason.startswith("QUOTA_FASTPATH:")


def test_first_working_free_key_wins_and_never_reaches_paid(monkeypatch, paid_ledger):
    _configure_free_keys(monkeypatch, "free-1", "free-2", "free-3")
    monkeypatch.setenv(gemini_backup_policy.PAID_KEY_ENV, "paid-key")
    order: list[str] = []

    def observe(key: str) -> str:
        order.append(key)
        if key == "free-1":
            raise _Quota()
        return f"answer-from-{key}"

    outcome = agy_gemini_client.run_gemini_key_ladder(
        item_key="item-e", observe=observe, purpose="canary"
    )

    assert order == ["free-1", "free-2"]
    assert outcome.observed == "answer-from-free-2"
    assert outcome.accepted_key_tier == gemini_backup_policy.FREE_KEY_TIER
    assert outcome.accepted_key_ordinal == 2
    assert not list(paid_ledger.glob("usage-*.jsonl"))


# --------------------------------------------------------------------------
# generate_content 形态（音频/视觉同一条通道，只有 mime 不同）
# --------------------------------------------------------------------------


def test_generate_content_sends_key_only_in_header_and_supports_both_modalities(
    monkeypatch,
):
    seen: list[dict] = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            return json.dumps(
                {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
            ).encode("utf-8")

    def fake_urlopen(request, timeout=None):
        seen.append(
            {
                "url": request.full_url,
                "headers": dict(request.headers),
                "body": json.loads(request.data.decode("utf-8")),
            }
        )
        return _Resp()

    monkeypatch.setattr(agy_gemini_client.urllib.request, "urlopen", fake_urlopen)

    assert (
        agy_gemini_client.generate_content(
            prompt="listen", key="secret-key", model="m", inline_data=b"audio",
            mime_type="audio/mpeg",
        )
        == "ok"
    )
    assert (
        agy_gemini_client.generate_content(
            prompt="look",
            key="secret-key",
            model="m",
            inline_parts=[(b"jpg-a", "image/jpeg"), (b"jpg-b", "image/jpeg")],
        )
        == "ok"
    )

    audio_call, vision_call = seen
    # 密钥只在 header，绝不进 URL / query。
    assert "secret-key" not in audio_call["url"]
    assert audio_call["headers"]["X-goog-api-key"] == "secret-key"
    assert (
        audio_call["body"]["contents"][0]["parts"][1]["inline_data"]["mime_type"]
        == "audio/mpeg"
    )
    vision_parts = vision_call["body"]["contents"][0]["parts"]
    assert [part["inline_data"]["mime_type"] for part in vision_parts[1:]] == [
        "image/jpeg",
        "image/jpeg",
    ]


def test_oversized_request_is_refused_before_any_network_call(monkeypatch):
    def explode(*_a, **_kw):  # pragma: no cover - must never run
        raise AssertionError("oversized request reached the network")

    monkeypatch.setattr(agy_gemini_client.urllib.request, "urlopen", explode)
    with pytest.raises(RuntimeError, match="GEMINI_API_REQUEST_TOO_LARGE"):
        agy_gemini_client.generate_content(
            prompt="x", key="k", model="m", inline_data=b"y" * 1000,
            max_request_bytes=10,
        )


# --------------------------------------------------------------------------
# ①/② 逐入口：agy_frame_witness 首次获得兜底
# --------------------------------------------------------------------------


def _stub_jpeg(monkeypatch, module) -> None:
    monkeypatch.setattr(module, "_extract_frame_jpeg", lambda *_a, **_kw: b"jpeg-bytes")
    monkeypatch.setattr(
        module, "_image_jpeg", lambda *_a, **_kw: (b"source-bytes", b"jpeg-bytes")
    )


def test_frame_witness_falls_back_to_gemini_when_agy_absent(monkeypatch, tmp_path):
    """这条视觉腿此前**零兜底**：AGY 不在就只会返回 UNAVAILABLE。

    摘掉 agy_frame_witness 里的 _gemini_vision_fallback 调用，本例必红。
    """

    from src.autoslice import agy_frame_witness

    _stub_jpeg(monkeypatch, agy_frame_witness)
    _configure_free_keys(monkeypatch, "free-1")
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "base"))
    monkeypatch.setattr(
        agy_frame_witness.agy_gemini_client,
        "run_local_agy",
        lambda *_a, **_kw: agy_gemini_client.AgyRun(
            completed=None,
            failure_category=agy_gemini_client.AGY_BINARY_ABSENT,
            launch_error_type="FileNotFoundError",
        ),
    )
    monkeypatch.setattr(
        agy_frame_witness.agy_gemini_client,
        "generate_content",
        lambda **_kw: "a blue panda overlay",
    )

    receipt = agy_frame_witness.frame_vision_probe(
        tmp_path / "clip.mp4", 1_000, "what is on screen?"
    )

    assert receipt["status"] == "OBSERVED"
    assert receipt["answer"] == "a blue panda overlay"
    # 三元组回执契约：provider / model / key_tier
    assert receipt["provider"] == "gemini_api"
    assert receipt["key_tier"] == gemini_backup_policy.FREE_KEY_TIER
    assert receipt["model"]
    assert receipt["provider_failures"][0] == {
        "provider": "agy",
        "category": agy_gemini_client.AGY_BINARY_ABSENT,
        "error_type": "FileNotFoundError",
    }


def test_frame_witness_still_prefers_agy_when_it_is_present(monkeypatch, tmp_path):
    """② AGY 在场就必须走 AGY，Gemini 一次都不许被碰。"""

    from src.autoslice import agy_frame_witness

    _stub_jpeg(monkeypatch, agy_frame_witness)

    def never(**_kw):  # pragma: no cover - must never run
        raise AssertionError("Gemini was called while AGY was available")

    monkeypatch.setattr(agy_frame_witness.agy_gemini_client, "generate_content", never)
    monkeypatch.setattr(
        agy_frame_witness.agy_gemini_client,
        "run_local_agy",
        lambda *_a, **_kw: agy_gemini_client.AgyRun(
            completed=subprocess.CompletedProcess(
                ["agy"], 0, stdout="agy saw a panda", stderr=""
            ),
            failure_category=None,
        ),
    )

    receipt = agy_frame_witness.frame_vision_probe(
        tmp_path / "clip.mp4", 1_000, "what is on screen?"
    )

    assert receipt["status"] == "OBSERVED"
    assert receipt["answer"] == "agy saw a panda"
    assert receipt["provider"] == "agy"
    assert "provider_failures" not in receipt


def test_frame_witness_reports_unavailable_only_when_both_legs_fail(
    monkeypatch, tmp_path
):
    from src.autoslice import agy_frame_witness

    _stub_jpeg(monkeypatch, agy_frame_witness)
    _configure_free_keys(monkeypatch)  # no keys at all
    monkeypatch.setattr(
        agy_frame_witness.agy_gemini_client,
        "run_local_agy",
        lambda *_a, **_kw: agy_gemini_client.AgyRun(
            completed=None,
            failure_category=agy_gemini_client.AGY_BINARY_ABSENT,
            launch_error_type="FileNotFoundError",
        ),
    )

    receipt = agy_frame_witness.image_vision_probe(
        tmp_path / "still.png", "what is on screen?"
    )

    assert receipt["status"] == "UNAVAILABLE"
    assert receipt["reason_code"] == "VISION_WITNESS_UNAVAILABLE"
    assert receipt["provider_failures"][0]["category"] == (
        agy_gemini_client.AGY_BINARY_ABSENT
    )


# --------------------------------------------------------------------------
# ① SSH 转录腿：远端 rc=127 才换腿，其它失败保持老语义
# --------------------------------------------------------------------------


def _ssh_transcribe_env(monkeypatch, tmp_path, *, rc_line: str):
    """让 ssh/scp/ffmpeg 全部离线可跑，远端 agy 返回指定 rc。"""

    from src.autoslice import full_session_transcription as fst

    media = tmp_path / "clip.mp4"
    media.write_bytes(b"clip bytes")

    def fake_run(command, **_kwargs):
        if command[0] in {"ssh", "scp"} and "agy.rc" in " ".join(map(str, command)):
            return subprocess.CompletedProcess(command, 0, stdout=rc_line, stderr="")
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"mp3 bytes")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(fst.subprocess, "run", fake_run)
    return fst, media


_SRT = "1\n00:00:00,000 --> 00:00:02,000\n你好\n"


def test_ssh_transcription_switches_to_gemini_only_when_remote_agy_is_absent(
    monkeypatch, tmp_path, capsys
):
    """rc=127 = free 上没有 agy → 本机 Gemini 音频腿接管。

    这条腿此前只有 ssh 一条路；把 _gemini_fresh_transcription 的调用摘掉，
    本例必红。
    """

    fst, media = _ssh_transcribe_env(monkeypatch, tmp_path, rc_line="rc=127")
    _configure_free_keys(monkeypatch, "free-1")
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "base"))
    sent: dict[str, str] = {}

    def fake_generate_content(*, prompt, key, **_kw):
        sent["prompt"] = prompt
        sent["key"] = key
        return _SRT

    monkeypatch.setattr(
        fst.agy_gemini_client, "generate_content", fake_generate_content
    )

    transcribe = fst._build_ssh_agy_transcribe_runner("free")
    assert transcribe(media).strip() == _SRT.strip()

    # API 形态下「write output.srt」那套本地工具指令必须已经被替换掉。
    assert "write_to_file" not in sent["prompt"]
    assert "output.srt" not in sent["prompt"]
    assert "attached to this request" in sent["prompt"]
    assert sent["key"] == "free-1"
    # 静默换 provider 是事故类：唯一的披露通道必须真的说话。
    assert "AGY_BINARY_ABSENT" in capsys.readouterr().out


def test_ssh_transcription_keeps_old_semantics_for_non_absent_failures(
    monkeypatch, tmp_path
):
    """rc=1（agy 真的跑挂了）不许静默换腿——保持抛出，交给既有重试/CPA 兜底。"""

    fst, media = _ssh_transcribe_env(monkeypatch, tmp_path, rc_line="rc=1")

    def never(**_kw):  # pragma: no cover - must never run
        raise AssertionError("Gemini took over a non-absent AGY failure")

    monkeypatch.setattr(fst.agy_gemini_client, "generate_content", never)

    transcribe = fst._build_ssh_agy_transcribe_runner("free")
    with pytest.raises(Exception) as excinfo:
        transcribe(media)
    assert getattr(excinfo.value, "reason_code", "") == "AGY_FAILED_RC"
