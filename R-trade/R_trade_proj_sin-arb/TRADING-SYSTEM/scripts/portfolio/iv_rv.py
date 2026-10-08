import sys, json, math, time, urllib.request, urllib.parse
from collections import defaultdict
sys.stdout.reconfigure(encoding="utf-8")

BASE = "https://api.bybit.com"
def pub(path, params):
    qs = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{BASE}{path}?{qs}", timeout=20) as r:
        return json.loads(r.read())
def f(x):
    try: return float(x)
    except: return 0.0

MONTHS={"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
def exp_ts(e):  # '13JUN26' -> unix
    d=int(e[:-5]); m=MONTHS[e[-5:-2]]; y=2000+int(e[-2:])
    return time.mktime((y,m,d,11,0,0,0,0,-1))  # ~11:00 UTC+3 ≈ 08:00 UTC сеттл

def rv(symbol, interval, limit, per_year):
    r = pub("/v5/market/kline", {"category":"linear","symbol":symbol,"interval":interval,"limit":str(limit)})
    closes = [f(k[4]) for k in r["result"]["list"]][::-1]
    if len(closes) < 20: return None
    rets = [math.log(closes[i]/closes[i-1]) for i in range(1,len(closes)) if closes[i-1]>0]
    mu = sum(rets)/len(rets)
    var = sum((x-mu)**2 for x in rets)/(len(rets)-1)
    return math.sqrt(var*per_year)*100

def iv_of(base):
    r = pub("/v5/market/tickers", {"category":"option","baseCoin":base})
    now = time.time()
    by_exp = defaultdict(list)
    for t in r["result"]["list"]:
        parts = t["symbol"].split("-")
        if len(parts) < 4: continue
        exp, K = parts[1], f(parts[2])
        iv = f(t.get("markIv"))
        U  = f(t.get("underlyingPrice"))
        if iv <= 0 or U <= 0: continue
        if iv < 5: iv *= 100  # decimal → %
        dte = (exp_ts(exp)-now)/86400
        if dte < 0.2: continue
        by_exp[exp].append((abs(K-U)/U, K, iv, dte, U))
    out = []
    for exp, lst in by_exp.items():
        lst.sort()
        atm = lst[:4]  # 4 ближайших к деньгам котировки (C и P)
        iv_atm = sum(x[2] for x in atm)/len(atm)
        out.append((atm[0][3], exp, iv_atm, atm[0][4]))
    out.sort()
    return out

ASSETS = [("BTC","BTCUSDT"),("ETH","ETHUSDT"),("SOL","SOLUSDT"),("MNT","MNTUSDT"),("XAUT","XAUTUSDT")]

print(f"\n{'='*86}")
print(f"  IV vs RV — {time.strftime('%Y-%m-%d %H:%M')}   (ATM mark-IV по сериям, RV аннуализир.)")
print(f"{'='*86}")
print(f"  {'':6} {'RV 24ч':>7} {'RV 7д':>7} │ {'фронт':>16} {'IV':>6} │ {'середина':>16} {'IV':>6} │ {'бэк':>14} {'IV':>6}")
print(f"  {'─'*84}")

for base, sym in ASSETS:
    try:
        rv1 = rv(sym, "5", 288, 288*365)
        rv7 = rv(sym, "60", 168, 24*365)
        series = iv_of(base)
        if not series:
            print(f"  {base:<6} опционов нет"); continue
        front = series[0]
        mid   = next((s for s in series if 5 <= s[0] <= 21), None)
        back  = next((s for s in series if s[0] >= 40), series[-1])
        def cell(s): return (f"{s[1]:>9} {s[0]:>4.1f}д", f"{s[2]:>5.1f}%") if s else ("    —    ","   — ")
        fr, fiv = cell(front); md, miv = cell(mid); bk, biv = cell(back)
        print(f"  {base:<6} {rv1:>6.1f}% {rv7:>6.1f}% │ {fr:>16} {fiv:>6} │ {md:>16} {miv:>6} │ {bk:>14} {biv:>6}")
        # вердикты
        verdict = []
        if front and rv7:
            d = front[2]-rv7
            verdict.append(f"фронт {'ДЁШЕВ к RV7д ✅ покупка центра ок' if d<-3 else 'ДОРОГ к RV7д ❌ гамма переплачена' if d>3 else '≈ RV — нейтрально'} ({d:+.0f}пп)")
        if back and rv7:
            d = back[2]-rv7
            verdict.append(f"бэк {'дорог к RV ✅ продажа краёв оправдана' if d>3 else 'НЕ дорог ❌ края продавать не за что' if d<-1 else '≈ RV'} ({d:+.0f}пп)")
        for v in verdict: print(f"         └ {v}")
    except Exception as e:
        print(f"  {base:<6} ошибка: {e}")
print()
print("  Терм-структура: фронт<бэк = контанго волы (норма) · фронт>бэк = бэквордация (стресс)")
