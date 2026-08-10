# 2026-08-10 BV1hquD6pE7X 同 BV 三合一置换(打歌服 auto_200130_1323_1603)

**置换内容**:①视频重烧回填 Ivan 手标动作注记 `(跃起)`(cue104 / cue128 合并窗)②封面从 AI 重绘换成公主抱名场面截图裁切 ③标题换为 integrator 依高播放语料定稿的手定标题。

**结果**:CID `40760773025` → **`40765099962`**,封面 URL 换新,标题 `【李豆沙】打歌服没召唤出来，公主抱倒是先来了`,时长 287s。BV 不变、不占配额。

## Ivan 授权(逐字)
- 「我需要保留这个(跃起)，其他的括号不留」
- 「为什么你不能自己拟一个更好的标题，你再好好学一下b站李豆沙相关的高播放量的切片封面标题都是什么样子的，怎么做的，你学习到后自己起一个。封面选公主抱吧」
- 「只要自相矛盾，当然就认为这个完全没有否决权，完全不可信就完事了。」(封面 host-identity 判官自矛盾裁定)

## 关键机制事件

1. **自相矛盾 witness 无否决权**(Ivan 8/10 机制级裁定,已实现并部署 `7d08564`):v4D 封面的 v3 判官在同一份回答里给出 `primary_subject_is_lidousha=true` + `identity_conflicts=[]` + 文字认人,却勾 `primary_subject_matches_other_source_participant=true`。新门判 `SELF_INCONSISTENT`,整体作废(既不否决也不当通过证据),结论由**绑定同字节的联合 QC 回执**承接;明确否定仍全额否决。
2. **封面三版迭代**(全部回执并存,无同字节重摇):v1 wide(punch FAIL)→ v3 紧裁(机器全绿但公主抱姿态被裁没,integrator 感知驳回)→ **v4D**(crop [790,112,1846,706],头到膝在画内,三门齐 PASS)。
3. **身份定论(Ivan 亲裁)**:公主抱是**李豆沙抱星汐Seki**(integrator 中途误判为反向,已更正)。
4. **感知复审**:`final-human-review.json`(reviewer_kind=delegated_root_agent),三个复审点与八项 checks 均为 integrator 亲验帧后写入((跃起) 两帧、封面、片头衔接、片尾、全流解码零错)。

## 残留

**合集分P显示标题未同步**:`sync_section_title` 返回 -400,状态 `SECTION_TITLE_SYNC_AMBIGUOUS`,工具按设计 `POLL_ONLY_NEVER_REEDIT`(防半截稿件)。合集条目指向的 CID 已是新的,仅列表显示滞后。待单独一次合集标题编辑处理。因此 `repair-verify-live` 的 completed 回执尚未冻结。
