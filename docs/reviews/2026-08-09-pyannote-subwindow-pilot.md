# 2026-08-09 pyannote 句内子窗试点

> 状态：**试点完成；不建议接入产线**  
> 范围：纯离线 WSL 试验；`repo` 产线代码与 `free` 均只读  
> 结论口径：探索性可行性证据，不是生产验收，也不是无偏 holdout

## 一句话结论

这条路线**有信号，但当前实现没有证明能治好两类病**。

- 对 13 条 mixed cue 的 26 个 **proxy 对齐子段**，历史冻结的已落地机器整句标签是 **13/26（50.0%）**，`pyannote segmentation-3.0 → CAM++ 子窗` 为 **16/26（61.5%）**；但 26 段是 13 个成对样本，任一整句单标签在“一 host + 一 guest”结构下机械得到 13/26。更直接的 cue 级结果只有 **3/13** 两侧都判对，其中仅 **2/13** 同时没有额外身份翻转。`200736` 还使用 provisional 媒体绑定。
- 对 8/7 点名的 6 条 mixed cue，evaluation-only 的 truth-segment-majority 字符代理分数从 **27/59（45.8%）** 到 **37/59（62.7%）**；这是用 oracle proxy 边界对齐完整真值子段后全有/全无计字，不是管线定位或恢复了 37 个字符，也没有 word timestamps。
- 句内 activity 变化在 13 条中检出 **9/13**；至少存在一个方向匹配身份边界的 cue 是 **7/13**，但这是从多候选中按真值方向筛选再取最近 proxy 的评测诊断，不等于 7 条已解决。一对一 proxy ±500 ms 计数仅 **TP=4、FP=9、FN=9，precision=recall=30.8%**。
- 132 条 whole-label nonmixed 对照中，**24/132（18.2%）** 出现身份变化候选；由于没有这些 cue 的人工声学子段真值，只能叫“潜在假切分”。子窗多数标签 **122/132（92.4%）**，未证明胜过历史冻结机器基线 **122/132（92.4%）** 或同 harness 的直接-enrollment 0.31 整句 CAM++ **124/132（93.9%）**。
- 8/8 `auto_200130_1323_1603` 的无 abstention 二元规则会给 251/251 子窗强制吐出标签，但 production `0.68` anchor gate 仍为 **0 个 whole cue / 0 个子窗，低于至少 2 个 anchor 的要求**。它没有修复原 `BLOCKED` 条件；又无人工真值，故仍不可交付。

因此本轮裁决是：**不改产线、不迁移 `AUTOSLICE_SPEAKER_PYTHON`、不把 8/8 阻塞件自动放行**。

## 1. 实际跑了什么

### 1.1 环境与 gated 模型

- WSL：32 逻辑核，CPU-only。
- `~/Project/vtuber-reproduce/venv`：Python 3.12.3，`pyannote.audio==3.4.0`，`torch==2.5.1+cpu`，`torchaudio==2.5.1+cpu`，`huggingface_hub==0.36.2`；`pip check` 通过。
- 官方 gated 模型：[pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0)。开始时匿名请求确实返回 401；本机标准 HF token 随后可用并通过官方授权检查，才继续。未显示 token，未注册账号，未下载或使用第三方镜像权重。
- 官方 revision：`e66f3d3b9eb0873085418a7b813d3b369bf160bb`；checkpoint SHA-256 `da85c298…1729298`；config SHA-256 `fa65a47a…d4263`。

`huggingface_hub` 先装到 1.27.0 时与 `pyannote.audio==3.4.0` 的旧 `use_auth_token` 调用不兼容，固定到 0.36.2 后真实加载路径和 `pip check` 都通过。这一点是实际触发并修复的环境兼容问题，不是纸面推断。

### 1.2 冻结的推理方法

分割阶段完全不读真值：

1. 每条 cue 取以 cue 为中心、受媒体边界约束的 10 s 上下文。
2. 官方 `segmentation-3.0` 输出 powerset hard multilabel（3 个局部说话人通道，最多 2 人重叠）。
3. 零活动帧归到最近的非零状态；小于 180 ms 的状态游程确定性并入相邻状态。
4. 状态变化的相邻 frame center 中点作为候选边界，再与 cue 相交。
5. 首次 CAM++ 实跑发现 cue 相交会留下 11–44 ms 首尾残片：11/18/23 ms 触发前端 assertion，29–44 ms 产生非有限 embedding，而 51 ms 起产生有限 192 维 embedding。这个探测不读真值。最终只把 **<60 ms 的首尾残片**并入唯一相邻状态，不删除内部边界，也不跨边界 padding。60 ms 只是运行时底线，不代表声纹在 60 ms 上生物识别可靠。

