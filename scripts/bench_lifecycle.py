"""Benchmark: Steel session lifecycle timing (create → connect → navigate → close → release)."""
import json, statistics, time, urllib.request
from urllib.parse import urlparse, urlunparse
from playwright.sync_api import sync_playwright

STEEL = "http://localhost:3001"
N = 10

def http(method, url, body=None):
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

def remap_ws(ws_url):
    pa = urlparse(STEEL)
    pw = urlparse(ws_url)
    return urlunparse(pw._replace(scheme="ws", netloc=pa.netloc))

results = []
print(f"Session lifecycle timing ({N} trials)\n")

with sync_playwright() as pw:
    for i in range(N):
        t0 = time.perf_counter()

        sess = http("POST", f"{STEEL}/v1/sessions", {})
        t_create = time.perf_counter() - t0
        if "error" in sess:
            print(f"  [{i+1:02d}/{N}] FAIL create: {sess['error']}")
            time.sleep(2)
            continue
        sid = sess["id"]

        ws = remap_ws(sess["websocketUrl"])
        t1 = time.perf_counter()
        browser = pw.chromium.connect_over_cdp(ws)
        t_connect = time.perf_counter() - t1

        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()
        t2 = time.perf_counter()
        page.goto("about:blank")
        t_navigate = time.perf_counter() - t2

        t3 = time.perf_counter()
        browser.close()
        t_close = time.perf_counter() - t3

        t4 = time.perf_counter()
        http("POST", f"{STEEL}/v1/sessions/{sid}/release", None)
        t_release = time.perf_counter() - t4

        total = time.perf_counter() - t0
        row = {"create": round(t_create, 3), "connect": round(t_connect, 3),
               "navigate": round(t_navigate, 3), "close": round(t_close, 3),
               "release": round(t_release, 3), "total": round(total, 3)}
        results.append(row)
        print(f"  [{i+1:02d}/{N}] create={t_create:.3f}s connect={t_connect:.3f}s "
              f"nav={t_navigate:.3f}s close={t_close:.3f}s release={t_release:.3f}s total={total:.3f}s")
        time.sleep(0.3)

print()
for key in ["create", "connect", "navigate", "close", "release", "total"]:
    vals = [r[key] for r in results]
    s = sorted(vals)
    print(f"  {key:10s}: avg={statistics.mean(vals):.3f}s  p50={s[len(s)//2]:.3f}s  "
          f"p95={s[min(int(len(s)*0.95),len(s)-1)]:.3f}s  max={max(vals):.3f}s")

with open("/tmp/bench_lifecycle.json", "w") as f:
    json.dump(results, f, indent=2)
print(f"\n  Saved to /tmp/bench_lifecycle.json")
