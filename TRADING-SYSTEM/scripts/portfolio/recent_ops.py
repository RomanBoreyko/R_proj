import sys, json, hmac, hashlib, time, urllib.request, urllib.parse
sys.stdout.reconfigure(encoding="utf-8")

with open(r"C:\Users\DoOs\.bybit\credentials.json", encoding="utf-8") as f:
    d = json.load(f)
KEY, SECRET = d["api_key"], d["api_secret"]
BASE = "https://api.bybit.com"

def get(path, params):
    ts = str(int(time.time()*1000)); recv = "20000"
    qs = urllib.parse.urlencode(params)
    sig = hmac.new(SECRET.encode(), f"{ts}{KEY}{recv}{qs}".encode(), hashlib.sha256).hexdigest()
    req = urllib.request.Request(f"{BASE}{path}?{qs}", headers={
        "X-BAPI-API-KEY":KEY,"X-BAPI-TIMESTAMP":ts,"X-BAPI-SIGN":sig,"X-BAPI-RECV-WINDOW":recv})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

now = int(time.time()*1000)
day_ago = now - 24*3600*1000

print(f"\n{'='*70}")
print(f"  ИСПОЛНЕНИЯ за 24ч — {time.strftime('%Y-%m-%d %H:%M:%S')}")
print(f"{'='*70}")

for cat in ["linear","option","spot"]:
    r = get("/v5/execution/list", {"category":cat,"startTime":str(day_ago),"limit":"100"})
    fills = r["result"]["list"]
    if not fills: continue
    print(f"\n  ── {cat.upper()} ({len(fills)}) " + "─"*45)
    for x in sorted(fills, key=lambda f:f["execTime"]):
        t = time.strftime("%d.%m %H:%M:%S", time.localtime(int(x["execTime"])/1000))
        fee = float(x.get("execFee",0) or 0)
        print(f"  {t}  {x['symbol']:<28} {x['side']:<5} {float(x['execQty']):>9.4f} @ {float(x['execPrice']):>10.3f}  fee={fee:+.4f} {x.get('execType','')}")

# Открытые ордера
print(f"\n{'='*70}")
print(f"  ОТКРЫТЫЕ ОРДЕРА")
print(f"{'='*70}")
for cat in ["linear","option","spot"]:
    try:
        r = get("/v5/order/realtime", {"category":cat,"settleCoin":"USDT","limit":"50"} if cat=="linear" else {"category":cat,"limit":"50"})
        orders = r["result"]["list"]
        if not orders: continue
        print(f"\n  ── {cat.upper()} ──")
        for o in orders:
            print(f"  {o['symbol']:<28} {o['side']:<5} {float(o['qty']):>9.4f} @ {float(o['price'] or 0):>10.3f}  {o['orderStatus']}")
    except Exception as e:
        print(f"  {cat}: {e}")
print()
