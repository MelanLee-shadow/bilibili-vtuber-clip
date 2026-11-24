#!/usr/bin/env python3
"""OCI3 only.  Score units with an exported speaker classifier (classifier.json from speaker_truth.py retrain, or the
frozen v8 one): the v8 scorer's formula for any number of classes.  Per block x=(raw-mean)/sd*block_scale (NaN->0),
each base softmax(coef@x+b); stacker input = the bases' log p in order; softmax(stacker); then softmax(log p * T).

  speaker_score.py --emb emb_units.npz --feats feats.npz --clf classifier.json --out pred.json
"""
import argparse
import json

import numpy as np


def softmax(z):
    z = z - z.max(1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(1, keepdims=True)


def main():
    ap = argparse.ArgumentParser()
    for k in ("emb", "feats", "clf", "out"):
        ap.add_argument("--" + k, required=True)
    a = ap.parse_args()
    clf = json.load(open(a.clf, encoding="utf-8"))
    E, F = np.load(a.emb), np.load(a.feats)
    ids = [str(i) for i in E["ids"]]
    assert ids == [str(i) for i in F["ids"]], "emb and feats ids differ"
    emb = E["emb"].astype(np.float64)
    emb = emb / np.maximum(np.linalg.norm(emb, axis=-1, keepdims=True), 1e-9)
    blocks = {"emb": emb, "mic": F["feats"].astype(np.float64), "voice": F["voice"].astype(np.float64),
              "bre": F["bre"].astype(np.float64)}
    zs = []
    for base in clf["bases"]:
        xs = []
        for b in base["blocks"]:
            st = base["standardize"][b]
            Zb = (blocks[b] - np.array(st["mean"])) / np.array(st["sd"])
            xs.append(np.where(np.isnan(Zb), 0, Zb) * st["block_scale"])
        p = softmax(np.hstack(xs) @ np.array(base["coef"]).T + np.array(base["intercept"]))
        zs.append(np.log(np.clip(p, 1e-12, 1)))
    stk = clf["stacker"]
    P = softmax(np.hstack(zs) @ np.array(stk["coef"]).T + np.array(stk["intercept"]))
    T = float(clf["temperature"])
    Pc = softmax(np.log(np.clip(P, 1e-9, 1)) * T)
    classes = clf["class_order"]
    out = [dict(id=i, probs={c: round(float(Pc[k][j]), 6) for j, c in enumerate(classes)}, ok=bool(E["ok"][k]))
           for k, i in enumerate(ids)]
    json.dump(dict(classes=classes, temperature=T, version=clf.get("version", "v8"), predictions=out),
              open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("wrote", a.out, len(out), flush=True)


if __name__ == "__main__":
    main()
