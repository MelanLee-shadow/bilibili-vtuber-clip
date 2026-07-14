#!/bin/bash
# 单条手动上传（部署为 free:/opt/bilive/app/tmp_manual_upload/do_upload.sh）。
# biliup 只跑一次; rc=0 即成功; 绝不为取 bvid 重跑 (2026-07-04 教训)。
#
# 2026-07-09 起：禁止裸调。必须经 scripts/authorized_upload.py upload
# （manifest 绑定审过的文件 hash + 幂等账本），它会带 AUTHORIZED_UPLOAD=1。
set -uo pipefail
if [ "${AUTHORIZED_UPLOAD:-}" != "1" ]; then
  echo "REFUSE: direct invocation is disabled." >&2
  echo "Use: python3 /opt/bilive/autoslice/repo/scripts/authorized_upload.py upload --manifest <manifest.json>" >&2
  echo "(make the manifest at review time with: authorized_upload.py make-manifest ...)" >&2
  exit 4
fi
cd /opt/bilive/app/tmp_manual_upload
VIDEO="$1"; COVER="$2"; TITLE="$3"
# $4 = manifest 冻结的完整 tag 行（authorized_upload.py 传入, 含基础位; 上限12
# 已实测）。无 tags 的旧 manifest 回退基础4位（Ivan 2026-07-13 口径, 砍 VUP/VTuber）。
TAGS="${4:-李豆沙,虚拟主播,虚拟UP主,直播切片}"
DESC="李豆沙个人主页：https://space.bilibili.com/1703797642
李豆沙直播间：https://live.bilibili.com/22966160"
/opt/bilive/bin/biliup -u biliup_cookies.json upload "$VIDEO" \
  --cover "$COVER" --copyright 2 --source "https://live.bilibili.com/" --tid 21 \
  --title "$TITLE" --tag "$TAGS" \
  --desc "$DESC" --submit app 2>&1 | tee /tmp/last_upload.log
rc=${PIPESTATUS[0]}
echo "rc=$rc"
# biliup's ResponseData debug print varies by version: `bvid: String("BV..")`
# vs `"bvid": String("BV..")` (2026-07-10 APP submit) — match both.
grep -oE "\"?bvid\"?: String\(\"BV[A-Za-z0-9]+\"\)" /tmp/last_upload.log | grep -oE "BV[A-Za-z0-9]+" | head -1 | sed "s/^/BVID=/"
exit $rc
