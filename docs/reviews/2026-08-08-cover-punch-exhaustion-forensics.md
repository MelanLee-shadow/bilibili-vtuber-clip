# 2026-08-08 auto_223750_913_1322 封面失败取证：为什么 9 次尝试全灭

候选：`auto_223750_913_1322`（2026-08-07，「如果是我杀的我就是女同」发誓梗）。
状态：`BLOCKED_AI_COVER_REQUIRED`，`cover_repair_lifetime_attempts=9`，
`cover_repair_exhausted=true`（`/opt/bilive/autoslice/state/2026-08-07.json`）。
标题：`【李豆沙】平时贪生怕死不做任务的小李被"最强女高中生"宠到猛做任务，被怀疑后拿
"我就是女同"发誓`。
完整故事钩子（StoryContract selection_hook）：`平时贪生怕死不做任务的小李被"最强女高
中生"宠到猛做任务，遭到怀疑后竟拿"如果是我杀的我就是女同"发誓。`——注意 cover_text
已经把 `如果是我杀的` 这个条件从句砍掉了，只留下裸的 `我就是女同`。

来源：`/opt/bilive/autoslice/logs/2026-08-07_auto_223750_913_1322_cover.log`（9 行，一
次尝试一行）+ `cover_repair/generations/*/`（9 个指纹目录，仅第 7 次留有完整 CPA 回执）
+ `/opt/bilive/autoslice/state/2026-08-07.json` 的 rec。

与 Ivan 简报口径的对账：简报写"8 lifetime / 预算 8/9"，磁盘现状是
`cover_repair_lifetime_attempts=9`、`cover_repair_exhausted=true`（9/9）。差异来自时间点
不同——第 9 次（`a198b7aa3bde-01`，08-08 17:10 UTC）发生在简报快照**之后**；简报描述的
"8/9 + 3/3 fingerprint" 精确对应第 8 次结束时的状态，不是计数分歧或候选认错。

## 一、逐次尝试表

| # | 指纹目录 | 时间 (UTC) | 网关结果 | 已知 punch 候选 | 备注 |
|---|---|---|---|---|---|
| 1 | `0f8ec3abfb14-01` | 08-07 19:17:02 | `COVER_PUNCH_REVIEW_REQUIRED` | 未持久化 | 无 image_generation_attempted，未留 cpa-request/response |
| 2 | `7ed35abe8058-01` | 08-07 20:45:28 | `COVER_PUNCH_REVIEW_REQUIRED` | 未持久化 | 同上 |
| 3 | `57691cc1ef04-01` | 08-07 22:11:11 | `COVER_PUNCH_REVIEW_REQUIRED` | 未持久化 | 同一指纹连续 3 次重试(1/3) |
| 4 | `57691cc1ef04-02` | 08-07 22:20:43 | `COVER_PUNCH_REVIEW_REQUIRED` | 未持久化 | (2/3) |
| 5 | `57691cc1ef04-03` | 08-07 22:31:53 | `COVER_PUNCH_REVIEW_REQUIRED` | 未持久化 | (3/3) 该指纹耗尽 |
| 6 | `7f1c4aa2bb73-01` | 08-08 00:57:11 | `COVER_PUNCH_REVIEW_REQUIRED` | 未持久化 | 新指纹 1/3 |
| 7 | `7f1c4aa2bb73-02` | 08-08 02:30:36 | **PASS 文字关，FAIL 图像身份关** | original: `["我就是女同","猛做任务"]` → final: `["最强女高中生","宠到猛做任务"]`（CPA REVISE，丢弃了"我就是女同"）| 唯一走到 `images.edit` 的一次；出图是"三个克隆熊猫女孩"贴纸构图；`final_host_identity_verification.status=FAIL`，`reason_code=FINAL_HOST_IDENTITY_MISMATCH`，但见证 JSON 内部自相矛盾（见下） |
| 8 | `7f1c4aa2bb73-03` | 08-08 02:40:31 | `COVER_PUNCH_REVIEW_REQUIRED` | 未持久化 | 同指纹 3/3，耗尽 |
| 9 | `a198b7aa3bde-01` | 08-08 17:10:35 | `COVER_PUNCH_REVIEW_REQUIRED` | original: `["我就是女同","最强女高中生"]` → final: `[]`（结构校验失败，reason_code=`CPA_PUNCH_SEMANTIC_REVIEW_REJECTED`）| 第 9 次，撞 lifetime cap=9，候选进入 `BLOCKED_AI_COVER_REQUIRED(...)( repair_budget_exhausted)` |

