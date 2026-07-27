# 6候选终审拦截晨间裁定清单 — 2026-07-26

来源：`ssh free` 读取 `/opt/bilive/autoslice/out/<date>/<cid>/<cid>.chat-authority.json`（`final_review_audit`/`foreign_script_consistency_audit`/`title_mark_balance_audit`）+ 对应 `entity_verdicts/<request_sha256前20位>/verdict*.json` + srt。只读，未改任何文件。

**cue 序号=审计内部 `cue_index`**，可能与当前 `padded.fresh.srt` 行号有偏移（candidate A 实测偏移+5），定位以本表"现文本"原文为准，不要按数字去数行。

## 根因（影响全部4个 FINAL_REVIEW_UNRESOLVED_FINDINGS 候选，20/20 findings 命中）

`src/autoslice/entity_audio_verifier.py:736-738`：

```python
and not isinstance(syllable_count, bool)
and isinstance(syllable_count, int)
and syllable_count == len(tokens)
```

酸性证人（AGY/Gemini 链）返回 `status:"OBSERVED"` + `heard_pinyin` + 自报的冗余字段 `syllable_count` 后，校验要求 `syllable_count == len(heard_pinyin.split())` 严格相等，不等就整份判 `WITNESS_REPORT_INVALID`→`UNCERTAIN`，即使实际转写清晰、置信度 0.95。抽样：本次4个 FINAL_REVIEW 候选的全部20条 finding，`witness_raw.status` 无一例外是 `OBSERVED`，全部只因这条自洽性校验被下游判 `UNCERTAIN`。**这是确认的代码缺陷，不是证据缺失**——已用 `spawn_task` 另开一票，不在本报告内修。

第二个根因（影响2个 FOREIGN 候选）：`src/autoslice/foreign_span_witness.py:35` `_MIN_VERBATIM_SIMILARITY=0.60`，逐字相似度门槛对"整句外语"和"中文句中嵌入2-3个拉丁字符的用户名/缩写"一视同仁，后者天然测不出高相似度。

---

## A. 2026-07-24 auto_183122_1209_1410 — FINAL_REVIEW_UNRESOLVED_FINDINGS

hook：听说安晚也吃了生豆角，李豆沙震惊之余决定下播暗示礼墨也吃，誓要用集体中招维护三人组的"团结默契"。`failure_recoverable: false`

| cue | 现文本 | 提案 | witness拼音(conf) | judge | 分类 |
|---|---|---|---|---|---|
| 11 | 切，那就差林墨没吃了。 | 切，那就差礼墨没吃了。 | na jiu cha ling dian ling mei chi le (0.95) | UNCERTAIN(原始OBS) | 门缺陷 — 下一句(cue17)原文已是"礼墨"，证据充分 |
| 30 | 可能是我当时焯水的时候想要咳咳 | …想要（删"咳咳"） | ken neng shi wo dang shi chao shui de shi hou (0.95，与目标句完全吻合，无"咳咳") | UNCERTAIN(原始OBS) | 门缺陷 — 拼音干净确认删除 |
| 35 | 谢谢桃林的钢镚。 | 谢谢唐琳韵的钢镚。 | shi tang lin de gan gan man gan dao duo shao you dian (0.70，背景噪音，不确定位[2-5]) | UNCERTAIN(原始OBS) | 耳裁 — 证据本身弱，噪声大 |
| 42 | 嗯，机甲也是一样的。 | 嗯，机架也是一样的。 | jia ye shi yi yang de (0.95) | UNCERTAIN(原始OBS) | 门缺陷 — 拼音吻合，语境支持"架" |

## B. 2026-07-25 auto_192000_371_669 — FINAL_REVIEW_UNRESOLVED_FINDINGS

hook：翻粉丝来信时把"浣溪沙"看成"深呼吸"，李豆沙一路嘴硬纠错，最后反把自己锤成了最大的CP粉头。`failure_recoverable: false`

