# vtuber-slice 交接（HANDOFF）

> 约定：每次实质进展或会话收尾更新本文件（五段：目标/已完成/进行中/阻塞/下一步）。
> 开工先读本文件 + AGENTS.md，别凭旧对话推断。

## 2026-07-09/10：外部审计修复轮（"控制面在撒谎"）—— runner v4 + 生产现场急救

**目标**：外部三轮审计（+ChatGPT Pro 终审）判定系统"关键处假绿"：①挂载死了心跳报绿；②BLOCK 记成 ok、0 交付叫 done；③上传授权不绑定最终文件；④talk 边界门是假门；⑤top5 先到先占坑；⑥state 覆盖写+损坏静默清零；⑦dirty tree 部署。**全部意见经逐条代码/现场复核认可**，本轮修复。

**已完成（生产现场急救，2026-07-09 19:0x–19:2xZ）**：
- **P0 live 事故**：CloudDrive FUSE 宿主挂载点 `Transport endpoint is not connected`（clouddrive 16:41Z 自杀重启后挂载传播断裂），`bilive_record` 绑死挂载进 `/app/Videos` → **录制链路当时是断的**，下场直播会全丢。修复序列：lazy umount → 重启 clouddrive2 → 宿主恢复 → 重启 bilive_record → 两侧 create/fsync/rename/read/delete 探针全过 + blrec API 正常。**故障窗口 ~16:54Z–19:15Z，期间每个 tick 均报未开播 → 无直播错过、无需回填**。
- cookie 全部 `chmod 600`（cookie.json / biliup_cookies.json / bak / yt txt）。
- **mount 看门狗**（`scripts/free_mount_watchdog.sh`，free cron */5）：探测 stale/hung FUSE（timeout ls，防挂死）→ 自动修复序列 + 恢复后重启 recorder + `ALERT_MOUNT_WATCHDOG.txt` 报告，30min 冷却防重启风暴。此故障 7/9 一天发生≥2 次，会复发。

**已完成（runner v4，commit 见 git log，342 tests）**：
- **源健康门**：tick 首步探挂载（subprocess ls + timeout，防 FUSE 挂死堵死 tick）；失败 → `ALERT_SOURCE_UNAVAILABLE.txt` + 心跳写 `SOURCE_UNAVAILABLE(...)` + 什么都不跑（fail-closed）。`list_dates/list_segments` 不再静默吞 OSError。
- **状态词诚实化**：歌 rc=0 无交付 = `blocked`（门在工作≠歌交付了）；talk 交付 = `review_ready`，带确定性红旗 = `quarantine`（交付但要人看）；批次终态 `review_ready / review_ready_with_failures / no_delivery`——0 交付永远不叫 done，摘要头新增"交付实况"行 + no_delivery 刺眼提示。旧 state 的 `ok` 兼容按已交付处理。
- **配额=交付**：`song_delivery_budget` 只数已交付；BLOCK 不占坑，按弹幕从结构化 backlog 自动回填（`SONG_ATTEMPT_CAP=6` 硬上限防无限产歌）。
- **全局选片 + 封场**：`prioritize` 改全局按 confidence 降序（metric v3 已嵌在召回 prompt 里），段内软上限 2（不够填满时让位）；**session sealing**——段清单（名+大小）跨 tick 稳定才做选片（7/9 实锤：0.94 晚段候选输给 5 条更早的 0.85-0.90）。晚段/多场同日进入竞争而非白来。
- **确定性边界红旗**（`produce_slice_package.boundary_red_flags`，不用 LLM）：切点后 speech island 续讲 ≥1.5s / 开头截进句中 / 下一句进尾部 pad / 收束句≠最终 SRT 末句 → 写进 boundary_audit + 交付记录，runner 标 `quarantine`。旧测试"允许切点后island续讲5s还叫绿"已改为断言出红旗。
- **state 原子写**：tmp+os.replace + `.bak` 上一版；损坏 → 隔离留证 `.corrupt-<ts>`、有 bak 恢复并持久化、无 bak 写 `state_corrupt_blocked` 标记文件（幂等，绝不当"全新日期"重产重交付）+ ALERT。
- **上传绑定**（`scripts/authorized_upload.py`）：`make-manifest`（审查时冻结 video+cover sha256+标题原文+Ivan 授权原话）→ `upload --manifest`（上传时重算 hash，漂移拒传；幂等账本 `upload_ledger.jsonl`，同视频重传硬拒=防充电器重复投稿复发；uploader 只接 manifest 参数）。free 的 `do_upload.sh` 已换为带门版本（裸调 exit 4），源收进 repo `scripts/free_do_upload.sh`。publish SKILL 已钉上此规则。
- **部署纪律**（`scripts/deploy_free_autoslice.sh`）：dirty tree 拒部署、远端打 `DEPLOYED_COMMIT` 指纹、部署后 md5 校验 runner；本轮起 free 上跑的就是 commit 的字节。

**进行中**：无后台进程。7/9 批次 5 条 talk 已交付待 Ivan 审（历史摘要头的 done 字样属修复前产物，歌切表格本身诚实）。

**阻塞**：无。上传永远逐条授权。

**下一步（审计"一周内"项，待排期/待 Ivan）**：①候选池 candidate_pool.jsonl + 封场后全局去重/多样性（本轮全局排序+封场已覆盖大半）；②talk/song 统一生命周期、golden replay（用 7/6、7/9 建回归集）；③审查界面（标题/hook/首尾上下文/红旗/Accept-Reject-Needs trim，落选默认折叠）；④根盘 88% 用量的水位/保留策略（审计正确指出：无背压设计前别上 local-first spool）。核心指标改口径：Ivan 接受并发布的片数 ÷ Ivan 审核分钟。

## 2026-07-07（续二）：《屑屑》歌切误报根因（歌名识别失败）+ 已上传 + 5 条收尾闭环

**目标**：查清《屑屑》为什么老被判"不完整"并**修工作流**（Ivan：不是放宽门那么简单——"歌曲要完整=后奏也要留、按 LRC 走、是屑屑"）；完成 5 条已授权上传收尾。

