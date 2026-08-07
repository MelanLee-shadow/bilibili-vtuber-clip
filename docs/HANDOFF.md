# Current handoff

Updated: 2026-08-07 by Claude（8/7 鹅鸭杀场三症状通病修复会话）。7/31 以下旧节仅存历史。

## 2026-08-07 live 状态

- **生产基线已在分支**：`codex/virtuareal-community-crawler`（beda5bf，8/7 18:05Z 部署，
  社区称呼 crawler 子系统）**未合回 main**。本会话工作分支
  `claude/session-live-context` 基于 beda5bf 之上（马有利/狍哥 roster+glossary、
  会话游戏语境通道、评分卡 rescore 设计稿），部署以该分支 tip 为准。
  **main 的下一次上游化必须合并这两串提交，不得从 main 直接部署（会回滚 codex 17 提交）。**
- **说话人二分已验证并已翻转**：CAM++ 二分 finalizer 对 8/7 `auto_203735_555_680` 冒烟
  READY（61 cue=25 主播+36 连线，LDS/GUEST 双样式）。8/7 部署（`78f64aa`，deploy 门内
  3036 全绿）后 runner cron 行已加 `env AUTOSLICE_SPEAKER_MODE=auto`（备份
  `/opt/bilive/autoslice/crontab.backup-20260807-speakermode`；翻转撤销 7/13 uniform 政策，
  不确定场 speaker_review_required 持留，符合 40-doc 口径；回滚=恢复备份 crontab）。
  roster 快照已手动刷新（马有利/狍哥/香香烧烤 已达生产 prompt）；8/7 游戏语境
  state=RESOLVED 鹅鸭杀（17 特征词面×90 次）。
- roster crawler 是每周日 06:12 cron；member_overrides 别名变更后需手动跑一次
  `scripts/crawl_psplive_roster.py --write /opt/bilive/autoslice/state/psplive_roster.json` 才达生产 prompt。
- 评分卡 rescore 状态机**设计稿**（未实施）：
  `docs/reviews/2026-08-07-source-fact-rescore-design.md`；`auto_220747_1271_1323`
  在实施前仍处 candidate_rejected 终态（revive 脚本救不了，见设计稿 §1）。
- 8/7 场联动台账行未写（参与者尾幼/星汐/萱萱卡娅/北柚香/汀汀汀有 roster+弹幕证据，
  紫妍/尤娜/天云海等非 PSP 成员写法未裁定）——等 Ivan 裁定后按 7/22 南町行格式补
  `session_relation_ledger.v1.json`。

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

- **（已解，留防复发警示）2026-08-02 runner 三日期粘滞封锁事故**：verify-live
  把 1013 修复凭证的 runtime 登记路径写在了**可回收沙箱**里
  （recovery/2026-07-29/auto_225056_1013_1116-jyl-r2/repo/.../verification/
  same-bv-repair-completed.json），后续金丝雀 rm -rf 沙箱→登记悬空→全册
  校验失败→07-25/26/29 全 blocked。已按 sha 原字节恢复（1e684dec…），
  runner 自愈解封。**该沙箱路径在 runtime 登记被重写前不得删除**；耐久
  副本在 /opt/bilive/autoslice/reports/authorized_uploads/
  2026-07-31-1013-jiuyuanling/。代码级修复（repair-verify-live 把凭证落
  耐久目录再入登记）在 backlog。

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

## 进行中（2026-07-31 深夜 → 08-01 已收官，Claude/Fable 线）

- **1013 同 BV 修复（案 1）：已完成并公开验收（2026-08-01T07:19Z）**。
  BV154GA6vEyD 置换新 CID 40496401196（原 40468153258），
  `repair-verify-live` = VERIFIED_FRESH_LIVE，出版登记已记
  publication_reconciliation。4 处修正：cue9/11 久远澪老师（Ivan 指认 +
  BCUT 独立转写）、cue25 柏拉图（Ivan 确认，101 人舰长榜唯一近音）、
  cue39 伪装成→栽赃给（exact-final 声学回执 + BCUT 双听）。r19 产物金样：
  全片 diff 恰 4 处、时间轴零变化、标题逐字线上、封面字节复用 8dca…。
  人审为实证型（全片解码/静音扫描/8 帧亲验/6 段烧录窗 BCUT 复听），
  receipt 由 Claude root 以 delegated_root_agent 签出（ce5ae6f 扩展枚举）。
  全部证据入库 `reports/authorized_uploads/2026-07-31-1013-jiuyuanling-source/`
  （manifest/plan/completed/receipt/evidence/审计/评审 manifest）。
- **随案发现两条（均为既有特征，不挡置换，已列 backlog）**：
  (a) sidecar `.srt` 相对烧录字幕存在恒定 +6.2s（=intro_offset）位移，
  发布版与置换版同位——疑为烧录 ASS 与 sidecar 写盘各自加了一次片头偏移；
  修复属流水线项，勿在置换 lane 单独动。
  (b) 发布包 sidecar 文本与线上烧录像素在 cue9 本就不一致（sidecar
  「就问你老实说」vs 线上像素「9月林老师」）——delivery-divergence 家族
  （xinyi 案同族）新样本；本轮修复以音频仲裁为准不受影响，但基线=reviewed
  sidecar 的前提要意识到像素可能另有其文。
- **CPA 风暴已解（2026-08-01）**：根因是 oracle 上 CLIProxyAPI 进程病态
  （2d20h 长跑后），`systemctl --user restart cliproxyapi` 治愈，4/4 健康。
  400「当前分组不支持」是上游透传不是配置错。遗留给 Ivan：oracle 上
  `cliproxyapi-codex-warmup.service` 处于 failed；建议加周期重启 timer。
  （历史：r10-r13 四轮死于该风暴不同落点；停机期间全量测试必红→部署门
  连带锁死是政策耦合，非 bug。）
