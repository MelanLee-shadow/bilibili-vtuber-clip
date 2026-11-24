#!/usr/bin/env python3
"""OCI3 only.  The speaker truth store and the retraining loop of the voiceprint speaker classifier (维护者 11-23:
「你至少得根据我的标注，不断的提升我们自己流水线的区分说话人的能力啊」).

Every label set 维护者 gives (a labelling page export) becomes one set in the store: the clip's audio, one unit per
labelled line, the labels, and the v8 features of those units (CAM++ embedding, microphone features, voice statistics,
computed by the frozen v8 code through speaker_v8_run.sh, so training and production read the same features).  The
frozen v8 workspace (the 10-30 trio session, 1013 lines) is the base set.

  add-set   one label export -> one set (features computed on the host)
  retrain   fit the v8 recipe (late-fusion stacking: per-block LR on emb / mic / voice -> LR) on the base and every
            training set, and measure it against the classifier production uses now on lines it was not trained on:
            each set is scored by a model fitted without its whole session (and, separately, without only that set);
            the base set by 8-fold question-grouped cross-validation.  Writes report.json and a version directory with
            the layout speaker_v8_run.sh reads (code/, in/truth_rows.json, out/final/classifier.json, score.py).

Nothing here switches production: the profile's speaker_v8 paths are changed by a commit, after the report shows the
new version is better on the held-out lines (scripts/speaker_label_page.py, docs/pipeline/manual-lane-supervision.md).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

OTHER = "其他"                       # a voice that is not a participant (a video, a game): its own class
GUEST_ANY = "嘉宾（不分是谁）"
OTHER_SPELLINGS = {"其他", "其他人", "视频", "动画"}   # and 「非<主播>」
SKIP = {"", "None", "未知", "不确定", "删掉", "混说", "一句多人"}
SPECS = [(("emb",), 0.3), (("mic",), 0.1), (("voice",), 0.1)]




BLOCK_C = {"emb": 0.3, "mic": 0.1, "voice": 0.1, "bre": 0.1, "text": 1.0}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def softmax(z):
    z = z - z.max(1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(1, keepdims=True)


def l2(x):
    return x / np.maximum(np.linalg.norm(x, axis=-1, keepdims=True), 1e-9)


def norm_speaker(s):
    s = str(s or "").strip()
    if s in SKIP:
        return None
    if s in OTHER_SPELLINGS or (s.startswith("非") and len(s) > 1):
        return OTHER
    if s == GUEST_ANY or s in {"连线", "嘉宾"}:
        return GUEST_ANY
    return s


# ------------------------------------------------------------------------------------------------ add-set
def srt_cues(path):
    out = {}
    for block in Path(path).read_text(encoding="utf-8").strip().split("\n\n"):
        lines = block.strip().split("\n")
        m = re.match(r"(\d+):(\d+):(\d+)[,.](\d+) --> (\d+):(\d+):(\d+)[,.](\d+)", lines[1] if len(lines) > 1 else "")
        if not m:
            continue
        g = list(map(int, m.groups()))
        out[int(lines[0])] = (((g[0] * 60 + g[1]) * 60 + g[2]) * 1000 + g[3], ((g[4] * 60 + g[5]) * 60 + g[6]) * 1000 + g[7],
                              re.sub(r"^\[[^\]]*\]\s*", "", " ".join(lines[2:])))
    return out


def labelled_lines(a):
    """[(key, start_ms, end_ms, speaker as written, text)] in the media's timeline."""
    doc = json.loads(Path(a.labels).read_text(encoding="utf-8"))
    labels = doc.get("labels", doc)
    rows = []
    if a.srt:                     # speaker_label_page.py export: {"cid:n": speaker}; times from the clip's final.srt
        cues = srt_cues(a.srt)
        for k, v in labels.items():
            cid, _, n = str(k).rpartition(":")
            if cid != a.clip or not n.isdigit() or int(n) not in cues:
                continue
            s, e, text = cues[int(n)]
            rows.append((k, s + a.offset_ms, e + a.offset_ms, v if isinstance(v, str) else v.get("speaker"), text))
    else:                         # older pages: {key: {speaker, abs_start_ms, abs_end_ms, text, in_cut, use}}
        for k, v in labels.items():
            if a.key_prefix and not str(k).startswith(a.key_prefix):
                continue
            if not isinstance(v, dict) or v.get("in_cut") is False or "abs_start_ms" not in v:
                continue
            rows.append((k, int(v["abs_start_ms"]) + a.offset_ms, int(v["abs_end_ms"]) + a.offset_ms, v.get("speaker"),
                         v.get("text", "")))
    return doc, rows


