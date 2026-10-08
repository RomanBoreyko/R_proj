"""КАЛЕНДАРНИК vs СИНТ-ФЬЮЧ: ищем неэффективность паритета на Bybit.
Синт из опционов (K + C - P, ATM) против датированного фьюча ТОЙ ЖЕ экспирации.
Оба гаснут к одному индексу в один deliveryTime -> схождение контрактное.
Исполнимый край: rev = fut_bid - synth_ask ; con = synth_bid - fut_ask.
Если |край| > костов (2 опц-ноги + фьюч) -> собираемо.
"""
import json, time, urllib.request, urllib.parse, sys, re
from collections import defaultdict
sys.stdout.reconfigure(encoding="utf-8")
B="https://api.bybit.com"
def pub(p,par):
    qs=urllib.parse.urlencode(par)
    with urllib.request.urlopen(f"{B}{p}?{qs}",timeout=30) as r: return json.loads(r.read())
def f(x):
    try: return float(x)
    except: return 0.0
MONTHS={"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
def dte(e):
    m=re.match(r'^(\d{1,2})([A-Z]{3})(\d{2})$',e)
    if not m: return -1
    d=int(m.group(1)); mo=MONTHS.get(m.group(2),0); y=2000+int(m.group(3))
    return (time.mktime((y,mo,d,8,0,0,0,0,-1))-time.time())/86400

COINS=["BTC","ETH","SOL"]
lin={t["symbol"]:t for t in pub("/v5/market/tickers",{"category":"linear"})["result"]["list"]}

# датированные фьючи: USDC (COIN-EXPIRY) и USDT (COINUSDT-EXPIRY)
futs=defaultdict(dict)
for s,t in lin.items():
    m=re.match(r'^([A-Z]+)-(\d{1,2}[A-Z]{3}\d{2})$',s)
    if m and m.group(1) in COINS: futs[m.group(1)].setdefault(m.group(2),{})["USDC"]=t
    m2=re.match(r'^([A-Z]+)USDT-(\d{1,2}[A-Z]{3}\d{2})$',s)
    if m2 and m2.group(1) in COINS: futs[m2.group(1)].setdefault(m2.group(2),{})["USDT"]=t

now=time.strftime("%Y-%m-%d %H:%M")
print(f"\n{'='*92}\n  КАЛЕНДАРНИК vs СИНТ-ФЬЮЧ — {now}\n{'='*92}")
print("  край+ = собираемый разрыв в $ на 1 ед. (до костов). rev=long синт/short фьюч, con=наоборот")

for coin in COINS:
    fexp=futs.get(coin,{})
    if not fexp:
        print(f"\n  {coin}: датированных фьючей нет"); continue
    try:
        opts=pub("/v5/market/tickers",{"category":"option","baseCoin":coin})["result"]["list"]
    except Exception as e:
        print(f"\n  {coin}: опционы недоступны ({e})"); continue
    chains=defaultdict(dict)
    for t in opts:
        pr=t["symbol"].split("-")
        if len(pr)>=4: chains[(pr[1],f(pr[2]))][pr[3]]=t
    ref=f(lin.get(coin+"USDT",{}).get("lastPrice"))
    print(f"\n  {coin}  (ref {ref:,.2f})   общих экспираций фьюч∩опцион: ", end="")
    common=[e for e in fexp if any((e,K) in [(ee,KK) for (ee,KK) in chains] for K in [0]) or any(ee==e for (ee,KK) in chains)]
    common=sorted(set(e for (e,K) in chains if e in fexp), key=dte)
    print(", ".join(common) if common else "—")
    if not common: continue
    print(f"  {'эксп':<9}{'DTE':>5}{'ATM K':>9}{'синт-mid':>11}{'фьюч-mid':>11}{'тип':>6}{'базис%':>9}{'rev$':>9}{'con$':>9}")
    for exp in common:
        # ATM strike
        ks=[K for (e,K) in chains if e==exp and "C" in chains[(e,K)] and "P" in chains[(e,K)]]
        if not ks: continue
        K=min(ks,key=lambda x:abs(x-ref))
        legs=chains[(exp,K)]
        C,P=legs["C"],legs["P"]
        cb,ca,pb,pa=f(C["bid1Price"]),f(C["ask1Price"]),f(P["bid1Price"]),f(P["ask1Price"])
        if not(ca and pa): continue
        synth_mid=K+(cb+ca)/2-(pb+pa)/2
        synth_buy=K+ca-pb   # стоимость встать в лонг-синт
        synth_sell=K+cb-pa  # выручка за шорт-синт
        for label,ft in fexp[exp].items():
            fb,fa=f(ft["bid1Price"]),f(ft["ask1Price"])
            if not(fb and fa):
                fb=fa=f(ft["lastPrice"])
            fmid=(fb+fa)/2
            if synth_mid<=0 or fmid<=0: continue
            basis=(fmid/synth_mid-1)*100
            rev=fb-synth_buy     # long синт @ask, short фьюч @bid
            con=synth_sell-fa    # short синт @bid, long фьюч @ask
            d=dte(exp)
            print(f"  {exp:<9}{d:>4.0f}д{K:>9,.0f}{synth_mid:>11,.2f}{fmid:>11,.2f}{label:>6}{basis:>+8.3f}%{rev:>+9.2f}{con:>+9.2f}")
print()
print("  Косты-ориентир (mid->исполнение уже учтён в rev/con): сеттл-фи 2 опц + фьюч ~0.04% индекса.")
print("  Собираемо когда rev (или con) > ~0.05% * цена. Иначе паритет в норме.")
