# 2026-07-09《芽吹くとき》LRC 假绿修复审查

> **日期化事故证据，不是当前歌切正例或操作手册。** 当前 song/package 规则见
> [../pipeline/50-song-lane.md](../pipeline/50-song-lane.md) 与
> [../pipeline/80-package-delivery.md](../pipeline/80-package-delivery.md)。

> **SUPERSEDED / REJECTED（2026-07-10）**：本文后半段的
> `ACCEPTED_NO_UPLOAD` 只验了“歌曲录音与 LRC 完整对齐”，遗漏了
> “李豆沙本人正在唱”这一产品前提。Ivan 已确认这段是下播画面播放的
> 背景原曲，不是李豆沙演唱；因此该交付已移入 `_superseded`，最终结论
> 改为 `BLOCK / SONG_BACKGROUND_PLAYBACK_ONLY`。旧 hash/LRC 记录仅保留为
> 事故取证，不得作为正例或重新交付依据。
>
> 当前生产契约已升级为 AGY v4 + `host-vocal-proof.v2`，commit
> `f64cd29494fdc0d2b37d249e659897514fd701dc` 已正式部署；事故六文件修复已
> `COMMITTED`；真唱《屑屑》v5 已 `READY/MATERIALIZED`，同音轨静态回放已
> BLOCK。当前《芽吹くとき》fresh 重跑自动取得正确 LRCLIB 日文 LRC 并 25/25
> 对齐；本轮 AGY v4 虽误报 live，独立 `host-vocal-proof.v2` 为 0/7，最终仍准确
> `BLOCK / SONG_NOT_LIDOUSHA_SINGING` 且不产 recut。10:50Z cron smoke 返回 0，
> state/summary/upload ledger 无漂移，`DISABLED` 已移除。本文后半段的 AGY v3
> 与 historical v4 hash 是事故证据，不得全局替换或解释为当前生产契约。

## 演唱者身份漏门的纠正

- 根因：AGY/LRC 层只问每句歌词是否 audible；25/25 `heard=true` 当然也会被原唱录音满足。shadow 随后把 LRC 时长直接写成 `foreground_song_overlap_seconds` 并置 `song_complete=true`，runner 既没有“是否现场唱”硬否决，也没有主播声纹门。
- 负例证据：整段是固定的 goodbye/end-card 画面；直播告别后开始播放歌曲，结束后主播才恢复说话。旧 `+17000ms` 全局位移只证明原曲播放完整。
- 新联合硬门：
  1. AGY v4 先作同主体现场演唱否决：除 `LIVE_STREAMER_SINGING`、confidence `>=0.85`、`continuous_live_song_performance=true`、`same_lidousha_live_performer_across_all_lyrics=true`、背景录音概率 `<=0.20` 外，首尾 canonical 行必须是 `LIDOUSHA + SINGING_THIS_LYRIC`，至少七行且至少 80% 的 canonical 行必须在唱。歌曲表演中的戏剧对白只允许一个连续 `PERFORMING_THIS_LYRIC_SPOKEN` 块，且同时不超过六行、12 秒 summed voiced duration、全部 canonical voiced duration 的 20%、15 秒 wall-clock span；普通说话+BGM 不属于该例外。恰好三个头/中/尾 evidence timestamp 都必须落在演唱行。逐行与聚合仍必须是同一李豆沙现场声源、无其他歌手/和声、无预录/回放人声。原唱/背景播放、guest/duet/harmony、其他歌手、offscreen/static replay、不确定、多个或超限对白块、缺字段或 raw/report 不一致一律 BLOCK。
  2. `src/autoslice/host_vocal_proof.py` 生成 `host-vocal-proof.v2` CAM++ 身份子证明：4–8 秒 post-song 主播说话锚点对三份 pinned enroll 的中位数必须 `>=0.60`；七个互不相同的实际 `SINGING_THIS_LYRIC` cue 每个至少 2.5 秒，取中央 2.5–4 秒，对白行不可成为 checkpoint。每点同时要求 enroll 中位数 `>=0.31` 且对同会话主播锚点 `>=0.31`；至少 5/7 且头/中/尾均覆盖。v2 把 required singing role 及 subject/role/same-source/no-other/no-recorded 五项断言重新绑定到每个 checkpoint；这一层仍只能声明 `LIDOUSHA_VOCAL_PRESENT_ON_LYRIC_CHECKPOINTS`。
  3. 只有两者 AND 才可产生 `VERIFIED_LIDOUSHA_SINGING`。CAM++ 命中本身不是歌唱分类器，不能推翻 AGY 的背景播放/说话+BGM 否决。
