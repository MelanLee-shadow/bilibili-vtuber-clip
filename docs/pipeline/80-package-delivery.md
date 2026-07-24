# 80 打包与交付

本文件是打包步骤的**分步权威**。入口：`src/autoslice/producer_package_finalization.py`。

- talk 车道成品强制前置 manifest 在册片头（当前 Z1/Z2 按主片 SHA-256 稳定轮换，fail-closed，`branding_intro.py`，manifest `assets/lidousha/intro/branding_intro.v1.json`）；**歌切不带片头**。验收必须按 record 的 `intro_id` 对照 manifest 的 hash/时长，不得写死任一 variant 的时长。`AUTOSLICE_BRANDING_INTRO=off` 仅测试/应急。
- 片头在最终烧录内拼接，下游 sha256 绑定 with-intro 字节；`.srt`/`.ass` sidecar 保持内容时间轴，偏移记 `burned_preview.branding_intro.intro_offset_ms`。
- 终态跨面校验：`producer_text_finalization.py::verify_chat_authority_final_surfaces`。
  所有 applied/satisfied source truth owner 与未被更高权威覆盖的 reviewed baseline mapping
  必须在最终 clean SRT 和 speaker SRT 的原时间窗逐项存活；文字/说话人 ASS 也必须绑定同一
  最终文本与 hash。低权威 repair 只有在 owner 已通过后才能记为 superseded。
- current/story-contract 的 talk/recovery item 必须把 `speaker_srt`、`ass_path` 两份真实字节
  连同 `speaker_srt_sha256`、`ass_sha256` 放进 package。两条路径都只能指向 package-relative
  regular file；绝对路径、越界、缺文件以及路径任一层 symlink 都拒绝。song lane 不进入这条
  talk speaker gate。
- `review_package_ass_audit.py` 不能只看 ASS 存在或 hash：speaker SRT 还须匹配
  chat-authority 的 `final_speaker_srt_sha256`，ASS 须同时匹配 record
  `artifact_hashes.ass_sha256` 与 chat-authority `speaker_ass_sha256`。auditor 再从包内
  speaker SRT 按生产同一 `_layout_cue_for_display`、speaker ASS escaping 与厘秒 rounding
  重建全部 `Dialogue` events；event 数、start/end、完整文本和 LDS/GUEST style 必须逐项精确
  相等，缺失/非法 Dialogue 或任一投影漂移都阻断。
- 最终 SRT 先过 `lidousha-srt-release-policy.v1`：每个 block 必须被严格解析，连续编号、
  合法且正向的时间、至少 300ms、单调无 overlap、非空/非孤立标点/非单个汉字、媒体边界
  合法。producer、package auditor 与 uploader 各自重跑，不能复用一次自报结果。
- 审计闸是 `scripts/audit_lidousha_review_package.py`，当前输出必须为
  `lidousha-review-package-audit.v2`，policy epoch 必须精确等于
  `2026-07-23.final-artifact-gates.v3`。**schema 仍是 v2，epoch 才是 v3**；不要把仍合法的
  audit schema、`lidousha-cover-route-decision.v2`、`subtitle-redelivery-baseline.v2` 或
  `lidousha-branding-intro.v2` 机械改成 v3。audit 绑定 auditor/策略代码与关键资产的
  `policy_fingerprint`、auditor source hash 以及完整 portable `audited_inputs` 闭包；
  任一文件或政策漂移都使旧 audit 失效。单独一个 `passed: true` JSON 不是证据。
- 视觉排版另由 `review_manifest.json.subtitle_visual_contract` 约束：历史/人工默认 18 字；
  autoslice Sapphire72 显式绑定 2 行/28 字上限。严格 SRT 结构门和视觉行宽门不可互相替代。
