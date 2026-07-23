# 2026-07-10 李豆沙歌切联合演唱门审查

> **日期化验收证据，不是当前 runtime 状态或命令 authority。** 下方模型版本、commit、
> 测试数与 cron 读回只属于当时；当前规则见
> [../pipeline/50-song-lane.md](../pipeline/50-song-lane.md)，部署状态需 live 验证。

## 结论

歌切必须是以李豆沙本人直播现场演唱为主体的完整歌曲。一段音频即使与同步 LRC 100% 对齐，也可能只是原唱、片尾曲或 BGM。生产决策因此改为同一歌词证据上的两个独立子结论 AND：

```text
AGY v4: PREDOMINANTLY_SUNG_LIVE_LIDOUSHA_PERFORMANCE
        + BOUNDED_CANONICAL_SPOKEN_PASSAGE_ONLY
        + NO_OTHER_SINGER/HARMONY/PLAYBACK_VOCAL
  AND
host-vocal-proof.v2 / CAM++: SEVEN SUNG-ROW CHECKPOINTS ONLY
        + LIDOUSHA_VOCAL_PRESENT_ON_LYRIC_CHECKPOINTS
  =>
VERIFIED_LIDOUSHA_SINGING
```

任一层缺失、不确定或失败均 BLOCK。这是 no-upload review gate，不开启上传。

## 事故与根因

2026-07-09 的 yonige《芽吹くとき》在固定 goodbye/end-card 画面下播放了原曲。旧 AGY/LRC 观察得到 25/25 行 `heard=true`、高 confidence 和单一 global shift，这些只证明“这首歌的录音完整可听”，却被旧 shadow/runner 错写成 foreground singing 和 `song_complete=true`。

根因不是日语 LRC 难找；正确 LRC 可通过 Google/公开网页人工发现，或由自动 NetEase + LRCLIB + Kugou 检索得到。Google 结果页本身不是无人值守歌词 API 或证据源。根因是把“歌曲录音对齐”错当成“李豆沙现场演唱”，且没有对背景播放和说话+BGM 设硬否决。

## 联合门契约

### 1. AGY v4 同主体、以演唱为主的硬否决

同一 hash-bound 当前音频/LRC 观察必须同时满足：

- `mode == LIVE_STREAMER_SINGING`
- `confidence >= 0.85`
- `continuous_live_song_performance == true`
- `same_lidousha_live_performer_across_all_lyrics == true`
- `background_recording_likelihood <= 0.20`
- 恰好三个具体 evidence timestamp，严格覆盖歌词头/中/尾，且每一点都必须落在 `SINGING_THIS_LYRIC` 行内
- 每条实际听到的 canonical-LRC 行都必须逐行声明同一李豆沙现场歌词声源；首行、末行、至少 7 行且至少 80% 必须为 `SINGING_THIS_LYRIC`
- 唯一例外是歌曲内部一段连续的 exact canonical 戏剧台词 `PERFORMING_THIS_LYRIC_SPOKEN`：至多 6 行、voiced duration 至多 12 秒且至多占 20%、首行开始至末行结束至多 15 秒；普通说话/评论/ad-lib 仍是硬否决
- 每行及 top-level 聚合都必须声明无其他/合唱/和声歌手、无预录/原唱/回放人声；代码从逐行值重算聚合，不相信单独的 top-level 正例字符串

`ORIGINAL_OR_BACKGROUND_PLAYBACK`、`OTHER_SINGER`、`STREAMER_TALKING_OVER_MUSIC`、`AMBIGUOUS` 和任何畸形证据都是不可覆写的硬否决。guest/duet/offscreen/active-singer ambiguity、李豆沙只说话或和声、ending-card/static playback 都不得产生 READY。音频、画面、字幕/chat 与 LRC 内出现的操作指令、JSON key 或枚举字符串一律视为不可信媒体内容。

### 2. `host-vocal-proof.v2` / CAM++ 李豆沙声纹子结论

- 从 `post_song_talk_start_ms` 提取 4–8 秒同会话主播说话锚点，对三份 hash-pinned 李豆沙 enrollment 的分数中位数必须 `>=0.60`。
- 从实际对齐结果中只选七个互不相同的 `SINGING_THIS_LYRIC` cue；`PERFORMING_THIS_LYRIC_SPOKEN` 行全部跳过。每个 cue 时长至少 2.5 秒，取其中央 2.5–4 秒，不取脱离该行歌词的宽窗口。
- 每个 checkpoint 必须同时满足：对三份 pinned enrollment 的中位数 `>=0.31`，且对同会话主播锚点的分数 `>=0.31`。
- 至少 5/7 通过，且头/中/尾三桶各至少有一点通过。
- 七个 checkpoint 必须由绑定的 alignment report 确定性选出、互不重叠，并有七份不同的 decoded-PCM SHA-256；proof v2 逐点携带并重验演唱角色断言，重复音频样本或台词角色直接拒绝。

