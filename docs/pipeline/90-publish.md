# 90 授权上传、同 BV 修复与公开验收

本文件是发布步骤的**分步权威**。唯一程序入口是
`scripts/authorized_upload.py`；`.agent/skills/bilive-autoslice-publish/SKILL.md`
只提供操作顺序，不得另立规则。

## 机械验收与真实人工复核的分流（2026-09-14）

维护者 最新要求：已验证的机械烧录流程及本次输入/输出检查正确时，不必每片再完整读/看一遍。
正常交付不额外增加全片人工或模型观看；流程/字体/布局变化与具体异常只做影响范围复核。
本段替代下文历史“每片完整观看”的普遍前置，不抹掉既有内容失败、原件缺口或真实发布权限。

普通新 BV 的当前代码本就不要求 `final_human_review`；不得在其现有 package/声文/边界/
标题封面/授权门外凭报告添加这一项。对有 recovery authority 的同 BV 包，正常机械路线为：

```sh
python3 scripts/build_final_human_review.py <package> \
  --package-audit <current-audit.json> --mechanical --out <package>/verification/mechanical-delivery-review.json
```

该命令复用当前 canonical auditor、同候选/目标/标题/封面/片头绑定和 create-only writer，输出
独立的 `lidousha-mechanical-delivery-review.v1 / VERIFIED_MECHANICAL_DELIVERY`。
`fresh_human_full_playback_claimed=false`、`new_upload_authorized=false` 必须保持；没有 reviewer
个人身份、伪造八项观看 observations 或补签旧人审。receipt 绑定当前 review manifest/audit、
实际 record/video/SRT/cover 字节与精确 publication authority；构建和使用时均重跑当前审计并
重新计算绑定，旧 PASS、损坏/漂移包、错 BV/CID 或未决内容不能借此放行。

`make-manifest --final-human-review <mechanical-receipt>` 仅是既有兼容参数名；validator 会按独立
schema 走机械重验，并继续拒绝新建 BV、越权、错目标及未知上传效果重发。接到这个入口不等于
已经完成任何稿件投稿。已有原稿+delta/C1 技术路线继续适用，不重建其历史证明。

真正需要并实际完成人工复核时，仍可使用下文 v2 人工路线；它的身份、具体 observations、
完整播放声明必须真实，不能把本机械规则解释成可以给人工 v2 模板自动填 PASS。
旧进行中事务按它原先封存的 receipt 继续恢复，不为切换验收类型重发平台变更。

## 快车道授权与串行发布边界

对 Claude line 947 穷举批次，候选点名错误修复完成即使用本页既有 package、manifest、
audit、授权和公开验收门进入上传；不新增 维护者 二次看片/复审。按 维护者 2026-09-08
[最新就绪优先指令](../reviews/2026-09-08-ready-first-publication.md)，已通过全部发布门的候选
先进入串行上传队列；较早候选未就绪不能卡住后面的合格片。就绪集合内优先时效性，其余
按原批次顺序选取；问题候选留在独立修复队列。candidate-private prepare/package/QC 可并行；transcription/AGY 之后仅 bounded per-cue
short calls 彼此并行，待字幕、媒体和标题输入冻结后 burn 与 cover 可并行；commit lease、formal state+journal、same-BV apply、upload mutation 与 queue
advancement 必须串行。mutation 完成后 public/Creator/section 三个 read-only probes 可并行，
但 joint acceptance 是屏障，三面收敛前不得释放下一候选。workflow 病因修复可并行推进，但
不能改写 artifact release critical path 或重新引入 维护者 review node。`review_ready` 不是
publication 状态。

## Readiness graph 不授权发布

固定 no-arg 的 scripts/publication_readiness_graph.py 只能读 canonical registry/runtime
state、authorized-upload manifest 与 ledger，输出观察分类和依赖；它不调用 provider、B 站或
Creator，也不写 state/ledger/package。READY_TO_PREPARE 与 review_ready 都不是上传许可。
只有 READY_FOR_SERIAL_UPLOAD 所列的已封存授权 manifest 经 load_and_verify、package audit、
title-cover QC 和 ledger 无歧义重放后，才可被排入串行候选；仍须使用 manifest 中 维护者 的
逐字授权，绝不把 graph 当 authority。单批、单部署与完整 suite 是实际 preparation/deploy
的准入规则，不是该观察分类自动授予的后续动作。

观察图必须识别真实 `uniform_host` 成品的 `burned_preview.ass_path`：没有单独说话人
ASS 时，只有 `status=BURNED` 的嵌套定位符可补充空的 `subtitle_ass_path`；文件仍须为
包内无链接普通文件且匹配 record 的 `ass_sha256`。两处都声明时各自验真，不得用一个
有效副本掩盖另一处路径越界、缺失或字节漂移。这只修正观察定位，不替代 canonical
package audit、联合检查、授权 manifest 或发布门。

已装配单候选包的观察图，须通过现有 `resolve_package_inputs` 选择 review manifest 中与
最终视频同 stem 的封面，不能把生成目录中的同字节图片路径当作最终质检的图片身份。
选择前重验当前候选、canonical record、publish/video 定位与唯一 manifest；原生成图片和最终
副本均须为安全普通文件、哈希相同。当前 manifest 缺文件、路径/身份漂移或多个候选时仍拒绝，
不得回退旧图来通过。尚无 canonical review manifest 的准备阶段保留旧生成路径规则。
此定位修复不改旧 QC 回执、不调用 provider，不替代实际上传器的原回答、哈希、audit 和授权验证。

## 增量 receipt 与发布隔离

`incremental-artifact-audit.v2` 只说明当前版本相对上一版本哪些组件、cue、时间窗或封面 ROI
真的变化，以及哪些复核已完成。它不能把未变组件的历史 evidence 从 `FLAGGED` 或 hash
漂移状态中“洗成”有效，也不能把 `review_ready` 变成上传授权。`authorized_upload.py` 仍在
副作用前现场重跑 package audit、标题/封面 QC、manifest hash 和 ledger；任一 current
record/media/authority 不一致都拒绝。

