# 2026-08-08 说话人分离 SOTA 调研：CAM++ 二分架构的升级候选

> 范围：为 `src/autoslice/speaker_finalizer.py` + `src/autoslice/speaker_host_evidence.py`
> 当前架构（CAM++ enrollment 相似度逐 cue 打分 + `_two_means` 二均值聚类 + policy gate，
> `assets/lidousha/voiceprint_profile.v1.json` `talk_speaker_policy`）调研 2025-2026 学界/工业界
> 对**同一问题形状**（binary-with-enrollment：单一登记目标 vs 其余一切）的现状。Read-only 调研，
> 不改代码、不改阈值，仅落盘本文档。

## 结论先行：Top 3 候选

| 优先级 | 候选 | 一句话理由 |
|---|---|---|
| 1（先跑） | **CAM++ → ERes2NetV2 embedding 替换**（同 3D-Speaker/modelscope 家族，`iic/speech_eres2netv2_sv_zh-cn_16k-common`） | 同一套 loader/enrollment/two-means 架构不用动，CNCeleb EER 6.78%→6.14%（相对 -9.4%），且该模型专门为短时长设计；用现有 61-cue/40-cue 真值直接重放即可验证，是全部候选里集成成本最低的一个 |
| 2（第二个 pilot） | **sub-cue windowing 层**（pyannote `segmentation-3.0`/`community-1` 或 3D-Speaker 自带 `--include_overlap` 输出切分窗口，喂给现有 CAM++/ERes2NetV2 打分器，接入已经预留的 `mixed_cue_speaker` seam） | 不换声学核心、不换 policy gate，只在 `speaker_host_evidence.py:142` 已经写明"未来子 cue 音频分窗"的接口处补真实实现，直接针对 mixed cue（~10%）和 overlap（当前完全未处理）两个失败面 |
| 3（架构级，成本最高） | **frame-level TS-VAD / personal-VAD 风格目标说话人模块** | 唯一能同时解决短 cue、mixed cue、overlap 三个失败面的架构类别（帧级输出没有 1.5s 门槛，天然支持多标签重叠），但没有现成的"任意中文目标人 + 单人 enrollment"预训练权重，本质是要建一个新模型，不是换一个 checkpoint |

**不选 NeMo Sortformer/pyannote/DiariZen 作为直接替换的原因**：这三个是目前最强的**开集聚类式** diarization pipeline，但都不原生支持"给一段 enrollment 音频，直接吐 target-vs-other 二分"——都需要在其聚类结果之上再接一层我们现在就在做的余弦匹配。换句话说，我们现有架构（CAM++ + 自定义阈值）已经走在这几个系统能提供的**唯一实用路径**上；SOTA 增益点在"用哪个 embedding/哪层前处理"，不在"整体换一条 pipeline"。详见第 1 节。

---

## 0. 现状锚点（供后文对照，均来自本仓库既有真值，非本次新测）

