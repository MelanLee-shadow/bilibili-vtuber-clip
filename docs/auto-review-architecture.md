# 架构概览

这是公开版的导航，不另立质量规则。具体契约以
[流水线索引](pipeline/README.md) 指向的 step、代码和 profile 资产为准。

## 三个不同入口

普通制作从原始录播、聊天和频道配置开始，经选题、边界、字幕、标题封面走到可审阅包。
单候选制作只省略自动选题，不省略其余门。已审原稿的定点修改则以有效底稿和明确授权为起点，
冻结未受影响的内容；不能把任意旧字幕当成已审底稿，也不能借修复重新润色全文。

```text
source + chat + profile
    → candidate selection and story boundary
    → BCUT text/timing → CPA whole-clip text review
    → demanded local audio evidence → CPA decision
    → final-interval text and subtitle checks
    → shared story contract → title + screenshot-first cover
    → actual final video bytes → package audit
    → review delivery
    → separately authorized publication / same-BV repair
```

## 证据与裁决分开

BCUT 提供基础语音识别和时间坐标。CPA 先根据完整上下文进行文字裁决，
只在适用合同要求时补充局部音频证据，再由文字裁决环节决定是否修订。
普通谈话不默认使用整片 AGY 精听；歌切和专门声学核验仍有独立流程。
不同供应商的观察是证据，不因“更像正确答案”就自动成为最终字幕。

选片范围、带缓冲的上下文范围和最终视频范围并不相同；
字幕必须经过明确的时间域转换，不能把上下文里的话全部烧进最终片段。
统一主播字幕样式也不是逐段身份鉴定。详见
[边界](pipeline/30-boundary.md)、[字幕](pipeline/40-subtitle-text.md) 与
[语义修复](pipeline/41-semantic-repair.md)。

## 缓存不是跳过审核

`transcription_stage_cache.py` 与 `subtitle_draft_preparation.py` 将成功结果绑定到内容及配置身份。
命中后仍检验缓存内容；无效、未知或漂移输入不能复用。普通 BCUT 与完整 CPA 阶段复用后，
下游代词修复和忠实度检查仍执行，因此不能承诺重试零调用或最终字幕逐字节相同。

其他回执也只有在当前包、回答和输入哈希仍满足原合同的情况下才可复用；
`run_title_cover_joint_qc.py --reuse-valid` 不是为失败回答重新生成 PASS 的开关。

## 歌切失败阶段接续

`delivery_recovery.py` 在原有重试资格、等待期和配额条件下，通过 `song_lane.py` 的
`_resume_song_full_source_proof` 选择继续位置。歌切实现指纹相同、全源阶段的失败信息相符时，
队列才设置 `resume_full_source`；已直接重试全源的结果也保留该阶段，避免下一次又从窄窗开始。
实现变化或证据不足时回到普通路径，确定性弃选与 Talk-only 隔离不会被这项优化解除。

这是从失败步骤重新执行，不是把失败结果变成缓存 PASS。`full_source_retry` 中的
selector 路径、SHA 和内部候选 ID 与顶层窄窗记录分开保存，用于追溯各自尝试；
它们本身不是发布授权，也不能从目录新旧猜补历史绑定。全源音频/LRC、本人演唱与
成片门仍独立执行，详见 [歌切步骤](pipeline/50-song-lane.md)。

## 后置代词与可追溯诊断

`pronoun_context.py` 将上游已经提供的选题说明、场次主题和格式化弹幕传给同一次代词专项，
不另外检索、扩大窗口或选择支持某个答案的行。数据只辅助识别指代对象，不能照抄弹幕中的
他／她判定性别；原逐项修改合同仍限制为单数代词，不改其余文字或时间轴。

`pronoun_stage_trace.py` 在媒体同名 `.pronoun-trace/` 中保存每次调用独立的诊断，
记录输入、实际提示词、逻辑请求、返回和输出；未完成／失败与正常返回分别标示。
写入不可用时发出警告并保留原结果或原异常，不把诊断失败变成新的质量门。
它不缓存决定、不证明模型后端身份，也不保证两个模型回答一致。目录和文件要求私有权限，
文本过长、非法编码或检测到当前已知 CPA key 回声时只保留相应省略状态；这不是任意秘密的
完整脱敏器，诊断必须私有保存，不能直接作为公开 bug 附件。

## 成片、包与发布是三个检查面

字幕文件通过检查不证明它被正确烧入视频。最终 MP4 的字幕、片头和声文对应需要自己的证据；
声文粗偏移门也不能证明每一条短句都精确。封面优先寻找合格真实帧；
换帧、局部修图和生成图像都受身份、参与者、标题和实际像素验收约束。
详见 [封面](pipeline/70-cover.md) 与 [打包交付](pipeline/80-package-delivery.md)。

包内审计通过后才进入独立发布授权。出版登记约束重复投稿；已发布稿件的修复保留同一 BV，
其修改和完成回执另有状态机。`review_ready`、历史上传成功或仅公开标题正确都不等于本次修复完成。
详见 [发布步骤](pipeline/90-publish.md)。

## 公开版边界

公共仓库只提供软件、模板、合成测试与通用契约，不携带某个部署的运营状态、
真实审定稿、用户授权或声纹。历史候选的专属适配器可能被剥离或保留为拒绝执行占位。
MOSS / MAI 的研究不在本版默认链中替代 BCUT 或 CPA，也不代表相应服务已可用。

主要入口是 `scripts/session_autoslice.py`、`scripts/produce_slice_package.py`、
`scripts/audit_review_package.py` 与 `scripts/authorized_upload.py`；
其余入口按 [脚本地图](../scripts/README.md) 查找，而不是从私有运营报告推断用法。