**根因（研究结论，非"门太严"）＝歌名根本没识别出来**：这次重跑 `song_repair` 的 LLM 猜歌名猜歪（快乐崇拜/穷开心…），歌词行查询也没搜到屑屑，最高才对上 10%（<55%）→ `song_boundary=null` → `full_song_evidence_ready=False` → `run_auto_review_shadow_pipeline` 的完整性豁免段（~1293）跳过 → `content_evidence` 退回**ASR 词重叠 / 歌时长**估完整度 → 而屑屑正是"末句+纯器乐后奏+念白"结构，词重叠必然不足 → `SONG_PARTIAL` 级联 `CPA_SEMANTIC_INCOMPLETE`（豁免需 song_complete=true）。且没走 LRC 边界→后奏（无词）被切。**三个诉求同源：钉死歌→识别成功→证据链成立（误报消失）+ LRC 边界（含后奏）+ 自动 LRC 字幕。**

**工作流修复（commit 5c634df，315 tests，已部署 free，端到端验证 decision=SONG_FULL_BOUNDARY_READY）**：
- **歌名去猜化·指纹钉歌**：`assets/lidousha/known_songs.json`（屑屑→netease 2615403834+指纹句）；`song_repair.fetch_netease_lrc` 按 id 直取；`_pinned_lrc_for_song` 拿窗口 ASR 指纹匹配（≥2 命中就钉，实测屑屑 4/5）塞进对齐池，靠真实对齐率裁定（钉错会被排掉，安全）。
- **后奏保留**：歌末不再切在最后一条 ASR cue，延到下一句念白前（`max_outro_ms` 封顶）。屑屑 3:31→**3:50**，多出 ~16s 无字幕器乐后奏。
- **LRC credit 出字幕的 bug**：`parse_lrc_text` 前缀过滤漏了中段关键词（音乐制作/贝斯演奏/混音、母带），netease 把它们打了后奏时间戳→烧成字幕。改为短冒号头内任意位置匹配 credit 词。屑屑 55→52 行，末行是末唱句。
- 歌一识别成功，烧录自动切 LRC 时间轴（`_write_lyric_timeline_srt`）→ LRC 成字幕权威（没洗≠没醒、high five≠beat）。

**已上传并闭环（Ivan 2026-07-07 授权"标题取为《屑屑》你就可以上传了"+"不用到本地直接传"）**：`【李豆沙】豆沙歌，《屑屑》你`（屑屑=谢谢谐音=谢谢你；歌切标题带"豆沙歌，"系列标记，封面不带）**BV1MgMt6UEGp**（aid 116877569297821/cid 39727268871），biliup 单次 rc=0，封面 right-split 持麦唱歌无吐舌。审核通过后后台 `xiexie_finalize.py` 自动补挂**小李歌唱**(8410735/9364628,code=0)+公开验证 **state=0**。uploaded.json commit 9d5989e、public_verify.json commit 2abaa7a。**发布链完整闭环**。

**5 条已授权上传收尾闭环（commit 3d969c1）**：礼墨CP BV1PHMt63Ez7 / 拉拉学历 BV1PHMt63Epi / 棒棒糖 BV1iBMt68EwS / 15坏女人(奖励) BV1RBMt6bEET / 电脑更新 BV1jBMt68EqL——全部 state=0、入小李切片(8383206/9320779；15坏女人+电脑首触 20111 限速重试成功；现 25 集)。10 份证据已 commit。

**进行中/阻塞**：无。本轮全部闭环（屑屑已发布入集验证、5 条切片已闭环、工作流修复已 commit+部署+测试）。上传逐条授权。可选后续：known_songs 随复现歌逐步补表（现仅屑屑）；歌切边界检测对"唱后念白/间奏"的 ADVISORY 提示仍保守（fail-closed 安全，非缺陷）。

## 2026-07-07（三）：SC 匹配工作流大修（首句谢错人的根因）+ 核听工具 + 提速

**目标**：Ivan 反复纠正——切片首句"谢谢X的SC"老是谢错人，要修**工作流**不是单个切片。

**根因（终于查清）**：她**批量清 SC 积压**，一条 SC 可能过好几分钟才轮到念。拉拉首句她谢的是 **十麻乃orient 在切片前 ~4 分钟(video 353s vs 切片 592s)** 发的 SC「想要成为真正的拉拉，还要经历一番风吹日晒」(切片 2-3 句逐字复读它)。旧逻辑只取窗内最近一条→抓成夏色星川(错，那条是"妈妈晚上好晚安")。CP 首句"跟李豆沙说句恭喜"(读 BlueArxiv 弹幕)被强礼墨Sumi语境改成"礼墨"(自称被带偏)。

**已修（工作流级，313 tests，已部署）**：
- **SC 回溯窗 120s→900s(15min)**(`produce_slice_package.SC_PRE_CONTEXT_MS`)：她拖很久批量补谢，"最近几条"不够；标 `【SC此前·名】`，靠内容匹配从积压挑对的。
- **agy_prompt SC 规则重写**：按发送者/内容匹配、时间只软提示、±10s 对SC不适用、对不上保留音频别硬套。
- **glossary 反向铁律**：自称(李豆沙/小李)绝不改成搭档名(礼墨Sumi/kmx)。
- **熊姐是对的**(她玩熊猫→熊姐梗)，收回误判。
- **核听工具** `scripts/apply_subtitle_correction.py`：BCUT层自动改不了的口播错字，给正确文本→改 srt+重烧 ~20s(不重产/不重封面)。`--set-line N='文本'`/`--replace 旧=新`(surgical)。
- **提速**：`produce_batch` 并行(MAX_PARALLEL_PRODUCE=3)、`--reuse-cover` 只改字幕跳 gpt-image-2。
- **自→白封面通病治本**：`_COVER_ZCOOL_WRONG_GLYPHS={"自"}`，含"自"整张换 SmileySans。

**验证 ✓**：重跑拉拉，工作流自己出对「谢谢十麻乃orient的SC」；CP 用核听工具改对李豆沙。两条 md5 同步到本地无误。

**进行中/待 Ivan**：①《屑屑》歌切 song-ID 已修(对上82%)，但完整性门卡"最后3句LRC无ASR匹配"(她可能没唱完/ASR漏尾)——放宽门 or 按现状交付 or 弃，Ivan 定。②5条上传候选(礼墨CP/拉拉/15个坏女人/棒棒糖/电脑)字幕现已可用；15个坏女人的"点上菜了/姐身子好"等仍是BCUT错听，要不要核听逐条改。

## 2026-07-07（续）：封面字卡放大（工作流级）+ 字幕结合SC + 5条上传候选字幕重审

