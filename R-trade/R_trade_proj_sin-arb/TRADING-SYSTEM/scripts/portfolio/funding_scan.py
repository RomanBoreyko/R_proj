import sys, json, urllib.request, urllib.parse
sys.stdout.reconfigure(encoding="utf-8")
BASE="https://api.bybit.com"
def pub(path, params):
    qs=urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{BASE}{path}?{qs}",timeout=30) as r: return json.loads(r.read())
def f(x):
    try:return float(x)
    except:return 0.0

# 1. интервалы фандинга
intervals={}
cursor=""
for _ in range(10):
    p={"category":"linear","limit":"1000"}
    if cursor:p["cursor"]=cursor
    r=pub("/v5/market/instruments-info",p)
    for i in r["result"]["list"]:
        intervals[i["symbol"]]=int(i.get("fundingInterval",480))
    cursor=r["result"].get("nextPageCursor","")
    if not cursor:break

# 2. тикеры перпов
perps=pub("/v5/market/tickers",{"category":"linear"})["result"]["list"]
# 3. споты (для хеджа)
spots={t["symbol"] for t in pub("/v5/market/tickers",{"category":"spot"})["result"]["list"]}

rows=[]
for t in perps:
    s=t["symbol"]
    if not s.endswith("USDT"): continue
    fr=f(t.get("fundingRate"))
    iv=intervals.get(s,480)
    per_year=fr*(365*24*60/iv)*100   # % годовых
    turn=f(t.get("turnover24h"))
    last=f(t.get("lastPrice"))
    has_spot=s in spots
    # базис спот-перп
    basis=None
    rows.append({"s":s,"fr":fr*100,"iv":iv,"apy":per_year,"turn":turn,"last":last,"spot":has_spot})

# базис только для ликвидных со спотом
spot_px={t["symbol"]:f(t["lastPrice"]) for t in pub("/v5/market/tickers",{"category":"spot"})["result"]["list"]}
for r in rows:
    if r["spot"] and spot_px.get(r["s"],0)>0:
        r["basis"]=(r["last"]/spot_px[r["s"]]-1)*100

LIQ=5_000_000  # мин оборот 24ч $5млн
liquid=[r for r in rows if r["turn"]>=LIQ]

print(f"\n{'='*78}\n  КАРРИ-СКАН Bybit  ·  перпов {len(rows)}, ликвидных(>{LIQ/1e6:.0f}M) {len(liquid)}\n{'='*78}")

# ── A. Высокий ПОЛОЖИТЕЛЬНЫЙ фандинг (short perp собирает) ──
print(f"\n  ── A. ШОРТ-КАРРИ: фандинг+ высокий → short perp собирает ──")
print(f"  {'Символ':<16}{'фандинг':>9}{'инт':>5}{'%год':>9}{'оборот':>9}{'спот?':>7}{'базис%':>8}")
for r in sorted(liquid,key=lambda x:-x["apy"])[:10]:
    b=f"{r.get('basis'):+.2f}" if r.get("basis") is not None else "—"
    sp="✓спот" if r["spot"] else "перп-only"
    print(f"  {r['s']:<16}{r['fr']:>+8.4f}%{r['iv']//60:>4}h{r['apy']:>+8.1f}%{r['turn']/1e6:>7.0f}M{sp:>9}{b:>8}")

# ── B. Высокий ОТРИЦАТЕЛЬНЫЙ фандинг (long perp собирает) ──
print(f"\n  ── B. ЛОНГ-КАРРИ: фандинг− сильный → long perp собирает ──")
print(f"  {'Символ':<16}{'фандинг':>9}{'инт':>5}{'%год':>9}{'оборот':>9}{'спот?':>7}{'базис%':>8}")
for r in sorted(liquid,key=lambda x:x["apy"])[:10]:
    b=f"{r.get('basis'):+.2f}" if r.get("basis") is not None else "—"
    sp="✓спот" if r["spot"] else "перп-only"
    print(f"  {r['s']:<16}{r['fr']:>+8.4f}%{r['iv']//60:>4}h{r['apy']:>+8.1f}%{r['turn']/1e6:>7.0f}M{sp:>9}{b:>8}")

# ── C. Дельта-нейтральные кандидаты: фандинг + спот для хеджа ──
print(f"\n  ── C. ★ СОБРАТЬ СЕГОДНЯ: |фандинг| высокий И есть спот (хедж) ──")
hedgeable=[r for r in liquid if r["spot"] and abs(r["apy"])>15]
for r in sorted(hedgeable,key=lambda x:-abs(x["apy"]))[:10]:
    side="short perp + long spot" if r["apy"]>0 else "long perp + short spot"
    print(f"  {r['s']:<14} {r['apy']:>+7.1f}%/год  базис{r.get('basis',0):+.2f}%  → {side}")
if not hedgeable:
    print("  (нет ликвидных со спотом и |фандинг|>15% — рынок спокоен)")
print()