身份阶段也不读真值：

- CAM++ 模型树 SHA-256：`f01588f1…745ed0c3`，与 voiceprint profile 一致。
- 三条登记声纹 SHA-256：`d54a637d…4105f`、`8fc095fe…7ef3`、`d257b7ae…c5a2`。
- 每个整句和每个子窗各取 192 维 embedding，分别与三条 enrollment 算生产同形态的 float32 cosine，相似度保留 5 位，取中位数。
- 主判定在看真值前固定为 `configuration.json:model.yesOrno_thr=0.31`：`median >= 0.31 → 李豆沙`，否则 `连线`；未用本轮真值拟合阈值。`0.31` 原本是单对 speaker-verification 阈值，把它套到“三参考五位分数的中位数”是本试点预注册的自定义聚合，不是 CAM++ 已校准好的 median 阈值。
- 这是**直接 enrollment 试点**，不是当前 `speaker_finalizer.py` 的 host/guest bank、two-means margin、短句/走廊/语义仲裁全链复播。报告中的两种整句对照分别是“历史已落地整句标签”和“同 harness 直接-enrollment 0.31 CAM++”；**没有重放当前 production finalizer 全链，124/132 不能解释成当前产线 finalizer 的准确率**。

最终处理 305 条 cue：145 条有真值评测 cue + 8/8 阻塞件 160 条 cue；得到 490 个子窗。整句与子窗合计 795 个语义单元、606 份去重 WAV，全部 CAM++ 成功。

## 2. 权威、媒体与泄漏控制

### 2.1 真值版本

任务点名的 `tests/lidousha/fixtures/ivan_truth_diff_20260807.json`（SHA `7e146929…691c12`）是已被当前 v2 推翻的 v1 工作表：它仍把 203735 cue10 算 mixed，共 6 条；当前 pristine/v2 明确将 cue10 改为整句嘉宾，并更新 cue59。因此本报告采用当前权威的：

- `auto_200511_61_138`：40 cue，7 mixed；`assets/lidousha/speaker_overrides/...speaker.v1.json`，SHA `1e2fe85a…64d97d4`；
- `auto_200736_298_383`：44 cue，1 mixed；truth v2 SHA `794b7d7b…fc84472` + override SHA `4005fb49…ac8443`；
- `auto_203735_555_680`：61 cue，5 mixed；truth v2 SHA `df37e886…fe1c15` + override SHA `8cfe47b1…6e8981da`；
- 合计 **145 cue / 13 mixed / 26 mixed truth segments / 132 nonmixed**。

分割脚本和 CAM++ 脚本没有真值路径；CAM++ checkpoint 完整且双 SHA 绑定最终 segmentation manifest 后，评测器才读取真值。0.31 阈值和所有后处理规则在真值计分前冻结。定稿评测器还在读取真值前验证 method-freeze、segmentation 与完整 CAM++ checkpoint，并把自身、method freeze 和三个输入的 SHA 写入 `evaluation_results.json`。

### 2.2 媒体绑定

| 场次 | 实际媒体 | 绑定状态 |
|---|---|---|
| `auto_200511_61_138` | `pilot/session_b_source.mp4`，`f5662ff2…f805cbc7` | exact；任务文字把 `session_a_recut.mp4` 叫作 7/22 场，实际哈希证明它不是 |
| `auto_203735_555_680` | `pilot/session_a_recut.mp4`，`f45b83aa…7433bed` | exact |
| `auto_200736_298_383` | `out/.../auto_200736_298_383.recut.mp4`，`e90f5ff9…ad76ae1` | **provisional**；时间轴/时长对齐，但真值绑定期望 `4849f341…c78a2e`；在 free 限定扫描 84 个媒体、3.09 GB 未找到旧字节 |
| `auto_200130_1323_1603` | 8/8 recut，`0e73603f…d9a6c6` | exact；现行 sidecar 明确 `BLOCKED: not enough Li Dousha clip anchors: []` |

另一个命名陷阱：`pilot/session_a_recut.mp4` 实际是 203735，而 `pilot/session_b_source.mp4` 才是 7/22 等音质联动场。本轮始终按 SHA 绑定，不按文件名猜。