- **封面字卡太小（Ivan 重复多次的通病，工作流级修复，已部署+310 tests）**：根因=标题带冒号→固定2行→侧分文字区窄(~700px)字被行宽卡小、竖直空间大量留白。修 `_fit_cover_lines`：不再锁死冒号2行，**评估所有行数取填满文字区的最大字号**，并**优先词安全分行**(LLM分行/冒号分句的相邻合并 `_regroup_lines`，在其≥最优90%时选它→放大不拆词)，词安全过小才退均衡器。艺术指导 prompt 加"侧分布局必须多分几行每行更短填满竖区"。实测礼墨CP封面从2行小字→5行大字铺满(被催找/礼墨学/谢礼物/你只是想/磕CP罢了)，礼墨/磕CP 整词。已就地换上 CP 交付封面；流水线修好后**后续所有封面自动变大**。
- **字幕结合画面SC（Ivan 2026-07-07，已部署）**：SC(醒目留言)不在弹幕xml，在 blrec 的 `.jsonl`(SUPER_CHAT_MESSAGE.data.message)。`produce_slice_package._load_superchats` 从 xml 同名 `.jsonl` 取 SC 文本(send_time-最早事件=视频相对时间)，标 `【SC】` 并入弹幕纠错上下文→AGY/CPA 结合她在读的 SC 卡片纠错。实证 SC 提供关键上下文(如"想看李漏右手唱地球大爆炸"=CP里"念到地球大爆炸"的由来；"想看豆沙多线程:被15个小女友拷打…扎针…蜘蛛"=15个坏女人由来)。
- **流水线提速（Ivan 2026-07-07："为什么这么慢"+"并行、只改字幕不重出封面"）**：诊断——单条 5-10min，瓶颈是**网络型 AI 步骤串行**(AGY gemini 看视频 1-3min + 几趟 CPA gpt-5.5 推理 + gpt-image-2 出封面 ~90s)+**源在 123云盘 FUSE 网盘慢读**+**5条串行**。修：① `produce_batch` 用 ThreadPoolExecutor **并行产出**(MAX_PARALLEL_PRODUCE=3，process_date 的 talk/song 循环改并行，title_failed 留 pending 下 tick 重试而非整批暂停)；② `produce_slice_package --reuse-cover` + `_stage_publish_draft(skip_cover=)` **只改字幕时复用已有封面**、跳过 art-direction+gpt-image-2(每条省~90-120s)，`produce_talk(reuse_cover=)` 接线；③ AGY prompt 说明**弹幕/SC已作为文本喂给你、别再去 OCR 糊字，把精力放音频+其他画面**(呼应 Ivan"agy工作降低了")。312 tests，已部署 free。**注**：SEND_GIFT 发送者名脱敏(小***)不可恢复；礼物感谢名认命。
- **SC 权威纠错（通病·工作流级，Ivan 2026-07-07 要求"严谨定义问题再动手"，已部署+311 tests）**：**问题**——她感谢/朗读 SC 的 cue，纯听 ASR 对**发送者名字+朗读内容**错得极多（名字/外语是 ASR 死穴；"谢谢十麻乃orient的SC"听成"谢谢142的SC"，日语おめでとう听成"没得到"）。之前我的 SC 集成只喂了 SC 正文当**软提示**、没带发送者名、AGY minimal-edit 不会替换。**修复方向**（严谨定过）：SUPER_CHAT 的 `user_info.uname` **不脱敏**(全名可取，SEND_GIFT 的 uname 脱敏成"小***"不可取→礼物名认命)，把 **uname+正文** 标 `【SC·<name>】<text>` 喂进纠错，并给 `agy_prompt` 加 **SUPER_CHAT AUTHORITY** 指令：与SC时间对齐的"谢谢…SC/朗读SC"cue，用SC精确名字+原文**覆盖音频**(屏上真值非猜测)；日语插话入 glossary(おめでとう等)。改 `produce_slice_package._load_superchats`(+uname)、`gemini_slice_jingting.agy_prompt`、glossary。**正用改好的流水线重跑5条验证**(十麻乃orient/おめでとう 两个靶点)。
- **5 条上传候选字幕重审（进行中，subagent）**：礼墨CP/拉拉学历/15个坏女人/抢棒棒糖/电脑更新——重跑 produce_talk(默认 bcut_agy_cpa=**新鲜 AGY 精听听音频**修错听 如"熊姐/念到地球大爆炸")+SC上下文+glossary(加 李漏/李露→小李)+大字封面，同 hook 覆盖原交付。**拉拉切片确认已产出**(promo_210019，标题「主播探究拉拉:先五年高考十年模拟」)，在上传集里。歌切不动。subagent 验字幕准确率后回报。

## 2026-07-07：选片 metric v3 + 歌切流水线双修 + 监控修复 + 专名纠错

**目标**：Ivan 反馈①选片 metric 要把百合/CP 提到最高优先级、把两条被落选的百合/CP切片做成成品；②歌切应由 free 流水线自动出（不该我手动）——修流水线；③监控 `com.ivan.lidousha-slice-monitor` exit=1。

