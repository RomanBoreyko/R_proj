"""БАЗИС-ДЕСК: три типа спреда × корзина активов.
  A. Спот/Перп   (cash-and-carry)
  B. Перп/Календарь  (перп vs датированный фьюч)
  C. Перп/Синтетика  (перп vs K+C−P из ATM опционов, паритет пут-колл)
Запуск: python basis_desk.py
"""
import sys, json, time, urllib.request, urllib.parse
from collections import defaultdict
sys.stdout.reconfigure(encoding="utf-8")
B="https://api.bybit.com"
def pub(p,par):
    qs=urllib.parse.urlencode(par)
    with urllib.request.urlopen(f"{B}{p}?{qs}",timeout=30) as r: return json.loads(r.read())
def f(x):
    try:return float(x)
    except:return 0.0
MONTHS={"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
def dte(e):
    try:
        d=int(e[:-5]);m=MONTHS[e[-5:-2]];y=2000+int(e[-2:])
        return (time.mktime((y,m,d,11,0,0,0,0,-1))-time.time())/86400
    except: return 0

ASSETS=["BTC","ETH","SOL","MNT","DOGE","XAUT"]

# тянем всё разом
linear={t["symbol"]:t for t in pub("/v5/market/tickers",{"category":"linear"})["result"]["list"]}
spot  ={t["symbol"]:t for t in pub("/v5/market/tickers",{"category":"spot"})["result"]["list"]}
ivmap={}
c=""
for _ in range(8):
    pr={"category":"linear","limit":"1000"}
    if c:pr["cursor"]=c
    r=pub("/v5/market/instruments-info",pr)
    for i in r["result"]["list"]: ivmap[i["symbol"]]=int(i.get("fundingInterval",480))
    c=r["result"].get("nextPageCursor","");
    if not c:break

now=time.strftime("%Y-%m-%d %H:%M")
print(f"\n{'='*82}\n  БАЗИС-ДЕСК — {now}   (корзина: {' '.join(ASSETS)})\n{'='*82}")

# ── A. Спот/Перп ─────────────────────────────────────────────
print(f"\n  A. СПОТ/ПЕРП  (cash-and-carry: long spot + short perp если базис+)")
print(f"  {'актив':<7}{'перп':>11}{'спот':>11}{'базис%':>9}{'фандинг':>10}{'%год':>8}{'спот об':>9}")
for a in ASSETS:
    ps=a+"USDT"
    if ps not in linear or ps not in spot: print(f"  {a:<7} нет пары"); continue
    pp=f(linear[ps]["lastPrice"]); sp=f(spot[ps]["lastPrice"])
    if sp<=0: continue
    basis=(pp/sp-1)*100
    fr=f(linear[ps].get("fundingRate"))*100
    apy=fr*(365*24*60/ivmap.get(ps,480))*100
    so=f(spot[ps].get("turnover24h"))/1e6
    print(f"  {a:<7}{pp:>11,.4f}{sp:>11,.4f}{basis:>+8.3f}%{fr:>+9.4f}%{apy:>+7.0f}%{so:>7.0f}M")

# ── B. Перп/Календарь ────────────────────────────────────────
print(f"\n  B. ПЕРП/КАЛЕНДАРЬ  (датированный фьюч vs перп; базис+ = контанго)")
print(f"  {'актив':<7}{'фьюч':>16}{'DTE':>6}{'перп':>11}{'фьюч цена':>11}{'базис%':>9}{'%год':>8}")
any_cal=False
for a in ASSETS:
    ps=a+"USDT"; pp=f(linear.get(ps,{}).get("lastPrice"))
    futs=[(s,t) for s,t in linear.items() if s.startswith(a+"USDT-") and f(t.get("turnover24h"))>=0]
    futs=[(s,t,dte(s.split("-")[1])) for s,t in futs if "-" in s]
    futs=[x for x in futs if 0<x[2]<400]
    futs.sort(key=lambda x:x[2])
    if not futs or pp<=0: continue
    for s,t,d in futs[:3]:
        any_cal=True
        fpx=f(t["lastPrice"])
        basis=(fpx/pp-1)*100
        apy=basis*365/d if d else 0
        print(f"  {a:<7}{s:>16}{d:>5.0f}д{pp:>11,.2f}{fpx:>11,.2f}{basis:>+8.3f}%{apy:>+7.1f}%")
if not any_cal: print("  (датированных фьючей у корзины нет — только XAUT/BTC/ETH обычно)")

# ── C. Перп/Синтетика (паритет) ──────────────────────────────
print(f"\n  C. ПЕРП/СИНТЕТИКА  (синт=K+C−P из ATM опц.; |базис|>ширин = собираемо)")
print(f"  {'актив':<7}{'серия':<9}{'DTE':>5}{'ATM K':>9}{'синт':>11}{'перп':>11}{'базис%':>8}{'шир%':>7}")
for a in ASSETS:
    ps=a+"USDT"; pp=f(linear.get(ps,{}).get("lastPrice"))
    if pp<=0: continue
    try:
        opts=pub("/v5/market/tickers",{"category":"option","baseCoin":a})["result"]["list"]
    except: continue
    chains=defaultdict(dict)
    for t in opts:
        pr=t["symbol"].split("-")
        if len(pr)>=4: chains[(pr[1],f(pr[2]))][pr[3]]=t
    by_exp={}
    for (exp,K),legs in chains.items():
        if "C" not in legs or "P" not in legs: continue
        d=dte(exp)
        if d<0.1 or d>45: continue
        cur=by_exp.get(exp)
        if cur is None or abs(K-pp)<abs(cur[0]-pp): by_exp[exp]=(K,legs,d)
    rows=sorted(by_exp.items(),key=lambda kv:kv[1][2])[:2]
    for exp,(K,legs,d) in rows:
        C,P=legs["C"],legs["P"]
        cb,ca,pb,pa=f(C["bid1Price"]),f(C["ask1Price"]),f(P["bid1Price"]),f(P["ask1Price"])
        if not(cb and ca and pb and pa): continue
        synth=K+(cb+ca)/2-(pb+pa)/2
        basis=(pp/synth-1)*100
        wid=((ca-cb)+(pa-pb))/pp*100
        mark=" ◄" if abs(basis)>wid else ""
        print(f"  {a:<7}{exp:<9}{d:>4.1f}д{K:>9,.0f}{synth:>11,.2f}{pp:>11,.2f}{basis:>+7.3f}%{wid:>6.2f}{mark}")
print()
print("  Связано: карточка скрининга (03-STRATEGIES/spread-screening-card)")
