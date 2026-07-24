# Autoslice local capability map

Updated: 2026-07-23

> 本文件只列本地 checkout 中的实现/测试入口，不是流水线规则，也不是 production 状态页。
> 当前规则只从 [pipeline/README.md](pipeline/README.md) 进入。版本号和 runtime 结论不在
> 本文件固化。

## 状态词

- **本地已实现**：所列源码入口存在于当前 checkout。
- **有测试入口**：所列定向测试可验证该能力；只有实际测试输出才能证明某次执行 PASS。
- **已部署**：必须读取 `free:/opt/bilive/autoslice/repo/DEPLOYED_COMMIT`、部署文件 hash 与
  live smoke；本地 commit、dirty worktree 或测试 PASS 均不能代替。
- **线上已修复**：必须满足对应 step 的 live/public/Creator/section/ledger 终态。只有
  review package、dry plan 或某个 API 成功都仍是未证明线上修复。

| 能力 | 本地实现入口 | 代表性测试入口 | 当前规则 |
|---|---|---|---|
| 源录制与源完整性 | `ops/recording/`、`src/autoslice/source_integrity.py` | [test_source_integrity.py](../tests/test_source_integrity.py) | [10-source-recording.md](pipeline/10-source-recording.md) |
| 选片与 exact closure | `src/autoslice/semantic_candidate_selector.py`、`selection_scorecard.py`、`batch_terminal_state.py` | [test_selection_scorecard.py](../tests/test_selection_scorecard.py)、[test_recovery_review_rerun.py](../tests/test_recovery_review_rerun.py) | [20-selection.md](pipeline/20-selection.md) |
| 边界 | `src/autoslice/boundary_resolver.py`、`boundary_semantic_review.py`、`producer_boundary_resolution.py` | [test_boundary_resolver.py](../tests/test_boundary_resolver.py)、[test_boundary_semantic_review.py](../tests/test_boundary_semantic_review.py)、[test_produce_slice_boundary.py](../tests/test_produce_slice_boundary.py)、[test_runtime_architecture.py](../tests/test_runtime_architecture.py) | [30-boundary.md](pipeline/30-boundary.md) |
| talk 字幕与语义修复 | `src/autoslice/producer_text_pipeline.py`、`producer_text_finalization.py`、`final_review_auditor.py`、`final_review_contract.py`、`chat_alignment_context.py` | [test_semantic_authority_pipeline.py](../tests/test_semantic_authority_pipeline.py)、[test_producer_text_pipeline_final_review.py](../tests/test_producer_text_pipeline_final_review.py)、[test_chat_authority.py](../tests/test_chat_authority.py) | [40-subtitle-text.md](pipeline/40-subtitle-text.md)、[41-semantic-repair.md](pipeline/41-semantic-repair.md) |
| song lane | `src/autoslice/song_lane.py`、`song_alignment.py`、`song_completion.py` | [test_song_lane_reasons.py](../tests/test_song_lane_reasons.py)、[test_song_completion_offsets.py](../tests/test_song_completion_offsets.py) | [50-song-lane.md](pipeline/50-song-lane.md) |
| 标题 | `src/autoslice/title_policy.py`、`publish_staging.py`、`recovery_title_authority.py` | [test_title_policy.py](../tests/test_title_policy.py)、[test_recovery_title_authority.py](../tests/test_recovery_title_authority.py)、[test_publish_staging_recovery_title.py](../tests/test_publish_staging_recovery_title.py) | [60-title.md](pipeline/60-title.md) |
| 封面 | `src/autoslice/cover_generation.py`、`cover_route_evidence.py` | [test_cover_reference_authority.py](../tests/test_cover_reference_authority.py)、[test_cover_text_pixel_evidence.py](../tests/test_cover_text_pixel_evidence.py) | [70-cover.md](pipeline/70-cover.md) |
| 打包与审计 | `src/autoslice/producer_package_finalization.py`、`review_package_ass_audit.py`、`scripts/audit_lidousha_review_package.py` | [test_producer_package_finalization.py](../tests/test_producer_package_finalization.py)、[test_lidousha_review_package_audit.py](../tests/test_lidousha_review_package_audit.py) | [80-package-delivery.md](pipeline/80-package-delivery.md) |
| 授权上传 | `scripts/authorized_upload.py`、`src/autoslice/bilibili_member_api.py` | [test_authorized_upload.py](../tests/test_authorized_upload.py)、[test_bilibili_member_api.py](../tests/test_bilibili_member_api.py) | [90-publish.md](pipeline/90-publish.md) |
| durable 同 BV 修复 | `src/autoslice/same_bv_repair.py`、`scripts/authorized_upload.py repair-*` | [test_same_bv_repair.py](../tests/test_same_bv_repair.py) | [90-publish.md](pipeline/90-publish.md) |

## 当前已知能力边界

- 自动发现、语义记忆和视觉/声学 verifier 都有各自证据边界；以相应 step 为准，本表不把
  “代码支持”扩大解释成“所有内容都能自动判对”。
- durable 同 BV 状态机及其测试入口已经存在于本地源码；在 live deployed commit、真实
  repair journal 与发布 step 的公开闭环出现之前，状态仍只能是“本地能力”，不能写成
  “production 已部署”或“原 BV 已修复”。
- correction mutation 文字权威回执与 anti-wash 审计、reviewer-visible boundary evidence、
  post-end next-topic witness、完整 18k hash-bound context、final endpoint binding、动态 cap
  production seam、SC 前缀门、portable speaker SRT→ASS 重放审计和统一 cookie parser 也仍是
  当前 checkout 的能力；部署 commit、线上登录态与旧 BV 是否已修复必须分别 live readback，
  不能从本表推导。
- 模型/provider、配额、cookie、合集 ID、部署 commit、runtime health 与具体稿件状态都是
  易变 live state，只能现场读取。
