# B站 AI 字幕原理 + 大厂免费 ASR 聚合方案（替代 whisper / agy 精听）

日期：2026-07-04。一句话结论：**B站 AI 字幕引擎不开源、无法本地复刻，但它的同源免费接口（必剪）可以直接白嫖；再聚合剪映作备源，就能整体替代现在管线里的 whisper 和 agy 精听——中文准、时间轴毫秒级、几秒出全场。日语这些中文接口全部不可用。**

脚本：`scripts/free_asr_client.py`（零第三方依赖，stdlib + ffmpeg，Mac / free 都跑通）。

---

## 1. B站 AI 字幕为什么又准又稳（原理，供理解，不复刻）

三代技术演进（证据链见文末）：

1. **2021-12「智能字幕」上线**：平台对已发布稿件自动生成，非 UP 手动开启。
2. **2022-2023 端到端 CTC Conformer**（自研，SpeechIO 2022-11 榜首，B站视频集 CER ~6%）。
3. **2024 起 Index 系大模型接管，2025 末公开 Index-ASR 技术报告**（arXiv:2601.00890）：
   - 架构 = Conformer 音频编码器 + 适配器 + **Qwen3-8B 作解码器**（LLM-based ASR）。
   - 大量真实带噪数据训练，游戏直播/户外 vlog 域显著领先，专门抑制幻觉。
   - **热词/上下文注入**：推理时把热词表+篇章摘要作为 LLM 指令注入，热词召回 67.6%→89.9%。（我们的 jingting 词表精修在做同一件事，只是发生在 ASR 之后。）
   - **仅中英、未开源**。日文字幕走独立日语 ASR + 机翻出「中文（自动翻译）」双轨。

时间轴准的本质：传统声学模型天然按帧（10-40ms）对齐，毫秒级句边界是行业常规能力。我们原来差在「LLM 听音频」路线（Gemini/agy 时间戳只能到 1-2 秒粒度、还整秒取栅格）。**换成真 ASR，时间轴问题自然消失。**

---

## 2. 聚合免费接口：三家实测（2026-07-04，李豆沙 7/2 录播）

三家都是官方剪辑产品内置的免费转写接口（社区逆向：SocialSisterYi/bcut-asr、WEIFENG2333/AsrTools+VideoCaptioner），无需账号登录。

| 接口 | 归属 | 中文可用性 | 依赖 | 速度（2.5min段） | 结论 |
|---|---|---|---|---|---|
| **bcut** | B站必剪 | ✅ 极佳（AI字幕同源） | 无（仅 UA 头） | 7-10s | **首选主源** |
| **jianying** | 剪映 | ✅ 极佳（与 bcut 相当） | 第三方签名服务器 + 客户端版本号 | 8-14s | **备源（failover）** |
| **kuaishou** | 快手快影 | ❌ 接口已下线 | 无 | — | 返回 `501 效果subtitle_generate禁用`，移除 |

### 速度与规模（free 主机，欧洲 IP，无 geo 限制）

| 输入 | bcut | jianying |
|---|---|---|
| 2.5 分钟杂谈段 | 6.6s / 37 条 | 13.9s / 41 条 |
| **30 分钟生产源段整段直喂** | **17.3s / 516 条** | （同量级） |

30 分钟音频 17 秒出全场毫秒级 SRT——对比现在 agy 精听同段要「分钟到小时级」且时间轴粗。这是数量级的改善。

### 中文质量对照（604-756s「一眼AI破防」段 vs 现有 agy 管线）

```
agy refined（现状，2s 栅格、句被拆碎）：
  00:22->00:24  好像Ado
  00:24->00:28  这真的不是融
  00:28->00:36  了Ado吗
bcut（毫秒时间轴、整句边界）：
  00:34,640->00:38,520  好像阿斗哈哈哈
  00:38,520->00:41,260  这真的不是融了阿斗吗
jianying（毫秒、整句）：
  与 bcut 几乎一致（"吸铁石会梦见树上虎鲸吗" 逐字相同）
```

通用文本 bcut/jianying 都优于 agy，时间轴则是碾压。唯一短板是**梗词/专名写同音字**（Ado→阿斗），这正好是现有 jingting 词表纠错的强项——**分工不变：新接口出准时间轴+draft，jingting 只做词表纠错（其本就"只改字不动时间"，`validate_same_timing` 强制）**。

### 日语实测：三家全部不可用 ❌

用 ASR-orchestra 的日语素材（maid ASMR、jarinko 关西腔动画）实测：

```
jarinko 日语动画对话原文（应为日语台词）：
  bcut     → "斯蒂卡巴尼欧西德·阿斯塔诺夫"、"爱德公的阿卡巴基"   （中文谐音乱码）
  jianying → "i don't know how much i can get that's why i'm the only one"  （幻觉成英文）
```

**bcut 是纯中文模型**（`model_id=7`，无语言参数），日语强行谐音成中文。**剪映免费接口层不暴露语言参数**，日语被当英文幻觉。二者对日语零价值。

→ **对 ASR-orchestra 的日语转录：这些接口帮不上忙，继续用 orchestra 自己的日语栈**（whisper large-v3 / kotoba-whisper / parakeet-ja 等）。若将来要给这台机器加本地日语 ASR，按调研走 `sherpa-onnx + nvidia/parakeet-tdt_ctc-0.6b-ja int8`（8 核 RTF≈0.11，JSUT CER 6.4，token 级时间戳，一次 pip 可装）。

---

## 3. 用法