**已完成**：
- **选片 metric v3**（`assets/lidousha/slice_selection_metric.md` + 记忆 lidousha-slice-selection-metric）：新增"优先级分层"硬序压过六维——第一层 百合/GL+与具体人物(kmx/礼墨Sumi/露蒂丝)的CP/磕CP/关系("看不腻")；第二层 围绕本人的自证/玩梗("看多会腻")；第三层 hook讲不清趣点的泛泛感慨(降级)。加"hook可读性准入门槛"。**通病**：metric v2 已列百合/关系维度但没定层级，召回把自证(0.86)/泛泛(0.86)排在礼墨Sumi-CP(0.84)之上。已 sync free。
- **专名纠错（Ivan 抓的错，已owning）**：CP切片人名不是"李墨素"——那是 ASR 听错版，真名 **礼墨Sumi**（glossary 早有）。我看到了礼墨Sumi 还另加了个错词条，是我疏忽。已把所有听错版(李默送/李救下/李墨素/李墨素描/刘李慕思维)并入 glossary 礼墨词条→修正为礼墨Sumi，metric/memory/驱动脚本全改，并 SendMessage 纠正在产的 subagent。
- **歌切流水线双修（都已部署 free + 测试 309 pass）**：诊断第2首歌切被门拦的根因=**两层 bug**，非"完整歌不够"。查明该歌是 **《屑屑》(ChiliChill乐团)**，实际可自动交付(ASR 对 LRC 67-71% 行匹配 > 55% 门)。Bug①**anchor 渗入前置谈话**：召回给的歌 anchor(1073s)起点扎进"猪鼻子"谈话，窗口以谈话开头→窗内召回改判 talk→AUTO_RECUT。修：`_song_core_span` 用 ASR 音乐前奏静默 gap 把窗口**前缘**裁到真正开唱处(只裁前缘不裁尾——裁尾会切掉歌末尾间奏后的副歌→SONG_PARTIAL)，`window_classified_song=False` 时自动重试一次(自愈，嘉宾那种干净窗口不受影响)。Bug②**歌名识别失败**：`_build_lyric_queries` 取"3条最长行"当 netease 查询，但最长的恰是 BCUT 糊掉的英文/rap段("chewe now baby")→查不到；改为**取干净的 CJK 密集行做单条查询**(实测干净行"谁说圆满的人生才能算圆满"→netease 首位就是屑屑)。
- **监控修复**（`scripts/lidousha_slice_monitor.py`，Ivan 问"修好了吗"——诚实：之前没动，现已修）：三个 bug——(a)`sys.exit(verdict码)` 让 DEGRADED→exit 1 污染 `launchctl list`(读着像监控自己挂了)，违背"告警只走报告文件"约定→改成**成功运行一律 exit 0**，健康走报告/邮件，旧行为留 `--health-exit` 开关；(b)`audio_missing` 误报根因=音频检查探的是 1GB 裸 `.m4s` 分片，ffprobe 扫它**超时**(rc=124)→异常→has_audio=False→假告警；改为**探已合成的可读 `.mp4`**(ORIG_RX 只配 .m4s 紧凑日期名，加 FINISHED_MP4_RX 配带横线的 .mp4)+**探测失败按"未知"不按"缺失"**。实测现 verdict=OK、audio=aac/LC/2ch、**exit=0**，launchctl 已清。

**进行中（两 subagent）**：①产 礼墨Sumi-CP + 拉拉GL 两成品(已发纠正、用礼墨Sumi重跑)；②用**原始 anchor** 跑 produce_song 复产《屑屑》证明流水线自愈端到端(不再手动)。都验字幕/封面后回报。

**阻塞**：无。上传永远逐条授权。**Codex/mhb 自动化**（6:10am ghostty检查、8am-mhb审计）确认**不在 launchd**(只有 Codex/Ghostty 桌面 app 本身)，属其他项目 Codex 托管，非 vtuber-slice——我没动；另注意到 `com.ivan.prediction.wsl-tunnel` exit=255(prediction 项目，也在挂)。要不要把这些转成 launchd 需 Ivan 定(跨项目)。

**下一步**：两 subagent 完成后验收；屑屑 若自愈交付成功=流水线双修验证闭环；metric v3 下批直播自动生效。

## 2026-07-06 晚：7/6 直播无人值守首个真批次（runner v3 实战）+ 封面自愈

**目标**：CPA 恢复后全新重跑 7/6 批次（产物已清空），验证 runner v3 五项修复真实生效。

**已完成**：
- **批次自动跑通中**：发现 5 talk（信心 0.86–0.92，hook 与选片 metric 对齐）+ 恰好 2 歌切（《嘉宾》x298 沙壁反转 /《今天也过得很愉快》x234）+ 10 落选 + 2 歌备份 + 死段账本捕获 2.9KB 残桩。标题全部真标题（如「【李豆沙】电脑要造反？小皇帝拒绝更新」）。
- **字幕豆腐框根除实拍验证**：pick1 交付 mp4 抽帧肉眼核对 sapphire72 白字宝蓝描边、CJK 字形完整（不再只看退出码）。
- **CPA 图像网关两层新故障，当场修**：① new_api 加了"宽高必须 16 倍数"校验，老请求 1920x1080 整批 400 → 请求改 `1920x1088` + 收图 `_normalize_cover_canvas` 裁回 1920x1080 叠字画布（`run_auto_review_shadow_pipeline.py`，探针实测 400 消失）；② 修完暴露图像渠道断供（500 渠道不存在 / 503 auth_unavailable providers=codex，token 无 gpt-image-1.5 权限）→ **不擅自换模型**（7/4 先例：换模型需 Ivan 拍板），runner 加批末 `repair_covers` 封面专项自愈：交付成品缺封面→用产出侧干净参考帧（`cover_refs/`，不用烧了字幕的 mp4 抽帧）重出，真实失败限 3 次，**通道断供签名不烧次数、跨 tick 无限等恢复**。已部署 free，298 tests。
- 记忆已更新 `cpa-real-ai-cover-always`（16 倍数 + 渠道断供两层故障与应对）。

**批次终态（20:31 free 时间，0 失败）**：5/5 谈话交付（标题全真全过禁词门、字幕抽帧 2/2 验过、边界全 ok_sentence_boundary_cut、hook+信心齐全）；歌切《嘉宾》x298 过完整性门交付（4:41 完整曲、LRC 字幕实拍正常）；《今天也过得很愉快》x234 被门拦（窗口内容被改判 talk，AUTO_RECUT 不交付，fail-closed 正确）；AUTOSLICE_SUMMARY.md 质量达标（含 10 落选+2 歌备份+死段账本）。Mac launchd 每 30 分钟拉回本地 `lidousha/2026-07-06/`。

**封面自愈闭环（21:22–21:29，Ivan 通报渠道恢复后手动踢 tick）**：6/6 全部 attempt-1 修复成功（~70s/张，art direction→gpt-image-2→裁画布→叠字全链），断供期正确走"不烧次数"分支（20:40 tick 实证 10s 内识别 503 并 defer）。抽验 2 张：构图/表情/安全区/hook 高亮/《嘉宾》原子性全过；歌切封面外观随切片（猪鼻+脸颊熊猫都在）。

**进行中**：无。批次全闭环（5 talk + 1 song + 6 covers），Mac launchd 半小时内拉回本地。

**阻塞**：无。