def add_set(a):
    root = Path(a.root)
    d = root / "sets" / a.set
    if d.exists() and not a.replace:
        sys.exit(f"set {a.set} exists (--replace to rebuild)")
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    doc, rows = labelled_lines(a)
    units, labels, skipped = [], [], {}
    for i, (k, s, e, spk, text) in enumerate(sorted(rows, key=lambda r: r[1])):
        sp = norm_speaker(spk)
        if sp is None or e - s < 200 or s < 0:
            skipped[str(spk)] = skipped.get(str(spk), 0) + 1
            continue
        uid = f"{a.set}-u{i}"
        units.append(dict(id=uid, seg=a.set, s=int(s), e=int(e), lw=int(e), kind="line"))
        labels.append(dict(id=uid, key=str(k), speaker=sp, written=str(spk), text=text, dur_s=round((e - s) / 1000, 2)))
    meta = dict(set=a.set, session=a.session, use=a.use, media=str(a.media), offset_ms=a.offset_ms, labels_file=str(a.labels),
                exported_at=doc.get("exported_at"), page=doc.get("page") or doc.get("package") or doc.get("cues_version"),
                changed=len(doc.get("changed") or []), n_units=len(units), skipped=skipped,
                speakers={s: sum(1 for r in labels if r["speaker"] == s) for s in sorted({r["speaker"] for r in labels})},
                note=a.note or "")
    (d / "units.json").write_text(json.dumps({"units": units}, ensure_ascii=False), encoding="utf-8")
    (d / "labels.json").write_text(json.dumps({"rows": labels}, ensure_ascii=False, indent=0), encoding="utf-8")
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    log("set", a.set, json.dumps(meta["speakers"], ensure_ascii=False), "skipped", skipped)
    env = dict(os.environ, SPEAKER_V8_CODE_DIR=a.v8_code, SPEAKER_V8_PYTHON=a.v8_python, SPEAKER_V8_MODEL=a.v8_model,
               SPEAKER_V8_SCORE=a.v8_score)
    r = subprocess.run(["bash", a.run_script, a.set, str(a.media), str(d / "work"), str(d / "units.json")], env=env,
                       capture_output=True, text=True)
    if "SPEAKER_V8_DONE" not in r.stdout:
        sys.exit(f"features failed: {r.stdout[-300:]} {r.stderr[-300:]}")
    for f in ("emb_units.npz", "feats.npz", "pred.json"):
        shutil.copy(d / "work" / "out" / f, d / f)
    shutil.rmtree(d / "work" / "audio", ignore_errors=True)
    shutil.rmtree(d / "work" / "wav", ignore_errors=True)
    log("features done", a.set)


