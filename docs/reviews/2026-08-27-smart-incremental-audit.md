# 2026-08-27 智能增量审计实现记录

## 目标

将“只改了哪一点就只复核哪一点”固化为真实代码和可复放 receipt，而不是依赖对话记忆：

- video、subtitle、cover、boundary、title 分别保存 raw/canonical hash；
- SRT 自动定位 changed cue/window；
- cover 通过实际像素 bbox 验证声明 ROI；
- video 必须有 producer edit-window map，且绑定 parent/current video raw hash 和 operation id，否则回退整段复核；
- 最新真实 record/media 生成新 current lineage，旧 authority 只读保留；
- 1–2 个用户修改点默认整片复核；明确“只有这些错误”且 diff 完整覆盖时可定向；
  超过 3 个点按 exhaustive candidate 处理但仍要求逐点覆盖；恰好 3 个点保守整片复核。

具体规则权威在 `docs/pipeline/40-subtitle-text.md`、`70-cover.md`、`80-package-delivery.md`
和 `90-publish.md`；本记录只保存实现状态。

## 实现入口

- `src/autoslice/incremental_artifact_audit.py`
- `src/autoslice/operator_correction_policy.py`
- `scripts/build_incremental_artifact_audit.py`
- `scripts/plan_operator_subtitle_correction.py`

`build_incremental_audit` 只生成 component-delta plan；`seal_incremental_review` 在所有
changed scope 有真实 PASS evidence 后生成 create-only receipt；`validate_incremental_receipt`
现场重读 current bytes。receipt 明确不替代 package audit、最终 SRT、boundary、title-cover
QC、authorized manifest 或 upload gate。

## 验证

当前 focused 验证：

- `tests/test_operator_correction_policy.py`
- `tests/test_incremental_artifact_audit.py`
- 结果：14 passed；Ruff 通过。

测试覆盖 subtitle cue delta、1/2 点 whole-clip、明确 only-these-errors 定向、恰好 3 点
whole-clip、超过 3 点覆盖缺失、cover ROI bbox、video edit-map fallback、current snapshot
漂移和 create-only replacement。

## 与快车道的关系

- 2026-08-13“动画看到关键处却没下一集、李豆沙说‘星兰不会死啊’”的 C3：内容修复和
  current source binding 已有独立实现；新 receipt 只能缩小复核范围，不能替代尚缺的同包
  final-review/owner/source-separation/cover evidence。
- 2026-08-14“弹幕把《坏结果》写错，小李却只听过《大结果》”的 C6：最新 record/media
  可作为 current 重新绑定，但必须生成新的 authority lineage，不能直接改旧 authority；
  旧 authority 与实际 record 的 boundary fingerprint 仍需合法重封。
- C3 仍是第一串行发布门；在 READY_FOR_SERIAL_UPLOAD 之前不得上传后续候选。