**封面词内断行已修（2026-07-06，Ivan 确认后做）**：均衡分行器 `_wrap_even` 词盲，会把「小皇帝拒/绝更新」「吵闹熊/猫头的」拦腰断词。修法：艺术指导 LLM 新增 `lines` 字段产出**词感知分行**，经 `_validated_cover_lines` 严校（拼接后与文案一字不差+顺序不变+《歌名》和 hook_word 整词同行+行数≤布局上限+单行≤12字）才采纳，否则回退均衡器（永不劣化）；分行优先级 冒号显式 > LLM词感知 > 均衡器。`_fit_cover_lines` 加 `forced_lines` 参，overlay 元数据加 `line_split`/`rendered_lines` 存证。307 tests。**6 张已交付封面就地重排**（复用已存的 `.cover.ai-bg.png` 无字底，按各自 cover_generation.json 的原始 layout/hook/color 重叠字，零图像 API 调用）：电脑「小皇帝/拒绝更新」、嘉宾「吵闹熊猫头的/《嘉宾》」、不想下播「又在幻想全职主播」、黑白小猪均已整词；两条冒号标题（棒棒糖/15个）本就显式分行未动。抽验 3 张：断词消除、构图与人物对位保持、字号更大。

**下一步（待 Ivan）**：①产品语义拍板：top-2 歌切被门拦时要不要顺位补备份歌（本场 x208《地球大爆炸》/x198《宝贝》按字面"弹幕最高的两个为准"未补位）；②歌切封面文案按权威规则不带"豆沙歌"系列标记（记忆 cpa-real-ai-cover-always），非缺陷。

## 2026-07-05：李豆沙 7/5 直播 → 5 条上传-ready 候选切片（本地 no-upload）

**目标**：Ivan 要 7/5 直播上传-ready 候选切片到本地审；主控读全部候选池挑题，subagent 做出片体力活。

**已完成**：
- **5 条成品在 `lidousha/2026-07-05/`**（+ `OPEN_ME.md` + `index.html` 可视化过片）：A 数学讲成兄弟情虐恋(4:20，全场语义分最高)、B 以为小猪结果熊猫(0:31)、C 有没有李豆沙/缩成小点(0:54)、D 联动游戏名/聋子(1:07)、E 一本正经讲丧尸偶像/血鬼舞台(0:54)。每条 video+准字幕(cos/sin/OBS/VIVINOS等专名全准)+围绕李豆沙自动标题+CPA真图 gpt-image-2 封面。**5/5 verify PASS**（无词表泄漏、字幕行合规、含音视频轨、topic-closure 落完整句）。**未上传**。
- 选题：主控读 6 段生产候选池(98候选/57 talk)+jingting字幕+弹幕，观众视角+围绕李豆沙+题材多样挑 5；出片 subagent 各跑一条 `produce_slice_package.py --substrate aggregate_asr --correct cpa`（Ivan 2026-07-04 定的字幕架构）。
- **两个坑修好并入记忆**：(1) spec `remote_media` 必须 free **宿主** 123云盘 路径(`/root/clouddrive2/CloudNAS/CloudDrive/123云盘/live-streaming/...`)，**不是容器 `/app/Videos`**（脚本宿主侧 ffmpeg 无 docker exec）；FUSE 挂载~36s/小文件极慢；semantic_start/end 对准真实语音起止读 `padded.fresh.srt`，候选 start 含前置铺垫会 fail-close。记忆 `produce-slice-package-host-path-and-slow-mount`。(2) 封面字体 `ZCOOLKuaiLe` 把「自」渲成「白」形（自私→白私），A/D 封面钩子临时改写绕开(自私→还小气、自认→成了)，B站标题保留原字。记忆 `cover-font-zi-renders-as-bai`。
- B/E 初版边界飘（B 拖进无关banter、E overshoot climax），已收紧 semantic_end 重跑（缓存切复用，快）。

**进行中**：无（本节收尾已由 Fable 会话完成 2026-07-06 05:3xZ，Opus 会话勿重复执行）：
- **A(cos/sin) 已发布** BV1QzTS65Enf：单次 biliup rc=0、入小李切片(code 0)、封面换「自私」修正版（**ZCOOL 版实为白私字形**——放大核验过；修正版=得意黑整张，`_cover_evidence/*.zisi-smiley.png`；换封面触发复审后已回 state=0，公开 pic=b73d7446 新版 ✓）。证据 cossin.uploaded.json + cossin.public_verify.json，已 commit。
- **D 重做 ✓** 收束「这就是本周直播直播安排」（试了 3 个目标位：92k→"然后就这样"、95k→蹿到下话题"露总"且自动标题带双自字，最终 92k+钉标题「还没联动，小李先把盲聋哑念乱了」命中理想句）。
- **H 重做 ✓** 收束「也没有阻止这个展开啊」（完整句收题，避开谢SC杂段；比塞到 02:08 更干净）。
- **F2 真由来 ✓** 新增 `小丑鼻子的真正由来_猪鼻不见了.mp4`：19:30 段 06:35–09:13 = 猪猪侠盲盒→VTS 猪鼻不见了→借陆医生小丑鼻代替→"先用这个代替一下吧"收。原 F(生日/活动小丑)保留为续集。全段 BCUT 存 `pull/seg1930.full.bcut.srt`。
- **标题新规**：「直接」全局禁用（Ivan：直呼打咩>>直接打咩）——代码硬门+词库+skill+记忆全链固化，287 tests。
- B/C/E/G 未动。OPEN_ME.md 已补 2026-07-06 节。

**阻塞**：无。上传永远需 Ivan 逐条授权。

**下一步**：
1. **Ivan 审 `lidousha/2026-07-05/`**（浏览器开 `index.html` 过片）。
2. **封面字体 `自→白` 治本**（通病应入代码，勿单例补丁）：给 `scripts/run_auto_review_shadow_pipeline.py` 的 `_overlay_lidousha_cover_title` 逐字渲染加「自」→回退字体映射（现有"缺字回退"对字形错的「自」不触发），或整体换 `自` 正确的快乐体系字体（仍 fail-closed）。
3. 可选第二批：日语歌《男も女も恋してるべき》(20:30 段，走完整曲 LRC 全局位移 lane) + 备选 talk（米老鼠版权/被乌鸦俯冲/百合是工作/回不了家/木马/人设太帅，见 OPEN_ME）。
4. 未提交改动：本轮 `reports/lidousha-autoslice-20260705/`(specs+verify脚本)、`lidousha/2026-07-05/`(成品+OPEN_ME+index.html) + 之前所有未提交改动（Ivan 未让 commit）。

