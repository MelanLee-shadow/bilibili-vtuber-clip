"""usage_ledger.py PURPOSE START_ISO END_ISO LEDGER -- Gemini usage from the CPA ledger, read-only.

Run on OCI3 (the ledger lives there).  Prints one JSON line: calls, responded, input, output, thinking tokens,
output+thinking, and per model.  Read-only: it never writes to the ledger."""
import collections
import json
import sys

PURPOSE, START, END, LEDGER = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]   # LEDGER: the lane passes it
calls = ok = err = 0
tok = collections.Counter()
per_model = collections.defaultdict(lambda: collections.Counter())
with open(LEDGER, encoding="utf-8") as fh:   # streamed: the ledger grows with every call
    for line in fh:
        row = json.loads(line)
        if row.get("purpose") != PURPOSE:
            continue
        ts = row.get("recorded_at", "")
        if not (START <= ts < END):
            continue
        if row.get("status") == "DISPATCH_RESERVED":
            continue
        calls += 1
        if row.get("status") == "RESPONDED":
            ok += 1
            u = row.get("usage") or {}
            for key, name in (("promptTokenCount", "input"), ("candidatesTokenCount", "output"),
                              ("thoughtsTokenCount", "thinking"), ("totalTokenCount", "total")):
                tok[name] += int(u.get(key) or 0)
                per_model[row.get("identity_model")][name] += int(u.get(key) or 0)
            per_model[row.get("identity_model")]["calls"] += 1
        else:
            err += 1
            per_model[row.get("identity_model")]["errors"] += 1
print(json.dumps({"purpose": PURPOSE, "window": [START, END], "calls": calls, "responded": ok, "not_responded": err,
                  "input_tokens": tok["input"], "output_tokens": tok["output"], "thinking_tokens": tok["thinking"],
                  "total_tokens": tok["total"], "output_plus_thinking": tok["output"] + tok["thinking"],
                  "per_model": {k: dict(v) for k, v in per_model.items()}}, ensure_ascii=False))
