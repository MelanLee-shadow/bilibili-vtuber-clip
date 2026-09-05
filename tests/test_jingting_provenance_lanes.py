"""Typed jingting provenance lanes (歌lane provider门根因修复).

六条歌切候选的 reason_codes 全部带 ``JINGTING_PROVIDER_NOT_AGY`` +
``JINGTING_MODEL_MISSING``。考据结论：这两条**不是** AGY 配额耗尽的产物，而是
歌lane 自己的 **设计内旁路** 被下游当成了违规：

``source_context_executor.execute_source_context_job`` 在 ``refinement_required
=False``（歌切：LRC 才是字幕权威，AGY 文本润色既冗余又是 provider 依赖）时
故意写出 ``provider="source_draft_context"`` / ``model=null`` /
``subtitle_authority_scope="proof_context_only_external_lrc_required"`` 的
jingting manifest，并且 ``_agy_reason_codes`` 对该旁路正确地返回空。

但 ``auto_review.evaluate_jingting_provenance`` 只认 ``provider=="agy"``，
于是**每一条**歌切都被无条件扣上 provider/model 两顶帽子。后果有三：

1. review 层 BLOCK（``hard_block_prefixes``）；
2. ``is_publish_gate_satisfied`` 无条件重算 provenance，发布口再死一次；
3. 这两个 code 同时是 runner 的 ``SONG_INFRA_TRANSIENT_REASON_CODES`` 成员，
   于是 song_lane 的 ``remaining_infra`` 永不为空 → ``transient_failure_code``
   恒被置位 → ``project_terminal_song_disposition`` 永远认为「有明确 typed
   transient」而拒绝终态化 → 真实的 LRC 内容否决被伪装成基础设施故障无限重试
   （单场 26 次重试 / 0 产出的僵尸循环）。

本文件锁住修复后的契约：旁路 manifest 是**合法的 typed provenance**，不产生任何
阻断码，也不产生任何 infra-transient 码；内容门（LRC 身份/对齐、完整歌、歌词
对齐）一律不受影响。
"""

from __future__ import annotations

import pytest

from src.autoslice.auto_review import (
    AutoReviewManifest,
    CandidateReview,
    DecisionAction,
    JingtingProvenance,
    ReviewDecision,
    _provenance_reason_codes,
    evaluate_jingting_provenance,
    is_publish_gate_satisfied,
    review_candidate,
)
from src.autoslice.batch_terminal_state import project_terminal_song_disposition


# scripts/session_autoslice.py 的 SONG_INFRA_TRANSIENT_REASON_CODES 中与
# jingting provenance 相关的成员。runner 是 4000+ 行的 __main__ 模块，这里按值
# 复制以免为了一个常量拖入整个 runner；session_autoslice.py:316-322 是真值。
JINGTING_INFRA_TRANSIENT_CODES = frozenset(
    {
        "JINGTING_PROVENANCE_MISSING",
        "JINGTING_PROVIDER_NOT_AGY",
        "JINGTING_AGY_FAILED",
        "JINGTING_MODEL_MISSING",
        "JINGTING_PROVIDER_FALLBACK_USED",
        "JINGTING_PROVIDER_FALLBACK_UNKNOWN",
    }
)

# Representative manifest shape for the public bypass-provenance regression.
SONG_BYPASS_MANIFEST = {
    "schema_version": "jingting-source-context-result.v1",
    "job_id": "seededsong_120000_850000",
    "provider": "source_draft_context",
    "model": None,
    "agy_rc": 0,
    "provider_fallback_used": False,
    "provider_request_id": "BYPASSED_NOT_AUTHORITATIVE_FOR_SONG_LRC",
    "requested_provider": None,
    "executed_provider": None,
    "refinement_required": False,
    "subtitle_authority_scope": "proof_context_only_external_lrc_required",
}


