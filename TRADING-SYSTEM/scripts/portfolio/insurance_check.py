"""D2 ETH: страховка (стрэнгл) сама себя кормит? — дневной чек.
Сравнивает тету стрэнгла (стоимость страховки/день) с реализованным доходом грида (ETHUSDT closed-pnl).
Read-only ключ из C:\\Users\\DoOs\\.bybit\\credentials.json. Запуск: python insurance_check.py [--days N]
"""
import sys, json, hmac, hashlib, time, urllib.request, urllib.parse, datetime, argparse
sys.stdout.reconfigure(encoding="utf-8")
with open(r"C:\Users\DoOs\.bybit\credentials.json", encoding="utf-8") as f:
    d = json.load(f)
KEY, SECRET = d["api_key"], d["api_secret"]; BASE = "https://api.bybit.com"

def sget(path, params):
    ts=str(int(time.time()*1000)); recv="20000"; qs=urllib.parse.urlencode(params)
    sig=hmac.new(SECRET.encode(), f"{ts}{KEY}{recv}{qs}".encode(), hashlib.sha256).hexdigest()
    req=urllib.request.Request(f"{BASE}{path}?{qs}", headers={"X-BAPI-API-KEY":KEY,"X-BAPI-TIMESTAMP":ts,"X-BAPI-SIGN":sig,"X-BAPI-RECV-WINDOW":recv})
    with urllib.request.urlopen(req, timeout=20) as r: return json.loads(r.read())
def pub(path, params):
    with urllib.request.urlopen(f"{BASE}{path}?"+urllib.parse.urlencode(params), timeout=20) as r: return json.loads(r.read())
def paged(path, params, mp=10):
    out=[]; cur=""
    for _ in range(mp):
        p=dict(params);
        if cur: p["cursor"]=cur
        r=sget(path,p); res=r.get("result") or {}; out+=res.get("list") or []; cur=res.get("nextPageCursor") or ""
        if not cur: break
    return out
def f(x):
    try: return float(x)
    except: return 0.0

ap=argparse.ArgumentParser(); ap.add_argument("--days", type=float, default=None); a=ap.parse_args()
now=datetime.datetime.now(datetime.timezone.utc)
start = (now - datetime.timedelta(days=a.days)) if a.days else now.replace(hour=0,minute=0,second=0,microsecond=0)
s_ms=int(start.timestamp()*1000); n_ms=int(now.timestamp()*1000)
label = f"за {a.days}д" if a.days else "сегодня (с 00:00Z)"

px=f(pub("/v5/market/tickers",{"category":"linear","symbol":"ETHUSDT"})["result"]["list"][0]["lastPrice"])
print(f"\n=== СТРАХОВКА D2 (ETH стрэнгл + грид): сама себя кормит? ===")
print(f"ETH {px:.2f}  ·  {now:%Y-%m-%d %H:%M}Z  ·  окно: {label}\n")

# --- стрэнгл (страховка) ---
opos=[p for p in sget("/v5/position/list",{"category":"option"})["result"]["list"]
      if f(p.get("size",0))!=0 and p["symbol"].startswith("ETH")]
theta=vega=dlt=upnl=0.0
print("СТРЭНГЛ (страховка):")
for p in sorted(opos,key=lambda x:x["symbol"]):
    q=f(p["size"])*(1 if p["side"]=="Buy" else -1)
    theta+=q*f(p.get("theta",0)); vega+=q*f(p.get("vega",0)); upnl+=f(p["unrealisedPnl"])
    print(f"  {p['symbol'].replace('-USDT',''):<22}{p['side']:<5}{f(p['size']):>5} mark {f(p['markPrice']):>7.2f} uPnL {f(p['unrealisedPnl']):>7.2f} θ {q*f(p.get('theta',0)):>6.2f}")
cost_day=-theta
print(f"  ТЕТА/день (стоимость страховки): {theta:+.2f} $   ·  Вега {vega:+.2f}  ·  uPnL стрэнгла {upnl:+.2f} $")

# --- грид (мотор дохода) ---
gpos=[p for p in sget("/v5/position/list",{"category":"linear","settleCoin":"USDT"})["result"]["list"]
      if p["symbol"]=="ETHUSDT" and f(p.get("size",0))!=0]
cp=paged("/v5/position/closed-pnl",{"category":"linear","symbol":"ETHUSDT","startTime":str(s_ms),"endTime":str(n_ms),"limit":"100"})
grid_pnl=sum(f(x.get("closedPnl",0)) for x in cp)
fills=paged("/v5/execution/list",{"category":"linear","symbol":"ETHUSDT","startTime":str(s_ms),"endTime":str(n_ms),"limit":"100"})
ntr=sum(1 for x in fills if x.get("execType")=="Trade")
print(f"\nГРИД (мотор дохода):")
for p in gpos:
    print(f"  Инвентарь ETHUSDT: {p['side']} {f(p['size'])} @ {f(p['avgPrice']):.2f}  uPnL {f(p['unrealisedPnl']):+.2f} $")
print(f"  ETHUSDT реализ. {label}: {grid_pnl:+.2f} $  ({ntr} филлов)   ← прокси грид-дохода (closed-pnl)")

# --- вердикт ---
per_day = grid_pnl/(a.days if a.days else 1)
print(f"\n=== ВЕРДИКТ ===")
gap = grid_pnl - cost_day*(a.days if a.days else 1)
if grid_pnl >= cost_day*(a.days if a.days else 1):
    print(f"  ✓ СТРАХОВКА КОРМИТ СЕБЯ: грид {grid_pnl:+.2f}$ ≥ тета {-cost_day*(a.days if a.days else 1):.2f}$  ·  запас {gap:+.2f}$")
else:
    print(f"  ✗ ДОПЛАТА: грид {grid_pnl:+.2f}$ < тета {-cost_day*(a.days if a.days else 1):.2f}$  ·  доплачиваешь {gap:+.2f}$")
print(f"  (грид-доход = ETHUSDT closed-pnl; для точности сверь «Общий P&L» на карточке грид-бота в UI)\n")