**取证缺口（本身就是一个发现）**：8 次 `COVER_PUNCH_REVIEW_REQUIRED` 里只有第 9 次的结构
化回执（`cover_punch_semantic_review` block）留在当前 `record.json`，其余 7 次（#1-6,8）
的 CPA 原始 request/response 从未落盘——`cover_generation.py` 只在流程走到 `images.edit`
分支（本例中只有 #7）才写 `final.cover.cpa-request/response.redacted.json`；纯文字被拒的
分支不建目录内容，只留一行日志摘要。9 个 `cover_repair/generations/*` 目录里 8 个是空的。
`cache/judge-verdicts/` 也不覆盖这个 CPA 文字裁决（那是 acoustic-witness 专用缓存）。逐轮
精确措辞因此**无法**从磁盘完全还原；能确认的是 #7、#9 两次的完整回执，以及全部 9 次的
终态分类（拒/过）。

## 二、为什么会被拒 —— (a)/(b)/(c) 判定

### (a) 这个 hook 在当前规则下确实难 punch —— 主因，有直接证据

`cover_text` 里唯一能表达"发誓梗"的原子是"我就是女同"（5 字，独立看没有它前面
被砍掉的"如果是我杀的"条件从句就读不出"荒诞誓言"这层因果——`docs/pipeline/70-cover.md:57-60`
明文要求："逐字来自原文"也不足以证明语义完整；截断了紧随语义原子的片段必须拒。而完整
版"如果是我杀的我就是女同"（11+全角字）本身就超过 `PUNCH_LINE_MAX_EM=9.0` 单行宽度上限
（`src/autoslice/cover_punch_semantics.py:15`），无法整体作为一行。

CPA 审核 prompt（`cover_punch_semantics.py:216-221`）明确写死了反例："两个各自来自标题、
合起来却不成事件的关键词"必须 REVISE，举的例子就是"生豆角 / 熊猫头下播"——两个各自成立
但拼不出一件事的名词短语。本候选反复被提交的正是结构相同的反例：`我就是女同`（发誓内容）
+ `最强女高中生`（另一个人的称谓）两条彼此独立的短语，拼在一起不构成一句可读事件。第 9 次
（最后一次，也是唯一留下完整回执的拒绝案例）三个布尔位全部为 `true`
（`stranger_can_infer_event`/`contains_concrete_subject`/`contains_action_or_conflict`）、
`story_summary`/`click_motivation` 也合格，但仍 `status=FAILED,
reason_code=CPA_PUNCH_SEMANTIC_REVIEW_REJECTED`——说明卡点不在"语义是否成立"，而在
`review_cover_punch_semantics()` 里 `final = punch_validator(...)` 返回空：CPA 提议的
`final_punch` 未能通过机械抽取校验（逐字连续子串/宽度/边界安全其一失败），`final_punch: []`
即是证据。

唯一一次真正过了文字关的第 7 次，CPA 选择的解法是**整体放弃 "我就是女同" 这条线**，改用
`最强女高中生 / 宠到猛做任务`——徒留反常宠爱情节，丢了 Ivan 想要的那句发誓梗。也就是说：
在当前"1-2 行、每行 2-12 字、必须组合成一个事件"的强约束下，CPA 判断"我就是女同"这句话
本身**没有安全的搭档能与它组成一个可读事件**，宁可整条换掉也不硬凑。

（第 9 次 `final_punch: []` 的具体机制留一个诚实的空白：`review_cover_punch_semantics()`
里空结果既可能来自 CPA 明确返回 `status:REJECT`（prompt 定义为"标题里不存在合格的抽取式
短梗字"，即便三个布尔位仍可为 true——REJECT 判的是"没有合格片段"而不是"故事不成立"），
也可能来自 `status:REVISE` 但 `final_punch` 未通过机械抽取校验（逐字连续子串/宽度/边界
安全其一失败）。这次的原始 response 没有落盘，两种读法都无法排除，但两者都指向同一个
(a) 结论：不管卡在语义裁决还是机械抽取，卡点都是"我就是女同"配不出合格搭档。）

### (b) 路由是否在傻重试同一条文本？—— 部分成立但不是主因

