# 85 成品审查车道（finished-media review lane，FMR）：上传兼容接口

现有上传工具（`audit_review_package.py`、`authorized_upload.py`）不接收手动做的成品包。
本车道只做一件事：把手动包接到上传工具上。它不规定怎么审查，也不复刻审查。

## 授权与口径

- 公开规则（U32）：「这个不是流水线慢的主要原因。这个可以新建一个审查车道。」
- 公开规则（U45）：「难道我以为你说的审查车道就是单纯的为了一个兼容接口而已？」，回复「可以，照做」。
- 公开规则（U44）担心「一次一次incremental的增加这个规则…让它自己变成一个逐步复杂的另一个流水线的复刻品」。
- PUB-02 / V1556（2026-09-27）：「通过审查的切片可以直接上传，我授权。当然如果没有通过流水线审查的切片仍然需要修改直到通过才能上传」。
 本车道口径：手动成品的回执 `verdict=PASS` 视同审查通过，可进入 make-manifest；
 上传授权仍只来自 `--quote` 的 公开规则原话与出版登记门，回执本身不是授权。

## 这条车道不该再加审查类机器门

审查是复审人的工作，不是车道的工作。怎么审、查什么、按什么原则判，见
运营方的 `MANUAL-SLICE-INTENT.md`
（「怎么做」与「审查车道是什么」两节），本文档不规定。

车道只认：复审结论为 PASS、复审人与制作人不同、各文件哈希对得上。
再有人想加某项「复审必须覆盖 X」「证据必须含 Y」「某字段须满足 Z」的机器校验，就是 U44 担心的那条路：
把车道变成又一条流水线。新的审查要求写进复审人的原则与经验（`MANUAL-SLICE-INTENT.md`、复审报告），
不写进 `src/autoslice/finished_media_review/`，也不写进 `audit_package` 的 FMR 分派。
只有「防出事的门」（文件没被换、标题前缀与简介、出版登记、查重、配额）可以加，且应加在上传门，不在本车道。

## 不变量

1. 不放宽老车道任何门：分派只在 `audit_package`（`schema_version == finished-media-review-package.v1`）
   与 `authorized_upload.py` 的 FMR 回执槽位上生效；其它 schema 走原路径，
   `tests/finished_media_review/test_lane_guards.py::test_other_manifest_schemas_take_the_legacy_audit_path`。
2. 车道不改字幕、不改媒体。
3. 非审查类安全门照旧，由上传门执行、车道不重复实现：SRT 释放校验、标题 policy 与前缀信封、简介可渲染、
   标签门、record 产物哈希、声文对应重放、分区版权 source、出版登记、ledger、配额、锁、查重、公开核验、授权来源。
   `_validate_v3_package_attestation` 通用体完整保留，FMR 只替换其中的 CPA 题封质检回执，
   并把 `story_contract_required` 对 FMR 包关闭（FMR record 没有 story_contract，也不伪造）。
4. 出版登记对 FMR record 生效：`publication_identity()` 对 `record_kind == "FINISHED_MEDIA_REVIEW"` 且只有
   `delivery_candidate_id` 的 record 以该 ID 作 `registry_candidate_id`；没有这条分支登记门会因空 ID 静默放行。

## 入口与流程

`scripts/finished_media_review.py`：

| 子命令 | 作用 |
|---|---|
| `init --member M --out-root R` | 校验成员输入，建 `R/out/<date>/<candidate_id>/`，文件硬链接进包（字节不变），stem ≡ candidate_id ≡ `fm-<YYYYMMDD>-<slug>` |
| `check --package P [--tags-from F] [--no-jianying] [--bcut-recorded F] [--jianying-recorded F]` | 跑 A 类自动检查工具，写 `evidence/`（含 `auto-checks.json`）、`nominations.json`、`<cid>.record.json`、`findings.md`。FAIL/FLAG 只列入 `findings.md` 供复审人处理，不拦截回执 |
| `attest --package P --review REVIEW.md --reviewer-id X --verdict PASS\|FAIL [--reviewer-kind AGENT\|维护者]` | 把复审报告复制进包为 `REVIEW.md`，写 `<cid>.finished-media-review.json`；复审人等于制作人、缺 `REVIEW.md`、未跑 `check` 均拒绝 |
| `status --package P` | 打印 `findings.md` |
| `seal --package P` | 仅 PASS 回执：写 `review_manifest.json` 并跑车道审计 |