增量入口还必须封存产物角色；角色与 ancestor record hash 闭包必须写入最终 record 并绑定到
snapshot/manifest，CLI 参数只是显式声明，不是独立信任来源。`RELEASE_CANDIDATE` 才能进入发布门；
`DIAGNOSTIC_TRAINING`
只用于把流水线输出与人工真值逐项比较，强制 `RELEASE_EXCLUDED`，不能被较新的文件时间自动升级；
`HISTORICAL_EVIDENCE` 只能保留为不可变 parent，不能成为 current 发布产物。时间优先级只适用于
同一角色、同一 lineage。诊断样本默认保留，待无引用扫描、独立封存和清理授权完成后才可删除。

因此，旧修复即使内容上符合 维护者 要求，也必须先被重新绑定为同一 canonical package 的
`RELEASE_CANDIDATE`，通过本页所有现有门禁；较晚的流水线诊断输出不得取代它，也不得被误传。

部署树中的 `assets/lidousha/publication_registry.v1.json` 由
`DEPLOYED_AUTHORITY_MANIFEST.json` 以当前 `DEPLOYED_COMMIT` 封存，运行中的发布对账不得
改写这份静态文件。部署模式下，`state/publication_registry.runtime.v1.json` 是经 authority
hash 校验的新增发布投影；`load_publication_registry()` 读取静态登记并合并该 runtime overlay，
所得结果才是生产上传/封面闸口使用的有效登记。静态文件、`DEPLOYED_COMMIT`、
authority manifest 缺失、漂移、损坏或含 symlink 时，对账必须在任何 runtime/state/recovery
写入前失败关闭；完整部署树及其模式仍由 no-upload soak gate 验证。普通 Git/本地工作树仍按
原规则更新静态登记。

## Reviewed-baseline replay 与上传隔离

`scripts/replay_reviewed_subtitle_baseline.py` 的 PLAN、full-dry-run、apply 与 readiness
graph 都是 no-upload package-recovery lane，不是本页的投稿入口。PLAN 只读 authority；普通 full
dry-run 在 private stage 完整验证 after-image，普通 apply 仍只接受 `candidate_rejected` 并在短
runner lease 内以最新 state CAS 顺序 state-last commit。候选已是 `published` 时，必须显式给
`--published-recovery-bvid`；此时 full-dry-run 只验证 package-only after-image，apply 还必须给新的
私有 `--recovery-package-root`，只落 `state_transition=none` 的 audited same-BV 包，绝不触碰
production state/package/delivery。包内 preflight + typed package receipt 把当时 state SHA、完整
published tuple、C4 predecessor（若适用）、deployment/publication authority 与最终媒体纳入 canonical
inputs；外层 receipt 必须为 `VERIFIED_PRIVATE_PACKAGE`。在冻结授权 manifest 前还须重跑 PLAN，
确认 current state/BVID 未漂移。两种模式都不得创建 `AUTO_UPLOAD`、authorized-upload manifest、
upload ledger row 或获取 `upload.lock`。完成 package transaction 后仍须重新通过本页的 canonical
audit、CPA title-cover QC、human/recovery evidence 和 explicit 维护者 manifest；`make-manifest` 会把
outer receipt path/hash 冻结进 package attestation，`verify`、`repair-plan` 与 live resume 会重放它；
只有 `authorized_upload.py` 在单一
`upload.lock` 下可产生上传副作用，且 transport/状态歧义不得重试上传。

## 发布准入

- 新生成的谈话投稿和同 BV 视频修复 manifest 必须消费 [80](80-package-delivery.md) 的最终
  声文对应证据；`make-manifest`、`verify` 与实际变更前重读包内见证并重新计算检查结果，
  缺失、整体错位、锚点不足和哈希漂移均拒绝。已冻结、正在平台审核中的旧修复事务继续按
  既有 resume 合同校验原字节；仅换封面且已有 scope 证明视频未变的事务不重新调用 ASR。

- 每条都需要 维护者 明确授权；manifest 保存授权原话，工具不能替用户创造授权。
- publication registry 的人工搁置不得靠删除历史来解除：
  维护者 明确放行后改为 `released_for_upload`，保留放行日期/原话与剩余准入门；
  该状态只解除 `hold_pending_review` 阻断，不等于机器 audit 通过，也不产生上传副作用。
- 新投稿只接受 `authorized-upload-manifest.v3`。它必须绑定同 stem 的最终视频、封面、
  record、SRT、review manifest、冻结标题、最终 tags、StoryContract 与当前 package audit。
- package audit 必须为 `lidousha-review-package-audit.v2`，policy epoch 精确等于
  `2026-09-17.host-only-v4-package-binding.v7`。audit schema 仍是 v2；policy fingerprint、
  auditor source hash 与完整
  portable `audited_inputs` 闭包有效。上传器在任何副作用前重跑当前 canonical auditor、
  严格 SRT 与共享标题门，并要求结果与 manifest 绑定一致；自报 `passed:true` 不算。
- HOST_ONLY v4 publication 还要求 manifest item 的 `lidousha-host-only-v4-package-binding.v1` 现场
  重放通过：canonical receipt、comparison、reference、final cover 必须都是包内 canonical regular
  file，四面 SHA 闭合，receipt 与三面 generation verification 完全相同。缺 binding、旧 v1 successor、
  任一 artifact 缺失/漂移、locator 逃出包根或经过 symlink，都以
  `COVER_HOST_ONLY_V4_PACKAGE_BINDING_MISSING_OR_INVALID` 阻断。安全 v2 successor 即使 audit 通过也只到
  ready-unpublished，不能自行产生上传权。
- talk 标题统一 profile 前缀 envelope（默认 profile：`【李豆沙】`），song 精确目录式；人工正文不能绕过外壳、长度或
  结构门。tags 必须逐项等于 audited record 的冻结结果。