- 架构：CAM++（`iic/speech_campplus_sv_zh-cn_16k-common`，Interspeech 2023，[arXiv:2303.00332](https://arxiv.org/abs/2303.00332)）逐 cue 提取 embedding，与登记 host 声纹算余弦相似度得 `margin`，`_two_means(margins)` 在**单场次自身**分布上二均值分割出 `threshold`，`short_cue_ms=1500` 以下和 `|margin-threshold|<ambiguity_band` 一律转入语义佐证带（`src/autoslice/speaker_finalizer.py:872-883`）。
- 已测得的失败面（`docs/reviews/2026-08-07-speaker-crosssession-eval.md`）：
  - 短 cue（<1.5s）在游戏语音场次占比未知，但在 equal-quality 场次的 40-cue 候选上，纯 margin 临界带 17.5% vs 含短句强制后 42.5%——短句强制让近半数 cue 必须依赖语义佐证，而这部分语义投票在该候选上 0 命中 HOST。
  - mixed cue（6/61 ≈ 9.8%，任务描述里的"~10%"）目前无子 cue 音频分窗，`mixed_cue_speaker`（`src/autoslice/speaker_host_evidence.py:142`）在所有窗口非一致同意时统一退化为 GUEST——是个占位实现，注释原文自称"reusable combinator seam for a future per-cue audio-window scorer"。
  - 声学分离度：equal-quality 候选 HOST−GUEST margin 分离度 0.3908（机器标签自洽），游戏语音候选（Ivan 真值分组）0.4481——equal-quality 场次分离度确实更薄。
  - Overlap（真正意义上的重叠语音，非仅快速轮换插话）：无任何处理路径，架构假设每个 cue 只有一个"窗口"。
  - 语义/LLM 兜底：`docs/reviews/2026-07-12-speaker-phase1-diagnostic.md`/`2026-07-12-speaker-binary-v2-blocker.md` 记录纯文本 whole-clip context 独立准确率 `29/49=59.18%`、另一批 holdout `42/52=80.77%`——2026-08-07 裁定据此把语义投票从"可独立定案"降级为"仅在声学临界带内佐证 HOST，不能反向独立定案"（`src/autoslice/speaker_finalizer.py:890-892` 注释直接写明是 Ivan 2026-08-07 的决定）。

这些数字是本次调研评估候选"预期收益"的基准，不是本次新跑的实验。

---

## 1. Target-speaker VAD / Personal VAD 谱系：谁原生支持"enrollment→二分"

### 1.1 血统脉络

Personal VAD 是源头：给定一个说话人的声纹（acoustic footprint），只做单目标"是/否在说话"判定，不建模说话人之间关系。TS-VAD（[Target-Speaker VAD, 2020](https://arxiv.org/pdf/2005.07272)）把它扩展为同时接收多个说话人 profile、并行预测各自活动，早期用 i-vector + MFCC 输入、说话人数固定，后来演进到 LSTM/Transformer 处理可变说话人数。2025 年的 **TS-VAD+**（APSIPA 2025，[论文](http://www.apsipa.org/proceedings/2025/papers/APSIPA2025_P333.pdf)）把 TS-VAD 模块化，在 AliMeeting、DIHARD-III 上做了规模可扩展性分析——这条线仍然活跃，但发布的都是"针对已知说话人数据集训练/评测"的研究代码，没有找到"任意登记单一目标 + 中文"的现成权重。

另一条相关但方向不同的 2026 年 1 月新论文——[Adaptive Speaker Embedding Self-Augmentation for Personal VAD with Short Enrollment Speech](https://arxiv.org/pdf/2601.12769)——处理的是"enrollment 音频本身很短"（而非我们的问题：enrollment 音频充足，是**待判定的 cue** 很短），用混合语音里挑出的关键帧去增强 enrollment embedding。方向相邻但不是同一问题，仅供谱系参考。

### 1.2 逐系统核查："原生 enrollment→binary" 有没有"

| 系统 | 是否原生支持 enrollment 二分 | 依据 |
|---|---|---|
| **NVIDIA NeMo (Streaming) Sortformer** | **否**。Arrival-Order Speaker Cache 动态发现说话人，"eliminates the need for explicit speaker queries or enrollment audio"；上限 4 人，说话人是运行时才确定的匿名 ID | [HF model card](https://huggingface.co/nvidia/diar_streaming_sortformer_4spk-v2)、[MarkTechPost](https://www.marktechpost.com/2025/08/21/nvidia-ai-just-released-streaming-sortformer-a-real-time-speaker-diarization-that-figures-out-whos-talking-in-meetings-and-calls-instantly/) |
| **pyannote 3.1 / 4.0 (Community-1)** | **否**（开源侧）。3.x 起用 Powerset 多分类统一了 segmentation+overlap，但身份匹配没有官方 API；GitHub Discussion #1667 里维护者认可的做法是社区自建：enroll 参考音频→算 embedding→对 diarization 输出的每个匿名 cluster 算余弦相似度取最近邻，阈值要自己调 | [pyannote-audio#1667](https://github.com/pyannote/pyannote-audio/discussions/1667)、[Community-1 博客](https://www.pyannote.ai/blog/community-1) |
| pyannoteAI **Precision-2**（商业 API） | 是，但只在付费云 API：可上传 enrollment 视频/音频建 voiceprint，diarization 后按最高匹配分打标 | [pyannote 博客](https://www.pyannote.ai/blog/what-is-speaker-diarization) |
| **DiariZen**（WavLM-Large + Conformer + powerset + VBx，ICASSP 2025，"开源 SOTA"自我定位） | **否**。本质是 EEND+聚类，输出匿名 speaker 标签，同 pyannote 一样需要后接身份匹配 | [BUTSpeechFIT/DiariZen](https://github.com/BUTSpeechFIT/DiariZen)、[教程](https://arxiv.org/html/2604.21507) |
| **3D-Speaker / SpeakerLab**（CAM++ 的娘家：ERes2Net、ERes2NetV2、ERes2Net-large） | **是，原生**。这些是纯 verification 模型，输入两段音频输出相似度，enrollment-binary 本来就是它的设计目标——我们现在的架构已经是这条路线 | [3D-Speaker README](https://github.com/modelscope/3D-Speaker/blob/main/README.md) |
| **WeSpeaker 2.x** | 是（verification 工具箱同款设计），但中文侧只有一个 `wespeaker-cnceleb-resnet34`（[HF](https://huggingface.co/Wespeaker/wespeaker-cnceleb-resnet34)），未找到与 3D-Speaker 200k 说话人规模同级的中文旗舰模型；其最强架构（ResNet293, VoxCeleb1-O EER 0.447%）是英文优先 | [wespeaker/README](https://github.com/wenet-e2e/wespeaker/blob/master/README.md) |
| **ECAPA2** | 是（verification 模型），但未找到中文预训练 checkpoint，仅 VoxCeleb 英文 | [arXiv:2401.08342](https://arxiv.org/abs/2401.08342) |
| **SpeakerLM**（Aug 2025 / AAAI 2026，Alibaba 系，Qwen2.5-7B + SenseVoice-large + ERes2NetV2 embedding） | **是，且是本次调研里唯一"联合 ASR+diarization+enrollment"三合一的系统**：`Match-Regist` 模式下预登记说话人，模型直接输出"该片段属于哪个登记说话人"，含 Mandarin 评测（AliMeeting-Eval cpCER 16.05%、AISHELL4-Eval 18.37%） | [arXiv:2508.06372](https://arxiv.org/abs/2508.06372) |

**关键判断**：把"目标说话人验证"当作头等公民、原生支持 enrollment 二分的，只有纯 verification 工具箱（3D-Speaker 家族、WeSpeaker、ECAPA2）和一个 2025 年才出现、还没放权重的 LLM 联合模型（SpeakerLM）。Sortformer / pyannote / DiariZen 这三个"diarization pipeline 派"目前发布的都是无监督聚类或端到端置换消解，身份匹配统一是外挂的后处理——这正是我们现在架构在做的事。SOTA 机会在换 embedding 模型和加前处理层，不在换整条 pipeline。

SpeakerLM 值得关注但暂不列入 Top 3：论文只给了项目页 <https://sites.google.com/view/speakerlm/overview>，未确认 GitHub/HF/ModelScope 权重开放；7B LLM 骨干，训练用 4×A800，推理算力需求明显超出 CPU-ish 宿主机的现实预算。留作观察项。

---

## 2. 短片段 embedding：CAM++ 在 <2s 上到底差多少

3D-Speaker 官方 README 给出的同协议对比表（VoxCeleb1-O 英文 / CNCeleb 中文 / 3D-Speaker 中文数据集，均为**全长**测试）：

| 模型 | 参数量 | VoxCeleb1-O EER | CNCeleb EER | 3D-Speaker EER |
|---|---|---|---|---|
| CAM++（我们的生产模型） | 7.2M | 0.65% | 6.78% | 7.75% |
| ERes2Net-base | 6.61M | 0.84% | 6.69% | 7.21% |
| **ERes2NetV2** | 17.8M | 0.61% | **6.14%** | **6.52%** |
| ERes2Net-large | 22.46M | 0.52% | 6.17% | 6.34% |

来源：[3D-Speaker README](https://github.com/modelscope/3D-Speaker/blob/main/README.md)。CAM++→ERes2NetV2 在 CNCeleb 上相对降 EER 9.4%，在同源 3D-Speaker 数据集上相对降 15.9%。

ERes2NetV2 论文（[arXiv:2406.02167](https://arxiv.org/abs/2406.02167)）的设计目标就是短时长：多尺度特征融合专门针对短语音捕捉说话人特征，论文自报 VoxCeleb1-O **短时长**结果——全长 0.61%、3 秒裁剪 0.98%、2 秒裁剪 1.48%——但这组数字是英文 VoxCeleb 协议，**没有找到 CNCeleb/中文语料下 <2s 专项 EER**，是本次调研没能填上的空白，也正是候选 1 pilot 要优先补的数据。

更早期的文献佐证"短时长是普遍痛点、新架构族确实更抗打"：Res2Net 系相对 ResNet baseline 在 2s/3s/4s 裁剪上分别有 17.6%/19.0%/13.7% 的相对 EER 降低；另有一篇较老的 segment-aggregation 研究报告纯 1 秒测试语音 EER 高达 12.82%（[arXiv:2005.03329](https://arxiv.org/pdf/2005.03329)）——这两个数字来自搜索摘要而非本次直接精读原文表格，置信度低于上面 3D-Speaker/ERes2NetV2 的一手数字，仅作为"短时长确实是行业公认难题、且投入短时长鲁棒性设计有实测收益"的背景支撑，不作为精确基准引用。

ECAPA2（[arXiv:2401.08342](https://arxiv.org/abs/2401.08342)）走的是另一条路：训练策略里专门加了 0.5-2s 随机裁剪采样，在 VoxCeleb1-S（0.5-2s 裁剪版）上相对提升 10.9%——证明"专门为短语音设计训练策略"这条路径本身是有效的，可惜没有中文 checkpoint，只能作为方法论参考，不是直接可部署候选。

**小结**：换 embedding 能拿到的收益是"同架构家族里已发表的相对提升"（CNCeleb -9.4% 到 -15.9%），不是"短 cue 问题被解决"——25% 短 cue 强制进临界带这条策略性规则（`short_cue_ms=1500`）是判定链的独立防线，不会因为换了更强的 embedding 就自动放宽；更好的 embedding 只是让**同样时长**下的 margin 更可信、临界带内需要语义兜底的比例可能下降，但这需要实测验证。

---

## 3. Overlap / mixed cue 处理

现有架构完全没有 sub-cue 音频分窗，`mixed_cue_speaker` 是一个显式占位（`src/autoslice/speaker_host_evidence.py:142` 注释原文承认"this slice does not add new sub-cue audio windowing"）。SOTA 现状：

- **pyannote Powerset**（Bredin, ICASSP 2023，3.x 系起成为 pyannote 分割骨干）：把传统"segmentation→聚类"合并成统一多分类模型，overlap 检测是分割输出的原生部分而非独立后处理，Community-1（pyannote.audio 4.0，2025）在此基础上又新增 "exclusive diarization mode"（任意时刻只保留最可能被转写的一个说话人，专门为下游 ASR 对齐设计）。MIT 协议，权重在 HF 需接受许可协议下载。([pyannote-audio GitHub](https://github.com/pyannote/pyannote-audio)、[Community-1 博客](https://www.pyannote.ai/blog/community-1))
- **NeMo MSDD**：仍在 NeMo 仓库里维护，多尺度聚类初始化+neural refinement 的旧范式；文档没有正式标"deprecated"，但 NVIDIA 2025 年的宣传重心全部转向 Sortformer——Sortformer 本身是"permutation-resolved"端到端架构（[arXiv:2409.06656](https://arxiv.org/pdf/2409.06656)，ICML 2025），通过 Sort Loss 直接从帧级多标签输出里解决重叠，不需要 MSDD 那种多尺度拼接。
- **WEEND**（Google，[speaker-id/publications/WEEND](https://github.com/google/speaker-id/blob/master/publications/WEEND/README.md)）：word-level 端到端联合 ASR+diarization，RNN-T 插入说话人标签 token，在 2 人短对话上超过 turn-based baseline，可泛化到 5 分钟音频；不支持 enrollment，3 人以上明显更难。
- **TagSpeech**（2026 年 1 月，[arXiv:2601.06896](https://arxiv.org/pdf/2601.06896)）：LLM 骨干、时间锚点交织机制，语义/说话人双流解耦，论文自称在重叠语音场景上优于 Qwen-Omni/Gemini baseline，但摘要页没有透出具体重叠 F1/DER 数字，也没找到开源权重信息，不支持 enrollment。
- **SpeakerLM**（见第 1 节）：联合建模天然处理重叠，是本次调研里唯一"重叠处理 + enrollment"同时具备的系统，但同样卡在算力和权重可得性上。
- **WhisperX 2025 状态**：v3.8.6（2025-05）把 diarization 后端从 `pyannote/speaker-diarization-3.1` 切到 `pyannote/speaker-diarization-community-1`（[QWE 安装指南](https://www.qwe.edu.pl/ai-tools/whisperx-speaker-diarization-install/)）。本质仍是 Whisper ASR + pyannote diarization + 强制对齐的级联组合，不是原生 speaker-attributed ASR，重叠处理能力等于继承 Community-1 的 Powerset 输出，没有额外增量。
- **3D-Speaker 自家的 overlap 支持**：diarization pipeline 提供可选 `--include_overlap` 阶段（`local/overlap_detection.py`），但需要 `--hf_access_token`——即底层很可能包了一个 HF 托管模型（推测是 pyannote 系），不是纯 modelscope 自封闭组件。这意味着即便走"同门候选 2"，实际的重叠/分窗检测大概率还是要接 HF 生态。
- 独立的 2025 年重叠检测研究：[Towards Robust Overlapping Speech Detection: A Speaker-Aware Progressive Approach Using WavLM](https://arxiv.org/html/2505.23207)（Interspeech 2025）确认重叠检测本身仍是一个活跃的独立研究方向，还没有"公认最优解"。

**小结**：候选 2（sub-cue windowing）最现实的落地形态是拿 pyannote `segmentation-3.0`/Community-1（或 3D-Speaker 包装的同款）跑每条 cue，得到子窗口边界，再用现有 CAM++/ERes2NetV2 打分器逐窗口独立判定，把结果喂回 `mixed_cue_speaker` 的输入 `window_speakers`——这个函数签名今天就是按这个用法设计的。唯一没有独立验证到的点：pyannote 的 segmentation 组件单独在中文语音上的分窗精度，本次调研只查到完整 pipeline 的中文 DER（见第 5 节表格），没查到分割组件本身脱离聚类之后的独立指标，pilot 时需要先用现有 6 条 mixed cue 真值验证边界质量再决定要不要扩大依赖。

---

## 4. Streaming/incremental（简述，管线是离线批处理，非必需）

- **diart**（2021 年至今，pyannoteAI CTO 原创，[文档](https://diart.readthedocs.io/)）：滚动缓冲区 + 增量聚类，默认接 pyannote.audio 模型，轻量级、仍在维护。
- **pyannoteAI Live-1**（2026-07 商业发布）：亚 300ms 延迟，云端商业产品。
- **NeMo Streaming Sortformer**：Arrival-Order Speaker Cache，面向会议/通话实时场景。

这条线全部针对"边录边判"的实时场景，我们的管线是切片后离线批处理，没有延迟约束，不构成升级动机，本次不深入。

---

## 5. Top 3 候选：集成方案与预期收益

### 候选 1：CAM++ → ERes2NetV2 embedding 替换

| 项目 | 内容 |
|---|---|
| 模型 | `iic/speech_eres2netv2_sv_zh-cn_16k-common`，ModelScope，Apache 2.0，200k 中文说话人训练，17.8M 参数（CAM++ 7.2M，约 2.5x 但绝对量仍小，CPU 可行） |
| 集成点 | `src/autoslice/speaker_finalizer.py` 里加载/推理 CAM++ pipeline 的位置（`_load_campplus_pipeline` 及其调用链，约 L570-590、L712 起的 `_run_campplus_analysis`）——只需换 model_dir/model_id 和对应的 `model_tree_sha256` 校验值，enrollment references、two-means、policy gate 全部不动 |
| 针对的失败面 | 部分缓解失败面 1（短 cue，架构上为短时长设计但中文短时长数字未验证）、部分缓解失败面 3（equal-quality 薄 margin，更好 embedding 理论上能拉开分离度但需要实测） |
| 不解决的失败面 | 2（mixed cue 分窗）、4（overlap）——这两个是判定链结构问题，不是 embedding 精度问题 |
| 预期收益（有据可查部分） | CNCeleb EER 6.78%→6.14%（相对 -9.4%），3D-Speaker 数据集 EER 7.75%→6.52%（相对 -15.9%）,均为全长测试、非本仓库场次上的数字 |
| 先跑什么 | **本次调研建议的第一个 pilot**：用现有两个已有逐 cue 真值的场次——`auto_203735_555_680`（61-cue Ivan 真值，游戏语音）和 `auto_200511_61_138`（40-cue equal-quality，待人工核对）——重新提取 cue 音频并过 ERes2NetV2，按 `tests/lidousha/test_speaker_host_evidence.py` 同一套回放方法重算 false-host/false-guest。注意现有 fixture 只持久化了 margin/threshold，没有存 embedding 或原始 cue 音频路径，pilot 需要重新切 cue 音频，不能直接复用 fixture |

### 候选 2：sub-cue windowing 层（接入 `mixed_cue_speaker` seam）

| 项目 | 内容 |
|---|---|
| 模型 | `pyannote/segmentation-3.0` 或 Community-1（HF，MIT，需接受许可协议）；备选 3D-Speaker 自带 `--include_overlap` 阶段（同样底层依赖 HF token） |
| 集成点 | 新增一个前处理步骤：对判定为 mixed 的 cue（现有 6/61 走 `mixed_cue_speaker` 路径的那批）先跑分窗，得到 sub-window 边界，每个 window 单独过现有/候选 1 打分器算 margin，结果作为 `window_speakers: list[str]` 传给 `src/autoslice/speaker_host_evidence.py:142` 的 `mixed_cue_speaker`——这个函数今天的签名就是为这个用法设计的（docstring 明说是"reusable combinator seam for a future per-cue audio-window scorer"） |
| 针对的失败面 | 直接命中失败面 2（mixed cue，~10%）和失败面 4（overlap，当前零处理）——这是三个候选里唯一直接处理这两个失败面的 |
| 不解决的失败面 | 1（短 cue 阈值本身）、3（薄 margin 分离度，除非分窗后单窗口更纯净、间接有帮助但未验证） |
| 预期收益 | 无法引用一个通用 EER/DER 数字——这是"让判定链多一层证据来源"而非"提升某个已知指标",收益要靠现有 6 条 mixed cue 真值加一批新采集的重叠 cue 真值实测 |
| 先跑什么 | 用现有 6 条 mixed cue（`auto_203735_555_680` 里那批）先验证 pyannote 分窗的边界质量和中文场景下的可用性，这一步比候选 1 更便宜（只需跑分割不需要重新设计打分逻辑），但**没有 pilot 优先级排在候选 1 之前**，原因是候选 1 复用现成回放脚本、候选 2 需要新写分窗到打分器的胶水代码 |

### 候选 3：frame-level TS-VAD / personal-VAD 模块

| 项目 | 内容 |
|---|---| 
| 模型 | 没有现成的"中文任意目标人 + 单人 enrollment"预训练权重——TS-VAD+（2025）等公开工作都是"已知说话人数据集训练/评测"，SpeakerLM 的 `Match-Regist` 最接近但未开放权重、且是 7B LLM。此候选本质是"要不要投入自建/微调一个帧级目标说话人模型",不是"选哪个 checkpoint" |
| 集成点 | 架构级：会替换掉 cue 级 CAM++ 打分+two-means 整条链路，变成帧级二分类输出后再聚合回 cue 边界——不是一个插槽式集成，是重新设计判定层 |
| 针对的失败面 | 理论上同时覆盖 1、2、4——帧级输出没有 1.5s 门槛（可以对任意短片段给出置信度）、天然支持多标签（重叠时两个目标同时激活）、不依赖 whole-cue 边界（mixed cue 不需要人工分窗，模型自己在帧级别分开） |
| 不解决的失败面 | 无（如果做成的话是唯一"三个一起解决"的路线），但代价是要么等业界发布中文预训练权重，要么自己拿现有登记声纹+已积累的真值场次去微调一个模型——这是数据和工程投入都远高于候选 1/2 的选项 |
| 预期收益 | 无法估计——没有可比基准，只能说"是这三个候选里唯一架构完整的答案" |
| 先跑什么 | 不建议现在启动——先看候选 1、2 的 pilot 结果，如果换 embedding 和加分窗层之后 equal-quality 场次的 false-host/false-guest 仍然不达标，再考虑是否值得投入自建 |

### 观察名单（不进 Top 3，但值得记录）

- **SpeakerLM**（第 1、3 节都提到）：唯一同时具备 enrollment + 联合 ASR + overlap 处理的系统，7B LLM/A800 训练，权重未确认开放，AAAI 2026 才刚收录，半年到一年内可能有开源版本或蒸馏小模型，值得定期回查。
- **DiariZen**：中文 DER 目前是开源系统里最强之一（见第 5 节表格，10.1% vs 开源 pyannote 3.1 的 19.8%），如果候选 2 pilot 发现 pyannote 分窗在中文场景下质量不够，DiariZen 的 WavLM 分割骨干是备选分窗来源，但同样不原生支持 enrollment，不能替代候选 1。

### 跨语言 DER 参考表（诊断"谁的中文基础声学能力更强"，非直接可比我们的二分准确率）

来自 [Benchmarking Diarization Models](https://arxiv.org/html/2509.26177v1)（2025-09-30，独立评测，5 套系统在 English/Mandarin/German/Japanese/Spanish 上的完整聚类式 DER，不含 enrollment 场景）：

| 系统 | Mandarin DER | English DER |
|---|---|---|
| Sortformer v2 / v2-stream | **9.2% / 9.4%**(最优) | 15.3% / 14.1% |
| PyannoteAI(商业 Precision 系) | 10.0% | 6.6% |
| DiariZen(开源) | 10.1% | 7.0% |
| Sortformer v1 | 13.1% | 15.5% |
| pyannote 3.1(开源) | **19.8%**(明显偏弱) | 11.5% |

注意两点：(1) 这是纯聚类 DER，不是我们要的 enrollment 二分准确率，只能当"底层声学/分割能力在中文上强不强"的代理指标；(2) NVIDIA 官方给 `diar_streaming_sortformer_4spk-v2` 的 HF model card 写的是"trained primarily in English...performance may degrade on non-English speech"这类通用免责声明，与上表 Sortformer v2 中文表现最优这一独立评测结果存在张力，本次调研没有进一步查证是否为不同 checkpoint 版本导致的差异——如果未来真要评估 Sortformer（例如作为候选 2 分窗层的替代来源），这条需要先核实清楚，不能直接采信任一方。表里最重要的信号是：**开源 pyannote 3.1 中文明显偏弱**，这也是候选 2 选型时如果要接 pyannote，应该优先试 Community-1 而非 3.1 的一个依据（Community-1 未单独出现在此表，但其分割骨干是 3.1 的直接迭代）。

---

## 参考链接汇总

- CAM++: [论文](https://arxiv.org/abs/2303.00332) / [ModelScope](https://github.com/modelscope/3D-Speaker)
- ERes2NetV2: [论文](https://arxiv.org/abs/2406.02167) / [3D-Speaker README 对比表](https://github.com/modelscope/3D-Speaker/blob/main/README.md)
- NeMo (Streaming) Sortformer: [HF model card](https://huggingface.co/nvidia/diar_streaming_sortformer_4spk-v2) / [ICML 2025 论文](https://arxiv.org/pdf/2409.06656) / [Streaming 论文](https://arxiv.org/pdf/2507.18446) / [MarkTechPost](https://www.marktechpost.com/2025/08/21/nvidia-ai-just-released-streaming-sortformer-a-real-time-speaker-diarization-that-figures-out-whos-talking-in-meetings-and-calls-instantly/)
- pyannote: [Community-1 博客](https://www.pyannote.ai/blog/community-1) / [enrollment 社区讨论](https://github.com/pyannote/pyannote-audio/discussions/1667) / [GitHub](https://github.com/pyannote/pyannote-audio)
- DiariZen: [GitHub](https://github.com/BUTSpeechFIT/DiariZen) / [教程论文](https://arxiv.org/html/2604.21507)
- WeSpeaker: [GitHub](https://github.com/wenet-e2e/wespeaker) / [CNCeleb checkpoint](https://huggingface.co/Wespeaker/wespeaker-cnceleb-resnet34)
- ECAPA2: [论文](https://arxiv.org/abs/2401.08342)
- TS-VAD 谱系: [原始 TS-VAD](https://arxiv.org/pdf/2005.07272) / [TS-VAD+ 2025](http://www.apsipa.org/proceedings/2025/papers/APSIPA2025_P333.pdf) / [短 enrollment 2026](https://arxiv.org/pdf/2601.12769)
- SpeakerLM: [论文](https://arxiv.org/abs/2508.06372)
- WEEND: [Google speaker-id](https://github.com/google/speaker-id/blob/master/publications/WEEND/README.md)
- TagSpeech: [论文](https://arxiv.org/pdf/2601.06896)
- WhisperX 2025 后端切换: [安装指南](https://www.qwe.edu.pl/ai-tools/whisperx-speaker-diarization-install/)
- 跨语言 DER 基准: [Benchmarking Diarization Models](https://arxiv.org/html/2509.26177v1)
- 重叠检测 2025: [Robust OSD](https://arxiv.org/html/2505.23207)
- EEND-TA: [Pushing the Limits of End-to-End Diarization](https://arxiv.org/abs/2509.14737)
- diart: [文档](https://diart.readthedocs.io/) / [Live-1 博客](https://www.pyannote.ai/blog/introducing-live-1-streaming-diarization)

## 本次调研方法说明

纯 WebSearch/WebFetch 调研，未运行任何代码、未调用任何模型、未改动生产阈值或 `voiceprint_profile.v1.json`。现状锚点部分引用的数字均来自本仓库 `docs/reviews/2026-08-07-speaker-crosssession-eval.md`、`2026-07-12-speaker-phase1-diagnostic.md`、`2026-07-12-speaker-binary-v2-blocker.md` 三份既有文档和对应源代码行号，本次未重新验证。外部 SOTA 数字凡标注"来自搜索摘要非直接精读"的置信度低于其余直接 WebFetch 到原文表格的数字，使用时按此区分对待。中文语料下 <2s 专项 EER（CAM++ 和 ERes2NetV2 均缺）是本次调研最大的数据空白，候选 1 的 pilot 本身就是在补这个空白。
