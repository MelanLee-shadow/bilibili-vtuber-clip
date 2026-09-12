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
