import sys, json, time, urllib.request, urllib.parse
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

def scan(base_coin, perp_symbol):
    r = pub("/v5/market/tickers", {"category":"option","baseCoin":base_coin})
    ticks = r["result"]["list"]
    perp = f(pub("/v5/market/tickers", {"category":"linear","symbol":perp_symbol})["result"]["list"][0]["lastPrice"])

    # группируем по (expiry, strike)
    chains = defaultdict(dict)
    for t in ticks:
        parts = t["symbol"].split("-")  # ETH-19JUN26-1650-C-USDT или без USDT
        if len(parts) < 4: continue
        exp, strike, typ = parts[1], f(parts[2]), parts[3]
        if typ not in ("C","P"): continue
        chains[(exp, strike)][typ] = t

    print(f"\n{'='*78}")
    print(f"  {base_coin}  —  перп {perp:,.2f}   ({time.strftime('%H:%M:%S')})")
    print(f"{'='*78}")
    print(f"  Паритет: C − P = F − K  (F = underlyingPrice серии)")
    print(f"  conv$ = C_bid − P_ask − (F−K)   |   rev$ = (F−K) − C_ask + P_bid")
    print(f"  (исполняемые цены; >0 = можно собрать; косты ещё не вычтены)")

    rows = []
    for (exp, K), legs in chains.items():
        if "C" not in legs or "P" not in legs: continue
        C, P = legs["C"], legs["P"]
        cb, ca = f(C["bid1Price"]), f(C["ask1Price"])
        pb, pa = f(P["bid1Price"]), f(P["ask1Price"])
        F = f(C.get("underlyingPrice")) or perp
        if F == 0: continue
        fk = F - K
        conv = (cb - pa - fk) if (cb > 0 and pa > 0) else None   # sell C, buy P, buy base
        rev  = (fk - ca + pb) if (ca > 0 and pb > 0) else None   # buy C, sell P, sell base
        moneyness = abs(K - F) / F
        if moneyness > 0.25: continue  # дальние крылья с пустыми стаканами не интересны
        rows.append((exp, K, F, cb, ca, pb, pa, conv, rev))

    # сортировка: по экспирации, потом страйк
    def expkey(e):
        months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
        d, m, y = int(e[:-5]), e[-5:-2], int(e[-2:])
        return (y, months.get(m,0), d)
    rows.sort(key=lambda r:(expkey(r[0]), r[1]))

    best = []
    cur_exp = None
    for exp, K, F, cb, ca, pb, pa, conv, rev in rows:
        if exp != cur_exp:
            print(f"\n  ── {exp}  (F={F:,.2f}) " + "─"*40)
            print(f"  {'K':>8} {'C bid/ask':>17} {'P bid/ask':>17} {'conv$':>8} {'rev$':>8}")
            cur_exp = exp
        cs = f"{cb:,.1f}/{ca:,.1f}" if (cb or ca) else "—"
        ps = f"{pb:,.1f}/{pa:,.1f}" if (pb or pa) else "—"
        cv = f"{conv:+.2f}" if conv is not None else "  —"
        rv = f"{rev:+.2f}" if rev is not None else "  —"
        mark = ""
        if conv is not None and conv > 0: mark += " ◄CONV"; best.append((exp,K,"CONV",conv))
        if rev  is not None and rev  > 0: mark += " ◄REV";  best.append((exp,K,"REV",rev))
        print(f"  {K:>8,.0f} {cs:>17} {ps:>17} {cv:>8} {rv:>8}{mark}")

    if best:
        print(f"\n  ★ Кандидаты (до костов):")
        for exp,K,side,v in sorted(best, key=lambda x:-x[3])[:8]:
            print(f"    {side:<5} {exp} K={K:,.0f}  +${v:.2f}")
    else:
        print(f"\n  Чистых разрывов нет — паритет держится (или стаканы пустые).")
    return best

PERPMAP={"ETH":"ETHUSDT","XAUT":"XAUTUSDT","BTC":"BTCUSDT","SOL":"SOLUSDT","MNT":"MNTUSDT","XRP":"XRPUSDT"}
args=[a.upper() for a in sys.argv[1:]] or ["ETH","XAUT"]
for bc in args:
    ps=PERPMAP.get(bc, bc+"USDT")
    try: scan(bc, ps)
    except Exception as e: print(f"  {bc}: ошибка {e}")
print()