### 2.3 边界真值限制

三份 speaker override 都明确说：mixed 子段时间是按字符长度比例估计，没有 word timestamps。说话人顺序和标签是 Ivan 权威，毫秒边界不是声学标注。因此下文只写“**到 proxy 的差值**”；它不能回答真实声学切点误差是多少。

### 2.4 开发集暴露

正式冻结前曾用已知评测 cue `203735/30` 做一次 20 s full-diarization golden probe，只验证数据流并观察到明显过分割/重叠；随后放弃该 full-diarization 路线，改用本报告的原生 segmentation hard-multilabel 路径。没有用真值数值调 180 ms、60 ms 或 0.31，但已知评测 cue 的观察影响了路线选择，因此三场都属于 development-set 探索，不是无偏 holdout；任何正向可行性信号都需新 holdout 复核。

## 3. 结果

### 3.1 病一：6 条既有法证 mixed cue（字符覆盖 proxy）

这 6 条是 `200736/17` 与 `203735/30,31,41,44,59`；前者使用 provisional 媒体绑定。历史冻结机器基线全标嘉宾，所以 evaluator 先按 oracle proxy 边界对齐 truth segment，再以预测时长多数标签把整段 Python `len(text)` 全有/全无计分，得到 59 个字符单位中的 27 个；标点与 ASCII 各计 1。该分数只用于和 8/7 的 45.8% 法证口径对齐，不是管线定位、切分或恢复了这些字符。

| 指标 | 历史冻结机器整句标签 | pyannote + CAM++ 子窗 |
|---|---:|---:|
| 字符覆盖 proxy | 27/59（45.8%） | **37/59（62.7%）** |
| 主播 truth-segment 折算覆盖 | 0/32（0%） | **22/32（68.8%）** |
| 嘉宾 truth-segment 折算覆盖 | 27/27（100%） | **15/27（55.6%）** |
| 子段正确 | 6/12（50.0%） | **8/12（66.7%）** |

也就是说，按整段折算，新路径覆盖了更多主播文本，但以 12 个嘉宾侧 proxy 字符落入“假李豆沙”整段为代价。具体看：

- `200736/17` 仍没有方向正确的身份切点，两段都判嘉宾；**点名病灶本身未解决**。
- `203735/31`、`203735/44` 两段全对。
- `203735/30` 有一次方向正确但位置偏移的切换；`203735/41` 才有后续反复翻转，两者都仍有一侧错。
- `203735/59` 有三段 activity 变化和三次身份翻转，主播侧仍错。

所以只能说“oracle 对齐诊断中出现可用的子窗身份信号”，不能说“实际恢复了被吞文字”或“解决了混说 cue”。

### 3.2 13 条 mixed cue 全量

| 方法 | 正确/26 | 准确率 | 假李豆沙 | 假连线 |
|---|---:|---:|---:|---:|
| 历史冻结机器整句标签展开到子段 | 13 | 50.0% | 4 | 9 |
| 同 harness 直接-enrollment 0.31 CAM++ 整句展开到子段 | 13 | 50.0% | 13 | 0 |
| pyannote + CAM++ 子窗 | **16** | **61.5%** | 8 | 2 |

子窗按 proxy 时间重叠加权为 **23,111.406 / 34,730 ms = 66.5%**。不过每条 mixed cue 恰好一段 host、一段 guest，任一整句单标签都机械得到 1/2；26 个子段也不是独立样本。子窗的净结果只是 **3/13 cue 两侧都判对，10/13 仍只有一侧对**，其中只有 `200511/23` 与 `203735/44` 同时没有额外身份翻转。

| 场次/cue | proxy split | activity 最近绝对 Δ | 方向匹配身份边界最近绝对 Δ | 子段正确 | 子窗 |
|---|---:|---:|---:|---:|---:|
| `200511`/9 | 18,198 | 未检出 | 未检出 | 1/2 | 1 |
| `200511`/12 | 22,234 | 未检出 | 未检出 | 1/2 | 1 |
| `200511`/20 | 32,485 | 786.3 ms | 未检出 | 1/2 | 2 |
| `200511`/23 | 39,408 | 93.3 ms | 93.3 ms | 2/2 | 4 |
| `200511`/25 | 43,528 | 466.1 ms | 968.3 ms | 1/2 | 3 |
| `200511`/36 | 64,145 | 未检出 | 未检出 | 1/2 | 1 |
| `200511`/40 | 76,016 | 未检出 | 未检出 | 1/2 | 1 |
| `200736`/17 | 38,300 | 165.6 ms | 未检出 | 1/2 | 3 |
| `203735`/30 | 48,950 | 691.2 ms | 691.2 ms | 1/2 | 2 |
| `203735`/31 | 52,935 | 438.1 ms | 438.1 ms | 2/2 | 4 |
| `203735`/41 | 74,290 | 104.3 ms | 441.8 ms | 1/2 | 5 |
| `203735`/44 | 85,110 | 388.8 ms | 388.8 ms | 2/2 | 2 |
| `203735`/59 | 119,440 | 51.3 ms | 574.4 ms | 1/2 | 4 |

