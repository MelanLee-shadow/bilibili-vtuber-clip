# Current handoff

Updated: 2026-07-31T14:45:00-04:00 by Claude（从 Codex root 接手；Ivan 于 07-31 14:0x
停止该 Codex 任务并指示 Claude 接手）。

本文件只记录会影响下一次操作的 live 状态。流水线规则只读
[`docs/pipeline/`](pipeline/README.md)。`review_ready`、本地 commit、旧 PID 或旧 handoff
都不是发布完成证明；已发布稿必须读取 Creator/public/section fresh-live 回执或 cover-only
receipt。

## 目标

保持 `free:/opt/bilive/autoslice` 正式 cron 不停，继续收敛 2026-07-25、07-26、07-29
及后续日期。用户已授权：修复稿可直接同 BV 编辑上传；当天新稿可权宜上传；指定旧稿重做后
可直接上传，无需再次等待审阅。

## Runtime authority

- 远端部署：`free:/opt/bilive/autoslice/repo/DEPLOYED_COMMIT` =
  `a4e68048c548828707031b2b78671771ed373c82`（2026-07-31T18:03:48Z）。
  `DISABLED` 不存在；正式 runner cron 为每 10 分钟一次。
- **本地 `main` 领先部署一个 commit：`f616bbf`（deploy 全量测试门 + 架构债务账本），
  尚未部署。** 部署它之后，`scripts/deploy_free_autoslice.sh` 会在 dirty-tree 拒绝之后、
  任何远端动作之前跑 `pytest -q` 全量，红了拒绝部署；**无 marker 排除、无 bypass 开关**。
  当前全量为 `2910 passed / 0 failed / 0 skipped`（`f616bbf`，干净树复验）。
- 架构债务账本冻结于 2026-07-31：20 个超预算函数 / 12 个超预算模块
  （`tests/test_runtime_architecture.py`）。成员只减不增、逐项行数只降不升；
  7/31 之后新增任何一行必须带 Ivan 的批准出处。部署时会打印当前数字。
  最重的一项 `final_review_auditor.py:2030 adjudicate_context_finding` 764 行，
  留作单独 bounded 拆解 task。
- 2026-07-31T17:20Z 起 07-25 / 07-26 / 07-29 三天均已 `published` / 
  `published_with_failures`，pending talk/song 全空；`live=False` 无新直播。
  因此 `baaf250` 之后**没有任何新的 route decision 数据**，其对封面路线分布的影响未实测。

## 已完成

- 07-22 与用户点名的 07-24 修复均已上线：`BV1xgg462Env`、`BV1AD366DEd9`、
  `BV1Eo3L6zECt`、`BV1ec3A6bEWF`。07-24 的“礼墨”、0:13 怪叫、0:59
  “他一副”、1:23 “kmx 欺负人”均已按对应修复范围处理；专名与语境通病已进入
  glossary/CPA 最终裁决链。
- `auto_142942_496_618` 已用最新版流水线整片重跑并在原
  `BV1zk386LEjC` 完成同 BV 修复：CID `40453148097 -> 40455570336`，未新建 BV。
  最终字幕将已确认的日语人称统一为假名（`ぼく/おれ/あたし/おら/わたくし`），不再混用
  `boku/ore/atashi`；39/39 reviewed baseline mappings、native-script gate、
  source-truth owner gate、redelivery baseline owner gate 和 exact-final v2 均 PASS。
  最终视频/SRT/封面 SHA-256 分别为
  `70818f97e595a4a29d50d0ab7aa40877e04e3286970c36d448e2150d964dbd37`、
  `34dbb888e1bff78d2832c57360418ec9c890575aaf0d78c93ac0d74882a8d4cc`、
  `2f34fbc1bc28e2503e10f48b488c04ad9afac670b2a4a1ec29277e69d862d9ff`。
  CPA 主图身份复核确认李豆沙为主体，标题字面为“一声‘偶’让小李 / 日语人称翻译大会”。
  Creator/public/section 已回读新 CID；完成侧车为 `VERIFIED_FRESH_LIVE`。
  固化证据在
  `reports/authorized_uploads/2026-07-30-japanese-pronoun-r7/`（commit `f38bbad`）。