之后：`audit_review_package.py <root> --json > package_audit.json`（走 FMR 分派）→
`authorized_upload.py make-manifest`（自动绑定包内回执；无需 `--title-cover-qc`）→ `verify` → `upload`。

`REVIEW.md` 是复审人写的人能读的说明（自由格式：查了什么、怎么判的、证据在哪），车道不解析其内容。
成品改动后须重新 `check`、复审、`attest`。

成员输入 `finished-media-member.v1`：成品文件、来源区间（`source.pieces`，flv 绝对路径、起止 ms）、
`source.context_srt`、`maker`（kind + id）、钩子句、疑点表 `doubts`、`cover_claims`、可选 `description_extra`。
封面必须是真实 PNG。

## A 类自动检查（工具，不是门）

`check` 跑 A1–A13（文件与来源哈希、SRT、ASS/speaker 对等、BCUT 与剪映重听、解码与流、片头、来源区间、术语、
标题引文、标签、封面、说话人抽帧、外部查重），结果与证据写进包内，其中 A1 的文件哈希与 record 的字段
（`pieces`、`upload_tags`、声文对应）也是上传桥需要的数据；某项失败使 record 缺字段时 record 不会写出，包也就无法封存。
唯一例外是 A5 的解码失败（`FMR_A5_FULL_DECODE_FAILED`/`FMR_A5_PROBE_FAILED`）：上传门不解码媒体，这是防出事的门，车道审计保留阻断（`FMR_MEDIA_NOT_DECODABLE`）。
其余 A 项的 FAIL/FLAG 由复审人在 `REVIEW.md` 里处理，车道不据此拦截。

## 回执与审计

回执 `finished-media-review-receipt.v2` 只含：`schema_version`、`candidate_id`、`recording_date`、`maker`、
`reviewer`（kind + id）、`verdict`、`bindings`（8 个成品文件 + member + record 的路径与哈希）、`title_sha256`、
`auto_checks`（`evidence/auto-checks.json` 哈希，证明 `check` 跑过）、`review`（`REVIEW.md` 哈希）、`created_at`。

车道审计 `finished_media_review.audit.package_problems` 只核对：

- 包清单是 FMR schema、单 item，member 结构合法，候选身份与日期一致；
- 回执 schema 版本正确、`verdict == PASS`、`reviewer.id != maker.id`；
- 成品各文件、member、record、`auto_checks`、`REVIEW.md` 的哈希与回执一致，`review_manifest.json` 里的哈希与当前文件一致；
- `auto-checks.json` 结构完整，且 A1 记录的文件哈希等于当前成品字节（即 `check` 跑的就是这份成品）；
- record 的身份、产物哈希、来源区间与 `auto-checks.json` 一致（出版登记与查重身份依赖它）。
- A5 未报告解码失败（见上）。

`audit_package` 的 FMR 分派返回同格式（`lidousha-review-package-audit.v2`）审计 JSON，
故 `authorized_upload` 里「审计现场重算并比对内容绑定」原样适用。

`authorized_upload.py`：make-manifest 在 FMR 包上自动把包内回执同时绑为 `package_attestation.finished_media_review`
与 `title_cover_qc`；ledger 里 `title_cover_qc_sha256` 列对 FMR 行即回执哈希，`package_audit_sha256` 列即车道审计 JSON 哈希。
record 为 `finished-media-review-record.v1`，无 story_contract / boundary_audit / scorecard。

## description_extra

成员输入可选字段 `description_extra`（公开规则10-30 要求长合集简介逐字附话）。取值二选一：一个字符串（一行），或字符串列表
（每项独占一行，按顺序）。渲染在频道固定行与「相关主播」行之后。同一定义 `description_for_title(title, submission=…, extra=…)`
供 make-manifest、verify 重算与 upload 第五个参数使用，逐字相等才通过；manifest 的 `description_extra` 必须与回执绑定的
成员字段完全相等（字符串与列表不互换，列表不得被拍平成带换行的字符串、不得重排或删项），改清单绕不过。
文字校验 `channel_envelope.description_extra_problems`，对每一行：非空、无首尾空白、无控制字符与换行、过
`assert_audience_text(surface="description")`；列表不得为空、每项必须是字符串；整块（各行用换行连起来）不超过
`DESCRIPTION_EXTRA_MAX_CHARS`（300，受众文本合理性边界，非平台契约；按整块计，所以单个字符串口径不变，多行也不会多出预算）。
渲染的逆运算是 `channel_envelope.parse_description_extra(description, base_description)`：给定不带 extra 的简介，
从读回的简介还原出各行（单项列表与单个字符串渲染相同，还原结果是行元组）。不填时简介与现行逐字节相同。
`--description-extra` 是单个字符串，仅 FMR 包可用，且必须等于成员字段；成员字段是列表时不要传它。