汇总：activity 边界检出 9/13，其中 proxy ±250 ms 为 4/13、±500 ms 为 7/13；方向匹配身份边界的 existential 检出是 7/13，其中 ±250 ms 为 1/13、±500 ms 为 4/13。评测会先按真值方向筛边，再在同 cue 多候选中取离 proxy 最近者；这是 evaluation-only 匹配，不是 blind detector hit。把每 cue 最多一个命中、其余预测都算 FP 后：

- activity proxy ±500 ms：TP=7、FP=13、FN=6，precision 35.0%、recall 53.8%；
- identity proxy ±500 ms：TP=4、FP=9、FN=9，precision=recall=30.8%；±250 ms 仅 TP=1、FP=12、FN=12。

13 条 cue 共有 20 个 activity 候选和 13 个身份变化边界；后者中 9 个方向匹配、4 个方向错误。这些 ms 都只是字符比例 proxy 差值。

### 3.3 132 条 nonmixed 负对照

| 场次 | 绑定 | cue | 历史冻结机器整句 | 同 harness 直接-enrollment 0.31 CAM++ 整句 | 子窗多数 | 有身份变化候选 cue |
|---|---|---:|---:|---:|---:|---:|
| `200511` | exact | 33 | 29/33（87.9%） | 32/33（97.0%） | 31/33（93.9%） | 5 |
| `200736` | provisional | 43 | 38/43（88.4%） | 39/43（90.7%） | 40/43（93.0%） | 7 |
| `203735` | exact | 56 | 55/56（98.2%） | 53/56（94.6%） | 51/56（91.1%） | 12 |
| **合计** |  | **132** | **122/132（92.4%）** | **124/132（93.9%）** | **122/132（92.4%）** | **24/132（18.2%）** |

合计错误方向与率（分母均为 132）：

- 历史冻结机器基线：假李豆沙 1（0.8%），假连线 9（6.8%）；
- 同 harness 直接-enrollment 0.31 整句 CAM++：假李豆沙 7（5.3%），假连线 1（0.8%）；
- 子窗多数：假李豆沙 7（5.3%），假连线 3（2.3%）。

相同的 122/132 并不代表逐条等价：相对冻结机器基线，子窗有 8 条改善、8 条退化；相对同 harness 整句 CAM++，子窗 1 条改善、3 条退化，exact McNemar `p=0.625`，样本不足以证明系统性差异。24 条 whole-label cue 内共有 29 个身份变化候选；因为 nonmixed 真值只有 cue 级标签、没有人工声学子段或 overlap 标注，不能直接断言全是“假切分”。

239 个有标签子窗里 170 个（71.1%）短于 1.5 s，119 个短于 1 s；另有 **60/239（25.1%）** 是 pyannote 双人 activity 窗，占时长 **20.8%**。单个 CAM++ embedding 在双人混音上仍被强压成一个身份，因此当前身份分数混入了不适用输入；生产候选必须对 overlap 双标签、abstain 或送人工复核，并把 singleton/overlap 分层评测。

### 3.4 病二：8/8 整批 unresolved

代表件采用 Ivan 举例的 `auto_200130_1323_1603`，而不是根据“缺 sidecar 的目录数”反推候选。其原始 finalizer sidecar 是精确证据：

```text
status = BLOCKED
production_ready = false
reason = SpeakerFinalizationError: not enough Li Dousha clip anchors: []
```

新路径的技术结果：

- 160 cue / 251 子窗全部有 CAM++ 数值标签；
- 20/160 cue 出现至少一次身份变化，共 24 次；
- 子窗多数为主播 63 cue、嘉宾 97 cue；整句 CAM++ 为主播 70、嘉宾 90；两者有 7 cue 不同；
- 67/251 子窗落在阈值 ±0.05 内；185/251 短于 1.5 s；81/251（32.3%）为双人 activity 窗，占时长 23.4%；
- production `host_session_seed_min=0.68` 下，160 个 whole cue 与 251 个子窗均为 **0 个过线**，最大仅 `0.65203`，仍不满足至少 2 个 clip anchors。

