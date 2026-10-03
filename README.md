# ReefWatch (flat layout: every file in this folder)
Fetch order: LIVE (NOAA, then PacIOOS) -> local SNAPSHOT -> SIMULATED (last resort). Every reef is badged with which one it used.

## A. Your machine can reach NOAA/PacIOOS
  pip install -r requirements.txt
  python diagnose.py       # should end with "LIVE data OK"
  python app.py            # http://127.0.0.1:5000

## B. Your machine is blocked: build a snapshot elsewhere (e.g. GitHub Codespace), then use it locally
  1. In the Codespace:  pip install -r requirements.txt && python download_snapshot.py
  2. Download snapshot.csv and snapshot_meta.json (Explorer -> right-click -> Download)
  3. Put both files beside app.py on your machine, delete model.npz/footprint.json if present, then: python app.py
  Re-run step 1-3 whenever you want fresher data.

python -m pytest   # no network needed
Env: CRW_SAFETY_DAYS(3) CRW_COOLDOWN_S(120) CRW_FORCE_IPV4=1 TRAIN_DAYS(1100) REFRESH_S(3600) PORT(5000)
