# tag 建议 — 审查记录（Ivan 2026-07-13 拍板，口径已固化进脚本）

## 执行结果（2026-07-13 第二轮后）

- **10 条已全部补 tag 并读回验证**（TAG_UPDATED 全绿、标题/封面/简介原样）：`tag_update_results_10.json`（计划=`tag_plan_10.json`，工具=`scripts/bili_update_tags.py`，free 上跑）。
- **tag 上限实测 = 12**：BV1EQNk6KErE 一次性提交 12 个 tag，edit code=0 且读回 12 个（`MAX_TAGS_DEFAULT` 已改 12）。
- 二轮新增硬毙词：**暴力女**。
- 10 条中 7 条线上标题已被 Ivan 手工改过（title_expect 守卫如实报漂移后按线上现值重新锁定）；BVID 映射以上传账本为权威。
- 10 号的 `椎名立希` 因弹幕误锚按暂缓处理未上；01 号的 `椎名立希/高松灯/要乐奈` 有真实台词依据已上。

原型: `scripts/suggest_upload_tags.py`（两层: 确定性专名规则 + CPA LLM 内容词; 专名绝不让 LLM 产出）。
本目录: `tag_suggestions.md`(逐条建议+证据) + `tag_suggestions.json`(原始输出)。
对象: 2026-07-13 权宜上传的 10 条 (`reports/lidousha-backlog-20260713-publish/`)。

## Ivan 拍板（2026-07-13，第一轮审查反馈）

1. **基础位砍成 4 个**：李豆沙/虚拟主播/虚拟UP主/直播切片（VUP/VTuber 砍掉）→ 6 个内容位。已是脚本默认。
2. **邦多利族 4 tag 全保留**（梦限大/夢限大みゅーたいぷ/BanG Dream/邦多利）：无所谓，不裁。
3. **专名只出正主名**：南町（大N老师/豆町只作触发面，不出梗形态 tag）。
4. **半梗半内容词放行**：坏女人、宿敌恋人。
5. **内容词硬标准（核心纠偏）**：必须「既贴合内容，又足够通用、观众真的会搜」。
   硬毙（过专一没人搜）：彩排、宠粉、玩梗、热情邀约、初次登场、脑补剧情、粉丝互动、线下合照。
   已进脚本 `_BANNED_CONTENT_TAGS` + LLM 提示词正反例。
6. **tag 必须按最终成品字幕出**。字幕待修但事实已裁定 → batch 条目 `suppress_tags`/`add_tags` 人工通道，换源后去裁定重算。
   实案 **03**：安晚未出场，全片「安晚」=大N老师误听 → 压掉 `安晚awa`、补 `南町`（裁定已写入 batch 与输出）。

## 全量回填（2026-07-13，Ivan 授权"把之前发的所有李豆沙切片都加上tag"）

- 范围：账号 304 稿中标题前缀【李豆沙】共 53 条 = 已补 10 + talk 35 + 歌切 8。**43 条全部 UPDATED 且读回验证全绿**（`backfill/tag_backfill_results.json`）。
- 内容来源分层：7/9 批 10 条有 recut.srt（完整两层）；7/2–7/6 早期 25 条字幕存档已不存在（成品字幕只烧在视频里）→ **title-only 模式**（专名扫标题 + LLM 仅凭标题宁缺毋滥）。
- 歌切 8 条走确定性词面：`歌名(《》内verbatim) + 翻唱 + 歌回 + 唱歌 (+清唱/哄睡按标题)`，见 `backfill/plan_backfill_songs.json`。
  - 待 Ivan 顺眼：BV1AN7u6kEHB（偶像宣言/被可爱击中）标题无《》没出歌名；BV1MgMt6UEGp 歌名按标题原样出「屑屑」（谐音写法）。
- 本地按"通用可搜"口径二次裁掉的 LLM 词（全记录 `backfill/backfill_curation_log.json`）：鸡同鸭讲、大喊大叫、手忙脚乱、语言歧义、误会、学历、动物、安全科普、性取向、年龄歧视、厨房事故、哭哭。
- 人工补：BV1qxMc6XEM9（反沙）标题太隐晦 LLM 零输出 → 按 persona 谐音翻车类补 `谐音梗,绕口令`。
- 7/9 批字幕里的 `安晚awa` 已核实为拉丁原形+在场证据（"awa也在点头"），非 03 式误听。

## 逐条残留判读要点

- **06** BV1JQNk6KEkh: 源字幕/封面「直女」待修; 规则已按铁律将「直女」当「侄女」触发, tag 不受待修影响。
  「大舞台」字幕内无 BW 字样, 自动层不猜; 若 Ivan 确认=BW 现场可人工补。
- **08** BV1JQNk6KENJ: `伊索尔` 依据「想到今天142说的那个故事」, 已核实非数字误报。
- **10** BV1EQNk6KErE: 「立希不是算妈妈吗」是已记录弹幕误锚 → `椎名立希` 暂缓; 其余邦多利族 tag 有独立证据
  （梦限大×3 含跨行截断连体匹配、Mujica、Popipa、MyGO、邦多利、武士道）。
- **05** BV1zQNk6KEUS: 字幕「萤火虫的live」→ `萤火虫漫展`（新专名规则, 命名口径 Ivan 未点名反对, 暂用）。

## 接入状态（2026-07-13 全部完成）

- 上传通道：`make-manifest --tags` 冻结 → `do_upload.sh $4`（无 tags 回退基础4位）。✔ 已部署
- **全自动生成（Ivan 指示）**：`produce_slice_package` 产包时按成品标题+成品字幕生成 `upload_tags` 进 record.json
  （fail-safe: OK/OK_NO_LLM/FAILED, 绝不阻塞交付）；`make-manifest` 无 --tags 自动拾取视频旁 `<stem>.record.json`
  （--tags 覆盖、--no-tags 关闭、坏 sidecar 响亮拒绝, tags_source 入 manifest）。✔
- `apply_subtitle_correction` 修字幕后自动重算 record 的 upload_tags；已发布稿件 B 站侧同步用 `bili_update_tags.py`。✔
- 已传 10 条 + 历史 43 条全部补完（见上文执行结果/全量回填）。✔
- 歌切自动词面目前=标题《》歌名+翻唱/歌回/唱歌（回填口径）；产包侧歌切走同一 generate_upload_tags（歌词字幕会让
  LLM 层宁缺毋滥, 专名层照扫）——如需歌切专用词面再扩。