def song_candidate(**overrides) -> CandidateReview:
    """一条内容门全绿、仅靠旁路 manifest 提供 provenance 的歌切候选。"""

    base = dict(
        candidate_id="song-bypass",
        jingting_done=True,
        # 关键：不依赖 verified_song_lrc_authority 这条既有豁免。它只有在 LRC
        # 证明已经成功时才为 True，因此无法解释「证明失败的候选为什么额外背上
        # provider 帽子」，也无法保护 is_publish_gate_satisfied。
        verified_song_lrc_authority=False,
        release_ready=True,
        review_required_findings=(),
        foreground_song_overlap_seconds=180.0,
        song_complete=True,
        lyrics_alignment_ready=True,
        start_boundary_score=0.99,
        end_boundary_score=0.99,
        standalone_score=0.99,
        payoff_score=0.99,
        open_loop_count=0,
        editorial_score=90.0,
        duplicate_similarity=0.10,
        subtitle_alignment_p95_ms=120.0,
        actual_cut_error_ms=20.0,
        jingting_provenance=JingtingProvenance.from_manifest(SONG_BYPASS_MANIFEST),
    )
    base.update(overrides)
    return CandidateReview(**base)


class TestSongLrcBypassLane:
    def test_bypass_manifest_fields_are_parsed(self):
        """from_manifest 必须保留旁路自证字段，否则无法区分旁路与冒名。"""

        provenance = JingtingProvenance.from_manifest(SONG_BYPASS_MANIFEST)

        assert provenance.provider == "source_draft_context"
        assert provenance.model is None
        assert provenance.refinement_required is False
        assert (
            provenance.subtitle_authority_scope
            == "proof_context_only_external_lrc_required"
        )

    def test_bypass_manifest_emits_no_blocking_reason_codes(self):
        """核心：设计内旁路不是 provider 违规。"""

        checks = evaluate_jingting_provenance(
            JingtingProvenance.from_manifest(SONG_BYPASS_MANIFEST)
        )

        assert _provenance_reason_codes(checks) == ()

    def test_bypass_manifest_emits_no_infra_transient_codes(self):
        """僵尸循环的根：旁路不得贡献任何 SONG_INFRA_TRANSIENT_REASON_CODES 成员。"""

        emitted = set(
            _provenance_reason_codes(
                evaluate_jingting_provenance(
                    JingtingProvenance.from_manifest(SONG_BYPASS_MANIFEST)
                )
            )
        )

        assert emitted & JINGTING_INFRA_TRANSIENT_CODES == set()

    def test_bypass_song_reaches_auto_upload_without_lrc_authority_waiver(self):
        decision = review_candidate(song_candidate())

        assert decision.action == DecisionAction.AUTO_UPLOAD
        assert decision.reason_codes == ()

    def test_publish_gate_accepts_bypass_provenance(self):
        """is_publish_gate_satisfied 无条件重算 provenance —— 发布口同样要放行。"""

        artifacts = {
            "video_sha256": "sha256:video",
            "draft_subtitle_sha256": "sha256:draft",
            "jingting_subtitle_sha256": "sha256:jingting",
            "jingting_manifest_sha256": "sha256:manifest",
            "cover_sha256": "sha256:cover",
            "publish_json_sha256": "sha256:publish",
        }
        manifest = AutoReviewManifest(
            candidate_id="song-bypass",
            decision=ReviewDecision(action=DecisionAction.AUTO_UPLOAD, score=90.0),
            artifacts=artifacts,
            jingting_provenance=JingtingProvenance.from_manifest(
                SONG_BYPASS_MANIFEST
            ),
        )

        assert is_publish_gate_satisfied(
            jingting_done=True, manifest=manifest, expected_artifacts=artifacts
        )

    def test_serialized_manifest_does_not_reinject_provider_reason_codes(self):
        """AutoReviewManifest.to_dict 会在落盘时重算 provenance，不能再污染。"""

        manifest = AutoReviewManifest(
            candidate_id="song-bypass",
            decision=ReviewDecision(action=DecisionAction.AUTO_UPLOAD, score=90.0),
            artifacts={"video_sha256": "sha256:video"},
            jingting_provenance=JingtingProvenance.from_manifest(
                SONG_BYPASS_MANIFEST
            ),
        )

        data = manifest.to_dict()

        assert data["decision"]["reason_codes"] == []
        assert [check["code"] for check in data["checks"] if not check["pass"]] == []