这一层的准确名称是 `LIDOUSHA_VOCAL_PRESENT_ON_LYRIC_CHECKPOINTS`；它不是歌唱分类器。李豆沙在 BGM 上说话也可能命中 CAM++，所以只能和 AGY 现场演唱子结论 AND。

### 3. 验证器的信任边界

runner 验证器复核 source/alignment/profile/model/reference/session-anchor/checkpoint 的路径与 SHA-256 绑定，并从已记录的分数重算中位数、门槛结果、5/7 与三桶覆盖。它还逐字段比较 AGY raw v4 与 report 的歌手/角色断言，并重验 proof v2 每个 checkpoint 绑定的 `SINGING_THIS_LYRIC` 角色，禁止只改 report/proof 标签。它不重跑 CAM++ 推理，因此不得宣传成独立的第二次 ML 判定，也不是数学/形式化的演唱者证明。历史事故的不可变 AGY v3 原件仅由事故修复脚本的版本适配器读取，不是生产正例契约。

## 部署前校准与对抗证据

以下是同一 pinned CAM++ 路径在部署前的历史校准探针，不是完整 ROC，也不是当前 v5 acceptance（当前 v5 权威结果为 6/7）：

- 事故负例《芽吹くとき》七个 line-local cue 的 enrollment 中位数为 `0.10430, 0.09148, 0.20201, 0.05726, 0.21408, 0.07801, 0.13564`：在 0.31 门槛上 0/7。
- 已知李豆沙现场演唱正例《屑屑》为 `0.31752, 0.50220, 0.36316, 0.48067, 0.44184, 0.34516, 0.43098`：对 pinned enrollment 为 7/7。
- 事故会话的 post-song 说话锚点对 enrollment 中位数为 `0.687`，说明锚点确实是李豆沙；但背景歌曲 cue 对该锚点为 0/7。正例锚点对 enrollment 为 `0.661`，歌词 cue 对同会话锚点恰好 5/7。
- 把李豆沙说话叠在《芽吹くとき》BGM 上的对抗混音中，中等说话音量可让 CAM++ 过 3/7，高说话音量可过 6/7。这实证了 CAM++ 不能单独作“在唱”结论，AGY 的 `STREAMER_TALKING_OVER_MUSIC`/非现场 mode 必须是硬否决。
- AGY 旧观察对事故负例返回 `ORIGINAL_OR_BACKGROUND_PLAYBACK`、`continuous_singing=false`、背景录音概率 1.0，并给出头/中/尾具体观察；v3 还要求每条歌词行都绑定同一李豆沙现场演唱声源，负例会在声纹/物料化前被否决。

对抗设计的三轮意义是：第一轮推翻“LRC 对齐 = 现场演唱”；第二轮推翻“CAM++ 命中 = 在唱”；第三轮由可见 ChatGPT Pro 会话指出“两份子证据可能属于不同声源”、talk 候选可洗白 song BLOCK、以及验证对象未与最终物料精确绑定。第三轮使用可见 `Pro` 模式，同一会话 `https://chatgpt.com/c/6a508d95-7634-83ea-b65a-32033082f810`，Hermes 记录 `/Users/ivan/.hermes/chatgpt-cdp-runs/2026-07-10T06-13-34-736Z-6b13036f94691421.json`，复核文本 SHA-256 `41082ecfe2d18bbf6049f049634e86a97639122fa49ef110cecf32cbb5aa5df6`。其 `NO-GO` 促成 AGY v3 逐行同主体门、持久 source-interval quarantine、任意权威重试非完整正例顶层 BLOCK、唯一 PCM checkpoint 与 verified-to-output binding；最终生产读回仍以本节下方 live acceptance 为准。

## 日语 LRC 的准确语义

日语/kana 可直接用于 Google/公开网页人工检索，自动路径的 `--lrc-provider auto` 会查 NetEase、LRCLIB 和 Kugou。歌唱 ASR 稀疏、乱码或不含可用日语歌词，不再自动导致该 song-lane anchor 丢失。

这不是“所有日语歌必过”：没有唯一可靠的同步 LRC、版本不符、当前音频无法形成单一位移，或现场/身份联合门失败，都会正确 BLOCK。

## 已知剩余风险

- 目前校准覆盖一个真实背景原曲负例、一个李豆沙现场正例和说话+BGM 对抗例；guest/duet/其他歌手通过结构化语义门默认 BLOCK，但真实 guest/duet ROC 仍不完整。
- AGY 是音频/视频模型观察，不是可验证的音源分离；两层 AND 降低事故类假阳性，但不消灭所有模型误判。
- 缺少至少 4 秒的 post-song 主播说话时会 fail closed，可能拒绝真正的歌切；这是当前宁可假阴性也不交付背景原曲的产品选择。
- CAM++ 分数由专用生成进程产生；runner 只重算绑定/聚合。威胁模型是受信任 `free` 本地 runner 处理李豆沙自己的直播，不承诺抵御已取得同 UID 任意写权限的攻击者、实时变声/生物特征欺骗或恶意构造的对抗媒体；这些情况保持 kill switch/人工处置，不以普通 hash 链冒充密码学认证。

