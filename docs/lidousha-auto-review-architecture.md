# 李豆沙自动切片 Auto Review 架构方案

生成时间：2026-06-25
来源：本地代码/远端 `free` 实测 + ChatGPT Pro Extended consult（提交前和返回后均确认 visible composer mode 为 `Pro Extended`）。

## 结论摘要

当前问题不是 agy 精听字幕本身不够好，而是流水线顺序错了：系统先把粗候选直接切成最终片段，再对已经切好的片段做 agy 精听。这样 agy 只能修字幕文本，不能恢复被切掉的歌曲开头、歌曲结尾、谈话铺垫或 punchline 后反应。

目标流水线应改为：

```text
原始录播 + 粗字幕 + 弹幕
→ 高召回内容锚点
→ 扩展源上下文
→ agy 精听源上下文
→ 歌曲/对话结构分析
→ 自动边界解析
→ 自动 review / recut / drop / block
→ 最终切片
→ 渲染后 QA
→ 严格 release gate
→ 幂等上传
```

关键原则：

- 候选不再等于最终切片区间；候选只是“有趣锚点”。
- `SLICE_DURATION=90` 只能作为弹幕召回窗口，不能作为最终时长。
- `GLOBAL_SLICE_NUM=10` 必须解释为“最多 10 条”，不是“凑满 10 条”。
- `.jingting.done` 只表示精听任务完成，不表示 release-ready。
- 任何 `*.jingting.review-required.json` 且 `release_ready=false` 都是阻断项，不能自动上传。
- 无人 review 不等于全部自动发；无人 review 的核心是系统能自动 `AUTO_UPLOAD / AUTO_RECUT / DROP / BLOCK / RETRY`。

## 已验证事实

### 1. 当前配置和代码行为

远端 `free` 容器 `/app` 当前配置：

```text
AUTO_SLICE=True
GLOBAL_SLICE=True
GLOBAL_SLICE_NUM=10
SLICE_DURATION=90
SLICE_NUM=3
SLICE_OVERLAP=30
SEMANTIC_SLICE=True
SEMANTIC_SLICE_MIN_DURATION=60
SEMANTIC_SLICE_MAX_DURATION=180
SEMANTIC_SLICE_MIN_SCORE=70
SEMANTIC_SLICE_BATCH_SECONDS=600
SEMANTIC_SLICE_MAX_BATCH_CHARS=6000
MLLM_MODEL=cpa
```

`src/autoslice/hybrid_slice.py` 的当前逻辑：

- 从粗 SRT 解析 transcript。
- 调 CPA Responses 找 0-3 个语义候选，提示中建议 60-180 秒。
- 用 ASS 弹幕时间戳找弹幕密度窗口，默认 90 秒。
- `_sanitize_semantic_candidate()` 会把太短候选扩到 `SEMANTIC_SLICE_MIN_DURATION`，把太长候选裁到 `SEMANTIC_SLICE_MAX_DURATION`。
- `_score_candidates()` 使用 `semantic_score * 0.7 + density_score * 0.3`，hybrid 再加 8 分。
- `render_hybrid_slices()` 直接用 candidate `start/duration` 调 `slice_video()`。

`src/autoslice/auto_slice_video/autosv/slice/slice_video.py` 当前切法：

```text
ffmpeg -ss HH:MM:SS -i input -t HH:MM:SS -c:v copy -c:a copy output
```

这有两个风险：

- `format_time()` 只保留整数秒，会丢毫秒级边界。
- `-c copy` 受关键帧影响，实际切点可能偏离请求切点。

### 2. agy 精听事实

当前精听 daemon：

```text
/opt/bilive/app/scripts/gemini_slice_jingting.py --provider agy --daemon --room 22966160
```

虽然脚本名叫 `gemini_slice_jingting.py`，但 provider 是 `agy`。

已检查历史 `.jingting.manifest.json` 样本（2026-06-19 official_source、2026-06-20 blrec_llm_formal、2026-06-25 当前 10 条）：

```text
provider = agy
model = Gemini 3.5 Flash (Low)
```

因此，在已检查范围内，“精听字幕由 agy 生成，之前也是 agy 路径”是真的。更严谨的审计仍需未来 manifest 记录 `requested_model / resolved_model / provider_request_id / fallback_used / prompt_sha256`。

2026-06-25 当前 10 条：

