# 2026-07-09《芽吹くとき》LRC 假绿修复审查

## 结论

旧回填不能验收：歌曲身份错写成歌词句《ただそばにいて》，源窗漏首尾，且 selector 没有 LRC 对齐/完整边界证明，实际烧录来源是 `asr_cues`。修复已经以 commit `1e8818c1ed457273c38697b54d4cc6dd4e79e4a0` 部署，并在同一条 7/9 原始段的 v4 no-upload 重跑中通过歌曲身份、完整边界、LRC 字幕和精确重渲染验收。它是 `review_ready` 的 no-upload delivery，不是 `AUTO_UPLOAD` 或 publish-ready 结论。

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
- reviewer 用原复现方法复跑并检查最终 diff，结论：**无剩余 P0/P1**。聚焦回归 `135 passed`，全套 `367 passed`。

## Live acceptance gate

部署后只接受同一条新运行记录绑定的以下证据：

1. `song_boundary.status == FULL_SONG_READY`，且 `clip_start <= nominal_lrc_zero <= first_lyric <= last_lyric <= clip_end`。
2. `lyrics_alignment.status == READY`，provider/source/model/offset/report SHA 齐全。
3. alignment report schema 为 `lyrics-alignment-report.v1`，25 条 LRC 时间轴单调非空，匹配率至少 55%，内容与 summary 互相一致。
4. `materialized_recut.status == MATERIALIZED` 且 `subtitle_source == external_lrc_global_shift`；边界/nominal LRC zero/offset 与证明完全相等。
5. `accurate_rerender_used == true`、没有 `FFMPEG_ACCURATE_RECUT_FAILED`，fresh render QA 必须 pass 且 `actual_cut_error_ms <= threshold_ms`。
6. record-bound SRT 与 burned MP4 的 SHA-256 重算一致；交付 sidecar 正是这份 report/SRT/manifest。
7. 实看首句、副歌、重复段、最长间奏、尾部五点；确认前奏保留、后续晚安说话不混入。
8. 标题使用真实歌名并带直播语境；封面按项目 AI-cover 流程生成并同步标题文案。
9. `upload_enabled=false`，不存在上传 manifest/上传进程。

任一项失败即保持 blocked；不得借用本次人工 AGY audit 去伪造新运行记录的运行时绿灯。

## 2026-07-10 live acceptance

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

这次验收证明的是：日语唱歌 ASR 稀疏/乱码时，已知 song-lane anchor 不再被二次语义召回随机丢失；在外部检索得到唯一可靠 LRC 身份后，流水线能用当前音频建立严格正证据。它不保证任意日语歌都会通过；无可靠 LRC、身份歧义、版本不符或音频证明任一 gate 失败仍会 fail closed。