```bash
# auto = bcut 主，失败自动 failover 到 jianying（kuaishou 已下线，不在链上）
python3 scripts/free_asr_client.py input.mp4 --srt out.srt --json out.json
python3 scripts/free_asr_client.py input.mp4 --provider bcut --srt out.srt
python3 scripts/free_asr_client.py input.mp4 --provider jianying --srt out.srt
# out.json 保留逐字毫秒时间戳（bcut/jianying 都给 words[]），后续可做精确断行/卡拉OK
# --offset-ms 用于分块转写回拼时间轴
```

零第三方依赖；内置 412/429/5xx/-509 限流指数退避。

---

## 4. 集成到现有管线（下一步，本次未改生产代码）

现在字幕的**文本和时间轴都出自 agy**（`gemini_slice_jingting.py`），把「时间轴来源」换成聚合 ASR 即可，下游全不动：

- **选题/语义召回**：`run_full_session_selector_cpa_shadow.py --source-srt` 换成聚合 ASR 产物（30min 源段 17s 出全场，语义 lane 输入质量+时间锚点全面升级）。
- **切片 daemon**：`gemini_slice_jingting.py` 的 `find_srt()` 消费 `<slice>.srt` / `subtitles/<base>.srt`，无 SRT 即 skip。在切片落地后先跑 `free_asr_client.py` 写出该 SRT 即接入；**agy 从"听音频定时间轴"降级为可选的纯文本词表纠错**（甚至可去掉，靠 jingting 的 CPA 词表纠错）。耗时从分钟-小时级 → 秒级。
- **排版**：现有 ≤28 字/≤2 行逻辑不变；将来可用 JSON 逐字时间戳做精确断行。
- **歌切**：bcut 对唱歌段稀疏且歌词多误听（B站站内对音乐行也特殊处理，字幕 JSON 有未文档化 `music` 字段）。**歌切字幕继续走 LRC 对齐流程，聚合 ASR 只作参考轨。**

---

## 5. 风险与预案

- **非公开逆向接口，约半年变一次**（bcut：2024-12 加 UA 风控 / 2025-03 加必填字段 / 2025-08 412 收紧 / 2025-11 返回结构变；剪映：`appvr` 版本号+签名规则会随剪映升级变，本次实装已追到 `6.6.0` 且修了 commit-PUT 的 400 fail-soft）。**对策**：客户端 fail-fast + 管线原有 fail-closed 兜底（无字幕→不出片），坏了照 bcut-asr / VideoCaptioner 仓库最新代码修。
- **剪映的单点**：签名靠第三方服务器 `asrtools-update.bkfeng.top`（bkfeng 自建，Cloudflare 后需浏览器 UA），该域名挂掉剪映即失效——所以剪映只作 bcut 的 failover 备源，**不作无人值守主链路**；主链路是无第三方依赖的 bcut。
- **限流**：-509 为 IP 维度频控，无账号无封号面；单场直播几个 30min 段远够不到，客户端已带退避。
- **接口全死的 B 计划**（调研完成，按 Ivan 指示不预建）：本地 FunASR Paraformer-large onnx int8，中文 CER ~2%、字级时间戳 AAS ~71ms、free 8 核 RTF 0.01-0.05，`pip install funasr` 当天可恢复。

---

## 6. 接口契约（便于接口变动时快速修）

### bcut（必剪）— 全程带 `User-Agent: Bilibili/1.0.0`
Base `https://member.bilibili.com/x/bcut/rubick-interface`：
1. `POST /resource/create`（form: type=2, name, size, resource_file_type∈{flac,aac,m4a,mp3,wav}, model_id=7）→ `in_boss_key, resource_id, upload_id, upload_urls[], per_size`
2. `PUT` 各分片到 `upload_urls[i]`，收 `Etag`
3. `POST /resource/create/complete`（form: in_boss_key, resource_id, etags 逗号拼, upload_id, model_id）→ `download_url`
4. `POST /task`（json: `{"resource": download_url, "model_id": "7"}`）→ `task_id`
5. `GET /task/result?model_id=7&task_id=...` 轮询；state 4=完成，`result` 为 JSON 字符串 `{"utterances":[{"transcript","start_time","end_time","words":[{"label","start_time","end_time"}]}]}`（毫秒）

### jianying（剪映）— 签名靠第三方服务器
- 签名：`POST asrtools-update.bkfeng.top/sign`（body: url, current_time, pf=4, appvr=**6.6.0**, tdid；需浏览器 UA + tdid/t 头）→ `sign`
- 上传：`/lv/v1/upload_sign` 拿临时 AK/SK/token → `ApplyUploadInner`（AWS SigV4 到 `vod.bytedanceapi.com`）→ PUT 分片 → POST check → **commit-PUT（常 400，需 fail-soft 无视）**
- 任务：`POST /lv/v1/audio_subtitle/submit` → `POST /lv/v1/audio_subtitle/query` 轮询 → `data.utterances[].{text,start_time,end_time,words[]}`（毫秒）
- 关键坑：`appvr` 与签名绑定，剪映升级即变；tdid 有年份相关生成规则；commit-PUT 的 400 是正常的。

---

## 7. 来源

- Index-ASR 报告 https://arxiv.org/abs/2601.00890 ；2023 代系统 https://www.6aiq.com/article/1677688861215
- 智能字幕上线 https://www.bilibili.com/read/cv14325585 ；自动翻译字幕 https://www.bilibili.com/read/mobile?id=17581547
- bcut 逆向与维护 https://github.com/SocialSisterYi/bcut-asr （issue #12 历次风控变动）
- 剪映/快手实现 https://github.com/WEIFENG2333/AsrTools ；https://github.com/WEIFENG2333/VideoCaptioner （`videocaptioner/core/asr/jianying.py`，appvr 6.6.0）
- 本地兜底选型 https://github.com/modelscope/FunASR ；日语 https://k2-fsa.github.io/sherpa/onnx/pretrained_models/offline-ctc/nemo/japanese.html
