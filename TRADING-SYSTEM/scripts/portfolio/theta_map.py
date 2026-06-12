import sys, json, time, urllib.request, urllib.parse, hmac, hashlib
from collections import defaultdict
sys.stdout.reconfigure(encoding="utf-8")

CREDS=r"C:\Users\DoOs\.bybit\credentials.json"
BASE="https://api.bybit.com"
with open(CREDS,encoding="utf-8") as f: d=json.load(f)
KEY,SECRET=d["api_key"],d["api_secret"]
def sign(ts,recv,qs): return hmac.new(SECRET.encode(),f"{ts}{KEY}{recv}{qs}".encode(),hashlib.sha256).hexdigest()
def get(path,params):
    ts=str(int(time.time()*1000));recv="20000";qs=urllib.parse.urlencode(params)
    req=urllib.request.Request(f"{BASE}{path}?{qs}",headers={"X-BAPI-API-KEY":KEY,"X-BAPI-TIMESTAMP":ts,"X-BAPI-SIGN":sign(ts,recv,qs),"X-BAPI-RECV-WINDOW":recv})
    with urllib.request.urlopen(req,timeout=15) as r: return json.loads(r.read())
def f(x):
    try:return float(x)
    except:return 0.0

MONTHS={"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
def dte(e):
    dd=int(e[:-5]);m=MONTHS[e[-5:-2]];y=2000+int(e[-2:])
    return (time.mktime((y,m,dd,8,0,0,0,0,-1))-time.time())/86400

opts=[];cursor=""
for _ in range(5):
    p={"category":"option","limit":"200"}
    if cursor:p["cursor"]=cursor
    r=get("/v5/position/list",p);opts+=r["result"]["list"];cursor=r["result"].get("nextPageCursor","")
    if not cursor:break
legs=[]
for o in opts:
    if f(o["size"])==0:continue
    pr=o["symbol"].split("-")
    if len(pr)<4:continue
    legs.append({"base":pr[0],"exp":pr[1],"K":f(pr[2]),"typ":pr[3],
        "side":o["side"],"sz":f(o["size"]),"th":f(o.get("theta",0) or 0),
        "vega":f(o.get("vega",0) or 0),"dte":dte(pr[1])})

def bucket(d):
    return "ФРОНТ ≤2д" if d<=2 else "МИД 3-20д" if d<=20 else "БЭК >20д"

# группировка
by_bucket=defaultdict(lambda:{"pay":0,"collect":0,"vega":0})
by_asset=defaultdict(lambda:{"th":0,"vega":0})
covered_check=defaultdict(lambda:{"long_th":0,"short_th":0,"long_v":0,"short_v":0})
for l in legs:
    b=bucket(l["dte"]); a=l["base"]
    if l["th"]<0: by_bucket[b]["pay"]+=l["th"]
    else: by_bucket[b]["collect"]+=l["th"]
    by_bucket[b]["vega"]+=l["vega"]
    by_asset[a]["th"]+=l["th"]; by_asset[a]["vega"]+=l["vega"]
    if l["side"]=="Buy": covered_check[a]["long_th"]+=l["th"]; covered_check[a]["long_v"]+=l["vega"]
    else: covered_check[a]["short_th"]+=l["th"]; covered_check[a]["short_v"]+=l["vega"]

tot_pay=sum(l["th"] for l in legs if l["th"]<0)
tot_col=sum(l["th"] for l in legs if l["th"]>0)
tot=tot_pay+tot_col
tot_v=sum(l["vega"] for l in legs)

print(f"\n{'='*64}\n  РАЗБОР ТЕТЫ — {time.strftime('%Y-%m-%d %H:%M')}\n{'='*64}")
print(f"  Платим (купленные):   {tot_pay:>8.2f} /день")
print(f"  Собираем (проданные): {tot_col:>8.2f} /день")
print(f"  {'─'*40}")
print(f"  НЕТТО ТЕТА:           {tot:>8.2f} /день")
print(f"  Покрытие: собранное гасит {tot_col/-tot_pay*100:.0f}% уплаченного")
print(f"  Нетто вега: {tot_v:+.2f}  ({'ШОРТ волы' if tot_v<0 else 'лонг волы'})")

print(f"\n  ── По горизонту (где живёт тета) ──")
print(f"  {'Бакет':<12} {'платим':>9} {'собираем':>9} {'нетто Θ':>9} {'вега':>8}")
for b in ["ФРОНТ ≤2д","МИД 3-20д","БЭК >20д"]:
    v=by_bucket[b]; print(f"  {b:<12} {v['pay']:>9.2f} {v['collect']:>9.2f} {v['pay']+v['collect']:>9.2f} {v['vega']:>8.2f}")

print(f"\n  ── По активу: тета покрыта или голая? ──")
print(f"  {'Актив':<7} {'Θ нетто':>8} {'куплено Θ':>10} {'продано Θ':>10} {'покрытие':>10}")
for a,v in sorted(by_asset.items(),key=lambda x:x[1]['th']):
    c=covered_check[a]
    cov = (c['short_th']/-c['long_th']*100) if c['long_th']<0 else (999 if c['short_th']>0 else 0)
    tag = "голый шорт-вола" if c['long_th']>-0.05 and c['short_th']>0.05 else f"{cov:.0f}% покрыто" if c['long_th']<0 else "—"
    print(f"  {a:<7} {v['th']:>8.2f} {c['long_th']:>10.2f} {c['short_th']:>10.2f}   {tag}")
print()