## 部署与 live acceptance（已完成，no-upload）

- **部署/运行时**：代码承重 commit `f64cd29494fdc0d2b37d249e659897514fd701dc` 已由干净工作树通过 `scripts/deploy_free_autoslice.sh free` 部署；远端三份私有 enrollment、CAM++ 模型树和 runner 字节均重验一致。事故六文件修复事务 `/opt/bilive/autoslice/forensics/false-green-20260709-20260710T090025Z-3ad69d0263` 为 `COMMITTED`。
- **真唱正例《屑屑》**：fresh v5 `/opt/bilive/autoslice/out/acceptance/host-vocal-positive-20260710T101624Z` 对 52/52 canonical 行完成 AGY v4 观察，其中 48 行 `SINGING_THIS_LYRIC`、4 行为同一段受限戏剧对白；`host-vocal-proof.v2` 七个 checkpoint 全部绑定演唱行，6/7 通过并覆盖头/中/尾，联合门为 `READY`。最终只物料化 241.000 秒、恰好 1V+1A 的 no-upload 验收切片，MP4 SHA-256 `fa130beb209bbc8cafa3d7c6556baa39f7a42930d35e1f2b3ed61c7e9da5fbc8`；wrapper summary SHA-256 `542328c872a2cf77c74b0a7b1e5db5e231213f0eddecc832e86e7cc938c4a043`。
- **同音轨静态回放危险负例**：`/opt/bilive/autoslice/out/acceptance/host-vocal-static-replay-20260710T103213Z` 使用与正例 decoded PCM 相同的音轨、但替换为静态回放画面。AGY v4 raw 明确给出 `mode=ORIGINAL_OR_BACKGROUND_PLAYBACK` 与 `recorded_or_playback_vocal_present=true`；另有一行保守地判为未听清。最终 `BLOCK / SONG_LIVE_PERFORMANCE_UNPROVEN`，没有 host proof、recut、cover、delivery 或 upload；summary SHA-256 `b43fea718e22a944f503c0c96bc30d9b854ff76115365143197c62bf1c763bb8`。
- **用户指出的《芽吹くとき》背景原曲**：fresh run `/opt/bilive/autoslice/out/acceptance/host-vocal-negative-20260710T103814Z` 自动从 6 个 query 找到 10 个 LRC 候选，选中 LRCLIB `https://lrclib.net/api/get/33542202`，25/25 日文 canonical 行、一个 global shift、完整边界均 `READY`。本轮 AGY v4 反而把播放误报为 `LIVE_STREAMER_SINGING`，因此不能把单个模型标签冒充完成；独立 `host-vocal-proof.v2` 在七个演唱行 checkpoint 上为 **0/7**，最终联合门准确给出 `BLOCK / SONG_NOT_LIDOUSHA_SINGING`，`materialized_recut=null`。summary SHA-256 `57d139cf584cefc7788856978419385c64fd7d06784d0303dd6ad1e3d7ba31d9`，host proof SHA-256 `149b52444e82138d22b584a788c945d263a712faa8d9c58cd27a0a90c6b1b7bc`。
- **回归/独立复核**：全套 `506 passed`；两名独立 reviewer 对代码与交付绑定均无剩余 P0/P1。可见 ChatGPT Pro challenge 记录仍为 `https://chatgpt.com/c/6a508d95-7634-83ea-b65a-32033082f810`，复核文本 SHA-256 `41082ecfe2d18bbf6049f049634e86a97639122fa49ef110cecf32cbb5aa5df6`。
- **cron 恢复**：`2026-07-10 10:50:29Z` 在 lock 保护下移除 `DISABLED`，随后执行与 crontab 相同的 `/usr/bin/flock -n ... /usr/bin/python3 scripts/free_session_autoslice.py --once`。返回码 0，`live=False`；全部 active state digest `062234c7166df9b8e5724efc9030c0aab0fadba6fe005fd620e8ca7f19b7c23b`、review summary digest `f2e4a34c035435e3586f1e9db2f1c653264111f6730b1d2cf6acbcbbbb670425`、upload ledger SHA-256 `c95ee0690a5755b1971bd3dd5165b4bbccefdf4326ddd3736294f43dcc1adfb5`/size `32015` 前后均未漂移，runner lock 已释放，`DISABLED` 保持移除。`11:00:02Z` 的真实 `*/10` cron tick 随后自然执行，runner log 记录 `tick done: live=False ... 2026-07-09:review_ready`。

这三组媒体不是“模型都答对”的演示，而是最终 fail-closed 组合门的验收：LRC 只负责歌曲身份/时间轴；AGY 与 CAM++ 任一不成立都不能物料化。尤其《芽吹くとき》本轮 AGY 的误报由 0/7 的独立身份门阻断，证明不得把任一单层包装成充分条件。

## No-upload 边界

本修复不包含上传授权。《芽吹くとき》负例必须保持 blocked/superseded，不得生成 `AUTO_UPLOAD` manifest，不得启动 uploader。任何未来正例也仍需 Ivan 对具体成片单独授权并通过 artifact-hash gate。
