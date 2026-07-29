# 90 授权上传、同 BV 修复与公开验收

本文件是发布步骤的**分步权威**。唯一程序入口是
`scripts/authorized_upload.py`；`.agent/skills/bilive-autoslice-publish/SKILL.md`
只提供操作顺序，不得另立规则。

## 发布准入

- 每条都需要 Ivan 明确授权；manifest 保存授权原话，工具不能替用户创造授权。
- publication registry 的人工搁置不得靠删除历史来解除：
  Ivan 明确放行后改为 `released_for_upload`，保留放行日期/原话与剩余准入门；
  该状态只解除 `hold_pending_review` 阻断，不等于机器 audit 通过，也不产生上传副作用。
- 新投稿只接受 `authorized-upload-manifest.v3`。它必须绑定同 stem 的最终视频、封面、
  record、SRT、review manifest、冻结标题、最终 tags、StoryContract 与当前 package audit。
- package audit 必须为 `lidousha-review-package-audit.v2`，policy epoch 精确等于
  `2026-07-23.final-artifact-gates.v3`。audit schema 仍是 v2；policy fingerprint、
  auditor source hash 与完整
  portable `audited_inputs` 闭包有效。上传器在任何副作用前重跑当前 canonical auditor、
  严格 SRT 与共享标题门，并要求结果与 manifest 绑定一致；自报 `passed:true` 不算。
- talk 标题统一 `【李豆沙】` envelope，song 精确目录式；人工正文不能绕过外壳、长度或
  结构门。tags 必须逐项等于 audited record 的冻结结果。
- 新 BV 没有当前 audit + `AUTO_UPLOAD` manifest + artifact hash gate 就不得发布。
- exact same-BV repair 不消费 `AUTO_UPLOAD`；它只接受 exact closure COMPLETE、当前 audit、
  authorized manifest、最终感知 receipt、recovery publication authority 与 artifact hash
  全部绑定的既有稿修复 lane，并继续保持 `upload_allowed=false`。
- 授权上传/修复的证据必须 commit；媒体本身不因此入库。

## 最终感知复核 receipt 的权限边界

- recovery review manifest 的
  `finished_review_package_no_upload_pending_human_review` / `upload_allowed=false`
  是机器打包状态，不是发布许可。机器 audit 与最终感知复核是两道独立门，不能用其中一面
  替代另一面。
- `lidousha-final-human-review.v2` 只接受
  `scope=same_bv_repair`、`status=ACCEPTED_FOR_SAME_BV`。`reviewer_kind` 可为
  `human_owner`、`human_delegate` 或 `delegated_root_agent`，但必须与真实观看者一致：
  owner 固定 `reviewed_by=Ivan`，本项目 root agent 固定
  `reviewed_by="Codex root"`，human delegate 写实际姓名；带时区 `reviewed_at` 记录实际完成时间，
  `approval_quote` 保存授权/委托原话。委托 agent 自审不等于 Ivan 亲自观看，严禁把
  `reviewed_by` 伪写成 Ivan。
- receipt 以 committed `assets/lidousha/final_media_review_contracts.v1.json` 的 SHA-256
  为 review contract authority。每个 candidate 必须逐点复核 exact final-video window 与
  expectation 并提供 PASS/evidence；八个总检查也各自需要非空具体 evidence，裸 PASS 或任意
  泛化检查表无效。672 必须把 0:13“前半无声、后半有声、全段无我草”和 1:48“整条 L 问句
  无声并删除”作为两个独立 exact point 验收；850 必须分别验收 0:13 怪叫空白、0:59
  “他一副，一副”和 1:23“kmx欺负人”；1493 必须验收“下斗里”已按 Ivan 裁定修为
  粉丝队玩梗专名“沙豆李”。
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
  `AUTO_UPLOAD`，不授权新建 BV，也不替代 authorized manifest 中 Ivan 针对修复动作的授权原话。
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
- receipt 只能由 `scripts/build_lidousha_final_human_review.py` 从完成后的
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

1. 在审片字节冻结后运行 `make-manifest`，只引用 package 内最终文件与 Ivan 授权原话。
2. 先运行 `verify`；它必须重算全部 hashes、current audit 与政策绑定。
3. `upload` 只从 manifest 取路径/标题/元数据，禁止再手输一套参数。ledger 对同 artifact
   重传硬拒；拿到 BVID 后不得为“再取一次结果”重跑上传。
4. 合集 lane 由冻结标题确定：talk → `小李切片`，song → `小李歌唱`。发布未精确入集不算
   完成；已投稿但合集/公开验证未闭环时只运行幂等 `season-add`，绝不重传。

滚动 24 小时配额由 ledger 与 Creator 近期稿件共同估算，取较大值；达到 10 条或 B 站返回
21566 时停止新投稿。配额不授权降低内容/审计门。

## 已发稿修复：只改同一个 BV

字幕、边界、片头、视频字节、标题、封面或 tags 的修复都编辑原稿，不新建 BV、不删稿。
唯一入口是
`scripts/authorized_upload.py repair-plan / repair-run / repair-status / repair-verify-live`，
核心状态机在 `src/autoslice/same_bv_repair.py`。legacy `swap_video_p.py`、
`bili_archive_tool.py replace`、裸 API 和手工 append/edit 仍禁止。

执行前必须确认当前 source 已部署到 `free`，目标修复包通过本页发布准入，并先完成真实
dry plan；本地存在代码/测试不等于 production 已可用，也不等于五条线上稿件已经修复。

同 BV 的顺序固定为：

1. 先证明 `exact-talk-contract-closure.v1.status=COMPLETE`，且最终 state、重建 manifest 与
   selection contract 的 candidate 集合完全相等；五项整包未闭合时，不得先为已完成子集建立
   repair plan；
2. 冻结最终包，重建 pending-human review manifest，运行 current canonical package audit；
3. 先用 final-human-review builder 的 `--prepare-evidence-template` 冻结 v2 bindings；被如实
   命名的 reviewer 按 committed exact review contract 完整复核最终烧录字节、填写实际
   observations，再由 builder create-only 签出 `lidousha-final-human-review.v2`；只有真的
   完成观看后才可出 receipt；
3b. 逐案放行（Ivan 2026-07-26：「没有任何纪律要求必须5个全complete才能动BV，
   修复时哪个好了就可以改哪个」）：批仍 `recovery_incomplete` 时，可用
   `build_lidousha_recovery_review_manifest.py --release-candidate <cid>`（可重复）
   只冻结已交付合规的单案；manifest 以 `partial_release_scope` 披露范围、批状态与
   裁定出处，范围必须是 exact 合同子集且每案自身 delivered/CURRENT/COMPLIANT。
4. `make-manifest --final-human-review ...` 同时冻结 package/audit/receipt、publication
   authority 和 Ivan 的修复授权原话；缺 receipt 的 recovery manifest 直接拒绝；
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

## 安全边界

- cookie、token、BVID、season/section ID 与当前登录态都从 live 环境读取或由工具验证，
  文档中的历史数字不构成 authority。
- `do_upload.sh`、裸 `biliup`、legacy app uploader 和历史 memory playbook 都不能绕过
  authorized manifest。
- emergency `--skip-season` 不构成完成状态；必须补跑公开闭环。
