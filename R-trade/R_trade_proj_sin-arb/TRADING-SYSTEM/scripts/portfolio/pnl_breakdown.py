import sys, json, hmac, hashlib, time, urllib.request, urllib.parse
from collections import defaultdict
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
start = now - 48*3600*1000

# Transaction log: все денежные потоки
rows = []
cursor = ""
for _ in range(10):
    params = {"accountType":"UNIFIED","startTime":str(start),"limit":"50"}
    if cursor: params["cursor"] = cursor
    r = get("/v5/account/transaction-log", params)
    rows += r["result"]["list"]
    cursor = r["result"].get("nextPageCursor","")
    if not cursor: break

# Группировка по типу и дню
by_type = defaultdict(float)
by_day_type = defaultdict(lambda: defaultdict(float))
fees_by_type = defaultdict(float)
by_symbol_realized = defaultdict(float)

for x in rows:
    t = x.get("type","?")
    day = time.strftime("%d.%m", time.localtime(int(x["transactionTime"])/1000))
    change = float(x.get("change",0) or 0)
    fee = float(x.get("fee",0) or 0)
    by_type[t] += change
    by_day_type[day][t] += change
    fees_by_type[t] += fee
    sym = x.get("symbol","")
    if t == "TRADE" and sym:
        by_symbol_realized[sym] += change

print(f"\n{'='*64}")
print(f"  ДЕНЕЖНЫЕ ПОТОКИ за 48ч (transaction-log, {len(rows)} записей)")
print(f"{'='*64}")
print(f"\n  {'Тип':<22} {'Δ кэша':>12} {'Комиссии':>12}")
print(f"  {'─'*48}")
for t, v in sorted(by_type.items(), key=lambda x:-abs(x[1])):
    print(f"  {t:<22} {v:>+12.4f} {fees_by_type[t]:>12.4f}")
print(f"  {'─'*48}")
print(f"  {'ИТОГО':<22} {sum(by_type.values()):>+12.4f} {sum(fees_by_type.values()):>12.4f}")

print(f"\n  ── По дням ──")
for day in sorted(by_day_type):
    parts = "  ".join(f"{t}={v:+.2f}" for t,v in sorted(by_day_type[day].items(), key=lambda x:-abs(x[1])) if abs(v)>0.005)
    print(f"  {day}:  {parts}")

print(f"\n  ── TRADE-потоки по символам (realized за 48ч) ──")
for sym, v in sorted(by_symbol_realized.items(), key=lambda x:-abs(x[1])):
    if abs(v) > 0.01:
        print(f"  {sym:<32} {v:>+10.4f}")
print()
