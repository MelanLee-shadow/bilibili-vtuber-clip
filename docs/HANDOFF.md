# Current handoff

Updated: 2026-07-24 America/New_York

> 本文件只记录尚未完成任务的恢复点，不复制流水线规则。步骤规则只读
> [pipeline/README.md](pipeline/README.md)，live runtime 只以
> `free:/opt/bilive/autoslice` 的当前 state/out/reports/process/lock 和公开面读回为准。

## 目标

用当前流水线系统性修复 2026-07-22 五条已发李豆沙切片，exact 重跑
`3573, 672, 1863, 1573, 1475`，抑制 `6577` 且不补位；全部封面走
`screenshot_direct`。机器闭包、root 最终逐片观看和授权门通过后，只修复原 BV，不新建重复稿。

## 已完成

- 系统性第一阶段已提交并部署：
  - `ba06fe80e61592733a33a5a26e076024188ea0c5`：source truth 精确投影、exact-final
    timeline offset、whole-line `owner_eligible`、hash-bound boundary scope、原子 correction、
    exact closure、评分/标题/关系/封面与 same-BV 证据门；
  - `d995c848b5f0f82cf53dc7eb4c2b36a7beed6c22`：1475 开头只保留多路共同支持的
    “请坐在左边的弹”，禁止“李姐晚上好/就请坐”回灌。
- 上述部署已从 `free:/opt/bilive/autoslice/repo/DEPLOYED_COMMIT` 与关键文件 hash 读回。
- 为容量门精确删除过七个可重算 media/output 层；未删 state/reports/logs、immutable v2、
  v11 失败证据或 source/adjudication 资产。V12 结束后最近只读快照显示可用
  `29,281,452,032` bytes（约 27.27 GiB），高于 25 GiB 门。
- fresh V12 probe 已终止：
  `/opt/bilive/autoslice/recovery/2026-07-22/full-rerun-v12-screenshot-cover`。
  它没有上传，state=`recovery_incomplete`，exact closure=`INCOMPLETE`，0/5 delivery，
  outside-contract attempt 为空；终止审计当时 V12 target/global runner lock 均可取得。
  global lock 的此刻状态仍须 live readback，正常 cron 运行也可能短暂持有它。
- V12 精确暴露的五项结果：
  - 3573：compat publish adapter 漏传 `recovery_publication_authority`；
  - 672：四个已写对的“毁神” mention 被宽窗口误判 owner ambiguous；
  - 1863：旧 `required_given_end_ms=2084520` 把 cue 911 之后的新 SC 冻结成故事内容，继而
    30→60 秒仍追着烟头话题扩展；
  - 1573：closure cue 66 end=`141410` 在 30 秒 ceiling 内，但实际 source window 只到
    local `141820`，比 scope 要求的 `161840` witness reserve 少 `20020ms`；
  - 1475：`BOUNDARY_SEMANTIC_REVIEW_UNAVAILABLE:LlmCallError`，属于 provider transient，
    不是字幕裁决。
- 已重新阅读两份现有 Pro 回答。Pro 明确认定 1863 火锅故事结束于 source cue 911 /
  `2056480ms`；后续 3:24“邪恶守宫”属于下一条 SC，应作为 topic-separation evidence，
  不能靠扩大到 120 秒吞入。新的 Pro 请求因浏览器 attach 失败未提交，不能记作成功咨询。
- 当前未提交集成修复已经实现并有定向回归：
  - publish adapter 精确转发 same-BV authority；
  - mention-scoped `replace_substring` 先解析全部 mention，再用同一精确 cue 并集约束
    mutation 与 owner projection；未审父窗口 cue 不再可能“被改但从审计消失”；
  - source reviewer 前先落 `talk-boundary-source-context-coverage.v1`；reserve 不足时不调用
    LLM，而以 `source_witness_reserve` 触发一次最多 60 秒的 fresh 重物化/重审；
  - `recommended=null` 记为 missing，不再伪报 out-of-scope；
  - source truth 新增 `boundary_role=next_topic_witness`：仍要求 padded context 正确落字，
    但完全位于 semantic target 之后时不取得故事终点 owner；
  - 1863 publication authority 已改为
    `boundary_end_mode=exact_source_pin`、`required_given_end_ms=2056480`：source reviewer
    仍须通过四命题语义门，只可选择 pin 前 400ms 内的完整 fresh-ASR closure；resolver 绑定
    该 cue 后把最终媒体 end 精确锁到官方 source cue 911 / `2056480ms`，并排除跨 pin 的派生
    ASR cue。其余四项仍为 `semantic_lower_bound`。较早的“邪恶守宫”感知检查保留，后续新
    SC 明确排除。