251/251 覆盖是“有限分数必二选一”的无 abstention 总函数定义结果；它只是移除了原门禁，并没有恢复 anchor 证据或复播 production finalizer。又没有独立真值，无法知道标签是在恢复嘉宾、切开主播与嘉宾，还是把同一人过切。故回答用户问题“能否给出可交付判定（不再 unresolved）”是：

> **能强制生成完整二元标签；production anchor gate 仍是 0/2，不能据此给出安全可交付判定，仍应 unresolved/fail-closed。**

## 4. 为什么本轮没有赢

1. **分割 recall 不够**：4/13 mixed cue 没有内部 activity 边界；有边界的 cue 又常过切。
2. **边界与身份是两个误差源**：activity 命中 proxy 后，CAM++ 仍可能把相邻两窗判同一人，或反复翻转。`200736/17` 就是 activity 有候选、身份完全没切。
3. **子窗太短且 overlap 被硬单标签化**：有标签子窗 71.1% 小于 1.5 s，25.1% 窗为双人 activity；短语、笑声、停顿和双人混音让直接 enrollment cosine 极不稳定。
4. **0.31 不是产线 talk finalizer**：它是 CAM++ 单对 yes/no 阈值，本轮把它自定义用于三参考中位数。产线实际依赖 anchor gate、host/guest banks、session margin、two-means 和保守仲裁；8/8 的 production anchor 仍为 0/2，本轮不应冒充生产回放。
5. **真值仍不够强**：speaker 顺序可靠，但边界只是字符比例 proxy；8/8 代表件完全没有人工身份真值；200736 媒体字节还不是真值绑定的旧版本。

## 5. 产线裁决与下一轮门槛

### 5.1 本轮不执行的产线动作

- 不改 `src/autoslice/producer_speaker.py`、`speaker_finalizer.py` 或 `speaker_host_evidence.py`；
- 不给现有 `mixed_cue_speaker(window_speakers)` 喂数据后宣称完成，因为它最终仍折叠为一个 cue 标签；真正需求是 segment-valued 输出；
- 不在 `/opt/bilive/autoslice/venv-diar` 安装 pyannote；
- 不切换 `AUTOSLICE_SPEAKER_PYTHON`；
- 不解除 8/8 阻塞件。

### 5.2 建议先补的证据

1. 给 13 条 mixed cue 做真正的声学切点标注（可听边界/重叠区间），不要再用字符比例代理毫秒误差。
2. 给至少一条、最好全部 6 条 8/8 unresolved 候选做 cue/子段级人工说话人真值。
3. 在不看新 holdout 的前提下，预注册并比较：
   - 官方完整 diarization 解码/跨 cue 聚类，而不是每 cue 独立局部通道；
   - speech-masked window pooling 与最小可判身份时长；
   - 产线 host/guest bank margin 在子窗上的真正复用，而不是直接 0.31；
   - overlap 区间允许双标签或 review，而不是硬压成一个人。
4. 下一轮至少达到：mixed cue 两侧完整正确 ≥85%、预注册的一对一声学边界 ±500 ms precision/recall 均 ≥80%、人工确认的 nonmixed 过切 ≤2%、nonmixed 假李豆沙不高于冻结机器基线，并在有真值的 8/8 候选上通过原 production anchor/abstention 安全门；否则继续离线。

这些是建议的 go/no-go 门槛，不是从本轮 13 条上继续调出来的阈值。

### 5.3 只有下一轮赢后才启用的接入设计

现有调用点是 `producer_speaker.py:208` 的 `run_speaker_finalizer()`，由 `producer_request.py:85` 的 `--speaker-python` / `AUTOSLICE_SPEAKER_PYTHON` 选择独立运行时。若以后过门槛，接法应是：

1. 在 `speaker_finalizer.py` 的 cue WAV 准备之后新增 segmentation 前处理，输出 hash-bound `speaker_segments[] = {source_cue,start_ms,end_ms,activity_state,campp_scores,label,confidence}`。
2. 对每段走现有 CAM++ embedding/cache/hash 规范，再由新的 segment-valued policy 合并；保留整句决策作 fallback 和审计对照。
3. SRT/ASS 渲染器必须真正在 cue 内拆条；`mixed_cue_speaker()` 只返回一个标签，不能作为最终输出契约。
4. 新 manifest 同时绑定媒体、text-final SRT、segmentation 模型 revision/权重、CAM++ 模型树、enrollment、全部子窗 WAV 与 policy 版本；任何缺失/短窗/overlap 不确定性继续 fail-closed。

