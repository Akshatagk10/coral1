"""NOAA CRW 5km heat-stress fetch with failover. Raw ERDDAP names are used on the wire; renamed AFTER fetch."""
import io, json, logging, math, os, re, socket, time, zlib, datetime as dt
import numpy as np, pandas as pd, requests
log = logging.getLogger("reefwatch.crw")
TIMEOUT = (10, 60)  # (connect, read)
HDR = {"User-Agent": "reefwatch/1.0 (coral heat-stress dashboard)"}
_V4 = [os.environ.get("CRW_FORCE_IPV4") == "1"]  # flips to True automatically after a connect failure (broken IPv6 routes)
try:
    import urllib3.util.connection as _uc
    _orig_gai = _uc.allowed_gai_family
    _uc.allowed_gai_family = lambda: socket.AF_INET if _V4[0] else _orig_gai()
except Exception: pass
SAFETY_DAYS = int(os.environ.get("CRW_SAFETY_DAYS", 3))  # product lags ~60h; check dataset testOutOfDate
RAW_VARS = ["CRW_SST", "CRW_SSTANOMALY", "CRW_HOTSPOT", "CRW_DHW", "CRW_BAA"]  # copied from NOAA_DHW .das
RENAME = dict(zip(RAW_VARS, ["sst", "sst_anom", "hotspot", "dhw", "baa"]))
# Hosts are config. Verified against live ERDDAP metadata (Oct 2026):
#  1. NOAA SWFSC/ERD CoastWatch West Coast Node: NOAA_DHW, vars CRW_*, testOutOfDate=now-60hours, lon -180..180.
#  2. PacIOOS (Univ. of Hawaii SOEST, separate org + facility): dhw_5km, same schema; it is the upstream the NOAA copy
#     was redistributed from, so it is an independent serving backend, not a hostname alias. It may trail NOAA by ~1 day.
#  3. NOAA PIFSC OceanWatch: DISABLED. Separate datasets, DHW var is `degree_heating_week`, lon 0..360, and it did not
#     look current; needs anomaly/hotspot/BAA dataset ids confirmed before enabling (code path: mode="separate").
HOSTS = [
    {"name": "NOAA SWFSC/ERD (coastwatch.pfeg)", "base": "https://coastwatch.pfeg.noaa.gov/erddap", "dataset": "NOAA_DHW", "mode": "combined"},
    {"name": "PacIOOS (Univ. Hawaii)", "base": "https://pae-paha.pacioos.hawaii.edu/erddap", "dataset": "dhw_5km", "mode": "combined"},
    {"name": "NOAA PIFSC OceanWatch", "base": "https://oceanwatch.pifsc.noaa.gov/erddap", "mode": "separate", "lon360": True, "enabled": False,
     "datasets": {"dhw": "CRW_dhw_v1_0", "sst": "CRW_sst_v1_0"}},  # + sst_anom, hotspot, baa ids needed
]
MAX_STALE_DAYS = 10
COOLDOWN_S = int(os.environ.get("CRW_COOLDOWN_S", 120))  # circuit breaker: skip a host that just failed to connect
HERE = os.path.dirname(os.path.abspath(__file__))
SNAP_CSV, SNAP_META = os.path.join(HERE, "snapshot.csv"), os.path.join(HERE, "snapshot_meta.json")
TRAIN_DAYS = int(os.environ.get("TRAIN_DAYS", 1100))  # history used for training / stored in the snapshot
_SNAP = {"mtime": None, "df": None, "meta": {}}
_DOWN, _PREFERRED = {}, [None]
def reset_health(): _DOWN.clear()  # manual Refresh retries every host
def _is_down(h): return _DOWN.get(h["name"], 0) > time.time()
SITES = [  # id, name, lat, lon, value (0-1 ecological/economic), synthetic stress
    ("gbr-heron", "Heron Island, GBR", -23.44, 151.91, 0.95, 0.5), ("gbr-cairns", "Cairns Reefs, GBR", -16.75, 146.0, 0.9, 0.7),
    ("flk", "Florida Keys", 24.55, -81.8, 0.7, 0.8), ("belize", "Belize Barrier Reef", 17.3, -87.8, 0.85, 0.6),
    ("raja", "Raja Ampat", -0.55, 130.5, 0.95, 0.2), ("maldives", "Maldives (Ari Atoll)", 3.9, 72.8, 0.75, 0.9),
    ("hawaii", "Kāneʻohe Bay, Hawaii", 21.45, -157.8, 0.6, 0.4), ("redsea", "Red Sea (Eilat)", 29.5, 34.9, 0.65, 0.1),
    ("lakshadweep", "Lakshadweep", 10.57, 72.64, 0.55, 0.85), ("fiji", "Fiji (Great Astrolabe)", -18.8, 178.5, 0.7, 0.5),
    ("tubbataha", "Tubbataha, Philippines", 8.85, 119.9, 0.8, 0.45), ("saba", "Saba, Caribbean", 17.63, -63.25, 0.3, 0.3)]