- `*.jingting.srt`：10/10
- `*.jingting.done`：10/10
- `*.jingting.manifest.json`：10/10
- `*.jingting.review-required.json`：4/10

4 条 review-required 的 finding 都是：

```text
japanese_song_or_lyrics_alignment_required
```

这说明当前系统能识别日语歌词风险，但还没有自动歌词对齐/自动放行能力。

## 当前根因分析

| 现象 | 直接原因 | 根因位置 |
| --- | --- | --- |
| 时长集中在 62-94 秒 | 弹幕候选默认 90 秒；语义候选最短 60 秒；短候选被机械扩展 | 候选 sanitize 和固定窗口 |
| 歌曲被切断 | 没有前景歌唱检测、歌曲整体边界、歌曲原子性规则 | 候选生成和边界解析缺失 |
| 谈话没讲完 | 没有检查问答/故事/列表/指代/punchline 反应是否闭合 | render 前缺少 boundary resolver |
| 高弹幕片段压过完整片段 | 弹幕可补偿边界坏片段；加权总分不适合作硬 gate | 评分逻辑 |
| 精听后仍切坏 | agy 在切片之后运行，只能修片内文本 | 流水线顺序 |
| top10 质量参差 | `GLOBAL_SLICE_NUM=10` 被当作配额，而不是上限 | 全局选择 |
| 日语歌词阻断 | 只有风险标记，没有自动歌词对齐修复路径 | 精听后处理 |
| 实际切点可能偏 | ffmpeg 整秒 + stream copy | 渲染层 |

## 推荐数据模型

内部不要把 SRT 当作唯一真相。SRT 适合导出，不适合作为自动 review 的内部状态。建议建立源录播绝对时间轴：

```json
{
  "cue_id": "u_000123",
  "source_start_ms": 3814200,
  "source_end_ms": 3817650,
  "coarse_text": "……",
  "refined_text": "……",
  "language": "zh|ja|mixed",
  "speaker": "streamer|guest|unknown",
  "kind": "speech|singing|music|silence|reaction",
  "asr_confidence": 0.94,
  "provider": "agy",
  "model": "Gemini 3.5 Flash (Low)"
}
```

候选也应从最终区间改成内容锚点：

```json
{
  "anchor_start_ms": 1280000,
  "anchor_end_ms": 1294000,
  "setup_cue_ids": ["u_101", "u_102"],
  "payoff_cue_ids": ["u_108"],
  "candidate_reason": "反转/误解/吐槽",
  "content_type_hint": "talk|song|mixed",
  "boundary_state": "unresolved"
}
```

## 自动 review 状态机

| 状态 | 含义 |
| --- | --- |
| `AUTO_UPLOAD` | 所有硬 gate 通过，可以进入上传队列 |
| `AUTO_RECUT` | 内容值得保留，但边界可自动调整 |
| `DROP` | 内容不够好、重复、过长或缺上下文，直接丢弃 |
| `RETRY` | agy、CloudDrive、ffmpeg 等基础设施暂时失败 |
| `BLOCK` | 歌词、授权、模型来源、内容政策等无法自动解决 |

无人 review 模式里，`BLOCK` 应解释为“不上传并隔离”，而不是等待 Ivan 逐条看。

## Release gate 初始门槛

| 检查项 | 自动上传门槛 | 可自动 recut | 必须阻断/丢弃 |
| --- | --- | --- | --- |
| 开头边界完整度 | ≥ 0.92 | 0.65-0.92 | < 0.65 |
| 结尾边界完整度 | ≥ 0.95 | 0.65-0.95 | < 0.65 |
| 独立可理解度 | ≥ 0.90 | 可向前扩展解决 | 扩到 hard max 仍依赖前文 |
| punchline/payoff | ≥ 0.90 | - | 不存在则 DROP |
| 未闭合话题 | 0 个 | 可向后扩展 | 找不到自然闭合则 DROP |
| 前景歌曲完整度 | ≥ 99.5% | 可扩为整首歌 | 边界不确定或超上限 |
| 歌曲边界置信度 | ≥ 0.97 | - | 低于门槛 |
| `review_required` | false | 自动歌词修复后重审 | 未修复时 BLOCK |
| 字幕时间单调 | 必须通过 | 可重新生成 | 反复失败 |
| 字幕越界/重叠 | 0 | 可重新裁剪 | 反复失败 |
| 字幕对齐 p95 误差 | ≤ 350ms | 重新对齐 | > 800ms 或无法验证 |
| 实际切点误差 | ≤ 100ms | 重新编码 | 无法准确切割 |
| 编辑价值分 | ≥ 82/100 | - | < 82 DROP |
| 近重复相似度 | < 0.90 | - | ≥ 0.90 DROP |
| 授权状态 | ALLOWED | - | UNKNOWN/DENIED BLOCK |

