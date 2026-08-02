# 2026-07-24 无人值守自动切片批次

- 状态: **review_ready_with_failures**
- 运行模式: **PRODUCTION** · 来源: **RECORDER** · 上传许可: **否**
- 包口径: “谈话成品”只投影 CURRENT + COMPLIANT + 已交付；拒绝/候补/旧政策包互斥显示（旧包 0 条）
- 交付实况: 谈话 **2 交付**（2 条边界自修复后交付） / 歌 **0 交付** · 6 被完整性门拦截 · 共尝试 6
- 段: 完成 5 / 死段 0 / 待产出 talk 0 + song 0
- 联动证据旁路: **NO_TRIGGER**（NO_TRIGGER 仅表示开发旁路未触发，绝不等于非联动） · 未来候选场 0/5（仅未标注开发证据，不代表已确认联动或可训练）
- 会话关系权威: **UNKNOWN**

## 谈话成品（仅当前合规交付）

| 成品 | 时长 | 标题 | 选片理由(hook) | 量化分 | 收束句 | 边界 | 封面实际路线 | 路由理由 |
|---|---|---|---|---|---|---|---|---|
| `听说安晚也吃了生豆角，李豆沙震惊之余`（边界自修复×1） | 2:09 | 【李豆沙】听说安晚也吃了生豆角，小李决定下播暗示礼墨也吃，三人组必须要有这个团结默契！ | 听说安晚也吃了生豆角，李豆沙震惊之余决定下播暗示礼墨也吃，誓要用集体中招维护三人组的“团结默契”。 | T1 / 82.0 | 一天不见吧 | ok_sentence_boundary_cut | AI 重绘（AI已调用并用于最终图） | motion without confident cover subject (score=3.36) |
| `观众声称只带一个技能保证让她赢，李豆`（边界自修复×1） | 1:30 | 【李豆沙】观众声称只带一个技能保证让她赢，熊猫头连“支持李豆沙”都没说完就输了：有骗子！ | 观众声称只带一个技能保证让她赢，李豆沙刚准备下注就遭秒杀，当场怒斥骗子并宣布再也不信了。 | T1 / 92.25 | 我也不信你了 | ok_sentence_boundary_cut | AI 重绘（AI已调用并用于最终图） | motion without confident cover subject (score=5.12) |

## 封面路线审计（以实际执行证据为准）

> 内部兼容状态 `AI_COVER_READY` 只表示封面文件已就绪，不表示使用了 AI。以下结论只来自通过校验的 `lidousha-cover-route-decision.v2`；缺证时会显式显示 UNKNOWN。

- `auto_183122_1209_1410`：**AI 重绘（AI已调用并用于最终图）**；证据=VALID_V2；执行状态=READY；选中理由：motion without confident cover subject (score=3.36)
  - 决策时未选 截图直出：the source-frame evidence is not strong or verified enough to ship without image generation; selected evidence: motion without confident cover subject (score=3.36)
  - 决策时未选 截图轻调：bounded cleanup cannot repair the missing subject confidence, weak composition, or inability to express the hook; selected evidence: motion without confident cover subject (score=3.36)
- `auto_193129_850_940`：**AI 重绘（AI已调用并用于最终图）**；证据=VALID_V2；执行状态=READY；选中理由：motion without confident cover subject (score=5.12)
  - 决策时未选 截图直出：the source-frame evidence is not strong or verified enough to ship without image generation; selected evidence: motion without confident cover subject (score=5.12)
  - 决策时未选 截图轻调：bounded cleanup cannot repair the missing subject confidence, weak composition, or inability to express the hook; selected evidence: motion without confident cover subject (score=5.12)

## 候选门禁拒绝（终态，不是成品）