第 7 次和第 9 次的 `original_punch` 不同（`["我就是女同","猛做任务"]` vs
`["我就是女同","最强女高中生"]`），说明候选抽取器每轮确实在换不同的 n-gram 组合，不是
逐字重放同一败局；8 次 `COVER_PUNCH_REVIEW_REQUIRED` 分布在 5 个不同 pipeline
fingerprint 上（`0f8ec3abfb14/7ed35abe8058/57691cc1ef04×3/7f1c4aa2bb73×3/a198b7aa3bde`），
不是同指纹机械空转。所以主诉"路由不会换着法子试"不完全成立——它确实在变化候选片段。
但缺口在于：**在仅有的两次可考回执**（#7、#9）里，都没有出现"我就是女同"单行独占
（`main` 独占、`sub=null`）或保留"如果是我杀的"因果结构的更短同义抽取组合；#1-6、#8 这
7 次的候选内容本身就是上一节指出的取证缺口（未落盘），无法断言路由在那几轮里试过什么。
基于现有两个样本，候选生成器看起来只在"标题里的几个名词短语"之间排列组合，没有对"必须
带因果连接词才成句"这个反馈做针对性调整——但这是基于 2/9 样本的弱观察，不是对全部 9 轮
路由行为的断言。

### (c) 身份关有真实门 bug，且烧掉了唯一一次过关的图 —— 有代码证据

`src/autoslice/cover_host_identity_gate.py:245-254` 的 `identity_passed` 要求
`primary_subject_matches_other_source_participant is False` 才算过关——按测试用例
（`tests/test_cover_host_identity_gate.py:73-118`）这个字段的设计语义是"最终主体其实是
源图里**另一个、错误的**参与者"（true=坏，例如误认成"伊索尔Sol"）。但第 7 次的 CPA
回执里：

```
"primary_subject_is_lidousha": true,
"primary_subject_matches_other_source_participant": true,   ← 与上一行自相矛盾
"identity_conflicts": [],
"reason": "左图右下可见李豆沙的白发、熊猫耳帽和眼镜特征；右图最大主体延续这些身份
           特征，脸部大且清晰，表情和动作直接承担完成猛任务的故事反应。……"
```

九个布尔位里唯一为 `true`（本应为 false）的就是这一个易混淆命名的字段，其余八个
（含 `primary_subject_is_lidousha`、`identity_conflicts=[]`、`composition_conflicts=[]`
及自然语言 `reason`）通篇都是"身份延续正确、构图合格"的正面判断。这是模型对这个
英文字段名的语义误读（提示词全程用中文问"是否延续李豆沙而不是其他参与者"，模型很可能把
`true` 理解成"是延续（而不是别人）"而不是字段本意的"匹配了别的参与者"），代码按字面
`is False` 判定直接判死，产出的 `reason_code=FINAL_HOST_IDENTITY_MISMATCH` 与回执自身
文本矛盾。

同时要澄清：第 7 次出的图本身**确实有问题**——`final.cover.png` 实拍是同一个白发熊猫耳
女孩被复制成三份（主图 + 左右两张贴纸克隆），Ivan 原话"三个克隆熊猫人偶"准确描述了这张
图（已下载核对像素）。现有 identity/composition 九个布尔位schema里**没有任何一项检测
"同一角色被复制粘贴多份"**，所以就算那个字段没有 bug，这张图大概率也会被
`meaningless_dominant_decoration`/`composition_conflicts` 之外的某个没写进 schema 的
标准挡下——只是当前挡它的具体 reason_code 是错的、且判断依据是一处自相矛盾的字段。

结论：本例是 (a) 为主（"我就是女同"在现行 1-2 行/2-12 字/单事件三重约束下结构性难
punch）+ 一个真实但**不改变本例最终结果**的 (c) 身份关字段歧义 bug（它烧掉了本该更早
识别为"图有问题但理由错"的一次尝试，且该字段歧义会在其他候选上产生假阴性）+ 较弱的 (b)
（路由确实在变换候选，但从未尝试"单行独占我就是女同"这种更贴近钩子的组合）。

## 三、是否已有降级路径，为什么没走

`grep _talk_font_floor_layout_override / 无梗字`：命中 `src/autoslice/cover_generation.py:414,495,513`。
这是 **2026-07-31 拍板的字号下限自愈**，只处理"单段、无词原子、无 LLM 分行"且
`allow_punch=False`（例如歌切、或艺术指导阶段就判定不需要梗字）的情形，把整段短文案
放大到 120px 下限；它从来不是"punch 审核耗尽后允许发布无梗字/全文封面"的豁免通道。
`docs/pipeline/70-cover.md:44-45,66-70` 明文禁止 `cover_punch_allowed=false` 或空 punch
回执把长 `cover_text` 直接放行——这条本身就是为了防止之前"无梗字即整段回退"制造过
3-8 行违规封面的老 bug（`docs/reviews/cover-text-violations-triage-20260731.md`）。所以
`_talk_font_floor_layout_override` 对本候选**不适用**，不是"路由没走它"，而是这个候选
一直卡在 `allow_punch=True` 分支，走到这条自愈线要求的前置条件（放弃 punch）本身就是
被禁止的。