- 新 BV 没有当前 audit + CPA 标题/最终封面联合质检 receipt + `AUTO_UPLOAD` manifest +
  artifact hash gate 就不得发布。联合质检不是操作者看完后可以旁路的口头步骤：必须通过
  `make-manifest --title-cover-qc <receipt>` 冻结进
  `package_attestation.title_cover_qc={path,sha256,bytes}`，随后 `verify` 与 `upload` 都现场重读
  receipt 与最终封面字节。
- exact same-BV repair 不消费 `AUTO_UPLOAD`；它只接受 exact closure COMPLETE、当前 audit、
  authorized manifest、最终感知 receipt、recovery publication authority 与 artifact hash
  全部绑定的既有稿修复 lane，并继续保持 `upload_allowed=false`。
- 授权上传/修复的证据必须 commit；媒体本身不因此入库。

## 已受审原稿的同源定点纠正

维护者 2026-09-08明确快车道是“在原本听写稿件上，就地修改我指出的错误然后发布”。对已经发布、
已有hash固定的受审原稿且不改变源镜头范围的修复，允许独立的
`original-reviewed-fastlane-package.v1`与`original-fastlane-delta-technical-review.v1`。
它不是新一次人工全片观看，必须明示`fresh_human_full_playback_claimed=false`；不能把reviewer伪写维护者。
准入由`original_patch_package.py`独立重验：仓库封存原稿+明确补丁、原/新主素材相同、原/新完整音频
PCM相同、片头字节与offset相同、原/新窗口一致、实际ASS文字与时钟、独立最终声文见证、未改封面、
新标题事实与标题封面联合检查。源原稿不被新ASR覆盖，不为补“新的recut证明”再次裁切。

只有独立committed `.original-repair-target.v1.json`与`.original-delivery-closure.v1.json`，且
target源于fresh登录canary/public/Creator/section、精确绑定原BVID/AID/CID、旧/新标题、原授权引用，
才可用此lane。所有实际文件列入闭包并逐字节重验；未变文件的旧失败不能被技术receipt洗成PASS。
`original_patch_review.build_technical_receipt`只能对当前canonical audit通过的闭包生成机器delta证据。
现有`--final-human-review`参数作为兼容载体接收这个独立schema，不能伪装成`lidousha-final-human-review.v2`。
既有make-manifest/verify/repair-plan/dry-run/repair-run/verify-live与单upload锁保持；只能修原BV，
普通upload明确拒绝recovery authority，不扩大授权，不自动放行其他候选或未review的素材。

## 选择真实人工复核时的 v2 receipt 权限边界（非每片默认前置）

- recovery review manifest 的
  `finished_review_package_no_upload_pending_human_review` / `upload_allowed=false`
  是机器打包状态，不是发布许可。已选择人工 v2 的证据不能由机器填写；正常机械验收使用
  上面的独立 schema，不能伪装成这份人工证据。
- `lidousha-final-human-review.v2` 只接受
  `scope=same_bv_repair`、`status=ACCEPTED_FOR_SAME_BV`。`reviewer_kind` 可为
  `human_owner`、`human_delegate` 或 `delegated_root_agent`，但必须与真实观看者一致：
  owner 固定 `reviewed_by=<owner 标识>`，root agent 固定
  `reviewed_by="<root-agent 标识>"`，human delegate 写实际姓名；带时区 `reviewed_at` 记录实际完成时间，
  `approval_quote` 保存授权/委托原话。委托 agent 自审不等于 维护者 亲自观看，严禁把
  `reviewed_by` 伪写成 维护者。
- receipt 以 committed `assets/lidousha/final_media_review_contracts.v1.json` 的 SHA-256
  为 review contract authority。每个 candidate 必须逐点复核 exact final-video window 与
  expectation 并提供 PASS/evidence；八个总检查也各自需要非空具体 evidence，裸 PASS 或任意
  泛化检查表无效。逐 candidate 的 exact review point 来自 profile 的
  final_media_review_contracts 资产（本仓只有空模板，示例见其 `_example` 条目）。
  本段只约束所选的真实人工路线，机械验收按前述分流执行。
- receipt 同时绑定 create-only 提交的
  `lidousha-final-human-review-evidence.v2` 路径、SHA-256 与字节数，以及 package review
  manifest/audit、record、reviewed title、原 BVID/AID/CID publication target 和 final
  video/subtitle/cover。validator 必须重读 evidence 文件，验证其仍为完成状态并逐字段重建
  receipt；路径、字节或内容漂移都拒绝。封面 claims 集合只能精确投影 record
  StoryContract `cover_reference_authority` 的 `source_visible_claims → SOURCE_FRAME`，
  以及当前 record 中与最终封面 SHA-256 互相绑定的
  `cover_generation.rendered_lines + rendered_text_pixels → COVER_TEXT`。
  `narrative_presentation` 只是创作指导，不得被升级为最终 PNG 实际显示的文字；
  reviewer 也不能自行换成宽泛故事摘要。字段上线前已冻结的历史 receipt
  仅做只读兼容，新修复包缺当前 rendered-text 像素绑定必须拒发。
- receipt **仅准入 exact same-BV repair**。它不会把 `upload_allowed` 改成 true，不是
  `AUTO_UPLOAD`，不授权新建 BV，也不替代 authorized manifest 中 维护者 针对修复动作的授权原话。
  普通新投稿不得传 `--final-human-review` 借用这份权限。
- authorized manifest 以
  `package_attestation.final_human_review={path,sha256,bytes}` 冻结 receipt；repair plan 再冻结同一
  package root、review manifest 与 receipt。receipt 路径/hash、review contract hash、
  package evidence、reviewer identity、candidate/record/title/publication target、
  final video/subtitle/cover 路径/hash、exact review points、八项 checks、StoryContract
  源帧声明、最终渲染文字像素绑定或包内文件
  任一漂移，`verify`、`repair-plan`、`repair-run`、`repair-status` 都必须在 adapter 构造或
  远端变更前拒绝。不得编辑 receipt 后只更新 manifest hash 来“续期”旧人工结论。
