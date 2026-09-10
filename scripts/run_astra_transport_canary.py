#!/usr/bin/env python3
"""Exercise the exact candidate CPA shell transport without deploying it."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.autoslice.subtitle_draft_preparation import _required_cpa_cues

SSH = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=12"]
REMOTE = "/home/user/vtuber-astra-eval-20260909"
CODE = """
from pathlib import Path
import sys,json,os,subprocess,hashlib,time
sys.path.insert(0,'/opt/bilive/autoslice/repo')
from src.autoslice.llm_client import runtime_cpa_command_environment
from src.autoslice.provider_slots import runtime_provider_slot
request=json.load(sys.stdin)
root=Path('/home/user/vtuber-astra-eval-20260909')
wrapper=root/'llm_via_cpa-candidate.sh'
assert hashlib.sha256(wrapper.read_bytes()).hexdigest()==request['wrapper_sha256']
out=root/'native-wrapper-canary'
out.mkdir(mode=0o700,exist_ok=False)
prompt=out/'prompt.txt';response=out/'response.txt'
prompt.write_text(request['prompt'])
env={**os.environ,**runtime_cpa_command_environment(Path('/opt/bilive/autoslice'))}
start=time.monotonic()
with runtime_provider_slot(runtime_root='/opt/bilive/autoslice',timeout_seconds=300):
 queue=time.monotonic()-start
 run=subprocess.run(['bash',str(wrapper),str(prompt),str(response),'gpt-6-astra','low','1'],env=env,capture_output=True,text=True,timeout=600)
text=response.read_text() if response.is_file() else ''
assert env['CPA_API_KEY'] not in text and env['CPA_API_KEY'] not in run.stderr
result={'exit_code':run.returncode,'model_requested':'gpt-6-astra','effort':'low',
 'response':text,'transport_log':run.stderr,'queue_seconds':queue,
 'wall_seconds':time.monotonic()-start,'wrapper_sha256':request['wrapper_sha256']}
(out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\\n')
print(json.dumps(result,ensure_ascii=False))
"""


def main():
    wrapper = ROOT / "scripts/llm_via_cpa.sh"
    digest = hashlib.sha256(wrapper.read_bytes()).hexdigest()
    out = ROOT / "reports/astra-native-audio-20260909/native-wrapper-canary.json"
    if out.exists():
        raise SystemExit("Canary receipt already exists; inspect before a new call.")
    subprocess.run(
        [
            "scp",
            "-q",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            str(wrapper),
            "oci3:" + REMOTE + "/llm_via_cpa-candidate.sh",
        ],
        check=True,
    )
    originals = ["不是十五，是五十。", "あれ？", "重复重复，不要改成一次。"]
    prompt = (
        "仅作无改字的传输验收。逐字返回下面三条文本，不翻译、不改数值、不消除重复。"
        '输出JSON {"cues":[{"n":1,"text":"原文"},...]}。输入：'
        + json.dumps(originals, ensure_ascii=False)
    )
    request = {"prompt": prompt, "wrapper_sha256": digest}
    run = subprocess.run(
        [
            *SSH,
            "oci3",
            "sudo -n /opt/bilive/autoslice/venv-main/bin/python -B -c " + shlex.quote(CODE),
        ],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=950,
    )
    if run.returncode:
        raise RuntimeError("Canary transport failed before valid result: " + str(run.returncode))
    result = json.loads(run.stdout)
    parsed = _required_cpa_cues(prompt, lambda _: result["response"], len(originals))
    result["exact_text_validation"] = parsed == dict(enumerate(originals, 1))
    result["no_production_mutation"] = True
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    assert result["exit_code"] == 0 and result["exact_text_validation"]


if __name__ == "__main__":
    main()
