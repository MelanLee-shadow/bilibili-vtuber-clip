# 90 授权上传、同 BV 修复与公开验收

本文件是发布步骤的**分步权威**。唯一程序入口是
`scripts/authorized_upload.py`；`.agent/skills/bilive-autoslice-publish/SKILL.md`
只提供操作顺序，不得另立规则。

## 发布准入

- 每条都需要 Ivan 明确授权；manifest 保存授权原话，工具不能替用户创造授权。
- 新投稿只接受 `authorized-upload-manifest.v3`。它必须绑定同 stem 的最终视频、封面、
  record、SRT、review manifest、冻结标题、最终 tags、StoryContract 与当前 package audit。
- package audit 必须为 `lidousha-review-package-audit.v2`，policy epoch 精确等于
  `2026-07-23.final-artifact-gates.v3`。audit schema 仍是 v2；policy fingerprint、
  auditor source hash 与完整
  portable `audited_inputs` 闭包有效。上传器在任何副作用前重跑当前 canonical auditor、
  严格 SRT 与共享标题门，并要求结果与 manifest 绑定一致；自报 `passed:true` 不算。
- talk 标题统一 `【李豆沙】` envelope，song 精确目录式；人工正文不能绕过外壳、长度或
  结构门。tags 必须逐项等于 audited record 的冻结结果。
- 没有当前 audit + `AUTO_UPLOAD` manifest + artifact hash gate 就没有发布。
- 授权上传/修复的证据必须 commit；媒体本身不因此入库。

## 新投稿流程

1. 在审片字节冻结后运行 `make-manifest`，只引用 package 内最终文件与 Ivan 授权原话。
2. 先运行 `verify`；它必须重算全部 hashes、current audit 与政策绑定。
3. `upload` 只从 manifest 取路径/标题/元数据，禁止再手输一套参数。ledger 对同 artifact
   重传硬拒；拿到 BVID 后不得为“再取一次结果”重跑上传。
4. 合集 lane 由冻结标题确定：talk → `小李切片`，song → `小李歌唱`。发布未精确入集不算
   完成；已投稿但合集/公开验证未闭环时只运行幂等 `season-add`，绝不重传。

滚动 24 小时配额由 ledger 与 Creator 近期稿件共同估算，取较大值；达到 10 条或 B 站返回
21566 时停止新投稿。配额不授权降低内容/审计门。

## 已发稿修复：只改同一个 BV

字幕、边界、片头、视频字节、标题、封面或 tags 的修复都编辑原稿，不新建 BV、不删稿。
唯一入口是 `scripts/authorized_upload.py repair-plan / repair-run / repair-status`，核心状态机
在 `src/autoslice/same_bv_repair.py`。legacy `swap_video_p.py`、`bili_archive_tool.py replace`、
裸 API 和手工 append/edit 仍禁止。

执行前必须确认当前 source 已部署到 `free`，目标修复包通过本页发布准入，并先完成真实
dry plan；本地存在代码/测试不等于 production 已可用，也不等于五条线上稿件已经修复。

同 BV `repair-plan` 只接受 authorized manifest 顶层 hash-bound
`recovery-same-bv-publication-authority.v1`。该 authority 必须由包内 record 与 review item
两面精确投影；命令行 `--bvid` 在 adapter 构造和任何网络 observe **之前**就须与 authority
BVID 相等。只读 live snapshot 随后还要证明 Creator/public/section 的 AID 与旧单 P CID 均
等于 authority；`same-bv-repair-plan.v2` 冻结该 authority，repair-run/status 每次恢复都与 manifest
重验。这样不能把 A 包靠错误 CLI 参数指向 B 稿件。

same-BV 的 member/season 登录读取统一走 `bilibili_member_api.load_cookie_pairs`：
`{cookie_info:{cookies:[…]}}` 与 `{data:{cookie_info:{cookies:[…]}}}` 两种真实形态都按
唯一 schema 严格解析；根节点/字段/条目异常、两形态同时出现、空值、重复 cookie 名或缺
`bili_jct` 都在网络请求前 fail closed，且错误不得回显 secret。`biliup append` 另由显式
`--biliup-cookie-json` 提供 CLI 所需的 top-level `cookie_info` 文件，不能把 app 嵌套形态暗中
改写后复用。当前默认分别是 API `--cookie-json /opt/bilive/app/cookie.json` 与 CLI
`--biliup-cookie-json /opt/bilive/app/tmp_manual_upload/biliup_cookies.json`；通过 parser 只
证明文件结构，执行前仍必须读回已部署 CLI/模块版本并验证当前登录态，本地双形态测试不能
代替 production login。