# ------------------------------------------------------------------------------------------------ data
class Rows:
    """All labelled lines: blocks emb/mic/voice, label, set, session, cross-validation group, duration."""

    def __init__(self):
        self.emb, self.mic, self.voice, self.bre, self.text = [], [], [], [], []
        self.spk, self.set, self.session, self.group, self.dur, self.use, self.v8 = [], [], [], [], [], [], []

    def add(self, emb, mic, voice, spk, set_, session, group, dur, use, v8probs, bre=None, text=""):
        self.bre.append(bre)
        self.text.append(text or "")
        self.emb.append(emb)
        self.mic.append(mic)
        self.voice.append(voice)
        for lst, v in ((self.spk, spk), (self.set, set_), (self.session, session), (self.group, group), (self.dur, dur),
                       (self.use, use), (self.v8, v8probs)):
            lst.append(v)

    def done(self):
        self.emb = l2(np.asarray(self.emb, dtype=np.float64))
        self.mic = np.asarray(self.mic, dtype=np.float64)
        self.voice = np.asarray(self.voice, dtype=np.float64)
        self.bre = np.asarray(self.bre, dtype=np.float64)
        self.text = np.asarray(self.text, dtype=object)
        for k in ("spk", "set", "session", "group", "use"):
            setattr(self, k, np.asarray(getattr(self, k)))
        self.dur = np.asarray(self.dur, dtype=np.float64)
        self.n = len(self.spk)
        return self

    def block(self, b):
        return {"emb": self.emb, "mic": self.mic, "voice": self.voice, "bre": self.bre}[b]


def load_base(R, base, host):
    """The frozen v8 workspace's truth rows (10-30 trio session), grouped by question as v8 was."""
    U = json.load(open(base / "in" / "units.json"))["units"]
    uidx = {u["id"]: k for k, u in enumerate(U)}
    E = np.load(base / "out" / "emb_units.npz")
    F = np.load(base / "out" / "feats.npz")
    cues = {c["id"]: c for c in json.load(open(base / "in" / "all_cues.json"))["cues"]}
    T = json.load(open(base / "in" / "truth.json"))
    n = 0
    for r in T["rows"]:
        k = uidx[r["id"]]
        if not E["ok"][k]:
            continue
        q = cues[r["id"]]["q"] if r["id"] in cues else r["q"]
        R.add(E["emb"][k], F["feats"][k], F["voice"][k], r["speaker"], "base-20261030", "20261030", f"base:q{q}",
              (U[k]["e"] - U[k]["s"]) / 1000, "train", None, bre=F["bre"][k], text=r.get("text", ""))
        n += 1
    log("base rows", n)


def load_sets(R, root, host, skip=()):
    metas = []
    for d in sorted((root / "sets").glob("*")):
        if not (d / "meta.json").exists() or not (d / "emb_units.npz").exists() or d.name.startswith(tuple(skip)):
            continue
        meta = json.load(open(d / "meta.json"))
        U = json.load(open(d / "units.json"))["units"]
        lab = {r["id"]: r for r in json.load(open(d / "labels.json"))["rows"]}
        E, F = np.load(d / "emb_units.npz"), np.load(d / "feats.npz")
        ids = [str(i) for i in E["ids"]]
        pred = {p["id"]: p for p in json.load(open(d / "pred.json"))["predictions"]}
        k_of = {i: k for k, i in enumerate(ids)}
        n = 0
        for u in U:
            k = k_of[u["id"]]
            if not E["ok"][k]:
                continue
            r = lab[u["id"]]
            R.add(E["emb"][k], F["feats"][k], F["voice"][k], r["speaker"], meta["set"], meta["session"], f"set:{meta['set']}",
                  r["dur_s"], meta["use"], pred.get(u["id"], {}).get("probs"), bre=F["bre"][k], text=r.get("text", ""))
            n += 1
        meta["n_rows"] = n
        metas.append(meta)
        log("set", meta["set"], meta["session"], meta["use"], n)
    return metas


# ------------------------------------------------------------------------------------------------ model (v8 recipe)
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupKFold


class TextPrep:
    """Char 1-3 grams of the line's text (tf-idf, fitted on the training lines only)."""

    def __init__(self, R, idx):
        from sklearn.feature_extraction.text import TfidfVectorizer
        self.vec = TfidfVectorizer(analyzer="char", ngram_range=(1, 3), min_df=2, sublinear_tf=True)
        self.vec.fit(list(R.text[idx]))

    def x(self, R, idx):
        return self.vec.transform(list(R.text[idx]))