- 验证边界：runner 复核 source/alignment/profile/model/reference/session-anchor/checkpoint 的 hash 绑定，并重算已记录分数的中位数、门槛与分布；它不重跑 CAM++ 推理，不得宣传成第二个 ML 判定或形式化身份证明。
- 语义修复：host proof 未通过时不得设置 foreground/song complete，不得套用 complete-song CPA waiver，不得出 full-song delivery；模型/声纹缺失或漂移一律 fail closed。
- 无上传：该误判从未获得上传授权，也没有上传。

## 结论

旧回填的 LRC/边界层确实被修复，但 v4 仍不能验收为歌切：它只通过了歌曲身份、完整边界、LRC 字幕和精确重渲染，没有通过演唱者身份。它现为 rejected negative golden，既不是 `review_ready`，也不是 `AUTO_UPLOAD` / publish-ready。

## 事故证据

- 旧 source-context summary：`lyrics_alignment={}`、`song_boundary={}`。
- 旧 materialized recut：`subtitle_source=asr_cues`。
- 旧源窗：段内 `151.220–341.760s`。
- 部署前的 2026-07-10 只读快照：生产指纹当时仍是 `cab9a151ccd67605d087ee0aae48c1ecf1f88830`；7/9 state 仍把该旧物料写成 `song_complete=true`。这是事故现场证据，不是本文当前部署状态。
- 公开身份/LRC：yonige《芽吹くとき》，LRCLIB id `33542202`，25 条同步歌词，首/末时间戳 `7700/199890ms`。
- **部署前旧 probe（仅事故历史，已被 v4 supersede，不得作为后续边界）**：当时估计 LRC 零点约段内 `148.5s`、首句约 `156.2s`、尾部约 `349.0s`、下一段说话约 `348.820s`。v4 当前权威绝对点是 LRC zero `138.220s`、首句 `145.920s`、末句结束 `343.220s`、post-song talk/切点 `348.220s`；一个 global shift，无 stretch。

忽略目录中的审计证据：

- `reports/2026-07-09-mebukutoki-lrc-repair/agy_probe/source.lrc`
- `reports/2026-07-09-mebukutoki-lrc-repair/agy_probe/alignment.json`
- `reports/2026-07-09-mebukutoki-lrc-repair/agy_probe/notes.txt`

SHA-256：

- LRC：`cc6a4d18146f4d5210c5439e60bd323cedf23d54978d8d9c47b95e10406516fd`
- alignment：`3a854f78a42fb90f422856f6e9573342ef3361fcbcecbf01ace06ff7062df403`
- audit prompt：`1bf83a07efdc6aba7564374752fea14041fffd749742ed5ae1d55e4cbafc76b0`

## Challenge / defense

### Round 1

- 找到根因：仅检查没有 `SONG_PARTIAL`，会把“根本没跑出完整性证据”误当完整。
- 找到身份/输入问题：歌名错、LRC provider 只有 NetEase、旧紧窗口漏掉真正前奏与尾部。
- 找到 provenance 问题：旧 MP4/cover glob 可能把 stale artifact 继承给新记录。
- 防御：LRCLIB + provider-neutral pin + same-segment proof retry；交付必须是正证据；删除 stale glob。

### Round 2

- P0：full-session wrapper 把 inner `materialized_recut` 等字段投影丢了，runner 会把真实新证明永久看成缺失。
- P2：只要给空 `{}` report 自己算一个合法 SHA，早期版本的 edge gate 仍可通过。
- 防御：outer summary 透传 title/subtitle/boundary/recut；edge gate 解析报告内容并交叉绑定 schema、provider、source URL、title、candidate、offset、≥8 条单调非空歌词、≥55% match、边界、SRT/MP4 hash；两条均有回归测试。

### Round 3

- Hermes ChatGPT Pro 请求在可见模型标签未确认时停止，未提交；不能把它冒充 Pro 结论，也不能重复发送。
- 本地独立 challenger 先后实证三条 P1：
  1. 固定 selector 目录会在本次 rc=1 时重用旧 valid summary 并再次交付。
  2. 负 global offset 被截成 clip start 0，会给缺失器乐前奏的源发 `FULL_SONG_READY`。
  3. 外部 LRC 必须 sample-accurate 重渲染，但失败后仍曾保留 `MATERIALIZED`，hash 正确的 coarse copy 仍可能交付。
- 防御：每次 selector 使用独占空 `attempt-*` 目录并要求当前 rc=0；nominal LRC zero 必须在源内且由 boundary/alignment/report/recut 全链一致绑定；外部 LRC 精确重渲染与 fresh render QA 必须都成功，否则 producer=`RETRY_INFRA` 且 runner 独立拒绝。
- reviewer 用原复现方法复跑并检查当时的 LRC/边界 diff，结论：**当时这一层无剩余 P0/P1**。聚焦回归 `135 passed`，全套 `367 passed`。该结论早于本次演唱者事故，不能外推为联合演唱门已通过终审。

