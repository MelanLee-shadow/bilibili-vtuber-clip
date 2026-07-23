# 2026-06-29 李豆沙合唱直播切片工作流校正 Spark Spec

> **SUPERSEDED design snapshot，禁止照抄命令。** 本文只保存当时事故与方案；当前
> song/package 规则从 [../pipeline/README.md](../pipeline/README.md) 进入。

## 目标

重新校正 2026-06-29 李豆沙合唱直播的自动切片/本地 review 工作流，避免上一轮把“候选包补齐成可播放文件”误报为“按项目成品工作流交付”。

用户明确要求：

1. 字幕是第一优先级，字幕必须准确。
2. 必须严格遵守项目 skill，尤其是 `.agent/skills/song-lyrics-timeline-aligner/SKILL.md`。
3. 歌曲/合唱片段不能用 ASR/Jingting 时间轴冒充歌词时间轴。
4. 字幕必须满足项目行长/可读性限制。
5. 封面需要生成 AI 封面图，并嵌入标题；不能只用视频帧模板代替。
6. 成品应包含完整标题、准确字幕、AI封面+标题、烧录视频、证据/manifest。
7. 不上传。

## 上一轮错误复盘

### E1. 把 review packaging 当成 final workflow

上一轮执行的是：

```text
已有自动候选 FLV/Jingting SRT/publish.json/cover
  -> 复制/normalize SRT
  -> 简单 SRT->ASS
  -> ffmpeg 烧录
  -> 用视频帧模板替换坏封面
  -> 拉回本地
```

这只能得到“可打开的 review 包”，不能叫项目成品工作流。

### E2. 字幕时间轴不合格

对歌曲/合唱类片段，项目-local skill 的核心规则是：

```text
trusted timed lyrics -> identify first sung lyric in clip -> global shift -> verify tail -> preview/burn
```

上一轮没有做这些：

- 没有查外部 timed lyric / LRC / 官方字幕。
- 没有识别首句歌词 anchor。
- 没有检查尾句 lyric anchor。
- 没有记录 offset/tail delta/stretch evidence。
- 直接使用 Jingting/ASR SRT 的 cue timing。

这违反 `.agent/skills/song-lyrics-timeline-aligner/SKILL.md` lines 10-17, 24-42, 50-56。

### E3. 行长/屏显限制没被执行

上一轮虽然生成了 ASS，但只是把 SRT text lines 直接转 Dialogue，未做项目行长/viewability gate。

实测上一轮本地包：

- `138s_semantic...ass` 存在 78 字 Dialogue：
  `剩女是一个组织对剩女...你的这个百合厨`
- `366s_hybrid...ass` 存在 43 字 Dialogue：
  `谢谢cyn说 送个superchat 今天...`
- 多个 ASS dialogue 超过 18 字屏显阈值。

这违反“字幕必须准确且可读”的交付要求。SRT parseable 不等于可看。

### E4. 字幕 style 不符合既有发布级样式

既有 6/19 approved artifact 使用：

```text
final-sapphire72.ass
Fontsize=72
Style: white_fill_sapphire_blue_outline
```

上一轮生成：

```text
sapphire48.ass
Fontsize=48
```

这不是既有发布级样式，至少不能未说明地冒充 final。

### E5. 封面 workflow 错误

既有手动/发布级证据链：

```text
cover_generation.workflow = CPA/OpenAI-compatible image edit service
method = images.edit
model = gpt-image-2
reference_image = identity reference
ai_background = covers_ai_original/*.cpa-gpt-image-2.png
final_cover = AI background + embedded title text
cover_text = title without prefix
fallback_used = false
```

上一轮发现自动封面里有错误外部人物后，改成了 deterministic frame cover。这只能当 fallback/review-only，不符合用户要求的“生成 AI 封面图和嵌入标题”。

### E6. 候选选择策略错误

上一轮按“非 generic 标题”选候选，排除了标题为“弹幕密度高”的实际歌曲片段。对合唱/歌曲直播，这个策略不对。

正确逻辑：

- 对歌曲直播，先识别前景唱歌 span。
- 如果候选 materially overlaps song，则 song is atomic：扩到完整歌曲，或者 BLOCK。
- generic title 不是排除理由；它只是说明标题生成未完成。

## 正确工作流

### Stage 0: 只读盘点

输入：

- `/app/Videos/22966160/2026-06-29/sources/*.mp4`
- `/app/Videos/22966160/2026-06-29/sources/*.srt`
- `/app/Videos/22966160/2026-06-29/*.slice_candidates.json`
- existing candidate FLV/SRT/cover/publish/evidence

