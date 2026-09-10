#!/usr/bin/env python3
"""Task-private CPA Responses transport with measured model/usage metadata.

Runs on OCI3; credentials stay in the existing runtime loader. It never calls
an upstream provider directly, never falls back to GPT-5, and never prints keys
or private reasoning content. Used for controlled model/effort experiments.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request


def message_text(payload: dict) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    return "".join(
        part.get("text", "")
        for item in payload.get("output", [])
        if isinstance(item, dict) and item.get("type") == "message"
        for part in item.get("content", [])
        if isinstance(part, dict) and part.get("type") in ("output_text", "text")
    )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main() -> None:
    req = json.load(sys.stdin)
    if (
        set(req) - {"prompt", "model", "effort"}
        or req.get("model") != "gpt-6-astra"
        or req.get("effort") not in ("low", "medium", "high")
        or not isinstance(req.get("prompt"), str)
        or not 0 < len(req["prompt"]) <= 150000
    ):
        raise ValueError("INVALID_ASTRA_EVALUATION_REQUEST")
    sys.path.insert(0, "/opt/bilive/autoslice/repo")
    from src.autoslice.llm_client import runtime_cpa_command_environment
    from src.autoslice.provider_slots import runtime_provider_slot

    env = runtime_cpa_command_environment(Path("/opt/bilive/autoslice"))
    key = env["CPA_API_KEY"]
    body = json.dumps(
        {
            "model": req["model"],
            "input": req["prompt"],
            "reasoning": {"effort": req["effort"]},
            "max_output_tokens": 16000,
        },
        ensure_ascii=False,
    ).encode()
    started = time.monotonic()
    result = {
        "requested_model": req["model"],
        "requested_effort": req["effort"],
        "prompt_sha256": hashlib.sha256(req["prompt"].encode()).hexdigest(),
        "cache_replay": False,
        "attempts": [],
    }
    opener = urllib.request.build_opener(NoRedirect())
    with runtime_provider_slot(runtime_root="/opt/bilive/autoslice", timeout_seconds=300):
        result["queue_seconds"] = time.monotonic() - started
        for i in range(2):
            call_start = time.monotonic()
            request = urllib.request.Request(
                env["CPA_BASE_URL"].rstrip("/") + "/responses",
                data=body,
                headers={
                    "Authorization": "Bearer " + key,
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0",
                },
            )
            try:
                with opener.open(request, timeout=240) as response:
                    raw = response.read(8_000_001)
                    if len(raw) > 8_000_000:
                        raise ValueError("RESPONSE_TOO_LARGE")
                    payload = json.loads(raw)
                text = message_text(payload)
                result["attempts"].append(
                    {
                        "attempt": i + 1,
                        "status": "OK" if text else "EMPTY",
                        "seconds": time.monotonic() - call_start,
                        "http_status": 200,
                    }
                )
                if key in text:
                    raise ValueError("CREDENTIAL_ECHO_REJECTED")
                if payload.get("model") != req["model"]:
                    result.update(
                        status="ERROR",
                        reason_code="RETURNED_MODEL_MISMATCH",
                        returned_model=payload.get("model"),
                    )
                    break
                if text and payload.get("status") == "completed":
                    usage = payload.get("usage") or {}
                    result.update(
                        status="OK",
                        response=text,
                        returned_model=payload.get("model"),
                        response_status=payload.get("status"),
                        usage={
                            k: usage[k]
                            for k in (
                                "input_tokens",
                                "output_tokens",
                                "total_tokens",
                                "input_tokens_details",
                                "output_tokens_details",
                            )
                            if k in usage
                        },
                        response_sha256=hashlib.sha256(raw).hexdigest(),
                    )
                    break
                result.update(
                    status="ERROR",
                    reason_code="INCOMPLETE_OR_EMPTY_RESPONSE",
                    response_status=payload.get("status"),
                )
            except Exception as exc:
                http = getattr(exc, "code", None)
                result["attempts"].append(
                    {
                        "attempt": i + 1,
                        "status": "ERROR",
                        "error_type": type(exc).__name__,
                        "http_status": http if type(http) is int else None,
                        "seconds": time.monotonic() - call_start,
                    }
                )
                result.update(status="ERROR", reason_code="CPA_REQUEST_FAILED")
                if http is not None and http not in (408, 429) and http < 500:
                    break
            if i == 0:
                time.sleep(2)
    result["remote_wall_seconds"] = time.monotonic() - started
    encoded = json.dumps(result, ensure_ascii=False)
    if key in encoded:
        raise ValueError("CREDENTIAL_ECHO_REJECTED")
    print(encoded)


if __name__ == "__main__":
    main()