## 已发稿的同 BV 置换

依据 PUB-05：已发布视频要修，走编辑同一稿件（`authorized_upload.py repair-plan/repair-run`，规则见 90 号文档「已发稿修复：只改同一个 BV」），
不新上传。这一节只写 FMR 包怎样接上那条入口；不增加任何审查门，也不放松已有门。视频、标题、封面可在同一次置换里一起换
（`repair-run` 的一次编辑同时写入新 CID、新标题与新封面），标签与简介不动。

接口兼容的三处：

- `check`/`seal` 的 `--repair-authority <json>` 把同一份 `new-bv-same-bv-publication-authority.v1` 写进 record 与 review item 的
  `recovery_publication_authority`（先经 `validate_new_bv_repair_authority`）。不传则 record、清单与现在逐字节相同。
  record 带 authority 时 `seal` 必须传同一份，否则拒绝。
- 同一条 `check`/`seal` 入口可另传 `--title-receipt <json>`；它必须同时有 `--repair-authority`，并由
  `validate_title_repair_successor` 完整重放 FMR 标题回执链。通过后 record 写入
  `verified_title_repair.receipt={path,sha256}`；record 带该引用时 `seal` 必须传同一份，否则拒绝。
- FMR record 没有 `story_contract`，其候选身份取 `publication_identity()` 的判定（`delivery_candidate_id`），不另写规则。
- `final_human_review.replay_final_human_review_attestation` 认 FMR 回执（`finished-media-review-receipt.v2`）：规则就是本车道审计
  `package_problems`（PASS、复审人不等于制作方、全部哈希绑定），不复制。

命令顺序（`$NEW` 为新包目录，`$PUB` 为首发的 `<manifest>.publication_reconciliation.json`，所有改动都要重走复审，旧回执不能续用）：

1. 在发稿主机（证据文件所在，可能需要 root 读）构建 authority，标题用新标题：
 `python scripts/build_new_bv_repair_authority.py --publication $PUB --candidate-id <cid> --title-file <新 title.txt> --out authority.json`。
2. 制作方把 member 的 `files.title`、`files.cover`（及 `cover_claims`）换成新稿，`init` 出新包；内容标签要对着新标题重新生成
 （`check` 的 A10 绑定标题哈希），线上标签由第 7 步的 `--preserve-existing-tags` 保留。
3. `check --package $NEW --repair-authority authority.json [--authority-shape-only]`。制作机看不到首发证据文件时加
 `--authority-shape-only`（只跳过证据文件重放，字段与摘要仍校验；后面的 make-manifest、verify、repair-plan 全量重放）。
4. 复审人独立复审后 `attest`；`seal --package $NEW --repair-authority authority.json [--authority-shape-only]`。
5. 把包整体复制到发稿主机，核对哈希，运行 `audit_review_package.py $NEW --json`，把审计 JSON 拷进 `$NEW/package_audit.json`
 （replay 要求审计在包目录内）。
6. `authorized_upload.py make-manifest --video … --cover … --package-audit $NEW/package_audit.json --title "$(cat $NEW/<cid>.title.txt)"
 --final-human-review $NEW/<cid>.finished-media-review.json --authorized-by 公开规则--quote <原话> --out replace-manifest.json`，再 `verify`。
 同 BV 的 manifest 不得运行 `upload`。
7. `authorized_upload.py repair-plan --manifest replace-manifest.json --bvid <BV> --out plan.json --journal <ledger> --preserve-existing-tags --dry-run`；
 核对 plan 的 video/cover 哈希、`target_metadata`，去掉 `--dry-run` 落 plan，再 `repair-status`、`repair-run --dry-run`；
 真实 `repair-run` 与 `repair-verify-live` 的放行与验收按 90 号文档。

### 只改标题

FMR 稿件发布后只需改标题时用 `authorized_upload.py title-repair --title-authority <fmr-title-authority.v1>`，
不需要 source-fact 审阅产物；依据与不放宽的部分见 [90](90-publish.md) 中 `title-repair` 的 FMR 分支。