| candidate | hook | 门 | 原因 | 精确证据 |
|---|---|---|---|---|
| `auto_190124_1571_1804` | 她专挑“奶妈”切磋却被骗得一滴血都打不掉，随后又被高战力“李豆沙本物”击败，当场为谁才是本物激烈辩护。 | boundary_semantic_review | unsafe_boundary_backfilled | gate=BOUNDARY_CONTEXT_EXHAUSTED; lane=—; reason=— |
| `auto_183122_962_1042` | 弹幕用缩写拱李豆沙和礼墨“结婚”，她却解释成把对方赶出直播间后的“结冰”，最后承认故意喊人来。 | foreign_source_transcription | subtitle_authority_unresolved_backfilled | token=cosplay cosplay; cue=1; span=0-3280ms; missing=positive_source_audio_transcription |
| `auto_190124_199_480` | 她疯狂送花送钻戒把好感刷到最亲密，却在回复选项里纠结像妈妈还是激将法，选完把人气走后只能翻旧聊天找快乐回忆。 | final_review_findings | subtitle_authority_unresolved_backfilled | gate=—; lane=—; reason=— |
| `auto_200133_44_293` | 弹幕拿名字嘲笑李豆沙“是真的拉拉”，她先强行论证“没拉就是赢”，转头又自曝队伍里“只有女同”。 | foreign_source_transcription | subtitle_authority_unresolved_backfilled | token=orient sc; cue=42; span=108900-110540ms; missing=positive_source_audio_transcription |
| `auto_193129_424_535` | 李豆沙发现熟人百合厨成了别人的队友，立刻酸她“和别的女同一起”，甚至拿入间人间作品当诱饵挖人。 | final_review_findings | subtitle_authority_unresolved_backfilled | gate=—; lane=—; reason=— |
| `auto_193129_537_702` | 对手为了让李豆沙赢偷偷卸光技能，她从感动到嫌胜之不武，发现对方刮痧百局后彻底笑崩。 | final_review_findings | subtitle_authority_unresolved_backfilled | gate=—; lane=—; reason=— |
| `auto_183122_607_723` | 被问为何总在同事家开播，李豆沙坦白自己用惯豪华双屏回不去破笔记本，还被弹幕吐槽在玩“同事收集梦想生活”。 | final_review_findings | subtitle_authority_unresolved_backfilled | gate=—; lane=—; reason=— |
| `auto_193129_1648_1770` | 李豆沙赛前嘴甜说愿意输给观众、万一赢了只能怪自己天才，混战后看到73.9万战力又立刻质问谁敢对决。 | boundary_semantic_review | unsafe_boundary_backfilled | gate=BOUNDARY_CONTEXT_EXHAUSTED; lane=—; reason=— |

## 当前谈话候补（动态投影；已交付/已拒绝不会重复出现）

| candidate | 处置 | 时间 | hook | 量化分 |
|---|---|---|---|---|
| `auto_193129_1405_1460` | RESERVE | 1405-1460s | 弹幕说队友像贴身保镖，李豆沙却翻旧账吐槽他曾骗自己单挑秒杀，检查技能后吓得拒绝再战。 | T1 / 81.0 |
| `auto_200133_1277_1431` | RESERVE | 1277-1431s | 李豆沙兴冲冲挑战《想去海边》，唱到高潮才怀疑整首都错了，当场“啊啊啊不要呀”崩溃道歉并逃去换歌。 | T2 / 87.75 |
| `auto_190124_1178_1281` | RESERVE | 1178-1281s | 看到观众战力一个比一个高，她从“我成最拉的了”顿悟出真相：越要上班的人，越能靠摸鱼把这游戏玩明白。 | T2 / 87.0 |
| `auto_190124_719_818` | RESERVE | 719-818s | 面对“别氪太多”的劝告，她先说会克制，接着把克制解释成“氪金的氪”，最后得出成年人的自制力就是花钱。 | T2 / 83.0 |
| `auto_193129_153_401` | RESERVE | 153-401s | 李豆沙巡查玩家装扮，从“成分复杂的百合豚兼豆墨CP粉”一路锐评到乌鸦坏人，发现是队友后马上改口夸成夜行英雄。 | T2 / 82.25 |
| `auto_183122_297_552` | RESERVE | 297-552s | 李豆沙借住同事家却被猫现场挠了一爪，从嘴硬说猫叫是自己发的，到坦白连触碰活物都觉得越界。 | T2 / 80.75 |
| `auto_190124_1489_1569` | RESERVE | 1489-1569s | 小虎一来她就反复请求摸摸，从得意“最后还不是被我摸”到吓得不敢动，最后只能趁李墨抱住它时偷摸一下。 | T2 / 80.75 |
| `auto_193129_962_1013` | RESERVE | 962-1013s | 李豆沙挑战猛兽前豪言“洒洒水，NPC还能打不过”，转眼就开始寻找丢失的尊严，并宣布再也不信队友的话。 | T2 / 70.25 |
| `auto_193129_742_806` | RESERVE | 742-806s | 游戏让她在宝藏和史莱姆娘之间二选一，李豆沙毫不犹豫喊出“怕什么，我全都要”，随后才开始盘算收益。 | T2 / 69.25 |
| `auto_183122_228_294` | RESERVE | 228-294s | 李豆沙开场冒充礼墨Sumi，声称自己一小时染成白发，只留下两坨黑发把龙角包成了熊猫耳朵。 | T2 / 68.25 |
| `auto_190124_838_899` | RESERVE | 838-899s | 她看到“想听湖南人念satisfaction”的帖子竟然秒懂，还现场承认自己就爱这种抑扬顿挫、像唱歌一样的塑料英语。 | T2 / 64.0 |
| `auto_193129_0_53` | BELOW_THRESHOLD | 0-53s | 李豆沙看两位玩家形象很配便当场磕到，起哄“老公和小女友”打一局，随后又被公会战况急得撤退升级。 | T1 / 56.25 |

