# 2026-07-10 李豆沙歌切联合演唱门审查

## 结论

歌切必须是李豆沙本人在直播现场连续演唱。一段音频即使与同步 LRC 100% 对齐，也可能只是原唱、片尾曲或 BGM。生产决策因此改为两个独立子结论的 AND：

```text
AGY v2: LIVE_STREAMER_SINGING
  AND
CAM++: LIDOUSHA_VOCAL_PRESENT_ON_LYRIC_CHECKPOINTS
  =>
VERIFIED_LIDOUSHA_SINGING
```

任一层缺失、不确定或失败均 BLOCK。这是 no-upload review gate，不开启上传。

## 事故与根因

2026-07-09 的 yonige《芽吹くとき》在固定 goodbye/end-card 画面下播放了原曲。旧 AGY/LRC 观察得到 25/25 行 `heard=true`、高 confidence 和单一 global shift，这些只证明“这首歌的录音完整可听”，却被旧 shadow/runner 错写成 foreground singing 和 `song_complete=true`。

根因不是日语 LRC 难找；正确 LRC 可通过 Google/公开网页人工发现，或由自动 NetEase + LRCLIB + Kugou 检索得到。Google 结果页本身不是无人值守歌词 API 或证据源。根因是把“歌曲录音对齐”错当成“李豆沙现场演唱”，且没有对背景播放和说话+BGM 设硬否决。

## 联合门契约

### 1. AGY v2 现场演唱硬否决

同一 hash-bound 当前音频/LRC 观察必须同时满足：

- `mode == LIVE_STREAMER_SINGING`
- `confidence >= 0.85`
- `continuous_singing == true`
- `background_recording_likelihood <= 0.20`
- 恰好三个具体 evidence timestamp，严格覆盖歌词头/中/尾

`ORIGINAL_OR_BACKGROUND_PLAYBACK`、`OTHER_SINGER`、`STREAMER_TALKING_OVER_MUSIC`、`AMBIGUOUS` 和任何畸形证据都是不可覆写的硬否决。AGY 这一层只判断演唱模式，不单独证明歌手身份。

### 2. CAM++ 李豆沙声纹子结论

- 从 `post_song_talk_start_ms` 提取 4–8 秒同会话主播说话锚点，对三份 hash-pinned 李豆沙 enrollment 的分数中位数必须 `>=0.60`。
- 从实际对齐结果中选七个互不相同的歌词 cue；每个 cue 时长至少 2.5 秒，取其中央 2.5–4 秒，不取脱离该行歌词的宽窗口。
- 每个 checkpoint 必须同时满足：对三份 pinned enrollment 的中位数 `>=0.31`，且对同会话主播锚点的分数 `>=0.31`。
- 至少 5/7 通过，且头/中/尾三桶各至少有一点通过。

这一层的准确名称是 `LIDOUSHA_VOCAL_PRESENT_ON_LYRIC_CHECKPOINTS`；它不是歌唱分类器。李豆沙在 BGM 上说话也可能命中 CAM++，所以只能和 AGY 现场演唱子结论 AND。

### 3. 验证器的信任边界

runner 验证器复核 source/alignment/profile/model/reference/session-anchor/checkpoint 的路径与 SHA-256 绑定，并从已记录的分数重算中位数、门槛结果、5/7 与三桶覆盖。它不重跑 CAM++ 推理，因此不得宣传成独立的第二次 ML 判定，也不是数学/形式化的演唱者证明。

## 部署前校准与对抗证据

以下是同一 pinned CAM++ 路径的实际探针，不是完整 ROC：

- 事故负例《芽吹くとき》七个 line-local cue 的 enrollment 中位数为 `0.10430, 0.09148, 0.20201, 0.05726, 0.21408, 0.07801, 0.13564`：在 0.31 门槛上 0/7。
- 已知李豆沙现场演唱正例《屑屑》为 `0.31752, 0.50220, 0.36316, 0.48067, 0.44184, 0.34516, 0.43098`：对 pinned enrollment 为 7/7。
- 事故会话的 post-song 说话锚点对 enrollment 中位数为 `0.687`，说明锚点确实是李豆沙；但背景歌曲 cue 对该锚点为 0/7。正例锚点对 enrollment 为 `0.661`，歌词 cue 对同会话锚点恰好 5/7。
- 把李豆沙说话叠在《芽吹くとき》BGM 上的对抗混音中，中等说话音量可让 CAM++ 过 3/7，高说话音量可过 6/7。这实证了 CAM++ 不能单独作“在唱”结论，AGY 的 `STREAMER_TALKING_OVER_MUSIC`/非现场 mode 必须是硬否决。
- AGY v2 对事故负例返回 `ORIGINAL_OR_BACKGROUND_PLAYBACK`、`continuous_singing=false`、背景录音概率 1.0，并给出头/中/尾具体观察；该负例会在声纹/物料化前被否决。

对抗设计的三轮意义是：第一轮推翻“LRC 对齐 = 现场演唱”；第二轮推翻“CAM++ 命中 = 在唱”；第三轮限定 verifier 与校准的信任边界。最终独立 review 与生产读回尚待主任务补齐，本文不预告“已验收”。

## 日语 LRC 的准确语义

日语/kana 可直接用于 Google/公开网页人工检索，自动路径的 `--lrc-provider auto` 会查 NetEase、LRCLIB 和 Kugou。歌唱 ASR 稀疏、乱码或不含可用日语歌词，不再自动导致该 song-lane anchor 丢失。

这不是“所有日语歌必过”：没有唯一可靠的同步 LRC、版本不符、当前音频无法形成单一位移，或现场/身份联合门失败，都会正确 BLOCK。

## 已知剩余风险

- 目前校准覆盖一个真实背景原曲负例、一个李豆沙现场正例和说话+BGM 对抗例；其他歌手、合唱、模仿声线、大量观众嘈杂等 ROC 尚不完整。
- AGY 是音频/视频模型观察，不是可验证的音源分离；两层 AND 降低事故类假阳性，但不消灭所有模型误判。
- 缺少至少 4 秒的 post-song 主播说话时会 fail closed，可能拒绝真正的歌切；这是当前宁可假阴性也不交付背景原曲的产品选择。
- CAM++ 分数由专用生成进程产生；runner 只重算绑定/聚合，未防御已能任意篡改生成器与所有绑定物的攻击者。

## 部署与 live acceptance（待完成）

本文写入时只声明源码/文档契约，不声明已部署。完成标准是：

1. 从干净 commit 用 `scripts/deploy_free_autoslice.sh free` 部署，读回 `DEPLOYED_COMMIT` 与运行文件 hash。
2. 在 `free` 安装并重验 pinned CAM++ 模型和三份私有 enrollment。
3. 用 fresh candidate/run id 对同一《芽吹くとき》源重跑；AGY v2 应以背景播放 BLOCK，不得生成新歌切、封面或 delivery。
4. 用已知李豆沙现场正例证明 AGY-live 与 CAM++-identity 同时可过，且 runner 只在这个 AND 上生成 `VERIFIED_LIDOUSHA_SINGING`。
5. 在 flock 下备份并原子修复 7/9 state/report/summary，清除活跃交付指针，读回新 hash。
6. 跑完回归、最终独立 review 和 runner smoke tick 后才移除本轮临时 kill switch。

## No-upload 边界

本修复不包含上传授权。《芽吹くとき》负例必须保持 blocked/superseded，不得生成 `AUTO_UPLOAD` manifest，不得启动 uploader。任何未来正例也仍需 Ivan 对具体成片单独授权并通过 artifact-hash gate。