- 上述重跑暴露并修复了 baseline owner verifier 的真实漏洞：最终稿经过已审阅的
  日语 native-script canon 后，verifier 仍拿旧罗马音做字面比较。`bff2707` 让 expected
  baseline 经过同一 canon，并在 audit 中记录转换来源；无关文字漂移仍 fail-closed。
  `producer_text_finalization`、package finalization 与 integrity 共 88 个相关测试 PASS，
  修复已部署。
- `auto_192000_909_1014` 的“白色奶龙”封面已按用户提供的
  `/Users/ivan/Downloads/60bae315bc53a44bede0582389460d20475948079.png` 进行 cover-only
  修复：删除无关龙/3D 龙模型，在原位置使用白发、熊猫耳、头顶墨镜的李豆沙“白色奶龙”
  形象。原 `BV1s7326qEc9` 和 CID `40438664771` 均未改变；新公开 CDN 资产为
  `9994c2c75f31d0b1706be4e7709efe226ebcbca0.png`，本地目标与公开回下载 SHA-256 均为
  `8bd5fa4f47468f514dc32bc265896405a46289a31ea930ac748234a18c989c06`。
  CPA 主人公身份与白色奶龙专项视觉 QA 均 PASS，receipt 为 `VERIFIED_EDITED`；
  固化证据在
  `reports/cover_only_repairs/2026-07-30-auto_192000_909_1014-white-milk-dragon/`
  （commit `f2ea7eb`）。
- 其他已完成的同 BV/封面修复仍保持公开：粉色小姐姐 `BV1RPNR6dET9`、同事家开播
  `BV1Eo3L6zECt`、沙豆李投票 `BV1zzgd6JEHe`。下一次操作不应重复上传或新建替代 BV。
- Gemini 路由审计确认代码顺序正确：AGY 仍是音频 witness 首选，三把 free Gemini key
  最近生成 canary 仍为 429；paid backup 的最新最小生成 canary 已恢复 HTTP 200，新的音频
  witness 任务会在 typed AGY/free-key failure 后使用 paid fallback。CPA 是最终文字/语义
  裁判，并是图像 witness 首选；CPA 不接音频不等于 CPA 不能看图。

## 当前正式队列

- 07-25：`review_ready`，pending talk/song 均为 0；6 个 talk 已就绪，1 个 song 进入状态。
  `song_192000_1321` 当前 deterministic packaging 拒绝原因为 active publish draft
  缺失且不属于 verified deferred-cover case，需要流水线补齐合法 draft authority，不能
  绕过门直接发布。
- 07-26：2026-07-31T00:10Z 为 `processing`，pending song = 1；7 个 talk 已就绪。
  多个 song tight-window 能识别歌曲，但扩大到 full-source 后仍缺 positive LRC boundary
  proof；runner 正在按 typed recoverable 规则重新入队，不等待用户。
- 07-29：`review_ready_with_failures`，pending talk/song 均为 0；5 个 talk pick。
  `auto_225056_814_887` 的 `subtitle_authority / chat_authority_finalization` 仍需流水线
  自行重建 authority。

## 阻塞

没有需要 Ivan 补充的外部 blocker。