- 无多人物 source-frame authority 的 host-only 封面，只能从 record 中 hash-closed 的
  最终渲染文字生成 `COVER_TEXT` claim。`screenshot_direct` 必须绑定
  `SOURCE_SCREENSHOT + image_generation_used=false`；`screenshot_polish` 必须绑定
  `SOURCE_SCREENSHOT_AI_POLISH + image_generation_used=true`；`cpa_redraw` 必须绑定
  `AI_REDRAW + images.edit + gpt-image-2 + image_gen_model=cpa`，且 generation/route 两面都
  明示实际使用了生图。三种 provenance 不得混用，AI polish/redraw 也不会因此获得人物身份
  或 source-visible claim authority；receipt 只投影实际渲染文字，最终视觉身份仍由
  `cover_identity` 感知检查如实验收。
- receipt 只能由 `scripts/build_final_human_review.py` 从完成后的
  `lidousha-final-human-review-evidence.v2` 构建，禁止手写 PASS receipt。先在最终包和 current
  package audit 冻结后运行 `--prepare-evidence-template`；模板必须绑定 committed review
  contract、review manifest、current audit，以及每项 record/video/subtitle/cover 的当前
  SHA-256。实际 reviewer 完整播放后再如实填写 reviewer 元数据和每一条结构化
  `{anchor,detail}` observation；placeholder、裸 PASS、编号式“均无异常”、复述 expectation、
  跨点复用或 NFKC/去编号标点后近重复的 detail 均拒绝。
- 用同一脚本和完成后的 evidence 构建 receipt 时，builder 必须重跑 canonical package audit、
  重验所有 byte bindings 与正式 receipt validator，并以 create-only 方式提交输出；任何输入
  漂移都要求重做 evidence/复核，不能自动改绑。若输出名已提交但临时清理或目录 fsync 失败，
  CLI 返回 `COMMITTED_BUT_DURABILITY_UNCONFIRMED`（rc=3）；此时不得重跑覆盖，须先检查已存在
  receipt 的真实字节和目录持久性。

## 新投稿流程

1. 在审片字节冻结后，以完整投稿标题和同 stem 最终 `.cover.png` 运行 CPA 联合图像质检。
   receipt 必须是 `lidousha-title-cover-joint-qc.v1`，并精确绑定 record 的 `candidate_id`、完整
   `title`/UTF-8 `title_sha256`、最终绝对 `cover_path`/`cover_sha256`；只能接受
   `selected_provider=cpa` 且内嵌 `cpa-frame-witness.v1` 为 `provider=cpa`、
   `status=OBSERVED`、图片路径/hash 等于最终封面、可解析的 witness answer 与 `verdict` 完全
   相等。最终 `status=PASS`、顶层 `pass=true`，且 verdict 必须同时满足：
   `lidousha_primary=true`、`thumbnail_readable=true`、`physical_text_line_count` 为 1 或 2、
   `single_clear_hook=true`、`text_overcrowded=false`、`title_cover_aligned=true`、
   `unrelated_or_misleading_elements=[]`、`pass=true`。任一不确定、AGY 代答、无关/误导元素、
   主体不突出、破碎钩子或三行以上叠字都必须 BLOCK 并先重做封面/标题。
2. 在上述 receipt 和审片字节冻结后运行 `make-manifest --title-cover-qc ...`，只引用 package 内
   最终文件与 维护者 授权原话。普通新 BV 缺 receipt 直接拒绝；exact same-BV repair 继续走下文
   `--final-human-review` carrier，按前述机械/真实人工 schema 分流。
3. 先运行 `verify`；它必须重算全部 hashes、current audit、联合质检语义与政策绑定。
4. `upload` 只从 manifest 取路径/标题/元数据，禁止再手输一套参数。ledger 对同 artifact
   重传硬拒；拿到 BVID 后不得为“再取一次结果”重跑上传。
5. 合集 lane 由冻结标题确定（合集名属频道运营配置；默认 profile：talk →
   `小李切片`，song → `小李歌唱`）。发布未精确入集不算
   完成；已投稿但合集/公开验证未闭环时只运行幂等 `season-add`，绝不重传。

滚动 24 小时配额由 ledger 与 Creator 近期稿件共同估算，取较大值；达到 10 条或 B 站返回
21566 时停止新投稿。配额不授权降低内容/审计门。

## 已发稿修复：只改同一个 BV

字幕、边界、片头、视频字节、标题、封面或 tags 的修复都编辑原稿，不新建 BV、不删稿。
唯一入口是
`scripts/authorized_upload.py repair-plan / repair-run / repair-status / repair-verify-live`，
核心状态机在 `src/autoslice/same_bv_repair.py`。legacy `swap_video_p.py`、
`bili_archive_tool.py replace`、裸 API 和手工 append/edit 仍禁止。

### 仅换封面的窄路线

当且仅当视频字节、字幕、标题、简介、tags、分区、版权、source、唯一 P/CID 与 exact-section
成员关系全部保持不变，允许使用
`scripts/authorized_upload.py cover-repair-plan / cover-repair-run /
cover-repair-status / cover-repair-verify-live`。它不 append、不换 P，也不创建新 BV；因此不能拿来
修视频、字幕、标题、tags 或合集标题。`scripts/bili_cover_edit.py` 已 fail-closed，旧的
`bili-cover-edit-receipt.v1`、Creator 单面“URL 变了”或一次 edit 返回码都不是发布证据。

该窄路线仍必须使用当前 `authorized-upload-manifest.v3`，同时重放 same-BV
`final_human_review` 与**精确绑定新封面和当前完整标题的 CPA**
`lidousha-title-cover-joint-qc.v1`；后者在普通 same-BV 视频置换中可选，但在 cover-only
路线中强制必需。plan 冻结 manifest/hash、维护者 授权、publication authority、全部 package
attestation、新封面路径/hash/bytes、旧 Bilibili cover asset identity、唯一旧 CID、完整非封面
metadata，以及 Creator/public/public-tags/exact-section 四面快照。计划时任一面不可用、不一致、
非单 P、CID/AID/BVID/section 不等于 publication authority，或 manifest 目标 metadata 除封面
外与线上不同，都在远端写入前拒绝。