编辑价值分只用于硬 gate 通过后的排序。弹幕分最多占 10 分，不能再用 30% 权重补偿坏边界。

## 歌曲边界规则

歌曲必须按原子区间处理：

```text
如果候选与前景歌曲重叠 > 5 秒
或重叠超过歌曲时长的 10%
则只能：
1. 扩展到完整歌曲；
2. 或 DROP/BLOCK。
不能截成 90 秒。
```

建议配置：

```text
SONG_ATOMIC = true
SONG_PREROLL_SECONDS = 3
SONG_POSTROLL_SECONDS = 5
SONG_HARD_MAX_SECONDS = 720
```

前景歌唱初始检测可用规则版：

```text
foreground_song 开始：
  music_prob >= 0.80
  singing_prob >= 0.70
  连续 7 秒中至少 5 秒满足

foreground_song 结束：
  music_prob < 0.35 持续 8 秒
  或正常说话恢复超过 5 秒
```

在专用日语歌词自动对齐 gate 未实现前：

```text
foreground_song == true
且 lyrics_alignment_ready == false
→ BLOCK/DROP，不上传
```

## 对话边界规则

起点只允许落在：

- utterance 开始；
- 明显停顿后；
- 话题切换处；
- 新问题/新故事开始；
- 片内能解释指代的位置；
- 不以“然后/所以/但是/因为/结果/接着”等承接词开头。

终点只允许落在：

- 完整陈述结束；
- 问题回答完；
- 故事、引用、列表闭合；
- punchline 后反应结束；
- 弹幕/笑声峰值回落；
- 话题切换前。

推荐初始配置：

```text
CONTEXT_PRE_SECONDS = 90
CONTEXT_POST_SECONDS = 150
TALK_MIN_PUBLISH_SECONDS = 20
TALK_SOFT_MAX_SECONDS = 180
TALK_HARD_MAX_SECONDS = 300
RECUT_MAX_ATTEMPTS = 2
```

短片不要硬补到 60 秒；长片不要硬裁到 180 秒。找不到自然边界就 DROP。

## `slice-auto-review.v1` manifest 草案

```json
{
  "schema_version": "slice-auto-review.v1",
  "candidate_id": "sha256:...",
  "state": "POST_RENDER_REVIEWED",
  "provenance": {
    "recording_id": "room22966160-20260625",
    "source_sha256": "...",
    "code_commit": "...",
    "prompt_version": "boundary-v1",
    "transcript_provider": "agy",
    "transcript_model": "Gemini 3.5 Flash (Low)",
    "provider_request_id": "...",
    "provider_fallback_used": false
  },
  "timeline": {
    "anchor_start_ms": 0,
    "anchor_end_ms": 0,
    "context_start_ms": 0,
    "context_end_ms": 0,
    "resolved_start_ms": 0,
    "resolved_end_ms": 0,
    "actual_render_start_ms": 0,
    "actual_render_end_ms": 0
  },
  "content": {
    "type": "talk",
    "setup_cue_ids": [],
    "payoff_cue_ids": [],
    "closure_cue_ids": [],
    "open_loops": [],
    "foreground_song_spans": []
  },
  "scores": {
    "start_boundary": 0.0,
    "end_boundary": 0.0,
    "standalone": 0.0,
    "payoff_present": 0.0,
    "song_completeness": 1.0,
    "subtitle_alignment": 0.0,
    "editorial_score": 0.0
  },
  "checks": [
    {
      "code": "SONG_PARTIAL",
      "pass": true,
      "severity": "BLOCK",
      "evidence": {"span_ms": [0, 0]}
    }
  ],
  "decision": {
    "action": "AUTO_UPLOAD",
    "reason_codes": [],
    "recut_attempt": 0,
    "next_start_ms": null,
    "next_end_ms": null
  },
  "artifacts": {
    "video_sha256": "...",
    "subtitle_sha256": "...",
    "title_manifest_sha256": "...",
    "cover_sha256": "..."
  }
}
```

上传器必须验证 manifest 和当前待上传 artifacts 的 hash 一致。recut 后旧 title/cover/publish.json 必须失效重建。

