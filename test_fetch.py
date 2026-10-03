import sys, os, unittest, datetime as dt
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crw, tempfile
H1, H2 = crw.HOSTS[0]["base"], crw.HOSTS[1]["base"]
DAS = "Attributes {\n  CRW_SST {\n  }\n  CRW_SSTANOMALY {\n  }\n  CRW_HOTSPOT {\n  }\n  CRW_DHW {\n  }\n  CRW_BAA {\n  }\n  NC_GLOBAL {\n    String testOutOfDate \"now-60hours\";\n  }\n}"
def csv(days_old=4, n=40, nan_first=False, lat=-23.425, lon=151.925):
    end = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=days_old); v = "NaN" if nan_first else "27.5"
    rows = [f"{end - dt.timedelta(days=n-i)}T12:00:00Z,{lat},{lon},{v},0.3,{0 if nan_first else .2},{v if nan_first else 1.5},1" for i in range(n)]
    return "time,latitude,longitude,CRW_SST,CRW_SSTANOMALY,CRW_HOTSPOT,CRW_DHW,CRW_BAA\nUTC,degrees_north,degrees_east,Celsius,Celsius,Celsius,Celsius weeks,1\n" + "\n".join(rows)
class R:
    def __init__(s, code, text): s.status_code, s.text = code, text