## 2026-07-06 runner v3（无人值守首跑复盘修复，Ivan 验收打回后重造）

首跑五宗罪→根因→修复（commit c8fbd31，292 tests）：标题全是cid（CPA gpt-5.5/5.4 provider 整段断供，静默回退）→ **CPA 健康门**：断供即 paused_cpa_down 推迟批次、恢复自动从 pending 断点续产，标题失败的成品不交付（清理重试≤3次），llm_via_cpa.sh 加 5.5→5.4 failover；字幕豆腐框（free 零 CJK 字体，ASS 指名微软雅黑）→ free 已装 fonts-noto-cjk（烧录实测字形正常），runner 启动自检；摘要不知所云→重写（每条：标题/选片理由hook/信心分/收束句/边界/封面；歌：弹幕数/门判定/原因码；含落选清单+死段清单）；唱4+首只出1首→召回帽3→4+确定性演唱检测补充歌队列，最终按弹幕 top2；2.9KB 录制残桩每10分钟无限重试→桩过滤+BCUT失败2次入死段账本。交付文件名改用 hook 可读名。**7/6 产物已全域清空**（free 工作区/交付/缓存/state+本地拉回件），CPA 恢复后 cron 自动全新重跑（门控已实战验证："CPA chat lane down — batch deferred"）。

## 2026-07-06 第二轮收尾（Fable 接管续）

- **已发布 +2**（均单次 biliup+入小李切片+公开验证+证据 commit）：A cos/sin **BV1QzTS65Enf**（自私修正版封面）；D 联动 **BV1uCTS6kEuf**（Ivan 定稿标题「过期（？）女大终于迎来再次联动」，礼墨/安晚awa 词表闭环重出）。
- **staged 不上传**：百合长片 `yuri_full_arc_2000`（10:56 三段拼接，标题带《我在意的人不是男生》，百乃工/百破图已修，封面大字钩子）；F2 小丑由来（「猪鼻不见了，小李只好借小丑鼻顶上」）。旧 G/H/生日F/Opus重复由来片 → `_superseded/`。
- **规则升级**：标题禁词扩到 直接/当场/秒X（代码硬门+词库按 Ivan 真实标题重建+示例清洗）；作品讨论类标题必须带《作品名》；对话提炼的工作流偏好固化进 publish SKILL「Ivan 工作流偏好」节 + 记忆 ivan-implicit-workflow-preferences。
- **修的 bug**：llm_client 命令超时未包成 LlmCallError 会炸 produce（11 分钟长片踩到）→ 已修+测试；校正 CPA 超时 180→600s。288 tests。
- **待 Ivan**：①「牡丹原作者/百牡丹」正确写法（未确证未入词表）；②已上线 A 标题含「当场共鸣」（禁词规则前上传的），要不要热改。

## 目标（2026-07-04）

无人值守/半自动的李豆沙直播切片流水线。本轮核心：Ivan 审 7/3 竖屏直播切片后给出多类字幕/标题/构图反馈，
要求**把每类反馈当成流水线通病在代码里修掉、并固化进单一权威**（绝不做单例修补）。
关键洞察（Ivan）：**每个 prompt 都只带了本轮反馈的几条规则，没带项目历史积累的全部原则——prompt 不够强，且这个病可能在多处**。
7/3 六条成品在 `lidousha/2026-07-03/` 待审。

## 已完成（2026-07-04 本轮）

1. **字幕校正原则单一权威化（治本）**：新建 `assets/lidousha/subtitle_correction_principles.md` = 字幕文本校正唯一权威，
   收敛了此前**只活在代码提示词里**的操作规则（幻听置空删除、SC价格忽略、花体字会误读别盖词表、口误保留、咳嗽删语气词留、
   时间配对±10s、歌切文本以LRC为权威、人工订正即真值、自称误听/kmx 元规则）。
   改 `gemini_slice_jingting.glossary()` → 返回 `术语表+全量原则`，**所有**校正提示词一处改动全局同步；
   CPA 校正/裁决删掉重复硬编码换指针；`glossary.txt` 瘦身为纯术语表；`sync_lidousha_assets.sh` 增推 principles。
2. **BCUT×AGY×CPA 裁决框架修正**（Ivan：BCUT 很准只专名弱，AGY 只补专名/同音字，不采纳 AGY 对普通措辞的改写）。
3. **P0 标题（subagent T）**：`title_style.md` 回灌全量权威；LLM 自动标题强制 12–30字/【李豆沙】前缀/7违禁词命中有界重试；
   **人工标题铁律直通**。**离谱**改两用词（只禁"X到离谱"后缀，留"越看越离谱"）。**P2 封面**注入 persona 身份。
4. **P1 术语门（subagent Q）**：新建 `scripts/lidousha_glossary_terms.py` 动态解析 30 canon+34 误听黑名单注入 judge，
   `terminology_ok` 不再空判（此前只喂 kmx 一词）。
5. **竖屏→横屏 pillarbox**：`_burn_preview_subtitles` 检测竖屏，模糊侧填充到 1920×1080。
6. **词表学习闭环验证（§十三）**：哄睡妈妈 kmx"给我总弄上妈妈了"声学不可恢复 → 加 glossary 种子 → 重出得"kmx就叫妈妈了"。
7. **通病·篇章级代词消解**：Ivan flag"他→TA（同学性别未知）"通用校正做不到（规则被长prompt淹没）→ 加专职 `_cpa_pronoun_ta_pass`
   （模型只判 cue 编号、代码确定性替换 他/她→TA、带 其他/他们 守卫、区分匿名未知 vs 具名已知如司马懿、门控+重试+fail-open）。
8. **CPA 用对 gpt-5.5（Ivan 纠正）**：根因——gpt-5.x 是原生 **Responses-API 推理模型**，我一直错用 `/chat/completions`（gpt-5.5 因此 503、gpt-5.4 空 content）。
   改 `scripts/llm_via_cpa.sh` 走 `POST /responses` + `reasoning.effort=medium` + max_output_tokens 16000 + 5 次重试（上游间歇返回空 reasoning-only 响应）。
   实测长prompt 8/8 稳定。一处改动，所有 CPA 任务（字幕裁决/代词/标题/艺术指导）升 gpt-5.5(medium)。硬约束：只走 CPA、不直连 sudocode、不动 CPA 服务。见记忆 `cpa-gpt5-responses-api`。