## 当前 AGY v4 live acceptance gate

生产只接受同一条新运行记录绑定的以下证据：

1. `song_boundary.status == FULL_SONG_READY`，且 `clip_start <= nominal_lrc_zero <= first_lyric <= last_lyric <= clip_end`。
2. `lyrics_alignment.status == READY`，provider/source/model/offset/report SHA 齐全。
3. 同一 hash-bound AGY v4 raw/report 满足首尾演唱、至少七行且至少 80% 演唱、最多一个且受六行/12 秒/20%/15 秒四重限制的歌曲内戏剧对白块；头/中/尾三点证据全落演唱行，并逐行证明同一李豆沙现场声源、无其他歌手/和声、无预录/回放。任一非现场 mode、普通说话+BGM、缺字段或 raw/report 分歧硬否决。
4. `host-vocal-proof.v2` 的 `status == READY` 且 `decision == LIDOUSHA_VOCAL_PRESENT_ON_LYRIC_CHECKPOINTS`；post-song anchor 与七个仅来自演唱行的 line-local checkpoint 满足 0.60 / 0.31 / 5-of-7 / 三桶覆盖，required singing role、五项逐行断言、所有 hash 绑定与记录分数重算通过。
5. runner 产生 `joint_singing_decision == VERIFIED_LIDOUSHA_SINGING`；不得用单独 AGY、CAM++ 或人工听感代写这个字段。
6. alignment report schema 为 `lyrics-alignment-report.v1`，25 条 LRC 时间轴单调非空，匹配率至少 55%，内容与 summary 互相一致。
7. `materialized_recut.status == MATERIALIZED` 且 `subtitle_source == external_lrc_global_shift`；边界/nominal LRC zero/offset 与证明完全相等。
8. `accurate_rerender_used == true`、没有 `FFMPEG_ACCURATE_RECUT_FAILED`，fresh render QA 必须 pass 且 `actual_cut_error_ms <= threshold_ms`。
9. record-bound SRT 与 burned MP4 的 SHA-256 重算一致；交付 sidecar 正是这份 report/SRT/manifest。
10. 实看首句、副歌、重复段、最长间奏、尾部五点；确认前奏保留、后续晚安说话不混入。
11. 标题使用真实歌名并带直播语境；封面按项目 AI-cover 流程生成并同步标题文案。
12. `upload_enabled=false`，不存在上传 manifest/上传进程。

任一项失败即保持 blocked；不得借用本次人工 AGY audit 去伪造新运行记录的运行时绿灯。

当前受控验收状态（2026-07-10）：`f64cd29` 已部署；事故 state/summary/report/delivery 相关六文件修复已 `COMMITTED`；真唱《屑屑》v5 已同时达到 `READY` 与 `MATERIALIZED`，且没有上传。同一音轨换成静态回放画面后由 AGY v4 的 `ORIGINAL_OR_BACKGROUND_PLAYBACK` 硬否决阻断。当前《芽吹くとき》fresh run 根本没有因日语/乱码 ASR 失败：自动 provider 从 10 个候选中选中 LRCLIB `33542202`，25/25 canonical 行和完整边界均 READY；本轮 AGY v4 把播放误报为 live，但 `host-vocal-proof.v2` 七点 0/7，最终 `BLOCK / SONG_NOT_LIDOUSHA_SINGING`、`materialized_recut=null`。这正是联合 AND 不能降级成单层判断的实机证据。10:50:29Z exact cron entrypoint 返回 0，state/summary/upload ledger 不漂移，runner lock 释放且 `DISABLED` 已移除；scheduled runner 已恢复。

## 2026-07-10 historical LRC-only acceptance（已推翻）

部署后连续使用 fresh run id 重跑，失败轮次均没有借用旧 summary/artifact：

- v1 找到正确《芽吹くとき》，但稀疏日语 ASR 只有约 28% 的 LRC 行可匹配，按门槛安全阻断。
- v2 暴露直接调用 `produce_song()` 时 CPA endpoint 没有从 `CPA_ENV` 装载；修复落在 `d43a310`，仍未交付假成品。
- v3 同一份乱码 ASR 因语义召回非确定性返回零候选；修复 `1e8818c` 让 upstream song-lane anchor 穿过 tight/core/full selector，且仅 full retry 开启 audio+LRC。seed 只保召回，不生成完整性证明。
- v4 报告：`free:/opt/bilive/autoslice/reports/manual_rerun_2026-07-09_mebukutoki_v4.json`；rerun 本身 `manual_no_upload=true`、`state_write=false`、result `status=review_ready`、`decision=AUTO_RECUT` / `SONG_FULL_BOUNDARY_READY`。后续验收记录为 `final_acceptance.status=ACCEPTED_NO_UPLOAD`。

