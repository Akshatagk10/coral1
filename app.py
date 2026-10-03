from collections import Counter
import json, logging, os, time, threading, numpy as np, pandas as pd
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, jsonify, render_template
import crw, model as M, scoring
from green import InferenceMeter
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = Flask(__name__, template_folder="."); REFRESH_S = int(os.environ.get("REFRESH_S", 3600)); lock = threading.Lock()
S = dict(sites=[], ts=0, model=None, meter=None, report=None)

def _pool(days): 
    with ThreadPoolExecutor(6) as ex: return list(ex.map(lambda s: crw.get_site_data(s, days), crw.SITE_BY_ID.values()))

def ensure_model():
    p, rp = os.path.join(M.ART, "model.npz"), os.path.join(M.ART, "footprint.json")
    rep = json.load(open(rp)) if os.path.exists(p) and os.path.exists(rp) else None
    if rep and rep.get("training_data", {}).get("simulated", 1) == 0:  # reuse only models trained on real (live/snapshot) data
        h = next(v["hidden"] for v in rep["variants"] if v["name"] == rep["served"]); S["model"], S["report"] = M.MLP.load(p, h), rep
    else:
        if rep: logging.getLogger("reefwatch.app").warning("saved model was trained on simulated/unknown data: retraining")
        res = _pool(crw.TRAIN_DAYS); c = Counter(m["source"] for _, m in res); src = {k: c.get(k, 0) for k in ("live", "snapshot", "simulated")}
        logging.getLogger("reefwatch.app").info("training data sources: %s", src)
        S["model"], S["report"] = M.train_sweep({s: r[0] for s, r in zip(crw.SITE_BY_ID, res)}, sources=src)
    S["meter"] = InferenceMeter(S["model"].flops)

def compute():
    res = _pool(60); rows, X = [], []
    for site, (df, meta) in zip(crw.SITE_BY_ID.values(), res):
        f = M.features(df, label=False)
        if f.empty: continue
        last = f.iloc[-1]; X.append(last[M.FEATS].values.astype(float)); rows.append(dict(site, **meta, as_of=str(last.date.date()), obs={k: round(float(last[k]), 2) for k in ["sst", "sst_anom", "hotspot", "dhw", "baa", "trend7"]}))
    t = time.perf_counter(); p = S["model"].predict(np.array(X)); S["meter"].record(len(X), time.perf_counter() - t)  # one batched pass
    for r, risk in zip(rows, p):
        risk = float(risk); r.update(risk=round(risk, 3), tier=scoring.tier(risk), priority=round(scoring.priority(risk, r["value"]), 3), actions=scoring.ACTIONS[scoring.tier(risk)])
    rows.sort(key=lambda r: -r["priority"]); [r.update(rank=i + 1) for i, r in enumerate(rows)]
    with lock: S.update(sites=rows, ts=time.time())
    return rows

def payload():
    return dict(sites=S["sites"], updated=S["ts"], report=S["report"], inference=S["meter"].report(), refresh_s=REFRESH_S)

@app.get("/")
def index(): return render_template("index.html")
@app.get("/api/state")
def state():
    if time.time() - S["ts"] > REFRESH_S: compute()  # cached; recompute only after interval
    return jsonify(payload())
@app.post("/api/refresh")
def refresh():
    crw.reset_health(); rows = compute(); of = lambda k: [r for r in rows if r["source"] == k]; bad = [r for r in rows if r["source"] != "live"]
    return jsonify(ok=not bad, live=len(of("live")), snapshot=len(of("snapshot")), simulated=len(of("simulated")),
                   errors=[{"site": r["name"], "error": r["error"]} for r in bad], **payload())

if __name__ == "__main__":
    ensure_model(); compute(); app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)))