SITE_BY_ID = {s[0]: dict(id=s[0], name=s[1], lat=s[2], lon=s[3], value=s[4], stress=s[5]) for s in SITES}

class HostSkip(Exception): """This host can't serve the request (down / stale / wrong schema / dataset unloaded): try the next host."""
class ConnectionLevelError(HostSkip): """Never reached the server, or 429/5xx."""
class QueryLevelError(Exception): """Server understood and rejected the request: identical retry fails identically. Fail fast."""

def window(days, safety=None):
    end = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=SAFETY_DAYS if safety is None else safety)
    return end - dt.timedelta(days=days), end

def snap(x):
    """Snap to the 0.05-degree grid-cell CENTER (x.025 / x.075), as used by the CRW 5km grid."""
    return round((math.floor(x / 0.05) + 0.5) * 0.05, 3)

def _get(url, attempts=2):
    last, i = None, 0
    while i < attempts:
        try:
            r = requests.get(url, timeout=TIMEOUT, headers=HDR)
        except (requests.exceptions.RequestException, OSError) as e:  # OSError: bare socket TimeoutError/ConnectionError
            last = ConnectionLevelError(f"{type(e).__name__}: {e}")
            if not _V4[0] and not isinstance(e, requests.exceptions.ReadTimeout):
                _V4[0] = True; log.warning("connect failed (%s); retrying once IPv4-only (broken IPv6 routes are a common cause)", type(e).__name__); continue  # free retry
        else:
            t = r.text or ""
            if r.status_code == 200: return t
            if r.status_code >= 500 or r.status_code == 429: last = ConnectionLevelError(f"HTTP {r.status_code}")
            elif r.status_code == 404 and ("unknown datasetid" in t.lower() or "no matching results" in t.lower()):
                raise HostSkip(f"HTTP 404 (dataset unloaded or host behind/ahead of requested range): {t[:200].strip()}")
            else: raise QueryLevelError(f"HTTP {r.status_code}: {t[:300].strip()}")  # ERDDAP's own diagnostic text
        i += 1
        if i < attempts: time.sleep(1.5 * i)
    raise last

_PRE = {}
def preflight(host):
    """Fetch the dataset's real .das and confirm every required variable exists. Returns '' if OK, else the problem text."""
    key = host["base"] + host.get("dataset", "")
    if key in _PRE and time.time() - _PRE[key][0] < 3600: return _PRE[key][1]
    try: das = _get(f"{host['base']}/griddap/{host['dataset']}.das", attempts=1)
    except QueryLevelError as e: return f"preflight .das rejected: {e}"
    missing = [v for v in RAW_VARS if not re.search(rf"^\s*{v}\s*\{{", das, re.M)]
    msg = f"{host['name']}: dataset {host['dataset']} lacks variables {missing}" if missing else ""
    m = re.search(r'testOutOfDate"?\s+"?([^";\n]+)', das)
    (log.error if msg else log.info)("preflight %s: %s testOutOfDate=%s (SAFETY_DAYS=%s)", host["name"], msg or "variables OK", m.group(1) if m else "?", SAFETY_DAYS)
    _PRE[key] = (time.time(), msg); return msg

def _csv(text):
    df = pd.read_csv(io.StringIO(text), skiprows=[1])  # row 2 is the units row
    return df

def _point_combined(host, lat, lon, a):
    q = ",".join(f"{v}[({a}T12:00:00Z):1:(last)][({lat})][({lon})]" for v in RAW_VARS)  # '(last)': never request an unpublished date
    df = _csv(_get(f"{host['base']}/griddap/{host['dataset']}.csv?{q}"))
    return df.rename(columns=RENAME)  # rename AFTER fetch