文档另一处（`docs/pipeline/70-cover.md:62-63`）写的政策是：**有界重试让 CPA 改选更短的
连续原文，仍无则候选降 `PENDING_COVER`**——即"实在选不出合格短梗字，就把候选打回待定，
不发无梗字/全文封面"。代码层面对应的实现是 `cover_repair.py:2080-2148` 的
`cover_repair_attempts`(3/指纹) + `cover_repair_lifetime_attempts`(9/终身) 双预算，耗尽后
`_cover_repair_eligible()` 返回 False，但 `cover_repair_needed()` 仍然对
`BLOCKED_AI_COVER_REQUIRED` 状态返回 True（`cover_repair.py:2129-2130`）——即耗尽预算后
候选**不会**被自动打成不可逆的 `PENDING_COVER`，也不会自动发出无梗字/全文封面；它就停在
`BLOCKED_AI_COVER_REQUIRED(...)(repair_budget_exhausted)`，等待人工介入（新指纹 = Ivan
手动改 cover_text/story_hook，或提供 `lidousha-full-text-cover-contract.v1` 显式授权）。
`_cover_repair_eligible()` 上方注释直接写明这是有意为之："budget 限制 paid generation
attempts，never integrity detection……必须保持 fail-closed，让后续 title/media/document
drift 在 state 和报告里保持响亮"——即**故意不做静默降级**，这是设计选择而非缺陷。

## 四、最小系统性修复建议

1. **修复身份关字段歧义（最小、独立、可测）**：给
   `primary_subject_matches_other_source_participant` 加一条内部一致性校验——若它为
   `true`，`identity_conflicts` 必须非空且 `primary_subject_is_lidousha` 必须为
   `false`；若与其他字段矛盾（如本例），判为 `HOST_IDENTITY_VERDICT_UNPARSEABLE`
   （走"证据不可用"分支，可重跑而非直接终局 FAIL）而不是静默按字面判死。这不改变
   现有正确用例（两个既有测试的字段组合本就自洽），只堵住这一处会烧 lifetime 预算的
   假阴性。
2. **补全逐轮回执落盘**：`review_cover_punch_semantics()` 已经产出完整的 `proof` dict
   （含 request/response sha256、booleans、summary），但只有走到图像分支的那次才写文件。
   建议每次 punch 审核不论过/拒都落一份 `punch_review.json` 到当轮的 `cover_repair/
   generations/<fingerprint>/`，避免下次取证只能从一行日志摘要反推。
3. **耗尽后的状态要可行动，而不是无限期沉默 BLOCKED**：`cover_repair_needed()` 目前对
   `BLOCKED_AI_COVER_REQUIRED` 永远返回 True，但 `_cover_repair_eligible()` 耗尽后又
   永远 False——两者叠加的净效果是候选**卡在一个不会再被机器碰、也不会升级成更醒目
   信号的稳态**，只能靠人工巡查 `cover_status` 字符串发现。建议耗尽时额外写一条明确
   面向人工的 `cover_repair_exhausted_reason`（例如指出"我就是女同"是唯一未能通过
   两两组合测试的候选原子），并复用第 7 次已经被 CPA 验证过语义合格的
   `最强女高中生 / 宠到猛做任务`（一旦 (1) 修好，这条本该能过身份关）作为待人工确认的
   预案，而不是让 Ivan 从零排查 9 次日志。

不建议的方向：**不要**把"punch 耗尽自动退化成无梗字/全文/font-floor 布局"做成自动
路径——这与 `docs/pipeline/70-cover.md:44-45,66-70` 的显式禁令和 7/31 三行违规封面的
历史教训直接冲突，且会绕开"哪句话该不该单独出现在缩略图上"这种需要人工判断的语义
决定（本例核心正是"我就是女同"能不能独立成句地印在缩略图上）。

## 附：观察到的封面图（第 7 次，图像身份关判 FAIL 的那张）

`final.cover.png` 主体为白发熊猫耳女孩，被复制成三份贴纸（主图居中 + 左右各一张同角色
不同表情的克隆贴纸）——Ivan 原描述"三个克隆熊猫人偶"准确。当前 identity/composition
九项布尔 schema 未覆盖"同一角色被复制多份"这类缺陷，即使字段 2 的歧义被修好，仍建议
补一条 `duplicate_subject_clutter` 布尔位。
