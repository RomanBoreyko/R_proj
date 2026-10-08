import sys, json, hmac, hashlib, time, urllib.request, urllib.parse
sys.stdout.reconfigure(encoding="utf-8")

CREDS = r"C:\Users\DoOs\.bybit\credentials.json"
BASE  = "https://api.bybit.com"

with open(CREDS, encoding="utf-8") as f:
    d = json.load(f)
KEY, SECRET = d["api_key"], d["api_secret"]

def sign(ts, recv, qs):
    msg = f"{ts}{KEY}{recv}{qs}"
    return hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

def get(path, params):
    ts = str(int(time.time()*1000)); recv = "20000"
    qs = urllib.parse.urlencode(params)
    headers = {"X-BAPI-API-KEY":KEY,"X-BAPI-TIMESTAMP":ts,
               "X-BAPI-SIGN":sign(ts,recv,qs),"X-BAPI-RECV-WINDOW":recv}
    req = urllib.request.Request(f"{BASE}{path}?{qs}", headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def pub(path, params):
    qs = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{BASE}{path}?{qs}", timeout=15) as r:
        return json.loads(r.read())

# ── 1. Баланс ─────────────────────────────────────────────────────────────────
wb = get("/v5/account/wallet-balance", {"accountType":"UNIFIED"})
acc = wb["result"]["list"][0]
eq   = float(acc["totalEquity"])
im   = float(acc["totalInitialMargin"])
mm   = float(acc["totalMaintenanceMargin"])
avail= float(acc["totalAvailableBalance"])

print(f"\n{'='*60}")
print(f"  ПОРТФЕЛЬ — {time.strftime('%Y-%m-%d %H:%M:%S')}")
print(f"{'='*60}")
print(f"  Эквити:    {eq:>10.2f} USDT")
print(f"  IM:        {im:>10.2f}  ({im/eq*100:.1f}% эквити)")
print(f"  MM:        {mm:>10.2f}  (запас {eq/mm:.2f}×)")
print(f"  Свободно:  {avail:>10.2f}")

# ── 2. Все перпы/фьючи ────────────────────────────────────────────────────────
perps = []
for _sc in ("USDT","USDC"):
    pl = get("/v5/position/list", {"category":"linear","settleCoin":_sc,"limit":"200"})
    perps += [p for p in pl["result"]["list"] if float(p["size"])>0]

# ── 3. Все опционы с греками ──────────────────────────────────────────────────
opts_raw = []
cursor = ""
for _ in range(5):
    params = {"category":"option","limit":"200"}
    if cursor: params["cursor"] = cursor
    r = get("/v5/position/list", params)
    opts_raw += r["result"]["list"]
    cursor = r["result"].get("nextPageCursor","")
    if not cursor: break
opts = [o for o in opts_raw if float(o["size"])>0]

# ── 4. Текущие цены ───────────────────────────────────────────────────────────
prices = {}
for sym in ["XAUTUSDT","XAUUSDT","XAGUSDT","ETHUSDT","SOLUSDT","MNTUSDT","BTCUSDT","DOGEUSDT","XRPUSDT"]:
    try:
        r = pub("/v5/market/tickers", {"category":"linear","symbol":sym})
        prices[sym] = float(r["result"]["list"][0]["lastPrice"])
    except: pass

print(f"\n  Цены: " + "  ".join(f"{k.replace('USDT','')}={v:,.2f}" for k,v in prices.items()))

# ── 5. Греки и $-дельта по активам ───────────────────────────────────────────
from collections import defaultdict
G = defaultdict(lambda: {"delta":0,"gamma":0,"theta":0,"vega":0,"uPnL":0,"notional":0})

base_of_sym = lambda s: s.split("-")[0] if "-" in s else s.replace("USDT","").replace("PERP","")

for o in opts:
    b = o["symbol"].split("-")[0]
    sign_ = 1 if o["side"]=="Buy" else -1  # опционы: греки уже с правильным знаком от Bybit
    G[b]["delta"]   += float(o.get("delta",0) or 0)
    G[b]["gamma"]   += float(o.get("gamma",0) or 0)
    G[b]["theta"]   += float(o.get("theta",0) or 0)
    G[b]["vega"]    += float(o.get("vega",0)  or 0)
    G[b]["uPnL"]    += float(o.get("unrealisedPnl",0) or 0)
    qty = float(o["size"]); price = float(o.get("markPrice",0) or 0)
    G[b]["notional"] += qty * price

for p in perps:
    sym = p["symbol"]
    b = sym.split("-")[0].replace("USDT","").replace("PERP","")
    if b == "XAU": b = "XAU"   # XAUUSDT → XAU
    elif "XAUT" in sym: b = "XAUT"
    elif "XAG" in sym:  b = "XAG"
    elif "ETH" in sym and "JUN" not in sym and "JUL" not in sym and "MAR" not in sym: b = "ETH"
    elif "SOL" in sym and "JUN" not in sym: b = "SOL"
    elif "MNT" in sym and "JUN" not in sym: b = "MNT"
    elif "BTC" in sym and "JUN" not in sym: b = "BTC"
    elif "HUS" in sym: b = "HUS"
    elif "SAHARA" in sym: b = "SAHARA"
    d = (1 if p["side"]=="Buy" else -1) * float(p["size"])
    G[b]["delta"] += d
    G[b]["uPnL"]  += float(p.get("unrealisedPnl",0) or 0)
    G[b]["notional"] += abs(float(p["size"])) * float(p.get("avgPrice",0) or 0)

# $-дельта
sym_map = {"XAUT":"XAUTUSDT","XAU":"XAUUSDT","XAG":"XAGUSDT","ETH":"ETHUSDT",
           "SOL":"SOLUSDT","MNT":"MNTUSDT","BTC":"BTCUSDT","BNB":"BNBUSDT",
           "DOGE":"DOGEUSDT","XRP":"XRPUSDT","HUS":"HUSDT","SAHARA":"SAHARAUSDT"}
for b,g in G.items():
    px = prices.get(sym_map.get(b,b+"USDT"),0)
    g["price"] = px
    g["delta_usd"] = g["delta"] * px

tot_delta_usd = sum(g["delta_usd"] for g in G.values())
tot_theta     = sum(g["theta"]     for g in G.values())
tot_vega      = sum(g["vega"]      for g in G.values())
tot_gamma     = sum(g["gamma"]     for g in G.values())
tot_upnl      = sum(g["uPnL"]      for g in G.values())

print(f"\n{'─'*60}")
print(f"  МЕТА-СПРЕД (вся книга)")
print(f"{'─'*60}")
print(f"  Δ$ (направление):  {tot_delta_usd:>+10.2f}  ({'шорт рынка' if tot_delta_usd<0 else 'лонг рынка'})")
print(f"  Γ (гамма):         {tot_gamma:>+10.4f}  ({'шорт γ' if tot_gamma<0 else 'лонг γ'})")
print(f"  Θ (тета/день):     {tot_theta:>+10.2f}  ({'платим' if tot_theta<0 else 'собираем'})")
print(f"  Θ% эквити/день:    {tot_theta/eq*100:>+9.2f}%")
print(f"  V (вега):          {tot_vega:>+10.2f}  ({'шорт волы' if tot_vega<0 else 'лонг волы'})")
print(f"  uPnL total:        {tot_upnl:>+10.2f}")

# сценарий-убийца?
killer = tot_delta_usd < 0 and tot_vega < 0
if killer:
    print(f"\n  ⚠ СЦЕНАРИЙ-УБИЙЦА: рост рынка + всплеск IV → Δ+Vega+Γ бьют одновременно")

print(f"\n{'─'*60}")
print(f"  {'АКТИВ':<8} {'Δ$':>9} {'Δ шт':>8} {'Γ':>8} {'Θ/д':>8} {'Vega':>8} {'uPnL':>9} {'цена':>8}")
print(f"{'─'*60}")
for b,g in sorted(G.items(), key=lambda x:-abs(x[1]["delta_usd"])):
    print(f"  {b:<8} {g['delta_usd']:>+9.1f} {g['delta']:>+8.3f} {g['gamma']:>8.4f} {g['theta']:>+8.2f} {g['vega']:>+8.2f} {g['uPnL']:>+9.2f} {g['price']:>8.2f}")

# ── 6. Перпы/фьючи ────────────────────────────────────────────────────────────
print(f"\n{'─'*60}")
print(f"  ПЕРПЫ / ФЬЮЧИ ({len(perps)})")
print(f"{'─'*60}")
print(f"  {'Символ':<26} {'Сторона':<6} {'Размер':>8} {'Средняя':>9} {'Mark':>9} {'uPnL':>9}")
for p in sorted(perps, key=lambda x:x["symbol"]):
    print(f"  {p['symbol']:<26} {p['side']:<6} {float(p['size']):>8.4f} {float(p['avgPrice']):>9.2f} {float(p['markPrice']):>9.2f} {float(p['unrealisedPnl']):>+9.2f}")

# ── 7. Опционы с греками ──────────────────────────────────────────────────────
print(f"\n{'─'*60}")
print(f"  ОПЦИОНЫ ({len(opts)}) — сортировка по базовому активу + экспирации")
print(f"{'─'*60}")
print(f"  {'Контракт':<34} {'S':<4} {'Кол':>5} {'Mark':>7} {'uPnL':>8} {'Δ':>7} {'Γ':>7} {'Θ':>7} {'V':>7} {'IV%':>6}")
for o in sorted(opts, key=lambda x:x["symbol"]):
    s = o["symbol"].replace("-USDT","")
    iv = float(o.get("impliedVolatility",0) or 0)*100
    print(f"  {s:<34} {'B' if o['side']=='Buy' else 'S':<4} {float(o['size']):>5.2f} {float(o['markPrice']):>7.3f} {float(o['unrealisedPnl']):>+8.2f} {float(o.get('delta',0) or 0):>+7.3f} {float(o.get('gamma',0) or 0):>7.4f} {float(o.get('theta',0) or 0):>+7.3f} {float(o.get('vega',0) or 0):>+7.3f} {iv:>6.1f}")

# ── 8. Gold/Silver stat-arb ───────────────────────────────────────────────────
print(f"\n{'─'*60}")
print(f"  XAUT/XAG RATIO")
print(f"{'─'*60}")
pxaut = prices.get("XAUTUSDT",0); pxag = prices.get("XAGUSDT",0)
ratio = pxaut/pxag if pxag else 0
# исторически: μ≈60.3, σ≈1.67 (из предыдущих расчётов 1h×200)
mu, sigma = 60.32, 1.675
z = (ratio - mu) / sigma
print(f"  XAUT:  {pxaut:.2f}")
print(f"  XAG:   {pxag:.3f}")
print(f"  Ratio: {ratio:.3f}")
print(f"  μ={mu}, σ={sigma} (1h×200 из прошлого расчёта)")
print(f"  Z = (ratio - μ) / σ = {z:.2f}  {'🔴 GOLD EXPENSIVE vs SILVER' if z>2 else '🟢 нейтрально' if abs(z)<1 else '🟡'}")

# ── 9. Позиция по золоту ──────────────────────────────────────────────────────
print(f"\n{'─'*60}")
print(f"  ЗОЛОТО — нетто Δ$ (XAU + XAUT)")
print(f"{'─'*60}")
gold_delta_usd = (G.get("XAU",{}).get("delta_usd",0) or 0) + (G.get("XAUT",{}).get("delta_usd",0) or 0)
gold_delta     = (G.get("XAU",{}).get("delta",0) or 0) + (G.get("XAUT",{}).get("delta",0) or 0)
print(f"  Δ$ золота:  {gold_delta_usd:>+10.2f}  ({'≈нейтраль' if abs(gold_delta_usd)<50 else '⚠ направленная'})")
print(f"  Δ шт:       {gold_delta:>+10.4f}")

# ── 10. Спот-кошелёк ─────────────────────────────────────────────────────────
print(f"\n{'─'*60}")
print(f"  СПОТ / КОШЕЛЁК")
print(f"{'─'*60}")
coins = [(c["coin"], float(c["walletBalance"]), float(c.get("usdValue",0) or 0))
         for c in (acc.get("coin") or []) if abs(float(c.get("walletBalance",0) or 0))>1e-9]
coins.sort(key=lambda x:-abs(x[2]))
for coin,qty,usd in coins:
    print(f"  {coin:<8} {qty:>14.6f}  = {usd:>10.2f} $")

print(f"\n{'='*60}\n")