def _discover(host, ds):
    das = _get(f"{host['base']}/griddap/{ds}.das", attempts=1)
    c = [n for n in re.findall(r"^\s{4}(\w+) \{", das, re.M) if n not in ("time", "latitude", "longitude", "crs") and "mask" not in n.lower() and "quality" not in n.lower()]
    if not c: raise HostSkip(f"{ds}: no data variable found in .das")
    return c[0]

def _point_separate(host, lat, lon, a):
    need = ["sst", "sst_anom", "hotspot", "dhw", "baa"]
    if any(k not in host["datasets"] for k in need): raise HostSkip(f"{host['name']}: separate-dataset map incomplete (have {sorted(host['datasets'])})")
    out = None
    for k in need:
        ds = host["datasets"][k]; var = _discover(host, ds)  # variable names differ per dataset: read them from the .das
        p = _csv(_get(f"{host['base']}/griddap/{ds}.csv?{var}[({a}T12:00:00Z):1:(last)][({lat})][({lon % 360 if host.get('lon360') else lon})]"))
        p = p[["time", var]].rename(columns={var: k}); out = p if out is None else out.merge(p, on="time", how="outer")
    return out

OFFSETS = [(0, 0), (.05, 0), (-.05, 0), (0, .05), (0, -.05), (.05, .05), (.05, -.05), (-.05, .05), (-.05, -.05)]
def fetch_host(host, site, days):
    pre = preflight(host) if host["mode"] == "combined" else ""
    if pre: raise HostSkip(pre)
    a, _ = window(days); fn = _point_combined if host["mode"] == "combined" else _point_separate
    for dlat, dlon in OFFSETS:  # land/cloud pixels are NaN: fall back to nearest valid water cell
        lat, lon = round(snap(site["lat"]) + dlat, 3), round(snap(site["lon"]) + dlon, 3)
        if abs(lat) >= 90: continue
        lon = ((lon + 180) % 360) - 180
        df = fn(host, lat, lon, a)
        df = df.rename(columns={"time": "date"}); df["date"] = pd.to_datetime(df["date"], utc=True).dt.tz_localize(None).dt.normalize()
        for c in ["sst", "sst_anom", "hotspot", "dhw", "baa"]: df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["sst", "dhw"]).sort_values("date")
        if len(df) < 20: continue
        df[["sst_anom", "hotspot", "baa"]] = df[["sst_anom", "hotspot", "baa"]].ffill().fillna(0)
        lag = (dt.datetime.now(dt.timezone.utc).date() - df.date.iloc[-1].date()).days
        if lag > MAX_STALE_DAYS: raise HostSkip(f"{host['name']} stale: latest {df.date.iloc[-1].date()} ({lag}d old)")
        return df[["date", "sst", "sst_anom", "hotspot", "dhw", "baa"]].reset_index(drop=True), (lat, lon)
    raise QueryLevelError("no valid (non-NaN) water pixel within 1 cell of site")

def synthetic(site, days):
    a, b = window(days)
    d = pd.date_range(a, b); n = len(d); rng = np.random.default_rng(zlib.crc32(site["id"].encode()))  # deterministic across runs
    ph = 2 * np.pi * (d.dayofyear.values - (45 if site["lat"] < 0 else 225)) / 365.25
    amp = 2.5 * min(1, abs(site["lat"]) / 15 + .3); ar = np.zeros(n)
    for i in range(1, n): ar[i] = .97 * ar[i-1] + rng.normal(0, .12)
    sst = 27 + amp * np.cos(ph) + ar + site["stress"] * 1.4 * np.clip(np.cos(ph), 0, None) ** 2 + site["stress"] * .8
    mmm = 27 + amp + .2; hs = np.clip(sst - mmm, 0, None)
    df = pd.DataFrame({"date": d, "sst": sst, "sst_anom": sst - (mmm - 1.0), "hotspot": hs})
    df["dhw"] = df.hotspot.where(df.hotspot >= 1, 0).rolling(84, min_periods=1).sum() / 7
    df["baa"] = np.select([df.dhw >= 8, df.dhw >= 4, df.dhw > 0, df.hotspot > 0], [4, 3, 2, 1], 0)
    return df

