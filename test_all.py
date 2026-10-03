import sys, os, types, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from splits import grouped_split
import scoring, green

def test_grouped_split_no_leak():
    g = np.repeat([f"s{i}" for i in range(10)], 50)
    for seed in range(5):
        tr, te = grouped_split(g, .3, seed)
        assert not set(g[tr]) & set(g[te]) and len(tr) + len(te) == len(g) and len(te) > 0

def test_priority_not_risk_only():
    assert scoring.priority(.95, .1) < scoring.priority(.5, .9)
    assert scoring.priority(.6, .8) > scoring.priority(.3, .8)

def test_tracker_never_raises_without_codecarbon(monkeypatch):
    monkeypatch.setitem(sys.modules, "codecarbon", None)
    with green.EnergyTracker() as t: pass
    assert set(t.report) >= {"duration_s", "energy_kwh", "co2_kg", "tracker"} and t.report["tracker"] == "duration-only" and t.report["duration_s"] is not None

def test_tracker_survives_broken_tracker(monkeypatch):
    class Bad:
        def __init__(s, **k): raise RuntimeError("boom")
    m = types.ModuleType("codecarbon"); m.EmissionsTracker = Bad; monkeypatch.setitem(sys.modules, "codecarbon", m)
    with green.EnergyTracker() as t: pass
    assert "energy_kwh" in t.report

def test_inference_meter_keys():
    m = green.InferenceMeter(100); m.record(12, .01); r = m.report()
    assert r["samples"] == 12 and r["total_flops"] == 1200

def test_raw_oserror_degrades_to_simulated(monkeypatch):
    monkeypatch.setattr("crw.time.sleep", lambda s: None); monkeypatch.setattr("crw.SNAP_CSV", "/nonexistent/s.csv")
    import crw
    def boom(*a, **k): raise TimeoutError("raw socket")
    monkeypatch.setattr(crw.requests, "get", boom)
    df, meta = crw.get_site_data(crw.SITE_BY_ID["flk"], 30)
    assert meta["source"] == "simulated" and "ConnectionLevelError" in meta["error"] and len(df) > 0