9. **测试**：全量 `pytest tests/` → 282 passed（含下条封面改版 +5 用例）。
10. **封面改版（通病·点击率，另一会话主线，真 gpt-image-2 验证）**：旧"居中人物+单色标题横条+大留白"千篇一律 → 改成
   **大头胸像怼一侧 + 半屏多彩艺术字 + 波普/场景背景填满**，按切片轮换布局、表情贴角色、外观随切片真实皮肤。全落
   `scripts/run_auto_review_shadow_pipeline.py`：新 `_lidousha_cover_art_direction`+`LidoushaCoverArtDirection`（确定性基线
   `sha256(candidate_id)` 轮换 layout/hook + 关键词角色→表情映射，可选 CPA judge 精修 **fail-open+护栏**，护栏丢弃吐舌/性感/挑衅）；
   改写 `_lidousha_cover_prompt`（四布局 left/right-split/banner/song-clean + 表情 + 背景池谈话忙歌净 + **外观随参考帧不锁服装** +
   **永不吐舌**，保留 panda/小李/熊猫/16:9）；改写 `_overlay_lidousha_cover_title`（**只靠粗描边**托白字=参考图不加盒子(深色卡已停用=难看方框)、**按段选描边**修白字糊、
   多色钩子词高亮色池轮换不含cream、分区自适应字号，ZCOOLKuaiLe fail-closed 不动）。新 CLI `--cover-art-direction-llm-command`，
   `produce_slice_package.py`/`run_full_session_selector_cpa_shadow.py` 已接线（art-direction LLM 复用 `llm_via_cpa.sh`，即走 gpt-5.5/responses）。
   契约守住：真 gpt-image-2 无字背景 fail-closed 禁抽帧，`cover_generation` 的 `model/method/fallback_used/ai_background`+`cover_status` 键不动。
   同步 `persona.md`(加"封面表情/角色映射"节)+`bilive-autoslice-publish/SKILL.md` 封面节+两 docs。验证 `lidousha/2026-07-04/_封面改版评审/round3/`
   4 张真图：**3 套不同皮肤**(0702蓝棉衣/0703 JK/0629熊猫兜帽演出裙)各随其参考帧=外观随切片证据，表情贴角色无吐舌，四布局+繁简背景对比。
   记忆见 `lidousha-cover-redesign-halfbody`、`cpa-real-ai-cover-always`（含 CPA 图像模型 gpt-image-2↔gpt-image-1.5 会掉线的实测）。

**7/3 交付（`lidousha/2026-07-03/`，全部 1920×1080）**：哄睡妈妈(kmx✅/贡丸删✅/虫儿飞留✅ Ivan确认真实回应/再睡✅)、
称呼大战(倒反天罡✅/kmx✅/SC谢✅ + 定稿标题"叛逆小李一定要喊kmx妈妈，kmx只好喊宝宝")、熊猫伪装、奶龙斗虫、直播腔(写非说✅/TA)、
虫儿飞歌(切片不动，标题→"温柔《虫儿飞》清唱甜美哄睡"，封面已换字)。

## 已发布（2026-07-05，Ivan 逐条授权，biliup 各只跑一次，全部 state=0 + 入合集 + season_display=True）

- 称呼大战 **BV1tdMT64E47** → 小李切片
- 虫儿飞 **BV1CWMT6EE6c** → 小李歌唱（豆沙歌，round3 温柔清唱封面 + pillarbox 横屏）
- 哄睡妈妈 **BV1yWMT6EEHF** → 小李切片
- 直播腔 **BV1yWMT6EEWD** → 小李切片
封面均用最新流程 `scripts/regen_covers_latest_flow.py`（只重出封面不动字幕/标题）。证据存各 `<cid>.uploaded.json`。
上传经 `free:/opt/bilive/app/tmp_manual_upload/`（**不是** `upload.sh` 那个会投稿删文件的守护进程）。
合集 API 偶发 20111「编辑过于频繁」→ 隔 15s 重试；新稿 state=-30 审核中时不能入集，等 state=0。

## 已完成（2026-07-05 Fable 复审轮：审阅 7/4–7/5 全部 Opus 会话的指令与改动）

逐条对照 5 个会话（B站字幕研究/BCUT接入/通病大修/封面改版/7/5切片）的用户指令与落地代码。
**结论：架构与决策基本合格予以保留**（单一权威原则文件、glossary() 组合加载器、三段式字幕、代词专职 pass、
llm_via_cpa.sh /responses+UA+重试、标题强制+人工直通、封面新系统+feed安全区、上传纪律），282 tests 复跑全绿。
**复审修正的问题**：① free 资产漂移（词表旧版+principles 缺失）→ 已 sync+md5 核对；② `free_asr_client.py`
AUTO_CHAIN 仍含已下线的 kuaishou（与文档/对用户报告不符）→ 移出 auto 链（实现保留可显式调用）；③ skill 安全区数字
漂移（写 320/1600，代码实为 260/1660）→ 修正并注明教训；④ title skill「离谱」缺两用词标注 → 补齐；⑤ 删死代码
`_wrap_cover_title_lines`（旧冒号时代包装器）+ 修 `_overlay_lidousha_cover_title` 过期 docstring（还写着深色字卡）；
⑥ `bili_replace_covers.py` 从会话临时目录入库到 `scripts/`（换封面是复发需求，工具不能只活在 /tmp）。
**留档不改的历史事实**：充电器曾重复投稿（BV1WEMA6TES6，Ivan 已删）；诊断期一次直连 sudocode 上游+key 进过命令行
（已被 Ivan 立规禁止，repo 无残留，llm_via_cpa.sh 有 NEVER bypass 注释）；7/5 旗舰用了 `--correct cpa` 而非默认三段式（续跑时纠正）。

## 进行中