本地 phase-1 gate 已补充 `is_publish_gate_satisfied()`：未来 uploader / publish preparation 入口不得只检查 `.jingting.done`；必须同时满足 `slice-auto-review.v1` manifest 存在、`decision.action == AUTO_UPLOAD`、`reason_codes` 为空，并且当前 video/subtitle/title/cover/publish 等 artifact hash 与 manifest 记录一致。缺 manifest、`AUTO_RECUT/DROP/BLOCK/RETRY`、旧 artifact hash 都应 fail closed，不进入上传准备。

本地 phase-3 render/PTS QA 已补充 `src/autoslice/render_qa.py` 纯逻辑 contract：当渲染产物可提供实际源时间轴 metadata 时，比较 `requested_start_ms/requested_end_ms` 与 `actual_start_ms/actual_end_ms`，取 start/end 绝对误差最大值为 `actual_cut_error_ms` 并写入 manifest check evidence。`actual_cut_error_ms <= 100` 才允许 `AUTO_UPLOAD`；`> 100` 进入 `AUTO_RECUT`，recut 预算耗尽后 `BLOCK`，reason code 固定为 `ACTUAL_CUT_ERROR_HIGH`。字幕 p95 alignment 同步收紧为 `<= 350ms` 才允许 upload，`350ms < p95 <= 800ms` 进入 `AUTO_RECUT` (`SUBTITLE_ALIGNMENT_RETRY`)，`> 800ms` 直接 `BLOCK` (`SUBTITLE_ALIGNMENT_BAD`)。

## `source-context-jingting-job.v1` manifest 草案

本地 phase-2 job planner 已补充源上下文精听计划 manifest。它只做确定性本地规划，不调用 agy、不写输出、不部署 `free`。默认窗口为 anchor 前 90 秒、后 150 秒，并裁剪到源录播时长边界。

```json
{
  "schema_version": "source-context-jingting-job.v1",
  "job_kind": "SOURCE_CONTEXT_JINGTING",
  "job_id": "scj_<sha256-prefix>",
  "candidate_id": "anchor-id",
  "provider": "agy",
  "local_only": true,
  "source_offset_ms": 0,
  "provenance": {
    "recording_id": "room22966160-20260625",
    "source_sha256": "sha256:...",
    "source_uri": "file:///recordings/source.mp4",
    "planner_version": "source-context-planner.v1"
  },
  "timeline": {
    "source_duration_ms": 0,
    "anchor_start_ms": 0,
    "anchor_end_ms": 0,
    "context_start_ms": 0,
    "context_end_ms": 0,
    "context_duration_ms": 0,
    "context_anchor_start_ms": 0,
    "context_anchor_end_ms": 0
  },
  "input": {
    "source_uri": "file:///recordings/source.mp4",
    "source_sha256": "sha256:...",
    "source_offset_ms": 0,
    "duration_ms": 0,
    "write_outputs": false
  },
  "outputs": {
    "jingting_srt": null,
    "jingting_manifest": null,
    "review_required": null
  }
}
```

`job_id` 由 schema/provider、recording/source hash、candidate id、anchor 时间和裁剪后的 context 时间确定性生成；同一输入重复规划必须得到相同 id，方便 replay 与去重。

## 2026-06-25 P7 本地 evidence / executor / shadow runner

P7 在本地补了一个不上传、不部署的证据层垂直切片，目的是把前面“缺证据就 BLOCK”的安全 gate 往真实可审计 evidence 推进，而不是继续只输出空缺原因。

### `slice-review-evidence.v1`

新增 `src/autoslice/review_evidence.py`：

- `SourceCue` 只记录源录播绝对时间轴字段：`source_start_ms/source_end_ms/text/language/kind/speaker/confidence`。不把 clip-relative `start_ms` 当 cue identity，避免 recut/replay 时丢失来源。
- `ReviewEvidence` 统一承载歌曲、对话、边界、payoff、重复度、字幕 alignment、实际切点、editorial score、source cues、checks 和 `evidence_gaps`。
- `to_candidate_review()` 把 normalized evidence 转到既有 `CandidateReview`。转换边界 fail-closed：必填证据字段缺失会形成 `review_required_findings` 并阻止 `AUTO_UPLOAD`；但软质量发现（如承接词开头、缺 punchline）不直接塞进 `review_required_findings`，而由 numeric gate 决定 `AUTO_RECUT/DROP/BLOCK`。