def load_snapshot():
    """-> (df|None, meta). Never raises: a missing/corrupt snapshot just means 'no snapshot'."""
    try:
        m = os.path.getmtime(SNAP_CSV)
        if _SNAP["mtime"] != m:
            df = pd.read_csv(SNAP_CSV, parse_dates=["date"]); need = {"site", "date", "sst", "sst_anom", "hotspot", "dhw", "baa"}
            if not need <= set(df.columns): raise ValueError(f"missing columns {sorted(need - set(df.columns))}")
            meta = json.load(open(SNAP_META)) if os.path.exists(SNAP_META) else {}
            _SNAP.update(mtime=m, df=df, meta=meta)
        return _SNAP["df"], _SNAP["meta"]
    except FileNotFoundError: return None, {}
    except Exception as e: log.error("snapshot unreadable (%r); ignoring it", e); return None, {}

def save_snapshot(items):
    """items: {site_id: (df, meta)}. Merges with an existing snapshot (sites not in items are kept)."""
    old, meta = load_snapshot(); meta = dict(meta); parts = [] if old is None else [old[~old.site.isin(list(items))]]
    for sid, (df, m) in items.items():
        parts.append(df.assign(site=sid)); meta[sid] = {k: m.get(k) for k in ("host", "pixel", "fetched_at", "latest")}
    out = pd.concat(parts).sort_values(["site", "date"])[["site", "date", "sst", "sst_anom", "hotspot", "dhw", "baa"]]
    out.to_csv(SNAP_CSV, index=False); json.dump(meta, open(SNAP_META, "w"), indent=1); _SNAP["mtime"] = None

def from_snapshot(site, days):
    df, meta = load_snapshot()
    if df is None: return None
    sub = df[df.site == site["id"]].drop(columns="site").sort_values("date")
    if sub.empty: return None
    sub = sub[sub.date >= sub.date.max() - pd.Timedelta(days=days)].reset_index(drop=True); i = meta.get(site["id"], {})
    return sub, dict(source="snapshot", host=i.get("host") or "local snapshot", error=None, latest=str(sub.date.iloc[-1].date()), pixel=i.get("pixel"), fetched_at=i.get("fetched_at"))

def try_live(site, days):
    """-> (df, meta) on success, else (None, [error strings]). Never raises."""
    errs = []
    hosts = sorted([h for h in HOSTS if h.get("enabled", True) is not False], key=lambda h: h["name"] != _PREFERRED[0])  # last-good host first
    for h in hosts:
        if _is_down(h):
            errs.append(f"{h['name']}: skipped, connection failed recently (retries in <={COOLDOWN_S}s, or press Refresh)"); continue
        try:
            df, px = fetch_host(h, site, days); _PREFERRED[0] = h["name"]; _DOWN.pop(h["name"], None)
            return df, dict(source="live", host=h["name"], error=("; ".join(errs) or None), latest=str(df.date.iloc[-1].date()), pixel=px,
                            fetched_at=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%MZ"))
        except HostSkip as e:
            errs.append(f"{h['name']}: {type(e).__name__}: {e}"); log.warning("site=%s host=%s skipped (%s); trying next", site["id"], h["name"], e)
            if isinstance(e, ConnectionLevelError) and not _is_down(h):
                _DOWN[h["name"]] = time.time() + COOLDOWN_S; log.error("host %s UNREACHABLE; skipping it for %ss", h["name"], COOLDOWN_S)
        except QueryLevelError as e:  # same request would fail anywhere: fail fast, surface server text
            errs.append(f"{h['name']}: QUERY REJECTED: {e}"); log.error("site=%s host=%s query rejected: %s", site["id"], h["name"], e); break
        except Exception as e:  # parse/schema surprises must not kill the batch
            errs.append(f"{h['name']}: {type(e).__name__}: {e}"); log.error("site=%s host=%s unexpected: %r", site["id"], h["name"], e)
    return None, errs or ["no enabled hosts"]

def get_site_data(site, days):
    """-> (df, meta). Never raises. Order: LIVE -> local SNAPSHOT -> SIMULATED. meta['source'] says which; meta['error'] holds live errors."""
    df, r = try_live(site, days)
    if df is not None: return df, r
    err = " | ".join(r); snap = from_snapshot(site, days)
    if snap:
        snap[1]["error"] = err; log.warning("LIVE FAILED site=%s -> using LOCAL SNAPSHOT (latest obs %s). live errors: %s", site["id"], snap[1]["latest"], err)
        return snap
    log.error("!!! USING SIMULATED DATA site=%s (no live data, no snapshot) errors=%s", site["id"], err)
    return synthetic(site, days), dict(source="simulated", host=None, error=err, latest=None, pixel=None, fetched_at=None)