若该 BVID 此前已走完整 same-BV 视频置换、当前 CID 已不等于原始 publication authority，
必须给 `cover-repair-plan` 显式传
`--predecessor-completed <same-bv-repair-completed.v1>`。planner 重放该 completed 的旧 plan、
journal 与 CID 闭环；fresh snapshot 只允许 Creator/public cover asset 与 predecessor snapshot
不同，其余字段和 topology 必须精确相等。这样可承认已完成的视频置换与待修封面，但不能用
弱 cover receipt 或裸 CID 覆盖绕过前序证明。

操作顺序固定：

1. 运行 `cover-repair-plan --dry-run` 做真实只读四面观察；确认后再运行同命令（不带
   `--dry-run`）create-only 写 `same-bv-cover-repair-plan.v1` 和 hash-chain journal 的
   `PLANNED` 行。
2. 先运行 `cover-repair-status`，再运行 `cover-repair-run --dry-run` 检查本地下一动作；真执行
   时使用与所有投稿/修复共用的 `upload.lock`。
3. runner 先 fsync `COVER_UPLOAD_INTENT`，再上传 manifest 冻结的同一封面字节。仅上传 cover
   asset 不会改变稿件；若此步崩溃，只有线上 archive 仍精确等于 plan `before` 时才可重传
   同一字节。
4. asset URL 被验证为 Bilibili `/bfs/archive/<hash>` 后，runner 再次读取 Creator 原始稿件，
   要求 BVID/AID、全部非封面 metadata 和完整 videos/CID 列表仍与计划一致。随后先 fsync
   `EDIT_INTENT`，由 member API 从这次 fresh view 原样 clone payload，只替换 `cover`。
5. `EDIT_INTENT` 后无论成功返回、timeout、连接中断或进程崩溃，恢复都只能 poll，绝不再次
   edit。Creator/public 封面只能处于冻结的 old asset 或本次 uploaded new asset；其他字段、
   CID、section 任何第三值进入终态 `BLOCKED_DRIFT`。
6. Creator/public 都收敛到 new asset、其他字段与唯一 CID 完全不变、exact section 仍精确
   后才记 journal `VERIFIED`。随后必须运行 `cover-repair-verify-live --out ...` 再次读取四面，
   与终态 snapshot 精确一致后 create-only 写
   `same-bv-cover-repair-completed.v1`。该 receipt 明示 `unchanged_cid`、old/new cover、manifest、
   plan、终态 journal row 与 fresh snapshot；它不靠 upload ledger，也不伪装成视频置换的
   `same-bv-repair-completed.v1`。成功后同一命令还须把该 receipt 作为
   `VERIFIED_SAME_BV_COVER` authority 接入 publication reconciliation/runtime overlay；本地投影
   失败返回 rc=6，只重跑同一 fresh-verify/reconciliation，不再 edit 线上稿件。

默认 cover-only journal 为
`/opt/bilive/autoslice/reports/same_bv_cover_repair_ledger.jsonl`。多稿仍在同一 upload lock 下
逐稿顺序执行。旧脚本、手工 API、跳过 CPA 联合质检或只看 Creator 单面均禁止。

执行前必须确认所用工具 source 与本次授权的实际远端部署/私有冻结快照一致（host 见
[10](10-source-recording.md)，不得默认选旧 `free`），目标修复包通过本页发布准入，并先完成真实
dry plan；本地存在代码/测试不等于 production 已可用，也不等于五条线上稿件已经修复。

同 BV 的顺序固定为：

1. 先证明 `exact-talk-contract-closure.v1.status=COMPLETE`，且最终 state、重建 manifest 与
   selection contract 的 candidate 集合完全相等；五项整包未闭合时，不得先为已完成子集建立
   repair plan；
1a. 若候选已经 `published` 且须按 reviewed baseline 重建 bytes，只能先走 40/80 的显式
    package-only full-dry/apply；在第 4 步冻结授权 manifest **之前**必须再次运行同一
    `--published-recovery-bvid ... --plan`，确认 package 内完整 state tuple/predecessor authority 仍
    对应当前 state；`make-manifest` 必须消费 VERIFIED outer receipt 而非 pending/裸 package。
    不得用历史 `candidate_rejected` 前像、手工 state 或 package copy 代替；
2. 冻结最终包，重建兼容名称的 pending-human review manifest，运行 current canonical package audit；
3. 正常使用 builder `--mechanical` 生成独立机械 receipt；只有实际选择人工复核时，才准备
   v2 evidence template 并由真实观看者填写 observations。不得把人工全片观看作为每片默认
   前置，也不得给未观看的 v2 receipt 补签；
3b. 逐案放行：批仍 `recovery_incomplete` 时，可用
   `build_recovery_review_manifest.py --release-candidate <cid>`（可重复）
   只冻结已交付合规的单案；manifest 以 `partial_release_scope` 披露范围、批状态与
   裁定出处，范围必须是 exact 合同子集且每案自身 delivered/CURRENT/COMPLIANT。
4. `make-manifest --final-human-review ...` 同时冻结 package/audit/receipt、publication
   authority 和 维护者 的修复授权原话；缺 receipt 的 recovery manifest 直接拒绝；
   `make-manifest` 是新投稿与同 BV 修复共用的无副作用冻结入口，已登记 BVID 的 publication
   authority 在这里必须保留并允许生成 manifest；“已发布不得新建 BV”的 registry gate 只在
   普通 `upload` 副作用入口硬拒。否则会在 `repair-plan` 读取 authority 之前把唯一合法修复
   lane 自锁死；