| cue | 现文本 | 提案 | witness拼音(conf) | judge | 分类 |
|---|---|---|---|---|---|
| 3 | …筐端的一下 | …筐端了一下 | (含前后文)…na le ge kuang kuang de yi xia…(0.95，目标位置听感偏"的") | UNCERTAIN(OBS) | 耳裁 — 拼音未清晰支持"了" |
| 8 | 有，有一小边框 | 有，有一小半筐 | you xiao ban kuang dai gei gei (0.95，"ban kuang"清晰对应"半筐") | UNCERTAIN(OBS) | 门缺陷 — 拼音支持提案 |
| 22 | …一堆梦限大无料理解 | …无料（删"理解"） | …na nei li jie yao ma…(0.90，不确定位[11]) | UNCERTAIN(OBS) | 门缺陷（置信中等，倾向支持删除） |
| 38 | "我永远是深呼吸名啊" | "…深呼吸民啊" | …song hu xin **ming** a…(0.95，清晰读作"ming"=名) | UNCERTAIN(OBS) | 耳裁★ — 证据冲突：witness读"名"不是"民"，与"XX民"规律矛盾，建议直接听音频 |
| 52 | …笙哥的某对CP吧 | …笙歌… | …ken neng shi seng ge de mou ge **cpa**(0.85，尾字混入"cpa"，疑似被候选文本锚定) | UNCERTAIN(OBS) | 耳裁 — 疑似锚定污染+置信中等 |
| 57 | "我一直是浣溪沙名啊" | "…民啊" | …wo yi zhi shi huan xing sha **mi**(0.95，尾音无鼻音韵尾，名/民都不完全对) | UNCERTAIN(OBS) | 耳裁 — 名/民争议 |
| 59 | 原来李豆沙自己是深呼吸名 | …民 | "yuan lai li tou shi"开头重复两次，句子在到达目标字前被截断(0.95) | UNCERTAIN(OBS) | 耳裁 — witness复读/截断，证据不可用，按上下文"民"更合理但需耳核 |
| 69 | TATA是，TA是浣溪沙弥 | 收缩为"TA" | ta ta shuo ta ta shuo huan shi sha mi…(0.95，确认口吃重复，"沙弥"非"沙民") | UNCERTAIN(OBS) | 耳裁 — 结构性大改，策略已因 `LATIN_SCRIPT_REPAIR_REQUIRES_SOURCE_PROVENANCE` 拒绝自动建议，需人工定夺 |
| 76 | 我跟那个生哥说的是 | …笙歌… | …seng ge shuo de shi xin xing…(0.85，witness自评"dialectal variation in seng vs seng/seng") | UNCERTAIN(OBS) | 耳裁 — witness自陈方言歧义 |
| 80 | 然后那个生哥说的是 | …笙歌… | ran hou na ge **xuan** ge shuo de shi…(0.95，"xuan"明显偏离"sheng") | UNCERTAIN(OBS) | 耳裁 — 拼音明显漂移，与两候选都不匹配 |

## C. 2026-07-25 auto_195000_1493_1579 — FINAL_REVIEW_UNRESOLVED_FINDINGS

hook：围观两支粉丝队交战时，李豆沙让大家"发1支持对面、发0支持我"，说完自己都觉得太惨，赶紧把支持码改成2。`failure_recoverable: false`

| cue | 现文本 | 提案 | witness拼音(conf) | judge | 分类 |
|---|---|---|---|---|---|
| 13 | 那个发一支持下斗里的队伍 | …沙豆李的队伍 | fa yi zhi chi xia de li de dui wu fa ling zhi chi li de xia de dui wu(0.85，不确定位[10-12]，witness自陈口误/语序被打乱) | UNCERTAIN(OBS) | 耳裁 — 语速快+口误，证据本身弱 |
| 27 | 爱你爱你大熊猫已赢 | 暗影暗影大熊猫已赢 | ai ai zai shen me yi(0.95，与"爱你爱你""暗影暗影"均不完全对应) | UNCERTAIN(OBS) | 耳裁 — 拼音与两个候选都对不上，建议直接听 |

## D. 2026-07-25 auto_195000_911_1030 — FINAL_REVIEW_UNRESOLVED_FINDINGS

hook：粉丝顶着"李豆沙"队名一路碾压，主播先夸大家太强，随后委屈控诉"强者只和强者组队，不跟李豆沙组队"，看完阵容又惊呼女同太多。`failure_recoverable: false`（此候选 `selected_repair: false`）

