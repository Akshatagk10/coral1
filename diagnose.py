"""python diagnose.py  -- finds WHY live fetches fail on THIS machine (DNS / IPv4 / IPv6 / proxy / TLS / ERDDAP), then runs one real end-to-end fetch."""
import socket, time, urllib.parse as up, urllib.request, requests, crw
def tcp(ip, fam, t=6):
    s = None; t0 = time.time()
    try:
        s = socket.socket(fam, socket.SOCK_STREAM); s.settimeout(t); s.connect((ip, 443)); return True, f"connected in {time.time()-t0:.2f}s"
    except OSError as e: return False, f"{type(e).__name__}: {e}"
    finally:
        if s: s.close()
def check(h):
    host = up.urlparse(h["base"]).hostname; print(f"\n== {h['name']}  ({host})"); res = {"v4": None, "v6": None}
    try: infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as e: print("  DNS: FAILED", e); return "DNS cannot resolve this host: DNS/ISP block. Try another network, VPN, or DNS 1.1.1.1 / 8.8.8.8."
    for fam, key in ((socket.AF_INET, "v4"), (socket.AF_INET6, "v6")):
        ips = [i[4][0] for i in infos if i[0] == fam]
        if not ips: print(f"  {key}: no address record"); continue
        ok, msg = tcp(ips[0], fam); res[key] = ok; print(f"  {key} {ips[0]}: {'OK' if ok else 'FAIL'} ({msg})")
    if not any(res.values()): return "TCP/443 unreachable over every address family: network, ISP, firewall or VPN blocks this host. NOT a code problem. Try another network/VPN."
    note = "IPv6 black hole (v6 fails, v4 works): crw.py now auto-switches to IPv4; or set CRW_FORCE_IPV4=1." if res["v6"] is False and res["v4"] else ""
    try:
        r = requests.get(h["base"] + "/index.html", timeout=(10, 30), headers=crw.HDR); print(f"  HTTPS GET: {r.status_code}")
        if r.status_code != 200: return f"Reached something but got HTTP {r.status_code} ({r.text[:90].strip()!r}): a proxy / firewall / egress allowlist / WAF is answering instead of the ERDDAP server. Allow this host or use another network. {note}"
    except Exception as e: return f"TCP works but HTTPS fails ({type(e).__name__}: {str(e)[:120]}): proxy / TLS interception / corporate CA. {note}"
    return ("OK. " + note).strip()
def reach(host):
    try: ip = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    except OSError: return False
    return tcp(ip, socket.AF_INET, 5)[0]
print("== Baseline reachability (TCP/443 over IPv4)")
base = {h: reach(h) for h in ["www.google.com", "one.one.one.one", "www.noaa.gov", "coastwatch.noaa.gov", "oceanwatch.pifsc.noaa.gov", "coralreefwatch.noaa.gov"]}
[print(f"  {'OK  ' if v else 'FAIL'} {k}") for k, v in base.items()]
print("proxies seen by Python:", urllib.request.getproxies() or "none")
verd = {h["name"]: check(h) for h in crw.HOSTS if h.get("enabled", True) is not False}
print("\n== End-to-end fetch (site: gbr-heron, 60 days)"); df, m = crw.get_site_data(crw.SITE_BY_ID["gbr-heron"], 60)
print(f"  source={m['source']} host={m['host']} rows={len(df)} latest={m['latest']} pixel={m['pixel']}\n  errors={m['error']}")
print("\n== ROOT-CAUSE SUMMARY"); [print(f"  {k}: {v}") for k, v in verd.items()]
if not base["www.google.com"] and not base["one.one.one.one"]: print("  Baseline: general internet connectivity is failing; fix Wi-Fi/router first.")
elif not any(v for k, v in base.items() if k.endswith("noaa.gov")): print("  Baseline: internet works but ALL NOAA hosts fail: your network/ISP is blocking or mis-routing NOAA/US-gov ranges. Use a phone hotspot or VPN, then rerun.")
else: print("  Baseline: some NOAA hosts ARE reachable (", [k for k, v in base.items() if v and k.endswith("noaa.gov")], "): tell me which, and I can point the fetch layer at them.")
print("  Overall:", "LIVE data OK" if m["source"] == "live" else "still SIMULATED, fix the first non-OK line above")