- **cover-only 新 lane 零生产执行（2026-07-31，最高优先）**：`28b3576` 新建
  `src/autoslice/same_bv_cover_repair.py`（1063 行状态机）并同时把旧路
  `scripts/bili_cover_edit.py` 改成无条件拒绝。全仓库没有
  `same_bv_cover_repair_ledger.jsonl`、没有 plan、没有 receipt——**该 lane 从未真实执行过**。
  网络层复用已验证的 `BilibiliRepairAdapter`，未验证的是 plan/journal/transition 状态机。
  在它完成一次真实执行验收前，如遇封面事故：升级 Ivan 裁决，**不许临场解除
  `bili_cover_edit.py` 的 fail-close**。
  **`cover-repair-plan --dry-run` 不是零成本冒烟**（2026-07-31 实测执行序）：先拿
  `upload.lock`，再硬要求 manifest 带 title-cover joint-QC 回执（`required=True`），
  然后才 `adapter.observe` 四面观察、才判 dry-run。而 **07-31 之前的所有历史
  manifest 都没有 `title_cover_qc` 字段**（该门 `061f8ed` 07-31 06:54 才落地），
  拿老 manifest 跑必然 `return 2`——那是 fail-closed 的正常行为，不是 bug。
  所以今后任何已发布稿的封面修复都必须**重出 manifest**：先跑 CPA 联合质检拿
  receipt（receipt 绑定的是**新封面字节**的 sha，所以新封面得先产出来），再
  `make-manifest --title-cover-qc` 冻结。首个真实执行待 Ivan 授权白色奶龙重做。
  未验证面已收窄（07-31 核实）：`observe` / `normalise_snapshot` / `prepare_cover`
  与生产已多次真实执行的 video same-BV lane 共用同一个 `BilibiliRepairAdapter`
  （`same_bv_repair.py:333`）；真正没跑过的只有 `edit_cover_only`（`28b3576` 新加）
  的组装与 cover 专属 plan/journal/transition 状态机。
- **封面路由：标定分数路由已被整体退役（2026-07-31 调查确认）**。
  `publish_staging.py:1961-1970` 在 composition witness 存在且未建议重绘时**无条件返回截图**，
  `:1939` 建议重绘时直接重绘；witness 生成条件 `:1573` `enforce_final_host_identity` 在正常
  talk 恒真。因此 `:1971-2035` 的全套阈值（4.5 / 2.6 / 0.50 弥散帽 / camera window）
  **在有 witness 时不可达**。7/24-7/29 实测（witness 上线前）：24 条里 cpa_redraw 14
  （58%，标定基线为 29%），11 条走同一条 `motion without confident cover subject`；
  拆分为几何 flag 自身 False 5 条、弥散超帽 5 条、flag True 但超帽 0.027 被否 1 条
  （`auto_202004_553_831`，score 9.0027 / disp 0.5271）。`subject_confident` 探测器在
  23 条有效样本里 70% 给 False。修法未落地。
- ~~**封面文案链有一道被绕过的强制门**~~ **已修复（`f1c018e` + `c80bee1`）**：
  分行权威等级已立法并机器化（切点只属作者显式 `\n` / CPA punch 段 / full-text
  contract / 已验证 word_atoms；平衡器绝不发明切点）、`max_lines` 按缩略图合同封顶、
  renderer 背带、lane 内容触发门、contract 四路径穿透、CLI typed rc≠0，以及
  `70-cover.md` 内部那条「:44-45 禁回退 vs :60 要回退」的政策缝。整数行数门
  `physical_text_line_count ∈ {1,2}` 的实质已被「渲染行 == CPA final_punch +
  每行 ≤9em」取代（原始记录保留在下方）。**存量 6 条违例一条未修**，见
  `docs/reviews/cover-text-violations-triage-20260731.md`，等 Ivan 逐条点名。
  原始诊断留档：`auto_192000_909_1014` 的 7/30 cover-only 修复
  `cover_punch: []`，且证据目录内**没有任何 `lidousha-cover-punch-semantic-review.v1` 回执**
  ——选择器根本没被调用，然后 fail-open 回退整段 `cover_text` 并被 renderer 静默换行成
  3 行（`"白色奶龙"` / `表情小李` / `拒绝花钱`），把「观众想让新3D永久保留」整段丢失。
  `docs/pipeline/70-cover.md:44-45` 明令禁止"回执为空或回执失败即放行长 cover_text"。
  该稿仍公开（`BV1s7326qEc9`，CID 未变）。修复以流水线为单位，不做单切片手写文案。
