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
- 回放弹幕 xml（BBDown -dd）可能只含尾部池，不可当对齐依据；对齐只信音频锚点+BCUT。
- 回放缺失区间（开播前/断流丢帧）无法重建——BCUT 对齐门会把这种情况打成
  drift/收敛失败并拒绝，此时按段内分片人工裁定。