class TestBypassLaneStaysFailClosed:
    """放宽只覆盖**完整自证**的旁路；缺一项自证就照旧阻断。"""

    @pytest.mark.parametrize(
        ("override", "expected_reason"),
        [
            # 没声明 refinement_required=False → 无法证明这是歌切旁路
            ({"refinement_required": None}, "JINGTING_PROVIDER_NOT_AGY"),
            ({"refinement_required": True}, "JINGTING_PROVIDER_NOT_AGY"),
            # 字幕权威范围声明缺失/被改写 → 冒名
            ({"subtitle_authority_scope": None}, "JINGTING_PROVIDER_NOT_AGY"),
            (
                {"subtitle_authority_scope": "talk_source_context_refinement"},
                "JINGTING_PROVIDER_NOT_AGY",
            ),
            # 旁路必须 rc=0 且未走 fallback
            ({"agy_rc": 1}, "JINGTING_AGY_FAILED"),
            ({"provider_fallback_used": True}, "JINGTING_PROVIDER_FALLBACK_USED"),
            (
                {"provider_fallback_used": None},
                "JINGTING_PROVIDER_FALLBACK_UNKNOWN",
            ),
        ],
    )
    def test_incomplete_bypass_self_attestation_still_blocks(
        self, override, expected_reason
    ):
        manifest = {**SONG_BYPASS_MANIFEST, **override}

        codes = _provenance_reason_codes(
            evaluate_jingting_provenance(JingtingProvenance.from_manifest(manifest))
        )

        assert expected_reason in codes

    def test_bypass_claiming_a_model_is_a_provenance_violation(self):
        """旁路没有调用任何模型；声称模型串就是不诚实的 provenance。"""

        manifest = {**SONG_BYPASS_MANIFEST, "model": "Gemini 3.6 Flash (Low)"}

        codes = _provenance_reason_codes(
            evaluate_jingting_provenance(JingtingProvenance.from_manifest(manifest))
        )

        assert "JINGTING_BYPASS_MODEL_UNEXPECTED" in codes

    def test_unknown_provider_still_blocks(self):
        manifest = {**SONG_BYPASS_MANIFEST, "provider": "whatever"}

        codes = _provenance_reason_codes(
            evaluate_jingting_provenance(JingtingProvenance.from_manifest(manifest))
        )

        assert "JINGTING_PROVIDER_NOT_AGY" in codes

    def test_missing_manifest_still_blocks(self):
        codes = _provenance_reason_codes(
            evaluate_jingting_provenance(JingtingProvenance.from_manifest(None))
        )

        assert "JINGTING_PROVENANCE_MISSING" in codes


class TestGeminiApiFallbackLane:
    """维护者 拍板（项目 memory）：AGY 订阅 / 免费 key / 付费 backup 是
    同一个 Gemini 模型的**配额顺序**，「按 provider 层拒证据的门 = 过度限制」；
    「需要调用 AGY->gemini 这条链的，全都复用一种接口才好」。

    因此带完整 typed provenance 的 Gemini API 兜底是**有效证据**，但必须如实
    记录用了哪一层（provider_fallback_used=True）、模型串是什么、AGY 那条腿的
    退出码是多少。缺任何一项仍然阻断。

    注意：本仓当前 jingting 精听链（jingting_remote_runner）**没有**任何
    Gemini 兜底实现，只会返回 provider="agy"。所以本 lane 是前瞻契约，不修复
    任何已观测到的生产故障。见内部取证文档留存（song-lane-forensics）。
    """

    @staticmethod
    def manifest(**overrides) -> dict:
        base = {
            "provider": "gemini_api",
            "model": "gemini-3.6-flash",
            "agy_rc": 1,
            "provider_fallback_used": True,
            "refinement_required": True,
            "subtitle_authority_scope": "talk_source_context_refinement",
        }
        base.update(overrides)
        return base

    def test_typed_gemini_fallback_is_accepted(self):
        codes = _provenance_reason_codes(
            evaluate_jingting_provenance(
                JingtingProvenance.from_manifest(self.manifest())
            )
        )

        assert codes == ()

    @pytest.mark.parametrize(
        ("override", "expected_reason"),
        [
            ({"model": None}, "JINGTING_MODEL_MISSING"),
            ({"model": ""}, "JINGTING_MODEL_MISSING"),
            # 没如实记录「用了兜底」→ 拒
            ({"provider_fallback_used": False}, "JINGTING_PROVIDER_FALLBACK_UNKNOWN"),
            ({"provider_fallback_used": None}, "JINGTING_PROVIDER_FALLBACK_UNKNOWN"),
            # 没记录 AGY 那条腿到底怎么失败的 → 拒
            ({"agy_rc": None}, "JINGTING_AGY_FAILED"),
        ],
    )
    def test_dishonest_gemini_fallback_still_blocks(self, override, expected_reason):
        codes = _provenance_reason_codes(
            evaluate_jingting_provenance(
                JingtingProvenance.from_manifest(self.manifest(**override))
            )
        )

        assert expected_reason in codes