### source-context executor 初版

新增 `src/autoslice/source_context_executor.py`：

- 生成可审计的 ffmpeg source-context clip argv，不经 shell 拼接。
- 从源 SRT 选出 context window 内的 cue，写出 context-relative draft SRT，同时保留 `source-cues.json` 中的源绝对时间。
- 写 `jingting-source-context-result.v1` manifest，记录 `provider/model/agy_rc/provider_fallback_used/source_sha256/input_sha256/output_sha256/prompt_sha256/source_offset_ms`。
- fail-closed 行为：缺源视频、缺 draft SRT、source sha256 不匹配、ffmpeg 失败、agy rc 非 0、provider fallback unknown/used、model 缺失、refined SRT 缺失都不会生成 release-ready；只写 `review-required.json` 并返回 `RETRY/RETRY_INFRA`。

### rule-based content evidence + manual style profile

新增 `src/autoslice/content_evidence.py` 与 `src/autoslice/style_profile.py`：

- 规则版 evidence generator 根据 source cues/title 估计 foreground song overlap、song completeness、lyrics alignment readiness、start/end boundary、open loop、payoff、standalone、editorial seed score。
- 歌曲重叠但不完整或 lyrics auto alignment 未实现时保持 BLOCK/DROP 安全姿态，不自动放行。
- `ManualStyleProfile` 以 Ivan 手动切片偏好的时长 IQR、hook title pattern、payoff、上下文完整性和负样式模式打 `style_match_score`。profile 缺失时 editorial score 置空并 fail-closed；重复度 gate 仍优先于高 style score。

### shadow pipeline runner

新增 `scripts/run_auto_review_shadow_pipeline.py`：

- review-package 模式读取 `review_manifest.json` 与现有 `*.jingting.*` artifact，生成 `evidence/*.evidence.json`、`summary.json`、`README.md`。
- live-source 模式在只有 live sample MP4、没有 draft SRT/source ASR 时明确输出 `RETRY` + `DRAFT_SRT_MISSING`，不伪造字幕或 evidence。
- live-source 模式现在也能接收 source-context job、source-level draft SRT、agy/refined SRT provenance：先调用 `execute_source_context_job()` 写 context draft/refined/cues/manifest，再把 refined context SRT 转成 source-absolute `SourceCue`，进入 content evidence、manual style profile 和既有 `review_candidate()` gate。
- CLI 暴露 `--source-srt`、`--refined-srt`、`--source-context-job`、`--agy-model`、`--agy-rc`、`--agy-fallback-used` 与 `--skip-ffmpeg`，用于本地 dry-run 集成验证；默认仍不上传。
- 默认 shadow/no-upload；runner 只写报告，不触发 Bilibili uploader 或 `free` 部署。

本地 P7 shadow 输出曾验证过下列结论；原始 `reports/auto_review_shadow/**` 和
`reports/auto_review_replay/**` 运行产物已在 2026-06-26 本地清理中删除，避免把
一次性 replay 证据继续伪装成当前 source of truth：

```text
counts: candidates=10, content_evidence=10, style_profile_evidence=10, auto_upload=0, block=10
gap_summary: JINGTING_PROVIDER_FALLBACK_UNKNOWN=10, SONG_PARTIAL=3, LYRICS_ALIGNMENT_REQUIRED=3

counts: candidates=1, retry=1
gap_summary: DRAFT_SRT_MISSING=1
```

这说明 P7 已能输出真实 rule-based evidence/gap summary，并且 live-source shadow runner 已接上 source-context executor 的本地 contract；但它仍不是完整无人发布流水线。当前最大缺口是：真实 source ASR/draft SRT 生成、真实 agy 调用/调度、歌曲歌词自动对齐、duplicate/PTS/标题封面 artifact hash 还需要接真实产物后才能考虑 auto-upload。

## 分阶段实施

### 阶段 0：当前 10 条与风险控制

- 保持 `upload_enabled=false`。
- 4 条 `japanese_song_or_lyrics_alignment_required` 直接 BLOCK。
- 对当前 10 条从源录播取前 90s / 后 150s 上下文重新分析。
- 不只依赖片内 `.jingting.srt`，因为片内字幕看不到被切掉的上下文。
- recut 后重新生成视频、字幕、title、cover、publish.json、evidence。

### 阶段 1：本地最小垂直切片