运行时迁移应采用**全新 venv** 的版本化旁路，而不是复制已有 venv：复制目录后，`bin/pip` 的绝对 shebang 很可能仍指回旧环境，反而污染当前生产 venv。下面只是下一轮过门槛后需在 free 上另行验证的迁移顺序，不是本轮执行过的命令：

```bash
# 1) 只读核对旧环境的 base interpreter，并导出可审计依赖锁；
#    使用第一条命令实际打印出的绝对 base-python 路径执行第 2 步。
/opt/bilive/autoslice/venv-diar/bin/python -c \
  'import sys; print(sys._base_executable)'
/opt/bilive/autoslice/venv-diar/bin/python -m pip freeze --all \
  > /opt/bilive/autoslice/locks/venv-diar-before-pyannote-20260809.txt

# 2) <base-python> 必须替换为上一步输出，不能用旧 venv 的 bin/pip。
<base-python> -m venv /opt/bilive/autoslice/venv-diar-pyannote-20260809
/opt/bilive/autoslice/venv-diar-pyannote-20260809/bin/python -m pip install \
  -r /opt/bilive/autoslice/locks/venv-diar-before-pyannote-20260809.txt
/opt/bilive/autoslice/venv-diar-pyannote-20260809/bin/python -m pip install \
  --index-url https://download.pytorch.org/whl/cpu \
  torch==2.5.1+cpu torchaudio==2.5.1+cpu
/opt/bilive/autoslice/venv-diar-pyannote-20260809/bin/python -m pip install \
  pyannote.audio==3.4.0 huggingface_hub==0.36.2
/opt/bilive/autoslice/venv-diar-pyannote-20260809/bin/python -m pip check
```

还必须在新环境分别 smoke-test 官方 pyannote 模型加载和既有 ModelScope/CAM++ finalizer；依赖锁若不能从配置的可信源重建就停止，不得把旧 venv 当作修复目标。随后只在有官方 HF 授权、模型已预取并完成离线 smoke/影子批次后，把 canary 进程的 `AUTOSLICE_SPEAKER_PYTHON` 指向新 venv。回滚只需把环境变量改回 `/opt/bilive/autoslice/venv-diar/bin/python` 并重跑未交付候选；旧 venv、旧 manifest schema 和旧产物均不删除。

**这只是失败后保留的条件式设计，本轮不得执行。**

## 6. 可复现性与性能

主要工件位于 `~/Project/vtuber-reproduce/pilot/pyannote-pilot-out/`：

| 工件 | SHA-256 / 摘要 |
|---|---|
| 权威清单 `AUTHORITATIVE_ARTIFACTS.json` | `f074bda8d3735f0e2274a2a07bf658fd77be5d0b24aec4a76f15a20f15436c41` |
| `segmentation_manifest.json` | `afa61a1285782a435ca637e27f9f72f0cc13aac497d87d5d16652f89075b6d06` |
| final bound segmentation 规范化预测摘要 | `ed84fdbbc2e02fd4cb6e91980cfcd9ecca0489f29ce52f52281b052d788dd48e`；未保留该最终版本的第二份独立 manifest |
| `campp_subwindow_scores.json` | `a4b2763d47a283a8e002ee545ca4f2f9ae3b57a20a7a64e1b3420c1f01d856a7` |
| `campp_subwindow_scores.repeat.json` | `6012c8123a6a239881119116641d9439eed7aa4c9b0ed0df2b5f942794561d2e`；完整 binding/items/三参考分数规范化摘要两次均为 `16204cb12fedd4e380c5295df84ede0e2d027677fc1d47cfc031090124bb10ef` |
| method/config freeze | `448edfd54c6dffdd7cf687acee19a334b3022c32ffe9d6b7c5cd62b63d3293d2` / `4254de374b9f21a9fe85357df0ef84067a2ece6bc4727126f1cb49ecd812b795` |
| segmentation / CAM++ / evaluator 脚本 | `99e881145a6a797213ad55407cb9e2d4231c44837dbd9c0e0ddadb821ac9b786` / `21a93f53876e0738bce9c9b7d8381448fa5d233bed3d3821f50ba88a1dba96b9` / `86e7277184784ba73e10fe7a3b04f46d796765ea26272059839a51757f85b2bf` |
| authoritative evaluation | `evaluation_results.json` 文件 `2114d3e1713847a237d0b4b8746cb905987573efc7b3fd815b1f48777b70d565`，`complete:true`，payload `4eef5ecc23dce5d31f5c513eab3a77d64dc71b61c68a8de86914af453e50f2e2` |
| 独立 score 冻结复评 | `evaluation_results.repeat.json` 文件 `bfeedf1c9cb7da02281b24a152fd24b16ccfaf5c83515657c6567c356115305d`；两次指标摘要均为 `8c468b09ad26b047d2701a5c3030817f5f5905051c072dd39774253f8a0a9aeb` |