输出：候选 inventory，包括：

- source recording segment
- source start/end
- candidate strategy: semantic/danmaku/hybrid
- whether foreground singing/song
- existing title quality: generic / hook / song title
- existing subtitle source: coarse / Jingting / manual / lyrics-aligned
- existing cover source: auto / AI / fallback

### Stage 1: 分类，不直接烧录

每个候选先分类：

1. `song_full_candidate`
   - 前景唱歌/合唱/歌词连续。
   - 必须走歌词时间轴对齐。
2. `talk_candidate`
   - 纯聊天/吐槽/事故反应。
   - 可走 ASR/Jingting，但仍需 viewability 和 timing check。
3. `mixed_song_talk`
   - 歌前/歌后 talk + 歌曲。
   - 要决定是做 talk clip 还是 full song clip；不能随机截 90 秒。
4. `reject_or_block`
   - 歌曲片段不完整、歌词源找不到、标题/封面无法生成、或字幕无法校准。

### Stage 2: 字幕优先 gate

#### Song candidate 字幕 gate

必须满足：

- 有外部 timed lyrics / official captions / trusted LRC。
- 记录 lyric source URL/path。
- 记录 first lyric external time。
- 记录 first lyric clip-local time。
- 计算 offset。
- 记录 tail lyric external/clip-local time。
- tail delta 在可接受范围；否则重查版本，不能直接 stretch。
- SRT monotonic / no overlap / no zero duration。
- 不跨 10s+ instrumental gap 悬挂歌词。
- 输出 alignment report JSON。

不满足则 `BLOCK_LYRICS_ALIGNMENT_REQUIRED`，不得烧录成品。

#### Talk candidate 字幕 gate

必须满足：

- 使用 refined transcript，不直接信 coarse ASR。
- 对 source media spot-check timing：至少开头、中段、尾部。
- cue min duration >= 0.5s，除非有显式例外。
- no overlap。
- no duplicate adjacent cue。
- 行长限制：每屏每行建议 <= 14-16 CJK；硬上限 18 CJK-equivalent。
- ASS text 中每个 visual line 也必须满足行长，不只检查 SRT 行。
- 术语 lexicon 清洁：`天不熊/kimo熊/给我小` 不泄露。

### Stage 3: 标题 gate

- Title 必须是完整 B站标题。
- 李豆沙歌类标题使用 `【李豆沙】豆沙歌，...`。
- 非歌切片使用 `【李豆沙】<hook>`。
- cover_text = 去掉 `【李豆沙】` / `【李豆沙】豆沙歌，` 后的短标题。
- title/cover_text/publish JSON/manifest 必须一致。

### Stage 4: AI 封面 gate

发布级/成品级封面必须：

- 生成 AI background。
- 保存 `covers_ai_original/`。
- 有 reference image / prompt / model / method / sha256。
- 把 cover_text 嵌入最终 cover。
- final cover 1920x1080。
- contact sheet 视觉检查通过。

确定性视频帧封面只能标记为 `fallback_used=true`，不能冒充 AI cover。

### Stage 5: 烧录 gate

- 使用 approved ASS style。
- 对 720p 李豆沙成品，若既有同类 artifacts 用 `final-sapphire72`，默认跟随，不降到 48。
- ffmpeg burn 后必须 ffprobe video/audio。
- 抽帧检查字幕存在且不挡脸/不超长/不乱码。
- 如果字幕、标题、封面任一变动，必须重烧/重算 hash。

### Stage 6: Review package gate

本地包必须包含：

```text
media/*.burned-final-sapphire72.mp4
subtitles/*.final.zh.srt
ass/*.final-sapphire72.ass
covers/*.cover.png
covers_ai_original/*.png
cover_refs/*.png
publish/*.publish.json
publish/*.title.txt
evidence/*.evidence.json
lyrics_alignment/*.json   # song candidates
README_review.md
review_manifest.json
index.html
contact_sheet.jpg
```

Manifest validation 必须区分：

- `subtitle_timing_verified: true/false`
- `line_length_gate_passed: true/false`
- `lyrics_alignment_verified: true/false/not_applicable`
- `ai_cover_generated: true/false`
- `cover_title_embedded: true/false`
- `burned_video_has_audio_video: true/false`
- `upload_performed: false`

## 重新执行 6/29 的推荐顺序