- `physical_text_line_count ∈ {1,2}`（`scripts/authorized_upload.py:889`、
  `docs/pipeline/90-publish.md:103`、`70-cover.md:55`）由 `9563266`/`061f8ed` 于 2026-07-31
  引入，**无 Ivan 裁定**，且被证明是错度量（同一张图 Ivan 按阅读单元数 2、render spec 数 3；
  好断法与烂断法在该门下同样通过）。待退役为"渲染行与 CPA `final_punch` 逐行相等"。

- 歌切 active draft 缺失、positive LRC boundary proof 缺失、provider transient、
  subtitle/chat authority 失败都属于流水线/操作层 blocker；应保持 typed retry 或修复
  authority，不得停下来等待用户，也不得为了“变绿”绕过发布门。
- `auto_152944_1411_1463` 的 cover repair 当前在 image request 前 fail-closed，以保留
  screenshot route；下一次应修复 route authority，而不是用未验证 AI 像素覆盖截图路线。
- `auto_142942_496_618` 的 r6 因 recovery 目录漏建 `logs/` 在 ASR 前失败，且继承旧 lifetime
  retry cap；r6 仅保留为操作失败证据。不要原地洗绿或重用其 fingerprint；成功 authority 是
  fresh r7。

## 下一步

上一版的第 1–3 项（07-26 tick、`song_192000_1321` draft authority、
`auto_152944_1411_1463` cover route 与 `auto_225056_814_887` 的 subtitle/chat authority）
均已闭环，三天全部 published，不要重复处理。当前队列：

1. 部署 `f616bbf`，让部署测试门生效。这是后续所有改动的护航前提。
2. **封面文案链流水线修复**（不做单切片手写文案，Ivan 07-31 明确否决该方向）：封堵空 punch
   fail-open、renderer 静默换行改硬错误、引号左截断拒绝、上传闸与 cover-only lane 强制
   `rendered lines == CPA final_punch` 逐行相等、退役整数行数门。
3. **封面路由修复**：把 composition witness 的 bbox 从"路由法官"降回"置信输入"
   （删 `publish_staging.py:1961-1970` 的无条件 return），让标定分数恢复决定权；
   `subject_confident` 由前置条件改为降级信号；弥散帽收射程。验证走离线重放
   （`_decide_cover_treatment` 对已持久化的 route_decision 输入是纯函数，24 条样本可回放）
   加 3–5 条高分被否切片的像素级并排试点，**分布重放不能冒充质量验证**。
4. `auto_192000_909_1014` / `BV1s7326qEc9` 的封面重做：作为第 2、3 项修好后
   **cover-only lane 的首次真实执行验收**，由修好的流水线自动产出文案，不许抢跑。
5. `scripts/audit_lidousha_review_package.py:_audit_policy_fingerprint()` 解耦：它把 26 个
   源码模块的原始字节哈希进 `policy_fingerprint`，任何一行门代码修复都作废全部已冻结
   audit 并触发全量重审。改为显式 policy 版本号 + 脚本化迁移。
6. 后续封面继续执行最终实图检查：多人联动必须确认李豆沙主体；特殊梗必须绑定正确参考形象；
   CPA vision 为首选，公开 CDN 回下载需与目标图 hash 一致。

## 固化规则

- 用户只指出 1–2 个问题且未说明问题穷尽：默认整片重跑；指出 3 个及以上问题：修指定位置及
  背后通病，不因这条规则再次整片重跑。
- 大部分 glossary 专名允许按期望收益机械替换；专名之间平等，两个已注册专名冲突时由 CPA
  结合文字上下文裁决。
- 已发布稿只走 `scripts/authorized_upload.py repair-*`。**封面单独修复的入口已于
  2026-07-31 更换**：`scripts/bili_cover_edit.py` 被 `28b3576` 改成开头无条件
  `return 2`（旧实现仅作 endpoint archaeology 保留），现行入口是
  `scripts/authorized_upload.py cover-repair-plan / cover-repair-run /
  cover-repair-verify-live`。禁止新建替代 BV、裸 API、legacy replace 或删除旧证据制造绿灯。
