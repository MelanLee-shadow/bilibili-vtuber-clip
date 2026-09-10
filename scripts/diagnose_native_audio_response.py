#!/usr/bin/env python3
"""One bounded diagnostic for each observed native-ASR schema failure class.

Keep the original failed matrix cells unchanged. These two fresh requests only
recover full raw diagnostics; they are not best-of-N replacement results.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import time
import subprocess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import evaluate_agy_replacement_paired as prior
from scripts import evaluate_astra_native_audio as study

CASES = (
    ("moss", "auto_113028_1602_1698", "t01", "exact"),
    ("mai", "auto_200130_1323_1603", "t01", "context"),
)


def one(provider, cid, target, mode):
    original = study.OUT / cid / target / mode / provider
    failed = study.load(original / "response.json")
    assert failed["status"] == "ERROR"
    folder = study.OUT / "schema-diagnostics" / provider
    prior.attempt(folder, "one_raw_failure_diagnostic")
    crop = study.load(original.parent / "crop.json")
    audio = (original.parent / "audio.mp3").read_bytes()
    assert study.sha(audio) == crop["audio_sha256"]
    duration = crop["crop_end_ms"] - crop["crop_start_ms"]
    used = sum(study.load(p)["duration_ms"] for p in (study.OUT / cid).glob("t*/*/*/receipt.json"))
    assert used + duration <= 60000
    start = time.monotonic()
    if provider == "moss":
        from src.autoslice import moss_transcription as client

        p = Path("/Users/op/Project/vtuber-slice/.env")
        assert not p.is_symlink() and not stat.S_IMODE(p.stat().st_mode) & 0o077
        key = None
        for line in p.read_text().splitlines():
            k, sep, v = line.strip().removeprefix("export ").partition("=")
            if sep and k.strip() == "MOSS_API_KEY":
                key = shlex.split(v, comments=False)[0]
        assert key
        os.environ["AUTOSLICE_MOSS_API_KEY"] = key
        os.environ.pop("AUTOSLICE_MOSS_API_KEY_FILE", None)
        metadata, payload = client._fetch_payload(audio)
        result = {"status": "RAW_CAPTURED", "metadata": metadata}
        try:
            segments, _ = client._parse_segments(payload, duration, evidence=True)
            result.update(parse_status="VALID_THIS_ATTEMPT", native_segments=segments)
        except client.MossTranscriptionError as exc:
            result.update(parse_status="REPRODUCED", reason_code=exc.reason_code, detail=str(exc))
        assert key not in json.dumps(result, ensure_ascii=False)
    else:
        code = prior.MAI_CODE
        needle = "result={'status':'ERROR','reason_code':getattr(e,'reason_code',type(e).__name__),'http_status':(getattr(e,'metadata',None) or {}).get('http_status')}"
        assert needle in code
        code = code.replace(
            needle,
            "result={'status':'ERROR','reason_code':getattr(e,'reason_code',type(e).__name__),'detail':str(e),'metadata':getattr(e,'metadata',None)}",
        )
        run = subprocess.run(
            [*prior.SSH, "wsl-codex", "python3 -B -u -c " + shlex.quote(code)],
            input=json.dumps(
                {
                    "audio": base64.b64encode(audio).decode(),
                    "audio_sha256": study.sha(audio),
                    "duration_ms": duration,
                }
            ),
            text=True,
            capture_output=True,
            timeout=400,
        )
        assert run.returncode == 0, "MAI diagnostic transport failed"
        result = json.loads(run.stdout)
    result.update(
        source_candidate=cid,
        target=target,
        mode=mode,
        input_audio_sha256=study.sha(audio),
        duration_ms=duration,
        wall_seconds=time.monotonic() - start,
        original_matrix_response_sha256=study.sha((original / "response.json").read_bytes()),
        diagnostic_only=True,
        replace_primary_result=False,
    )
    study.save(folder / "result.json", result)
    print(
        json.dumps(
            {k: v for k, v in result.items() if k not in ("metadata", "native_segments")},
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    for case in CASES:
        one(*case)