SITE = crw.SITE_BY_ID["gbr-heron"]
class T(unittest.TestCase):
    def setUp(s):
        s.tmp = tempfile.mkdtemp(); mock.patch.object(crw, "SNAP_CSV", os.path.join(s.tmp, "snapshot.csv")).start(); mock.patch.object(crw, "SNAP_META", os.path.join(s.tmp, "m.json")).start()
        crw._SNAP.update(mtime=None, df=None, meta={}); s.addCleanup(mock.patch.stopall); crw._PRE.clear(); crw._DOWN.clear(); crw._V4[0] = False; crw._PREFERRED[0] = None; s.urls = []; p = mock.patch("crw.time.sleep"); p.start(); s.addCleanup(p.stop)
    def run_with(s, handler):
        def get(url, timeout=None, **kw):
            assert timeout == (10, 60); s.urls.append(url); return handler(url)
        with mock.patch("crw.requests.get", get): return crw.get_site_data(SITE, 30)
    def test_live_primary_uses_exact_names_last_and_snapped_pixel(s):
        df, m = s.run_with(lambda u: R(200, DAS if ".das" in u else csv()))
        q = [u for u in s.urls if ".csv" in u][0]
        s.assertEqual(m["source"], "live"); s.assertIn("pfeg", m["host"]); s.assertIn("(last)", q); s.assertNotIn("sea_surface", q)
        for v in crw.RAW_VARS: s.assertIn(v, q)
        s.assertIn("(-23.425)", q); s.assertIn("(151.925)", q); s.assertEqual(list(df.columns), ["date", "sst", "sst_anom", "hotspot", "dhw", "baa"])
    def test_raw_oserror_then_failover(s):
        def h(u):
            if H1 in u: raise TimeoutError("raw")
            return R(200, DAS if ".das" in u else csv())
        df, m = s.run_with(h); s.assertEqual(m["source"], "live"); s.assertIn("PacIOOS", m["host"]); s.assertIn("pfeg", m["error"])
    def test_unknown_dataset_404_fails_over(s):
        h = lambda u: R(404, "Not Found: Currently unknown datasetID=NOAA_DHW") if H1 in u else R(200, DAS if ".das" in u else csv())
        s.assertIn("PacIOOS", s.run_with(h)[1]["host"])
    def test_query_error_fails_fast_with_server_text(s):
        h = lambda u: R(200, DAS) if ".das" in u else R(400, "Bad Request: Unrecognized variable=foo")
        df, m = s.run_with(h)
        s.assertEqual(m["source"], "simulated"); s.assertIn("Unrecognized variable=foo", m["error"])
        s.assertEqual(len([u for u in s.urls if ".csv" in u]), 1)  # did NOT retry the identical query on host 2
    def test_nan_pixel_uses_neighbour(s):
        calls = []
        def h(u):
            if ".das" in u: return R(200, DAS)
            calls.append(u); return R(200, csv(nan_first=len(calls) == 1))
        df, m = s.run_with(h); s.assertEqual(m["source"], "live"); s.assertEqual(len(calls), 2)
    def test_stale_host_skipped(s):
        h = lambda u: R(200, DAS if ".das" in u else csv(days_old=40 if H1 in u else 4))
        df, m = s.run_with(h); s.assertIn("PacIOOS", m["host"]); s.assertIn("stale", m["error"])
    def test_missing_variable_in_das_skips_host(s):
        h = lambda u: R(200, (DAS.replace("CRW_HOTSPOT", "OTHER") if H1 in u else DAS) if ".das" in u else csv())
        df, m = s.run_with(h); s.assertIn("PacIOOS", m["host"]); s.assertIn("lacks variables", m["error"])
    def test_retry_on_503_then_ok(s):
        n = {"i": 0}
        def h(u):
            if ".das" in u: return R(200, DAS)
            n["i"] += 1; return R(503, "") if n["i"] == 1 else R(200, csv())
        s.assertEqual(s.run_with(h)[1]["source"], "live")
    def test_dead_host_is_not_retried_per_site(s):
        def h(u):
            if H1 in u: raise TimeoutError("x")
            return R(200, DAS if ".das" in u else csv())
        s.run_with(h); n = len([u for u in s.urls if H1 in u]); s.assertIn("NOAA SWFSC/ERD (coastwatch.pfeg)", crw._DOWN)
        s.run_with(h); s.assertEqual(len([u for u in s.urls if H1 in u]), n)  # 2nd site never touches the dead host
        crw.reset_health(); s.assertEqual(crw._DOWN, {})
    def test_ipv4_fallback_after_connect_timeout(s):
        n = {"i": 0}
        def h(u):
            if ".das" in u:
                n["i"] += 1
                if n["i"] == 1: raise crw.requests.exceptions.ConnectTimeout("x")
                return R(200, DAS)
            return R(200, csv())
        df, m = s.run_with(h); s.assertEqual(m["source"], "live"); s.assertTrue(crw._V4[0]); s.assertIn("pfeg", m["host"])
    def _dead(s, u): raise TimeoutError("blocked")
    def test_snapshot_used_when_live_fails_and_labelled(s):
        crw.save_snapshot({SITE["id"]: (crw.synthetic(SITE, 200), dict(host="NOAA SWFSC/ERD (coastwatch.pfeg)", pixel=(1, 2), fetched_at="2026-10-03 10:00Z", latest="x"))})
        df, m = s.run_with(s._dead); s.assertEqual(m["source"], "snapshot"); s.assertEqual(m["fetched_at"], "2026-10-03 10:00Z"); s.assertIn("pfeg", m["host"])
        s.assertTrue(len(df) in (30, 31) and list(df.columns) == ["date", "sst", "sst_anom", "hotspot", "dhw", "baa"]); s.assertIn("ConnectionLevelError", m["error"])
    def test_live_beats_snapshot(s):
        crw.save_snapshot({SITE["id"]: (crw.synthetic(SITE, 50), {})})
        s.assertEqual(s.run_with(lambda u: R(200, DAS if ".das" in u else csv()))[1]["source"], "live")
    def test_corrupt_snapshot_degrades_to_simulated(s):
        open(crw.SNAP_CSV, "w").write("garbage,,\n1,2"); df, m = s.run_with(s._dead); s.assertEqual(m["source"], "simulated")
    def test_snapshot_save_merges_sites(s):
        o = crw.SITE_BY_ID["flk"]; crw.save_snapshot({SITE["id"]: (crw.synthetic(SITE, 40), {})}); crw.save_snapshot({o["id"]: (crw.synthetic(o, 40), {})})
        d, _ = crw.load_snapshot(); s.assertEqual(set(d.site), {SITE["id"], "flk"})
    def test_snap(s): s.assertEqual((crw.snap(-23.44), crw.snap(151.91), crw.snap(0.0)), (-23.425, 151.925, 0.025))
if __name__ == "__main__": unittest.main()