5. `verify --manifest ...` 重跑 current audit、hash 与 receipt validator；
6. 先 `repair-plan --dry-run`；它必须先用显式 biliup cookie 对目标 BVID 运行只读
   `biliup show` 登录 canary，再读真实 Creator/public/section 单 P 事实。确认后才运行
  `repair-plan` create-only 落 plan/journal；Creator/public 已一致、但 exact section
  episode title 仍是唯一历史旧值时，planner 必须把该值冻结进 `before`，
   不得因待修复的 section 标题自锁；之后仍只能在 Creator/public 已收敛
   到新 CID 和目标 metadata 后，通过已有 one-shot `SECTION_TITLE_SYNC` 状态修复。

   若本次授权范围明确为“只修点名内容”、而当前已发稿的 tags 必须原样保留，可显式传
   `--preserve-existing-tags`。这不是任意 metadata override：manifest、current audit 和最终人审
   仍完整重验；planner 必须 fresh read Creator/public，并且两面 tags 都非空、无重复、规范化后
   完全相等。只有 `tags` 可从该 live 集合冻结为 target，plan 必须保存 typed hash-bound
   preservation receipt 和 manifest 原 tags；title/desc/tid/copyright/source/cover 仍只取 manifest。
   未传 flag 的默认行为不变。runner/status/resume/verify-live 都重放 receipt；tags 或任何其他
   metadata/identity 漂移一律 fail-closed。
7. 先 `repair-status`，再 `repair-run --dry-run`；最后只用 `repair-run` 执行或幂等 resume；
8. 每次 resume 前后均可用 `repair-status` 重验**本地** plan/journal/receipt 闭包；它不访问
   线上，也不能证明当前公开态。`repair-run` 进入 `VERIFIED` 后还必须运行
   `repair-verify-live --out <same-bv-repair-completed.json>`，重新读取
   Creator/public/public tags/exact section；只有 fresh snapshot 与 journal 的 VERIFIED
   snapshot 精确相等并 create-only 生成 completed sidecar，才算公开验收闭环。

同 BV `repair-plan` 只接受 authorized manifest 顶层 hash-bound
`recovery-same-bv-publication-authority.v1`。该 authority 必须由包内 record 与 review item
两面精确投影；命令行 `--bvid` 在 adapter 构造和任何网络 observe **之前**就须与 authority
BVID 相等。只读 live snapshot 随后还要证明 Creator/public/section 的 AID 与旧单 P CID 均
等于 authority；`same-bv-repair-plan.v2` 冻结该 authority，repair-run/status 每次恢复都与 manifest
重验。这样不能把 A 包靠错误 CLI 参数指向 B 稿件。

若同一 BVID 已完成过一次同 BV 修复，publication registry 中的原始 CID 仍保持不可变；
后续 `repair-plan` 不得靠裸 CID 覆盖绕过。必须显式传
`--predecessor-completed <same-bv-repair-completed.v1>`：planner 会重放 predecessor plan
及其 hash、冻结的 manifest/replacement/人工复核 envelope、hash-chain journal 的终态
`VERIFIED` 行和 completed fresh snapshot，并要求本次 fresh Creator/public/exact-section
单 P snapshot 与该 completed snapshot 精确相等，才把 predecessor 新 CID 冻结为本次
`before`。前序 `VERIFIED` 行证明的是**当时已通过完整准入并完成的历史 CID 迁移**；后续
review-package refresh 可以更新旧包内 `review_manifest` 等可再生产路径，不得因此反向撤销
已经完成的线上迁移。planner 因而只重验前序 plan 文件 hash、冻结 envelope、journal
bindings/终态与 live snapshot，不重新把旧包按今天规则发布一遍；本次 replacement 仍必须
逐项通过当前 manifest/audit/final-human-review 全量准入。completed/plan/journal 任一路径、
hash、candidate/BVID/AID、CID 拓扑或 fresh live 面漂移都 fail closed。共享 journal 只允许
前一 owner 已 `VERIFIED` 且新 `PLANNED` 行显式绑定其终态 row hash 的单链移交，禁止分叉或
并发抢占。

same-BV 的 member/season 登录读取统一走 `bilibili_member_api.load_cookie_pairs`：
`{cookie_info:{cookies:[…]}}` 与 `{data:{cookie_info:{cookies:[…]}}}` 两种真实形态都按
唯一 schema 严格解析；根节点/字段/条目异常、两形态同时出现、空值、重复 cookie 名或缺
`bili_jct` 都在网络请求前 fail closed，且错误不得回显 secret。`biliup append` 另由显式
`--biliup-cookie-json` 提供 CLI 所需的 top-level `cookie_info` 文件，不能把 app 嵌套形态暗中
改写后复用。当前默认分别是 API `--cookie-json /opt/bilive/app/cookie.json` 与 CLI
`--biliup-cookie-json /opt/bilive/app/tmp_manual_upload/biliup_cookies.json`。parser 只证明
文件结构；`repair-plan`、`repair-run` 与 `repair-verify-live` 在构造 adapter 前还必须对目标
BVID 运行只读 `biliup -u <cookie> show <BVID>` canary。canary 非零只返回经过清洗的拒绝原因，
不得写 `APPEND_INTENT`、plan、completed sidecar 或任何远端状态；本地双形态测试不能代替
production login。

1. `repair-plan --manifest … --bvid … --out … --journal …` 先重跑 manifest/audit，再只读
   Creator/public/exact section；只接受同 BVID/aid、公开 state=0、Creator 恰一 P、public 与
   section CID 同一且 section membership 恰一条。plan 与初始 journal 都 create-only，绑定
   manifest、视频/封面 hashes、目标 metadata、旧 CID、BVID/aid 与 season/section。
2. 默认 journal/ledger 是
   `/opt/bilive/autoslice/reports/same_bv_repair_ledger.jsonl`，与新投稿共用
   `/opt/bilive/autoslice/upload.lock`。journal 为 hash-chain
   `same-bv-repair-journal.v1`；截断、改写、非法跳转、同 BVID 被另一 plan 占用或 artifact
   漂移都拒绝执行。