1. `repair-plan --manifest … --bvid … --out … --journal …` 先重跑 manifest/audit，再只读
   Creator/public/exact section；只接受同 BVID/aid、公开 state=0、Creator 恰一 P、public 与
   section CID 同一且 section membership 恰一条。plan 与初始 journal 都 create-only，绑定
   manifest、视频/封面 hashes、目标 metadata、旧 CID、BVID/aid 与 season/section。
2. 默认 journal/ledger 是
   `/opt/bilive/autoslice/reports/same_bv_repair_ledger.jsonl`，与新投稿共用
   `/opt/bilive/autoslice/upload.lock`。journal 为 hash-chain
   `same-bv-repair-journal.v1`；截断、改写、非法跳转、同 BVID 被另一 plan 占用或 artifact
   漂移都拒绝执行。
3. 先用 `repair-status` 和 `repair-run --dry-run` 查看本地状态/下一动作；两者不做远端写。
   真执行只用 `repair-run`，它可以跨进程反复 resume，返回 `0=VERIFIED`、
   `6=仍在安全等待/推进`、`5=BLOCKED_DRIFT`。
4. 状态为 `PLANNED → APPEND_INTENT → APPEND_AMBIGUOUS → TWO_P_READY →
   SWAP_RETRYABLE → CREATOR_SINGLE_NEW → PUBLIC_PENDING → VERIFIED`；任一确定性身份/
   topology/metadata 漂移进入终态 `BLOCKED_DRIFT`。
5. `APPEND_INTENT` 先 fsync 再且只再调用一次 existing-BV append；之后即使进程崩溃、响应
   丢失或 append 效果迟到，也只能 poll live Creator，绝不二次 append。找不到唯一新增 CID
   就停在 ambiguous，三 P、重复/未知 CID 直接阻断。
6. swap 只允许保留 journal 已冻结的新 CID，并以同一 metadata/cover asset identity 重试；
   21540 或 timeout 后先读 live state，只有仍是原两 P topology 才重发完全相同 payload。
   Creator 已成为唯一新 CID 后只读收敛，不再 edit。
7. `PUBLIC_PENDING` 只轮询瞬时不可用/未传播的 public、tags 与 section；只有
   Creator/public/exact section 的 CID、title/desc/tid/copyright/source/tags/cover 与 section
   episode title 全部一致才进入幂等终态 `VERIFIED`。

同 BV replacement 的完成证据必须持久化并同时证明：

1. 修复字节先通过当前 manifest/audit v2；
2. append 后 Creator 恰为旧 P + 新 P；
3. swap 后 BVID/aid 不变，Creator 恰一 P 且 CID 为新 CID；
4. public `state=0` 且 public CID 为新 CID；
5. public 与 Creator 的 title、desc、tid、copyright、source、tags 精确一致；
6. 目标 section 中 membership count 恰为 1、episode title 与最终发布标题精确一致，
   public season title/display 正常；
7. Creator/public 不存在本次修复产生的第二个重复 BVID；
8. replacement/public/season evidence 与 ledger 已入库并 commit。

`scripts/swap_video_p.py` 只完成 P 置换，不单独证明上述八项，永远不得把它当作发布闭环入口。

## 公开验收

只有以下四面同时匹配才记 ledger `rc=0` 和 completed sidecar：

- B 站 public view（可见、最终 CID/标题/简介/分区/版权/source/合集）；
- public tags；
- Creator archive view（唯一稿件、唯一 P、完整元数据）；
- 精确 season section API（唯一 membership、episode title 精确）。

公开/创作中心任一面尚在转码、重审或传播中，状态就是 `posted_unverified`；后续只能复验/
补合集，不得重复上传。

## 安全边界

- cookie、token、BVID、season/section ID 与当前登录态都从 live 环境读取或由工具验证，
  文档中的历史数字不构成 authority。
- `do_upload.sh`、裸 `biliup`、legacy app uploader 和历史 memory playbook 都不能绕过
  authorized manifest。
- emergency `--skip-season` 不构成完成状态；必须补跑公开闭环。
