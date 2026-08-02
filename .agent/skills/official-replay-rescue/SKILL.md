# 官方录播补救（official-replay-rescue）

录制字节丢失（写缓存蒸发/上传 Fatal/磁盘事故）而弹幕 sidecar 与 BCUT 缓存幸存时，
从主播官方直播回放重建 session 源文件并复活候选。规则权威见
[docs/pipeline/10-source-recording.md](../../../docs/pipeline/10-source-recording.md)
（`SOURCE_MEDIA_MISSING` 语义）；本文件只是操作配方。首例：2026-07-25
19-20-00/19-50-00（commit 9dd7f4c，provenance 披露在 canonical 目录）。

## 前提清单（缺一不可）

- 丢失 stem 的 `.xml`（弹幕）幸存 → chat authority 与 wall t0 可恢复；
- `cache/<date>/<stem>.bcut.srt` 幸存 → 精确对齐的文本真值；
- 同场 ≥1 个幸存 session 带媒体 → 音频锚点绝对标定；
- 官方回放覆盖丢失区间（B站空间 → 合集「直播回放」，标题含日期场次）。

## 步骤

1. **找回放 BV**：space.bilibili.com/1703797642/lists → 系列·直播回放。
   record API（xlive/…/record/getList）常年为空且机房 IP 会 -352，别依赖。
2. **下载**（free）：BBDown + SESSDATA（`/opt/bilive/autoslice/vod_ingest_cookies.txt`）
   + `--upos-host upos-sz-mirroraliov.bilivideo.com`（yt-dlp/aria2 会拿坏字节）。
   门：时长窗 + 全片视频流解码零损伤（模板见 `/opt/bilive/vod-rescue/2026-07-25/download.sh`）。
3. **重建切割**：`scripts/rescue_from_official_replay.py`（先 STAGED，核对后 `--apply`）。
   关键设计（勿退化成单一仿射切割）：
   - 回放会在段边界**无缝吞秒**（7/25 实测 20:20↔20:50 间吞 4.3s）→ 双音频锚点
     不一致即拒绝单映射；
   - 每个丢失 session 独立收敛：锚点外推粗定位 → 音频-only 切割 → BCUT →
     与缓存 BCUT 逐 cue 文本对齐（中位差 ≤0.35s 收敛；头尾漂移 ≤0.6s 证明
     切割件内部无跳秒）→ 终视频编码 → probe/时长/解码扫描 → 终件独立 BCUT 复验；
   - `--apply` no-clobber 落 canonical + `<stem>.rescue-provenance.json` 披露
     （回放 sha、切割区间、锚点证据、收敛轨迹、复验统计）。**不伪造 .meta.json**
     （session 身份已冻结在 day state 的 segment_sessions）。
4. **复活候选**：唯一 sanctioned 通道 `scripts/revive_rejected_candidates.py`
   （`candidate_rejected/source_media_missing` → failed+recoverable → cron 自跑）。
   `failed` 态候选在下一次部署指纹变化时自动 requeue，无需复活脚本。
5. **验收**：候选按完整流水线正常交付（字幕/边界/封面全链门照跑）；上传证据 commit。

## 已知边界

- 回放画质 ≤1080P 转码（非原画）；provenance 里披露 encoder，源完整性门照过。
- 回放弹幕 xml（BBDown -dd）**相对时间轴实测可信**：抽帧对照直播间弹幕
  overlay，xml 时间戳与画面出现时刻一致（±1-2s），可用作选题热度信号与名场面
  定位。但**绝对 epoch 不可信**——批量导入时刻会整体覆盖发送时刻，绝对轴必须
  另行锚定；音频锚点+BCUT 仍是字幕对齐的唯一权威。
- **绝对时间锚点优先级：画面内时钟 > 官方标题的场次时间**。不少直播间 overlay
  自带 TIME 时钟——抽两帧读钟（间隔几百秒交叉验证）推 t0，比「标题写 18 点场」
  可靠得多（标题场次实测能偏差半小时量级）。stem 时间戳按画面时钟锚定。
- 回放缺失区间（开播前/断流丢帧）无法重建——BCUT 对齐门会把这种情况打成
  drift/收敛失败并拒绝，此时按段内分片人工裁定。
- 挑场次时**先确认横屏**（16:9）：回放列表里可能混着竖屏场（1080x1920），
  封面/烧录链的构图默认按横屏设计。

## 从零造 session 源（只有官方回放、无任何幸存件时）

上面的主流程覆盖"局部补洞"（.xml/BCUT 缓存/邻段幸存）。若**只有官方回放**，
需要自己把三件套造出来，消费端（`docs/pipeline/10-source-recording.md`）只认：
`<room>_YYYYMMDD-HH-MM-SS.mp4` + 同 stem `.jsonl` + `.meta.json`。要点：

- 官方回放的弹幕 xml **没有录播姬的 raw 协议载荷**，
  `ops/recording/bililive_recorder_adapter.py` 的 `xml_to_jsonl()` 会直接拒绝
  （lacks required raw evidence）。消费端（`src/autoslice/chat_evidence.py`）
  实际只读 `info[0][4]`（epoch ms）与 `info[1]`（文本）：可以据此写忠实转换——
  文本与相对时间逐字来自回放 xml，绝对时间＝画面时钟锚点＋真实偏移。
- **不伪造任何字段**：uid/uname 留空（管线本就把发送者存空串）；epoch 只做
  锚点平移，不做内容修改。
- 全部推导落一份 `.replay-provenance.json` 披露（回放 BV/sha、锚点证据帧、
  t0 推导、条数统计），随源文件入库。