- 较早的同算法 R2 冻结曾保留两次一致的规范化摘要 `3a1e6b2e…`；但最后一次 bound manifest 的摘要是 `ed84fdbb…`，没有保留该最终版本的第二份独立 manifest，因此**不把最终 segmentation 宣称为两次工件级复现**。最终 bound manifest 为 305 cue / 490 子窗，最短 65.031 ms。
- 最终 bound manifest 自报 segmentation 推理阶段 12.168 s；较早一次无争用完整命令 wall 约 7.57 s、最大 RSS 759,660 KiB，二者不是同一次 run，不能混作一个 benchmark。并发复跑会明显变慢，因此正式管线必须给 manifest 加独占锁/临时目录。
- CAM++ 独立完整重算 606 WAV：49.60 s，CPU 899%，最大 RSS 2,261,772 KiB；两次逐分数一致。
- 定稿 evaluator 的默认值已改为完整的 `campp_subwindow_scores.json`。权威清单保存了三阶段绝对入口；本轮实际使用/复核的命令形态如下，结果 JSON 在打开真值前验证 method freeze / segmentation / CAM++ freeze，并绑定 evaluator SHA：

  ```bash
  cd ~/Project/vtuber-reproduce
  ~/Project/vtuber-reproduce/venv/bin/python \
    pilot/pyannote_subwindow_pilot.py --batch-size 16 --torch-threads 16
  ~/Project/vtuber-reproduce/pilot/eres2-pilot-venv/bin/python \
    pilot/campp_subwindow_score.py \
    --manifest pilot/pyannote-pilot-out/segmentation_manifest.json \
    --output pilot/pyannote-pilot-out/campp_subwindow_scores.json \
    --profile ~/Project/repo/assets/lidousha/voiceprint_profile.v1.json \
    --reference-dir pilot/pyannote-subwindow-inputs/voiceprints/lidousha \
    --model-dir pilot/pyannote-subwindow-inputs/campp
  ~/Project/vtuber-reproduce/venv/bin/python \
    pilot/evaluate_pyannote_subwindow.py
  ```

- `AUTHORITATIVE_ARTIFACTS.json` 是唯一工件路由。`pilot_results.superseded.{json,md}` 是废弃的 per-session two-means 敏感性分析，已改名并标 `superseded:true / complete:false`，不属于主结论；method freeze 中的 `implementation_sha256.adapter_script` 只保留预注册方案的 provenance，实际 CAM++ scorer 直接读取 nested segmentation manifest。`pyannote_subwindow_config.json`、`pyannote_segment_stage.py`、`campp_subwindow_stage.py` 是更早的未采用预冻结路线（含错误 8/8 候选），不得用于复现本报告。主配置只有 `pyannote-pilot-config.json`。
- 本次 manifest 内记录的缓存 revision 与 checkpoint/config SHA 均正确，所以当前结果有效；但 segmentation 脚本仍以模型 ID 的 `main` 加载、共享固定输出目录，尚未在代码层钉死 `@e66f…` 或提供独占锁。正式复跑前必须补 revision/hash fail-fast、独占锁或 unique run dir；本轮不把这份探索脚本当生产入口。
- 进度与真实失败（401、依赖兼容、短窗 assertion、并发 manifest drift）均逐行保存在 `~/Project/vtuber-reproduce/logs/pyannote_pilot.log`；失败 checkpoint 被改名保留，没有伪装成成功。

## 7. 审查记录

### Round 1：方案攻击