class TestAgyLaneUnchanged:
    @staticmethod
    def manifest(**overrides) -> dict:
        base = {
            "provider": "agy",
            "model": "Gemini 3.6 Flash (Low)",
            "agy_rc": 0,
            "provider_fallback_used": False,
            "refinement_required": True,
            "subtitle_authority_scope": "talk_source_context_refinement",
        }
        base.update(overrides)
        return base

    def test_healthy_agy_still_passes(self):
        codes = _provenance_reason_codes(
            evaluate_jingting_provenance(
                JingtingProvenance.from_manifest(self.manifest())
            )
        )

        assert codes == ()

    @pytest.mark.parametrize(
        ("override", "expected_reason"),
        [
            ({"agy_rc": 2}, "JINGTING_AGY_FAILED"),
            ({"model": None}, "JINGTING_MODEL_MISSING"),
            ({"provider_fallback_used": True}, "JINGTING_PROVIDER_FALLBACK_USED"),
            ({"provider_fallback_used": None}, "JINGTING_PROVIDER_FALLBACK_UNKNOWN"),
        ],
    )
    def test_broken_agy_still_blocks(self, override, expected_reason):
        codes = _provenance_reason_codes(
            evaluate_jingting_provenance(
                JingtingProvenance.from_manifest(self.manifest(**override))
            )
        )

        assert expected_reason in codes


class TestTerminalityIsRestored:
    """污染停止后，真实的 LRC 内容否决必须重新终态化（8/7 语义，这次是对的），
    而不是被伪装成 infra transient 无限重试（8/8 僵尸循环）。"""

    @staticmethod
    def song_row(reason_codes: list[str], **overrides) -> dict:
        row = {
            "candidate_id": "song_213743_1754",
            "status": "blocked",
            "rc": 0,
            "reason_codes": reason_codes,
        }
        row.update(overrides)
        return row

    #  song_lane.produce_song 的分类逻辑（song_lane.py:636-648）：本次 attempt
    #  里残留的任一 SONG_INFRA_TRANSIENT_REASON_CODES 成员会被选作
    #  transient_failure_code（min() 取字典序最小）。
    @staticmethod
    def transient_code_for(reason_codes: list[str]) -> str | None:
        remaining = set(reason_codes) & JINGTING_INFRA_TRANSIENT_CODES
        return min(remaining) if remaining else None

    def test_pre_fix_shape_would_have_masked_a_content_rejection(self):
        """回归锚点：8/7 那组 reason_codes 里 JINGTING 两条一旦存在，
        min() 会选出 JINGTING_MODEL_MISSING，终态化就被否掉。"""

        polluted = [
            "JINGTING_PROVIDER_NOT_AGY",
            "JINGTING_MODEL_MISSING",
            "SONG_AUDIO_LRC_ALIGNMENT_INVALID",
        ]

        transient = self.transient_code_for(polluted)
        assert transient == "JINGTING_MODEL_MISSING"

        row = self.song_row(polluted, transient_failure_code=transient)
        assert (
            project_terminal_song_disposition(
                row,
                terminal_performer_rejection_codes=(),
                infra_transient_reason_codes=JINGTING_INFRA_TRANSIENT_CODES,
            )
            is False
        )
        assert row["status"] == "blocked"

    def test_clean_bypass_lets_content_rejection_go_terminal(self):
        """修复后：旁路不再贡献 JINGTING 码 → 无 transient → 内容否决终态化。"""

        clean = ["SONG_AUDIO_LRC_ALIGNMENT_INVALID"]

        assert self.transient_code_for(clean) is None

        row = self.song_row(clean)
        assert (
            project_terminal_song_disposition(
                row,
                terminal_performer_rejection_codes=(),
                infra_transient_reason_codes=JINGTING_INFRA_TRANSIENT_CODES,
            )
            is True
        )
        assert row["status"] == "candidate_rejected"

    def test_real_provider_outage_still_defers_terminality(self):
        """真的 provider 故障仍然是 transient —— 8/8 的修复方向本身没有被推翻。"""

        row = self.song_row(
            ["AGY_QUOTA_EXHAUSTED", "SONG_AUDIO_LRC_ALIGNMENT_INVALID"],
            transient_failure_code="AGY_QUOTA_EXHAUSTED",
        )

        assert (
            project_terminal_song_disposition(
                row,
                terminal_performer_rejection_codes=(),
                infra_transient_reason_codes=frozenset({"AGY_QUOTA_EXHAUSTED"}),
            )
            is False
        )
        assert row["status"] == "blocked"


