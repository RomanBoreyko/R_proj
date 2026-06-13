"""Коллектор C6: пишет ряд синт-фьюч(ATM опционы) vs перп в CSV.
Каждые 60с: ETH/BTC/SOL/XAUT, ближайшие 2 серии. Файл: data/parity_log.csv
Запуск: python parity_log.py  (Ctrl+C для остановки; гонять фоном пока ноут включён)
"""
import sys, json, time, csv, os, urllib.request, urllib.parse
from collections import defaultdict
sys.stdout.reconfigure(encoding="utf-8")
B="https://api.bybit.com"
def pub(p,par):
    qs=urllib.parse.urlencode(par)
    with urllib.request.urlopen(f"{B}{p}?{qs}",timeout=20) as r: return json.loads(r.read())
def f(x):
    try:return float(x)
    except:return 0.0
MONTHS={"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
def dte(e):
    d=int(e[:-5]);m=MONTHS[e[-5:-2]];y=2000+int(e[-2:])
    return (time.mktime((y,m,d,11,0,0,0,0,-1))-time.time())/86400
ASSETS={"ETH":"ETHUSDT","BTC":"BTCUSDT","SOL":"SOLUSDT","XAUT":"XAUTUSDT"}
OUT=os.path.join(os.path.dirname(os.path.abspath(__file__)),"data")
os.makedirs(OUT,exist_ok=True)
PATH=os.path.join(OUT,"parity_log.csv")
new=not os.path.exists(PATH)
fh=open(PATH,"a",newline="",encoding="utf-8")
w=csv.writer(fh)
if new: w.writerow(["ts","asset","exp","dte_d","K","perp","synth_mid","basis_pct","conv_exec","rev_exec","funding_pct"])
print(f"Пишу в {PATH} каждые 60с. Ctrl+C для остановки.")
while True:
    try:
        ts=time.strftime("%Y-%m-%d %H:%M:%S")
        for base,psym in ASSETS.items():
            pt=pub("/v5/market/tickers",{"category":"linear","symbol":psym})["result"]["list"][0]
            perp=f(pt["lastPrice"]); fund=f(pt["fundingRate"])*100
            opts=pub("/v5/market/tickers",{"category":"option","baseCoin":base})["result"]["list"]
            chains=defaultdict(dict)
            for t in opts:
                pr=t["symbol"].split("-")
                if len(pr)>=4: chains[(pr[1],f(pr[2]))][pr[3]]=t
            by_exp={}
            for (exp,K),legs in chains.items():
                if "C" not in legs or "P" not in legs: continue
                d=dte(exp)
                if d<0.05 or d>35: continue
                cur=by_exp.get(exp)
                if cur is None or abs(K-perp)<abs(cur[0]-perp): by_exp[exp]=(K,legs,d)
            for exp,(K,legs,d) in sorted(by_exp.items(),key=lambda kv:kv[1][2])[:2]:
                C,P=legs["C"],legs["P"]
                cb,ca,pb,pa=f(C["bid1Price"]),f(C["ask1Price"]),f(P["bid1Price"]),f(P["ask1Price"])
                if not(cb and ca and pb and pa): continue
                synth=K+(cb+ca)/2-(pb+pa)/2
                basis=(perp/synth-1)*100
                conv=(perp-(K+cb-pa))/perp*100
                rev=((K+ca-pb)-perp)/perp*100
                w.writerow([ts,base,exp,round(d,2),K,perp,round(synth,4),round(basis,4),round(conv,4),round(rev,4),round(fund,5)])
        fh.flush()
        print(f"{ts} ✓",end="\r")
        time.sleep(60)
    except KeyboardInterrupt:
        break
    except Exception as e:
        print(f"\n{e}; retry 60s"); time.sleep(60)
fh.close()