- **reuse-cover × recovery-manifest 证据结转 lane 本轮建成**（字幕-only 修复
  的永久基础设施）：`1878ee1` sidecar 结转 → `ecbb7cd` 身份重打 → `cc856cf`
  先结转后终验（拆鸡生蛋）→ `c192b97` StoryContract 投影重绑本轮 →
  `90f927a` **出版世代 v2 身份见证冻结结转条款**（r18 根因：见证 schema 已
  升 v3，对冻结字节重考 = live 政策重算，且视觉裁判同字节 r17 PASS/r18
  FAIL 彩票；现按 7/27 裁定直接结转 v2 PASS 见证，carry 丢弃三点补
  `carry_drop_reason` 披露）。离线已证明 r19 全链过门。
- **案 2（怕猫 181_480 分句）已结案：判不修（2026-08-01 音频仲裁）**。
  三层转写（BCUT fresh/asr_draft/agy_refined）一致：「猫」与「我也害怕」
  之间有 120ms 真实停顿（padded 24.61→24.73s），"我也害怕"语音落在 cue6
  窗口内。纯文本重切必造成 ~1s 音字错位（比"标点归属欠佳"更扎眼）；
  台账只有 replace_cue/replace_substring 两种文本动作，无重定时；全新重产
  被出版登记 fail-closed 挡死。现状=时间轴精确、断句语义欠佳，任何可行
  改动都是净退化，按 Ivan「能修就修，不能修就别动」判不动。

## 下一步

上一版的第 1–3 项（07-26 tick、`song_192000_1321` draft authority、
`auto_152944_1411_1463` cover route 与 `auto_225056_814_887` 的 subtitle/chat authority）
均已闭环，三天全部 published，不要重复处理。当前队列：

1. 部署 `f616bbf`，让部署测试门生效。这是后续所有改动的护航前提。
2. **封面文案链流水线修复**（不做单切片手写文案，Ivan 07-31 明确否决该方向）：封堵空 punch
   fail-open、renderer 静默换行改硬错误、引号左截断拒绝、上传闸与 cover-only lane 强制
   `rendered lines == CPA final_punch` 逐行相等、退役整数行数门。
3. ~~封面路由修复~~ **已落地 `9f51987`（2026-07-31）**：witness bbox 降为置信
   输入、`subject_confident = geometry ∨ source_composition`、几何否决移到
   relationship 分支之后；24 条历史样本离线重放通过。**剩余验收：接下来
   3–5 条真实生产切片作为 live 样本，人工核对路由选择与封面质量**（分布
   重放不能冒充质量验证）。
4. `auto_192000_909_1014` / `BV1s7326qEc9` 的封面重做：作为第 2、3 项修好后
   **cover-only lane 的首次真实执行验收**，由修好的流水线自动产出文案，不许抢跑。
5. `scripts/audit_lidousha_review_package.py:_audit_policy_fingerprint()` 解耦：它把 26 个
   源码模块的原始字节哈希进 `policy_fingerprint`，任何一行门代码修复都作废全部已冻结
   audit 并触发全量重审。改为显式 policy 版本号 + 脚本化迁移。
6. 后续封面继续执行最终实图检查：多人联动必须确认李豆沙主体；特殊梗必须绑定正确参考形象；
   CPA vision 为首选，公开 CDN 回下载需与目标图 hash 一致。
7. **切片生产提速（Ivan 2026-08-01 点名；已按 CPA 日志实证重排）**。
   oracle CPA 日志（~/.cli-proxy-api/logs/main.log gin 行）证明裁决期大头
   是 1-2 分钟**单发深推理调用**（修正/审片），并发救不了单发——原计划
   a（逐 cue 并发）降级为小赢项。已落地两项：
   - `90f927a` 复用封面不再重考身份见证（消灭整轮报废彩票）；
   - **`6261842`+`aebc1fb` pinned-replay 修复快路径**：v2 exact_interval
     _replay + verified_public_exact 双所有权成立时跳过审片员阶段（其产出
     注定被重放覆盖），零变更授权回执入 exact-final 终审；终审/边界评审
     原样保留。**r21 金丝雀：377s vs 老路径 694s（-46%），交付 SRT 字节
     等价（f96b6161…），discovery COMPLETE/release PASS。**
   教训（r20 烧一轮）：跳过阶段前必须穷举其输出对象的**传递性**消费者——
   final_review_audit 还作为 correction_audit 喂进终审做变更授权推导。
   剩余排序：新关键路径=转录/AGY 精听（~3-4min）→ 逐 cue 短调用串并发
   （小赢）→ 烧录∥封面。每项测试+金丝雀单独上，门链顺序不动。
   **luna 换 sol 试验结论（2026-08-02，Ivan 要求穷尽级验证后叫停）**：
   gpt-5.6-luna 已被上游启用；小样 A/B 闭集/念弹幕口味判决一致但边界
   评审分歧（sol 命中线上验收锚点），首轮金丝雀还出过一笔 luna 2m5s 500。
   曾短暂换链后按 Ivan 裁定回退（`ec414b4`），生产全线保持 sol。根本账：
   CPA 法官 lane 三周只有 5 次调用（配额收益≈0），量大的 lane 全是
   luna 已显分歧的深语义类。字节级重放工具已入库
   （`scripts/ab_model_replay_closed_set.py`，标准=重渲染 prompt sha 等于
   历史 judge_prompt_sha256），但现存语料 5/5 inputs_missing——若未来
   配额压力要重启此题，先给法官行持久化完整 request/witness 输入，攒够
   N≥30 真实案例再跑该工具。

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