class TestContentGatesUntouched:
    """内容门一律不动：本次修复只处理 provenance/基础设施层。"""

    @pytest.mark.parametrize(
        ("override", "expected_reason"),
        [
            ({"song_complete": False}, "SONG_PARTIAL"),
            ({"lyrics_alignment_ready": False}, "LYRICS_ALIGNMENT_REQUIRED"),
            ({"subtitle_alignment_p95_ms": 900.0}, "SUBTITLE_ALIGNMENT_BAD"),
            ({"release_ready": None}, "RELEASE_READY_MISSING"),
            ({"song_complete": None}, "SONG_COMPLETENESS_MISSING"),
            ({"lyrics_alignment_ready": None}, "LYRICS_ALIGNMENT_MISSING"),
        ],
    )
    def test_bypass_provenance_does_not_waive_content_gates(
        self, override, expected_reason
    ):
        decision = review_candidate(song_candidate(**override))

        assert decision.action == DecisionAction.BLOCK
        assert expected_reason in decision.reason_codes


class TestSongHintSamplingDensity:
    """``_full`` 权威复证窗的歌名识别不能被输入采样帽饿死。

    实测：同一候选，短窗（~30 cue）识别出歌名，214-cue 的 ``_full``
    窗返回「LLM returned no usable song guesses」。根因是
    ``generate_llm_song_queries`` 的 ``max_lines`` 帽做均匀抽样，214 -> 18 时
    抽样比 1/12，演唱段落只剩约 3 条。这是基础设施层的输入帽，不是内容门。
    """

    @staticmethod
    def window(total: int = 214, sung_slice: slice = slice(60, 100)):
        from src.autoslice.review_evidence import SourceCue

        cues = []
        for index in range(total):
            sung = sung_slice.start <= index < sung_slice.stop
            cues.append(
                SourceCue(
                    cue_id=f"u_{index:06d}",
                    source_start_ms=index * 2_500,
                    source_end_ms=index * 2_500 + 2_000,
                    text=("唱段歌词" if sung else "闲聊内容") + str(index),
                    language="zh",
                    kind="speech",
                    confidence=1.0,
                )
            )
        return cues

    def test_full_window_keeps_the_sung_block_recognizable(self):
        from src.autoslice.song_alignment import generate_llm_song_queries

        seen: dict[str, str] = {}

        def llm_call(prompt: str) -> str:
            seen["prompt"] = prompt
            return '{"guesses": [{"title": "告白气球", "artist": "周杰伦"}]}'

        generate_llm_song_queries(self.window(), llm_call)

        sung_lines = [
            line for line in seen["prompt"].splitlines() if line.startswith("唱段歌词")
        ]
        # 40 条演唱 cue，抽样后至少要活下来 20 条才谈得上识别；旧的 18 行帽只剩 3-4 条。
        assert len(sung_lines) >= 20, f"only {len(sung_lines)} sung lines survived sampling"

    def test_short_window_is_still_sent_whole(self):
        from src.autoslice.song_alignment import generate_llm_song_queries

        seen: dict[str, str] = {}

        def llm_call(prompt: str) -> str:
            seen["prompt"] = prompt
            return '{"guesses": []}'

        generate_llm_song_queries(self.window(total=30, sung_slice=slice(5, 25)), llm_call)

        assert len([
            line for line in seen["prompt"].splitlines() if line.startswith("唱段歌词")
        ]) == 20