class Prep:
    def __init__(self, R, blocks, idx):
        self.blocks, self.stat = blocks, {}
        for b in blocks:
            A = R.block(b)[idx]
            self.stat[b] = (np.nanmean(A, 0), np.nanstd(A, 0) + 1e-6, 1 / math.sqrt(A.shape[1]))

    def x(self, R, idx):
        parts = []
        for b in self.blocks:
            mu, sd, w = self.stat[b]
            Z = (R.block(b)[idx] - mu) / sd
            parts.append(np.where(np.isnan(Z), 0, Z) * w)
        return np.hstack(parts)


def pad(p, classes, model_classes):
    out = np.full((len(p), len(classes)), 1e-6)
    for j, c in enumerate(model_classes):
        out[:, int(c)] = p[:, j]
    return out / out.sum(1, keepdims=True)


def fit_stack(R, tr, classes):
    """tr: row indices to train on; returns predict(idx) -> (n, len(classes)) probabilities and the export dict."""
    tr = np.asarray(tr)
    y = np.array([classes.index(s) for s in R.spk[tr]])
    g = R.group[tr]
    K = len(classes)

    def base(rows, bl, C):
        pre = TextPrep(R, rows) if bl == ("text",) else Prep(R, list(bl), rows)
        yy = np.array([classes.index(s) for s in R.spk[rows]])
        m = LogisticRegression(C=C, max_iter=600).fit(pre.x(R, rows), yy)
        return pre, m

    Z = np.zeros((len(tr), K * len(SPECS)))
    for a_, b_ in GroupKFold(min(8, len(set(g)))).split(Z, groups=g):
        for j, (bl, C) in enumerate(SPECS):
            pre, m = base(tr[a_], bl, C)
            Z[b_, K * j:K * j + K] = np.log(pad(m.predict_proba(pre.x(R, tr[b_])), classes, m.classes_))
    stk = LogisticRegression(C=1.0, max_iter=800).fit(Z, y)
    fs = [base(tr, bl, C) for bl, C in SPECS]

    def predict(idx):
        zs = [np.log(pad(m.predict_proba(pre.x(R, idx)), classes, m.classes_)) for pre, m in fs]
        return pad(stk.predict_proba(np.hstack(zs)), classes, stk.classes_)

    def export():
        if any(bl == ("text",) for bl, _ in SPECS):
            return None                      # the text base is not exported to production yet (evaluation only)
        bases = []
        for (bl, C), (pre, m) in zip(SPECS, fs):
            full = np.zeros((K, m.coef_.shape[1]))
            icp = np.full(K, -30.0)
            for j, c in enumerate(m.classes_):
                full[int(c)] = m.coef_[j]
                icp[int(c)] = m.intercept_[j]
            bases.append(dict(blocks=list(bl), C=C, coef=full.tolist(), intercept=icp.tolist(),
                              standardize={b: dict(mean=np.nan_to_num(pre.stat[b][0]).tolist(), sd=pre.stat[b][1].tolist(),
                                                   block_scale=float(pre.stat[b][2])) for b in bl}))
        assert list(stk.classes_) == list(range(K)), "every class needs training rows"
        return dict(bases=bases, stacker=dict(C=1.0, coef=stk.coef_.tolist(), intercept=stk.intercept_.tolist()))
    return predict, export


def calibrate(P, y):
    best = (1e9, 1.0)
    for T in np.arange(0.3, 3.01, 0.05):
        Q = softmax(np.log(np.clip(P, 1e-9, 1)) * T)
        ll = -np.mean(np.log(np.clip(Q[np.arange(len(y)), y], 1e-9, 1)))
        best = min(best, (ll, float(T)))
    return round(best[1], 2)


def host_guest_scores(p_host, top_is_host, truth_host, thresholds=(None, 0.5, 0.6, 0.7, 0.8, 0.9)):
    """Accuracy of host-vs-anyone-else decisions: argmax (None) and host iff top is host and p >= threshold."""
    out = {}
    for t in thresholds:
        pred = top_is_host if t is None else (top_is_host & (p_host >= t))
        tag = "argmax" if t is None else f">={t}"
        out[tag] = dict(acc=round(float((pred == truth_host).mean()), 4), correct=int((pred == truth_host).sum()),
                        host_recall=round(float(pred[truth_host].mean()), 4) if truth_host.any() else None,
                        guest_recall=round(float((~pred[~truth_host]).mean()), 4) if (~truth_host).any() else None)
    return out