3. 先用 `repair-status` 和 `repair-run --dry-run` 查看本地状态/下一动作；两者不做远端写。
   其中 status 完全不访问远端，dry-run 仍须通过只读 login canary。真执行只用 `repair-run`，
   它可以跨进程反复 resume，返回 `0=VERIFIED`、`6=仍在安全等待/推进`、
   `5=BLOCKED_DRIFT`。多稿修复必须在同一 upload lock 下逐稿顺序执行，不得并发 append/swap。
4. 状态为 `PLANNED → APPEND_INTENT → APPEND_AMBIGUOUS → TWO_P_READY →
   SWAP_RETRYABLE → CREATOR_SINGLE_NEW → PUBLIC_PENDING → VERIFIED`；若唯一未收敛面是
   exact-section episode title 仍精确等于修复前标题，则中间允许
   `SECTION_TITLE_SYNC_INTENT → SECTION_TITLE_SYNC_AMBIGUOUS → VERIFIED`。任一确定性身份/
   topology/metadata 漂移进入终态 `BLOCKED_DRIFT`。普通 `repair-run` 不得离开该终态。
   唯一例外是旧 normalizer 把同一 `/bfs/archive/<hash>` 封面经
   `archive.biliimg.com` 与 `*.hdslb.com`/`*.biliimg.com` 两个 CDN 域名投影误判为漂移：
   仅当终态 reason 精确为
   `post-swap observation is neither exact two-P nor exact single-new`、被阻断快照除该
   CDN 别名外已是唯一新 CID 目标态、且 fresh 只读 Creator/public/exact-section 复验仍为
   target 或传播中，才可运行 `repair-reconcile-blocked`。该命令不调用 append/edit；
   `--dry-run` 不写 journal，真执行只追加带原阻断 row hash 的审计行。其他 reason、不同
   封面 asset path 或任何 metadata/CID/section 差异仍保持 `BLOCKED_DRIFT`；禁止手改或截断
   journal。
5. `APPEND_INTENT` 先 fsync 再且只再调用一次 existing-BV append；之后即使进程崩溃、响应
   丢失或 append 效果迟到，也只能 poll live Creator，绝不二次 append。找不到唯一新增 CID
   就停在 ambiguous，三 P、重复/未知 CID 直接阻断。
6. swap 只允许保留 journal 已冻结的新 CID，并以同一 metadata/cover asset identity 重试；
   21540 或 timeout 后先读 live state，只有仍是原两 P topology 才重发完全相同 payload。
   Creator 已成为唯一新 CID 后只读收敛，不再 edit。
7. `PUBLIC_PENDING` 默认只轮询瞬时不可用/未传播的 public、tags 与 section。唯一机械
   收敛例外是：Creator 已为唯一新 CID 和完整目标 metadata、public 已为同一新 CID 和完整
   目标 metadata、exact section 内 BVID/AID/CID membership 恰一条，且 episode title
   **仅仅**仍等于 plan 冻结的修复前标题。此时先 fsync `SECTION_TITLE_SYNC_INTENT`，fresh
   重读 episode/season/section/order 与单 P CID 身份后，只调用一次原 episode title edit；
   无论成功、超时、响应丢失或进程崩溃，之后都只能 poll，绝不二次 edit。标题是第三种值、
   public 尚未到目标态、身份不全或任一其他字段不一致都不得机械同步。只有
   Creator/public/exact section 的 CID、title/desc/tid/copyright/source/tags/cover 与 section
   episode title 全部一致才进入幂等终态 `VERIFIED`。
8. `VERIFIED` 是 journal 记录的那次线上快照，不代表以后仍未漂移。随后必须运行
   `repair-verify-live`：它先重验 plan/manifest/journal，再做只读 login canary 和四面 fresh
   observe，要求规范化 snapshot 与终态 journal row 完全相等。成功后以
   `same-bv-repair-completed.v1` 原子 create-only 写证；目标已存在、线上漂移或任一面不可用
   都拒绝且不覆盖旧证据。

同 BV replacement 的完成证据必须持久化并同时证明：

1. 修复字节先通过当前 manifest/audit v2；
2. append 后 Creator 恰为旧 P + 新 P；
3. swap 后 BVID/aid 不变，Creator 恰一 P 且 CID 为新 CID；
4. public `state=0` 且 public CID 为新 CID；
5. public 与 Creator 的 title、desc、tid、copyright、source、tags 精确一致；
6. 目标 section 中 membership count 恰为 1、episode title 与最终发布标题精确一致，
   public season title/display 正常；
7. Creator/public 不存在本次修复产生的第二个重复 BVID；
8. replacement/public/season evidence、终态 journal row 与 fresh
   `same-bv-repair-completed.v1` sidecar 已入库并 commit。

`scripts/swap_video_p.py` 只完成 P 置换，不单独证明上述八项，永远不得把它当作发布闭环入口。

## 公开验收

`repair-run` 只有在以下四面同时匹配时才记终态 journal `VERIFIED`；随后
`repair-verify-live` 必须再次观察同样四面，fresh snapshot 精确一致后才 create-only 生成
completed sidecar：

- B 站 public view（可见、最终 CID/标题/简介/分区/版权/source/合集）；
- public tags；
- Creator archive view（唯一稿件、唯一 P、完整元数据）；
- 精确 season section API（唯一 membership、episode title 精确）。

公开/创作中心任一面尚在转码、重审或传播中，状态就是 `posted_unverified`；后续只能复验/
补合集，不得重复上传。

上述四面证明稿件身份与元数据，不单独证明观众播放到的成片正确。发布/换片后的主会话
验收还应绑定该**最终 CID** 的真实可播放媒体，确认片头、字幕呈现、关键修复窗和结尾，
并检查音轨没有漏段/错位。平台转码后 SHA 通常不同，不能要求 CDN 字节等于上传字节，
也不能因只有 metadata 成功就跳过播放面。无法取得播放面证据时须单列未验收项；
不能借此重传已成功提交的稿件。复用既有音频听证须说明其与当前音轨的对应关系，
不得把 ASR 文本、视频抽帧或一次相关性计算写成新的实际听证。

## 公开真值回写

