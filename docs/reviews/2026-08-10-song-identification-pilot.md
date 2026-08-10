# 歌名识别方案金丝雀（2026-08-10）

**结论先行：Shazam 型指纹识别对李豆沙的直播翻唱不可用，不能当主路。**
Ivan 提的另一条——**Gemini 听音频猜**——应当作为主路；本仓**已有**的 LRC 逐行匹配可当验证器。

## 为什么要做这个金丝雀

Ivan 令（2026-08-10）：

> 李豆沙唱日语歌很多，绝不能用 BCUT 的中文 ASR 猜歌，应该用 Gemini 猜；
> 更好的是接免费听歌识曲 API —— 去查 GitHub 项目。

"更好的是接听歌识曲 API" 是个**可证伪的前提**。在照着实现之前先证伪它，比实现完再发现不能用便宜得多。

## 实验设计

候选实现：[ShazamIO](https://github.com/shazamio/ShazamIO)（Python，逆向 Shazam API，免费）。
同类还有 SongRec（Rust）、vibra（C++），**都基于同一套 Shazam 声学指纹**，
所以对 ShazamIO 的结论对它们同样成立。

被测音频：抢救回来的《心型病毒》成品包（`song_210131_1210`，全长 138.97s），
用 ffmpeg 抽 4 段 12 秒单声道 16kHz WAV：`0s / 8s / 45s / 100s`。

**关键方法论：先立阳性对照。** 没有阳性对照的阴性结果等于没有结果——
"没匹配上"可能只是我的 harness 坏了、或者 API 变了。

## 结果

| 音频 | 性质 | 结果 |
|---|---|---|
| `RSP - 優しい詩。` 50-62s | **日语商业录音室单曲** | **MATCH** `'Yasashiiuta.' / 'RSP'` |
| 心型病毒 0-12s | 李豆沙直播翻唱 | NO MATCH |
| 心型病毒 8-20s | 同上 | NO MATCH |
| 心型病毒 45-57s | 同上 | NO MATCH |
| 心型病毒 100-112s | 同上 | NO MATCH |
| `双笙 - 扬州姑娘` 40-52s | 中文翻唱 | NO MATCH |
| `泠鸢yousa - 本色` 40-52s | 中文翻唱 | NO MATCH |
| `Nerissa Ravencroft - Down By The River` 50-62s | 独立厂牌原创 | NO MATCH |

阳性对照通过，且 API 返回结构完好（`{"matches": [], "tagid": ..., "retryms": 12000}`
——是**合法的零匹配应答**，不是报错），所以阴性结果是真的。

## 为什么（机理，不是玄学）

Shazam 型指纹匹配的是**近乎同一份录音**的频谱峰值星座图。
一旦速度、调、配器或编排变了，指纹就不成立——翻唱恰好把这几样全变了。
直播翻唱还叠了两层：伴奏用的是**另一份 off-vocal 录音**（本身就不是原版母带），
人声是另一个人。

推论：**这不是选错库，是选错了整类算法。** 换 SongRec / vibra 不会改变结论，
因为它们复刻的是同一套指纹。

## ⚠️ 更正：Gemini 听音频识别**已经存在，而且是对的**

初稿把"主路改成 Gemini 听音频"写成了新方案。**这是错的，Ivan 当场指出。**
查真实产物（心型病毒 `lyrics-alignment-report.json`）：

```
song_title               = "心型病毒 (Live)"
artist                   = "SNH48祁静"
evidence_source          = "agy_audio_lrc"          ← 音频，不是画面
audio_alignment_provider = "gemini_api"
audio_alignment_model    = "gemini-3.6-flash"       ← 就是 Gemini 在听
source_ref               = "netease://song/536937431"
matched_line_ratio       = 1.0                      ← 逐行 100% 匹配
```

**这条链已经在跑，而且给出的曲名完全正确。** 所以本条的缺陷不是"缺能力"，是**次序与授权**。

## 真正的缺陷：垃圾名字在权威识别之前就定型并外流

歌名有**两个**来源，早晚不同、可靠性相反：

| 来源 | 写入点 | 时机 | 质量 |
|---|---|---|---|
| 画面 OCR（Gemini 读屏幕点歌板） | `visual_song_discovery.py:73` | **早** | 产出 `na`/`Leee`/`町`/`中生` 这类垃圾 |
| 音频识别（AGY/Gemini 听 → LRC 匹配） | `song_repair.py`（多处 `lrc.song_title`） | **晚**，在歌 lane 里 | 正确，带 netease 源引用与行匹配率 |

后果有二，都在《心型病毒》这条上实测到了：

1. **候选在权威识别之前死掉，垃圾名就是最终名。** free 那条 `blocked` 在 host-vocal 就停了，
   永远没跑到 `agy_audio_lrc`，所以 `hook` 里留着《**新**型病毒》。
2. **垃圾名会反过来污染检索。** `song_lane.py:484` 的 LRC 查询
   `query = boundary.song_title or result.title` —— 名字错了就去搜错的歌。

而《心型病毒》→《新型病毒》**不是 LLM 犯傻，是同音**（`pypinyin` 实测两者都是
`xin xing bing du`）。**纯文本侧无论多聪明都救不回来**，只有听音频那条链能定，
而它确实定对了（netease://song/536937431）。

## 该怎么做（据此重排）

1. **按 Ivan 的方案改次序**：一旦判定这段**可能是歌**，就把命名权从
   BCUT 中文 ASR / 画面 OCR **切到音频识别那条链**，让它在早期就成为命名权威，
   而不是等到歌 lane 末尾才纠正。日语歌尤其必须——中文 ASR 对日语全盲。
2. **验证器已有，不需新造**：LRC 逐行匹配率（本例 `matched_line_ratio=1.0`）。
   猜错了行匹配率会塌，是天然 fail-closed 判据。
3. **禁止**用 BCUT 中文 ASR 猜日语歌名（Ivan 明令）；画面 OCR 同理不应作为身份权威，
   降级为候选提示即可。
4. Shazam 型指纹**最多**当一个廉价可选前置：只有"播放原曲录音"（纯 BGM）才可能命中，
   对唱歌场景没有价值。**不建议**为它引入依赖。

## 复现

```
ffmpeg -v error -y -ss 50 -t 12 -i <原曲> -vn -ac 1 -ar 16000 pos.wav
python -c "import asyncio;from shazamio import Shazam;\
print(asyncio.run(Shazam().recognize('pos.wav')))"
```

环境注意：`shazamio` 依赖 `pydub` → `audioop`，**Python 3.13+ 已移除 audioop**；
在 3.14 上装 `audioop-lts` 会 segfault（实测 exit 139）。用 **Python 3.12** 跑。

## 我没做 / 边界

- 没测纯伴奏间奏段（`seg_0` 已是最接近前奏的一段，NO MATCH）。即便某段能命中伴奏，
  也只能证明伴奏来源，不能证明这一段是**李豆沙在唱**——本人演唱仍归声纹自证那条链。
- 没测 ACRCloud 等**付费**的 cover/humming 识别模式。它们的确针对翻唱设计，
  但要单独计费与 ToS 审查，超出"免费 API"的题面。要不要评估由 Ivan 定。
- 没有实现任何代码，本文只定方案与否决一条前提。