def frozen_probs(R, idx, clf, host):
    """The classifier production uses now, on rows idx (set rows carry pred.json from add-set; base rows are not
    re-scored: v8 was trained on all of them)."""
    ph, top = [], []
    for k in idx:
        p = R.v8[k]
        ph.append(float(p.get(host, 0.0)))
        top.append(max(p, key=p.get) == host)
    return np.array(ph), np.array(top)


def retrain(a):
    root, base, out = Path(a.root), Path(a.base), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    host = a.host
    R = Rows()
    load_base(R, base, host)
    metas = load_sets(R, root, host, [s for s in a.skip.split(",") if s])
    R.done()
    named = [s for s in sorted(set(R.spk[R.use == "train"])) if s != GUEST_ANY]
    counts = {s: int(((R.spk == s) & (R.use == "train")).sum()) for s in named}
    classes = [host] + [s for s in named if s != host and counts[s] >= a.min_rows]
    trainable = (R.use == "train") & np.isin(R.spk, classes)
    log("classes", classes, "train rows", int(trainable.sum()), counts)
    truth_host = R.spk == host
    report = dict(classes=classes, train_counts=counts, sets=metas, host=host, method="v8 recipe: stack emb(0.3)/mic(0.1)/voice(0.1) LR -> LR(C=1), GroupKFold OOF",
                  per_set={}, base_cv={})
    # ---- each set: frozen v8 vs a model fitted without the set's whole session, and without only the set
    for meta in metas:
        s = meta["set"]
        idx = np.where(R.set == s)[0]
        th = truth_host[idx]
        ph0, top0 = frozen_probs(R, idx, None, host)
        res = dict(n=len(idx), session=meta["session"], use=meta["use"], speakers=meta["speakers"],
                   frozen_v8=host_guest_scores(ph0, top0, th))
        for tag, keep in (("without_session", R.session != meta["session"]), ("without_set", R.set != s)):
            tr = np.where(trainable & keep)[0]
            if not len(tr):
                continue
            pred, _ = fit_stack(R, tr, classes)
            P = pred(idx)
            top = P.argmax(1) == 0
            res[tag] = host_guest_scores(P[:, 0], top, th)
            named_rows = np.isin(R.spk[idx], classes)
            if named_rows.any():
                y = np.array([classes.index(x) for x in R.spk[idx][named_rows]])
                res[tag]["named_acc"] = round(float((P[named_rows].argmax(1) == y).mean()), 4)
        report["per_set"][s] = res
        log("set", s, json.dumps({k: v.get("argmax") if isinstance(v, dict) else v for k, v in res.items()
                                   if k in ("frozen_v8", "without_session", "without_set")}, ensure_ascii=False))
    # ---- the base session: 8-fold question-grouped CV, base only (= v8 recipe) vs base + every training set
    bidx = np.where(R.set == "base-20261030")[0]
    folds = list(GroupKFold(8).split(bidx, groups=R.group[bidx]))
    oof = {k: np.zeros((len(bidx), len(classes))) for k in ("base_only", "with_sets")}
    for a_, b_ in folds:
        hold = set(R.group[bidx[b_]])
        for k in oof:
            keep = trainable & ~np.isin(R.group, list(hold)) & ((R.set == "base-20261030") if k == "base_only" else True)
            pred, _ = fit_stack(R, np.where(keep)[0], classes)
            oof[k][b_] = pred(bidx[b_])
    th = truth_host[bidx]
    for k, P in oof.items():
        y = np.array([classes.index(x) for x in R.spk[bidx]])
        report["base_cv"][k] = dict(named_acc=round(float((P.argmax(1) == y).mean()), 4), host_guest=host_guest_scores(P[:, 0], P.argmax(1) == 0, th))
    log("base cv", json.dumps({k: (v["named_acc"], v["host_guest"]["argmax"]["acc"]) for k, v in report["base_cv"].items()}))
    # ---- the candidate: every training row; temperature from leave-group-out probabilities
    tr = np.where(trainable)[0]
    oofP = np.zeros((len(tr), len(classes)))
    for a_, b_ in GroupKFold(10).split(tr, groups=R.group[tr]):
        pred, _ = fit_stack(R, tr[a_], classes)
        oofP[b_] = pred(tr[b_])
    T = calibrate(oofP, np.array([classes.index(x) for x in R.spk[tr]]))
    pred, export = fit_stack(R, tr, classes)
    report["candidate"] = dict(version=a.version, temperature=T, n_train=int(len(tr)), blocks=[bl[0] for bl, _ in SPECS])
    exported = export()
    if exported is None:
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        log("evaluation only (a base is not exportable yet); report", out / "report.json")
        return
    clf = dict(type="late-fusion stacking: per-block multinomial LR -> second-level multinomial LR", version=a.version,
               class_order=classes, n_train=int(len(tr)), temperature=T, **exported,
               trained_on=dict(base="20261030 trio (frozen v8 truth)", sets=[m["set"] for m in metas if m["use"] == "train"]),
               usage="per block x=(raw-mean)/sd*block_scale (NaN->0), p=softmax(coef@x+intercept); stacker input = log p of each base in order; softmax(stacker); then softmax(log p * temperature)")
    vd = out / a.version
    (vd / "code").mkdir(parents=True, exist_ok=True)
    (vd / "in").mkdir(exist_ok=True)
    (vd / "out" / "final").mkdir(parents=True, exist_ok=True)
    for f in ("oci3_embed.py", "oci3_feats.py"):
        txt = (base / "code" / f).read_text(encoding="utf-8").replace(str(base), str(vd))
        (vd / "code" / f).write_text(txt, encoding="utf-8")
    (vd / "in" / "truth_rows.json").write_text("[]", encoding="utf-8")
    (vd / "out" / "final" / "classifier.json").write_text(json.dumps(clf, ensure_ascii=False), encoding="utf-8")
    shutil.copy(Path(__file__).with_name("speaker_score.py"), vd / "score.py")
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    log("wrote", vd, "report", out / "report.json")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("add-set")
    s.add_argument("--root", required=True)
    s.add_argument("--set", required=True)
    s.add_argument("--session", required=True)
    s.add_argument("--use", choices=("train", "eval"), default="train")
    s.add_argument("--media", required=True)
    s.add_argument("--labels", required=True)
    s.add_argument("--offset-ms", type=int, default=0, help="media time = label time + offset")
    s.add_argument("--key-prefix", default="")
    s.add_argument("--srt", default="", help="speaker_label_page.py export: the clip's final.srt (cue times)")
    s.add_argument("--clip", default="")
    s.add_argument("--note", default="")
    s.add_argument("--replace", action="store_true")
    for k in ("v8-code", "v8-python", "v8-model", "v8-score", "run-script"):
        s.add_argument("--" + k, required=True)
    r = sub.add_parser("retrain")
    r.add_argument("--root", required=True)
    r.add_argument("--base", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--version", required=True)
    r.add_argument("--host", required=True, help="the channel host's name as written in the labels")
    r.add_argument("--min-rows", type=int, default=30)
    r.add_argument("--skip", default="", help="comma-separated set-name prefixes to leave out (ablation)")
    r.add_argument("--blocks", default="emb,mic,voice", help="base models of the stack: emb,mic,voice[,bre][,text]")
    a = ap.parse_args()
    if a.cmd == "add-set":
        add_set(a)
    else:
        SPECS[:] = [((b,), BLOCK_C[b]) for b in a.blocks.split(",")]
        retrain(a)


if __name__ == "__main__":
    main()