- 2026-07-22 起的新包按日期自动进入 StoryContract 严格审计（仍应显式声明 `story_contract_required=true`）、并必须声明 `run_mode` 与 `upload_allowed=false`；producer 的可选布尔值不能关闭新政策。审计器会用 record 中同一 StoryContract 重验最终 SRT、标题、封面文本及实际渲染行、南町专名/关系主张、字幕 hash 与 selection scorecard；封面内嵌的 contract 摘要也必须与 record 一致。任何旧字幕/旧标题/旧封面/旧 policy 字节混入都会把包判为不合规，而不是继续显示为当前成品。
- talk 包还必须携带并重算 `.clip-context.json`；record 的 artifact hash、StoryContract
  `clip_context_binding` 与 sidecar 内容必须三方一致。整片 draft 在 sidecar 内完整保存
  （60,000 字硬上限、禁止截断）；18,000 字 supplemental prompt 必须从 sidecar 重新渲染并与
  StoryContract 逐字相等，并以完整字节送入 boundary/final review；任何 12,000 字兼容切片、
  超预算或 prompt 重渲染漂移都拒发。topic resolution/scoped graph context 也必须留在同一
  digest 内。
  边界同理：human source endpoint
  只作为下界并与 boundary audit 精确一致，**同时**完整 semantic review 必须为 PASS，
  推荐 end 已实际 materialize，四命题与 cue/syntax 门全部通过。最终 semantic review 还必须
  携带 PASS 的 `talk-boundary-final-endpoint-binding.v1`，证明推荐 cue/ms 与最终唯一 closure
  cue / snapped endpoint 完全一致；缺失、BLOCK 或 repair 后沿用旧 endpoint 回执均拒发。
- chat authority 的 `frozen-boundary-owner-contract.v1` 与 record boundary audit 必须携带
  完全相同的 required owner 列表；所有 owner window 都在最终边界内，
  `required_boundary_owner_verification=PASS` 且
  `final_boundary_required_exclusion_count=0`。裁掉 owner 后把它标成成片外不构成通过。
- correction pass 的 `final-review-audit.v1` 不是 package 放行证据。package 必须携带
  `final-review-audit.v2`，其 `reviewed_srt_sha256` 精确绑定包内最终 SRT，discovery 完整、
  finding 合同合法且为空、release gate PASS、boundary semantic PASS，并携带 PASS 的
  `subtitle-correction-mutation-audit.v1` 与上述 final endpoint binding。后两项由 exact-final
  contract 强制；因此“第二遍零 finding”不能替代 correction mutation authority，普通 semantic
  PASS 也不能替代最终 endpoint 精确绑定。provider/JSON 失败、null/non-list/all-invalid
  findings、任何未决项、缺失回执或 BLOCK 都阻断。
- 封面审计按 `cover_generation.route_decision.actual_treatment` 分支验真：所有路线都验
  最终 cover SHA 与 `lidousha-cover-rendered-text-pixels.v3`。包内必须同时有 final cover、
  `.cover.pre-overlay.png`、`.cover.title-mask.png`、`.cover.route-background.png`；auditor
  用 committed trusted font 和 `lidousha-cover-title-render-spec.v1` 重放背景 fit、glyph mask
  与 alpha composite，并要求重组结果逐像素等于最终 PNG。包外绝对路径、同名旁路文件、
  自报 bbox/font/字号都不能补证；关系型
  screenshot_direct 另验 full-frame/no-crop deterministic compositor proof，screenshot_polish
  与 CPA 另要求独立 final-participant verifier。CPA 只认真实 AI 调用与资产 hash；任何共用
  默认字段都不能跨路线充当证据。最终 package audit
  是上传 manifest/hash gate 的前置条件，不允许把“生成过 sidecar”当成合规。可移植交付包若无法访问
  record 中的远端 `final_cover` 路径，只允许回退到 manifest 明示的交付 `cover`，且该文件必须与
  record 的 `final_cover_sha256` 完全一致；不能按相似文件名或任意现存图片替代。