- 当前 tracked 集成态已完成 `608 passed` 相关广覆盖与一次完整
  `2219 passed in 69.70s`；Ruff（全部本轮 Python diff 与两个新模块）、compileall、
  architecture `7 passed`、`git diff --check` 均通过。独立聚焦复核的 exact-pin、late-cue、
  package `pin+200`、context-only owner 与无 scope retry 反例均已 fail closed；仍未完成的是
  clean commit/deploy 后的 fresh artifact/runtime/public 验收，不能用测试代替。

## 当前工作树与权威 hash

- 只允许提交本轮 tracked 代码、资产、测试与 docs；不得碰用户/生成物：
  `lidousha/.recovery-archives/`、`uv.lock`、当前
  `lidousha/2026-07-22-full-rerun-review/` 未跟踪媒体/sidecar。
- 当前修改后的 authority hashes（提交前仍须重算）：
  - recovery publication registry：
    `sha256:be9ffbd42008b94d9e47ea714e1fae5d032f576bb0e71841624df3b77ea53757`；
  - subtitle truth ledger：
    `sha256:794a4e2f45beae9e612ae884ca48eea2cd601d3c4537f05ae4fc024fd88a5749`；
  - final media review contract：
    `sha256:d51040d6d02931c328c31c6a8fa52b457e096c46d0959dfc7b3b8ea22a67cbd1`。
- immutable recovery source 仍须在下一 base 创建前复验：
  - source state：
    `sha256:fc26e2d68f4d78f4420b3e49d79f791bd24e4c3bcf2740862e380e7af7108f54`；
  - official MP4：
    `sha256:0eb2778dc53e5eabbccae089e5db92d3fb3662d90e1dd2ddbe7765436718989a`；
  - BCUT：
    `sha256:edc0d233b49ce2beed6e82c9adae5ffbd76f58eae63b8d9448f268d8f1b30bce`。

## 进行中

1. 完成 tracked diff 的 targeted/full regression、Ruff、compile、`git diff --check` 和 root review；
2. clean commit 后从 detached worktree 部署，读回 `DEPLOYED_COMMIT` 与关键 hash；
3. 保留 V12 state/reports/logs 作为失败证据；若容量不足，只精确删除 V12 可重算 `out/`；
4. 新建 fresh V13：
   `/opt/bilive/autoslice/recovery/2026-07-22/full-rerun-v13-screenshot-cover`，
   只从 immutable v2 source 和新部署 repo 由 v7 planner 重建；
5. 重新计算五项 pipeline fingerprint，验证 exact-no-backfill、6577 suppression、1475
   replacement 与四处 `AUTO_UPLOAD` 不存在，再以
   `AUTOSLICE_COVER_MODE=screenshot` 跑到 exact closure COMPLETE；
6. COMPLETE 后整包重建 manifest/audit，先同步到 staging，audit 与 rsync 空 diff 通过后才
   `--delete` 覆盖本地旧审片包；
7. root 完整播放五个最终烧录 MP4，按 committed exact points、八项检查与 StoryContract
   cover claims 出具真实 `delegated_root_agent` receipt；
8. receipt 与 authorized manifest 通过后，对五个原 BVID 执行
   `repair-plan --dry-run → repair-plan → repair-status → repair-run --dry-run → repair-run`，
   最后核对 public/public-tags/Creator/exact-section 四面。

## 约束与阻塞判据

- V8/V10/V11/V12 都只是历史或失败证据，不得续跑、复制 state/out/receipt、补 hash 或冒充
  current package。V13 也只有 exact closure COMPLETE 才能覆盖本地包。
- `6577` 永不补位；任一 exact candidate 失败都保持真实失败状态。
- 本轮五封面强制 screenshot 是内容选择：源帧能证明双人/角色/情绪；不代表 AI 生图功能未部署。
- recovery review manifest 始终 `upload_allowed=false`。新 BV 需要 `AUTO_UPLOAD`；本轮只走
  exact same-BV receipt/authorized-manifest lane，不创建 `AUTO_UPLOAD`。
- 感知 receipt 只有 root 实际完成五片全片观看后才能签，不能把机器 audit 或旧包观看冒充
  当前最终字节复核。
- `AUTOSLICE_SUMMARY.md` 仍是醒目标记的历史 V8 快照；只有 V13 成功并覆盖审片包后才从最终
  state/records 整份重生成，不能局部改旧数字伪造新历史。

## 完成判据

最新代码 clean commit 并部署读回；V13 exact 五项 closure COMPLETE；当前 package audit、
root final-human receipt 与本地镜像验证全部通过；五个原 BVID 完成 same-BV repair，且
public、public tags、Creator、exact section 四面一致；相关现行 docs/summary 与发布证据提交。
