# 2026-07-22 无人值守自动切片批次

> **HISTORICAL V8 SNAPSHOT / STALE_POLICY / NO_UPLOAD**
>
> 本文件不是当前恢复 authority，也不证明当前可上传。不要从历史快照推断下一轮版本、路径、
> 指纹、部署或发布状态；当前 exact 集合、plan、publication authority 与执行状态只读项目
> `docs/HANDOFF.md`、对应 `docs/pipeline/` 步骤和 live `free`。下列状态、成品和候补都只是
> v8 当时的历史投影。

- 状态: **HISTORICAL_V8_STALE_POLICY**
- 运行模式: **RECOVERY_REVIEW** · 来源: **OFFICIAL_COMPLETE_REPLAY** · 上传许可: **否**
- 包口径: “谈话成品”只投影 CURRENT + COMPLIANT + 已交付；拒绝/候补/旧政策包互斥显示（旧包 0 条）
- 交付实况: 谈话 **5 交付**（3 条边界自修复后交付） / 歌 **0 交付** · 0 被完整性门拦截 · 共尝试 0
- 段: 完成 1 / 死段 0 / 待产出 talk 0 + song 0
- 联动证据旁路: **NO_TRIGGER**（NO_TRIGGER 仅表示开发旁路未触发，绝不等于非联动） · 未来候选场 0/5（仅未标注开发证据，不代表已确认联动或可训练）
- 会话关系权威: **CONFIRMED**

## 历史 v8 谈话产物（不是当前合规交付）

| 成品 | 时长 | 标题 | 选片理由(hook) | 量化分 | 收束句 | 边界 | 封面实际路线 | 路由理由 |
|---|---|---|---|---|---|---|---|---|
| `李豆沙展示最爱的金发有角妹妹，被说像` | 1:33 | 最包容异性恋的直播间，看到男角色只能说出一句不熟 | 李豆沙展示最爱的金发有角妹妹，被说像礼墨Sumi立刻否认，介绍得满眼喜爱，看到男性角色却瞬间改口“不熟”。 | T1 / 95.25 | 其实是最包容异性恋的直播间 | ok_sentence_boundary_cut | 截图直出（AI未调用） | hash-bound source frame verifies all required participants |
| `被当面追问为什么说“最最最喜欢”，李`（边界自修复×1） | 4:39 | 被坏女人南町问到最最最最喜欢的原因，后来才发现自己才是被收集的那个 | 被当面追问为什么说“最最最喜欢”，李豆沙先拿报恩当借口，又被一串L系CP名拆穿成集邮海王。 | T1 / 89.5 | 哈哈哈 | ok_sentence_boundary_cut | 截图直出（AI未调用） | hash-bound source frame verifies all required participants |
| `弹幕追问李豆沙为何请南町吃火锅，她从`（边界自修复×1） | 3:12 | 【李豆沙】弹幕追问李豆沙为何请南町吃火锅，从“付出劳动”嘴硬到“最最喜欢”，刚认识就互相霸凌 | 弹幕追问李豆沙为何请南町吃火锅，她从“对方付出了劳动”一路嘴硬到“最最喜欢”，最后把两人的关系定义成互相霸凌、刚认识但马上一起吃火锅。 | T1 / 89.25 | 暂时不太熟 | ok_sentence_boundary_cut | 截图直出（AI未调用） | hash-bound source frame verifies all required participants |
| `看着像不良帅姐的搭档把大椅子让给李豆`（边界自修复×1） | 1:47 | 【李豆沙】看着像不良帅姐的搭档把大椅子让给李豆沙，缩在角落假哭，熊猫头怎么反成霸凌者了 | 看着像不良帅姐的搭档把大椅子让给李豆沙，自己缩在角落，最终被鉴定成外表凶狠、实际挨欺负还会假哭的类型。 | T1 / 72.75 | 那很严苛了 | ok_sentence_boundary_cut | 截图直出（AI未调用） | hash-bound source frame verifies all required participants |
| `弹幕让左边的人弹右边一个脑瓜崩，两人` | 1:08 | 【李豆沙】弹幕让左边的人弹右边一个脑瓜崩，熊猫头争了半天镜像左右，还是被kmx算明白了 | 弹幕让左边的人弹右边一个脑瓜崩，两人为了镜像左右争论半天，李豆沙还是中招，才发现观众早把她们算得明明白白。 | T1 / 68.75 | 再弹，再，再硬弹一弹 | ok_sentence_boundary_cut | 截图直出（AI未调用） | hash-bound source frame verifies all required participants |