- **[Claude 封面会话·已完成 2026-07-05]** 全账号(mid 55006782)**24 条李豆沙切片封面全部按新流程重做并已替换上线**(只换封面,标题/标签/合集未动;24/24 `COVER_UPDATED=True`)。含大字自适应(多换行+均衡分行)、feed 4:3 安全区(文字 x260–1660)、缺字整张换字体(镚→得意黑)、去方框/去 baked-text/永不吐舌/外观随切片(老切片无本地源→用其当前线上封面当参考帧)。换封面脚本已入库 `scripts/bili_replace_covers.py`(在 free 上跑,inspect→apply 两段,只改封面) + `scripts/regenerate_lidousha_cover.py`(出图);账号里少前2游戏视频非切片未动;规则固化进 `bilive-autoslice-publish/SKILL.md` 封面节 + 记忆 `lidousha-cover-redesign-halfbody`。**遗留**:含长英文串(shadowlee)/超长标题的封面字号有物理下限,要更大需把封面文案缩成钩子短句(待 Ivan 定)。
- ~~7/5 直播切片轮·被会话切换中断~~ **已被 Opus 会话续跑完成**（见顶部「2026-07-05」节：5/5 交付 verify PASS）。遗留提醒仍有效：该批字幕用的 `--correct cpa` 非默认三段式 `bcut_agy_cpa`（Ivan 定的架构是三段式；上传前如对专名有疑虑可按默认重出）；封面「自→白」字形坑待治本（见该节下一步2）。
- **[无人值守自动切片 runner·已部署 2026-07-06 (Fable)]** Ivan 目标落地：**直播结束后 free 自动跑完整产出链，无需人开口**（上传仍关）。`scripts/free_session_autoslice.py` 部署在 `free:/opt/bilive/autoslice/`（repo 副本+cpa.env(600)+state/cache/logs/reports），**cron 每 10 分钟** flock 单飞 tick：blrec API(带RECORD_KEY) 判在播→在播/API失联一律跳过（fail-safe）；下播→逐新段 BCUT 转写→CPA 语义召回(带弹幕突发hints，LLM挂了退确定性兜底不静默)→**谈话 top5**（跨段轮转）逐条 `produce_slice_package --ssh-host localhost`（默认三段式 bcut_agy_cpa+真图封面）→**歌切每场至多2个、按窗口弹幕量最高排序**（Ivan 2026-07-05），走 `run_full_session_selector_cpa_shadow` LRC lane（anchor±180/150s 窗，完整性门 fail-closed，只交付 AUTO_UPLOAD 判定的）→交付 `repo/lidousha/<date>/`+`AUTOSLICE_SUMMARY.md`+报告文件。**杀开关** `touch /opt/bilive/autoslice/DISABLED`；7/1–7/5 已预认领（manual）不会被重跑；失败不自动重试（防 retry storm）。Mac 端 launchd `com.ivan.lidousha-autoslice-pull` 每 30 分钟 rsync 交付+报告回本地 `lidousha/`+`reports/slice_monitor/autoslice_free/`。冒烟：talk 全链 rc=0（cut→BCUT→AGY(ssh localhost)→CPA→烧录→真图封面→交付）✅、语义召回 lane ✅（选出的候选与 Opus 人工选题重合）、**歌切全链 ✅**（虫儿飞靶：窗口判 song、netease LRC 匹配 92.3%、门判 AUTO_UPLOAD、标题"温柔《虫儿飞》哄你睡觉"、成品+封面自动交付；Mac 睡眠期间 free cron 独立跳 17 tick=自主性证明）。歌切两坑已修：①窗口余量必须小（anchor−15s/+20s，宽了窗内召回会改判 talk）；②**QA 判官曾以"翻唱版权"BLOCK 合格歌切（0.74 分）——已校准口径**：翻唱是频道常规内容（24 条上传里 7 条豆沙歌），unsafe 维度只管隐私/引战/平台违规/第三方画面（`cpa_semantic_qa_llm.py` + 测试）。**选片 metric 已资产化**：`assets/lidousha/slice_selection_metric.md`（Opus 会话 v2 校准 + 24 条已上传标题提取的偏好 + 熊猫伪装/奶龙拒发反例）注入语义召回 prompt（回退链），sync 推 free，286 tests。为此 free 配置了 root 自我 ssh(ed25519)。记忆 `free-unattended-autoslice-runner`。
- 无本会话后台进程（free 上只有常驻 cron runner + 既有 jingting daemon）。

## 阻塞

- 无硬阻塞。上传永远需要 Ivan 逐条授权。
- 重复投稿 BV1WEMA6TES6（充电器）需 Ivan 在创作中心手动删（API 删档要验证码，无法 headless）。

## 下一步

1. **熊猫伪装 / 奶龙斗虫：Ivan 决定不发**（2026-07-05）。7/3 已发 4 条（称呼大战/虫儿飞/哄睡妈妈/直播腔），本场收工。
2. ~~sync 推 free~~ **已完成（2026-07-05 Fable 复审时执行）**：复审发现 free 上词表停在 7/4 19:43 旧版（缺 kmx 主语误听种子）、`lidousha_subtitle_principles.md` 从未推上去——已跑 `sync_lidousha_assets.sh`，三个文件 md5 与本地一致。教训：以后跑 sync 别 `>/dev/null 2>&1` 吞错，跑完 md5 核对。
3. **7/5 直播切片续跑**（见「进行中」，另一 Opus 会话在跑，别撞车）：A 重出封面+按默认 `bcut_agy_cpa` 复核字幕，B/C/D/E 按 spec 产出，全部拉到 `lidousha/2026-07-05/` 给 Ivan 审。上传永远逐条授权。**"仙童数学"已核实**（2026-07-05 Fable）：BCUT 声学层与 free 侧 ASR 两路独立听到 xiāntóng shùxué，上下文=她模仿数学UP收尾口播的自称，Ivan 确认写法"仙童数学"→已入 glossary（黑名单：先童/神童/线童数学）并 sync free，成品字幕现状即正确。
4. **封面改版验收**：Ivan 看 `lidousha/2026-07-04/_封面改版评审/round3/` 4 张 → 定稿后决定 commit。persona.md 目前只在 repo（sync 脚本不推它，free 端封面不消费它，无需推）。
   可选小调：`produce_slice_package.py` 人工标题时不跑 LLM 艺术指导（走确定性基线，仍贴角色）；要人工标题也精修就把 `art_direction_llm` 提出 `if not given_title`（一行）。
5. **commit 规矩（Ivan 2026-07-05）：授权上传的内容必须 commit**——已执行，commit `7dcfdfa`（84 files：全部管线代码/skill/资产 + 9 份 `*.uploaded.json` 上传证据含追溯补记的充电器 + 24 张换封面状态证据；`.gitignore` 已加 `!reports/**/*.uploaded.json` 例外；媒体不入库，hash 在证据里）。以后每次授权上传后：写 uploaded.json → commit。见记忆 `authorized-upload-must-commit`。
