import sys, json, math, time, urllib.request, urllib.parse, hmac, hashlib
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
def pub(path,params):
    qs=urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{BASE}{path}?{qs}",timeout=15) as r: return json.loads(r.read())
def f(x):
    try:return float(x)
    except:return 0.0

MONTHS={"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
def exp_ts(e):
    dd=int(e[:-5]);m=MONTHS[e[-5:-2]];y=2000+int(e[-2:])
    return time.mktime((y,m,dd,8,0,0,0,0,-1))  # ~08:00 UTC settle

def N(x): return 0.5*(1+math.erf(x/math.sqrt(2)))
def bs(S,K,T,iv,typ):
    if T<=0: return max(0,S-K) if typ=="C" else max(0,K-S)
    if iv<=0: iv=0.01
    sig=iv*math.sqrt(T); d1=(math.log(S/K)+0.5*iv*iv*T)/sig; d2=d1-sig
    return S*N(d1)-K*N(d2) if typ=="C" else K*N(-d2)-S*N(-d1)

ASSET=sys.argv[1].upper() if len(sys.argv)>1 else "ETH"
PERP={"ETH":"ETHUSDT","BTC":"BTCUSDT","SOL":"SOLUSDT","XAUT":"XAUTUSDT","MNT":"MNTUSDT"}[ASSET]
S0=f(pub("/v5/market/tickers",{"category":"linear","symbol":PERP})["result"]["list"][0]["lastPrice"])

# markIv по тикерам
iv_map={}
for t in pub("/v5/market/tickers",{"category":"option","baseCoin":ASSET})["result"]["list"]:
    iv_map[t["symbol"].replace("-USDT","")]=f(t.get("markIv"))

# позиции
opts=[];cursor=""
for _ in range(5):
    p={"category":"option","limit":"200"};
    if cursor:p["cursor"]=cursor
    r=get("/v5/position/list",p);opts+=r["result"]["list"];cursor=r["result"].get("nextPageCursor","")
    if not cursor:break
legs=[]
for o in opts:
    if f(o["size"])==0:continue
    parts=o["symbol"].split("-")
    if parts[0]!=ASSET or len(parts)<4:continue
    legs.append({"k":"opt","exp":parts[1],"K":f(parts[2]),"typ":parts[3],
                 "sz":f(o["size"])*(1 if o["side"]=="Buy" else -1),
                 "iv":(iv_map.get(o["symbol"].replace("-USDT",""),0) or f(o.get("markPrice"))*0),
                 "mark":f(o["markPrice"])})
# перпы/фьючи
for cat,settle in [("linear","USDT")]:
    pl=get("/v5/position/list",{"category":"linear","settleCoin":"USDT","limit":"200"})["result"]["list"]
    for p in pl:
        if f(p["size"])==0:continue
        s=p["symbol"]
        if not s.startswith(ASSET):continue
        if s==PERP:
            legs.append({"k":"perp","sz":f(p["size"])*(1 if p["side"]=="Buy" else -1),"avg":f(p["avgPrice"])})
        elif "-" in s:  # dated future
            legs.append({"k":"fut","sz":f(p["size"])*(1 if p["side"]=="Buy" else -1),"avg":f(p["avgPrice"])})

now=time.time()
near=min((exp_ts(l["exp"]) for l in legs if l["k"]=="opt"),default=now+86400)
T_near=max((near-now)/(365*86400),0)

def book_value(S,t_off):
    """стоимость книги при цене S; t_off=0 сейчас, t_off=T_near на ближней экспирации"""
    v=0
    for l in legs:
        if l["k"]=="opt":
            T=max((exp_ts(l["exp"])-now)/(365*86400)-t_off,0)
            v+=l["sz"]*bs(S,l["K"],T,l["iv"] or 0.5,l["typ"])
        elif l["k"] in("perp","fut"):
            v+=l["sz"]*(S-l["avg"])
    return v

V_now0=book_value(S0,0)
grid=[S0*(0.85+0.30*i/60) for i in range(61)]
rows=[]
for S in grid:
    pnl_now=book_value(S,0)-V_now0
    pnl_exp=book_value(S,T_near)-V_now0
    rows.append((S,pnl_now,pnl_exp))

# греки нетто (числено)
h=S0*0.01
g_now=lambda S:book_value(S,0)
delta=(g_now(S0+h)-g_now(S0-h))/(2*h)
gamma=(g_now(S0+h)-2*g_now(S0)+g_now(S0-h))/(h*h)
theta=(book_value(S0,1/365)-book_value(S0,0))  # за 1 день

# вывод JSON для визуализации + текст
print(f"\n{'='*64}\n  PAYOFF — {ASSET}-субкнига   S₀={S0:,.2f}   ({time.strftime('%H:%M')})")
print(f"{'='*64}")
print(f"  Ног: {len(legs)}  · ближняя экспирация через {T_near*365:.1f}д")
print(f"  Нетто Δ={delta:+.3f} {ASSET} (Δ$={delta*S0:+.0f}) · Γ={gamma:+.6f} · Θ/день={theta:+.2f}")
# брейк-ивены на экспирации
be=[]
for i in range(1,len(rows)):
    a,b=rows[i-1][2],rows[i][2]
    if a==0 or (a<0)!=(b<0):
        x=rows[i-1][0]+(rows[i][0]-rows[i-1][0])*(0-a)/(b-a) if b!=a else rows[i][0]
        be.append(x)
print(f"  Брейк-ивены (на 13JUN): {', '.join(f'{x:,.0f}' for x in be) if be else '—'}")
mn=min(rows,key=lambda r:r[2]);mx=max(rows,key=lambda r:r[2])
print(f"  Худшее на экспирации: {mn[2]:+.1f}$ @ {mn[0]:,.0f}  ·  Лучшее: {mx[2]:+.1f}$ @ {mx[0]:,.0f}")
print(f"\n  {'ETH':>8} {'P&L сейчас':>12} {'P&L 13JUN':>12}  payoff(expiry)")
lo=min(r[2] for r in rows);hi=max(r[2] for r in rows);span=hi-lo or 1
for S,pn,pe in rows[::2]:
    col=int(40*(pe-lo)/span); mark="◄S₀" if abs(S-S0)<S0*0.0025 else ""
    bar=("·"*col+"●").rjust(0)
    print(f"  {S:>8,.0f} {pn:>+12.1f} {pe:>+12.1f}  {bar}{mark}")

data={"asset":ASSET,"S0":S0,"delta":delta,"gamma":gamma,"theta":theta,"be":be,
      "grid":[[round(S,1),round(pn,2),round(pe,2)] for S,pn,pe in rows]}
open(r"C:\Users\DoOs\payoff_data.json","w").write(json.dumps(data))
print(f"\n  → данные для графика: payoff_data.json")