- 汇总表时长必须优先使用 producer 最终 record 打印进 summary 的 `duration_ms`（边界自修复后的内容时长），其次才是 candidate 的 `effective_duration_ms`；原始选片锚点 `end_ms-start_ms` 只作旧状态兜底，不能把已延长的 5:00 成片仍显示成 4:32。
- 汇总中的封面路线必须从校验通过的 `lidousha-cover-route-decision.v2` 投影实际执行路线、是否调用/采用 AI、选中理由和两个未选路线的拒绝理由。内部兼容状态 `AI_COVER_READY` 仅表示封面 artifact 已就绪，绝不能被报告解释成 AI 生图；缺少有效 v2 证据时必须显示 UNKNOWN/缺证。
- `reporting.py` 是从既有 state/record 生成只读审片报告的投影层，不属于会改变选片、字幕、边界、标题、封面或媒体 bytes 的 proof closure；内容与歌切流水线指纹都必须排除它。报告变化直接重写报告，不得唤醒成片重制或无关失败重试。
- 已为 `CURRENT + COMPLIANT` 的历史审片包不会因宽流水线指纹变化被 cron 自动重做。确需全量重出时，只能在新的 `RECOVERY_REVIEW` base 运行 `scripts/plan_recovery_review_rerun.py`：它要求源 state 字节 SHA-256、全部 CURRENT candidate allowlist、共同旧指纹和当前新指纹完全匹配，且 source/target 均无 `AUTO_UPLOAD`；旧 record 完整降为 `SUPERSEDED + STALE_PIPELINE`，新项以 `selected_repair` 入队，随后仍由正常 runner 生成 CURRENT 成品。禁止把旧 `review_ready` 手改成 failed，也禁止在旧 base 原地覆盖。
- recovery plan 同时写入 exact-no-backfill selection contract；本地审片包只能在
  `exact-talk-contract-closure.v1.status=COMPLETE` 后逐 stem 重建。每个 contract ID 必须
  恰有一个 `rc=0 + CURRENT + COMPLIANT`，且无 pending、missing、failure、重复/冲突或
  outside-contract attempt。否则 runner 与报告保持 `recovery_incomplete`，不得覆盖旧本地包
  或沿用 `review_ready`。
- state 的最终 status 必须来自 `batch_terminal_state.py` 的一次精确投影；future retry、
  部分 delivery 或报告层旧状态都不能盖过 incomplete exact closure。只有 closure COMPLETE
  才能投影 `review_ready` 并进入本地覆盖。
- exact recovery 重跑结束后必须用 `scripts/build_lidousha_recovery_review_manifest.py` 从最终 state 与 record **整份重建** `review_manifest.json`，禁止复用/手补上一轮清单。审计器必须比较 manifest item 与 record 的 candidate/title，并在存在 `cover_route_attestations` 时重验 reference/final hash、method、完整 route decision 与 reference authority；任一旧标题、旧封面 hash 或旧路由证据都要阻断上传。
- recovery 保留已发布 same-BV 标题时，包内必须额外携带 `.publish.json` regular file，并以
  record `artifact_hashes.publish_draft_sha256` 绑定。record 顶层、record
  `publish_staging`、publish draft 与 manifest item 必须携带同一个
  `recovery-public-title-authority.v1`；auditor 会重新读取其 repo-relative
  `authorized-upload-public-verify.v2`、复算源 SHA/authority SHA，并逐字比较 candidate、
  title、BVID/AID/CID。只有其中一面有 authority、裸标题相等但缺 receipt、或 publish hash
  漂移都拒发。Ivan manual title 和普通自动标题没有该 authority 时不伪造此字段。
- exact-no-backfill 合同中的入选项失败时必须保留真实终态（`failed`、`boundary_unrepairable`、
  `speaker_review_required` 或 `speaker_evidence_insufficient`），并记录“合同禁止补位”；不得把它改写成代表可由候补替换的
  `candidate_rejected`。这样相关 failure-scoped fingerprint 变化后仍可自动重试。对于此规则上线前
  已被误标的记录，只允许在同一有效 exact contract 内、且保留上述 `rejected_status` 时迁移重试；
  普通 production 的 `candidate_rejected` 仍是终态，不能借此复活。
- authorized uploader 在任何副作用前重跑**当前** canonical package auditor、严格 SRT 与共享
  标题门，并要求重跑结果与 manifest 绑定的 v2 audit 完全一致；它不信任旧 audit 自报。
- 上传路径 fail-closed：无当前 audit v2 + `AUTO_UPLOAD` manifest + artifact hash 门就没有发布。
- tag 按成品字幕出（`upload_tag_policy.py`，Ivan 2026-07-13）。