v4 绑定证据：

- candidate `seededsong_45000_200540`；LRCLIB `https://lrclib.net/api/get/33542202`，yonige《芽吹くとき》。
- `agy` `Gemini 3.5 Flash (High)` 对当前 245.566s full proof window 验证 25/25 行均 heard、逐行 confidence `0.95`、matched ratio `1.0`，一个精确 `+17000ms` global shift；`first_line / chorus / longest_instrumental_gap / tail` 与下一段说话 `227000ms` 均有 raw 观察。
- 原始 raw 中 `repeated_section` 标签误指了第一次出现；原文件和 SHA 保持不可改。final acceptance 另以同一 raw AGY observation 的 **later recurrence** 补证：exact repeated lyric `最初に望んだ未来とは少し違うけれど`，LRC index `17`，full-window `152910ms`，原始段 absolute `274130ms`，confidence `0.95`。补证已写入 manual report，报告 SHA-256 `bcf0500829d6802bc4d6397ea7b48c8ab79edc2d00656d766ecf314a63241e85`。后续生产 commit `c847325ce43e7e3914e8921c3f27c1254a6beffa` 已部署：validator/prompt 要求 exact repeated lyric 的 later occurrence，错误首现点会 fail closed；alignment report 也直接携带 spot checks 与 post-song talk。部署轮聚焦回归 `70 passed`、全套 `382 passed`；本文档轮另复跑最窄 sparse-Japanese/audio-LRC mutation 集 `6 passed`。
- `song_boundary=FULL_SONG_READY`、`lyrics_alignment=READY`、`subtitle_source=external_lrc_global_shift`；alignment report SHA-256 `d2daa6f4ea007e587c0a99c34bce2d117bdce16f606e4db0df2b9cf8854a6c2e`，raw audio observation SHA-256 `bc2fc53ff6d0de7bc20ff4980be717a286b1f4f9671e491d6a976ab935139f09`。
- sample-accurate re-render 实际切点误差 `16ms <= 100ms`，render QA pass；最终 SRT SHA-256 `e89a1b6d1dda509bbc44ff95ffa7d40d17761076dbc49b217e8ae91b11dcb789`，burned MP4 SHA-256 `759bb74fa3b31f663b40547c7b45a96bec606d1470764d8eea9c3b2c97a04bee`。
- 标题 `【李豆沙】豆沙歌，《芽吹くとき》｜下播前的温柔哄睡小歌`。v4 selector result 本身仍记录 `cover_release_gate_satisfied=false`；review delivery 后才按项目封面流程单独生成 `gpt-image-2` 封面。最终封面路径 `free:/opt/bilive/autoslice/repo/lidousha/2026-07-09/歌切_【李豆沙】豆沙歌，《芽吹くとき.cover.png`，SHA-256 `7ee06854118b08f573111000f766f0b22ee2d078a9f9b8a576b77ee058f8ba17`；绑定的 `cover-generation.json` SHA-256 `2304705c921c5eca2b68328e5a74b268004c63a16f29424a337a7144c6cf3998` 记录 `method=images.edit`、`model=gpt-image-2`、`fallback_used=false`、`AI_COVER_READY` / visual QA pass，且 `cover_text=《芽吹くとき》｜下播前的温柔哄睡小歌` 与标题一致。这仍不把 v4 提升为 publish-ready。
- rerun 过程没有上传且 `state_write=false`。验收后才备份并修复 7/9 state、把旧假绿 MP4/cover 移入 `_quarantine/false-green-song_223019_166-20260710T040416Z`；final acceptance 仍记录 `no_upload_verified=true`。公开视频发布仍须另行获得 Ivan 授权，并满足独立 `AUTO_UPLOAD` manifest + artifact-hash gate。
- 本次 live acceptance 读回时 `/opt/bilive/autoslice/DISABLED` 仍存在，scheduled runner 是 paused；部署/验收成功不等于 cron 已恢复。恢复与最终运行态由顶层 `docs/HANDOFF.md` 的收尾步骤记录。

这次被推翻的验收只证明：日语唱歌 ASR 稀疏/乱码时，已知 song-lane anchor 不再被二次语义召回随机丢失；在 Google/公开网页或自动 provider 找到唯一可靠同步 LRC 身份后，流水线能建立歌曲/边界证据。它不曾证明李豆沙在唱。新联合门在日语 LRC 路径之后另行验证现场演唱和主播身份；无可靠 LRC、身份歧义、版本不符、背景播放、其他歌手、说话+BGM、无合格 post-song 锚点或任一 hash 门失败均 fail closed。