| cue | 现文本 | 提案 | witness拼音(conf) | judge | 分类 |
|---|---|---|---|---|---|
| 40 | 求神颜值对胜利的渴望 | kmx眼里只有对胜利的渴望 | tiao zhan zhe de sheng li de ke wang dui(0.95，读作"挑战者的胜利的渴望对"，与current/提案均不同) | UNCERTAIN(OBS) | 耳裁 — witness给出第三种读法，current和提案可能都不对 |
| 42 | 慕强批评的，慕强批 | 慕强批，真的，慕强批 | mou zhong xue li chu shi zhe yang de mu qian pi e gei wo de mu qian pi e(0.95，未清晰确认插入"真的") | UNCERTAIN(OBS) | 耳裁 |
| 44 | 没有说女童的幕墙逼的意思 | 没有说女同的慕强批的意思 | mei you shuo nv tong dou mu qian bi de yi si…(0.95，"同"+"慕强批"轮廓吻合提案) | UNCERTAIN(OBS) | 门缺陷 — 拼音支持提案 |
| 46 | 说里面女同太多了 | 里面女同太多了（删"说"） | xiang shuo yi ju jin tian san wei nv sang tai duo le(0.95，读作"想说一句今天三位女嗓太多了"，与current/提案均不符) | UNCERTAIN(OBS) | 耳裁 — witness明显跑偏，疑似区间覆盖错位 |

---

## E. 2026-07-25 auto_195000_742_887 — FOREIGN_SOURCE_TRANSCRIPTION_REQUIRED

hook：刚唱完《快乐星猫》就发现自己已经被光速淘汰，李豆沙从不敢相信到连声道歉，最后苦笑"我以为我是快乐星猫呢"。`failure_recoverable: false`

触发 span：cue45（源时间轴144760-148280ms）**"Say you, say father，哈"**，`latin_words: [say, you, say, father]`。
witness：`audible_language: en`，`witnessed: false`，`text_similarity: 0.4667`（低于0.60阈值）。
下一句 cue46："这是谁发的呀"（紧接着追问"谁发的"）。
同一 correction pass 已自行提出假设：suspect="say you, say father，" → why="无英语对话来源且整句失义，say you/say father 分别与「谁又/谁发的」音近，下一句也继续追问是谁发的"，但因 `EDIT_LENGTH_DELTA_TOO_LARGE` 被自动拒绝改写。

**判断**：全场唯一一处"英语"，且与下一句主动追问高度呼应，误判为中文"谁又/谁发的"的可能性不低；但 witness 自身也判"en"+相似度不足，机器证据不 determinative。

**分类：需Ivan耳裁**（直接听144.7-148.3s，确认是否真讲英文）。

次要待办（非阻塞原因，尚未过witness）：cue20 官服→光速，cue21 和→温柔型。

## F. 2026-07-25 auto_192000_909_1014 — FOREIGN_SOURCE_TRANSCRIPTION_REQUIRED

hook：观众想让新3D永久保留"白色奶龙"表情，李豆沙当场拒绝花钱，还坦白旧模型越看越恐怖。`failure_recoverable: false`

触发 span：cue45-46（重复两遍，源时间轴94230-96770ms）**"谢谢AC风了AC的比心" / "谢谢AC风了AC的比心"**，`latin_words: [ac, ac]`。
witness：`audible_language: zh`（！），`witnessed: false`，`text_similarity: 0.5455`。

**判断**："AC"是弹幕/礼物赠送者的英文ID缩写（直播常见用户名格式），非真实外语对白；witness 自己都判定 `audible_language: zh`（即认定整体是中文），仅因两个拉丁字符触发 `mixed_cjk_latin` 计数门，逐字相似度检查对"朗读用户名"天然打不了高分（校验设计针对整句外语，没有嵌入式ID的放行分支）。

**分类：疑似门缺陷**（`audible_language=zh` 已自我反驳"外语"假设，建议人工确认后放行，而非等更多witness重试）。

次要待办：cue4 粉团灯牌→粉丝团灯牌（漏"丝"），cue12 斯大夫→staff（近音）。

---

## G. 2026-07-25 auto_195000_48_127 — TITLE_MARK_BALANCE_REQUIRED（failed）

hook：弹幕点歌引出李豆沙的苦情歌生态学：大家听《念麻》只等一句名场面，但《嘉宾》的"封号"必须陪完整首。`failure_kind: producer_error`，**`failure_recoverable: true`**（本组唯一一个）。

触发：`title_mark_balance_audit` cue11，快照文本 **"没有嘉宾吗，》。"** — 闭合书名号"》"×1，开书名号"《"×0，`status: UNRESOLVED_COMPLEX_IMBALANCE`。

对照：同候选 cue38"来个《快乐熊吗"（单个悬空开括号）已被自动补全为"来个《快乐熊吗》"——简单情形 heuristic 能处理，cue11 的"悬空闭括号+标点错序"更复杂，没有对应的自动修复分支。

