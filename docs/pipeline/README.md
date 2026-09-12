# 流水线分步权威索引

**本目录是切片流水线的分步文档权威。** 结构规则：

1. **每一步的规则只写在该步的 step 文件里**（或 step 文件明确指向的更强机器权威：
   code/schema/profile asset）。skill 只能是操作配方，不能反向覆盖 step。
2. **其他任何文档（AGENTS.md、skill、memory）只允许放入口、
   操作方法或历史证据，不允许另立规则正文。** 复制即债。
3. **主会话先读本索引，再亲读本次任务涉及的 step**，确认当前要求后才发任务。
   委派包应给出实际 host/产物、适用 step、用户最新指令、改动范围和验收终点；不能让子任务
   从旧报告自行猜默认路线。无需为普通单步工作重读无关步骤或全仓。
4. 改某步规则 = 改对应 step 文件 + 它指向的代码/资产强制层；随后必须扫描 README、skills、workflows、assets 与历史 runbook 中的冲突措辞。历史事实可以保留，但必须有醒目的历史快照标记和当前入口。
5. 纠偏时先核对已有实现：若只是文档落后于已生效的用户要求/代码，修正文档并检查链接和
   冲突措辞；若行为本身错误，再修改对应代码/资产并运行相关验证。不能把“增加新门、全量
   测试”作为每次文档纠偏的前提，也不能把文档更新宣称为代码已部署。
5b. **先找门，再建门。** 新增任何 gate / reason code / 校验谓词之前，必须先 grep 既有
   reason code 与谓词，并在 commit body 里写明「查过：没有 / 有但不覆盖 X」。
   反复实证的同一个病——**门建好了，但消费位接错或没人知道它在**：为救援而建的
   witness 被接成整体替换既有路由；「待建的门」其实早已实现且 reason code 更精确，
   新加的重复门只会遮蔽它；「待建的模式」其实本就是现行为。
   查的成本是一次 grep，不查的成本是一条遮蔽门加一轮返工。同理适用于测试：补测试前
   先数既有覆盖，把缺口收窄到可陈述的那几个函数，别补一批已有的。

6. runtime 状态不固化进本目录。按 [10](10-source-recording.md) 确认当前 host 和 source root，
   读取当前部署的 `$AUTOSLICE_BASE/{repo,state,out,reports}` 与 B 站公开/创作中心面；
   不能把旧部署路径当成永久 authority。
7. 同一适用范围的用户要求有冲突时，按**用户消息原始时间**采用较新的要求，明确标记
   被替代的旧条款；会话摘要、助手提议、代码注释和文件修改时间都不能代替用户裁定。
   候选专属授权不扩大成全局规则，实验/诊断路线不自动晋升为日常默认。

本轮逐字出处、冲突处理及实现/部署差异见
[2026-09-06 要求核对](../reviews/2026-09-06-pipeline-requirements-reconciliation.md)。
该报告是证据索引；规则正文仍只在下面各 step。

提速并行设计的历史证据与当前映射见 [source-bound review](../reviews/2026-08-23-pipeline-speedup-source-bound.md)；
该页仅作 provenance/入口，不增加分步规则；并行与发布边界见 [80-package-delivery.md](80-package-delivery.md)、
[90-publish.md](90-publish.md) 及上述 speedup source-bound 文档。这里不写死任何当前
runtime 状态。

| 步 | 文件 | 职责 | 代码入口 |
|---|---|---|---|
| 10 | [10-source-recording.md](10-source-recording.md) | 录制、源健康、mount 看门狗 | `ops/recording/bililive_recorder_adapter.py`、`scripts/session_autoslice.py`（源门）、`src/autoslice/source_integrity.py` |
| 20 | [20-selection.md](20-selection.md) | 候选召回、Tier/量化校准、exact 状态、**同主题合并** | `src/autoslice/semantic_candidate_selector.py`、`selection_scorecard.py`、`candidate_selection.py`、`batch_terminal_state.py` |
| 30 | [30-boundary.md](30-boundary.md) | 双层语义边界：source full-window 见证、resolver 与 final-delivery 重审 | `src/autoslice/boundary_resolver.py`、`boundary_semantic_review.py`、`producer_boundary_review_stage.py`、`producer_boundary_resolution.py`、`producer_boundary_owner_contract.py` |
| 40 | [40-subtitle-text.md](40-subtitle-text.md) | 字幕文本链：ASR→专名→弹幕→语义修复→materialize 后精确 SRT 终审 | `src/autoslice/producer_text_pipeline.py`、`producer_source_truth_authority.py`、`chat_read_aloud_candidates.py`、`chat_event_timing.py`、`entity_audio_verifier.py`、`clip_context.py`、`topic_entity_graph.py`、`chat_alignment_context.py`、`final_review_contract.py` |
| 41 | [41-semantic-repair.md](41-semantic-repair.md) | 语义修复、专名/幻听/长程呼应与权威裁决 | `src/autoslice/producer_text_finalization.py`、`source_subtitle_truth.py` |
| 50 | [50-song-lane.md](50-song-lane.md) | 歌切专线：识别、LRC 对齐、host-vocal 证明、完整性 | `src/autoslice/song_lane.py`、`song_alignment.py`、`song_completion.py` |
| 60 | [60-title.md](60-title.md) | 标题（谈话 + 歌切铁律） | `src/autoslice/title_policy.py`、`publish_staging.py` |
| 70 | [70-cover.md](70-cover.md) | 封面路由、最终像素、人物与真实字形 | `src/autoslice/cover_generation.py`、`publish_staging.py`、`cover_repair.py`、`cover_title_rendering.py`、`cover_text_pixel_evidence.py`、`cover_route_evidence.py` |
| 80 | [80-package-delivery.md](80-package-delivery.md) | 打包、片头、严格 SRT、raw-byte 与双层边界回执闭包 | `src/autoslice/producer_package_finalization.py`、`subtitle_validation.py`、`review_package_ass_audit.py`、`review_package_boundary_contract.py`、`scripts/build_recovery_review_manifest.py`、`scripts/audit_review_package.py` |
| 90 | [90-publish.md](90-publish.md) | authorized upload、最终感知复核 receipt、durable 同 BV 修复、合集与公开验证 | `scripts/build_final_human_review.py`、`scripts/authorized_upload.py`、`src/autoslice/final_human_review.py`、`src/autoslice/same_bv_repair.py`、`bilibili_member_api.py` |
