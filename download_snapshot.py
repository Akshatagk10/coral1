"""Run this on a machine that CAN reach NOAA/PacIOOS (e.g. a GitHub Codespace).
   python download_snapshot.py [days]     -> writes snapshot.csv + snapshot_meta.json
   Then copy those two files into the same folder as app.py on your own machine."""
import sys, logging
from concurrent.futures import ThreadPoolExecutor
import crw
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
days = int(sys.argv[1]) if len(sys.argv) > 1 else crw.TRAIN_DAYS
sites = list(crw.SITE_BY_ID.values()); crw.reset_health()
with ThreadPoolExecutor(4) as ex: res = list(ex.map(lambda s: crw.try_live(s, days), sites))
good = {s["id"]: r for s, r in zip(sites, res) if r[0] is not None}
for s, (df, r) in zip(sites, res):
    print(f"  {'OK  ' if df is not None else 'FAIL'} {s['name']:<28}", f"{len(df)} rows, latest {r['latest']}, via {r['host']}" if df is not None else str(r[-1])[:150])
if not good:
    print("\\nNo live data could be downloaded, so nothing was written. Run `python diagnose.py` here to see why."); sys.exit(1)
crw.save_snapshot(good)
print(f"\\nSaved {len(good)}/{len(sites)} reefs to {crw.SNAP_CSV} and {crw.SNAP_META}" + ("" if len(good) == len(sites) else "  (missing reefs will fall back to synthetic data until you re-run)"))
print("Next: download snapshot.csv and snapshot_meta.json and put them beside app.py on your machine.")