## 歌切（每场至多 1 个、本日汇总；按弹幕量排序；已发布歌曲跳过；仅李豆沙本人演唱且完整才切；背景音乐/原曲播放/SONG_PARTIAL 均不交付；被拦不占配额、备份自动回填）

| 歌 | 弹幕 | 门判定 | 原因码 | 标题 | 交付 |
|---|---|---|---|---|---|
| `song_200133_959` | x186 | BLOCK | JINGTING_PROVIDER_NOT_AGY,JINGTING_MODEL_MISSING,CPA_SEMANTIC_INCOMPLETE,CPA_RELEASE_NOT_READY,NOT_INTERESTING,SONG_AUDIO_LRC_ALIGNMENT_INVALID,SONG_FULL_BOUNDARY_PROOF_MISSING,SONG_LYRICS_ALIGNMENT_PROOF_MISSING,SONG_HOST_VOCAL_UNPROVEN,SONG_MATERIALIZED_RECUT_MISSING,SONG_SUBTITLE_ARTIFACT_HASH_INVALID,SONG_BURNED_PREVIEW_MISSING,SONG_RECUT_MANIFEST_HASH_INVALID,SONG_RECUT_MANIFEST_CONTENT_INVALID,SONG_RECUT_SOURCE_BINDING_INVALID,SONG_RECUT_PROOF_BINDING_INVALID,SONG_RECUT_INTERVAL_BINDING_INVALID,SONG_RECUT_STREAM_CONTRACT_INVALID,SONG_RECUT_ARTIFACT_BINDING_INVALID | — | 未过门不交付 |
| `song_203136_1509` | x155 | BLOCK | SONG_FULL_BOUNDARY_READY,SONG_PREVIOUSLY_PUBLISHED | 【李豆沙】豆沙歌，《宝贝》 | 未过门不交付 |
| `songvis_203136_230_e1411d75` | x156 | BLOCK | JINGTING_PROVIDER_NOT_AGY,JINGTING_MODEL_MISSING,CPA_SEMANTIC_INCOMPLETE,CONTEXT_DEPENDENCY_HIGH,CPA_RELEASE_NOT_READY,NOT_INTERESTING,VIEWER_CONTEXT_INCOMPLETE,SONG_AUDIO_LRC_ALIGNMENT_INVALID,SONG_FULL_BOUNDARY_PROOF_MISSING,SONG_LYRICS_ALIGNMENT_PROOF_MISSING,SONG_HOST_VOCAL_UNPROVEN,SONG_MATERIALIZED_RECUT_MISSING,SONG_SUBTITLE_ARTIFACT_HASH_INVALID,SONG_BURNED_PREVIEW_MISSING,SONG_RECUT_MANIFEST_HASH_INVALID,SONG_RECUT_MANIFEST_CONTENT_INVALID,SONG_RECUT_SOURCE_BINDING_INVALID,SONG_RECUT_PROOF_BINDING_INVALID,SONG_RECUT_INTERVAL_BINDING_INVALID,SONG_RECUT_STREAM_CONTRACT_INVALID,SONG_RECUT_ARTIFACT_BINDING_INVALID | — | 未过门不交付 |
| `song_203136_992` | x121 | BLOCK | JINGTING_PROVIDER_NOT_AGY,JINGTING_MODEL_MISSING,CPA_SEMANTIC_INCOMPLETE,CPA_RELEASE_NOT_READY,SONG_AUDIO_LRC_ALIGNMENT_INVALID,SONG_FULL_BOUNDARY_PROOF_MISSING,SONG_LYRICS_ALIGNMENT_PROOF_MISSING,SONG_HOST_VOCAL_UNPROVEN,SONG_MATERIALIZED_RECUT_MISSING,SONG_SUBTITLE_ARTIFACT_HASH_INVALID,SONG_BURNED_PREVIEW_MISSING,SONG_RECUT_MANIFEST_HASH_INVALID,SONG_RECUT_MANIFEST_CONTENT_INVALID,SONG_RECUT_SOURCE_BINDING_INVALID,SONG_RECUT_PROOF_BINDING_INVALID,SONG_RECUT_INTERVAL_BINDING_INVALID,SONG_RECUT_STREAM_CONTRACT_INVALID,SONG_RECUT_ARTIFACT_BINDING_INVALID | — | 未过门不交付 |
| `songvis_203136_1220_753b4bdd` | x113 | BLOCK | JINGTING_PROVIDER_NOT_AGY,JINGTING_MODEL_MISSING,CPA_SEMANTIC_INCOMPLETE,CONTEXT_DEPENDENCY_HIGH,CPA_RELEASE_NOT_READY,NOT_INTERESTING,VIEWER_CONTEXT_INCOMPLETE,SONG_AUDIO_LRC_ALIGNMENT_INVALID,SONG_FULL_BOUNDARY_PROOF_MISSING,SONG_LYRICS_ALIGNMENT_PROOF_MISSING,SONG_HOST_VOCAL_UNPROVEN,SONG_MATERIALIZED_RECUT_MISSING,SONG_SUBTITLE_ARTIFACT_HASH_INVALID,SONG_BURNED_PREVIEW_MISSING,SONG_RECUT_MANIFEST_HASH_INVALID,SONG_RECUT_MANIFEST_CONTENT_INVALID,SONG_RECUT_SOURCE_BINDING_INVALID,SONG_RECUT_PROOF_BINDING_INVALID,SONG_RECUT_INTERVAL_BINDING_INVALID,SONG_RECUT_STREAM_CONTRACT_INVALID,SONG_RECUT_ARTIFACT_BINDING_INVALID | — | 未过门不交付 |
| `song_200133_1522` | x48 | BLOCK | JINGTING_PROVIDER_NOT_AGY,JINGTING_MODEL_MISSING,CPA_SEMANTIC_INCOMPLETE,CPA_RELEASE_NOT_READY,SONG_AUDIO_LRC_ALIGNMENT_INVALID,SONG_FULL_BOUNDARY_PROOF_MISSING,SONG_LYRICS_ALIGNMENT_PROOF_MISSING,SONG_HOST_VOCAL_UNPROVEN,SONG_MATERIALIZED_RECUT_MISSING,SONG_SUBTITLE_ARTIFACT_HASH_INVALID,SONG_BURNED_PREVIEW_MISSING,SONG_RECUT_MANIFEST_HASH_INVALID,SONG_RECUT_MANIFEST_CONTENT_INVALID,SONG_RECUT_SOURCE_BINDING_INVALID,SONG_RECUT_PROOF_BINDING_INVALID,SONG_RECUT_INTERVAL_BINDING_INVALID,SONG_RECUT_STREAM_CONTRACT_INVALID,SONG_RECUT_ARTIFACT_BINDING_INVALID | — | 未过门不交付 |

## 歌切候选备份（按弹幕排序；门拦截后自动回填的来源）

- 22966160_20260724-20-01-33.mp4 1443-1506s 弹幕x42: 确定性歌检测补充(演唱段)
- 22966160_20260724-20-01-33.mp4 1700-1803s 弹幕x37: 画面右上歌单识别到《summertime》
- 22966160_20260724-20-01-33.mp4 1700-1803s 弹幕x37: 画面右上歌单识别到《小城夏天》
- 22966160_20260724-20-01-33.mp4 1700-1803s 弹幕x37: 画面右上歌单识别到《海芋恋》