目标：先在本地建立 auto-review gate，证明 `.jingting.done` 不是 release-ready，`review-required` 会阻断，topN 不会凑满。

改动：

- 新增 `src/autoslice/auto_review.py` 纯逻辑模块。
- 新增 `tests/test_auto_review.py`。
- 上传/发布逻辑后续只认 `AUTO_UPLOAD` manifest。
- 当前先不部署到 `free`。

### 阶段 2：边界 resolver

目标：将 CPA 输出从最终 start/end 改成 anchor；根据源上下文、agy 精听、VAD/song spans 输出 resolved boundary。

改动：

- 新增 `src/autoslice/boundary_resolver.py`。
- 将 `SLICE_DURATION=90` 降级为弹幕召回窗口。
- `GLOBAL_SLICE_NUM` 改成上限。
- 找不到自然边界时 DROP，不硬裁。

### 阶段 3：本地 replay 验证

用 2026-06-25 当前素材和历史 2026-06-19/20 样本做回归集：

- 好片段；
- 开头截断坏样本；
- 结尾截断坏样本；
- 歌曲中途截断；
- 高弹幕但无内容；
- 字幕偏移；
- 重复候选。

### 阶段 4：部署到 free 的 shadow mode

部署前必须：

- 本地 tests 通过；
- 本地 replay 生成 auto-review manifest；
- 不自动上传；
- 只在 `free` 上 shadow 决策，写 manifest 和报告。

### 阶段 5：canary / limited / full

```text
shadow：只决策，不上传
→ canary：每场最多 1 条
→ limited：每日/每场限量
→ full：最多 10 条，但允许 0 条
```

full 前需要历史样本证明：

- 话没说完严重错误率 < 1%；
- 歌曲截断错误率 < 0.5%；
- 严重字幕错误率 < 0.5%；
- 重复上传 = 0；
- provider/model 变化后重跑 golden set。

## 失败与风控

| 失败 | 行为 |
| --- | --- |
| CloudDrive `State not recoverable` | `RETRY_INFRA`，不得 release-ready |
| agy 超时/限流 | 指数退避重试，不静默换低质量 provider |
| provider/model 变化 | 暂停自动上传，重跑 golden set |
| reviewer 分歧 | recut 一次；仍分歧则 BLOCK/DROP |
| 找不到完整对话结尾 | DROP，不在 hard max 处裁断 |
| 歌曲边界不确定 | BLOCK/DROP |
| 日语歌词无法自动对齐 | BLOCK |
| ffmpeg 实际 PTS 偏差 | 重新编码，不使用不准确的 stream copy |
| 上传响应不确定 | 查询远端状态，禁止盲目重试导致重复投稿 |
| transcript 中有提示注入文本 | 字幕/弹幕视为不可信数据；LLM 不能直接控制上传器 |
| 授权状态不明 | BLOCK |

CloudDrive 不应作为正在执行任务的唯一工作目录。推荐：

```text
本地 durable spool
→ 全部处理和校验完成
→ 原子 rename
→ SHA-256 校验
→ 再同步到 CloudDrive
```

## 立即优先级

1. 写入本文件并将它作为 Kanban 任务图的设计依据。
2. 本地新增 auto-review gate 的 TDD 垂直切片。
3. 创建 Kanban 任务：gate、boundary resolver、local replay、free shadow deploy、independent review。
4. 本地测试通过后再同步到 `free`，先 shadow，不开自动上传。

## 2026-07-01 状态更新：自动歌切与谈话 fallback

当前 machine-readable 状态见 `docs/autoslice-capability-status.json`，人读摘要见 `docs/autoslice-capability-status.md`。

- 自动歌切 no-upload/shadow 能力已标记为 `complete_no_upload_shadow`：song candidate 只作为 anchor；有 `song_boundary.status=FULL_SONG_READY` 与 `lyrics_alignment.status=READY` 时，live-source runner 会输出完整歌曲范围的 `AUTO_RECUT`，否则继续 fail-closed。
- 自动谈话切片已补上 no-prepared-slices fallback：当天没有 prepared slices 但有 full source media + SRT 时，daemon 会运行 full-session selector，选择 setup/payoff/closure 候选并进入 live-source shadow pipeline，而不是直接 `no_prepared_slices` skipped。
- 这两个状态都不代表开启上传；发布仍必须满足 `AUTO_UPLOAD` decision manifest 与 artifact hash gate。