## 封面路线审计（以实际执行证据为准）

> 内部兼容状态 `AI_COVER_READY` 只表示封面文件已就绪，不表示使用了 AI。以下结论只来自通过校验的 `lidousha-cover-route-decision.v2`；缺证时会显式显示 UNKNOWN。

- `auto_193450_3573_3665`：**截图直出（AI未调用）**；证据=VALID_V2；执行状态=READY；选中理由：hash-bound source frame verifies all required participants
  - 决策时未选 截图轻调：the verified source frame already carries the hook; image cleanup would add generation risk without a demonstrated need; selected evidence: hash-bound source frame verifies all required participants
  - 决策时未选 AI 重绘：the verified source frame already carries the hook and participant relationship; a redraw would discard grounded story evidence; selected evidence: hash-bound source frame verifies all required participants
- `auto_193450_672_945`：**截图直出（AI未调用）**；证据=VALID_V2；执行状态=READY；选中理由：hash-bound source frame verifies all required participants
  - 决策时未选 截图轻调：the verified source frame already carries the hook; image cleanup would add generation risk without a demonstrated need; selected evidence: hash-bound source frame verifies all required participants
  - 决策时未选 AI 重绘：the verified source frame already carries the hook and participant relationship; a redraw would discard grounded story evidence; selected evidence: hash-bound source frame verifies all required participants
- `auto_193450_1863_2056`：**截图直出（AI未调用）**；证据=VALID_V2；执行状态=READY；选中理由：hash-bound source frame verifies all required participants
  - 决策时未选 截图轻调：the verified source frame already carries the hook; image cleanup would add generation risk without a demonstrated need; selected evidence: hash-bound source frame verifies all required participants
  - 决策时未选 AI 重绘：the verified source frame already carries the hook and participant relationship; a redraw would discard grounded story evidence; selected evidence: hash-bound source frame verifies all required participants
- `auto_193450_1573_1672`：**截图直出（AI未调用）**；证据=VALID_V2；执行状态=READY；选中理由：hash-bound source frame verifies all required participants
  - 决策时未选 截图轻调：the verified source frame already carries the hook; image cleanup would add generation risk without a demonstrated need; selected evidence: hash-bound source frame verifies all required participants
  - 决策时未选 AI 重绘：the verified source frame already carries the hook and participant relationship; a redraw would discard grounded story evidence; selected evidence: hash-bound source frame verifies all required participants
- `auto_193450_1475_1543`：**截图直出（AI未调用）**；证据=VALID_V2；执行状态=READY；选中理由：hash-bound source frame verifies all required participants
  - 决策时未选 截图轻调：the verified source frame already carries the hook; image cleanup would add generation risk without a demonstrated need; selected evidence: hash-bound source frame verifies all required participants
  - 决策时未选 AI 重绘：the verified source frame already carries the hook and participant relationship; a redraw would discard grounded story evidence; selected evidence: hash-bound source frame verifies all required participants

## 历史 reserve 投影（当前 exact contract 下 INELIGIBLE）

| candidate | 处置 | 时间 | hook | 量化分 |
|---|---|---|---|---|
| `auto_193450_1304_1368` | RESERVE | 1304-1368s | 观众送泡泡机起哄表演，两人唱完立刻要求礼物翻倍，李豆沙最后把撒娇变成“我很可爱，请给我钱”的当场打劫。 | T1 / 70.25 |
| `auto_193450_5552_5669` | RESERVE | 5552-5669s | 李豆沙嘴上制止地域地狱梗，却自曝被爱唱张雪峰烂梗的朋友彻底洗脑，聊到最后已经分不清直播间里谁才最地狱。 | T2 / 66.0 |
| `auto_193450_5782_5844` | RESERVE | 5782-5844s | 游戏里的龙角少年只带波浪号“嗯”了一声，李豆沙就被萌到反复回味，发现不能重放后急得想直接去搜角色语音。 | T2 / 63.0 |
| `auto_193450_5341_5459` | RESERVE | 5341-5459s | 两个人争论谁下播时无人挽留更可怜，李豆沙却坦白自己挽留搭档只是因为东西还没下好，随后又放话零个人想看就该赶紧下播。 | T2 / 57.0 |

## 歌切（每场至多 1 个、本日汇总；按弹幕量排序；已发布歌曲跳过；仅李豆沙本人演唱且完整才切；背景音乐/原曲播放/SONG_PARTIAL 均不交付；被拦不占配额、备份自动回填）

(本场未检出/未产出歌切)
