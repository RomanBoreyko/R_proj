"""Скринер C6: синтетический фьюч из опционов (паритет) vs перп.
Для каждого актива с опционами: ATM-синтетика по сериям, исполняемые ширины,
косты, фандинг. Ранжирует где базис собираем. Запуск: python synth_screener.py [--csv]
"""
import sys, json, time, urllib.request, urllib.parse
from collections import defaultdict
sys.stdout.reconfigure(encoding="utf-8")
B="https://api.bybit.com"
def pub(p,par):
    qs=urllib.parse.urlencode(par)
    with urllib.request.urlopen(f"{B}{p}?{qs}",timeout=25) as r: return json.loads(r.read())
def f(x):
    try:return float(x)
    except:return 0.0
MONTHS={"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
def dte(e):
    d=int(e[:-5]);m=MONTHS[e[-5:-2]];y=2000+int(e[-2:])
    return (time.mktime((y,m,d,11,0,0,0,0,-1))-time.time())/86400

ASSETS={"BTC":"BTCUSDT","ETH":"ETHUSDT","SOL":"SOLUSDT","XRP":"XRPUSDT",
        "DOGE":"DOGEUSDT","MNT":"MNTUSDT","XAUT":"XAUTUSDT"}
OPT_FEE=0.0002  # 0.02% от ноционала за ногу (такер опц.)
PERP_FEE=0.0002 # мейкер перп

rows=[]
for base,psym in ASSETS.items():
    try:
        pt=pub("/v5/market/tickers",{"category":"linear","symbol":psym})["result"]["list"][0]
        perp=f(pt["lastPrice"]); fund=f(pt["fundingRate"])*100
        opts=pub("/v5/market/tickers",{"category":"option","baseCoin":base})["result"]["list"]
        chains=defaultdict(dict)
        for t in opts:
            pr=t["symbol"].split("-")
            if len(pr)<4:continue
            chains[(pr[1],f(pr[2]))][pr[3]]=t
        # для каждой серии берём ATM-страйк
        by_exp={}
        for (exp,K),legs in chains.items():
            if "C" not in legs or "P" not in legs:continue
            d=dte(exp)
            if d<0.1 or d>60:continue
            cur=by_exp.get(exp)
            if cur is None or abs(K-perp)<abs(cur[0]-perp): by_exp[exp]=(K,legs,d)
        for exp,(K,legs,d) in sorted(by_exp.items(),key=lambda kv:kv[1][2]):
            C,P=legs["C"],legs["P"]
            cb,ca,pb,pa=f(C["bid1Price"]),f(C["ask1Price"]),f(P["bid1Price"]),f(P["ask1Price"])
            if not(cb and ca and pb and pa):continue
            synth_mid=K+(cb+ca)/2-(pb+pa)/2
            basis=(perp/synth_mid-1)*100
            # исполняемые ширины (за вычетом костов 3 ног)
            costs=(OPT_FEE*2+PERP_FEE)*perp
            conv=(perp - (K+cb-pa) - costs)/perp*100   # sell perp? нет: conv= perp_bid - synth_ask
            rev =((K+ca-pb) - perp - costs)/perp*100*-1
            sprC=(ca-cb)/perp*100; sprP=(pa-pb)/perp*100
            rows.append(dict(base=base,exp=exp,dte=d,K=K,perp=perp,synth=synth_mid,
                basis=basis,conv=conv,sprC=sprC,sprP=sprP,fund=fund))
    except Exception as e:
        print(f"{base}: {e}",file=sys.stderr)

print(f"\n{'='*86}\n  СКРИНЕР СИНТ-ФЬЮЧ(опционы) vs ПЕРП — {time.strftime('%Y-%m-%d %H:%M')}\n{'='*86}")
print(f"  базис% = перп/синтетика−1 · |базис| > ширин стаканов = собираемо")
print(f"  {'актив':<6}{'серия':<9}{'DTE':>5}{'ATM K':>9}{'синт-mid':>10}{'перп':>10}{'базис%':>8}{'шир.C%':>7}{'шир.P%':>7}{'фанд%':>8}")
flag=[]
for r in sorted(rows,key=lambda x:(x['base'],x['dte'])):
    mark=""
    if abs(r['basis'])>r['sprC']+r['sprP']:
        mark=" ◄"; flag.append(r)
    print(f"  {r['base']:<6}{r['exp']:<9}{r['dte']:>4.1f}д{r['K']:>9,.0f}{r['synth']:>10,.2f}{r['perp']:>10,.2f}{r['basis']:>+8.3f}{r['sprC']:>7.2f}{r['sprP']:>7.2f}{r['fund']:>+8.4f}{mark}")
if flag:
    print(f"\n  ★ Базис шире суммы стаканов (кандидаты):")
    for r in sorted(flag,key=lambda x:-abs(x['basis'])):
        side="long synth + short perp" if r['basis']>0 else "short synth + long perp"
        print(f"    {r['base']} {r['exp']} K={r['K']:,.0f}: базис {r['basis']:+.3f}% → {side}")
else:
    print(f"\n  Паритет держится везде — собираемых разрывов нет (норма для спокойного рынка)")