1. 不继续使用上一轮 `auto_finished_review_20260630_collab` 作为成品；标记为 `invalid_review_draft`。
2. 重新 inventory 所有 6/29 candidates，包括 generic song candidates。
3. 先选 1 条最强 `song_full_candidate` 或 1 条最强 `talk_candidate` 做 gold-path 修复，不要一次修 8 条。
4. 如果是 song：找外部 timed lyrics，按 skill 生成 alignment report + final SRT。
5. 如果是 talk：做 refined transcript timing spot-check + line wrapping。
6. 生成 AI cover + embedded title。
7. 使用 final-sapphire72 ASS 烧录。
8. 做 contact sheet + playback/ffprobe/line-length/term gates。
9. 只在一个样本通过后再批量扩展。

## 不做的事

- 不上传。
- 不把 fake CPA 或 generic候选标题当发布级质量证明。
- 不用 deterministic frame cover 冒充 AI cover。
- 不用 ASR 歌词时间轴冒充 timed lyric alignment。
- 不把 parseable SRT 当可读字幕。

## Multiagent / Pro consultant fan-in

三个只读审阅代理一致确认：上一轮根本错误不是某个 artifact 小瑕疵，而是把“已有 auto-slice 产物重新打包”误当成“歌曲/合唱成品生产流程”。

补充硬证据：

- `tmp/build_20260629_finished_package.py` 的历史 builder 只做 `normalize_srt()` 和 `srt_to_ass()`，没有 lyric alignment、行长 gate、AI cover generation。
- 成品包 manifest 的 `source_srt` 指向 `.jingting.srt`；包内没有 `.lrc`、`lyrics_alignment/*.json`、`clip_first`、`clip_last`、`offset`、`tail_delta`。
- `447s_hybrid...`、`1629s_hybrid...`、`43s_hybrid...` 是 song/mixed song 风险片段，但没有 song workflow evidence。
- `447s...srt` 存在 `词曲 陈绮贞` 挂约 29.98s；`1629s...srt` 存在日文歌词长挂约 22.52s；`43s...srt` 尾部歌词 cue 挂约 29.98s。
- `138s...srt` 有 8 行 cue；`366s...srt` 有 4 行 cue；`583s...srt` 有 3 行 cue。
- 歌曲标题没有进入 `【李豆沙】豆沙歌，...《歌名》...` 模板；例如 `447s` 应先识别《旅行的意义》，`1629s` 应先识别《Fly Me to the Star》后再定标题。
- `title.txt`、`publish.json`、cover text 不是同一 source of truth。
- deterministic frame cover 只能是 review fallback；不能替代 AI background + embedded title 的 finished cover chain。

## 程序化审计 gate

新增 gate：`scripts/audit_lidousha_review_package.py`。

当前上一轮包审计结果：

```text
passed False
blocking 54
issues 55
SUBTITLE_LONG_STATIC_CUE: 8
PUBLISH_TITLE_TXT_MISMATCH: 8
COVER_FALLBACK_NOT_FINISHED: 8
AI_COVER_EVIDENCE_MISSING: 8
SUBTITLE_CUE_TOO_MANY_LINES: 5
SONG_USES_JINGTING_SRT: 4
SONG_LYRIC_SOURCE_MISSING: 4
SONG_ALIGNMENT_REPORT_MISSING: 4
SONG_TITLE_FORMAT_INVALID: 4
PACKAGE_MARKED_INVALID_REVIEW_DRAFT: 1
SUBTITLE_LINE_TOO_LONG: 1
```

这个 gate 应在任何 `finished`/`review` 包对外报告前运行；失败时只能报告为 draft/block，不能叫 finished。

## 验收标准

一个 6/29 成品样本只有在以下全通过时才能叫“按项目工作流生成”：

- 字幕文本准确，时间轴经首/中/尾 spot-check 或 lyric alignment 验证。
- 行长 gate 通过，ASS visual lines 不超限制。
- song candidate 不直接使用 `.jingting.srt` 作为歌词时间轴；必须有 lyric source + alignment report。
- 标题完整且 cover_text / publish.json / title.txt 一致。
- 歌曲标题按 `【李豆沙】豆沙歌，...《歌名》...`，除非明确降级为 talk/事故 clip。
- AI cover background 已生成，标题已嵌入，证据链完整。
- deterministic frame cover 只允许作为 `review_fallback`，不允许通过 finished gate。
- burned MP4 使用 approved ASS style，ffprobe audio/video 通过。
- contact sheet 视觉检查通过。
- review_manifest 明确 no-upload。
- `scripts/audit_lidousha_review_package.py <package> --json` 返回 `passed: true`。