已有充分证据的修复方案（同一 correction pass 的实体发现，glossary+cue12/13/14三处重复《嘉宾》佐证）：**"没有《嘉宾》吗？"**

当前磁盘文本（`padded_38740_175290.agy_refined.srt`，写入时间晚于本轮 chat-authority.json）cue11 已经是 **"没有《嘉宾》。"**——书名号已平衡，只是问号变句号，非阻塞性问题。说明磁盘上已存在比审计快照更新的版本。

**分类：机器可重试**（系统自评 `recoverable: true`，且磁盘现状显示书名号已平衡，大概率是中间态快照滞后；重试大概率直接过。若重试后仍卡在同一 `UNRESOLVED_COMPLEX_IMBALANCE`，退化方案是人工采用"没有《嘉宾》吗？"）。

次要待办：cue25 捏吗→念麻，cue32 封→分（发分号）。

---

## 机器可重试清单

**现在可直接机器重试**：
- **G. auto_195000_48_127**（TITLE_MARK）——系统自评 `recoverable:true`，磁盘现状已平衡书名号，退化方案已就绪。

**其余6个不建议裸重跑**（`failure_recoverable:false`，且 A-D 的根因是确定性代码缺陷，同样输入重跑大概率复现同一 `WITNESS_REPORT_INVALID`）：
- 需先修 `entity_audio_verifier.py:736-738`（放宽/去掉 `syllable_count==len(tokens)` 严格相等）才能让 A-D 的 witness 重新走通；修完后 **A 的4条**证据都干净，大概率整批直接过；B/C/D 里仍有多条 witness 拼音本身跑偏或与提案矛盾（已在表中标"耳裁"），修完代码后仍需人工看这些。
- F（"AC"）建议人工快速确认后手动放行，或补一个嵌入式拉丁ID的放行分支；E 需要实际听音频判断真假外语，两者都不是"重跑就好"。

已用 `spawn_task` 另开一票追踪 `entity_audio_verifier.py` 的 `syllable_count` 校验缺陷（不在本次审计范围内修复）。

---

## H. 2026-07-27 补充：witness-reserve 跨段缺口（3 候选，设计已定、实现排期）

**候选**：7/24 `auto_190124_1571_1804`（尾 30:04）、`auto_193129_1648_1770`（尾 29:30）、
7/26 `auto_152944_1646_1804`（尾 30:04）。全部 `BOUNDARY_CONTEXT_EXHAUSTED:
BOUNDARY_SOURCE_WITNESS_RESERVE_INCOMPLETE`，未随 0a97deb 批复活（复活也会复现，属真缺口）。

**根因链**（全部代码定位，非猜测）：
1. 边界终审要求 `required_local_source_context_end_ms = 语义目标 + recommendation_forward(30s)
   + witness_reserve(15s)`（`boundary_source_context_coverage.py:58`）。
2. 可用上下文 = `sum(durations)`（候选自身 padded 片窗，`producer_text_pipeline.py:1934`）。
3. 缺口触发 `retry_scope=source_witness_reserve` 的加宽重试（`talk_lane.py:1379-1451`），但
   `source_context_reachable` 要求 `scoped_retry_end <= item["seg_dur_ms"]`——录制按约 30 分钟
   分段，贴段尾的候选把 45s 需求伸出了本段文件 → 重试被拒 → 终态。

**设计（跨段 witness reserve piece）**：piece schema 已按片携带
`remote_media/danmaku_xml_local/chat_jsonl_local/chat_origin_epoch_ms` 等源绑定字段
（`talk_filler.py:800`），结构上容纳异源片。修法 = 重试路径在
`scoped_retry_end > seg_dur_ms` 且 session 关系权威证明下一段与本段墙钟连续时，
向 spec.pieces 追加一片 `reserve` 角色的下一段头部（长度 = 缺口 + 余量），并：
- filler 审计不变量 `len(durations)==len(removals)+1` 需感知 reserve 片（排除在 removals 映射外）；
- reserve 片仅供转录/边界评审上下文；交付 recut 的语义终点仍可被推荐延入下一段
  （防直播断裂要求：分段是录制工件，不是内容边界），但必须经墙钟连续性验证；
- 跨段 chat 时基按片自带 `chat_origin_epoch_ms` 归一；
- provenance 逐片如实（异源 sha256）。

**为何不即修**：动 pieces 构造、filler 不变量、跨段 chat 时基、异源 provenance、边界推荐语义
五个子系统，属天级手术；当前三条失败 fail-closed 安全滞留，产线其余车道优先。