公开验收闭环必须在同一次命令中幂等对账 publication registry 与逐日 runner state；只拿到
BVID、只写 upload ledger 或只生成 completed sidecar 都不能把命令报成完成：

- 新 BV 只接受当前 manifest、`VERIFIED_PUBLIC` public/Creator/exact-section 证据、
  `IN_SEASON_PUBLIC`（或 manifest 明示 opt-out）证据和 `authorized-upload-result.v3` 的完整
  hash 闭包。ledger 行不是出版真值，不能单独触发回写。
- same-BV 只接受 create-only `same-bv-repair-completed.v1` 的
  `VERIFIED_FRESH_LIVE` authority；journal `VERIFIED` 或历史 registry 行不能替代 fresh
  completed sidecar。
- 强证据先写 create-only reconciliation authority 和部署外 runtime registry overlay，再投影
  committed registry 与所有匹配的逐日 state。candidate 原身份字段不得被线上 CID 覆盖；
  state 行改为 `published`，保留 `prepublication_status`，线上 CID 单列 `published_cid`。
- BVID 冲突、authority/hash 漂移、同 candidate/date 重复行、找不到唯一 state candidate 或
  任一写入失败均返回 rc=6，保留已经完成的线上事实并提示只重跑 `season-add` 或原
  `repair-verify-live`；绝不重传视频。若 same-BV completed sidecar 已由上次 create-only
  成功写出，重跑只允许其内容与 fresh closure 除时间戳外完全相同，并仅续做本地对账，
  不覆盖 sidecar、不调用远端变更。
- runner 每次读 state 都重放 hash-valid runtime overlay，使“公开成功后进程在 state 写入前
  崩溃”能够自动收敛；overlay 损坏或 authority 漂移时 fail closed 为
  `publication_reconciliation_blocked`，不能把旧 `candidate_rejected`/`review_ready` 当成
  当前发布结论。
- 若所需 canonical `state/<date>.json` 与其 `.bak` 都不存在，强证据仍可完成
  runtime/static registry 对账，但不得凭公开事实伪造候选集或整日状态。命令会在每个适用
  runtime root 下 create-or-append 独立的 `state/publication-recovery/<date>.json`，其 schema
  为 `publication-recovery-projection.v1`、`projection_kind=PUBLISHED_TARGET_ONLY`，并固定声明
  `original_state_status=MISSING`、`original_candidate_set=UNKNOWN`、`day_completion=UNKNOWN`。
  `entries` 按 candidate 追加，保留字符串 `candidate_id`/`cid` 身份，另列数字
  `published_cid`，并绑定完整 hash-validated publication mapping；同日第二个已发布候选只能合并
  到既有 sidecar，不能覆盖不同 authority、BVID 或 publication tuple。该 sidecar 不是公开 authority，
  不含 `picks`、`songs`、pending 集合、`publication_closure` 或 `prepublication_status`，也不参与
  day closure/terminal 判定。runner 发现缺失 state 但存在该 sidecar 或该日期 runtime registry 时，
  只返回内存 `publication_reconciliation_blocked`（明确 original state/candidate set unknown），
  直到真实 state 恢复；已有 state 但目标缺失或重复仍拒绝 overlay。

## 安全边界

- cookie、token、BVID、season/section ID 与当前登录态都从 live 环境读取或由工具验证，
  文档中的历史数字不构成 authority。
- `do_upload.sh`、裸 `biliup`、legacy app uploader 和历史 memory playbook 都不能绕过
  authorized manifest。
- emergency `--skip-season` 不构成完成状态；必须补跑公开闭环。

### 原稿修复与缺失日状态的后继对账

`publication-recovery-projection.v1`的旧CID只能由完整verified same-BV计划的before→new
证据迁移；除既有recovery authority外，原稿lane的`original-fastlane-authorized-same-bv.v1`
也须被消费。后者必须与manifest精确相同并由原生`validate_publication`重读仓库封存target
验证，不能仅加入一个字符串白名单或手改published_cid。迁移后仍保留candidate字符串身份与
`day_completion=UNKNOWN`，不凭单片公开完成伪造整日state。

## 已公开手动成片的队列对账（2026-09-21）

手动包通过原生 package/JQC/authorized-upload 后可能已公开，而同一候选仍在
`pending_talk`。这不是再次上传的理由，也不能删除队列行或伪造 `review_ready`。
`scripts/intake_published_manual_candidate.py` 默认为 dry-run；只有当前原生 manifest、
manual review、record、源媒体/区间、完整新 BV public/Creator/精确 section/uploaded
证据及 immutable reconciliation authority 都重验成功，且同日恰一匹配 Talk 队列行、
无重复/冲突且实际源区间覆盖原候选，才允许显式 `--apply`。

该操作只在维护 `DISABLED` 存在时持 runner 与 publication-reconciliation 锁，封存
原行/完整状态 preimage，再按 state SHA/CAS 将该行转入 `published` picks，并完整保留
原 CID、源码区间、原队列证据与既有暂停；不生成 `review_ready`、不改字幕/成片、
不访问源挂载、不调 provider，也不授权上传。坏引用、另一 BV/日期/源、未公开、
诊断产物、重复目标、Song 或非 pending_talk 均拒绝。中断先回读目标和原 preimage，
同一 manifest/publication 的重复接纳幂等。

接纳后只对**原 BVID**运行原生 `season-add`/reconciliation 闭合 runtime registry、
state 与 ledger，再完成实际公开播放验收；不能拿本工具替代发布前审核或绕过正常
external-package import 的准入要求。本条只修复已发生且完整验真的公开事实对账。

已接纳后的幂等检查使用 immutable publication authority、当前 manifest/package、
已写入的精确原队列行与接纳引用，不要求可变的 public/season 轮询文件恢复旧字节。
该识别分支只返回 ALREADY_APPLIED，零状态写、零新发布；首次接纳仍必须完整验证
原 public/Creator/section/uploaded 证据，缺证或漂移不能通过此分支首次接纳。