独立 challenger 在运行前指出四个 P0：v1/v2 真值冲突、媒体映射/200736 hash 问题、字符比例边界不能冒充声学 ms 真值、CAM++ 判定规则未预注册。全部接受并落实为当前 v2 的 145/13 grid、SHA 绑定、proxy 措辞和 0.31 真值盲冻结；还增加了 132 条 nonmixed 负对照、同 harness 整句基线、短窗覆盖和 8/8 诊断边界。

### Round 2：三路独立结果审查

三名只读 reviewer 分别审查了评测代码/复算契约、真值 authority/任务覆盖、统计语义/媒体结果。共同结论是：固定 0.31 主链的算术可复算，`不上产线 / 8/8 fail-closed` 裁决成立，但原稿不能直接定稿。已接受并修复：

- evaluator 默认误指 `complete:false` checkpoint：改为完整 score 默认值，增加 `complete:true`、method/evaluator/input SHA 闭环，并实际默认复跑；
- 旧 two-means 与固定 0.31 双主链：旧结果改名 `pilot_results.superseded.*` 并标明非权威；
- 37/59 改称 oracle 对齐的字符代理，不再声称实际恢复字符；
- 将 16/26 前置降解为 cue 级 3/13，并补一对一 collar precision/recall、候选过切惩罚；
- nonmixed “假切分”改成“身份变化候选”，补 paired churn 和 false-host/false-guest 率；
- 补报 60/239 与 81/251 双人 activity 窗，以及 8/8 production anchor gate 仍为 0/2；
- 删除复制 venv 的迁移建议，改为全新 venv + `python -m pip` + canary/回滚。

Round 2 修后，两个独立 CAM++ score freeze 复评的 `overall/sessions/mixed/unresolved/method/limitations` 指标摘要同为 `8c468b09ad26b047…`。

### Round 3：ChatGPT Pro 阻塞与非 Pro fallback

按 `chatgpt-pro-consult-cdp` 使用 pinned Hermes 可见 WSL Chrome；doctor 通过，但提交前的 mode 检查只看到 `Advanced / Model GPT-5.6 Sol / Effort Extra High`，没有有效的 `Pro`、`Standard Pro` 或 `Pro Extended` 标签。工具以 `mode_not_confirmed` 阻塞，**prompt 未提交、没有 thread、没有 Pro 回答**；记录为 `/home/ivan/.hermes/chatgpt-cdp-runs/2026-08-09T05-49-15-575Z-ac9ff6051e7cf09e.json`。本轮不得称作 ChatGPT Pro reviewed。

随后执行明确标注的 **Round 3 fallback - not ChatGPT Pro** 只读目标对齐复核。它接受科学 no-go 与全部 fail-closed 决策，但指出废弃 `complete:true` 结果易误选、字符代理/整句基线语义需澄清、开发集 golden probe 应披露、复现哈希/命令和 integrator diff 不完整。已将废弃结果改名并标 superseded，新增 `AUTHORITATIVE_ARTIFACTS.json`，明确未重放 production finalizer，披露 development-set 路线暴露并补全工件链；修后定点复核接受报告内容，report-only handoff 为 `~/Project/vtuber-reproduce/pilot/2026-08-09-pyannote-subwindow-pilot.diff`。最终报告仍不依赖该 fallback 的模型判断替代实测。

## 8. 最终限制

- 只有 3 个有标签场、13 条 mixed cue，不是未见生产 holdout。
- 冻结前查看过已知评测 cue `203735/30` 的 full-diarization probe，路线选择存在 development-set exposure。
- `auto_200736_298_383` 媒体只做时间轴对齐的 provisional 评测。
- mixed 毫秒差是 proxy，不是 acoustic error。
- 37/59 是 oracle truth-segment 对齐后的字符代理，不是实际字符定位或恢复。
- 直接 0.31 enrollment 规则不是生产 finalizer 全链。
- 25.1% 有标签子窗与 32.3% 的 8/8 子窗是双人 activity，却被单 embedding 强制单标签；当前未做 overlap-safe 解码。
- 8/8 代表件无人工 speaker truth。
- final bound segmentation manifest 没有保留同版本第二份独立复跑工件。
- 当前探索脚本还缺模型 revision 代码级 pin 与输出锁；当前工件由缓存 hash 绑定有效，不等于未来 main 复跑天然可复现。
- ChatGPT Pro 精确模式不可用；Round 3 只完成了明确标注的 non-Pro fallback。
- 本轮结果能支持“继续研究子窗”，不能支持“上线自动拆分或放行拒绝件”。
