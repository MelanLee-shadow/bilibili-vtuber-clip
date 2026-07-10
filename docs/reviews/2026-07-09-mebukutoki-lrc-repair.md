# 2026-07-09《芽吹くとき》LRC 假绿修复审查

## 结论

旧回填不能验收：歌曲身份错写成歌词句《ただそばにいて》，源窗漏首尾，且 selector 没有 LRC 对齐/完整边界证明，实际烧录来源是 `asr_cues`。本轮代码改成正证据、hash 绑定、无证据即阻塞；最后 challenger 复跑后没有剩余 P0/P1。本地可以提交，但正式部署及 7/9 no-upload 重跑仍需 Ivan 授权后做 live acceptance。

## 事故证据

- 旧 source-context summary：`lyrics_alignment={}`、`song_boundary={}`。
- 旧 materialized recut：`subtitle_source=asr_cues`。
- 旧源窗：段内 `151.220–341.760s`。
- 2026-07-10 只读复核的生产指纹仍是 `cab9a151ccd67605d087ee0aae48c1ecf1f88830`；7/9 state 仍把该旧物料写成 `song_complete=true`，所以本地补丁尚未改变正式运行面。
- 公开身份/LRC：yonige《芽吹くとき》，LRCLIB id `33542202`，25 条同步歌词，首/末时间戳 `7700/199890ms`。
- 原始段音频/LRC 高质量审核：LRC 零点约段内 `148.5s`，首句约 `156.2s`，尾部约 `349.0s`，下一段说话约 `348.820s`；一个 global shift 即可，无 stretch。

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

1. `song_boundary.status == FULL_SONG_READY`，且 `clip_start <= first_lyric <= last_lyric <= clip_end`。
2. `lyrics_alignment.status == READY`，provider/source/model/offset/report SHA 齐全。
3. alignment report schema 为 `lyrics-alignment-report.v1`，25 条 LRC 时间轴单调非空，匹配率至少 55%，内容与 summary 互相一致。
4. `materialized_recut.status == MATERIALIZED` 且 `subtitle_source == external_lrc_global_shift`；边界/nominal LRC zero/offset 与证明完全相等。
5. `accurate_rerender_used == true`、没有 `FFMPEG_ACCURATE_RECUT_FAILED`，fresh render QA 必须 pass 且 `actual_cut_error_ms <= threshold_ms`。
6. record-bound SRT 与 burned MP4 的 SHA-256 重算一致；交付 sidecar 正是这份 report/SRT/manifest。
7. 实看首句、副歌、重复段、最长间奏、尾部五点；确认前奏保留、后续晚安说话不混入。
8. 标题使用真实歌名并带直播语境；封面按项目 AI-cover 流程生成并同步标题文案。
9. `upload_enabled=false`，不存在上传 manifest/上传进程。

任一项失败即保持 blocked；不得借用本次人工 AGY audit 去伪造新运行记录的运行时绿灯。
