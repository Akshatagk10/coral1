import json, os, numpy as np, pandas as pd
from sklearn.metrics import roc_auc_score
from splits import grouped_split
from green import EnergyTracker
ART = os.path.dirname(os.path.abspath(__file__))  # flat layout: model.npz / footprint.json live beside the code
FEATS = ["dhw", "sst_anom", "hotspot", "baa", "dhw_l7", "dhw_l14", "trend7", "anom7", "hs_max7"]
HORIZON, THRESH = 28, 8.0  # proxy label: DHW>=8 (NOAA Alert Level 2, significant mortality likely) within next 28d

def features(df, label=True):
    d = df.sort_values("date").copy()
    d["dhw_l7"], d["dhw_l14"] = d.dhw.shift(7), d.dhw.shift(14); d["trend7"] = d.dhw - d.dhw_l7
    d["anom7"], d["hs_max7"] = d.sst_anom.rolling(7).mean(), d.hotspot.rolling(7).max()
    if label: d["y"] = (d.dhw[::-1].rolling(HORIZON).max()[::-1].shift(-1) >= THRESH).astype(float).where(d.dhw.shift(-HORIZON).notna())
    return d.dropna(subset=FEATS + (["y"] if label else []))

class MLP:
    def __init__(self, hidden, nin=len(FEATS), seed=0):
        r = np.random.default_rng(seed); s = [nin, *hidden, 1]
        self.W = [r.normal(0, np.sqrt(2 / a), (a, b)) for a, b in zip(s, s[1:])]; self.b = [np.zeros(b) for b in s[1:]]
        self.mu, self.sd = np.zeros(nin), np.ones(nin)
    @property
    def params(self): return sum(w.size + b.size for w, b in zip(self.W, self.b))
    @property
    def flops(self): return int(sum(2 * w.size for w in self.W))  # analytic: 2*in*out per dense layer (MACs x2), activations ignored
    def _fwd(self, X):
        a = [X]
        for i, (w, b) in enumerate(zip(self.W, self.b)):
            z = a[-1] @ w + b; a.append(1 / (1 + np.exp(-np.clip(z, -30, 30))) if i == len(self.W) - 1 else np.maximum(z, 0))
        return a
    def predict(self, X): return self._fwd((np.asarray(X) - self.mu) / self.sd)[-1].ravel()  # whole batch, one pass
    def fit(self, X, y, Xv, yv, budget=60, patience=8, lr=3e-3, bs=256, seed=0):
        self.mu, self.sd = X.mean(0), X.std(0) + 1e-6; Xn = (X - self.mu) / self.sd; r = np.random.default_rng(seed)
        m = [np.zeros_like(p) for p in self.W + self.b]; v = [np.zeros_like(p) for p in self.W + self.b]; t = 0; best, bad, snap = 1e9, 0, None; ep = 0
        for ep in range(1, budget + 1):
            idx = r.permutation(len(Xn))
            for s in range(0, len(idx), bs):
                j = idx[s:s + bs]; a = self._fwd(Xn[j]); d = (a[-1] - y[j][:, None]) / len(j); gW, gb = [], []
                for k in range(len(self.W) - 1, -1, -1):
                    gW.insert(0, a[k].T @ d); gb.insert(0, d.sum(0))
                    if k: d = (d @ self.W[k].T) * (a[k] > 0)
                t += 1
                for i, (p, g) in enumerate(zip(self.W + self.b, gW + gb)):
                    m[i] = .9 * m[i] + .1 * g; v[i] = .999 * v[i] + .001 * g * g
                    p -= lr * (m[i] / (1 - .9 ** t)) / (np.sqrt(v[i] / (1 - .999 ** t)) + 1e-8)
            pv = np.clip(self.predict(Xv), 1e-6, 1 - 1e-6); loss = -np.mean(yv * np.log(pv) + (1 - yv) * np.log(1 - pv))
            if loss < best - 1e-4: best, bad, snap = loss, 0, ([w.copy() for w in self.W], [b.copy() for b in self.b])
            else:
                bad += 1
                if bad >= patience: break
        if snap: self.W, self.b = snap
        return ep
    def save(self, path): np.savez(path, mu=self.mu, sd=self.sd, **{f"W{i}": w for i, w in enumerate(self.W)}, **{f"b{i}": b for i, b in enumerate(self.b)})
    @classmethod
    def load(cls, path, hidden):
        m = cls(hidden); z = np.load(path); m.W = [z[f"W{i}"] for i in range(len(m.W))]; m.b = [z[f"b{i}"] for i in range(len(m.b))]; m.mu, m.sd = z["mu"], z["sd"]; return m

VARIANTS = {"tiny (8)": (8,), "small (16,8)": (16, 8), "medium (64,32)": (64, 32)}
def train_sweep(frames, budget=60, tol=0.01, sources=None):
    """frames: {site_id: df}. Identical grouped split for every variant; serve the cheapest within `tol` AUC of best."""
    D = pd.concat([features(df).assign(site=s) for s, df in frames.items()]); D = D.reset_index(drop=True)
    tr, te = grouped_split(D.site.values, 0.3, seed=1); sub = D.iloc[tr].reset_index(drop=True)
    itr, iva = grouped_split(sub.site.values, 0.25, seed=2)  # validation also grouped
    X, y = sub[FEATS].values, sub.y.values; Xt, yt = D.iloc[te][FEATS].values, D.iloc[te].y.values
    out, models = [], {}
    for name, h in VARIANTS.items():
        m = MLP(h)
        with EnergyTracker() as tk: ep = m.fit(X[itr], y[itr], X[iva], y[iva], budget=budget)
        p = m.predict(Xt); auc = float(roc_auc_score(yt, p)) if len(set(yt)) > 1 else None
        out.append(dict(name=name, hidden=list(h), params=m.params, flops_per_sample=m.flops, epochs_run=ep, epochs_budget=budget,
                        test_acc=float(((p > .5) == yt).mean()), test_auc=auc, **tk.report)); models[name] = m
    ok = [o for o in out if o["test_auc"] is not None]; top = max((o["test_auc"] for o in ok), default=0)
    served = min([o for o in ok if o["test_auc"] >= top - tol] or out, key=lambda o: o["flops_per_sample"])["name"]
    rep = dict(training_data=sources or {}, variants=out, served=served, label=f"DHW>={THRESH} within {HORIZON}d (proxy for mortality-level stress; NOT observed mortality)",
               split=dict(train_sites=sorted(D.iloc[tr].site.unique().tolist()), test_sites=sorted(D.iloc[te].site.unique().tolist()), n_train=len(tr), n_test=len(te)))
    os.makedirs(ART, exist_ok=True); models[served].save(os.path.join(ART, "model.npz")); json.dump(rep, open(os.path.join(ART, "footprint.json"), "w"), indent=1)
    return models[served], rep
