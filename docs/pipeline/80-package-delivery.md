# 80 打包与交付

本文件是打包步骤的**分步权威**。入口：`src/autoslice/producer_package_finalization.py`。

- talk 车道成品强制前置 manifest 在册片头（当前 Z1/Z2 按主片 SHA-256 稳定轮换，fail-closed，`branding_intro.py`，manifest `assets/lidousha/intro/branding_intro.v1.json`）；**歌切不带片头**。验收必须按 record 的 `intro_id` 对照 manifest 的 hash/时长，不能把 Z1 的 5749ms 写死。`AUTOSLICE_BRANDING_INTRO=off` 仅测试/应急。
- 片头在最终烧录内拼接，下游 sha256 绑定 with-intro 字节；`.srt`/`.ass` sidecar 保持内容时间轴，偏移记 `burned_preview.branding_intro.intro_offset_ms`。
- 终态跨面校验：`producer_text_finalization.py::verify_chat_authority_final_surfaces`（文字+染色真的落进交付 SRT/ASS 才算数）。
- 审计闸：`scripts/audit_lidousha_review_package.py`（歌切标题格式、字幕行长/行数/静置时长、对齐证据等）。历史/人工包默认按每行 18 字审计；autoslice Sapphire72 包必须在 `review_manifest.json.subtitle_visual_contract` 显式绑定当前渲染器的 2 行/28 字上限，审计器拒绝任何超过渲染器上限的自报宽松契约。
- 2026-07-22 起的新包按日期自动进入 StoryContract 严格审计（仍应显式声明 `story_contract_required=true`）、并必须声明 `run_mode` 与 `upload_allowed=false`；producer 的可选布尔值不能关闭新政策。审计器会用 record 中同一 StoryContract 重验最终 SRT、标题、封面文本及实际渲染行、南町专名/关系主张、字幕 hash 与 selection scorecard；封面内嵌的 contract 摘要也必须与 record 一致。任何旧字幕/旧标题/旧封面/旧 policy 字节混入都会把包判为不合规，而不是继续显示为当前成品。
- talk 包还必须携带并重算 `.clip-context.json`；record 的 artifact hash、StoryContract
  `clip_context_binding` 与 sidecar 内容必须三方一致。边界同理：human source endpoint 必须与
  boundary audit 精确一致，或完整 semantic review 为 PASS 且推荐 end 已实际 materialize。
- 封面审计按 `cover_route_decision.route` 分支验真：截图只验 source reference/final 像素/文字，
  CPA 只认真实 AI 调用与资产 hash；任何共用默认字段都不能跨路线充当证据。最终 package audit
  是上传 manifest/hash gate 的前置条件，不允许把“生成过 sidecar”当成合规。
- 汇总表时长必须优先使用 producer 最终 record 打印进 summary 的 `duration_ms`（边界自修复后的内容时长），其次才是 candidate 的 `effective_duration_ms`；原始选片锚点 `end_ms-start_ms` 只作旧状态兜底，不能把已延长的 5:00 成片仍显示成 4:32。
- 已为 `CURRENT + COMPLIANT` 的历史审片包不会因宽流水线指纹变化被 cron 自动重做。确需全量重出时，只能在新的 `RECOVERY_REVIEW` base 运行 `scripts/plan_recovery_review_rerun.py`：它要求源 state 字节 SHA-256、全部 CURRENT candidate allowlist、共同旧指纹和当前新指纹完全匹配，且 source/target 均无 `AUTO_UPLOAD`；旧 record 完整降为 `SUPERSEDED + STALE_PIPELINE`，新项以 `selected_repair` 入队，随后仍由正常 runner 生成 CURRENT 成品。禁止把旧 `review_ready` 手改成 failed，也禁止在旧 base 原地覆盖。
- recovery plan 同时写入 exact-no-backfill selection contract；本地审片包只能从最终 state 的
  exact CURRENT+COMPLIANT 交集逐 stem 重建，不能整目录复制 inherited delivery 或旧 summary。
  缺封面、缺 regression/record、非终态、pending/backlog 补位或集合不等都必须阻止覆盖旧本地包。
- exact-no-backfill 合同中的入选项失败时必须保留真实终态（`failed`、`boundary_unrepairable`、
  `speaker_review_required` 或 `speaker_evidence_insufficient`），并记录“合同禁止补位”；不得把它改写成代表可由候补替换的
  `candidate_rejected`。这样相关 failure-scoped fingerprint 变化后仍可自动重试。对于此规则上线前
  已被误标的记录，只允许在同一有效 exact contract 内、且保留上述 `rejected_status` 时迁移重试；
  普通 production 的 `candidate_rejected` 仍是终态，不能借此复活。
- 上传路径 fail-closed：无 `AUTO_UPLOAD` manifest + artifact hash 门就没有发布（AGENTS.md 方向）。
- tag 按成品字幕出（`upload_tag_policy.py`，Ivan 2026-07-13）。
