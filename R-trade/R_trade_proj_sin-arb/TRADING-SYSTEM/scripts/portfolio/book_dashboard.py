# -*- coding: utf-8 -*-
"""Real-time дашборд книги Bybit: MM/IM, греки, бета-линия во времени (TV-стиль),
payoff, профиль греков и структурный срез пут/колл (страйк×время).
Бэкенд читает .bybit/credentials.json (read-only). Запуск: python book_dashboard.py -> http://localhost:8899
"""
import json, hmac, hashlib, time, re, datetime, math, urllib.request, urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tail_pairs, portfolio_book, arb_book, tradfi_scan, ruff_scan, capital_split, ruff_tick, income, grid_stats, pair_stats, alt_lab, desk

CREDS = r"C:\Users\DoOs\.bybit\credentials.json"; BASE = "https://api.bybit.com"
d = json.load(open(CREDS, encoding="utf-8")); KEY, SECRET = d["api_key"], d["api_secret"]
MON = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
HIST = deque(maxlen=600)   # история беты во времени
# Earn-позиции тянутся автоматически (/v5/earn/position) — read-only ключ ИХ ВИДИТ.
# Этот dict — ручной фолбэк на случай, если API вернёт пусто (монет в стейке/LP, long-дельта).
EARN = {"ETH": 0.0, "SOL": 0.0, "BTC": 0.0}
# Монеты, по которым показываем доходность Earn в дашборде (релевантны книге/карри).
EARN_WATCH = ["USDT","USDC","ETH","SOL","BTC","XAUT","MNT"]

def sget(path, params):
    ts = str(int(time.time()*1000)); recv = "20000"; qs = urllib.parse.urlencode(params)
    sig = hmac.new(SECRET.encode(), (ts+KEY+recv+qs).encode(), hashlib.sha256).hexdigest()
    req = urllib.request.Request(BASE+path+"?"+qs, headers={"X-BAPI-API-KEY":KEY,"X-BAPI-TIMESTAMP":ts,"X-BAPI-SIGN":sig,"X-BAPI-RECV-WINDOW":recv})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())

def spost(path, body):   # подписанный POST — только для чтения деталей ботов (/v5/fgridbot/detail и т.п.)
    ts = str(int(time.time()*1000)); recv = "20000"; js = json.dumps(body, separators=(",",":"))
    sig = hmac.new(SECRET.encode(), (ts+KEY+recv+js).encode(), hashlib.sha256).hexdigest()
    req = urllib.request.Request(BASE+path, data=js.encode(), method="POST", headers={"X-BAPI-API-KEY":KEY,"X-BAPI-TIMESTAMP":ts,"X-BAPI-SIGN":sig,"X-BAPI-RECV-WINDOW":recv,"Content-Type":"application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())

def pub(path, params):
    with urllib.request.urlopen(BASE+path+"?"+urllib.parse.urlencode(params), timeout=15) as r:
        return json.loads(r.read())

def ff(x):
    try: return float(x)
    except: return 0.0

def dte_of(s):
    m = re.match(r"(\d+)([A-Z]{3})(\d{2})", s or "")
    if not m: return 0.0
    try:
        exp = datetime.datetime(2000+int(m.group(3)), MON.get(m.group(2),1), int(m.group(1)), 8, 0, 0)
        return max((exp-datetime.datetime.utcnow()).total_seconds()/86400, 0.0)
    except: return 0.0

def _apr(x):
    try: return float(str(x.get("estimateApr","0")).replace("%","").strip())
    except: return 0.0

SCALP = {"step": 15.0, "band": 1.0}   # шаг сетки ($) и порог дельты (ETH) для ре-хеджа
_SCC = {"t": 0.0, "v": None}
def scalp_snapshot(ttl=6):
    """Перп-филлы ETHUSDT за сегодня: якорь (последний филл), комиссии, мейкер/тейкер, закрытый P&L."""
    if _SCC["v"] is not None and (time.time()-_SCC["t"]) < ttl:
        return _SCC["v"]
    day0 = int(datetime.datetime.combine(datetime.date.today(), datetime.time()).timestamp()*1000)
    fills, cur = [], ""
    try:
        for _ in range(5):
            q = {"category":"linear","symbol":"ETHUSDT","startTime":day0,"limit":"100"}
            if cur: q["cursor"] = cur
            r = sget("/v5/execution/list", q)["result"]
            fills += [x for x in r.get("list", []) if x.get("execType") == "Trade"]
            cur = r.get("nextPageCursor", "")
            if not cur: break
        cpnl, cur = 0.0, ""
        for _ in range(5):
            q = {"category":"linear","symbol":"ETHUSDT","startTime":day0,"limit":"100"}
            if cur: q["cursor"] = cur
            r = sget("/v5/position/closed-pnl", q)["result"]
            cpnl += sum(ff(x.get("closedPnl")) for x in r.get("list", []))
            cur = r.get("nextPageCursor", "")
            if not cur: break
    except Exception:
        out = {"ok": False}
        _SCC["t"]=time.time(); _SCC["v"]=out; return out
    fills.sort(key=lambda x: int(x["execTime"]))
    last = fills[-1] if fills else None
    out = {"ok": True, "n": len(fills),
           "maker": sum(1 for x in fills if x.get("isMaker")),
           "fees": round(sum(ff(x.get("execFee")) for x in fills), 2),
           "cpnl": round(cpnl, 2),
           "anchor": ff(last["execPrice"]) if last else None,
           "aside": last["side"] if last else "",
           "aqty": ff(last["execQty"]) if last else 0.0,
           "atime": time.strftime("%H:%M:%S", time.localtime(int(last["execTime"])/1000)) if last else ""}
    _SCC["t"]=time.time(); _SCC["v"]=out
    return out

VOLH = deque(maxlen=800)         # история ATM IV в сессии (для ранга)
_VOLC = {"t": 0.0, "v": None}
def vol_snapshot(ttl=60):
    """Датчик волы: ATM IV, реализованная вола 30д, премия, полоса. Только чтение."""
    if _VOLC["v"] is not None and (time.time()-_VOLC["t"]) < ttl:
        return _VOLC["v"]
    try:
        s0 = ff(pub("/v5/market/tickers", {"category":"linear","symbol":"ETHUSDT"})["result"]["list"][0]["lastPrice"])
        kl = pub("/v5/market/kline", {"category":"linear","symbol":"ETHUSDT","interval":"D","limit":"31"})["result"]["list"]
        cl = [ff(c[4]) for c in kl][::-1]
        rts = [math.log(cl[i]/cl[i-1]) for i in range(1, len(cl)) if cl[i-1] > 0]
        mu = sum(rts)/len(rts); var = sum((r-mu)**2 for r in rts)/max(len(rts)-1, 1)
        rv = math.sqrt(var*365)*100
        inst = pub("/v5/market/instruments-info", {"category":"option","baseCoin":"ETH","limit":"1000"})["result"]["list"]
        now = datetime.datetime.utcnow(); exps = {}
        for it in inst:
            m = re.search(r"-(\d+)([A-Z]{3})(\d{2})-", it["symbol"])
            if not m: continue
            e = datetime.datetime(2000+int(m.group(3)), MON[m.group(2)], int(m.group(1)), 8)
            exps.setdefault(m.group(1)+m.group(2)+m.group(3), (e-now).days)
        cand = sorted([(dd, kk) for kk, dd in exps.items() if dd >= 7])
        EXP, dte = cand[0][1], cand[0][0]
        strikes = sorted(set(ff(it["symbol"].split("-")[2]) for it in inst if "-"+EXP+"-" in it["symbol"]))
        Katm = min(strikes, key=lambda K: abs(K-s0))
        ivs = []
        for cp in ("C","P"):
            t = pub("/v5/market/tickers", {"category":"option","symbol":"ETH-%s-%d-%s-USDT"%(EXP,int(Katm),cp)})["result"]["list"][0]
            v = ff(t.get("markIv"))
            if v > 0: ivs.append(v)
        iv = sum(ivs)/len(ivs)*100 if ivs else 0.0
    except Exception:
        out = {"iv":0,"rv":0,"prem":0,"band":"нет данных","rank":None,"lo":0,"hi":0,"exp":"","dte":0}
        _VOLC["t"]=time.time(); _VOLC["v"]=out; return out
    if iv > 0: VOLH.append(iv)
    lo, hi = (min(VOLH), max(VOLH)) if VOLH else (iv, iv)
    rank = round((iv-lo)/(hi-lo)*100) if hi > lo else None
    if iv < 40:   band = "ДЁШЕВО — вега дешева (окно покупать вол)"
    elif iv < 55: band = "норма-низ — ждать"
    elif iv < 70: band = "повышенная — присматриваться к продаже"
    else:         band = "СПАЙК — вола дорога (окно ПРОДАВАТЬ путы)"
    out = {"iv":round(iv,1),"rv":round(rv,1),"prem":round(iv-rv,1),"band":band,
           "rank":rank,"lo":round(lo,1),"hi":round(hi,1),"exp":EXP,"dte":dte}
    _VOLC["t"]=time.time(); _VOLC["v"]=out
    return out

_EARNC = {"t": 0.0, "v": None}   # кэш Earn (APR меняются медленно -> TTL)
def earn_snapshot(ttl=300):
    """Возвращает {'pos':{coin:amount}, 'watch':[...], 'top':[...]}. Только чтение."""
    if _EARNC["v"] is not None and (time.time()-_EARNC["t"]) < ttl:
        return _EARNC["v"]
    prods = []
    try:
        for cat in ("FlexibleSaving","OnChain"):
            for p in sget("/v5/earn/product", {"category":cat}).get("result",{}).get("list",[]):
                if p.get("status") != "Available": continue
                p["_cat"] = "On-Chain" if cat=="OnChain" else "Flex"
                prods.append(p)
    except Exception as e:
        prods = []
    pos = {}
    try:
        for cat in ("FlexibleSaving","OnChain"):
            for q in sget("/v5/earn/position", {"category":cat}).get("result",{}).get("list",[]):
                amt = ff(q.get("amount"))
                if amt > 0: pos[q.get("coin")] = pos.get(q.get("coin"),0.0) + amt
    except Exception:
        pos = {}
    # лучшая доступная доходность по монетам из watch-листа
    watch = []
    for c in EARN_WATCH:
        best = None
        for p in prods:
            if p.get("coin") != c: continue
            if best is None or _apr(p) > _apr(best): best = p
        if best is not None:
            watch.append({"coin":c,"apr":round(_apr(best),2),"cat":best["_cat"],
                          "min":best.get("minStakeAmount",""),"held":round(pos.get(c,0.0),4)})
        else:
            watch.append({"coin":c,"apr":None,"cat":"—","min":"","held":round(pos.get(c,0.0),4)})
    # топ «интересных» доходностей (вне watch-листа) — высокий APR = мелкие/волатильные
    wl = set(EARN_WATCH); seen = set(); top = []
    for p in sorted(prods, key=_apr, reverse=True):
        c = p.get("coin")
        if c in wl or c in seen: continue
        seen.add(c); top.append({"coin":c,"apr":round(_apr(p),2),"cat":p["_cat"]})
        if len(top) >= 6: break
    out = {"pos":pos, "watch":watch, "top":top, "n":len(prods)}
    _EARNC["t"] = time.time(); _EARNC["v"] = out
    return out

# ===================== ЗОЛОТО: спред XAUT/XAU (read-only) =====================
# спред ‰ = (XAUTUSDT / XAUUSDT − 1) × 1000. Сигналы — по окну 30д (1ч), контекст — 2д (15м) и 166д (4ч).
GOLD_WIN = {"2d": ("15", 192, 60), "30d": ("60", 720, 300), "166d": ("240", 1000, 900)}   # interval, limit, TTL
GOLD_CFG = {"k_add": 0.75, "k_exit1": 0.5, "stop_pad": 0.5, "fund_per_day": 6}
_GC = {}
def _cached(key, ttl, fn):
    c = _GC.get(key)
    if c and time.time()-c[0] < ttl: return c[1]
    v = fn(); _GC[key] = (time.time(), v); return v

def _kl(sym, iv, n):
    r = pub("/v5/market/kline", {"category":"linear","symbol":sym,"interval":iv,"limit":n})["result"]["list"]
    return {int(x[0]): ff(x[4]) for x in r}

def _gstats(v):
    q = sorted(v); n = len(q); m = sum(v)/n; sd = math.sqrt(sum((x-m)**2 for x in v)/n)
    pc = lambda p: q[min(n-1, max(0, int(round(p*(n-1)))))]
    return {"n":n,"mean":round(m,3),"sd":round(sd,3),"p5":round(pc(.05),3),"p10":round(pc(.10),3),"p50":round(pc(.5),3),
            "p90":round(pc(.90),3),"p95":round(pc(.95),3),"min":round(q[0],3),"max":round(q[-1],3)}

def _hl(v):   # полураспад отклонения, AR(1): Δs = a + b·s
    if len(v) < 30: return None
    x = v[:-1]; dy = [v[i+1]-v[i] for i in range(len(v)-1)]
    mx = sum(x)/len(x); my = sum(dy)/len(dy)
    vx = sum((a-mx)**2 for a in x)
    if vx <= 0: return None
    b = sum((a-mx)*(c-my) for a, c in zip(x, dy))/vx
    return -math.log(2)/math.log(1+b) if -1 < b < 0 else None

def _beta(ry, rx):
    n = len(rx)
    if n < 20: return None, None
    mx = sum(rx)/n; my = sum(ry)/n
    cxy = sum((a-mx)*(b-my) for a, b in zip(rx, ry)); vx = sum((a-mx)**2 for a in rx); vy = sum((b-my)**2 for b in ry)
    if vx <= 0 or vy <= 0: return None, None
    return round(cxy/vx, 3), round(cxy/math.sqrt(vx*vy), 3)

def _gseries(win):
    iv, n, ttl = GOLD_WIN[win]
    def f():
        A = _kl("XAUTUSDT", iv, n); B = _kl("XAUUSDT", iv, n)
        ts = sorted(t for t in A if t in B and B[t] > 0 and A[t] > 0)
        return {"s": [[t, round((A[t]/B[t]-1)*1000, 3)] for t in ts], "A": [A[t] for t in ts], "B": [B[t] for t in ts], "ts": ts}
    return _cached("ser"+win, ttl, f)

def _gbetas():
    def f():
        s = _gseries("30d"); E = _kl("ETHUSDT", "60", 720)
        A, B, ts = s["A"], s["B"], s["ts"]
        ra = [math.log(A[i]/A[i-1]) for i in range(1, len(ts))]; rb = [math.log(B[i]/B[i-1]) for i in range(1, len(ts))]
        b_ab, c_ab = _beta(ra, rb)
        pe = [(math.log(B[i]/B[i-1]), math.log(E[ts[i]]/E[ts[i-1]])) for i in range(1, len(ts)) if ts[i] in E and ts[i-1] in E]
        b_ge, c_ge = _beta([p[0] for p in pe], [p[1] for p in pe])
        return {"b_ab": b_ab, "c_ab": c_ab, "b_ge": b_ge, "c_ge": c_ge}
    return _cached("betas", 300, f)

def _gfund():
    def f():
        out = {}
        for sym in ("XAUUSDT", "XAUTUSDT"):
            h = pub("/v5/market/funding/history", {"category":"linear","symbol":sym,"limit":"180"})["result"]["list"]
            fr = [ff(x["fundingRate"]) for x in h]
            out[sym] = sum(fr)/len(fr) if fr else 0.0
        return out
    return _cached("fund", 900, f)

def _gfees():
    def f():
        out = {}
        for sym, dflt in (("XAUUSDT", (0.0, 0.000275)), ("XAUTUSDT", (0.00036, 0.001))):
            try:
                x = sget("/v5/account/fee-rate", {"category":"linear","symbol":sym})["result"]["list"][0]
                out[sym] = (ff(x["makerFeeRate"]), ff(x["takerFeeRate"]))
            except Exception:
                out[sym] = dflt
        return out
    return _cached("fees", 3600, f)

# ---- однодневки XAUT: продажа далёких OTM + перп-стоп на страйке ----
WING_CFG = {"max_h": 72, "min_h": 0.5, "min_bid": 0.2, "min_bsz": 0.1, "fee_cap": 0.07, "rows": 6}
def _N(x): return 0.5*(1+math.erf(x/math.sqrt(2)))
def _bs(S, K, T, s, cp):
    if T <= 0 or s <= 0: return max(S-K, 0) if cp == "C" else max(K-S, 0)
    d1 = (math.log(S/K)+.5*s*s*T)/(s*math.sqrt(T)); d2 = d1-s*math.sqrt(T)
    return S*_N(d1)-K*_N(d2) if cp == "C" else K*_N(-d2)-S*_N(-d1)

def gold_wings(S, ratio, tkA, tkB):
    def f():
        def rv(ser, per_year):
            a = ser["A"]; r = [math.log(a[i]/a[i-1]) for i in range(1, len(a)) if a[i-1] > 0]
            m = sum(r)/len(r); return math.sqrt(sum((x-m)**2 for x in r)/(len(r)-1)*per_year)
        rv30 = rv(_gseries("30d"), 365*24); rv2 = rv(_gseries("2d"), 365*96)
        try:
            x = sget("/v5/account/fee-rate", {"category":"option","baseCoin":"XAUT"})["result"]["list"][0]
            fmk = ff(x["makerFeeRate"])
        except Exception:
            fmk = 0.0002
        tk = pub("/v5/market/tickers", {"category":"option","baseCoin":"XAUT"})["result"]["list"]
        return {"rv30": rv30, "rv2": rv2, "fmk": fmk, "tk": tk}
    D = _cached("wings", 15, f)
    rv30, rv2, fmk = D["rv30"], D["rv2"], D["fmk"]; sig = max(rv30, rv2)    # честная вола = бОльшая из двух
    now = datetime.datetime.utcnow(); c = WING_CFG
    exps = {}
    for t in D["tk"]:
        pr = t["symbol"].split("-")
        m = re.match(r"(\d+)([A-Z]{3})(\d{2})", pr[1])
        e = datetime.datetime(2000+int(m.group(3)), MON[m.group(2)], int(m.group(1)), 8)
        h = (e-now).total_seconds()/3600
        if c["min_h"] < h <= c["max_h"]: exps.setdefault(pr[1], {"h":h, "ts":[]})["ts"].append(t)
    out = []
    for e, E in sorted(exps.items(), key=lambda kv: kv[1]["h"]):
        T = E["h"]/24/365; rows = []
        und = ff(E["ts"][0].get("underlyingPrice")) or S
        atm = min(E["ts"], key=lambda t: abs(ff(t["symbol"].split("-")[2])-und))
        Katm = ff(atm["symbol"].split("-")[2])
        ivatm = [ff(t["markIv"]) for t in E["ts"] if ff(t["symbol"].split("-")[2]) == Katm]
        for t in E["ts"]:
            pr = t["symbol"].split("-"); K = ff(pr[2]); cp = pr[3]
            if (cp == "C" and K <= und) or (cp == "P" and K >= und): continue
            bid, bsz = ff(t["bid1Price"]), ff(t["bid1Size"])
            if bid < c["min_bid"] or bsz < c["min_bsz"]: continue
            fair = _bs(und, K, T, sig, cp)
            d2 = (math.log(und/K)-.5*sig*sig*T)/(sig*math.sqrt(T))
            pitm = _N(d2) if cp == "C" else _N(-d2)
            fee = min(fmk*und, c["fee_cap"]*bid)
            rows.append({"cp":cp, "K":K, "bid":bid, "bsz":bsz, "ask":ff(t["ask1Price"]), "mark":round(ff(t["markPrice"]),2),
                         "iv":round(ff(t["markIv"])*100,1), "d":round(ff(t["delta"]),3), "dist":round((K/und-1)*100,2),
                         "sd":round(abs(math.log(K/und))/(sig*math.sqrt(T)),2), "pitm":round(pitm*100,2),
                         "fair":round(fair,2), "fee":round(fee,2), "edge":round(bid-fair-fee,2),
                         "stop_xaut":K, "stop_xau":round(K*ratio,1), "kill_xaut":round(bid/(2*tkA*K),2), "kill_xau":round(bid/(2*tkB*K*ratio),2)})
        calls = sorted([r for r in rows if r["cp"] == "C"], key=lambda r: -r["K"])
        puts = sorted([r for r in rows if r["cp"] == "P"], key=lambda r: r["K"])
        fc, fp = (calls[0] if calls else None), (puts[0] if puts else None)
        strg = None
        if fc and fp:
            prem = fc["bid"]+fp["bid"]-fc["fee"]-fp["fee"]
            strg = {"Kc":fc["K"], "Kp":fp["K"], "prem":round(prem,2), "edge":round(fc["edge"]+fp["edge"],2),
                    "pitm":round(fc["pitm"]+fp["pitm"],2), "be_hi":round(fc["K"]+prem,1), "be_lo":round(fp["K"]-prem,1),
                    "lot01":round(prem*0.1,2)}
        out.append({"exp":e, "h":round(E["h"],1), "und":round(und,2), "ivatm":round(sum(ivatm)/len(ivatm)*100,1) if ivatm else None,
                    "calls":calls[:c["rows"]], "puts":puts[:c["rows"]], "strangle":strg})
    return {"rv30":round(rv30*100,1), "rv2":round(rv2*100,1), "sig":round(sig*100,1), "fmk":fmk,
            "cost_xaut":round(2*tkA*S,2), "cost_xau":round(2*tkB*S*ratio,2), "exps":out}

def gold_snapshot():
    tk = {}
    for sym in ("XAUUSDT", "XAUTUSDT"):
        t = pub("/v5/market/tickers", {"category":"linear","symbol":sym})["result"]["list"][0]
        tk[sym] = {"bid":ff(t["bid1Price"]),"ask":ff(t["ask1Price"]),"last":ff(t["lastPrice"]),"fund":ff(t.get("fundingRate")),
                   "nft":int(ff(t.get("nextFundingTime")))}
    A, B = tk["XAUTUSDT"], tk["XAUUSDT"]
    amid, bmid = (A["bid"]+A["ask"])/2, (B["bid"]+B["ask"])/2
    mid = (amid/bmid-1)*1000
    conv_x = (A["ask"]/B["bid"]-1)*1000     # вход на схождение: купить XAUT по ask, продать XAU по bid
    div_x = (A["bid"]/B["ask"]-1)*1000      # вход на расхождение: продать XAUT по bid, купить XAU по ask
    ser = {w: _gseries(w) for w in GOLD_WIN}
    st = {w: _gstats([p[1] for p in ser[w]["s"]]) for w in GOLD_WIN}
    rank = {w: round(sum(1 for p in ser[w]["s"] if p[1] <= mid)/len(ser[w]["s"])*100) for w in GOLD_WIN}
    hl_h = _hl([p[1] for p in ser["30d"]["s"]]); hl2 = _hl([p[1] for p in ser["2d"]["s"]])
    hl2_h = hl2*0.25 if hl2 else None
    fe = _gfees(); fa = _gfund(); be = _gbetas()
    mkA, tkA = fe["XAUTUSDT"]; mkB, tkB = fe["XAUUSDT"]
    rt_mm = 2*(mkA+mkB)*1e4; rt_mt = 2*(mkA+tkB)*1e4          # круг (вход+выход) в бп: мейкер-мейкер / XAU тейкером
    npd = GOLD_CFG["fund_per_day"]
    carry_now = (B["fund"]-A["fund"])*npd*1e4                 # бп/день для СХОЖДЕНИЯ (лонг XAUT, шорт XAU)
    carry_avg = (fa["XAUUSDT"]-fa["XAUTUSDT"])*npd*1e4
    s30, s166 = st["30d"], st["166d"]; c = GOLD_CFG
    hold_d = min((hl_h or 48)/24, 14)
    lv = {"conv_entry": s30["p5"], "conv_add": round(s30["p5"]-c["k_add"]*s30["sd"],3),
          "conv_exit1": round(s30["mean"]-c["k_exit1"]*s30["sd"],3), "conv_exit2": s30["mean"],
          "conv_stop": round(min(s166["min"], s30["min"])-c["stop_pad"],3),
          "div_entry": s30["p95"], "div_add": round(s30["p95"]+c["k_add"]*s30["sd"],3),
          "div_exit1": round(s30["mean"]+c["k_exit1"]*s30["sd"],3), "div_exit2": s30["mean"],
          "div_stop": round(max(s166["max"], s30["max"])+c["stop_pad"],3)}
    edge_conv = (lv["conv_exit2"]-conv_x)*10 - rt_mm + carry_avg*hold_d
    edge_div = (div_x-lv["div_exit2"])*10 - rt_mm - carry_avg*hold_d
    now = datetime.datetime.utcnow(); wk = now.weekday()
    weekend = wk >= 5 or (wk == 4 and now.hour >= 16) or (wk == 0 and now.hour < 1)
    if weekend:
        sig, kind = "ВЫХОДНЫЕ — рынок золота закрыт/ограничен, XAU заморожен: новые спреды не открывать", "stop"
    elif conv_x <= lv["conv_stop"]:
        sig, kind = "СПРЕД ЗА СТОПОМ СХОЖДЕНИЯ (%.2f‰ < %.2f) — режим сломан, не входить" % (conv_x, lv["conv_stop"]), "stop"
    elif div_x >= lv["div_stop"]:
        sig, kind = "СПРЕД ЗА СТОПОМ РАСХОЖДЕНИЯ (%.2f‰ > %.2f) — режим сломан, не входить" % (div_x, lv["div_stop"]), "stop"
    elif conv_x <= lv["conv_entry"] and edge_conv > 0:
        sig, kind = "ВХОД НА СХОЖДЕНИЕ: лонг XAUT / шорт XAU  (%.2f‰ ≤ %.2f, эдж %+.1f бп)" % (conv_x, lv["conv_entry"], edge_conv), "conv"
    elif div_x >= lv["div_entry"] and edge_div > 0:
        sig, kind = "ВХОД НА РАСХОЖДЕНИЕ: шорт XAUT / лонг XAU  (%.2f‰ ≥ %.2f, эдж %+.1f бп)" % (div_x, lv["div_entry"], edge_div), "div"
    else:
        sig, kind = "ждать — до входа на схождение %.2f‰, до расхождения %.2f‰" % (conv_x-lv["conv_entry"], lv["div_entry"]-div_x), "wait"
    # позиции по золоту: перпы/фьючи XAU*, опционы XAUT-*, спот XAUT
    gp = []; oz = {"XAUTUSDT":0.0,"XAUUSDT":0.0,"other":0.0}; avg = {}
    for sc in ("USDT", "USDC"):
        for p in sget("/v5/position/list", {"category":"linear","settleCoin":sc,"limit":"200"})["result"]["list"]:
            if ff(p["size"]) > 0 and p["symbol"].startswith("XAU"):
                sg = 1 if p["side"] == "Buy" else -1; q = sg*ff(p["size"])
                k = p["symbol"] if p["symbol"] in oz else "other"; oz[k] += q
                if p["symbol"] in ("XAUTUSDT","XAUUSDT"): avg[p["symbol"]] = ff(p["avgPrice"])
                gp.append({"sym":p["symbol"],"side":"B" if sg>0 else "S","sz":ff(p["size"]),"avg":ff(p["avgPrice"]),
                           "d":round(q,4),"g":0,"t":0,"v":0,"up":round(ff(p.get("unrealisedPnl")),2)})
    G = {"d":0.0,"g":0.0,"t":0.0,"v":0.0}; cur = ""
    shorts_itm, shorts_all, stop_need = [], [], 0.0
    for _ in range(5):
        q = {"category":"option","limit":"200"}
        if cur: q["cursor"] = cur
        r = sget("/v5/position/list", q)["result"]
        for o in r["list"]:
            if ff(o["size"]) > 0 and o["symbol"].startswith("XAUT-"):
                od, og_, ot, ov = ff(o.get("delta")), ff(o.get("gamma")), ff(o.get("theta")), ff(o.get("vega"))
                G["d"]+=od; G["g"]+=og_; G["t"]+=ot; G["v"]+=ov
                if o["side"] == "Sell":   # стоп-хедж: ITM-шорт требует перп на всю ногу
                    pr_ = o["symbol"].split("-"); K_ = ff(pr_[2]); q_ = ff(o["size"])
                    if pr_[3] == "C" and amid > K_: shorts_itm.append(("C", K_, q_)); stop_need += q_
                    if pr_[3] == "P" and amid < K_: shorts_itm.append(("P", K_, q_)); stop_need -= q_
                    shorts_all.append({"sym":o["symbol"].replace("-USDT",""), "K":K_, "cp":pr_[3], "q":q_,
                                       "dist":round((K_/amid-1)*100,2)})
                gp.append({"sym":o["symbol"].replace("-USDT",""),"side":"B" if o["side"]=="Buy" else "S","sz":ff(o["size"]),
                           "avg":ff(o.get("avgPrice")),"d":round(od,4),"g":round(og_,5),"t":round(ot,2),"v":round(ov,2),
                           "up":round(ff(o.get("unrealisedPnl")),2)})
        cur = r.get("nextPageCursor","")
        if not cur: break
    wb = sget("/v5/account/wallet-balance", {"accountType":"UNIFIED"})["result"]["list"][0]
    coins = {x["coin"]: x for x in wb.get("coin", [])}
    xs = ff(coins.get("XAUT",{}).get("walletBalance")); xe = ff(earn_snapshot().get("pos",{}).get("XAUT",0.0))
    d_oz = oz["XAUTUSDT"]+oz["XAUUSDT"]+oz["other"]+xs+xe+G["d"]
    # открытый спред и его P&L в ‰
    sp = None; la, lb = oz["XAUTUSDT"], oz["XAUUSDT"]
    if la*lb < 0:
        leg = min(abs(la), abs(lb)); dirn = "conv" if la > 0 else "div"
        ent = (avg.get("XAUTUSDT",0)/avg.get("XAUUSDT",1)-1)*1000 if avg.get("XAUUSDT") else None
        now_exit = div_x if dirn == "conv" else conv_x      # закрытие: обратные стороны стакана
        pnl = (now_exit-ent) if dirn == "conv" else (ent-now_exit)
        sp = {"dir":dirn,"leg":round(leg,4),"entry":round(ent,3) if ent is not None else None,"pnl_pm":round(pnl,3) if ent is not None else None,
              "pnl_usd":round(pnl/1000*bmid*leg,2) if ent is not None else None,"resid":round(la+lb,4)}
    mm = ff(wb["totalMaintenanceMargin"]); eq = ff(wb["totalEquity"])
    mmp = mm/eq*100 if eq else 0
    warns = []
    k0 = kind
    if mmp > 75:
        warns.append("MM %.0f%% > 75%% — маржи нет, новые спреды НЕ открывать" % mmp)
        if kind in ("conv", "div"):
            sig, kind = "НЕ ВХОДИТЬ — MM %.0f%% > 75%%. Сигнал был бы: %s" % (mmp, sig), "stop"
    s2 = st["2d"]; kind, k0 = k0, kind     # проверки ниже — по исходному сигналу; потом вернём итоговый
    if kind == "conv" and s2["mean"] < lv["conv_entry"]:
        warns.append("дрейф: среднее 2д (%.2f‰) уже ниже порога входа — уровень сместился; вход половиной, вторая половина на добор %.2f‰" % (s2["mean"], lv["conv_add"]))
    if kind == "div" and s2["mean"] > lv["div_entry"]:
        warns.append("дрейф: среднее 2д (%.2f‰) уже выше порога расхождения — вход половиной, добор %.2f‰" % (s2["mean"], lv["div_add"]))
    if kind in ("conv","div") and carry_now*(1 if kind=="conv" else -1) < 0:
        warns.append("фандинг сейчас против позиции: %+.1f бп/день (среднее 30д %+.1f)" % (carry_now*(1 if kind=="conv" else -1), carry_avg*(1 if kind=="conv" else -1)))
    kind = k0
    try:
        WG = gold_wings(amid, bmid/amid, tkA, tkB)
    except Exception as e:
        WG = {"err": str(e)}
    perp_oz = oz["XAUTUSDT"]+oz["XAUUSDT"]+oz["other"]
    WG["stop"] = {"need":round(stop_need,3), "perp":round(perp_oz,3), "gap":round(stop_need-perp_oz,3),
                  "itm":shorts_itm, "shorts":shorts_all}
    return {"wings": WG, "t": time.strftime("%H:%M:%S"), "warns": warns, "xau":round(bmid,2), "xaut":round(amid,2), "tk":tk,
            "mid":round(mid,3), "usd":round(amid-bmid,2), "conv_x":round(conv_x,3), "div_x":round(div_x,3),
            "z":{w: round((mid-st[w]["mean"])/st[w]["sd"],2) if st[w]["sd"] else None for w in st}, "rank":rank, "stats":st,
            "hl_h": round(hl_h,1) if hl_h else None, "hl2_h": round(hl2_h,1) if hl2_h else None,
            "fees":{"mkA":mkA,"tkA":tkA,"mkB":mkB,"tkB":tkB,"rt_mm":round(rt_mm,2),"rt_mt":round(rt_mt,2)},
            "carry_now":round(carry_now,2), "carry_avg":round(carry_avg,2), "fund_avg":{k:round(v*100,5) for k,v in fa.items()},
            "lv":lv, "edge_conv":round(edge_conv,1), "edge_div":round(edge_div,1), "hold_d":round(hold_d,1),
            "sig":sig, "kind":kind, "usd_pm_oz":round(bmid/1000,3),
            "greeks":{"d_oz":round(d_oz,4),"d_usd":round(d_oz*bmid,1),"g":round(G["g"],5),"t":round(G["t"],2),"v":round(G["v"],2),
                      "spot":round(xs,5),"earn":round(xe,5),"opt_d":round(G["d"],4)},
            "beta":be, "pos":gp, "spread":sp,
            "acct":{"eq":round(eq,2),"mm_pct":round(mm/eq*100,1) if eq else 0,"av":round(ff(wb["totalAvailableBalance"]),2)},
            "series":{w: ser[w]["s"] for w in ser}}

def snapshot():
    wb = sget("/v5/account/wallet-balance", {"accountType":"UNIFIED"})["result"]["list"][0]
    eq = ff(wb["totalEquity"]); im = ff(wb["totalInitialMargin"]); mm = ff(wb["totalMaintenanceMargin"]); av = ff(wb["totalAvailableBalance"])
    ethpx = ff(pub("/v5/market/tickers", {"category":"linear","symbol":"ETHUSDT"})["result"]["list"][0]["lastPrice"])
    solpx = ff(pub("/v5/market/tickers", {"category":"linear","symbol":"SOLUSDT"})["result"]["list"][0]["lastPrice"])
    btcpx = ff(pub("/v5/market/tickers", {"category":"linear","symbol":"BTCUSDT"})["result"]["list"][0]["lastPrice"])
    coins = {c["coin"]: c for c in wb.get("coin", [])}
    cbal = lambda s: ff(coins[s].get("walletBalance")) if s in coins else 0.0
    eth_spot, sol_spot, btc_spot = cbal("ETH"), cbal("SOL"), cbal("BTC")
    usdt_bal, usdc_bal = cbal("USDT"), cbal("USDC")
    opts = []; cur = ""
    for _ in range(5):
        p = {"category":"option","limit":"200"}
        if cur: p["cursor"] = cur
        r = sget("/v5/position/list", p); opts += r["result"]["list"]; cur = r["result"].get("nextPageCursor","")
        if not cur: break
    opts = [o for o in opts if ff(o["size"]) > 0]
    perps = []
    for sc in ("USDT","USDC"):
        for p in sget("/v5/position/list", {"category":"linear","settleCoin":sc,"limit":"200"})["result"]["list"]:
            if ff(p["size"]) > 0: perps.append(p)
    perps += tail_pairs.inverse_positions(sget)   # ETHUSD (инверс) → size в монетах
    B = {"n":0,"d":0,"g":0,"t":0,"v":0}; S = {"n":0,"d":0,"g":0,"t":0,"v":0}
    OG = {}   # греки опционов ПО АКТИВУ (future-proof: ETH/SOL/BTC/XAUT/любой)
    legs = []; legs_out = []
    for o in opts:
        k = B if o["side"] == "Buy" else S
        od, ogg, ot, ov = ff(o.get("delta")), ff(o.get("gamma")), ff(o.get("theta")), ff(o.get("vega"))
        pr = o["symbol"].split("-")
        if pr[0] == "ETH":   # куплено/продано — только ETH (дельта MNT/QQQ в других единицах)
            k["n"] += 1; k["d"] += od; k["g"] += ogg; k["t"] += ot; k["v"] += ov
        asset = pr[0] if pr else "?"
        a = OG.setdefault(asset, {"d":0.0,"g":0.0,"t":0.0,"v":0.0,"n":0})
        a["d"]+=od; a["g"]+=ogg; a["t"]+=ot; a["v"]+=ov; a["n"]+=1
        if len(pr) >= 4 and pr[0] == "ETH":
            sd = 1 if o["side"] == "Buy" else -1
            legs.append({"K":ff(pr[2]),"cp":pr[3],"sz":ff(o["size"]),"side":sd,"entry":ff(o.get("avgPrice"))})
            legs_out.append({"cp":pr[3],"K":ff(pr[2]),"dte":round(dte_of(pr[1]),2),"side":"B" if sd==1 else "S",
                             "g":ogg,"v":ov,"t":ot})
    perp = []; perp_d = 0.0; sol_perp_d = 0.0; btc_perp_d = 0.0
    for p in perps:
        sgn = 1 if p["side"] == "Buy" else -1
        if "ETH" in p["symbol"]:
            perp_d += sgn*ff(p["size"])
            perp.append({"sz":ff(p["size"]),"side":sgn,"entry":ff(p.get("avgPrice"))})
        elif "SOL" in p["symbol"]:
            sol_perp_d += sgn*ff(p["size"])
        elif "BTC" in p["symbol"]:
            btc_perp_d += sgn*ff(p["size"])
    og = lambda a,k: OG.get(a,{}).get(k,0.0)
    opt_d = og("ETH","d"); net_d = opt_d+perp_d   # ETH-specific: опционы ETH + ETH-перп
    upnl = sum(ff(o.get("unrealisedPnl")) for o in opts) + sum(ff(p.get("unrealisedPnl")) for p in perps)
    lo = ethpx*0.55; hi = ethpx*1.25; N = 140; payoff = []
    for i in range(N+1):
        s = lo+(hi-lo)*i/N; pnl = 0.0
        for L in legs:
            intr = max(s-L["K"],0) if L["cp"] == "C" else max(L["K"]-s,0)
            pnl += L["side"]*L["sz"]*(intr-L["entry"])
        for P in perp: pnl += P["side"]*P["sz"]*(s-P["entry"])
        payoff.append([round(s,1), round(pnl,1)])
    bes = []
    for i in range(1, len(payoff)):
        y0, y1 = payoff[i-1][1], payoff[i][1]
        if (y0 <= 0 <= y1) or (y0 >= 0 >= y1):
            if y1 != y0: bes.append(round(payoff[i-1][0]+(payoff[i][0]-payoff[i-1][0])*(-y0)/(y1-y0),0))
    # суммы abs по греку для пут/колл bought/sold -> для беты во времени
    def sums(cp, sd):
        sub = [l for l in legs_out if l["cp"] == cp and l["side"] == sd]
        return {g: round(sum(abs(x[g]) for x in sub), 4) for g in ("g","v","t")}
    pt = {"t": time.strftime("%H:%M:%S"), "eth": round(ethpx,1),
          "pb": sums("P","B"), "ps": sums("P","S"), "cb": sums("C","B"), "cs": sums("C","S")}
    HIST.append(pt)
    def _k(o):
        pr = o["symbol"].split("-")
        return (pr[0] != "ETH", dte_of(pr[1]), ff(pr[2])) if len(pr) >= 4 else (True, 0, 0)
    positions = []
    for o in sorted(opts, key=_k):
        positions.append({"sym":o["symbol"].replace("-USDT",""),"side":"B" if o["side"]=="Buy" else "S",
            "sz":ff(o["size"]),"d":round(ff(o.get("delta")),3),"g":round(ff(o.get("gamma")),4),
            "t":round(ff(o.get("theta")),2),"v":round(ff(o.get("vega")),2),"up":round(ff(o.get("unrealisedPnl")),2)})
    for p in perps:
        sgn = 1 if p["side"] == "Buy" else -1
        positions.append({"sym":p["symbol"],"side":"B" if sgn>0 else "S","sz":ff(p["size"]),
            "d":round(sgn*ff(p["size"]),3),"g":0,"t":0,"v":0,"up":round(ff(p.get("unrealisedPnl")),2)})
    rat = lambda a,b: round(abs(a)/abs(b)*100,1) if b else 0
    # истинная дельта с учётом спота и Earn, по активу (в монетах)
    VV = vol_snapshot()           # live датчик волы (IV/RV/премия/полоса)
    EA = earn_snapshot()          # live Earn: позиции + возможности (read-only)
    ep = EA.get("pos", {})
    # застейканная дельта: из реальных Earn-позиций, с фолбэком на ручной EARN
    eth_earn = ep.get("ETH", ff(EARN.get("ETH")))
    sol_earn = ep.get("SOL", ff(EARN.get("SOL")))
    btc_earn = ep.get("BTC", ff(EARN.get("BTC")))
    eth_true = opt_d + perp_d + eth_spot + eth_earn
    sol_true = og("SOL","d") + sol_perp_d + sol_spot + sol_earn
    btc_true = og("BTC","d") + btc_perp_d + btc_spot + btc_earn
    spot = {"eth":round(eth_spot,4),"sol":round(sol_spot,4),"btc":round(btc_spot,6),
            "usdt":round(usdt_bal,2),"usdc":round(usdc_bal,2),
            "earn_eth":round(eth_earn,4),"earn_sol":round(sol_earn,4),"earn_btc":round(btc_earn,6)}
    trued = {"eth":round(eth_true,4),"eth_usd":round(eth_true*ethpx,1),
             "eth_hedge":round(-(opt_d+eth_spot+eth_earn)-perp_d,4),
             "sol":round(sol_true,4),"sol_usd":round(sol_true*solpx,1),
             "sol_hedge":round(-(og("SOL","d")+sol_spot+sol_earn)-sol_perp_d,4),
             "btc":round(btc_true,6),"btc_usd":round(btc_true*btcpx,1),
             "btc_hedge":round(-(og("BTC","d")+btc_spot+btc_earn)-btc_perp_d,6),
             "solpx":round(solpx,2),"btcpx":round(btcpx,2),
             "eth_perp":round(perp_d,4),"sol_perp":round(sol_perp_d,4),"btc_perp":round(btc_perp_d,6),"opt_d":round(opt_d,4),
             "total_usd":round(eth_true*ethpx + sol_true*solpx + btc_true*btcpx,1)}
    # скальп-сигнал: дельта ETH (опц+перп+спот+Earn) против порога, уровни сетки от последнего перп-филла
    SC = scalp_snapshot()
    gam = og("ETH","g"); step = SCALP["step"]; band = SCALP["band"]
    qstep = round(abs(gam)*step, 2)
    anc = SC.get("anchor") or ethpx
    if eth_true > band:    act = "ПРОДАЙ %.2f ETH перп (дельта %+.2f > +%.1f)" % (eth_true, eth_true, band)
    elif eth_true < -band: act = "КУПИ %.2f ETH перп (дельта %+.2f < −%.1f)" % (-eth_true, eth_true, band)
    else:                  act = "ждать — дельта %+.2f в коридоре ±%.1f" % (eth_true, band)
    SC = dict(SC, gam=round(gam,4), step=step, band=band, qstep=qstep, act=act,
              sell_at=round(anc+step,1), buy_at=round(anc-step,1), dist=round(ethpx-anc,1),
              hot=abs(eth_true) > band, longg=gam > 0,
              theta_h=round((B["t"]+S["t"])/24, 2),
              net=round(SC.get("cpnl",0)-SC.get("fees",0), 2) if SC.get("ok") else None)
    return {"t":pt["t"],"eth":round(ethpx,2),"eq":round(eq,2),"im":round(im,2),"mm":round(mm,2),"av":round(av,2),
        "im_pct":round(im/eq*100,1) if eq else 0,"mm_pct":round(mm/eq*100,1) if eq else 0,"buf":round(eq/mm,2) if mm else 0,
        "net":{"dusd":round(net_d*ethpx,1),"deth":round(net_d,4),"g":round(og("ETH","g"),4),"t":round(og("ETH","t"),2),"v":round(og("ETH","v"),2)},
        "og":{a:{k:round(v,4) for k,v in gg.items()} for a,gg in OG.items()},
        "port":{"g":round(B["g"]+S["g"],4),"t":round(B["t"]+S["t"],2),"v":round(B["v"]+S["v"],2)},
        "upnl":round(upnl,2),
        "B":{k:round(v,3) for k,v in B.items()},"S":{k:round(v,3) for k,v in S.items()},
        "beta":{"g":rat(S["g"],B["g"]),"t":rat(S["t"],B["t"]),"v":rat(S["v"],B["v"])},
        "payoff":payoff,"be":sorted(set(bes)),"legs":legs_out,"hist":list(HIST),"positions":positions,
        "spot":spot,"true":trued,"earn":EA,"vol":VV,"scalp":SC}

def _port():   # общий снимок портфеля для /port и /arb (кэш 3 с)
    return tail_pairs.cached("port_snap", 3, lambda: portfolio_book.portfolio_snapshot(sget, pub, earn_snapshot))

HTML = r'''<!doctype html><html><head><meta charset="utf-8"><title>Книга Bybit</title><style>
body{background:#0d1117;color:#e6edf3;font:13px/1.4 Segoe UI,Roboto,sans-serif;margin:0;padding:12px}
.hd{display:flex;justify-content:space-between;align-items:center}
.row{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0;align-items:flex-start}
.tile{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:7px 12px;min-width:84px}
.tile .lbl{font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px}
.tile .val{font-size:18px;font-weight:600;margin-top:2px}
.pos{color:#3fb950}.neg{color:#f85149}.warn{color:#d29922}
table{border-collapse:collapse;font-size:12px}td,th{padding:3px 12px;text-align:right}
th{color:#8b949e;font-weight:500}td:first-child,th:first-child{text-align:left}
canvas{background:#161b22;border:1px solid #30363d;border-radius:8px;margin-top:4px;max-width:100%}
h3{margin:12px 0 2px;font-size:11px;color:#8b949e;font-weight:600;text-transform:uppercase;letter-spacing:.5px}
#upd{font-size:12px;color:#8b949e}
.gbtn{cursor:pointer;padding:2px 10px;border:1px solid #30363d;border-radius:6px;margin-right:4px;color:#8b949e}
.gbtn.on{background:#1f6feb33;border-color:#1f6feb;color:#e6edf3}
.ksub{font-size:11px;color:#8b949e;margin:6px 0 0}
.tile.sm{padding:6px 12px;min-width:84px}.tile.sm .val{font-size:19px;font-weight:600;margin-top:1px}.tile.sm .lbl{font-size:10px}
#dkhead{position:sticky;top:0;z-index:5;background:#0d1117;padding:3px 0 5px;border-bottom:1px solid #30363d}</style></head><body>
<div class="hd"><b>Книга Bybit — реалтайм</b><span id="upd">загрузка...</span></div>
<div style="margin:6px 0 2px" id="mainbar"><span class="gbtn" id="mbDK" onclick="showMain('DK')">Деск: спред SOXL/SOXS</span><span class="gbtn on" id="mbK" onclick="showMain('K')">Книга</span><span class="gbtn" id="mbSP" onclick="showMain('SP')">Оператор спреда SOXL/SOXS</span><span class="gbtn" id="mbETH" onclick="showMain('ETH')">ETH: sell far put</span><span class="gbtn" id="mbSH" onclick="showMain('SH')">Шиткоины</span><span class="gbtn" id="mbAP" onclick="showMain('AP')">Арб-порт</span></div>
<div style="margin:0 0 6px;font-size:12px;min-height:4px" id="subbar"></div>
<div id="tabK">
<div class="row" id="ktiles"></div>
<div id="kwarn" style="font-size:12px;margin:0 0 4px"></div>
<div class="row" style="gap:16px">
<div><h3 id="kpayh">Payoff — весь портфель</h3><canvas id="kpALL" width="780" height="260"></canvas><div id="kpaynote" style="color:#8b949e;font-size:11px;margin:2px 0 0;max-width:780px"></div></div>
<div><h3>Группы (клик — детали)</h3><table id="ksumm"></table></div>
</div>
<h3>Гамма и скальп · шаг сетки: <span id="kgsteps"></span></h3>
<table id="kgam"></table>
<div class="row" style="gap:16px">
<div><div class="ksub" id="kgph"></div><canvas id="kgprof" width="620" height="230"></canvas></div>
<div><div class="ksub" id="kgeh"></div><canvas id="kgexp" width="440" height="230"></canvas></div>
</div>
<div id="kgnote" style="color:#8b949e;font-size:11px;margin:2px 0 6px;max-width:1100px"></div>
<h3>Payoff по активам (клик — детали группы)</h3><div class="row" id="kpgrid"></div>
<h3>Стресс T+0 по сценариям, $ (относительно текущей стоимости)</h3><table id="kstress"></table>
<div id="kstnote" style="color:#8b949e;font-size:11px;margin:2px 0 6px"></div>
<h3>Во времени (память сервера, точка раз в 20 с, до ~12 ч)</h3>
<div class="row">
<div><div class="ksub">Эквити $ · ETH и корзина % от начала окна</div><canvas id="kt1" width="480" height="130"></canvas></div>
<div><div class="ksub">Δ$ портфеля</div><canvas id="kt2" width="480" height="130"></canvas></div>
<div><div class="ksub">IM, % эквити</div><canvas id="kt3" width="480" height="130"></canvas></div>
<div><div class="ksub">MM, % эквити</div><canvas id="kt4" width="480" height="130"></canvas></div>
<div><div class="ksub">Θ $/день</div><canvas id="kt5" width="480" height="130"></canvas></div>
<div><div class="ksub">Вега $ на 1 п.п. IV</div><canvas id="kt6" width="480" height="130"></canvas></div>
</div>
<div id="ktnote" style="color:#8b949e;font-size:11px;margin:2px 0 6px"></div>
<details id="kposd" ontoggle="if(this.open&&PORT)renderKPos(PORT)"><summary id="kposs" style="cursor:pointer;color:#8b949e;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;margin-top:12px">Позиции с греками</summary><table id="kpos"></table></details>
</div>

<div id="tabC" style="display:none">
<h3>Деление капитала: ETH-каркас на Bybit · MEXC (ерши, сеточный бот). Только чтение</h3>
<div class="row" id="ctop"></div>
<div style="font-size:12px;margin:4px 0" id="cctl"></div>
<div id="cmsg" style="font-size:12px;margin:4px 0"></div>
<h3>Bybit (UTA)</h3><div class="row" id="cbb"></div>
<h3>MEXC</h3><div id="cmx"></div>
</div>
<div id="tabI" style="display:none">
<h3>Источники дохода — по журналу операций Bybit (UTA). Только чтение</h3>
<div style="font-size:12px;margin:4px 0" id="ictl"></div>
<div class="row" id="itop"></div>
<h3>По категориям</h3><table id="icat"></table>
<h3>По дням (столбики — категории, линия — накопленный итог)</h3>
<canvas id="iday" width="1200" height="260" style="max-width:100%;border:1px solid #30363d;border-radius:6px;background:#0d1117"></canvas>
<div id="ileg" style="font-size:11px;margin:2px 0 8px"></div>
<h3>По инструментам <span id="iflt" style="font-size:12px;font-weight:400"></span></h3>
<div style="display:flex;gap:16px;flex-wrap:wrap"><div style="flex:1;min-width:420px"><div style="color:#8b949e;font-size:12px">лучшие</div><table id="iwin"></table></div>
<div style="flex:1;min-width:420px"><div style="color:#8b949e;font-size:12px">худшие</div><table id="ilos"></table></div></div>
<h3>Ликвидации и пополнения</h3><div id="iliq" style="font-size:12px"></div>
<div style="color:#8b949e;font-size:11px;margin-top:8px">Как считается: журнал /v5/account/transaction-log за окно → поле «change» (деньги, которые реально пришли/ушли: реализованный P&amp;L перпов, премии опционов, поставка, фандинг; комиссии уже внутри), монеты переведены в $ по текущей цене. Для ещё открытых опционов к деньгам добавлена текущая стоимость (mark × размер) — это «итог» символа: сколько он дал бы, если закрыть сейчас по марку. «Проданные / купленные» — по первой сделке символа в окне. Спот — переоценка: потраченные доллары + купленные монеты по сегодняшней цене. Пополнения в доход не входят. Ограничение: позиции, открытые до начала окна, видны частично (например, откуп опциона, проданного раньше, без его премии) — поэтому итоги разных окон не складываются и могут расходиться с «P&amp;L Analysis» Bybit (там позиционный учёт и комиссии отдельно). Журнал за 30 дней — ~8 тыс. строк, первый расчёт ~1 мин, дальше кэш 10 мин.</div>
</div>
<div id="tabS" style="display:none">
<h3>Спред SOXL / SOXS — 3× бык и 3× медведь на индекс полупроводников. Отработка ершей и волатильности в дельта-нейтрали</h3>
<div style="font-size:12px;margin:2px 0 4px" id="prctl"></div>
<div id="prwarn" style="font-size:12px;margin:2px 0 4px"></div>
<div class="row" id="prtop"></div>
<div style="display:flex;gap:16px;flex-wrap:wrap">
<div style="flex:2;min-width:520px"><table id="prlegs"></table></div>
<div style="flex:1;min-width:300px"><table id="prscen"></table></div>
</div>
<div class="row" id="prmkt" style="margin-top:6px"></div>
<div id="prc1_box" style="margin-top:12px"></div>
<div id="prc2_box" style="margin-top:12px"></div>
<h3>Графики TradingView: SOXL, SOXM, SOXS (спред, отношение, две линии)</h3>
<div style="font-size:12px;margin:4px 0" id="prtv_ctl"></div>
<div id="prtv_box" style="height:520px;max-width:1400px;border:1px solid #2a2e39;border-radius:4px;background:#131722;margin-bottom:8px"></div>
<div style="color:#8b949e;font-size:11px;margin:2px 0 10px">Пара не сходится по цене: обе ноги отыгрывают один индекс в разные стороны, отношение SOXL/SOXS ≈ ход индекса × 6. <b>Чистая ставка</b> = $SOXL − $SOXS (лонг SOXS считается с минусом), в долларах индекса — × 3. <b>Захеджировано</b> — меньшая из ног, если обе в одну сторону (лонг+лонг или шорт+шорт): эта часть не зависит от направления. <b>До нейтрали</b> — что докупить или продать в одной ноге, чтобы ставка стала нулевой. Сценарии линейные (ход индекса × 3, без учёта сложного процента за несколько дней). «Лонг обеих поровну» — что дало бы держание пары в равных долларах: (доходность SOXL + доходность SOXS) / 2. Крайние случаи заявок: исполнились все покупки SOXL и продажи SOXS (ставка на рост) или наоборот (на падение). В Portfolio Margin маржа по SOXL и SOXS считается отдельно и не зачитывается, даже если пара нейтральна. Только чтение.</div>
<div style="font-size:12px;margin:2px 0 4px" id="pcctl"></div>
<canvas id="prc" width="1400" height="780" style="width:100%;max-width:1400px;border:1px solid #2a2e39;border-radius:4px;background:#131722;display:block;margin-bottom:4px;cursor:crosshair"></canvas>
<div style="color:#8b949e;font-size:11px;margin:2px 0 10px">Три панели. <b>Верх</b> — цены последних сделок <span style="color:#2962ff">SOXL</span> и <span style="color:#22c55e">SOXS</span> (или % от начала окна), пунктир — мои средние цены входа, столбики — оборот SOXL. <b>Середина</b> — <span style="color:#f7525f">отношение SOXL/SOXS</span>, % от начала окна (то же, что SOXLUSDT.P/SOXSUSDT.P на TradingView; ≈ ход индекса × 6) и <span style="color:#f0b90b">«лонг обеих поровну»</span> — что дала бы нейтральная пара за окно (уходит в минус на болтанке — распад 3× ETF). <b>Низ</b> — сколько заработала бы МОЯ ТЕКУЩАЯ позиция, если бы стояла с начала окна (размеры ног × изменение цен), $. Бары 10с/30с собираются из сделок Bybit и копятся с запуска сервера (до 2 ч); 1м/5м/1ч — свечи Bybit, история есть сразу.</div>
<h3>Волатильность и «ёрш» пары (минутные свечи Bybit)</h3>
<div class="row" id="lbtop"></div>
<table id="lbvol"></table>
<div style="color:#8b949e;font-size:11px;margin:2px 0 10px">σ 1 мин — стандартное отклонение минутной доходности. Размах — (макс − мин) / средняя за окно. Путь — сумма всех минутных ходов по модулю; путь / смещение — во сколько раз путь больше итогового хода (чем больше, тем сильнее «ёрш»: цена мечется, а не идёт). Пересечения — сколько раз цена пересекла свою среднюю за окно. «Лонг обеих поровну» — результат нейтральной пары за окно без перебалансировки; шорт обеих — тот же результат с обратным знаком (зарабатывает на болтанке, теряет на тренде).</div>
<h3>Симулятор сетки по последним часам (что дала бы сетка с такими параметрами)</h3>
<div style="font-size:12px;margin:2px 0 4px" id="lbctl"></div>
<table id="lbsim"></table>
<div id="lbmsg" style="font-size:12px;margin:4px 0"></div>
<div style="color:#8b949e;font-size:11px;margin:2px 0 10px">Модель: симметричная сетка от цены начала окна, уровни через «шаг» %, на каждом уровне лот в $. Касание уровня ниже — покупка, выше — продажа; сделка, уменьшающая остаток, закрывает лот с прибылью лот × шаг. Путь внутри минуты восстановлен по свече (открытие → минимум → максимум → закрытие или наоборот), исполнение на касании считается гарантированным (оптимистично), мейкер-комиссия 0. «Хедж» — раз в минуту остаток SOXL закрывается такой же по долларам позицией SOXS в ту же сторону, если отклонение ≥ 5 лотов; хедж-сделки по тейкеру 0.0275%. «Макс. ставка» — худший остаток за окно в пересчёте на 1% индекса. Это оценка на прошлых данных, а не прогноз.</div>
<h3>Сетки и скальп SOXL / SOXS (мои заявки и остаток)</h3>
<div style="font-size:12px;margin:2px 0 4px" id="grctl"></div>
<div class="row" id="grtop"></div>
<table id="grtab"></table>
<div style="color:#8b949e;font-size:11px;margin:2px 0 10px">Символ попадает сюда, если за окно у него ≥ 20 исполнений или сейчас ≥ 10 открытых заявок. <b>Реализовано</b> — закрытые куски (Bybit closed-pnl, комиссии внутри). <b>Остаток</b> — позиция, которую сетка ещё не закрыла, и её нереализованный P&amp;L: сетка закрывает только прибыльные куски, а то, что ушло против, копится тут — поэтому настоящий итог = реализовано + нереализовано. <b>Заявки сверх позиции</b> — если исполнится вся сторона, позиция станет такой; в Portfolio Margin такие заявки держат маржу как исполненные (10.11: продажи SOXL сверх позиции держали ≈ $100 маржи). Только чтение.</div>
</div>
<div id="tabO" style="display:none">
<h3>Оператор — сводка портфеля спред-трейдинга</h3>
<div class="row" id="op_acct"></div>
<h3>Δ портфель (бета)</h3>
<div class="row" id="op_pdelta"></div>
<h3>Дельта и тета по активам</h3>
<div class="row" id="op_assets"></div>
<details style="margin-top:8px"><summary style="cursor:pointer;color:#8b949e;font-size:12px">ETH и SOXL/SOXS — дубли: полные версии на вкладках «ETH: sell far put» и «Оператор спреда» (развернуть)</summary>
<h3>ETH: греки и payoff</h3>
<div class="row" id="op_eth_ov"></div>
<div id="op_eth_vol" style="font-size:12px;margin:2px 0 6px"></div>
<canvas id="op_eth_pay" width="780" height="240"></canvas>
<div id="op_eth_paynote" style="color:#8b949e;font-size:11px;margin-bottom:6px"></div>
<details open><summary style="cursor:pointer;color:#8b949e;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;margin-top:8px">Вся ETH-книга</summary><table id="op_eth_bookt"></table></details>
<h3>Гамма и скальп ETH</h3>
<div style="font-size:12px;margin:2px 0" id="op_eth_gsteps"></div><table id="op_eth_gam"></table>
<div style="display:flex;gap:12px;flex-wrap:wrap;margin-top:6px"><canvas id="op_eth_gprof" width="620" height="230"></canvas><canvas id="op_eth_gexp" width="440" height="230"></canvas></div>
<h3>Куплено / продано + спот+Earn ETH</h3><table id="op_eth_bs"></table><table id="op_eth_spot" style="margin-top:6px"></table>
<h3>ETH против корзины стресс</h3><table id="op_eth_beta"></table><div id="op_eth_betanote" style="color:#8b949e;font-size:11px"></div>
<h3>Открытые заявки ETH</h3><div id="op_eth_ordh" style="color:#8b949e;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;margin-top:4px"></div><table id="op_eth_ord"></table>
<h3>Арб ETH: связки · открытые ноги · фандинг</h3>
<div id="op_eth_abar" style="color:#8b949e;font-size:12px"></div>
<table id="op_eth_alinks"></table><table id="op_eth_aopen" style="margin-top:6px"></table><table id="op_eth_afund" style="margin-top:6px"></table>
<h3>Графики TradingView: SOXL/SOXS</h3>
<div style="font-size:12px;margin:4px 0" id="op_tv_ctl"></div>
<div id="op_tv_box" style="height:520px;max-width:1400px;border:1px solid #2a2e39;border-radius:4px;background:#131722;margin-bottom:8px"></div>
<h3>SOXL / SOXS: пара и конструкции</h3>
<div class="row" id="op_pr_top"></div>
<div id="op_prc1" style="margin-top:12px"></div>
<div id="op_prc2" style="margin-top:12px"></div>
</details>
</div>
<div id="tabD" style="display:none">
<div id="dkdocs" style="font-size:12px;margin:0 0 4px;line-height:1.8"><span style="color:#8b949e;margin-right:6px">Документация:</span><a href="obsidian://open?path=D%3A%5CVault%5CProject%5CR_vault%5C_coord%5CONBOARDING.md" title="открыть в Obsidian" style="color:#58a6ff;text-decoration:none;font-weight:600">Онбординг</a><a href="https://github.com/RomanBoreyko/R_proj/blob/main/_coord/ONBOARDING.md" target="_blank" title="на GitHub" style="font-size:10px;color:#8b949e;text-decoration:none;margin:0 10px 0 3px">gh</a><a href="obsidian://open?path=D%3A%5CVault%5CProject%5CR_vault%5CR-trade%5CR_trade_proj_sin-arb%5CTRADING-SYSTEM%5C03-STRATEGIES%5CSPREAD-%D0%BE%D0%BF%D0%B5%D1%80%D0%B0%D1%82%D0%BE%D1%80-SOXL-SOXS.md" title="открыть в Obsidian" style="color:#58a6ff;text-decoration:none;font-weight:600">Спецификация спреда</a><a href="https://github.com/RomanBoreyko/R_proj/blob/main/R-trade/R_trade_proj_sin-arb/TRADING-SYSTEM/03-STRATEGIES/SPREAD-%D0%BE%D0%BF%D0%B5%D1%80%D0%B0%D1%82%D0%BE%D1%80-SOXL-SOXS.md" target="_blank" title="на GitHub" style="font-size:10px;color:#8b949e;text-decoration:none;margin:0 10px 0 3px">gh</a><a href="obsidian://open?path=D%3A%5CVault%5CProject%5CR_vault%5CR-trade%5CR_trade_proj_sin-arb%5CTRADING-SYSTEM%5C08-operator%5C%D1%87%D0%B5%D0%BA-%D0%BB%D0%B8%D1%81%D1%82-%D0%B4%D0%BD%D1%8F.md" title="открыть в Obsidian" style="color:#58a6ff;text-decoration:none">Чек-лист дня</a><a href="https://github.com/RomanBoreyko/R_proj/blob/main/R-trade/R_trade_proj_sin-arb/TRADING-SYSTEM/08-operator/%D1%87%D0%B5%D0%BA-%D0%BB%D0%B8%D1%81%D1%82-%D0%B4%D0%BD%D1%8F.md" target="_blank" title="на GitHub" style="font-size:10px;color:#8b949e;text-decoration:none;margin:0 10px 0 3px">gh</a><a href="obsidian://open?path=D%3A%5CVault%5CProject%5CR_vault%5CR-trade%5CR_trade_proj_sin-arb%5CTRADING-SYSTEM%5C06-RISK%5Crisk-rules.md" title="открыть в Obsidian" style="color:#58a6ff;text-decoration:none">Правила риска</a><a href="https://github.com/RomanBoreyko/R_proj/blob/main/R-trade/R_trade_proj_sin-arb/TRADING-SYSTEM/06-RISK/risk-rules.md" target="_blank" title="на GitHub" style="font-size:10px;color:#8b949e;text-decoration:none;margin:0 10px 0 3px">gh</a><a href="obsidian://open?path=D%3A%5CVault%5CProject%5CR_vault%5CR-trade%5CR_trade_proj_sin-arb%5CTRADING-SYSTEM%5C13-LEARNING%5C%D0%9E%D0%BF%D0%B5%D1%80%D0%B0%D1%82%D0%BE%D1%80%20%D1%81%D0%BF%D1%80%D0%B5%D0%B4%D0%B0%5C%D0%9E%D0%A1-00%20%D0%9A%D0%B0%D1%80%D1%82%D0%B0%20%D0%BA%D1%83%D1%80%D1%81%D0%B0.md" title="открыть в Obsidian" style="color:#58a6ff;text-decoration:none;font-weight:600">Курс: карта</a><a href="https://github.com/RomanBoreyko/R_proj/blob/main/R-trade/R_trade_proj_sin-arb/TRADING-SYSTEM/13-LEARNING/%D0%9E%D0%BF%D0%B5%D1%80%D0%B0%D1%82%D0%BE%D1%80%20%D1%81%D0%BF%D1%80%D0%B5%D0%B4%D0%B0/%D0%9E%D0%A1-00%20%D0%9A%D0%B0%D1%80%D1%82%D0%B0%20%D0%BA%D1%83%D1%80%D1%81%D0%B0.md" target="_blank" title="на GitHub" style="font-size:10px;color:#8b949e;text-decoration:none;margin:0 10px 0 3px">gh</a><a href="obsidian://open?path=D%3A%5CVault%5CProject%5CR_vault%5CR-trade%5CR_trade_proj_sin-arb%5CTRADING-SYSTEM%5C13-LEARNING%5C%D0%9E%D0%BF%D0%B5%D1%80%D0%B0%D1%82%D0%BE%D1%80%20%D1%81%D0%BF%D1%80%D0%B5%D0%B4%D0%B0%5C%D0%9E%D0%A1-09%20%D0%A8%D0%BF%D0%B0%D1%80%D0%B3%D0%B0%D0%BB%D0%BA%D0%B0%2C%20%D0%B7%D0%B0%D0%B4%D0%B0%D1%87%D0%B8%2C%20%D0%BC%D0%B5%D1%82%D1%80%D0%B8%D0%BA%D0%B8.md" title="открыть в Obsidian" style="color:#58a6ff;text-decoration:none">Шпаргалка</a><a href="https://github.com/RomanBoreyko/R_proj/blob/main/R-trade/R_trade_proj_sin-arb/TRADING-SYSTEM/13-LEARNING/%D0%9E%D0%BF%D0%B5%D1%80%D0%B0%D1%82%D0%BE%D1%80%20%D1%81%D0%BF%D1%80%D0%B5%D0%B4%D0%B0/%D0%9E%D0%A1-09%20%D0%A8%D0%BF%D0%B0%D1%80%D0%B3%D0%B0%D0%BB%D0%BA%D0%B0%2C%20%D0%B7%D0%B0%D0%B4%D0%B0%D1%87%D0%B8%2C%20%D0%BC%D0%B5%D1%82%D1%80%D0%B8%D0%BA%D0%B8.md" target="_blank" title="на GitHub" style="font-size:10px;color:#8b949e;text-decoration:none;margin:0 10px 0 3px">gh</a><a href="obsidian://open?path=D%3A%5CVault%5CProject%5CR_vault%5CR-trade%5CR_trade_proj_sin-arb%5CTRADING-SYSTEM%5C11-ASSETS%5CSOXL-SOXS.md" title="открыть в Obsidian" style="color:#58a6ff;text-decoration:none">SOXL/SOXS</a><a href="https://github.com/RomanBoreyko/R_proj/blob/main/R-trade/R_trade_proj_sin-arb/TRADING-SYSTEM/11-ASSETS/SOXL-SOXS.md" target="_blank" title="на GitHub" style="font-size:10px;color:#8b949e;text-decoration:none;margin:0 10px 0 3px">gh</a><a href="obsidian://open?path=D%3A%5CVault%5CProject%5CR_vault%5CR-trade%5CR_trade_proj_sin-arb%5CTRADING-SYSTEM%5C10-DASHBOARD%5C%D0%92%D0%BA%D0%BB%D0%B0%D0%B4%D0%BA%D0%B0%20%D0%94%D0%B5%D1%81%D0%BA.md" title="открыть в Obsidian" style="color:#58a6ff;text-decoration:none">Как читать деск</a><a href="https://github.com/RomanBoreyko/R_proj/blob/main/R-trade/R_trade_proj_sin-arb/TRADING-SYSTEM/10-DASHBOARD/%D0%92%D0%BA%D0%BB%D0%B0%D0%B4%D0%BA%D0%B0%20%D0%94%D0%B5%D1%81%D0%BA.md" target="_blank" title="на GitHub" style="font-size:10px;color:#8b949e;text-decoration:none;margin:0 10px 0 3px">gh</a><a href="obsidian://open?path=D%3A%5CVault%5CProject%5CR_vault%5CR-trade%5CR_trade_proj_sin-arb%5CTRADING-SYSTEM%5Cscripts%5Cportfolio%5CDESK.md" title="открыть в Obsidian" style="color:#58a6ff;text-decoration:none">Техдока и формулы</a><a href="https://github.com/RomanBoreyko/R_proj/blob/main/R-trade/R_trade_proj_sin-arb/TRADING-SYSTEM/scripts/portfolio/DESK.md" target="_blank" title="на GitHub" style="font-size:10px;color:#8b949e;text-decoration:none;margin:0 10px 0 3px">gh</a><a href="obsidian://open?path=D%3A%5CVault%5CProject%5CR_vault%5CR-trade%5CR_trade_proj_sin-arb%5CTRADING-SYSTEM%5C00-INDEX.md" title="открыть в Obsidian" style="color:#58a6ff;text-decoration:none">Индекс</a><a href="https://github.com/RomanBoreyko/R_proj/blob/main/R-trade/R_trade_proj_sin-arb/TRADING-SYSTEM/00-INDEX.md" target="_blank" title="на GitHub" style="font-size:10px;color:#8b949e;text-decoration:none;margin:0 10px 0 3px">gh</a></div>
<details style="font-size:12px;margin:0 0 4px"><summary style="cursor:pointer;color:#8b949e">Ещё: модули курса, архив, Bybit, старая версия</summary><div id="dklinks" style="font-size:12px;margin:0 0 6px;line-height:1.9"><a href="/old" style="color:#d29922;text-decoration:none;margin-right:14px">◂ Старая версия (все вкладки)</a><span style="color:#8b949e;margin-right:6px">Курс:</span><a href="obsidian://open?path=D%3A%5CVault%5CProject%5CR_vault%5CR-trade%5CR_trade_proj_sin-arb%5CTRADING-SYSTEM%5C13-LEARNING%5C%D0%9E%D0%BF%D0%B5%D1%80%D0%B0%D1%82%D0%BE%D1%80%20%D1%81%D0%BF%D1%80%D0%B5%D0%B4%D0%B0%5C%D0%9E%D0%A1-01%20%D0%9F%D0%BB%D0%B5%D1%87%D0%B5%D0%B2%D1%8B%D0%B5%20ETF%20%D0%B8%20%D1%8F%D0%BA%D0%BE%D1%80%D1%8C%20%D0%B4%D0%BD%D1%8F.md" title="открыть в Obsidian" style="color:#58a6ff;text-decoration:none">1 Якорь дня</a><a href="https://github.com/RomanBoreyko/R_proj/blob/main/R-trade/R_trade_proj_sin-arb/TRADING-SYSTEM/13-LEARNING/%D0%9E%D0%BF%D0%B5%D1%80%D0%B0%D1%82%D0%BE%D1%80%20%D1%81%D0%BF%D1%80%D0%B5%D0%B4%D0%B0/%D0%9E%D0%A1-01%20%D0%9F%D0%BB%D0%B5%D1%87%D0%B5%D0%B2%D1%8B%D0%B5%20ETF%20%D0%B8%20%D1%8F%D0%BA%D0%BE%D1%80%D1%8C%20%D0%B4%D0%BD%D1%8F.md" target="_blank" title="на GitHub" style="font-size:10px;color:#8b949e;text-decoration:none;margin:0 10px 0 3px">gh</a><a href="obsidian://open?path=D%3A%5CVault%5CProject%5CR_vault%5CR-trade%5CR_trade_proj_sin-arb%5CTRADING-SYSTEM%5C13-LEARNING%5C%D0%9E%D0%BF%D0%B5%D1%80%D0%B0%D1%82%D0%BE%D1%80%20%D1%81%D0%BF%D1%80%D0%B5%D0%B4%D0%B0%5C%D0%9E%D0%A1-02%20%D0%9F%D0%B5%D1%80%D0%BF%D1%8B%20Bybit%20%D0%BD%D0%B0%20ETF.md" title="открыть в Obsidian" style="color:#58a6ff;text-decoration:none">2 Перпы</a><a href="https://github.com/RomanBoreyko/R_proj/blob/main/R-trade/R_trade_proj_sin-arb/TRADING-SYSTEM/13-LEARNING/%D0%9E%D0%BF%D0%B5%D1%80%D0%B0%D1%82%D0%BE%D1%80%20%D1%81%D0%BF%D1%80%D0%B5%D0%B4%D0%B0/%D0%9E%D0%A1-02%20%D0%9F%D0%B5%D1%80%D0%BF%D1%8B%20Bybit%20%D0%BD%D0%B0%20ETF.md" target="_blank" title="на GitHub" style="font-size:10px;color:#8b949e;text-decoration:none;margin:0 10px 0 3px">gh</a><a href="obsidian://open?path=D%3A%5CVault%5CProject%5CR_vault%5CR-trade%5CR_trade_proj_sin-arb%5CTRADING-SYSTEM%5C13-LEARNING%5C%D0%9E%D0%BF%D0%B5%D1%80%D0%B0%D1%82%D0%BE%D1%80%20%D1%81%D0%BF%D1%80%D0%B5%D0%B4%D0%B0%5C%D0%9E%D0%A1-03%20%D0%94%D0%B5%D0%BB%D1%8C%D1%82%D0%B0%20%D0%BF%D0%B0%D1%80%D1%8B.md" title="открыть в Obsidian" style="color:#58a6ff;text-decoration:none">3 Дельта</a><a href="https://github.com/RomanBoreyko/R_proj/blob/main/R-trade/R_trade_proj_sin-arb/TRADING-SYSTEM/13-LEARNING/%D0%9E%D0%BF%D0%B5%D1%80%D0%B0%D1%82%D0%BE%D1%80%20%D1%81%D0%BF%D1%80%D0%B5%D0%B4%D0%B0/%D0%9E%D0%A1-03%20%D0%94%D0%B5%D0%BB%D1%8C%D1%82%D0%B0%20%D0%BF%D0%B0%D1%80%D1%8B.md" target="_blank" title="на GitHub" style="font-size:10px;color:#8b949e;text-decoration:none;margin:0 10px 0 3px">gh</a><a href="obsidian://open?path=D%3A%5CVault%5CProject%5CR_vault%5CR-trade%5CR_trade_proj_sin-arb%5CTRADING-SYSTEM%5C13-LEARNING%5C%D0%9E%D0%BF%D0%B5%D1%80%D0%B0%D1%82%D0%BE%D1%80%20%D1%81%D0%BF%D1%80%D0%B5%D0%B4%D0%B0%5C%D0%9E%D0%A1-04%20%D0%93%D0%B0%D0%BC%D0%BC%D0%B0%20%D0%B8%20%D1%82%D0%B5%D1%82%D0%B0%20%D0%BF%D0%B0%D1%80%D1%8B.md" title="открыть в Obsidian" style="color:#58a6ff;text-decoration:none">4 Гамма и тета</a><a href="https://github.com/RomanBoreyko/R_proj/blob/main/R-trade/R_trade_proj_sin-arb/TRADING-SYSTEM/13-LEARNING/%D0%9E%D0%BF%D0%B5%D1%80%D0%B0%D1%82%D0%BE%D1%80%20%D1%81%D0%BF%D1%80%D0%B5%D0%B4%D0%B0/%D0%9E%D0%A1-04%20%D0%93%D0%B0%D0%BC%D0%BC%D0%B0%20%D0%B8%20%D1%82%D0%B5%D1%82%D0%B0%20%D0%BF%D0%B0%D1%80%D1%8B.md" target="_blank" title="на GitHub" style="font-size:10px;color:#8b949e;text-decoration:none;margin:0 10px 0 3px">gh</a><a href="obsidian://open?path=D%3A%5CVault%5CProject%5CR_vault%5CR-trade%5CR_trade_proj_sin-arb%5CTRADING-SYSTEM%5C13-LEARNING%5C%D0%9E%D0%BF%D0%B5%D1%80%D0%B0%D1%82%D0%BE%D1%80%20%D1%81%D0%BF%D1%80%D0%B5%D0%B4%D0%B0%5C%D0%9E%D0%A1-05%20%D0%9E%D0%BF%D1%86%D0%B8%D0%BE%D0%BD%D1%8B%20SOXL.md" title="открыть в Obsidian" style="color:#58a6ff;text-decoration:none">5 Опционы</a><a href="https://github.com/RomanBoreyko/R_proj/blob/main/R-trade/R_trade_proj_sin-arb/TRADING-SYSTEM/13-LEARNING/%D0%9E%D0%BF%D0%B5%D1%80%D0%B0%D1%82%D0%BE%D1%80%20%D1%81%D0%BF%D1%80%D0%B5%D0%B4%D0%B0/%D0%9E%D0%A1-05%20%D0%9E%D0%BF%D1%86%D0%B8%D0%BE%D0%BD%D1%8B%20SOXL.md" target="_blank" title="на GitHub" style="font-size:10px;color:#8b949e;text-decoration:none;margin:0 10px 0 3px">gh</a><a href="obsidian://open?path=D%3A%5CVault%5CProject%5CR_vault%5CR-trade%5CR_trade_proj_sin-arb%5CTRADING-SYSTEM%5C13-LEARNING%5C%D0%9E%D0%BF%D0%B5%D1%80%D0%B0%D1%82%D0%BE%D1%80%20%D1%81%D0%BF%D1%80%D0%B5%D0%B4%D0%B0%5C%D0%9E%D0%A1-06%20%D0%9C%D0%B0%D1%80%D0%B6%D0%B0%20Portfolio%20Margin.md" title="открыть в Obsidian" style="color:#58a6ff;text-decoration:none">6 Маржа</a><a href="https://github.com/RomanBoreyko/R_proj/blob/main/R-trade/R_trade_proj_sin-arb/TRADING-SYSTEM/13-LEARNING/%D0%9E%D0%BF%D0%B5%D1%80%D0%B0%D1%82%D0%BE%D1%80%20%D1%81%D0%BF%D1%80%D0%B5%D0%B4%D0%B0/%D0%9E%D0%A1-06%20%D0%9C%D0%B0%D1%80%D0%B6%D0%B0%20Portfolio%20Margin.md" target="_blank" title="на GitHub" style="font-size:10px;color:#8b949e;text-decoration:none;margin:0 10px 0 3px">gh</a><a href="obsidian://open?path=D%3A%5CVault%5CProject%5CR_vault%5CR-trade%5CR_trade_proj_sin-arb%5CTRADING-SYSTEM%5C13-LEARNING%5C%D0%9E%D0%BF%D0%B5%D1%80%D0%B0%D1%82%D0%BE%D1%80%20%D1%81%D0%BF%D1%80%D0%B5%D0%B4%D0%B0%5C%D0%9E%D0%A1-07%20%D0%94%D0%B2%D0%B0%20%D1%81%D0%BB%D0%BE%D1%8F%20%D1%81%D0%BA%D0%B0%D0%BB%D1%8C%D0%BF%D0%B0.md" title="открыть в Obsidian" style="color:#58a6ff;text-decoration:none">7 Скальп</a><a href="https://github.com/RomanBoreyko/R_proj/blob/main/R-trade/R_trade_proj_sin-arb/TRADING-SYSTEM/13-LEARNING/%D0%9E%D0%BF%D0%B5%D1%80%D0%B0%D1%82%D0%BE%D1%80%20%D1%81%D0%BF%D1%80%D0%B5%D0%B4%D0%B0/%D0%9E%D0%A1-07%20%D0%94%D0%B2%D0%B0%20%D1%81%D0%BB%D0%BE%D1%8F%20%D1%81%D0%BA%D0%B0%D0%BB%D1%8C%D0%BF%D0%B0.md" target="_blank" title="на GitHub" style="font-size:10px;color:#8b949e;text-decoration:none;margin:0 10px 0 3px">gh</a><a href="obsidian://open?path=D%3A%5CVault%5CProject%5CR_vault%5CR-trade%5CR_trade_proj_sin-arb%5CTRADING-SYSTEM%5C13-LEARNING%5C%D0%9E%D0%BF%D0%B5%D1%80%D0%B0%D1%82%D0%BE%D1%80%20%D1%81%D0%BF%D1%80%D0%B5%D0%B4%D0%B0%5C%D0%9E%D0%A1-08%20%D0%A0%D0%B8%D1%81%D0%BA%D0%B8%20%D0%B8%20%D1%83%D1%80%D0%BE%D0%BA%D0%B8.md" title="открыть в Obsidian" style="color:#58a6ff;text-decoration:none">8 Риски и уроки</a><a href="https://github.com/RomanBoreyko/R_proj/blob/main/R-trade/R_trade_proj_sin-arb/TRADING-SYSTEM/13-LEARNING/%D0%9E%D0%BF%D0%B5%D1%80%D0%B0%D1%82%D0%BE%D1%80%20%D1%81%D0%BF%D1%80%D0%B5%D0%B4%D0%B0/%D0%9E%D0%A1-08%20%D0%A0%D0%B8%D1%81%D0%BA%D0%B8%20%D0%B8%20%D1%83%D1%80%D0%BE%D0%BA%D0%B8.md" target="_blank" title="на GitHub" style="font-size:10px;color:#8b949e;text-decoration:none;margin:0 10px 0 3px">gh</a><br><span style="color:#8b949e;margin-right:6px">Ещё:</span><a href="obsidian://open?path=D%3A%5CVault%5CProject%5CR_vault%5CR-trade%5CR_trade_proj_sin-arb%5CCLAUDE.md" title="открыть в Obsidian" style="color:#58a6ff;text-decoration:none">Инструкция проекта</a><a href="https://github.com/RomanBoreyko/R_proj/blob/main/R-trade/R_trade_proj_sin-arb/CLAUDE.md" target="_blank" title="на GitHub" style="font-size:10px;color:#8b949e;text-decoration:none;margin:0 10px 0 3px">gh</a><a href="obsidian://open?path=D%3A%5CVault%5CProject%5CR_vault%5C_coord%5CCHAT.md" title="открыть в Obsidian" style="color:#58a6ff;text-decoration:none">Чат нейронок</a><a href="https://github.com/RomanBoreyko/R_proj/blob/main/_coord/CHAT.md" target="_blank" title="на GitHub" style="font-size:10px;color:#8b949e;text-decoration:none;margin:0 10px 0 3px">gh</a><a href="obsidian://open?path=D%3A%5CVault%5CProject%5CR_vault%5CR-trade%5CR_trade_proj_sin-arb%5CTRADING-SYSTEM%5C07-LINKS%5Cdocumentation.md" title="открыть в Obsidian" style="color:#58a6ff;text-decoration:none">Ссылки: проспект, Bybit, API</a><a href="https://github.com/RomanBoreyko/R_proj/blob/main/R-trade/R_trade_proj_sin-arb/TRADING-SYSTEM/07-LINKS/documentation.md" target="_blank" title="на GitHub" style="font-size:10px;color:#8b949e;text-decoration:none;margin:0 10px 0 3px">gh</a><a href="obsidian://open?path=D%3A%5CVault%5CProject%5CR_vault%5CR-trade%5CR_trade_proj_sin-arb%5CTRADING-SYSTEM%5C09-INFRASTRUCTURE.md" title="открыть в Obsidian" style="color:#58a6ff;text-decoration:none">Стек</a><a href="https://github.com/RomanBoreyko/R_proj/blob/main/R-trade/R_trade_proj_sin-arb/TRADING-SYSTEM/09-INFRASTRUCTURE.md" target="_blank" title="на GitHub" style="font-size:10px;color:#8b949e;text-decoration:none;margin:0 10px 0 3px">gh</a><a href="obsidian://open?path=D%3A%5CVault%5CProject%5CR_vault%5CR-trade%5CR_trade_proj_sin-arb%5CTRADING-SYSTEM%5C99-%D0%90%D0%A0%D0%A5%D0%98%D0%92%5CREADME.md" title="открыть в Obsidian" style="color:#58a6ff;text-decoration:none">Архив (что и как вернуть)</a><a href="https://github.com/RomanBoreyko/R_proj/blob/main/R-trade/R_trade_proj_sin-arb/TRADING-SYSTEM/99-%D0%90%D0%A0%D0%A5%D0%98%D0%92/README.md" target="_blank" title="на GitHub" style="font-size:10px;color:#8b949e;text-decoration:none;margin:0 10px 0 3px">gh</a><a href="https://www.bybit.com/en/help-center/article/Introduction-to-Portfolio-Margin" target="_blank" style="color:#58a6ff;text-decoration:none;margin-right:10px">Bybit: Portfolio Margin ↗</a><a href="https://bybit-exchange.github.io/docs/v5/asset/portfolio-margin" target="_blank" style="color:#58a6ff;text-decoration:none;margin-right:10px">API portfolio-margin ↗</a></div></details>
<div id="dkhead"><div id="dkalerts"></div><div id="dktiles"></div></div>
<div id="dksw" style="margin:4px 0 6px"><span class="gbtn on" id="dkswD" onclick="dkView('D')">Деск</span><span class="gbtn" id="dkswB" onclick="dkView('B')">Конструктор позиции</span></div>
<div id="dkmain">
<h3>Что сделать сейчас</h3>
<div id="dktodo" style="font-size:13px;margin:2px 0 8px"></div>
<h3>Гамма-скальп SOXL / SOXS: сетка хеджа, учёт, опционы у денег</h3>
<div id="dkgam" style="font-size:12px;margin:2px 0 8px"></div>
<h3>Маржа спреда SOXL / SOXS — сценарии Bybit (цена × IV), худший убыток на каждый сдвиг цены</h3>
<table id="dkmargin"></table>
<h3>Дельта по позициям и конструкциям: SOXL + SOXS</h3>
<table id="dkdpos"></table>
<div id="dkethsec" style="display:none">
<h3>ETH: P&L книги от цены (как в конструкторе Bybit)</h3>
<canvas id="dkeplot" width="1100" height="280"></canvas>
<h3>ETH: ноги</h3>
<table id="dkelegs"></table>
<table id="dkeperps" style="margin-top:6px"></table>
<div style="display:flex;gap:28px;flex-wrap:wrap;margin-top:6px">
 <div><h3>Стресс ETH-книги: цена × сдвиг IV (все ноги одинаково), $</h3><table id="dkestress"></table></div>
 <div><h3>Хвост проданных путов по датам экспираций</h3><table id="dketail"></table></div>
</div>
</div>
<div id="dkchartsec" style="display:none">
<h3>TradingView: спред SOXL / SOXS</h3>
<div style="font-size:12px;margin:2px 0 4px" id="dktv_ctl"></div>
<div id="dktv_box" style="height:560px;max-width:1400px;border:1px solid #2a2e39;border-radius:4px;background:#131722;margin-bottom:8px"></div>
<h3>Спред SOXL / SOXS по перпам Bybit — как в TradingView, с моими сделками, позициями и заявками</h3>
<div style="font-size:12px;margin:2px 0 4px" id="dkpcctl"></div>
<canvas id="dkpc" width="1400" height="640" style="width:100%;max-width:1400px;border:1px solid #2a2e39;border-radius:4px;background:#131722;display:block;margin-bottom:4px;cursor:crosshair"></canvas>
<div id="dkpcnote" style="font-size:12px;margin:0 0 8px"></div>
</div>
<h3>Спред SOXL / SOXS: P&L книги от цены SOXL (SOXS пересчитан через β)</h3>
<canvas id="dksplot" width="1100" height="280"></canvas>
<h3>Спред SOXL / SOXS: ноги</h3>
<table id="dksperps"></table>
<table id="dkslegs" style="margin-top:6px"></table>
<div id="dkspseudo" style="font-size:12px;margin:6px 0"></div>
<div style="display:flex;gap:28px;flex-wrap:wrap">
 <div><h3>Тета по дням (до 10:00 Киева, при неизменных цене и IV), $</h3><table id="dktheta"></table></div>
 <div style="display:none"><h3>Плечи</h3><table id="dklev"></table></div>
</div>
<div style="color:#8b949e;font-size:11px;margin:8px 0 10px">Только чтение. Маржа и сценарии — из Bybit <code>/v5/asset/portfolio-margin</code> (MM = худший сценарий + надбавки; IM ≈ 1.2×MM). <b>Запас</b> = эквити − MM: на столько может упасть эквити до ликвидации. «Закрыть → маржа» — как изменится худший сценарий Bybit по ETH, если закрыть ногу целиком (+ освободит, − добавит; надбавки не пересчитаны, ±несколько $). Дельта SOX — в SOXL-эквиваленте: Δ SOXL·цена − SOXS·цена·|β| (β — дневные свечи 120 дн). Псевдо-стренгл пары перпов: P&L ≈ 4.5·N·(X² − Σx²день), Γ$ — при ходе индекса на X за срок удержания, распад = 4.5·N·σ²день. Стресс ETH и тета — Блэк–Шоулз по mark IV ног; перп и спот — линейно. Хвост: что останется после каждой экспирации и выплата на экспирации при ETH −50% и −70% от текущей.</div>
</div>
<div id="dkbuild" style="display:none">
<div style="margin:2px 0;font-size:12px"><span class="gbtn" id="bldcm1" onclick="bldCompactSet(1)">компактно</span><span class="gbtn" id="bldcm0" onclick="bldCompactSet(0)">все плитки</span></div>
<div style="font-size:12px;color:#8b949e;margin:2px 0" id="bldupd"></div>
<div id="bldmm" style="margin:4px 0"></div>
<div class="row" id="bldtop"></div>
<div id="bldbots"></div>
<div id="bldctl"></div>
<table id="bldtab" style="margin-top:4px"></table>
<div style="display:flex;gap:14px;flex-wrap:wrap;margin-top:8px">
 <div style="flex:1 1 640px"><h3>P&L книги и вероятность</h3><canvas id="bldpnl" width="980" height="420" style="width:100%;border:1px solid #21262d;border-radius:4px"></canvas><div id="bldpnlnote" style="font-size:12px;margin:4px 0"></div></div>
 <div style="flex:1 1 460px"><h3>Индекс SOXL</h3><div id="bldkctl" style="font-size:12px;margin-bottom:4px"></div><canvas id="bldk" width="760" height="420" style="width:100%;border:1px solid #21262d;border-radius:4px"></canvas></div>
</div>
<h3>Параметры спреда</h3><div id="bldpar"></div>
<h3>Арбитражные калькуляторы</h3><div id="bldarb"></div>
<h3>Калькуляторы без прогноза</h3><div id="bldnoarb"></div>
<h3>(Е) Калькулятор теты — новые ноги и путь к экспирации</h3><div id="bldth"></div>
<h3>Квесты арбитражника</h3><div id="bldq"></div>
<h3>Как пользоваться вкладкой (для оператора спреда)</h3>
<div style="font-size:12px;line-height:1.6;max-width:1200px">
<p><b>Что это.</b> Конструктор позиции как на Bybit (Tools → Options → конструктор), но со вторым инструментом спреда — SOXS — и с параметрами пары. Все цифры — из API Bybit (только чтение) и публичного API MEXC. Ордера отсюда не ставятся: кнопки меняют только симуляцию.</p>
<p><b>Плашка маркетмейкера (самый верх).</b> Доля опционов у денег с двусторонней котировкой, норма ~90%. Красная — маркетмейкер ушёл: стакан опционов пустой, лимитки стоят без встречной стороны, а бид/аск и P&amp;L считаются по пустому стакану. Ниже — журнал уходов (сервер пишет его сам раз в 15 с).</p>
<p><b>Квесты арбитражника (внизу).</b> Двенадцать квестов из ОС-12. По каждому — где смотреть, что делать, критерий успеха и живые цифры «сейчас». Зелёная точка — квест актуален в это время (открытие или закрытие США, выходные, маркетмейкер ушёл). «Записать снимок» кладёт текущие цифры в журнал браузера, «копировать как Markdown» — строки для «Журнала квестов» ОС-12. Галочка — квест пройден.</p>
<p><b>Компактно / все плитки.</b> «Компактно» прячет шапку деска и оставляет три строки: <b>Риск</b> (эквити и запас до ликвидации, IM, худшая маржа SOX, P&L ног), <b>Книга</b> (Δ по зеркалу, хедж с порогом $40, Γ, Θ с фандингом, вега, ожидание в сутки, RV/IV), <b>Время</b> (цены, сессия США, ближайшая экспирация). Строка бота — 5 плиток. «Все плитки» — шапка деска и под ней строка конструктора без дублей: цены MEXC, Δ только SOXL (как Bybit), Γ/вега/Θ, P&amp;L ног; Δ и хедж «с изменениями» — только если ноги сняты, изменены или добавлены. Выбор запоминается в браузере.</p>
<p><b>Боты (строка под плитками).</b> Фьючерсные сетки и мартингейлы Bybit из файла <code>C:/Users/DoOs/.bybit/bots.json</code> (как добавить — 10-DASHBOARD/Боты в конструкторе.md). Первая плитка — сигналы: вне диапазона, засажен, риск ликвидации, пора доливать. «Засажен» — доля сетки, набранная в позицию. «Ликвидация сейчас» — от Bybit по текущей позиции; «на краю» — оценка, если цена дойдёт до края и позиция наберётся целиком. «Доливка» — куда уйдёт ликвидация, если долить весь свободный USDT с Funding. Пороги — в bots.json (доливка 15%, внимание 25%).</p>
<p><b>Калькулятор теты (Е).</b> Две ноги — по умолчанию коллар: купить пут у денег, продать колл на $3 выше, экспирация 4DEC. Цена пустая — текущий аск/бид; впиши цену своей лимитки. Первая таблица — премия, Δ$, гамма, Θ и вега новых ног по грекам Bybit и книга до/после (Θ с фандингом, лимит −$10 в сутки). Вторая — как растёт Θ новых ног к экспирации при разных ценах SOXL, Θ всей книги и сколько новые ноги потеряют или заработают на времени к этой дате.</p>
<p><b>Плитки сверху.</b> Эквити (и сколько это акций SOXL), IM/MM счёта, цены SOXL и SOXS на Bybit и MEXC. <b>Δ только SOXL</b> — как считает Bybit (без SOXS). <b>Δ с SOXS по зеркалу</b> — правильная дельта пары внутри дня: лонг SOXS = шорт SOXL на S0/L0 акций (база — прошлое закрытие США); в подписи — дельта после закрытия. <b>Хедж дельты</b> — сколько SOXLUSDT купить/продать, чтобы обнулить дельту. Γ/вега/Θ — сумма по включённым ногам.</p>
<p><b>Таблица ног.</b> Твои позиции Bybit: перпы SOXL/SOXS и опционы SOXL. Бид/аск с размером и IV, P&L, греки на позицию (для SOXS Δ в SOXL-эквиваленте).
<br>• <b>Галочка</b> — включить/выключить ногу в расчётах (что будет, если её закрыть).
<br>• <b>Кол-во</b> — поменять объём: добавка считается по текущему аску (покупка) или биду (продажа).
<br>• <b>Добавить ногу</b> — опцион из цепочки или перп; «сим.» — симулированная нога, ✕ убирает. «Сбросить» возвращает реальную позицию.</p>
<p><b>График P&L.</b> Зелёная линия — P&L книги на выбранный горизонт (ползунок «горизонт», 0 = сейчас), оранжевый пунктир — на последней экспирации опционов. SOXS при горизонте 0 — по зеркалу (как внутри дня), при горизонте ≥ 1 дня — по многодневной β. Точки на нуле — безубытки. Синяя заливка — распределение цены SOXL по IV (логнормаль без дрейфа). Под графиком: <b>вероятность прибыли</b>, ожидание P&L по IV и худший P&L в диапазоне. Наведи мышь — значения в точке. Диапазон цены задаётся «от / до × текущей».</p>
<p><b>Индекс SOXL.</b> Свечи индексной цены Bybit с MA7/14/28, интервалы 15м…неделя.</p>
<p><b>Параметры спреда.</b> База зеркала и текущее отклонение (норма ~10 бп), β (дни и часы), факторы (SOXX, QQQ, NVDA), RV по окнам, IV ATM по экспирациям, variance ratio (тренд/пила), безубыточный ход гаммы, фандинг Bybit и MEXC, комиссии, сессия США.</p>
<p><b>Арбитражные калькуляторы.</b>
<br>(а) <b>синтетика против перпа</b>: синтетический фьючерс K + колл − пут сравнивается с перпом Bybit и MEXC; конверсия и реверсия с комиссиями и фандингом до экспирации; подсвечена строка, где после всех издержек остаётся плюс.
<br>(б) <b>Bybit ↔ MEXC</b>: разница цен одного перпа на двух биржах в бп и в $ на номинал, после двух кругов комиссий; разница фандинга.
<br>(в) <b>фандинг-кэрри</b>: доход от фандинга за срок, годовых, окупаемость комиссий. Спота SOXL на Bybit/MEXC нет — реальна только связка перп/перп между биржами.
<br>(г) <b>зеркало SOXL ↔ SOXS</b>: отклонение SOXS от зеркала в бп и σ, сколько принесёт схождение, какую Δ-нейтральную пару сделок сделать.</p>
<p><b>Без прогноза.</b> (Г) <b>риск перпа на опцион 1:1</b>: лестница усреднения перп + пут на каждом уровне, премии, худший итог на экспирации в $ и % эквити. (Д) <b>нейтрализация дельты опционами</b>: сколько путов/коллов купить, чтобы погасить дельту книги, и влезает ли премия в лимит % эквити.</p>
<p><b>Порядок работы.</b> 1) Сверь плитку «Δ с SOXS по зеркалу» с коридором ±$40. 2) Перед сделкой добавь её симулированной ногой и посмотри дельту, P&L-кривую и вероятность. 3) Раз в день пройди калькуляторы (а)–(г): подсвеченная строка — повод проверить стакан вручную. 4) Помни: маржу Portfolio Margin симуляция не считает — для неё деск, «Маржа спреда».</p>
<p style="color:#8b949e">Оговорки: греки и IV — Bybit (по mark); P&L на горизонте — Блэк–Шоулз при неизменной IV; вероятность — модель без дрейфа и без «толстых хвостов»; калькуляторы не видят глубину стакана и проскальзывание. Курс: <b>ОС-10…12</b> (Ло для арбитражника, карта заработков, гипотезы и квесты).</p>
</div>
</div>
</div>
<div id="tabL" style="display:none">
<h3>Мои шиткоины: позиции, сетки, фандинг</h3>
<div style="font-size:12px;margin:2px 0 4px" id="gactl"></div>
<div class="row" id="gatop"></div>
<table id="gatab"></table>
<div style="color:#8b949e;font-size:11px;margin:2px 0 10px">Крипто-перпы USDT, кроме ETH, BTC, SOL, MNT, золота и TradFi. Символ попадает сюда, если есть открытая позиция, ≥ 20 исполнений за окно или ≥ 10 заявок. <b>Фандинг $/сутки</b> — по текущей ставке на текущую позицию (+ получаю, − плачу); у шиткоинов ставка меняется каждый час и бывает ±1–2% в час.</div>
<h3>Лаборатория ёрша: что дала бы схема на этом перпе (минутные свечи, фандинг, мои комиссии)</h3>
<div style="font-size:12px;margin:2px 0 4px" id="alctl"></div>
<div id="alwarn" style="font-size:12px;margin:2px 0 4px"></div>
<div class="row" id="altop"></div>
<canvas id="alc" width="1200" height="240" style="max-width:100%;border:1px solid #30363d;border-radius:6px;background:#0d1117"></canvas>
<table id="alcmp" style="margin-top:6px"></table>
<div style="color:#8b949e;font-size:11px;margin:4px 0 10px"><b>Схемы.</b> «Ядро + шорт по шкале»: лонг-ядро L монет, шорт растёт от 0 у нижней границы до L у верхней — чистая позиция от полного лонга до нуля (как покрытый колл: рост ограничен, падение полное; ядро — спот без фандинга или перп с фандингом). «Нейтральная сетка» — как нейтральный фьючерсный грид-бот Bybit: от +L у низа до −L у верха. «Трендовая» — наоборот: покупает рост и продаёт падение (зарабатывает на тренде, платит за пилу). Коридор по умолчанию — минимум и максимум окна (это подглядывание в будущее: в реальности границы ставятся заранее — задай свои). Уровней N: при касании уровня позиция доводится до целевой лимиткой по цене уровня, комиссия — мейкер аккаунта по этому символу. Путь внутри минуты восстановлен по свече, исполнение на касании считается гарантированным — оценка оптимистичная. «Держать старт» — если бы стартовую позицию просто держали без сетки; «эдж сетки» = сетка − держать. Фандинг — по истории ставок на позицию в момент списания. Только чтение, не прогноз.</div>
</div>
<div id="tabR" style="display:none">
<h3>Ерши MEXC ↔ Bybit: мелкие монеты-перпы на обеих биржах — расхождение цены и фандинга, болтанка, стакан, комиссии. ТОЛЬКО МОНИТОРИНГ</h3>
<div class="row" id="rtop"></div>
<h3>График TradingView: спред между биржами (Bybit / MEXC)</h3>
<div style="font-size:12px;margin:2px 0 4px" id="rtvctl"></div>
<div id="rtv" style="height:480px;max-width:1200px;border:1px solid #30363d;border-radius:6px"></div>
<h3>Секундный спред Bybit ↔ MEXC (свой сэмплер, bid/ask раз в 2 с)</h3>
<div style="font-size:12px;margin:2px 0 4px" id="rtkctl"></div>
<div class="row" id="rtktop"></div>
<canvas id="rtkp" width="1400" height="620" style="width:100%;max-width:1400px;border:1px solid #2a2e39;border-radius:4px;background:#131722;display:block;margin-bottom:6px;cursor:crosshair"></canvas>
<div style="color:#8b949e;font-size:11px;margin:4px 0 2px">Исполнимый эдж (bid/ask) — свечи разницы mid-цен и точки выше комиссий:</div>
<canvas id="rtkc" width="1200" height="220" style="max-width:100%;border:1px solid #30363d;border-radius:6px;background:#0d1117"></canvas>
<div style="color:#8b949e;font-size:11px;margin:2px 0 8px">Большой график — как на TradingView: цены последних сделок <span style="color:#2962ff">Bybit</span> и <span style="color:#22c55e">MEXC</span>, объём Bybit, внизу <span style="color:#f7525f">красная линия</span> — отношение последних цен сделок BYBIT/MEXC − 1 на закрытии бара (то же, что спред-график на TradingView, собрано из сделок самих бирж: Bybit recent-trade, MEXC deals). Свечи — разница mid-цен (Bybit/MEXC − 1), % по барам выбранной длины (как отношение BYBIT/MEXC на TradingView). Точки — исполнимый эдж внутри бара: <span style="color:#3fb950">зелёная сверху</span> = купить на MEXC по ask и продать на Bybit по bid, <span style="color:#58a6ff">синяя снизу</span> = купить на Bybit и продать на MEXC; точка появляется, только когда эдж выше суммы тейкеров обеих бирж (пунктир ±комиссии). Проскальзывание и глубина стакана не учтены. История живёт в памяти сервера (~2 ч, до 4 монет — что недавно открывали) и сбрасывается при перезапуске. TradingView в виджете секундных интервалов не даёт — они только в платном аккаунте на самом сайте.</div>
<div style="color:#8b949e;font-size:11px;margin:2px 0 8px">Отношение цен перпов: 1.00 = цены равны, 1.01 = Bybit на 1% дороже MEXC. Клик по строке таблицы ниже — открыть график этой монеты. Виджет TradingView грузится с s3.tradingview.com.</div>
<div style="font-size:12px;margin:4px 0" id="rctl"></div>
<div id="rbar" style="color:#8b949e;font-size:11px;margin:2px 0"></div>
<table id="rtab"></table>
<div style="color:#8b949e;font-size:11px;margin-top:6px">Пары: перп USDT на Bybit (XUSDT) и на MEXC (X_USDT); монеты с разным множителем (1000X) отброшены. «Ёрш» — оборот Bybit $0.3–80 млн за 24 ч. Разница — mid MEXC к mid Bybit. Эдж — исполнимый сейчас: купить по ask дешёвой биржи, продать по bid дорогой; «чистый» — минус тейкер обеих (Bybit — ставка аккаунта, MEXC — публичная по контракту); «фанд. 8ч в направлении» — сколько за 8 ч заплатит лонг и получит шорт; «чистый + фанд.» — итог за первые 8 ч (огромный фандинг съедает ценовой эдж быстрее, чем сходятся цены). Глубина стакана не учтена — объём на лучших ценах у ершей часто маленький. Фандинг приведён к % за 8 ч (период Bybit из инструментов, MEXC — из API, у непроверенных принят 8 ч). Болтанка (по 1-часовым свечам Bybit за 48 ч): путь/смещение — сколько раз суммарный ход больше итогового (больше = сильнее мечется), пересечения средней. Перевод между биржами (сети, комиссии вывода, статус) — только через приватные API, здесь не показан. Это не арбитраж «без риска»: цены могут разойтись сильнее, вывод может быть закрыт, ликвидность исчезнуть.</div>
</div>
<div id="tabF" style="display:none">
<h3>TradFi на Bybit: акции, ETF, сырьё, форекс — ликвидность, волатильность, комиссии (только чтение)</h3>
<div class="row" id="ftop"></div>
<div style="font-size:12px;margin:4px 0" id="fctl"></div>
<div id="fbar" style="color:#8b949e;font-size:11px;margin:2px 0"></div>
<table id="ftab"></table>
<div style="color:#8b949e;font-size:11px;margin-top:6px">Оборот и OI — за 24 ч, в $. Спред — (ask−bid)/mid сейчас. ATR14 — средний истинный диапазон дневных свечей за 14 дней, % цены (фоновое обновление раз в 30 мин). «ATR в спредах» = ATR% / спред%: сколько спредов помещается в обычный дневной ход — чем больше, тем дешевле ловить движение (для сетки мейкером при нулевой комиссии важнее ATR и глубина, для тейкера — спред). Мейкер/тейкер — ставки ТВОЕГО аккаунта (/v5/account/fee-rate). TradFi-перпы следуют часам своего рынка: в выходные и ночью котировки могут стоять или идти по внебиржевой цене. «Самый волатильный» и «лучший для сетки» — только среди оборота ≥ $1 млн.</div>
</div>
<div id="tabT" style="display:none">
<div class="row" id="tacct"></div>
<div id="tliq" style="color:#8b949e;font-size:11px;margin:0 0 4px"></div>
<details id="tordbox" style="margin-top:4px"><summary id="tordh" style="cursor:pointer;color:#8b949e;font-size:11px;font-weight:600;letter-spacing:.3px"></summary><table id="tord"></table></details>
<div id="twarn" style="font-size:12px;margin:0 0 4px"></div>
<div style="color:#8b949e;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;margin-top:4px">Греки как в конструкторе Bybit (ETH, в монетах) · сколько фьючей нужно</div><div class="row" id="tbg"></div>
<h3>Трёхногий каркас ETH: проданные путы · шорт инверса до 1:1 · купленные 3-дневные коллы (+ однодневки, скальп)</h3>
<div id="tfr"></div>
<h3>ETH: группа, вола, payoff (из снимка портфеля)</h3>
<div class="row" id="teov"></div>
<div id="tevol" style="font-size:12px;margin:2px 0 6px"></div>
<canvas id="tepay" width="780" height="240"></canvas>
<div id="tepaynote" style="color:#8b949e;font-size:11px;margin-bottom:6px"></div>
<details id="tbookd" open><summary style="cursor:pointer;color:#8b949e;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;margin-top:8px">Вся ETH-книга: опционы, фьючи, перпы, спот</summary><table id="tbookt"></table></details>
<h3>Гамма и скальп ETH</h3>
<div style="font-size:12px;margin:2px 0" id="tegsteps"></div><table id="tegam"></table>
<div style="display:flex;gap:12px;flex-wrap:wrap;margin-top:6px"><canvas id="tegprof" width="620" height="230"></canvas><canvas id="tegexp" width="440" height="230"></canvas></div>
<div id="tegnote" style="color:#8b949e;font-size:11px;margin:2px 0 6px"></div>
<h3>Куплено vs продано + покрытие · спот / Earn / истинная Δ ETH</h3><table id="tebs"></table><table id="tespot" style="margin-top:6px"></table>
<h3>Вола: продавать вегу на высокой, откупать на низкой (DVOL ETH, перцентиль за год)</h3>
<div id="tvbar" style="margin:2px 0 4px;padding:7px 10px;border-radius:8px;border:1px solid #30363d;background:#161b22;font-size:14px;font-weight:600"></div>
<div class="row" id="tvol"></div>
<canvas id="tdv" width="780" height="120"></canvas>
<h3>Дальние шорт-путы по экспирам: кредит, собрано, крыло, макс. убыток</h3>
<div class="row" id="tacct2"></div>
<div id="tgroups"></div>
<h3>Прикрытие голых путов шортом фьючерса ETH (сверху его закрывают купленные коллы)</h3>
<div class="row" id="tfh"></div><table id="tfhv"></table>
<div style="font-size:12px;margin:6px 0 2px;color:#8b949e">Лестница: сколько шорта держать на каждом уровне падения (дельта голых путов растёт из-за гаммы и роста IV)</div>
<table id="tfhl"></table>
<div style="color:#8b949e;font-size:11px;margin-top:4px">Фьючерс и опционы одной монеты PM зачитывает → маржа снижается, а не растёт (в отличие от хеджа другой монетой). Шорт фьючерса до размера купленных коллов той же или более поздней экспиры = синтетический купленный пут, хвоста вверх нет; сверх коллов — голый шорт. Защита фьючерсом живёт до его экспирации (25.12), проданные путы — 2027: перед декабрём её нужно перекатить. P&L вариантов: мгновенный ход, IV +20 на падении, по всей ETH-книге.</div>
<h3>Тактические хеджи (лонг-путы ≤14д)</h3><table id="thedge"></table>
<h3>Стресс всей ETH-книги — мгновенный шок цены × IV (путы +шок, коллы меньше), $</h3><table id="tstress"></table>
<h3>ETH против корзины — β-стресс всего портфеля (T+0), $</h3><table id="tebeta"></table><div id="tebetanote" style="color:#8b949e;font-size:11px"></div>
<h3>Кандидаты в хеджированную продажу: пут-спреды (продать верхний / купить нижний), по доходу к риску в месяц</h3><table id="tcand"></table>
<h3>Арбитраж по ETH: связки, открытые ноги, фандинг, сканер (из «Арбитража», обновление 20 с)</h3>
<div id="teabar" style="color:#8b949e;font-size:12px"></div>
<table id="tealinks"></table><table id="teaopen" style="margin-top:6px"></table><table id="teafund" style="margin-top:6px"></table><table id="teascan" style="margin-top:6px"></table>
<div style="color:#8b949e;font-size:11px;margin-top:6px">«Дальний» = страйк ≤ 85% цены ETH. «Собрано» = доля проданной премии, которую уже можно забрать (≥60% — тейк). «Покрытие» = сколько шортов закрыто купленными путами ниже в той же экспире; голая часть = неограниченный риск, «крыло» — самый дешёвый пут ниже для её закрытия (стоимость / макс. убыток после). P(убыток) ≈ |Δ| проданного пута, P(макс) ≈ |Δ| купленного (риск-нейтрально, по рынку). Стресс: Black-Scholes по markIv, без учёта изменения маржи. Параметры — TAIL_CFG в tail_pairs.py.</div>
</div>
<div id="tabA" style="display:none">
<div style="margin:2px 0 6px"><span class="gbtn" id="asb_mine" onclick="setASub('mine')">Мой арб-портфель</span><span class="gbtn" id="asb_scan" onclick="setASub('scan')">Сканер</span><span class="gbtn" id="asb_gold" onclick="setASub('gold')">Золото XAUT/XAU</span><span class="gbtn" id="asb_pairs" onclick="setASub('pairs')">Стат-арб (пары)</span></div>
<div id="asMine">
<div class="row" id="atiles"></div>
<h3>1 · Эквити, $ (левая ось); ETH и корзина — % от начала окна (правая ось)</h3><canvas id="ach1" width="980" height="170"></canvas>
<h3>2 · IM, % эквити</h3><canvas id="ach2" width="980" height="130"></canvas>
<h3>3 · MM, % эквити</h3><canvas id="ach2b" width="980" height="130"></canvas>
<h3>4 · IV ETH ATM, %</h3><canvas id="ach2c" width="980" height="130"></canvas>
<h3>5 · Δ$ портфеля</h3><canvas id="ach3" width="980" height="130"></canvas>
<h3>6 · Θ $/день</h3><canvas id="ach4" width="980" height="130"></canvas>
<h3>7 · Вега $ на 1 п.п. IV</h3><canvas id="ach5" width="980" height="130"></canvas>
<div id="achnote" style="color:#8b949e;font-size:11px;margin:2px 0 6px"></div>
<h3>Связки (арбитражные конструкции в книге)</h3><table id="alinks"></table>
<h3>Открытые ноги (не вошли в связки)</h3><table id="aopen"></table>
<h3>Фандинг книги</h3><table id="afund"></table>
<div style="color:#8b949e;font-size:11px;margin-top:6px">Арбитражный портфель (APT, Ло): Σw=0 (без своих денег), Σwβ=0 (нет риска фактора), остаток диверсифицирован → доход только от зафиксированного эджа. «Остаточный риск» = лучший − худший исход по всем ячейкам стресса β-блока «Портфеля» (ETH отдельно / корзина отдельно / ETH против корзины / согласованно по β, ±5…20%) и 0; у арбитражного портфеля ≈ 0. Связки ищутся автоматически, жадно, по количеству в монетах: кэрри (спот+Earn против шорта перпа) → базис золота (XAUT против XAU) → конверсия (лонг пута K + шорт колла K + лонг базы) / реверсия → межперп (лонг и шорт перпов одного актива: USDT/USDC/инверс). Боксы и связки через опционы с разными страйками не распознаются. «Можно закрыть в плюс» — нога с uPnL &gt; 0 (правило «не фиксировать убытки»). Фандинг $/день = −знак × |позиция $| × ставка × периодов в день (лонг платит при ставке &gt; 0).</div>
</div>
<div id="asScan" style="display:none">
<div id="asbar" style="margin:2px 0 4px;font-size:12px;color:#8b949e"></div>
<h3>Паритет: конверсия / реверсия (опционы + фьючерс той же экспирации, иначе перп)</h3><table id="aspar"></table>
<h3>Боксы (колл-спред + пут-спред, выплата K2−K1)</h3><table id="asbox"></table>
<h3>Cash-and-carry: спот против датированного фьючерса</h3><table id="ascc"></table>
<h3>Календарь: перп против датированного фьючерса</h3><table id="ascal"></table>
<h3>Кэрри спот + перп (фандинг)</h3><table id="ascarry"></table>
<h3>Инверс против линейного (разница фандингов)</h3><table id="asinv"></table>
<div style="color:#8b949e;font-size:11px;margin-top:6px">Все эджи — после <b>мейкерских</b> комиссий (/v5/account/fee-rate; если недоступно — оценка, см. строку статуса) по <b>исполнимой стороне стакана</b> (покупка по ask, продажа по bid) — для мейкера это консервативно. Ставка = 0 (без дисконтирования): эдж конверсии/бокса сравнивай с доходностью USDT в Earn. Комиссия поставки опциона принята = мейкер опциона × S (допущение); опционная комиссия без кэпа от премии (консервативно). Паритет: конверсия = cb − pa + K − Fa − комиссии, реверсия = Fb − K − ca + pb − комиссии; хедж — фьючерс той же экспирации, если есть, иначе перп; ожидаемый фандинг перпа до экспирации (средний за 7д × дней) показан <b>отдельно</b>: «эдж по ценам» &gt; 0 — арбитраж, плюс «только с фандингом» — ставка на то, что фандинг не изменится. Страйки ±30% от F. TradFi-опционы (QQQ): перп торгуется не круглосуточно, риск гэпа и дивидендов. % годовых = эдж / цена × 365 / дней. Маржа и доходность на маржу пока не считаются. «Объём» = меньший из размеров лучшей котировки двух опционных ног. Серым в кэрри — активы вне корзины (топ по рынку, оборот ≥ $5M, комиссии — оценка). Скан — в фоне, раз в 30 с. Параметры — ARB_CFG в arb_book.py.</div>
</div>
<div id="asGold" style="display:none">
<div class="row" id="gacct"></div>
<h3>Сигнал спреда (пороги по окну 30д, исполнимые цены стакана)</h3>
<div id="gbar" style="margin:2px 0 4px;padding:7px 10px;border-radius:8px;border:1px solid #30363d;background:#161b22;font-size:14px;font-weight:600"></div>
<div id="gwarn" style="font-size:12px;margin:0 0 4px"></div>
<div class="row" id="gtiles"></div>
<h3>Схождение — дисконт XAUT сузится: ЛОНГ XAUT / ШОРТ XAU (‰)</h3><div class="row" id="gconv"></div>
<h3>Расхождение — дисконт XAUT расширится: ШОРТ XAUT / ЛОНГ XAU (‰)</h3><div class="row" id="gdiv"></div>
<h3>График спреда ‰ = (XAUT/XAU − 1)×1000 &nbsp;
 <span class="gbtn" id="gw2d" onclick="setGW('2d')">2д · 15м</span><span class="gbtn on" id="gw30d" onclick="setGW('30d')">30д · 1ч</span><span class="gbtn" id="gw166d" onclick="setGW('166d')">166д · 4ч</span></h3>
<canvas id="gcv" width="980" height="300"></canvas>
<div id="gstat" style="color:#8b949e;font-size:11px;margin:2px 0 6px"></div>
<div id="gspread" style="font-size:12px;margin:2px 0 4px"></div>
<div style="color:#8b949e;font-size:11px;margin-top:6px">Схождение входим по ask XAUT / bid XAU, расхождение — по bid XAUT / ask XAU. Эдж = (среднее30д − вход)×10 бп − круг комиссий (мейкер-мейкер) ± фандинг × полураспад (≤14д). Ноги 1:1 по унциям (обе = 1 oz золота); β&lt;1 на часовках — это запаздывание XAUT, а не другой хедж-коэффициент. Сначала тонкая нога XAUT лимиткой post-only, потом хедж XAU (тейкер там 0.0275%). Не держать через выходные. Параметры порогов — GOLD_CFG в начале блока «ЗОЛОТО» в скрипте.</div>
</div>
<div id="asPairs" style="display:none">
<div style="margin:2px 0 6px;font-size:12px" class="warn">Статистический арбитраж, не чистый: пары β-нейтральны, но собственный риск ног остаётся (корреляция &lt; 1, β плавает).</div>
<div id="pbar" style="margin:2px 0 4px;font-size:12px;color:#8b949e"></div>
<h3>Пары: z сейчас, сигнал, размер ног из бюджета (без стопов: вход |z|≥2, добор |z|≥3, выход z=0, выход по времени 10д)</h3>
<table id="ptab"></table>
<canvas id="pcv" width="980" height="200"></canvas>
<div id="pnote" style="color:#8b949e;font-size:11px;margin:2px 0 6px"></div>
<h3>Корзина по факторам (нетто $, лимиты, бета к QQQ)</h3><table id="pfac"></table>
<div style="color:#8b949e;font-size:11px;margin-top:6px">z = (ln A − β·ln B − среднее)/σ на окне 260 баров по 30м только в сессии пары (US — 14:30–21:00 UTC зимой, KR — 00:00–06:30 UTC, WK — будни 24ч). Live-z — по mid стакана с β/средним/σ последнего окна. Бэктест 80д без стопа: n сделок, % прибыльных, средняя, худшая и MAE (худшая просадка в позиции) в бп ноги A; «модель в позиции» — что бэктест держит сейчас. Нога A = бюджет / (2×|MAE|), нога B = β×A (противоположный знак). Мейкер 0% на TradFi-перпах; на крипто-перпах (ETH, BTC, SOL, ETHFI, LDO) мейкер 0.036% — «нетто» = средняя минус круг мейкером обеих ног; тейкером (0.1%) крипто-пары в минусе. Крипто-пары — сессия ALL (24/7, окно 260 баров ≈ 5.4 дня). Только лимитки. Параметры — PAIRS, PAIR_CFG, FACTORS в tail_pairs.py.</div>
</div>
</div>
<div id="tabG" style="display:none">
<h3>Однодневки XAUT: продажа самых далёких OTM + перп-стоп на страйке</h3>
<div class="row" id="wtiles"></div>
<div id="wstop" style="font-size:12px;margin:2px 0 4px"></div>
<div id="wexps"></div>
<div style="color:#8b949e;font-size:11px;margin:2px 0 6px">Берём самый далёкий страйк с живым bid (≥0.2, размер ≥0.1). Хедж — стоп-ордер перпа на страйке: колл → buy-stop, пут → sell-stop, объём = объёму опциона; снимать, когда цена вернулась за страйк. «Честная» цена — Black-Scholes по бОльшей из RV 30д / RV 2д; эдж = bid − честная − комиссия опциона (мейкер, кэп 7% премии, ≈). «Разворотов» = сколько кругов стопа (вход+выход тейкером) покрывает премия; &lt;1 значит, что один ложный пробой съест всю премию. Стоп на XAU дешевле (тейкер 0.0275%), но страйк надо пересчитать через базис (колонка «стоп XAU»), а базис ходит ±1‰ ≈ $4. Мин. лот опциона 0.1 oz. Параметры — WING_CFG в скрипте.</div>
<h3>Греки и бета по золоту</h3><div class="row" id="ggreeks"></div>
<h3>Позиции по золоту (XAU*/XAUT перпы, фьючи, опционы XAUT)</h3><table id="gpos"></table>
</div>
<div id="tabE" style="display:none">
<div id="pgsel" style="margin:2px 0 6px"></div>
<div class="row" id="tiles"></div>
<div id="bybitfmt" style="margin:0 0 6px;color:#8b949e;font-size:12px"></div>
<div id="summwrap"><h3>Группы портфеля (клик — открыть группу)</h3><table id="summ"></table></div>
<div id="betawrap"><h3>Бета к ETH и стресс спреда «ETH против корзины»</h3>
<div class="row" id="betatiles"></div>
<table id="betatab"></table>
<h3>Стресс T+0 по сценариям, $ (относительно текущей стоимости)</h3><table id="betastress"></table>
<div id="betanote" style="color:#8b949e;font-size:11px;margin:2px 0 6px"></div></div>
<h3 id="volh">Датчик волы</h3>
<div class="row" id="voltiles"></div>
<div id="volband" style="margin:2px 0 4px;font-size:12px"></div>
<table id="voltab"></table>
<h3>Спот + Earn + истинная дельта по активу (опционы + перп + спот + Earn)</h3><table id="spot"></table>
<div style="color:#8b949e;font-size:11px;margin-top:2px">Earn — из /v5/earn/position (read-only). «Нужный перп-хедж» = на сколько скорректировать перп, чтобы истинная Δ по активу стала 0. Γ$ на 1% = на сколько $ изменится Δ$ при ходе актива на 1% (Γ·S²·0.01) — в этих единицах гамму разных активов можно складывать.</div>
<h3>Куплено vs продано + покрытие (срез сейчас)</h3><table id="bs"></table>
<h3 id="payh">Payoff</h3>
<canvas id="cv" width="780" height="260"></canvas>
<div id="paynote" style="color:#8b949e;font-size:11px;margin:2px 0 6px"></div>
<h3>Позиции с греками (позиционные, со знаком)</h3>
<table id="pos"></table>
</div>
<script>
const $=i=>document.getElementById(i);
const tile=(l,v,c)=>`<div class="tile"><div class="lbl">${l}</div><div class="val ${c||''}">${v}</div></div>`;
let LAST=null, GREEK='g';
const GL={g:'Γ',v:'Vega',t:'Θ'};
// ===================== ПОРТФЕЛЬ =====================
let PORT=null, PG='ALL';
try{const v=localStorage.getItem('pg');if(v)PG=v;}catch(e){}
const cl=(v,inv)=>v==null||v==0?'':((v>0)!=!!inv?'pos':'neg');
function setPG(k){PG=k;try{localStorage.setItem('pg',k)}catch(e){} if(PORT)renderPort(PORT);}
async function tick(){
 let d; try{d=await (await fetch('/port')).json()}catch(e){if(TAB=='E'||TAB=='K')$('upd').textContent='offline';return}
 if(d.err){if(TAB=='E'||TAB=='K')$('upd').textContent='err: '+d.err;return}
 PORT=d; if(TAB=='E')renderPort(d); else if(TAB=='K')renderBook(d); else if(TAB=='T')renderEthPort(d);
}
function renderPort(d){
 if(!d.groups[PG])PG='ALL';
 const g=d.groups[PG],a=d.acct,F=d.flat,all=d.groups.ALL,t=g.tot,one=g.main&&g.rows.length==1;
 $('pgsel').innerHTML=d.order.map(k=>`<span class="gbtn ${k==PG?'on':''}" onclick="setPG('${k}')">${d.groups[k].name}</span>`).join('');
 if(TAB=='E')$('upd').textContent=(g.main?g.main+' '+g.px:'портфель')+'  ·  '+d.t;
 const mc=a.mm_pct>62?'neg':(a.mm_pct>55?'warn':'pos'),fl=v=>Math.abs(v||0)<F?'pos':'warn';
 let h=tile('Эквити','$'+a.eq)+tile('Свободно','$'+a.av)+tile('IM',a.im_pct+'%')+tile('MM',a.mm_pct+'%',mc)+tile('Запас',a.buf+'x',mc);
 if(one) h+=tile('Δ '+g.main+' опц+перп',sg(t.od_perp))+tile('Δ '+g.main+' +спот',sg(t.true));
 h+=tile('Δ$ '+g.name,sg(t.dusd),fl(t.dusd));
 if(PG!='ALL') h+=tile('Δ$ портфель',sg(all.tot.dusd),fl(all.tot.dusd));
 if(PG=='ALL'&&t.eq!=null) h+=tile('Δ ETH-экв.',sg(t.eq)+'$',fl(t.eq));
 if(PG=='GOLD'&&t.hedge_xau!=null) h+=tile('хедж в XAU oz',sg(t.hedge_xau),'warn');
 if(one) h+=tile('Γ '+g.main,t.g,cl(t.g));
 h+=tile('Γ$ на 1%',sg(t.g1),cl(t.g1))+tile('Θ /д',sg(t.t),cl(t.t))+tile('Vega',sg(t.v),cl(t.v))+tile('uPnL',sg(t.up)+'$',cl(t.up));
 $('tiles').innerHTML=h;
 const at=all.tot;
 $('bybitfmt').innerHTML=(PG!='ALL'?'<b style="color:#e6edf3">ПО '+g.name.toUpperCase()+':</b> '
   +(one?'Δ '+t.od_perp+' (опц+перп, Bybit-формат) · Δ '+t.true+' (+спот+Earn) · Γ '+t.g+' &nbsp;·&nbsp; ':'')
   +'Δ$ '+sg(t.dusd)+' · Γ$1% '+sg(t.g1)+' · Θ '+sg(t.t)+' · V '+sg(t.v)+' &nbsp;·&nbsp; P&L '+sg(t.up)+'$<br>':'')
  +'<b style="color:#e6edf3">ВЕСЬ ПОРТФЕЛЬ:</b> Δ$ '+sg(at.dusd)+' · Γ$1% '+sg(at.g1)+' · Θ '+sg(at.t)+' · V '+sg(at.v)+' &nbsp;·&nbsp; P&L '+sg(at.up)+'$ (все группы; спот и Earn включены; стейблы USDT '+d.stable.USDT+' / USDC '+d.stable.USDC+')';
 // группы
 $('summwrap').style.display=PG=='ALL'?'':'none';
 if(PG=='ALL'){let s='<tr><th>группа</th><th>позиций</th><th>Δ$</th><th>Γ$ на 1%</th><th>Θ/д</th><th>Vega</th><th>uPnL</th></tr>';
  (all.summ||[]).forEach(r=>{s+=`<tr style="cursor:pointer" onclick="setPG('${r.key}')"><td>${r.name}</td><td>${r.n}</td><td class="${fl(r.dusd)}">${sg(r.dusd)}</td><td class="${cl(r.g1)}">${sg(r.g1)}</td><td class="${cl(r.t)}">${sg(r.t)}</td><td class="${cl(r.v)}">${sg(r.v)}</td><td class="${cl(r.up)}">${sg(r.up)}</td></tr>`;});
  s+=`<tr style="border-top:1px solid #30363d"><td><b>Итого</b></td><td>${all.n}</td><td class="${fl(at.dusd)}"><b>${sg(at.dusd)}</b></td><td>${sg(at.g1)}</td><td class="${cl(at.t)}">${sg(at.t)}</td><td>${sg(at.v)}</td><td class="${cl(at.up)}">${sg(at.up)}</td></tr>`;
  $('summ').innerHTML=s;}
 renderPBeta(d,g); renderPVol(g); renderPSpot(d,g); renderPBS(g); drawPPay(g); renderPPos(g);
}
function renderPBeta(d,g){
 $('betawrap').style.display=(PG=='ALL'&&g.beta)?'':'none'; if(PG!='ALL'||!g.beta)return;
 const b=g.beta,F=d.flat,fl=v=>Math.abs(v||0)<F?'pos':'warn',pn=v=>v<0?'neg':(v>0?'pos':'');
 $('betatiles').innerHTML=tile('Δ ETH-экв. (всё)',sg(b.eq_total)+'$',fl(b.eq_total))+tile('Δ ETH-экв. (крипта)',sg(b.eq_crypto)+'$',fl(b.eq_crypto))+tile('Δ$ (без беты)',sg(g.tot.dusd)+'$',fl(g.tot.dusd));
 let s='<tr><th>актив</th><th>Δ$</th><th>β к ETH</th><th>корр.</th><th>ETH-экв. $</th><th></th></tr>';
 b.rows.forEach(r=>{const weak=r.crypto&&r.base!='ETH'&&r.corr!=null&&r.corr<0.7&&Math.abs(r.dusd)>=F;
  s+=`<tr><td>${r.base}${r.crypto?'':' <span style="color:#8b949e">(не крипта)</span>'}</td><td>${sg(r.dusd)}</td><td>${r.beta??'—'}</td><td class="${weak?'warn':''}">${r.corr??'—'}</td><td>${r.eq==null?'—':sg(r.eq)}</td><td class="warn" style="text-align:left">${weak?'слабый хедж: корр. &lt; 0.7':''}</td></tr>`;});
 $('betatab').innerHTML=s;
 const st=b.stress; let h='<tr><th>сценарий</th>'+st.moves.map(m=>'<th>'+(m>0?'+':'')+m+'%</th>').join('')+'</tr>';
 st.rows.forEach(r=>{h+='<tr><td>'+r.name+'</td>'+r.pnl.map(v=>'<td class="'+pn(v)+'">'+sg(v)+'</td>').join('')+'</tr>';});
 $('betastress').innerHTML=h;
 const worst=st.rows.reduce((w,r)=>r.pnl.reduce((w2,v,i)=>v<w2.v?{v,n:r.name,m:st.moves[i]}:w2,w),{v:Infinity});
 $('betanote').innerHTML='Корзина = '+(b.basket.join(', ')||'—')+'. β и корреляция — 4ч лог-доходности за 30д против ETH; ETH-экв. = Δ$·β (сколько $ ETH-дельты даёт позиция при типичном ходе). «Крипта» — без золота/QQQ/акций. Стресс: T+0, Black-Scholes при текущей IV, без пересчёта маржи; в сценариях 1–3 некрипто-активы стоят. β не учитывает смену режима и хвостовые корреляции (в обвал всё сходится к 1).'
  +(worst.v<0?' <b class="neg">Худшая клетка: '+sg(worst.v)+'$ — «'+worst.n+'» при '+(worst.m>0?'+':'')+worst.m+'%.</b>':'');
}
function vcls(r){return !r?'':(r>=1.3?'neg':(r>=1.1?'warn':(r<0.9?'pos':'')))}
function renderPVol(g){
 if(PG=='ALL'){$('volh').textContent='Датчик волы по всем опционным активам (ATM ближайшей экспиры ≥7д)';$('voltiles').innerHTML='';$('volband').innerHTML='';
  let s='<tr><th>актив</th><th>ATM IV</th><th>RV 30д</th><th>IV−RV</th><th>IV/RV</th><th>ранг сессии</th><th>экспира</th><th>сигнал</th></tr>';
  (g.vols||[]).forEach(v=>{s+=`<tr><td>${v.base}</td><td>${v.iv}%</td><td>${v.rv}%</td><td class="${v.prem>0?'warn':'pos'}">${sg(v.prem)}пт</td><td class="${vcls(v.ratio)}">${v.ratio||'—'}</td><td>${v.rank==null?'—':v.rank+'%'}</td><td>${v.exp} (${v.dte}д)</td><td class="${vcls(v.ratio)}">${v.band}</td></tr>`;});
  $('voltab').innerHTML=s;return;}
 $('voltab').innerHTML='';const v=g.vol;
 if(!v||!v.iv){$('volh').textContent='Датчик волы';$('voltiles').innerHTML='';$('volband').textContent=g.main?'нет опционов на '+g.main+' на Bybit':'';return;}
 $('volh').textContent='Датчик волы '+v.base+' (сигнал «ждать / продавать»)';
 $('voltiles').innerHTML=tile('ATM IV',v.iv+'%',vcls(v.ratio))+tile('RV 30д',v.rv+'%')+tile('премия IV−RV',sg(v.prem)+'пт',v.prem>0?'warn':'pos')
  +tile('IV / RV',v.ratio||'—',vcls(v.ratio))+tile('ранг сессии',v.rank==null?'—':v.rank+'%')+tile('диапазон',v.lo+'–'+v.hi+'%');
 const prem=v.prem>0?('опционы ДОРОЖЕ реализованной на '+v.prem+'пт → лонг-вега платит премию'):('опционы ДЕШЕВЛЕ реализованной ('+v.prem+'пт) → вега недорога');
 $('volband').innerHTML='<b class="'+vcls(v.ratio)+'">'+v.band+'</b> &nbsp;(ATM '+v.exp+' K'+v.K+', '+v.dte+'д) &nbsp;·&nbsp; '+prem;
}
function renderPSpot(d,g){
 const F=d.flat,hed=(r)=>Math.abs(r.dusd)<1?'нейтраль':(r.hedge>0?'ЛОНГ +':'ШОРТ ')+Math.abs(r.hedge)+' '+r.base;
 let s='<tr><th>актив</th><th>цена</th><th>спот</th><th>Earn</th><th>опц Δ</th><th>опц Γ</th><th>опц Θ</th><th>опц V</th><th>Γ$ 1%</th><th>перп Δ</th><th>истинная Δ</th><th>Δ$</th><th>нужный перп-хедж</th></tr>';
 g.rows.forEach(r=>{s+=`<tr><td>${r.base}</td><td>${r.px}</td><td>${r.spot}</td><td>${r.earn}</td><td>${r.od}</td><td>${r.og}</td><td class="${cl(r.ot)}">${r.ot}</td><td>${r.ov}</td><td>${r.g1}</td><td>${r.pd}</td><td class="${Math.abs(r.dusd)<F?'pos':'warn'}">${r.true}</td><td class="${Math.abs(r.dusd)<F?'pos':'warn'}">${sg(r.dusd)}</td><td class="warn">${hed(r)}</td></tr>`;});
 if(g.rows.length>1){const t=g.tot;s+=`<tr style="border-top:1px solid #30363d"><td><b>Итого ${g.name}</b></td><td></td><td></td><td></td><td></td><td></td><td class="${cl(t.t)}">${t.t}</td><td>${t.v}</td><td>${t.g1}</td><td></td><td></td><td class="${Math.abs(t.dusd)<F?'pos':'warn'}"><b>${sg(t.dusd)}</b></td><td class="warn">${PG=='GOLD'&&t.hedge_xau!=null?('в XAU: '+(t.hedge_xau>0?'ЛОНГ +':'ШОРТ ')+Math.abs(t.hedge_xau)+' oz'):''}</td></tr>`;}
 if(PG=='ALL'){['USDT','USDC'].forEach(c=>{s+=`<tr><td>${c}</td><td>1</td><td>${d.stable[c]}</td><td colspan="8" style="text-align:center;color:#8b949e">—</td><td class="pos">0 стейбл</td><td>—</td></tr>`;});}
 $('spot').innerHTML=s;
}
function renderPBS(g){
 const B=g.B,S=g.S,c=g.cov,one=g.main&&g.rows.length==1;
 if(!B.n&&!S.n){$('bs').innerHTML='<tr><td style="color:#8b949e">опционов в группе нет</td></tr>';return;}
 let s='<tr><th></th><th>ног</th>'+(one?'<th>Γ</th><th>Δшт</th>':'')+'<th>Γ$ 1%</th><th>Θ/д</th><th>Vega</th><th>Δ$</th></tr>';
 const row=(n,x,cg,ct,cv)=>`<tr><td>${n}</td><td>${x.n}</td>`+(one?`<td class="${cg}">${x.g}</td><td>${x.d}</td>`:'')+`<td class="${cg}">${x.g1}</td><td class="${ct}">${x.t}</td><td class="${cv}">${x.v}</td><td>${x.dusd}</td></tr>`;
 s+=row('Куплено',B,'pos','neg','pos')+row('Продано',S,'neg','pos','neg');
 s+=`<tr><td>Покрытие % (short÷long)</td><td></td>`+(one?'<td></td><td></td>':'')+`<td class="${c.g>100?'neg':'warn'}">${c.g}</td><td class="pos">${c.t}</td><td class="warn">${c.v}</td><td></td></tr>`;
 $('bs').innerHTML=s;
}
function drawPPay(g){const cv=$('cv'),x=cv.getContext('2d'),W=cv.width,H=cv.height,p=46;x.clearRect(0,0,W,H);
 const P=g.pay,N=g.now;if(!P||P.length<2)return;
 $('payh').textContent='Payoff '+g.name+(g.pct?' — ось: ход ВСЕХ активов на один и тот же %':' — ось: цена '+g.main+(g.rows.length>1?' (остальные активы группы двигаются на тот же %)':''));
 const xs=P.map(a=>a[0]),ys=P.map(a=>a[1]).concat(N.map(a=>a[1]));
 const x0=Math.min(...xs),x1=Math.max(...xs);let y0=Math.min(0,...ys),y1=Math.max(0,...ys);if(y1==y0){y1+=1;y0-=1}
 const X=v=>p+(v-x0)/(x1-x0)*(W-2*p),Y=v=>H-p+10-(v-y0)/(y1-y0)*(H-2*p);
 x.strokeStyle='#30363d';x.beginPath();x.moveTo(p,Y(0));x.lineTo(W-p,Y(0));x.stroke();
 const cur=g.pct?0:g.px;
 x.strokeStyle='#8b949e';x.setLineDash([4,4]);x.beginPath();x.moveTo(X(cur),p-20);x.lineTo(X(cur),H-p+10);x.stroke();x.setLineDash([]);
 const line=(A,c,w)=>{x.strokeStyle=c;x.lineWidth=w;x.beginPath();A.forEach((a,i)=>i?x.lineTo(X(a[0]),Y(a[1])):x.moveTo(X(a[0]),Y(a[1])));x.stroke();x.lineWidth=1;};
 line(N,'#58a6ff',1.6);line(P,'#3fb950',2);
 x.font='11px sans-serif';(g.be||[]).forEach(b=>{x.fillStyle='#8b949e';x.beginPath();x.arc(X(b),Y(0),3,0,7);x.fill();x.fillText(b+(g.pct?'%':''),X(b)-16,Y(0)+16)});
 x.fillStyle='#8b949e';x.fillText('$'+Math.round(y1),4,Y(y1)+8);x.fillText('$'+Math.round(y0),4,Y(y0));
 x.fillText(g.pct?'сейчас 0%':(g.main+' '+g.px),X(cur)-28,p-24);
 const fx=v=>Math.abs(v)>=100?Math.round(v):+(+v).toPrecision(4);x.fillText(fx(x0)+(g.pct?'%':''),p-10,H-4);x.fillText(fx(x1)+(g.pct?'%':''),W-p-20,H-4);
 x.fillStyle='#3fb950';x.fillText('━ на экспирации',p+60,H-4);x.fillStyle='#58a6ff';x.fillText('━ сейчас (T+0, BS по markIv)',p+170,H-4);
 const mi=N.reduce((m,a)=>a[1]<m[1]?a:m,N[0]);
 $('paynote').innerHTML='Зелёная — на экспирации (внутренняя стоимость − цена входа; экспиры смешаны, поэтому приближённо). Синяя — если ход случится сейчас, IV без изменений. Худшая точка T+0 на графике: <b class="neg">'+sg(mi[1])+'$</b> при '+(Math.abs(mi[0])>=100?Math.round(mi[0]):+(+mi[0]).toPrecision(4))+(g.pct?'%':'')
  +(g.pct?'. Для «Весь портфель» все активы сдвигаются одинаково — корреляции (золото против крипты) не учтены, это грубый шок.':'')+(g.be_now&&g.be_now.length?' · безубыток T+0: '+g.be_now.join(', '):'');}
function renderPPos(g){
 const all=PG=='ALL';
 let s='<tr>'+(all?'<th>группа</th>':'')+'<th>контракт</th><th>ст</th><th>разм</th><th>Δ</th><th>Δ$</th><th>Γ</th><th>Θ/д</th><th>Vega</th><th>IV</th><th>uPnL</th></tr>';
 g.pos.forEach(p=>{s+='<tr>'+(all?'<td style="color:#8b949e">'+p.grp+'</td>':'')+`<td>${p.sym}</td><td class="${p.side=='B'?'pos':'neg'}">${p.side}</td><td>${p.sz}</td><td>${p.d}</td><td>${p.dusd}</td><td>${p.g}</td><td class="${p.t<0?'neg':(p.t>0?'pos':'')}">${p.t}</td><td>${p.v}</td><td>${p.iv==null?'':p.iv+'%'}</td><td class="${p.up<0?'neg':'pos'}">${p.up}</td></tr>`;});
 if(!g.pos.length)s+='<tr><td colspan="10" style="color:#8b949e">позиций нет</td></tr>';
 $('pos').innerHTML=s;
}
function uniq(a){return [...new Set(a)]}
function sumAbs(arr,g){return arr.reduce((s,l)=>s+Math.abs(l[g]),0)}
function greekProfile(cv,legs,g){const x=cv.getContext('2d'),W=cv.width,H=cv.height,p=30;x.clearRect(0,0,W,H);
 const ks=uniq(legs.map(l=>l.K)).sort((a,b)=>a-b);if(!ks.length)return;
 const net=ks.map(k=>legs.filter(l=>l.K==k).reduce((s,l)=>s+l[g],0));
 const mx=Math.max(1e-9,...net.map(Math.abs)),mid=H/2;
 const X=i=>p+i/(Math.max(1,ks.length-1))*(W-2*p);
 x.strokeStyle='#30363d';x.beginPath();x.moveTo(p,mid);x.lineTo(W-p,mid);x.stroke();
 ks.forEach((k,i)=>{const h=net[i]/mx*(mid-14),bw=(W-2*p)/ks.length*0.8;x.fillStyle=net[i]>=0?'#3fb95099':'#f8514999';
   x.fillRect(X(i)-bw/2,net[i]>=0?mid-h:mid,bw,Math.abs(h));});
 x.fillStyle='#8b949e';x.font='8px sans-serif';ks.forEach((k,i)=>{if(ks.length<=10||i%Math.ceil(ks.length/10)==0)x.fillText(k,X(i)-10,H-3)});
 x.fillText('нетто '+GL[g]+' (лонг вверх / шорт вниз)',4,11);}
function drawViews(fId,sId,tId,sub){
 const ks=uniq(sub.map(l=>l.K)).sort((a,b)=>a-b),dz=uniq(sub.map(l=>Math.round(l.dte))).sort((a,b)=>a-b);
 bars($(fId),ks.map(String),ks.map(k=>sumAbs(sub.filter(l=>l.K==k&&l.side=='B'),GREEK)),ks.map(k=>sumAbs(sub.filter(l=>l.K==k&&l.side=='S'),GREEK)),'по страйку');
 bars($(sId),dz.map(d=>d+'д'),dz.map(dd=>sumAbs(sub.filter(l=>Math.round(l.dte)==dd&&l.side=='B'),GREEK)),dz.map(dd=>sumAbs(sub.filter(l=>Math.round(l.dte)==dd&&l.side=='S'),GREEK)),'по времени');
 const grid=dz.map(dd=>ks.map(k=>sumAbs(sub.filter(l=>l.K==k&&Math.round(l.dte)==dd&&l.side=='B'),GREEK)-sumAbs(sub.filter(l=>l.K==k&&Math.round(l.dte)==dd&&l.side=='S'),GREEK)));
 heat($(tId),ks.map(String),dz.map(d=>d+'д'),grid,'страйк×время');}
function bars(cv,labels,bought,sold,title){const x=cv.getContext('2d'),W=cv.width,H=cv.height,pad=26,mid=(H+8)/2;x.clearRect(0,0,W,H);
 x.fillStyle='#8b949e';x.font='9px sans-serif';x.fillText(title,4,10);if(!labels.length)return;
 const mx=Math.max(1e-9,...bought,...sold),step=(W-2*pad)/labels.length,bw=step*0.7;
 x.strokeStyle='#30363d';x.beginPath();x.moveTo(pad,mid);x.lineTo(W-pad,mid);x.stroke();
 labels.forEach((l,i)=>{const cx=pad+i*step+step/2,hb=bought[i]/mx*(mid-16),hs=sold[i]/mx*(mid-16);
   x.fillStyle='#3fb950';x.fillRect(cx-bw/2,mid-hb,bw,hb);x.fillStyle='#f85149';x.fillRect(cx-bw/2,mid,bw,hs);});
 x.fillStyle='#8b949e';x.font='8px sans-serif';labels.forEach((l,i)=>{if(labels.length<=7||i%Math.ceil(labels.length/7)==0)x.fillText(l,pad+i*step+step/2-10,H-3)});}
function heat(cv,xs,ys,grid,title){const x=cv.getContext('2d'),W=cv.width,H=cv.height;x.clearRect(0,0,W,H);
 x.fillStyle='#8b949e';x.font='9px sans-serif';x.fillText(title,4,10);if(!xs.length||!ys.length)return;
 const padL=30,padB=14,cw=(W-padL-4)/xs.length,ch=(H-16-padB)/ys.length;let mx=1e-9;grid.forEach(r=>r.forEach(v=>mx=Math.max(mx,Math.abs(v))));
 ys.forEach((yl,yi)=>xs.forEach((xl,xi)=>{const v=grid[yi][xi],a=Math.min(1,Math.abs(v)/mx);
   x.fillStyle=v>=0?`rgba(63,185,80,${a})`:`rgba(248,81,73,${a})`;x.fillRect(padL+xi*cw,16+yi*ch,cw-1,ch-1);}));
 x.fillStyle='#8b949e';x.font='8px sans-serif';xs.forEach((xl,xi)=>{if(xs.length<=7||xi%Math.ceil(xs.length/7)==0)x.fillText(xl,padL+xi*cw,H-3)});
 ys.forEach((yl,yi)=>x.fillText(yl,0,16+yi*ch+ch/2+3));}
// ===================== ЗОЛОТО =====================
let TAB='K', GW='30d', GLD=null, ASUB='mine';
try{const v=localStorage.getItem('asub');if(['mine','scan','gold','pairs'].includes(v))ASUB=v;}catch(e){}
function showTab(t){TAB=t;['D','K','E','A','G','T','F','R','C','I','S','O','L'].forEach(k=>{$('tab'+k).style.display=t==k?'':'none';$('tb'+k)&&$('tb'+k).classList.toggle('on',t==k);});
 navSync(t);
 try{localStorage.setItem('tab',t)}catch(e){} if(t=='K'){if(PORT)renderBook(PORT);khtick();} if(t=='E'&&PORT)renderPort(PORT); if(t=='G')gtick(); if(t=='T'){ttick();if(PORT)renderEthPort(PORT);etick();} if(t=='F')ftick(); if(t=='R'){rtick();rtvLoad();rtkTick();} if(t=='S'){prtick();pctick();lbtick();grtick();prTvLoad();} if(t=='C')ctick(); if(t=='I')itick(); if(t=='A')setASub(ASUB); if(t=='O')opTick(); if(t=='L'){gatick();altLab();} if(t=='D'){dtick();PCT=[];pctick();}}
// ===================== ХВОСТЫ ETH =====================
async function ttick(){ if(TAB!='T')return;
 let d; try{d=await (await fetch('/tail')).json()}catch(e){$('tvbar').textContent='offline';return}
 if(d.err){$('tvbar').textContent='ошибка: '+d.err;return}
 const mc=d.mm>d.cfg.mm_max?'neg':(d.mm>40?'warn':'pos');
 const bcT=d.buf==null?'':(d.buf<1.5?'neg':(d.buf<2?'warn':'pos'));
 const g_=d.bg||{},dE=(g_.d_opt||0)+(g_.d_lin||0)+(g_.d_inv||0)+(g_.d_spot||0),dU=dE*d.S,eqv=d.eq||0;
 const PA=PORT&&PORT.groups&&PORT.groups.ALL?PORT.groups.ALL.tot:null,lq=d.liq||{},ln=lq.now||{},lc=lq.cons||{};
 const lv=(x,y)=>x?(x.S+' ('+(x.pct>0?'+':'')+x.pct+'%)'+(y&&y.S!=x.S?' · конс. '+y.S:'')):'нет до '+(y===undefined?'':'±')+'края сетки';
 const near=[ln.down,ln.up].filter(Boolean).some(x=>Math.abs(x.pct)<=15);
 $('tacct').innerHTML=tile('ETH',d.S)+tile('Эквити','$'+d.eq)+tile('Свободно','$'+d.av,d.av<100?'warn':'')
  +tile('IM',d.im+'%',d.im>50?(d.im>80?'neg':'warn'):'pos')+tile('MM',d.mm+'%',mc)+tile('Запас до MM',(d.buf??'—')+'×',bcT)
  +tile('Δ ETH всего, шт',(dE>0?'+':'')+dE.toFixed(3),Math.abs(dU)>0.2*eqv?'warn':'pos')
  +tile('Δ ETH в $ · до нейтрали',(dU>0?'+':'')+Math.round(dU)+'$ · '+(Math.abs(dE)<0.005?'нейтраль':(dE>0?'продать ':'купить ')+Math.abs(dE).toFixed(3)+' ETH'),Math.abs(dU)>0.2*eqv?'warn':'pos')
  +tile('Δ$ портфеля',PA?sg(Math.round(PA.dusd)):'—',PA&&Math.abs(PA.dusd)>0.2*eqv?'warn':'pos')
  +tile('Θ $/день портфель · ETH',(PA?sg(PA.t):'—')+' · '+sg(Math.round((g_.t||0)*100)/100),PA&&Math.abs(PA.t)>0.01*eqv?'warn':(PA&&PA.t>0?'pos':''))
  +tile('Ликвидация: ETH ↓ / ↑',(ln.down?ln.down.S+' ('+ln.down.pct+'%)':'—')+' / '+(ln.up?ln.up.S+' (+'+ln.up.pct+'%)':'—'),near?'neg':'warn');
 $('tacct2').innerHTML=tile('кредит дальних','$'+d.prem_far)+tile('Θ/д дальних',sg(d.theta_far)+'$',d.theta_far>0?'pos':'neg')+tile('Vega дальних',sg(d.vega_far))
  +tile('макс.убыток спредов','$'+d.tot_def+' / '+d.budget,d.tot_def>d.budget?'neg':'pos')
  +tile('голый номинал','$'+d.naked_notional,d.naked_notional>0?'neg':'pos')
  +tile('Θ/д тактич. хеджей',sg(d.hedge_theta)+'$',d.hedge_theta<0?'warn':'')
  +tile('нетто Θ (дальние+хеджи)',sg(Math.round((d.theta_far+d.hedge_theta)*100)/100)+'$',(d.theta_far+d.hedge_theta)>0?'pos':'neg');
 $('tliq').innerHTML='Ликвидация — оценка: ETH-книга T+0 (BS), на падении IV +20 п.п., на росте +10, прочие активы стоят. Запас над MM $'+(lq.room??'—')
  +'. При текущей MM: ↓ '+(ln.down?ln.down.S+' ('+ln.down.pct+'%)':'нет до −90%')+' · ↑ '+(ln.up?ln.up.S+' (+'+ln.up.pct+'%)':'нет до +200%')
  +'. Консервативно (MM +10%): ↓ '+(lc.down?lc.down.S+' ('+lc.down.pct+'%)':'нет')+' · ↑ '+(lc.up?lc.up.S+' (+'+lc.up.pct+'%)':'нет')
  +'. Реальный порог зависит от пересчёта PM-маржи и движения других активов.';
 $('twarn').innerHTML=(d.warns||[]).map(w=>'<div class="warn">⚠ '+w+'</div>').join('');
 try{renderFrame(d.frame,d)}catch(e){$('tfr').textContent='каркас: '+e}
 // открытые заявки: только ETH, одной строкой-сводкой; таблица — свёрнута (раскрыть кликом)
 {const O=(d.orders||[]).filter(o=>/^ETH/.test(o.sym)).sort((a,b)=>a.sym<b.sym?-1:a.sym>b.sym?1:a.price-b.price),bk={};(d.book||[]).forEach(r=>{if(r.mark!=null)bk[r.sym.split(' ')[0]]=r.mark;});
  const G={};O.forEach(o=>{const k=o.sym+' '+(o.side=='Buy'?'покупка':'продажа');const g=G[k]||(G[k]={n:0,q:0,lo:1e18,hi:-1e18});g.n++;g.q+=o.qty;g.lo=Math.min(g.lo,o.price);g.hi=Math.max(g.hi,o.price);});
  $('tordh').textContent='Заявки ETH ('+O.length+')'+(O.length?': '+Object.entries(G).map(([k,g])=>k+' '+g.n+' шт · '+(+g.q.toFixed(5))+' · '+(g.lo==g.hi?g.lo:g.lo+'–'+g.hi)).join(' | ')+' — раскрыть':' — нет');
  let h=O.length?'<tr><th>инструмент</th><th>тип</th><th>сторона</th><th>цена</th><th>триггер</th><th>кол-во</th><th>исполнено</th><th>сейчас марк</th><th>до цены</th><th>reduce-only</th><th>создана</th></tr>':'';
  O.forEach(o=>{const m=bk[o.sym]??(o.sym.startsWith('ETH')&&o.cat!='option'?d.S:null),dist=m&&o.price?((o.price/m-1)*100):null,tm=new Date(o.t);
   h+='<tr><td><b>'+o.sym+'</b> <span style="color:#8b949e">'+o.cat+'</span></td><td>'+o.type+(o.tif&&o.tif!='GTC'?' '+o.tif:'')+'</td><td class="'+(o.side=='Buy'?'pos':'neg')+'">'+o.side+'</td><td>'+o.price+'</td><td>'+(o.trig??'—')+'</td><td>'+o.qty+'</td><td>'+o.filled+'</td><td>'+(m??'—')+'</td><td>'+(dist==null?'—':(dist>0?'+':'')+dist.toFixed(1)+'%')+'</td><td>'+(o.ro?'да':'')+'</td><td>'+tm.toLocaleString('ru',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})+'</td></tr>';});
  $('tord').innerHTML=h;}
 if(d.bg){const g=d.bg,F=d.fh||{},f4=v=>(v>0?'+':'')+(+v).toFixed(4),dl=g.d_opt+g.d_lin,da=dl+g.d_inv+g.d_spot;
  $('tbg').innerHTML=tile('Delta (опционы)',f4(g.d_opt),g.d_opt<0?'neg':'pos')+tile('Delta + фьючи USDT/USDC (как Bybit)',f4(dl))+tile('Delta всего (+инверс, спот)',f4(da)+' ≈ $'+Math.round(da*d.S))
   +tile('Gamma',(g.g>0?'+':'')+g.g.toFixed(5),g.g<0?'neg':'pos')+tile('Theta',f4(g.t),g.t>0?'pos':'neg')+tile('Vega',f4(g.v),g.v<0?'warn':'')
   +tile('голых путов',(F.naked_q??'—')+' ETH',F.naked_q>0?'neg':'pos')
   +tile('фьючей: Δ голых путов',(F.d_naked??'—')+' ETH')+tile('фьючей: книга в ноль',(F.d_book??'—')+' ETH')
   +tile('фьючей: обвал до 0 (1:1)',(F.naked_q??'—')+' ETH · коллы сверху '+(F.call_q??'—'),F.naked_q>F.call_q?'warn':'');}
 const col={sell:['#3fb950','#3fb95022'],cover:['#d29922','#d2992222'],wait:['#8b949e','#161b22']}[d.vk];
 const b=$('tvbar');b.textContent=d.vsig;b.style.color=col[0];b.style.borderColor=col[0]=='#8b949e'?'#30363d':col[0];b.style.background=col[1];
 const v=d.dvol||{};
 $('tvol').innerHTML=tile('DVOL',v.now??'—')+tile('перцентиль 1г',(v.pct??'—')+'%')+tile('p10 / p50 / p80 / p90',[v.p10,v.p50,v.p80,v.p90].join(' / '))
  +tile('ATM IV '+(d.atm?d.atm.exp:''),(d.atm?d.atm.iv:'—')+'%')+tile('RV 30д',d.rv30+'%')
  +tile('IV − RV',d.atm?sg(Math.round((d.atm.iv-d.rv30)*10)/10)+'пт':'—',d.atm&&d.atm.iv>d.rv30?'pos':'neg');
 drawDV(v);
 let h='';
 (d.groups||[]).forEach(g=>{
  h+='<div style="margin:8px 0 2px;font-size:12px"><b style="color:#e6edf3">'+g.exp+'</b> · '+g.dte+'д · покрытие '+g.cover+'%'
   +(g.naked>0?' · <span class="neg">голые '+g.naked+'</span>':' · <span class="pos">спред</span>')
   +' · кредит $'+g.credit+' · сейчас $'+g.now+' · P&L <span class="'+(g.pnl>=0?'pos':'neg')+'">'+sg(g.pnl)+'$</span>'
   +' · собрано '+(g.captured??'—')+'%'+(g.tp?' <b class="pos">→ ТЕЙК</b>':'')
   +' · макс.убыток '+(g.maxloss!=null?'$'+g.maxloss:'<span class="neg">не ограничен</span>')+' · Θ '+sg(g.theta)+' · Vega '+sg(g.vega)+'</div>';
  h+='<table><tr><th>шорт</th><th>кол-во</th><th>ср.</th><th>марк</th><th>IV</th><th>bid / ask</th></tr>';
  g.shorts.forEach(s=>{h+='<tr><td>'+s[0]+'P</td><td>'+s[1]+'</td><td>'+s[2]+'</td><td>'+s[3]+'</td><td>'+s[4]+'%</td><td>'+s[5]+' / '+s[6]+'</td></tr>';});
  g.longs.forEach(s=>{h+='<tr><td class="pos">лонг '+s[0]+'P</td><td>'+s[1]+'</td><td>'+s[2]+'</td><td>'+s[3]+'</td><td></td><td></td></tr>';});
  h+='</table>';
  if(typeof g.wing=='string')h+='<div style="font-size:12px;margin:2px 0" class="warn">Крыло для голых '+g.naked+': '+g.wing+' — закрыть риск можно только откупом</div>';
  else if(g.wing&&g.wing.length)h+='<div style="font-size:12px;margin:2px 0">Крыло для голых '+g.naked+': '+g.wing.map(w=>'купить '+w.K+'P @'+w.ask+' → $'+w.cost+', макс.убыток $'+w.ml).join(' · ')+'</div>';
 });
 $('tgroups').innerHTML=h||'<span style="color:#8b949e">дальних шорт-путов нет</span>';
 // вся ETH-книга
 const nz=v=>(v==null?'—':v);
 let bt='<tr><th>нога</th><th>тип</th><th>кол-во</th><th>дней</th><th>открыта, д</th><th>до страйка</th><th>в σ</th><th>ср. / марк</th><th>bid / ask</th><th>IV</th><th>Δ на 1</th><th>P(ITM)≈</th><th>Δ$</th><th>Γ$ 1%</th><th>Θ/д</th><th>Vega</th><th>безубыток</th><th>премия → сейчас</th><th>собрано</th><th>P&L</th></tr>',sdu=0,sg1=0,st_=0,sv=0,su=0;
 (d.book||[]).forEach(r=>{sdu+=r.dusd||0;sg1+=r.g1||0;st_+=r.t||0;sv+=r.v||0;su+=(r.up||0);
  const opt=r.K!=null, sh=r.q<0;
  bt+='<tr><td>'+r.sym+'</td><td class="'+(sh?'neg':'pos')+'">'+r.kind+'</td><td>'+r.q+'</td><td>'+nz(r.dte)+'</td><td>'+nz(r.age)+'</td>'
   +'<td>'+(opt?(r.dist>0?'+':'')+r.dist+'%':'—')+'</td><td class="'+(opt&&sh&&r.dsig!=null&&Math.abs(r.dsig)<1?'neg':'')+'">'+(opt?nz(r.dsig):'—')+'</td>'
   +'<td>'+nz(r.avg)+' / '+r.mark+'</td><td>'+(opt?r.bid+' / '+r.ask:'—')+'</td><td>'+(opt?r.iv+'%':'—')+'</td>'
   +'<td>'+r.d1+'</td><td>'+(opt?r.pitm+'%':'—')+'</td><td>'+r.dusd+'</td><td>'+(r.g1?sg(r.g1):'—')+'</td><td>'+(r.t?sg(r.t):'—')+'</td><td>'+(r.v?sg(r.v):'—')+'</td>'
   +'<td>'+(r.be!=null?r.be+' <span style="color:#8b949e">('+(r.be_dist>0?'+':'')+r.be_dist+'%)</span>':'—')+'</td>'
   +'<td>'+(opt?'$'+r.prem+' → $'+r.now:'—')+'</td><td class="'+(r.capt!=null&&r.capt>=d.cfg.tp_capture?'pos':'')+'">'+(r.capt!=null?r.capt+'%':'—')+'</td>'
   +'<td class="'+((r.up||0)>=0?'pos':'neg')+'">'+nz(r.up)+(r.up_pct!=null?' <span style="color:#8b949e">('+(r.up_pct>0?'+':'')+r.up_pct+'%)</span>':'')+'</td></tr>';});
 bt+='<tr style="font-weight:700"><td>итого</td><td colspan="11"></td><td>'+Math.round(sdu)+'</td><td>'+sg(Math.round(sg1*10)/10)+'</td><td>'+sg(Math.round(st_*100)/100)+'</td><td>'+sg(Math.round(sv*100)/100)+'</td><td colspan="3"></td><td class="'+(su>=0?'pos':'neg')+'">'+su.toFixed(2)+'</td></tr>';
 $('tbookt').innerHTML=bt+'<tr><td colspan="20" style="text-align:left;color:#8b949e;font-size:11px">«до страйка» — расстояние страйка от цены; «в σ» — то же в сигмах до экспирации (IV·√T), у проданных &lt;1σ подсвечено; P(ITM)≈|Δ на 1|; безубыток — на экспирации (страйк ∓ средняя премия); «собрано» — доля проданной премии, уже заработанная (тейк ≥'+d.cfg.tp_capture+'%); P&L в скобках — % от премии.</td></tr>';
 // прикрытие фьючерсом
 const f=d.fh||{},fu=f.fut||{};
 if(f.variants){
  $('tfh').innerHTML=tile('голых путов, ETH',f.naked_q,f.naked_q>0?'neg':'pos')+tile('Δ голых путов',f.d_naked+' ETH')+tile('Δ всей книги',f.d_book+' ETH')
   +tile('коллы до '+(fu.sym||'фьюча').replace('ETHUSDT-',''),f.call_q+' ETH')+tile('перп/фьюч сейчас',f.perp+' ETH')
   +tile(fu.sym||'фьючерс',fu.F?fu.F+' · базис '+fu.basis+'% ('+fu.basis_y+'% год.)':(fu.err||'—'));
  let vt='<tr><th>вариант</th><th>шорт '+(fu.sym||'')+'</th><th>$</th><th>сверх коллов</th>'+f.moves.map(m=>'<th>'+(m>0?'+':'')+m+'%</th>').join('')+'<th></th></tr>';
  const cell=p=>'<td class="'+(p<-0.25*d.eq?'neg':(p<0?'warn':'pos'))+'">'+(p>0?'+':'')+p+'</td>';
  vt+='<tr><td style="color:#8b949e">без шорта (сейчас)</td><td>0</td><td>0</td><td>—</td>'+f.base.map(cell).join('')+'<td></td></tr>';
  f.variants.forEach(v=>{vt+='<tr><td>'+v.name+'</td><td><b>'+v.h+'</b></td><td>'+v.usd+'</td><td class="'+(v.up_naked>0?'neg':'pos')+'">'+(v.up_naked>0?'голо '+v.up_naked:'закрыт')+'</td>'+v.pnl.map(cell).join('')+'<td style="color:#8b949e;text-align:left">'+v.note+'</td></tr>';});
  $('tfhv').innerHTML=vt;
  let lt='<tr><th>уровень</th><th>ETH</th><th>IV +</th><th>шорт под голые путы, ETH</th><th>дельта всей книги, ETH</th></tr>';
  f.ladder.forEach(r=>{lt+='<tr><td>'+r.mv+'%</td><td>'+r.S+'</td><td>'+r.iv_sh+'</td><td><b>'+r.h_naked+'</b></td><td class="'+(r.d_book<0?'neg':'pos')+'">'+sg(r.d_book)+'</td></tr>';});
  $('tfhl').innerHTML=lt;
 }
 let hh='<tr><th>хедж</th><th>кол-во</th><th>ср.</th><th>марк</th><th>дней</th><th>стоимость/д</th></tr>';
 (d.hedges||[]).forEach(x=>{hh+='<tr><td>'+x[0]+'</td><td>'+x[1]+'</td><td>'+x[2]+'</td><td>'+x[3]+'</td><td>'+x[4]+'</td><td class="neg">'+x[5]+'</td></tr>';});
 if(!(d.hedges||[]).length)hh+='<tr><td colspan="6" style="text-align:left;color:#8b949e">нет</td></tr>';
 $('thedge').innerHTML=hh;
 let sh='<tr><th>ход</th><th>ETH</th>'+d.shocks.map(s=>'<th>IV +'+s+'</th>').join('')+'</tr>';
 d.stress.forEach(r=>{sh+='<tr><td>'+(r.mv>0?'+':'')+r.mv+'%</td><td>'+r.S+'</td>'+r.pnl.map(p=>'<td class="'+(p<-0.25*d.eq?'neg':(p<0?'warn':'pos'))+'">'+(p>0?'+':'')+p+'</td>').join('')+'</tr>';});
 $('tstress').innerHTML=sh;
 let ch='<tr><th>экспира</th><th>дней</th><th>продать</th><th>купить</th><th>от цены</th><th>|Δ|</th><th>IV пр/пок</th><th>кредит</th><th>макс.убыток</th><th>доход/риск</th><th>в месяц</th><th>P(убыток)≈</th><th>P(макс)≈</th></tr>';
 (d.cands||[]).forEach(c=>{ch+='<tr><td>'+c.exp+'</td><td>'+c.dte+'</td><td>'+c.Ks+'P</td><td>'+c.Kl+'P</td><td>'+c.dist+'%</td><td>'+c.delta+'</td><td>'+c.ivs+' / '+c.ivl+'</td><td>'+c.credit+'</td><td>'+c.ml+'</td><td>'+c.rr+'%</td><td>'+c.rr_m+'%</td><td>'+Math.round(c.delta*100)+'%</td><td>'+Math.round(c.pl*100)+'%</td></tr>';});
 $('tcand').innerHTML=ch;
 $('upd').textContent='ETH '+d.S+' · хвосты  .  '+d.t;}
// ===================== ETH: всё по эфиру из /port и /arb (вкладка T) =====================
function renderEthPort(d){const g=d.groups.ETH;if(!g)return;const t=g.tot,a=d.acct,eq=a.eq||0,dl=eq*0.2,th=eq*0.01,v=g.vol||{};
 $('teov').innerHTML=tile('Δ ETH опц+перпы+инверс',sg(t.od_perp))+tile('Δ ETH +спот+Earn',sg(t.true))+tile('Δ$ ETH',sg(t.dusd),Math.abs(t.dusd)>dl?'warn':'pos')
  +tile('Γ ETH',t.g,cl(t.g))+tile('Γ$ на 1%',sg(t.g1),cl(t.g1))+tile('Θ /д',sg(t.t),Math.abs(t.t)>th?'warn':cl(t.t))+tile('Vega',sg(t.v),cl(t.v))+tile('uPnL ETH',sg(t.up)+'$',cl(t.up))
  +(v.iv?tile('ATM IV / RV30',v.iv+'% / '+v.rv+'%',vcls(v.ratio))+tile('IV/RV',v.ratio||'—',vcls(v.ratio)):'');
 $('tevol').innerHTML=v.iv?('Датчик волы: <b class="'+vcls(v.ratio)+'">'+v.band+'</b> · ATM '+v.exp+' K'+v.K+' ('+v.dte+'д) · ранг сессии '+(v.rank==null?'—':v.rank+'%')+' · диапазон '+v.lo+'–'+v.hi+'% · премия IV−RV '+sg(v.prem)+'пт'):'';
 bookPay($('tepay'),g,false);
 const N=g.now||[],mi=N.length?N.reduce((m,p)=>p[1]<m[1]?p:m,N[0]):null;
 $('tepaynote').innerHTML='Payoff ETH-группы: зелёная — на экспирации, синяя — T+0 (BS, IV без изменений), ось — цена ETH.'+(mi?' Худшая точка T+0: <b class="neg">'+sg(mi[1])+'$</b> при '+Math.round(mi[0]):'')+(g.be&&g.be.length?' · безубыток на экспирации: '+g.be.join(', '):'')+(g.be_now&&g.be_now.length?' · T+0: '+g.be_now.join(', '):'');
 // гамма и скальп — только ETH (та же логика, что в «Книге»)
 $('tegsteps').innerHTML='шаг сетки: '+[['0.5','0.5%'],['1','1%'],['1.5','1.5%'],['2','2%'],['be','= безубыток']].map(([k,l])=>`<span class="gbtn ${GSTEP==k?'on':''}" onclick="setGS('${k}')">${l}</span>`).join('');
 const r=(d.groups.ALL.gam||[]).find(x=>x.base=='ETH');
 if(!r){$('tegam').innerHTML='<tr><td style="color:#8b949e">опционов ETH нет</td></tr>';}
 else{const S=r.px,lg=r.g1>0,h=(GSTEP=='be'?(r.be||1):+GSTEP)/100,gg=r.g1/(S*S*0.01),q=Math.abs(gg)*S*h,cyc=100*Math.abs(r.g1)*h*h,fee=2*0.00036*q*S,net=cyc-fee;
  const ivd=r.iv?r.iv/Math.sqrt(365):null,rvd=r.rv?r.rv/Math.sqrt(365):null;let vv='—',vc='';
  if(r.be!=null&&rvd!=null){if(lg){vv=rvd>=r.be?'RV окупает Θ':'Θ дороже RV';vc=rvd>=r.be?'pos':'warn'}else{vv=rvd<=r.be?'продажа Γ окупается':'RV бьёт шорт Γ';vc=rvd<=r.be?'pos':'neg'}}
  const hq=-r.true,out=Math.abs(r.dusd)>dl,rb=Math.abs(r.dusd)<1?'—':`<span class="${out?'warn':''}">${hq<0?'продать':'купить'} ${gfq(hq)} ${r.perp}</span>${out?'':' <span style="color:#8b949e">(в коридоре ±20% экв.)</span>'}`;
  const up=S*(1+h),dn=S*(1-h),act=lg?['продать','купить']:['купить','продать'];
  $('tegam').innerHTML='<tr><th>цена</th><th>Γ шт/$1</th><th>Γ$ 1%</th><th>Θ/д</th><th>Δ шт / Δ$</th><th>безуб. ход/д</th><th>IV/д · RV/д</th><th>вывод</th><th>ребаланс сейчас</th><th>шаг</th><th>объём шага</th><th>сетка</th><th>$ за цикл</th><th>циклов/д на Θ</th></tr>'
   +`<tr><td>${gfp(S,S)}</td><td class="${cl(gg)}">${(+gg).toPrecision(3)}</td><td class="${cl(r.g1)}">${sg(r.g1)}</td><td class="${cl(r.t)}">${sg(r.t)}</td><td>${sg(r.true)} / ${sg(r.dusd)}$</td><td>${r.be==null?'—':r.be.toFixed(2)+'%'}</td><td>${ivd==null?'—':ivd.toFixed(2)}% · ${rvd==null?'—':rvd.toFixed(2)}%</td><td class="${vc}">${vv}</td><td style="text-align:left">${rb}</td>`
   +`<td>${(h*100).toFixed(2)}% (${gfp(S*h,S)})</td><td>${gfq(q)}</td><td>${act[0]} @ ${gfp(up,S)} · ${act[1]} @ ${gfp(dn,S)}</td><td class="${lg?(net>0?'pos':'neg'):'neg'}">${lg?'+':'−'}${Math.abs(net).toFixed(2)}</td><td>${lg&&r.t<0&&net>0?(Math.abs(r.t)/net).toFixed(1):'—'}</td></tr>`;
  gamProf($('tegprof'),r,h); gamExp($('tegexp'),r.exps||[]);
  $('tegnote').innerHTML='Слева: Δ$ (синяя) и Γ$ на 1% (жёлтая) при сдвиге цены ±15% (T+0), пунктиры — уровни сетки. Справа: Γ$ на 1% по экспирациям (зелёный — купленные, красный — проданные), под столбцами Θ/д. '+(lg?'Длинная Γ: продаёшь на росте, откупаешь на падении.':'<b class="warn">Короткая Γ: каждый ход против тебя, скальп = догонять цену (убыток за цикл).</b>');}
 // куплено / продано + покрытие
 const B=g.B,Sd=g.S,c=g.cov;
 $('tebs').innerHTML=(!B.n&&!Sd.n)?'<tr><td style="color:#8b949e">опционов нет</td></tr>':
  '<tr><th></th><th>ног</th><th>Γ</th><th>Δ шт</th><th>Γ$ 1%</th><th>Θ/д</th><th>Vega</th><th>Δ$</th></tr>'
  +[['Куплено',B,'pos','neg','pos'],['Продано',Sd,'neg','pos','neg']].map(([n,x,cg,ct,cv])=>`<tr><td>${n}</td><td>${x.n}</td><td class="${cg}">${x.g}</td><td>${x.d}</td><td class="${cg}">${x.g1}</td><td class="${ct}">${x.t}</td><td class="${cv}">${x.v}</td><td>${x.dusd}</td></tr>`).join('')
  +`<tr><td>Покрытие % (short÷long)</td><td></td><td></td><td></td><td class="${c.g>100?'neg':'warn'}">${c.g}</td><td class="pos">${c.t}</td><td class="warn">${c.v}</td><td></td></tr>`;
 const R=(g.rows||[]).filter(x=>x.base=='ETH');
 $('tespot').innerHTML='<tr><th>актив</th><th>цена</th><th>спот</th><th>Earn</th><th>опц Δ</th><th>опц Γ</th><th>опц Θ</th><th>опц V</th><th>Γ$ 1%</th><th>перп/фьюч Δ</th><th>истинная Δ</th><th>Δ$</th><th>нужный перп-хедж</th></tr>'
  +R.map(x=>`<tr><td>${x.base}</td><td>${x.px}</td><td>${x.spot}</td><td>${x.earn}</td><td>${x.od}</td><td>${x.og}</td><td class="${cl(x.ot)}">${x.ot}</td><td>${x.ov}</td><td>${x.g1}</td><td>${x.pd}</td><td>${x.true}</td><td>${sg(x.dusd)}</td><td class="warn">${Math.abs(x.dusd)<1?'нейтраль':(x.hedge>0?'ЛОНГ +':'ШОРТ ')+Math.abs(x.hedge)+' ETH'}</td></tr>`).join('');
 // ETH против корзины
 const b=d.groups.ALL.beta;
 if(b&&b.stress){const st=b.stress,pn=v2=>v2<0?'neg':(v2>0?'pos':'');let hh='<tr><th>сценарий</th>'+st.moves.map(m=>'<th>'+(m>0?'+':'')+m+'%</th>').join('')+'</tr>';
  st.rows.forEach(rr=>{hh+='<tr><td>'+rr.name+'</td>'+rr.pnl.map(v2=>'<td class="'+pn(v2)+'">'+sg(v2)+'</td>').join('')+'</tr>';});
  $('tebeta').innerHTML=hh;
  const er=(b.rows||[]).find(x=>x.base=='ETH');
  $('tebetanote').innerHTML='Корзина = '+(b.basket.join(', ')||'—')+'. Весь портфель, T+0, BS при текущей IV. Δ ETH-экв. портфеля '+sg(b.eq_total)+'$'+(er?' (сам ETH '+sg(er.eq)+'$)':'')+'. Эквити над MM: $'+Math.round(a.eq-a.mm)+'.';}
}
let EARB=0;
async function etick(){ if(TAB!='T')return; if(Date.now()-EARB<15000)return; EARB=Date.now();
 let d; try{d=await (await fetch('/arb')).json()}catch(e){$('teabar').textContent='арбитраж: offline';return}
 const isE=x=>/(^|[^A-Z])ETH/.test(x||''), f2=v=>v==null?'—':(+v).toFixed(2), ap=v=>`<td class="${cl(v)}"><b>${v==null?'—':v}</b></td>`;
 let h='<tr><th>связка ETH</th><th>ноги</th><th>кол-во</th><th>uPnL $</th><th>смысл</th></tr>';const L=(d.links||[]).filter(l=>isE(l.legs));
 L.forEach(l=>{h+=`<tr><td>${l.kind}</td><td style="text-align:left">${l.legs}</td><td>${l.q}</td><td class="${cl(l.up)}">${f2(l.up)}</td><td style="text-align:left;color:#8b949e">${l.note}</td></tr>`});
 if(!L.length)h+='<tr><td colspan=5 style="color:#8b949e">связок с ETH нет</td></tr>'; $('tealinks').innerHTML=h;
 h='<tr><th>открытая нога ETH</th><th>сторона</th><th>остаток / всего</th><th>Δ$</th><th>Θ/день</th><th>uPnL $</th><th>статус</th></tr>';const O=(d.open||[]).filter(o=>isE(o.sym));
 O.forEach(o=>{const st=o.close_ok?'<span class="pos">можно закрыть в плюс</span>':(o.up<0?'<span class="neg">в минусе — не закрывать, хеджировать</span>':'<span style="color:#8b949e">—</span>');
  h+=`<tr><td>${o.sym}</td><td>${o.side}</td><td>${o.rem}${o.part?' / '+o.q:''}</td><td class="${cl(o.dusd)}">${sg(o.dusd)}</td><td class="${cl(o.t)}">${f2(o.t)}</td><td class="${cl(o.up)}">${f2(o.up)}</td><td style="text-align:left">${st}</td></tr>`});
 $('teaopen').innerHTML=h;
 h='<tr><th>перп ETH</th><th>ставка %</th><th>% годовых</th><th>$/день</th></tr>';const Fd=(d.fund||[]).filter(r=>isE(r.sym));
 Fd.forEach(r=>{h+=`<tr><td>${r.sym}</td><td>${r.rate}</td><td>${r.apy}</td><td class="${cl(r.usd_day)}">${sg(r.usd_day)}</td></tr>`});
 if(!Fd.length)h+='<tr><td colspan=4 style="color:#8b949e">перпов ETH нет</td></tr>'; $('teafund').innerHTML=h;
 const s=d.scan; $('teabar').textContent=s?('скан '+d.scan_age+' с назад; ниже — только строки ETH'):(d.scan_busy?'скан идёт…':'скан не готов');
 if(!s){$('teascan').innerHTML='';return}
 h='<tr><th>сканер ETH</th><th>инструмент / эксп.</th><th>дней</th><th>направление</th><th>эдж</th><th>% год.</th></tr>';let n=0;
 (s.parity||[]).filter(r=>r.b=='ETH').forEach(r=>{n++;h+=`<tr><td>паритет K${r.K}</td><td>${r.exp} · ${r.hedge}${r.fund_only?' <span class="warn">только фандинг</span>':''}</td><td>${r.dte}</td><td>${r.kind}</td><td class="${cl(r.edge)}">${r.edge} $/ед.</td>${ap(r.apy)}</tr>`});
 (s.box||[]).filter(r=>r.b=='ETH').forEach(r=>{n++;h+=`<tr><td>бокс ${r.K1}–${r.K2}</td><td>${r.exp}</td><td>${r.dte}</td><td>${r.kind}</td><td>${r.edge} $</td>${ap(r.apy)}</tr>`});
 (s.cc||[]).filter(r=>r.b=='ETH').forEach(r=>{n++;h+=`<tr><td>cash-and-carry</td><td>${r.fut}</td><td>${r.dte}</td><td>${r.kind}${r.need_spot?' <span class="warn">(нужен спот)</span>':''}</td><td class="${cl(r.pct)}">${r.pct}%</td>${ap(r.apy)}</tr>`});
 (s.cal||[]).filter(r=>r.b=='ETH').forEach(r=>{n++;h+=`<tr><td>календарь перп/фьюч</td><td>${r.fut}</td><td>${r.dte}</td><td>${r.kind} (фандинг ${r.fund_pct}%)</td><td class="${cl(r.pct)}">${r.pct}%</td>${ap(r.apy)}</tr>`});
 (s.carry||[]).filter(r=>r.b=='ETH').forEach(r=>{n++;h+=`<tr><td>кэрри спот+перп</td><td>${r.sym} · ${r.rate}% (${r.ih}ч)</td><td>—</td><td>${r.dir}</td><td>безуб. ${r.be_days==null?'—':r.be_days} д</td>${ap(r.apy)}</tr>`});
 (s.inv||[]).filter(r=>r.b=='ETH').forEach(r=>{n++;h+=`<tr><td>инверс/линейный</td><td>инв ${r.inv} / лин ${r.lin}</td><td>—</td><td>${r.dir}</td><td>${r.diff}</td><td></td></tr>`});
 if(!n)h+='<tr><td colspan=6 style="color:#8b949e">по ETH кандидатов с эджем нет</td></tr>';
 $('teascan').innerHTML=h;}
function renderFrame(f,d){if(!f){$('tfr').innerHTML='';return}
 const S=d.S,n2=v=>v==null?'—':(+v).toFixed(2),n4=v=>v==null?'—':(+v).toFixed(4),c=v=>v>0?'pos':(v<0?'neg':''),R=f.reserve,H=f.hedge,V=f.vol,Sc=f.scalp;
 let h='';
 // 9. вол-сенсор для коллов
 h+='<div style="margin:2px 0 6px;padding:6px 10px;border-radius:8px;border:1px solid '+(V.ok?'#3fb950':'#d29922')+';font-size:13px;font-weight:600;color:'+(V.ok?'#3fb950':'#d29922')+'">'
  +(V.ok?'КОЛЛЫ БРАТЬ МОЖНО':'КОЛЛЫ ДОРОГИЕ — ждать')+': IV трёхдневки '+(f.e3||'—')+' ATM '+(V.iv3??'—')+'% '+(V.ok?'<':'≥')+' RV7 '+(V.rv7??'—')+'% <span style="color:#8b949e;font-weight:400">(правило: покупать коллы, только когда IV &lt; RV)</span></div>';
 // 1–2. маржа и резерв
 h+='<div class="row">'+tile('загрузка маржи IM/Equity',R.im_load+'%',R.im_load>50?(R.im_load>80?'neg':'warn'):'pos')
  +tile('держать USDT для IM ≤ 50%',R.need_now>0?'+$'+R.need_now:'хватает',R.need_now>0?'warn':'pos')+'</div>';
 h+='<table><tr><th>ETH</th>'+R.stress.map(r=>'<th>'+(r.mv>0?'+':'')+r.mv+'% ('+r.S+')</th>').join('')+'</tr>'
  +'<tr><td>P&L книги ETH</td>'+R.stress.map(r=>'<td class="'+c(r.pnl)+'">'+sg(r.pnl)+'</td>').join('')+'</tr>'
  +'<tr><td>запас над MM, $</td>'+R.stress.map(r=>'<td class="'+(r.room<0?'neg':(r.room<0.2*d.eq?'warn':'pos'))+'">'+Math.round(r.room)+'</td>').join('')+'</tr>'
  +'<tr><td>докинуть USDT до IM ≤ 50%</td>'+R.stress.map(r=>'<td class="'+(r.need>0?'warn':'pos')+'">'+(r.need>0?'+'+Math.round(r.need):'0')+'</td>').join('')+'</tr></table>';
 // 3–4. ноги
 const row=(name,r,cls)=>'<tr><td'+(cls?' class="'+cls+'"':'')+'>'+name+'</td><td>'+r.sym+'</td><td class="'+c(r.q)+'">'+r.q+'</td><td>'+(r.avg??'—')+' / '+r.mark+'</td><td class="'+c(r.up)+'">'+n2(r.up)+'</td><td>'+n4(r.d)+'</td><td>'+sg(r.dusd)+'</td><td>'+(r.t?sg(r.t):'—')+'</td><td>'+(r.v?sg(r.v):'—')+'</td></tr>';
 const tot=(name,t)=>t?'<tr style="font-weight:700;border-top:1px solid #30363d"><td colspan="4">'+name+'</td><td class="'+c(t.up)+'">'+n2(t.up)+'</td><td>'+n4(t.d)+'</td><td>'+sg(t.dusd)+'</td><td>'+sg(t.t)+'</td><td>'+sg(t.v)+'</td></tr>':'';
 let lt='<table><tr><th>нога</th><th>инструмент</th><th>кол-во</th><th>ср. / марк</th><th>P&L $</th><th>Δ, ETH (как Bybit)</th><th>Δ$</th><th>Θ/д</th><th>Vega</th></tr>';
 f.leg1.forEach(r=>lt+=row('1 · проданный пут',r,'neg')); lt+=tot('Нога 1 — проданные путы',f.leg1s);
 f.inv.forEach(r=>lt+=row('2 · инверс',r,r.q<0?'pos':'warn')); if(!f.inv.length)lt+='<tr><td>2 · инверс</td><td colspan="8" style="color:#8b949e;text-align:left">позиции нет</td></tr>'; lt+=tot('Нога 2 — инверс',f.invs);
 lt+='<tr><td colspan="9" style="text-align:left;font-size:12px">Состояние инверса: шорт <b>'+f.inv_short+'</b> из цели <b>'+f.inv_target+'</b> ETH (1:1 к голым путам) = <b>'+(f.inv_fill??0)+'%</b>'
  +(f.invs&&f.invs.q>0?' · <span class="neg">сейчас инверс в ЛОНГЕ '+f.invs.q+' ETH — противоположно каркасу</span>':'')+' · синтетический проданный колл = '+f.synth_q+' ETH</td></tr>';
 f.lin.forEach(r=>lt+=row('перп/фьюч (нейтраль)',r)); lt+=tot('Линейные перпы/фьючи',f.lins);
 f.leg3.forEach(r=>lt+=row('3 · купленный колл',r,'pos')); lt+=tot('Нога 3 — купленные коллы ≤7д',f.leg3s);
 f.one.forEach(r=>lt+=row('однодневка (продана)',r,'neg')); if(f.one.length)lt+=tot('Однодневки',f.ones);
 if(f.other.length){lt+=tot('Вне каркаса ('+f.other.map(r=>r.sym.replace('ETH-','')+' '+r.q).join(', ')+')',f.others);}
 lt+='</table>'; h+=lt;
 // 5. хедж-калькулятор
 const pos=H.h_now,mx=Math.max(H.h_full,pos,0.01),mn=Math.min(0,pos),X=v=>((v-mn)/(mx-mn)*100).toFixed(1);
 h+='<div class="row">'+tile('шорт для Δ-нейтрали путов',H.h_delta+' ETH')+tile('шорт для 1:1 (полное)',H.h_full+' ETH')
  +tile('сейчас шорт фьючей',(pos>0?'':'нетто-лонг ')+Math.abs(pos).toFixed(3)+' ETH',pos<0?'neg':(pos>=H.h_delta?'pos':'warn'))
  +tile('путь до 1:1',(H.pos_pct??'—')+'%')+tile('инверса позволяет маржа',H.inv_by_av+' ETH','')+tile('инверса до IM 50%',H.inv_to_im50+' ETH',H.inv_to_im50>0?'':'warn')+'</div>'
  +'<div style="position:relative;height:18px;background:#161b22;border:1px solid #30363d;border-radius:4px;margin:2px 0 2px;max-width:780px">'
  +'<div style="position:absolute;left:'+X(0)+'%;top:0;bottom:0;border-left:1px solid #8b949e"></div>'
  +'<div style="position:absolute;left:'+X(H.h_delta)+'%;top:0;bottom:0;border-left:2px solid #58a6ff"></div>'
  +'<div style="position:absolute;left:'+X(H.h_full)+'%;top:0;bottom:0;border-left:2px solid #3fb950"></div>'
  +'<div style="position:absolute;left:calc('+X(pos)+'% - 5px);top:2px;width:10px;height:12px;background:'+(pos<0?'#f85149':'#d29922')+';border-radius:2px"></div></div>'
  +'<div style="color:#8b949e;font-size:11px">шкала шорта фьючей, ETH: серая — 0, синяя — Δ-нейтраль путов ('+H.h_delta+'), зелёная — 1:1 ('+H.h_full+'), маркер — сейчас (инверс '+H.inv_q+' + линейные '+H.lin_q+'). «Позволяет маржа» — ориентир при IM '+(H.imr*100)+'% цены на 1 ETH инверса; в PM шорт против проданных путов до их дельты маржу снижает.</div>';
 // 6. калькулятор коллов
 h+='<div style="font-size:12px;margin:8px 0 2px">Коллы против роста синтетического проданного колла: сейчас синтетика '+f.synth_q+' ETH, нужно докупить <b>'+f.need_calls_now+'</b>; при полном наборе 1:1 — <b>'+f.need_calls_full+'</b> (уже есть трёхдневок '+f.l3c+'). Трёхдневка: <b>'+(f.e3||'—')+'</b></div>';
 let ct='<table><tr><th>колл</th><th>от цены</th><th>ask</th><th>IV</th><th>Δ</th><th>Θ/д за 1</th><th>стоимость сейчас / при 1:1</th><th>Θ/д сейчас / при 1:1</th></tr>';
 f.calc3.forEach(r=>ct+='<tr><td>'+r.K+'C</td><td>+'+r.dist+'%</td><td>'+r.ask+'</td><td>'+r.iv+'%</td><td>'+r.d+'</td><td class="neg">'+r.th+'</td><td>$'+r.cost_now+' / $'+r.cost_full+'</td><td class="neg">'+r.th_now+' / '+r.th_full+'</td></tr>');
 if(!f.calc3.length)ct+='<tr><td colspan="8" style="color:#8b949e">нет страйков +1…+12% в трёхдневке</td></tr>'; h+=ct+'</table>';
 // 7. однодневки
 h+='<div style="font-size:12px;margin:8px 0 2px">Однодневки <b>'+(f.e1||'—')+'</b>: сколько продать, чтобы покрыть 10 / 25 / 50% теты ноги 3 (|Θ| = $'+f.th3+'/д). Больше '+f.cover_q+' ETH — продажа голая (красным).</div>';
 let ot='<table><tr><th>продать колл</th><th>от цены</th><th>bid</th><th>IV</th><th>Δ</th><th>Θ/д за 1</th><th>10%</th><th>25%</th><th>50%</th></tr>';
 f.calc1.forEach(r=>ot+='<tr><td>'+r.K+'C</td><td>+'+r.dist+'%</td><td>'+r.bid+'</td><td>'+r.iv+'%</td><td>'+r.d+'</td><td>'+r.th+'</td>'+r.qs.map((q,i)=>'<td class="'+(r.naked[i]?'neg':'')+'">'+q+'</td>').join('')+'</tr>');
 if(!f.calc1.length)ot+='<tr><td colspan="9" style="color:#8b949e">'+(f.th3?'нет страйков':'теты ноги 3 нет — покрывать нечего')+'</td></tr>'; h+=ot+'</table>';
 // 8. скальп синтетики
 h+='<div class="row" style="margin-top:6px">'+tile('Δ синтетического пута (инверс + коллы ноги 3)',(Sc.syn_d>0?'+':'')+Sc.syn_d+' ETH',Math.abs(Sc.syn_d)>0.05?'warn':'pos')
  +tile('перп для нейтрали',Math.abs(Sc.perp_to_neutral)<0.005?'нейтраль':(Sc.perp_to_neutral<0?'продать ':'купить ')+Math.abs(Sc.perp_to_neutral).toFixed(3)+' ETHUSDT')
  +tile('скальп ETHUSDT: реализ. P&L (посл. '+(Sc.n??0)+' закрытий)',Sc.err?'—':sg(Sc.pnl7)+'$',c(Sc.pnl7))
  +tile('тета ноги 3 потрачена',sg(-Sc.theta_spent)+'$',Sc.theta_spent>0?'neg':'')
  +tile('скальп − тета',Sc.err?'—':sg(Math.round((Sc.pnl7-Sc.theta_spent)*100)/100)+'$',c(Sc.pnl7-Sc.theta_spent))+'</div>';
 $('tfr').innerHTML=h;}
// ===================== TradFi-сканер (вкладка F) =====================
let FSORT='turn', FTYPE='все', FMIN=1e6, FDATA=null;
try{FSORT=localStorage.getItem('fsort')||'turn';FTYPE=localStorage.getItem('ftype')||'все';const m=localStorage.getItem('fmin');if(m!=null)FMIN=+m;}catch(e){}
function setF(k,v){if(k=='s')FSORT=v;if(k=='t')FTYPE=v;if(k=='m')FMIN=v;try{localStorage.setItem('fsort',FSORT);localStorage.setItem('ftype',FTYPE);localStorage.setItem('fmin',FMIN)}catch(e){} if(FDATA)renderF(FDATA);}
async function ftick(){ if(TAB!='F')return;
 let d; try{d=await (await fetch('/tradfi')).json()}catch(e){$('fbar').textContent='offline';return}
 if(d.err&&!d.rows){$('fbar').textContent='ошибка: '+d.err;return} FDATA=d; renderF(d);}
function renderF(d){const T=d.top||{},fm=v=>v==null?'—':(v>=1e9?(v/1e9).toFixed(2)+' млрд':v>=1e6?(v/1e6).toFixed(1)+' млн':v>=1e3?(v/1e3).toFixed(0)+' тыс':Math.round(v));
 const tt=(l,r,k,u)=>tile(l,r?'<b>'+r.sym.replace('USDT','')+'</b> · '+(k=='turn'?'$'+fm(r.turn):k=='atr'?r.atr+'%/д':r.atr_sp+'×')+'<div style="font-size:11px;color:#8b949e;font-weight:400">'+r.name+'</div>':'—');
 $('ftop').innerHTML=tt('самый ликвидный (оборот 24ч)',T.liquid,'turn')+tt('самый волатильный (ATR, оборот ≥ $1 млн)',T.vol,'atr')+tt('лучший ход на спред (ATR/спред)',T.grid,'atr_sp')
  +tile('инструментов',d.n+' · с нулевым мейкером '+d.zero_maker)+tile('ликвидных (≥ $1 млн)',T.liq_n);
 const b=(k,v,l,cur)=>`<span class="gbtn ${cur==v?'on':''}" onclick="setF('${k}',${typeof v=='string'?"'"+v+"'":v})">${l}</span>`;
 $('fctl').innerHTML='сортировка: '+b('s','turn','оборот',FSORT)+b('s','atr','ATR %/д',FSORT)+b('s','atr_sp','ATR в спредах',FSORT)+b('s','oi','OI',FSORT)+b('s','chg','изм. 24ч',FSORT)
  +' &nbsp; тип: '+['все','акция','ETF','сырьё','форекс'].map(x=>b('t',x,x,FTYPE)).join('')
  +' &nbsp; оборот от: '+[[0,'любой'],[1e5,'$100 тыс'],[1e6,'$1 млн'],[1e7,'$10 млн']].map(([v,l])=>b('m',v,l,FMIN)).join('');
 $('fbar').textContent='обновлено '+d.t+' · ATR: '+(d.atr_busy?'считается…':(d.atr_age!=null?d.atr_age+' с назад ('+d.atr_n+')':'нет'))+(d.err?' · ошибка ATR: '+d.err:'');
 let R=(d.rows||[]).filter(r=>(FTYPE=='все'||r.type==FTYPE)&&r.turn>=FMIN);
 R.sort((a,b2)=>((b2[FSORT]??-1e18)-(a[FSORT]??-1e18)));
 let h='<tr><th>#</th><th>символ</th><th>название</th><th>тип</th><th>рынок</th><th>цена</th><th>изм. 24ч</th><th>оборот 24ч $</th><th>OI $</th><th>спред %</th><th>диапазон 24ч %</th><th>ATR14 %/д</th><th>ATR в спредах</th><th>мейкер / тейкер</th><th>фандинг %</th></tr>';
 R.slice(0,150).forEach((r,i)=>{h+=`<tr><td>${i+1}</td><td><b>${r.sym.replace('USDT','')}</b></td><td style="text-align:left;max-width:240px;overflow:hidden;white-space:nowrap">${r.name}</td><td>${r.type}</td><td>${r.region||'—'}</td><td>${r.px}</td><td class="${cl(r.chg)}">${sg(r.chg)}%</td><td>${fm(r.turn)}</td><td>${fm(r.oi)}</td><td>${r.spread??'—'}</td><td>${r.rng??'—'}</td><td>${r.atr??'—'}</td><td>${r.atr_sp??'—'}</td><td class="${r.maker===0?'pos':''}">${r.maker==null?'—':(r.maker*100).toFixed(3)+'%'} / ${r.taker==null?'—':(r.taker*100).toFixed(3)+'%'}</td><td class="${cl(-r.fund)}">${r.fund}</td></tr>`;});
 if(!R.length)h+='<tr><td colspan="15" style="color:#8b949e">ничего под фильтр</td></tr>';
 $('ftab').innerHTML=h; if(TAB=='F')$('upd').textContent='TradFi · '+d.t;}
// ===================== Ерши MEXC↔Bybit (вкладка R) =====================
let RSORT='net8', RONLY=true, RDATA=null;
try{RSORT=localStorage.getItem('rsort')||'net8';RONLY=localStorage.getItem('ronly')!=='0';}catch(e){}
function setR(k,v){if(k=='s')RSORT=v;if(k=='j')RONLY=v;try{localStorage.setItem('rsort',RSORT);localStorage.setItem('ronly',RONLY?'1':'0')}catch(e){} if(RDATA)renderR(RDATA);}
async function rtick(){ if(TAB!='R')return;
 let d; try{d=await (await fetch('/ruff')).json()}catch(e){$('rbar').textContent='offline';return}
 if(d.err&&!d.rows){$('rbar').textContent='ошибка: '+d.err;return} RDATA=d; renderR(d);}
function renderR(d){const T=d.top||{},fm=v=>v==null?'—':(v>=1e6?(v/1e6).toFixed(1)+' млн':v>=1e3?(v/1e3).toFixed(0)+' тыс':Math.round(v));
 const tt=(l,r,v)=>tile(l,r?'<b>'+r.coin+'</b> · '+v(r)+'<div style="font-size:11px;color:#8b949e;font-weight:400">оборот B '+fm(r.bturn)+' / M '+fm(r.mturn)+'</div>':'—');
 $('rtop').innerHTML=tt('лучший чистый эдж (цена + фандинг 8ч)',T.net,r=>sg(r.net8)+'% · '+r.dir)+tt('макс. разница фандинга (за 8ч)',T.fdiff,r=>'B '+r.fb+'% / M '+r.fm+'%')
  +tt('сильнее всех мечется',T.chop,r=>'путь/смещ. '+r.chop+'× · '+r.cross+' пересеч.')+tile('пар на обеих биржах',T.n+' · ершей '+T.junk);
 const b=(k,v,l,cur)=>`<span class="gbtn ${cur===v?'on':''}" onclick="setR('${k}',${typeof v=='string'?"'"+v+"'":v})">${l}</span>`;
 $('rctl').innerHTML='сортировка: '+b('s','net8','чистый + фандинг 8ч',RSORT)+b('s','net','чистый эдж цены',RSORT)+b('s','adiff','|разница цены|',RSORT)+b('s','afd','|разница фандинга|',RSORT)+b('s','chop','болтанка',RSORT)+b('s','rng','диапазон 24ч',RSORT)+b('s','bturn','оборот Bybit',RSORT)
  +' &nbsp; '+b('j',true,'только ерши',RONLY)+b('j',false,'все пары',RONLY);
 $('rbar').textContent='обновлено '+d.t+' · болтанка/периоды фандинга: '+(d.busy?'считаются…':(d.heavy_age!=null?d.heavy_age+' с назад ('+d.osc_n+' монет)':'нет'))+(d.err?' · ошибка: '+d.err:'');
 let R=(d.rows||[]).filter(r=>!RONLY||r.junk).map(r=>Object.assign({adiff:Math.abs(r.diff),afd:Math.abs(r.fdiff)},r));
 R.sort((a,b2)=>((b2[RSORT]??-1e18)-(a[RSORT]??-1e18)));
 const pc=v=>(v*100).toFixed(3)+'%';
 let h='<tr><th>#</th><th>монета</th><th>Bybit</th><th>MEXC</th><th>разница</th><th>спред B / M</th><th>эдж сейчас</th><th>чистый</th><th>фанд. 8ч в направлении</th><th>чистый + фанд.</th><th>направление</th><th>фандинг B / M, %/8ч</th><th>разница фанд.</th><th>оборот B / M</th><th>диапазон 24ч</th><th>болтанка 48ч</th><th>пересеч.</th><th>комиссии B (м/т) · M (м/т)</th></tr>';
 R.slice(0,120).forEach((r,i)=>{h+=`<tr style="cursor:pointer" onclick="rtvSet('BYBIT:${r.coin}USDT.P/MEXC:${r.coin}USDT.P')"><td>${i+1}</td><td><b>${r.coin}</b></td><td>${r.bb_px}</td><td>${r.mx_px}</td><td class="${Math.abs(r.diff)>0.5?'warn':''}">${sg(r.diff)}%</td><td>${r.bb_sp} / ${r.mx_sp}</td><td>${sg(r.edge)}%</td><td class="${r.net>0?'pos':''}">${sg(r.net)}%</td><td class="${cl(r.f8)}">${sg(r.f8)}%</td><td class="${r.net8>0?'pos':'neg'}"><b>${sg(r.net8)}%</b></td><td style="text-align:left;color:#8b949e">${r.net8>0?r.dir:''}</td>`
  +`<td>${r.fb} / ${r.fm}${r.fi_m==null?'*':''}</td><td class="${Math.abs(r.fdiff)>0.1?'warn':''}">${sg(r.fdiff)}</td><td>${fm(r.bturn)} / ${fm(r.mturn)}</td><td>${r.rng??'—'}%</td><td>${r.chop??'—'}</td><td>${r.cross??'—'}</td>`
  +`<td>${pc(r.bfee[0])}/${pc(r.bfee[1])} · ${pc(r.mfee[0])}/${pc(r.mfee[1])}${r.mzero?' <span class="pos">0-fee</span>':''}</td></tr>`;});
 if(!R.length)h+='<tr><td colspan="18" style="color:#8b949e">нет пар</td></tr>';
 $('rtab').innerHTML=h; if(TAB=='R')$('upd').textContent='ерши · '+d.t;}
// ===================== Капитал Bybit / MEXC (вкладка C) =====================
let CTGT=70, CMAN=0, CDATA=null;
try{const a=localStorage.getItem('ctgt');if(a!=null)CTGT=+a;const b=localStorage.getItem('cman');if(b!=null)CMAN=+b;}catch(e){}
function setC(k,v){v=+v;if(isNaN(v))return;if(k=='t')CTGT=Math.max(0,Math.min(100,v));if(k=='m')CMAN=Math.max(0,v);try{localStorage.setItem('ctgt',CTGT);localStorage.setItem('cman',CMAN)}catch(e){} if(CDATA)renderC(CDATA);}
async function ctick(){ if(TAB!='C')return;
 let d; try{d=await (await fetch('/capital')).json()}catch(e){$('cmsg').textContent='offline';return}
 if(d.err){$('cmsg').textContent='ошибка: '+d.err;return} CDATA=d; renderC(d);}
function renderC(d){const B=d.bybit,M=d.mexc,live=M.key&&M.eq!=null,mx=live?M.eq:CMAN,be=B.eq||0,tot=be+mx,sb=tot?be/tot*100:0;
 const tB=tot*CTGT/100,diff=be-tB,f0=v=>v==null?'—':(+v).toFixed(2);
 $('ctop').innerHTML=tile('Bybit эквити','$'+f0(be))+tile('MEXC '+(live?'(по ключу)':'(вручную)'),'$'+f0(mx),live?'':'warn')+tile('всего','$'+f0(tot))
  +tile('доля Bybit / MEXC',sb.toFixed(1)+'% / '+(100-sb).toFixed(1)+'%')+tile('цель Bybit / MEXC',CTGT+'% / '+(100-CTGT)+'%')
  +tile('до цели',Math.abs(diff)<1?'в цели':('перевести $'+Math.abs(diff).toFixed(0)+(diff>0?' Bybit → MEXC':' MEXC → Bybit')),Math.abs(diff)<0.05*tot?'pos':'warn');
 $('cctl').innerHTML='цель доли Bybit, %: <input type="number" min="0" max="100" value="'+CTGT+'" style="width:60px" onchange="setC(\'t\',this.value)">'
  +(live?'':' &nbsp; баланс MEXC вручную, $: <input type="number" min="0" value="'+CMAN+'" style="width:90px" onchange="setC(\'m\',this.value)">')
  +' <span style="color:#8b949e">(хранится в этом браузере)</span>';
 $('cmsg').innerHTML=live?'<span class="pos">MEXC подключён по read-only ключу · '+d.t+'</span>':(M.err?'<span class="neg">MEXC: '+M.err+'</span>':
  '<span class="warn">Ключа MEXC нет.</span> Создай на MEXC API-ключ <b>только с правом чтения</b> (без торговли и вывода; лучше с привязкой к IP) и положи в файл <code>'+M.path+'</code> в виде <code>{"api_key": "...", "api_secret": "..."}</code>. В чат ключ не присылай. После этого баланс и позиции MEXC подтянутся сами (сервер перечитывает файл при каждом запросе).');
 const bc=B.buf<1.5?'neg':(B.buf<2?'warn':'pos');
 $('cbb').innerHTML=tile('эквити','$'+f0(B.eq))+tile('свободно','$'+f0(B.av))+tile('IM',B.im_pct+'%',B.im_pct>50?'warn':'pos')+tile('запас до MM',B.buf+'×',bc)
  +tile('ETH-каркас: Δ$ / uPnL',sg(B.eth_dusd)+' / '+sg(B.eth_up)+'$')+tile('весь Bybit: Δ$ / uPnL',sg(B.all_dusd)+' / '+sg(B.all_up)+'$')
  +tile('стейблы','USDT '+(B.stable?B.stable.USDT:'—')+' · USDC '+(B.stable?B.stable.USDC:'—'));
 let h='';
 if(live){h+='<div class="row">'+tile('спот','$'+f0(M.spot_usd))+tile('фьючерсы (USDT/USDC)','$'+f0(M.fut_usd))+'</div>';
  h+='<table><tr><th>спот</th><th>кол-во</th><th>$</th></tr>'+(M.spot||[]).map(r=>'<tr><td>'+r.asset+'</td><td>'+r.qty+'</td><td>'+(r.usd??'—')+'</td></tr>').join('')+'</table>';
  h+='<table style="margin-top:6px"><tr><th>фьюч. счёт</th><th>эквити</th><th>свободно</th><th>маржа</th><th>uPnL</th></tr>'+(M.fut_assets||[]).map(r=>'<tr><td>'+r.asset+'</td><td>'+r.equity+'</td><td>'+r.avail+'</td><td>'+r.im+'</td><td class="'+cl(r.upl)+'">'+r.upl+'</td></tr>').join('')+'</table>';
  h+='<table style="margin-top:6px"><tr><th>позиция</th><th>сторона</th><th>контрактов</th><th>ср.</th><th>ликвидация</th><th>маржа</th><th>плечо</th></tr>'+((M.fut_pos||[]).map(p=>'<tr><td>'+p.sym+'</td><td class="'+(p.side=='long'?'pos':'neg')+'">'+p.side+'</td><td>'+p.vol+'</td><td>'+p.avg+'</td><td class="warn">'+p.liq+'</td><td>'+p.im+'</td><td>'+p.lev+'×</td></tr>').join('')||'<tr><td colspan="7" style="color:#8b949e">позиций нет</td></tr>')+'</table>';}
 else h='<div style="color:#8b949e;font-size:12px">данных MEXC нет — используется ручной баланс</div>';
 $('cmx').innerHTML=h; if(TAB=='C')$('upd').textContent='капитал · '+d.t;}
// ---- TradingView-виджет на вкладке «Ерши» ----
let RTV='BYBIT:SOXLUSDT.P/MEXC:SOXLUSDT.P', RTVI='15', RTVW=null;
try{RTV=localStorage.getItem('rtv')||RTV;RTVI=localStorage.getItem('rtvi')||RTVI;}catch(e){}
function rtvLoad(){if(window.TradingView){rtvDraw();return}
 if(document.getElementById('tvjs'))return; const sc=document.createElement('script');sc.id='tvjs';sc.src='https://s3.tradingview.com/tv.js';sc.onload=rtvDraw;document.head.appendChild(sc);}
function rtvDraw(){if(!window.TradingView||TAB!='R')return;$('rtv').innerHTML='';
 RTVW=new TradingView.widget({container_id:'rtv',symbol:RTV,interval:RTVI,autosize:true,theme:'dark',style:'1',locale:'ru',timezone:'Etc/UTC',
  hide_side_toolbar:false,allow_symbol_change:true,withdateranges:true,studies:['BB@tv-basicstudies']});
 const iv=[['1','1м'],['5','5м'],['15','15м'],['60','1ч'],['240','4ч'],['D','Д']];
 $('rtvctl').innerHTML='символ: <input id="rtvin" style="width:340px" value="'+RTV+'" onkeydown="if(event.key==\'Enter\')rtvSet(this.value)"> <span class="gbtn" onclick="rtvSet($(\'rtvin\').value)">показать</span>'
  +' &nbsp; '+iv.map(([v,l])=>`<span class="gbtn ${RTVI==v?'on':''}" onclick="rtvInt('${v}')">${l}</span>`).join('')
  +' &nbsp; <span class="gbtn" onclick="rtvSet(\'BYBIT:SOXLUSDT.P/MEXC:SOXLUSDT.P\')">SOXL</span>';}
// ===================== Секундный спред (вкладка R) =====================
let RTK='SOXL', RTKB=10, RTKW=1800, RTKD=null;
try{RTK=localStorage.getItem('rtk')||RTK;RTKB=+(localStorage.getItem('rtkb')||10);RTKW=+(localStorage.getItem('rtkw')||1800);}catch(e){}
function rtkSet(k,v){if(k=='c'){v=(v||'').toUpperCase().replace(/^.*?:/,'').replace(/USDT.*$/,'').replace(/[^A-Z0-9]/g,'');if(!v)return;RTK=v}
 if(k=='b')RTKB=+v; if(k=='w')RTKW=+v; try{localStorage.setItem('rtk',RTK);localStorage.setItem('rtkb',RTKB);localStorage.setItem('rtkw',RTKW)}catch(e){} rtkTick();}
async function rtkTick(){ if(TAB!='R')return; let d;
 try{d=await (await fetch('/rufftick?coin='+encodeURIComponent(RTK)+'&bar='+RTKB)).json()}catch(e){return}
 if(d.err&&!d.bars){$('rtktop').textContent='ошибка: '+d.err;return} RTKD=d; rtkRender(d);}
function rtkRender(d){const b=(k,v,l,cur)=>`<span class="gbtn ${cur==v?'on':''}" onclick="rtkSet('${k}',${v})">${l}</span>`;
 $('rtkctl').innerHTML='монета: <input id="rtkin" style="width:80px" value="'+d.coin+'" onkeydown="if(event.key==\'Enter\')rtkSet(\'c\',this.value)"> <span class="gbtn" onclick="rtkSet(\'c\',$(\'rtkin\').value)">показать</span>'
  +' &nbsp; бар: '+b('b',2,'2с',RTKB)+b('b',10,'10с',RTKB)+b('b',30,'30с',RTKB)+b('b',60,'1м',RTKB)
  +' &nbsp; окно: '+b('w',600,'10м',RTKW)+b('w',1800,'30м',RTKW)+b('w',3600,'1ч',RTKW)+b('w',7200,'2ч',RTKW)
  +' &nbsp; <span style="color:#8b949e">отслеживаются: '+(d.tracked||[]).join(', ')+'</span>';
 const now=Date.now()/1000, B=(d.bars||[]).filter(r=>r[0]>=now-RTKW), S=d.stats||{}, L=B.length?B[B.length-1]:null, f4=v=>v==null?'—':(v>0?'+':'')+(+v).toFixed(3)+'%';
 const zc=S.z==null?'':Math.abs(S.z)>=2?'warn':'';
 $('rtktop').innerHTML=tile('разница сейчас',L?f4(L[4]):'—')+tile('средняя / σ (2 ч)',S.mean!=null?f4(S.mean)+' / '+(+S.sd).toFixed(3)+'%':'копится…')
  +tile('z-score',S.z!=null?(S.z>0?'+':'')+S.z:'—',zc)+tile('диапазон',S.lo!=null?f4(S.lo)+' … '+f4(S.hi):'—')
  +tile('тейкер B + M',(+d.fee_b).toFixed(3)+' + '+(+d.fee_m).toFixed(3)+' = '+(+d.fee).toFixed(3)+'%')
  +tile('баров с эджем > комиссий',S.win!=null?S.win+' ('+S.win_pct+'%) · лучший '+f4(S.best):'—',S.win?'pos':'')
  +tile('точек',d.n+' · шаг '+d.step+'с'+(d.err?' · '+d.err:''));
 const T=(d.tv||[]).filter(r=>r[0]>=now-RTKW); RTKT=T; drawTV(); drawTick('rtkc',B,d.fee,RTKB,T); if(TAB=='R')$('upd').textContent='ерши · '+d.t;}
// ---- график «как TradingView»: цены Bybit/MEXC + объём + панель отношения ----
let RTKT=[], RTKH=null, RTKY=null;
function drawTV(){const c=$('rtkp');if(!c)return;const x=c.getContext('2d'),W=c.width,H=c.height,T=RTKT,bar=RTKB,coin=RTK;
 x.fillStyle='#131722';x.fillRect(0,0,W,H);x.font='12px -apple-system,Segoe UI,sans-serif';
 if(T.length<2){x.fillStyle='#787b86';x.fillText('сделки копятся…',14,24);return}
 const pr=78,pl=6,pb=26,top=8,split=Math.round((H-pb)*0.68),p2t=split+6,p2b=H-pb-4;
 const n=T.length,X=i=>pl+(W-pl-pr)*(i+.5)/n,bw=Math.max(1,(W-pl-pr)/n*.6);
 const pv=T.flatMap(r=>[r[1],r[2]]);let lo=Math.min(...pv),hi=Math.max(...pv);const pm=(hi-lo)*.08||0.02;lo-=pm;hi+=pm;
 const Y=p=>top+(split-top)*(1-(p-lo)/(hi-lo));
 const rv=T.map(r=>r[3]);let rlo=Math.min(...rv,0),rhi=Math.max(...rv,0);const rm=(rhi-rlo)*.1||0.02;rlo-=rm;rhi+=rm;
 const R=v=>p2t+(p2b-p2t)*(1-(v-rlo)/(rhi-rlo));
 const nice=(a,b,k)=>{const raw=(b-a)/k,p=Math.pow(10,Math.floor(Math.log10(raw))),m=[1,2,2.5,5,10].find(q=>q*p>=raw)*p;const o=[];for(let v=Math.ceil(a/m)*m;v<=b;v+=m)o.push(+v.toFixed(10));return o};
 x.strokeStyle='#1e222d';x.lineWidth=1;x.fillStyle='#b2b5be';x.textAlign='left';
 nice(lo,hi,8).forEach(v=>{const y=Y(v);x.beginPath();x.moveTo(pl,y);x.lineTo(W-pr,y);x.stroke();x.fillText(v.toFixed(2),W-pr+8,y+4)});
 nice(rlo,rhi,4).forEach(v=>{const y=R(v);x.beginPath();x.moveTo(pl,y);x.lineTo(W-pr,y);x.stroke();x.fillText(v.toFixed(2)+'%',W-pr+8,y+4)});
 const tstep=Math.max(1,Math.round(n/9));x.textAlign='center';
 for(let i=0;i<n;i+=tstep){const xx=X(i);x.strokeStyle='#1e222d';x.beginPath();x.moveTo(xx,top);x.lineTo(xx,p2b);x.stroke();x.fillStyle='#b2b5be';x.fillText(new Date(T[i][0]*1000).toTimeString().slice(0,8),xx,H-8)}
 x.textAlign='left';x.strokeStyle='#2a2e39';x.beginPath();x.moveTo(pl,split+2);x.lineTo(W,split+2);x.stroke();x.beginPath();x.moveTo(W-pr,0);x.lineTo(W-pr,H-pb);x.stroke();
 const vmax=Math.max(...T.map(r=>r[4]||0),1),vh=(split-top)*.3;
 T.forEach((r,i)=>{const up=i?r[1]>=T[i-1][1]:true;x.fillStyle=up?'rgba(38,166,154,.55)':'rgba(239,83,80,.55)';const h=(r[4]||0)/vmax*vh;x.fillRect(X(i)-bw/2,split-h,bw,h)});
 [[2,'#22c55e'],[1,'#2962ff']].forEach(([j,col])=>{x.strokeStyle=col;x.lineWidth=2;x.beginPath();T.forEach((r,i)=>{const y=Y(r[j]);i?x.lineTo(X(i),y):x.moveTo(X(i),y)});x.stroke()});
 x.strokeStyle='#787b86';x.setLineDash([2,3]);x.lineWidth=1;x.beginPath();x.moveTo(pl,R(0));x.lineTo(W-pr,R(0));x.stroke();x.setLineDash([]);
 x.strokeStyle='#f7525f';x.lineWidth=1.5;x.beginPath();T.forEach((r,i)=>{const y=R(r[3]);i?x.lineTo(X(i),y):x.moveTo(X(i),y)});x.stroke();x.lineWidth=1;
 const tag=(y,txt,bg)=>{x.fillStyle=bg;x.fillRect(W-pr+1,y-9,pr-2,18);x.fillStyle='#fff';x.fillText(txt,W-pr+6,y+4)};
 const L=T[n-1];let yb=Y(L[1]),ym=Y(L[2]);if(Math.abs(yb-ym)<19){if(yb<=ym)ym=yb+19;else yb=ym+19}
 tag(ym,L[2].toFixed(2),'#16a34a');tag(yb,L[1].toFixed(2),'#2962ff');tag(R(L[3]),(L[3]>0?'+':'')+L[3].toFixed(3)+'%','#f7525f');
 const h=RTKH!=null&&RTKH>=0&&RTKH<n?RTKH:n-1,Hr=T[h],F=T[0],chg=(a,b)=>{const d=a-b;return (d>=0?'+':'')+d.toFixed(2)+' ('+(d>=0?'+':'')+(d/b*100).toFixed(2)+'%)'};
 const fmV=v=>v>=1e6?(v/1e6).toFixed(2)+'M':v>=1e3?(v/1e3).toFixed(1)+'K':String(Math.round(v));
 x.font='13px -apple-system,Segoe UI,sans-serif';
 const leg=(y,parts)=>{let xx=12;parts.forEach(([t,col])=>{x.fillStyle=col;x.fillText(t,xx,y);xx+=x.measureText(t).width+6})};
 leg(top+16,[['Объём $','#b2b5be'],[fmV(Hr[4]||0),'#26a69a']]);
 leg(top+36,[['●','#2962ff'],[coin+'USDT Perpetual · Bybit','#d1d4dc'],[Hr[1].toFixed(2),'#2962ff'],[chg(Hr[1],F[1]),'#2962ff']]);
 leg(top+56,[['●','#22c55e'],[coin+' / USDT Perpetual · MEXC','#d1d4dc'],[Hr[2].toFixed(2),'#22c55e'],[chg(Hr[2],F[2]),'#22c55e']]);
 leg(p2t+16,[[coin+'USDT.P/'+coin+'USDT.P · '+(bar<60?bar+'с':(bar/60)+'м')+' · Bybit/MEXC','#d1d4dc'],[(Hr[3]>0?'+':'')+Hr[3].toFixed(3)+'%','#f7525f']]);
 x.font='12px -apple-system,Segoe UI,sans-serif';
 if(RTKH!=null&&RTKH>=0&&RTKH<n){const xx=X(RTKH);x.strokeStyle='#9598a1';x.setLineDash([4,4]);x.beginPath();x.moveTo(xx,top);x.lineTo(xx,p2b);x.stroke();
  if(RTKY!=null&&RTKY<p2b){x.beginPath();x.moveTo(pl,RTKY);x.lineTo(W-pr,RTKY);x.stroke();x.setLineDash([]);
   const v=RTKY<split?(lo+(hi-lo)*(1-(RTKY-top)/(split-top))).toFixed(2):(rlo+(rhi-rlo)*(1-(RTKY-p2t)/(p2b-p2t))).toFixed(3)+'%';tag(RTKY,v,'#363a45')}
  x.setLineDash([]);const tt=new Date(T[RTKH][0]*1000).toTimeString().slice(0,8);x.fillStyle='#363a45';const tw=x.measureText(tt).width+12;x.fillRect(xx-tw/2,H-pb+2,tw,20);x.fillStyle='#fff';x.textAlign='center';x.fillText(tt,xx,H-8);x.textAlign='left';}}
(function(){const bind=()=>{const c=$('rtkp');if(!c||c._b)return;c._b=1;
 c.addEventListener('mousemove',e=>{const r=c.getBoundingClientRect(),sx=c.width/r.width,mx=(e.clientX-r.left)*sx,my=(e.clientY-r.top)*sx,n=RTKT.length;if(!n)return;
  RTKH=Math.max(0,Math.min(n-1,Math.floor((mx-6)/((c.width-6-78)/n))));RTKY=my;drawTV();});
 c.addEventListener('mouseleave',()=>{RTKH=null;RTKY=null;drawTV();});};
 if(document.readyState!='loading')bind();else document.addEventListener('DOMContentLoaded',bind);})();
// ===================== Доходы (вкладка I) =====================
let IDAYS=30, IDATA=null, ICAT=null;
try{IDAYS=+(localStorage.getItem('idays')||30);}catch(e){}
function setI(k,v){if(k=='d'){IDAYS=+v;try{localStorage.setItem('idays',IDAYS)}catch(e){} IDATA=null;itick();} if(k=='c'){ICAT=ICAT===v?null:v;if(IDATA)renderI(IDATA);}}
async function itick(){ if(TAB!='I')return; let d;
 try{d=await (await fetch('/income?days='+IDAYS)).json()}catch(e){$('itop').textContent='offline';return}
 if(d.err&&!d.cats){$('itop').innerHTML='<span class="neg">ошибка: '+d.err+'</span>';return}
 if(!d.cats){$('ictl').innerHTML=ictl()+' &nbsp; <span class="warn">считается журнал за '+IDAYS+' дн. (~1 мин на 30 дн.)…</span>';return}
 IDATA=d; renderI(d);}
function ictl(){const b=(v,l)=>`<span class="gbtn ${IDAYS==v?'on':''}" onclick="setI('d',${v})">${l}</span>`;return 'окно: '+b(7,'7 дн')+b(30,'30 дн')+b(90,'90 дн');}
const ICOL={'ETH перпы и фьючерсы':'#58a6ff','Опционы ETH · проданные':'#3fb950','Опционы ETH · купленные':'#d29922','Спот (переоценка по текущей цене)':'#bc8cff','Прочие перпы (TradFi, альты)':'#8b949e','Проценты по займу':'#f85149'};
const icol=n=>ICOL[n]||(n.startsWith('Опционы')?'#a5d6ff':'#6e7681');
function renderI(d){const m=v=>v==null?'—':(v>0?'+':'')+(+v).toFixed(2)+'$';
 $('ictl').innerHTML=ictl()+' &nbsp; <span style="color:#8b949e">строк журнала '+d.rows+' · посчитано '+d.t+(d.busy?' · обновляется…':'')+'</span>';
 const best=d.cats[0],worst=d.cats[d.cats.length-1];
 $('itop').innerHTML=tile('итог за '+d.days+' дн.',m(d.total),cl(d.total))+tile('главный источник',best?best.name+'<div style="font-size:12px">'+m(best.tot)+'</div>':'—','pos')
  +tile('главный убыток',worst?worst.name+'<div style="font-size:12px">'+m(worst.tot)+'</div>':'—','neg')
  +tile('комиссии',m(-d.fees),'neg')+tile('ликвидации (деньги)',m(d.liq)+'<div style="font-size:11px;color:#8b949e">событий: '+d.liq_events.length+'</div>',d.liq<0?'neg':'')
  +tile('фандинг',m(-d.funding),cl(-d.funding))+tile('проценты по займу',m(d.interest),cl(d.interest))
  +tile('пополнения (не доход)','$'+(+d.deposits).toFixed(0))+tile('открытые опционы сейчас',m(d.open_val));
 let h='<tr><th>категория</th><th>итог</th><th>деньги</th><th>+ стоимость открытых</th><th>прибыльные символы</th><th>убыточные</th><th>комиссии</th><th>ликвидации</th><th>символов</th></tr>';
 d.cats.forEach(c=>{h+=`<tr style="cursor:pointer;${ICAT==c.name?'background:#1f2937':''}" onclick="setI('c','${c.name}')"><td style="text-align:left"><span style="color:${icol(c.name)}">■</span> ${c.name}</td><td class="${cl(c.tot)}"><b>${m(c.tot)}</b></td><td class="${cl(c.cash)}">${m(c.cash)}</td><td>${c.open?m(c.open):'—'}</td><td class="pos">${m(c.win)}</td><td class="neg">${m(c.loss)}</td><td class="neg">${m(-c.fee)}</td><td class="${cl(c.liq)}">${c.liq?m(c.liq):'—'}</td><td>${c.n}</td></tr>`});
 h+=`<tr><td style="text-align:left"><b>всего</b> (с процентами)</td><td class="${cl(d.total)}"><b>${m(d.total)}</b></td><td colspan="7"></td></tr>`;
 $('icat').innerHTML=h;
 const S=d.syms.filter(x=>!ICAT||x.cat==ICAT);$('iflt').innerHTML=ICAT?'— фильтр: '+ICAT+' <span class="gbtn" onclick="setI(\'c\',null)">сбросить</span>':'(клик по категории — фильтр)';
 const row=x=>`<tr><td style="text-align:left">${x.sym}${x.is_open?' <span class="warn">откр.</span>':''}</td><td class="${cl(x.tot)}"><b>${m(x.tot)}</b></td><td>${m(x.cash)}</td><td>${x.is_open?m(x.open):'—'}</td><td class="neg">${x.fee?m(-x.fee):'—'}</td><td class="${cl(x.liq)}">${x.liq?m(x.liq):'—'}</td><td>${x.n}</td><td style="color:#8b949e">${x.last.slice(5)}</td></tr>`;
 const th='<tr><th>инструмент</th><th>итог</th><th>деньги</th><th>открыт сейчас</th><th>комиссии</th><th>ликвид.</th><th>операций</th><th>посл.</th></tr>';
 $('iwin').innerHTML=th+S.filter(x=>x.tot>0).slice().reverse().slice(0,20).map(row).join('');
 $('ilos').innerHTML=th+S.filter(x=>x.tot<0).slice(0,20).map(row).join('');
 $('iliq').innerHTML='<b>Ликвидации</b> (час · операций): '+(d.liq_events.length?d.liq_events.map(e=>e[0]+' · '+e[1]).join(' &nbsp;|&nbsp; '):'нет')
  +'<br><b>Пополнения</b>: '+(d.dep_list.length?d.dep_list.map(e=>e[0].slice(5)+' '+e[2]+' '+e[1]+' ($'+e[3]+')').join(' &nbsp;|&nbsp; '):'нет');
 drawIDay(d); if(TAB=='I')$('upd').textContent='доходы · '+d.t;}
function drawIDay(d){const c=$('iday'),x=c.getContext('2d'),W=c.width,H=c.height;x.clearRect(0,0,W,H);x.font='10px sans-serif';
 const D=d.daily;if(!D.length)return;const names=[...new Set(D.flatMap(r=>Object.keys(r[1])))];
 let cum=0;const cu=D.map(r=>cum+=Object.values(r[1]).reduce((a,b)=>a+b,0));
 let hi=0,lo=0;D.forEach(r=>{let p=0,n=0;Object.values(r[1]).forEach(v=>v>0?p+=v:n+=v);hi=Math.max(hi,p);lo=Math.min(lo,n)});hi=Math.max(hi,...cu);lo=Math.min(lo,...cu);const mg=(hi-lo)*.06||1;hi+=mg;lo-=mg;
 const pl=56,pr=10,pt=10,pb=20,bw=(W-pl-pr)/D.length,Y=v=>pt+(H-pt-pb)*(1-(v-lo)/(hi-lo));
 for(let k=0;k<=4;k++){const v=hi-(hi-lo)*k/4,yy=Y(v);x.strokeStyle='#21262d';x.beginPath();x.moveTo(pl,yy);x.lineTo(W-pr,yy);x.stroke();x.fillStyle='#8b949e';x.fillText((v>0?'+':'')+v.toFixed(0)+'$',4,yy+3)}
 x.strokeStyle='#484f58';x.beginPath();x.moveTo(pl,Y(0));x.lineTo(W-pr,Y(0));x.stroke();
 D.forEach((r,i)=>{let p=0,n=0;const xx=pl+i*bw+bw*.15,w=bw*.7;names.forEach(k=>{const v=r[1][k]||0;if(!v)return;x.fillStyle=icol(k);if(v>0){x.fillRect(xx,Y(p+v),w,Y(p)-Y(p+v));p+=v}else{x.fillRect(xx,Y(n),w,Y(n+v)-Y(n));n+=v}});
  if(D.length<=40||i%Math.ceil(D.length/30)==0){x.fillStyle='#8b949e';x.fillText(r[0].slice(8),xx,H-6)}});
 x.strokeStyle='#e6edf3';x.lineWidth=1.5;x.beginPath();cu.forEach((v,i)=>{const xx=pl+i*bw+bw/2;i?x.lineTo(xx,Y(v)):x.moveTo(xx,Y(v))});x.stroke();x.lineWidth=1;
 $('ileg').innerHTML=names.map(k=>`<span style="color:${icol(k)}">■</span> ${k}`).join(' &nbsp; ')+' &nbsp; <span style="color:#e6edf3">—</span> накопленный итог '+(cu[cu.length-1]>0?'+':'')+cu[cu.length-1].toFixed(0)+'$';}
// ===================== Сетки (вкладка R) =====================
let GRW={gr:'today',ga:'today'}, GRDATA=null;
try{GRW.gr=localStorage.getItem('grwin')||'today';GRW.ga=localStorage.getItem('gawin')||'today';}catch(e){}
function setGR(v,P){P=P||'gr';GRW[P]=v;try{localStorage.setItem(P=='gr'?'grwin':'gawin',v)}catch(e){} P=='gr'?grtick():gatick();}
async function grtick(){ if(TAB!='S')return; let d;
 try{d=await (await fetch('/grid?f=sox&win='+GRW.gr)).json()}catch(e){return}
 if(d.err){$('grtop').innerHTML='<span class="neg">ошибка: '+d.err+'</span>';return} GRDATA=d; renderGRx(d,'gr');}
async function gatick(){ if(TAB!='L')return; let d;
 try{d=await (await fetch('/grid?f=alt&win='+GRW.ga)).json()}catch(e){return}
 if(d.err){$('gatop').innerHTML='<span class="neg">ошибка: '+d.err+'</span>';return} renderGRx(d,'ga');}
function renderGR(d){renderGRx(d,'gr');}
function renderGRx(d,P){const b=(v,l)=>`<span class="gbtn ${GRW[P]==v?'on':''}" onclick="setGR('${v}','${P}')">${l}</span>`,m=v=>v==null?'—':(v>0?'+':'')+(+v).toFixed(2)+'$',T=d.tot,alt=P=='ga';
 $(P+'ctl').innerHTML='окно: '+b('today','сегодня')+b('24h','24 ч')+b('7d','7 дн')+' &nbsp; <span style="color:#8b949e">обновлено '+d.t+'</span>';
 $(P+'top').innerHTML=tile('итог (реализ. + остаток)',m(T.tot),cl(T.tot))+tile('реализовано',m(T.real),cl(T.real))+tile('нереализовано по остатку',m(T.upl),cl(T.upl))
  +(alt?tile('фандинг по текущей ставке, $/сутки',m(T.fund_day),T.fund_day<-5?'neg':cl(T.fund_day)):'')
  +tile('исполнений · оборот',T.n_ex+' · $'+(+T.turn).toFixed(0))+tile('комиссии',m(-T.fee))+tile('остаток в позициях','$'+(+T.inv_usd).toFixed(0))
  +tile('заявки сверх позиции','$'+(+T.over_usd).toFixed(0),T.over_usd>100?'warn':'');
 let h='<tr><th>символ</th><th>итог</th><th>реализ.</th><th>закрытий +/−</th><th>лучш. / худш.</th><th>исполнений · мейкер</th><th>оборот</th><th>комис.</th><th>остаток</th><th>ср. → марк</th><th>нереализ.</th>'+(alt?'<th>24ч</th><th>фандинг сейчас</th><th>фандинг $/сутки</th>':'')+'<th>покупки (шт · кол-во · $ · цены)</th><th>продажи</th><th>поз. если всё купят / продадут</th><th>сверх поз., $</th></tr>';
 d.rows.forEach(r=>{const pr=a=>a[0]==null?'':a[0]+'–'+a[1];
  h+=`<tr><td style="text-align:left"><b>${r.sym.replace('USDT','')}</b>${alt?' <span class="gbtn" style="padding:0 4px" onclick="altSym(\''+r.sym+'\')">лаб.</span>':''}</td><td class="${cl(r.tot)}"><b>${m(r.tot)}</b></td><td class="${cl(r.real)}">${m(r.real)}</td><td>${r.win} / <span class="${r.loss?'neg':''}">${r.loss}</span></td><td>${r.best==null?'—':r.best+' / '+r.worst}</td><td>${r.n_ex} · ${r.maker??'—'}%</td><td>$${(+r.turn).toFixed(0)}</td><td>${(+r.fee).toFixed(3)}</td>`
  +`<td class="${r.size>0?'pos':r.size<0?'neg':''}">${r.size} <span style="color:#8b949e">($${(+r.inv_usd).toFixed(0)})</span></td><td>${r.avg??'—'} → ${r.mark}</td><td class="${cl(r.upl)}">${m(r.upl)}</td>`
  +(alt?`<td class="${cl(r.chg24)}">${r.chg24==null?'—':(r.chg24>0?'+':'')+r.chg24+'%'}</td><td class="${r.fund<0?'neg':''}">${r.fund==null?'—':r.fund+'% / '+r.fund_h+'ч'}</td><td class="${r.fund_day<-1?'neg':cl(r.fund_day)}"><b>${r.fund_day==null?'—':m(r.fund_day)}</b></td>`:'')
  +`<td>${r.o_buy} · ${r.o_buy_q} · $${(+r.o_buy_usd).toFixed(0)} <span style="color:#8b949e">${pr(r.o_buy_px)}</span></td><td>${r.o_sell} · ${r.o_sell_q} · $${(+r.o_sell_usd).toFixed(0)} <span style="color:#8b949e">${pr(r.o_sell_px)}</span></td>`
  +`<td>${r.after_buy} / ${r.after_sell}</td><td class="${r.over_usd>50?'warn':''}">$${(+r.over_usd).toFixed(0)}${r.ro?' <span style="color:#8b949e">ro '+r.ro+'</span>':''}</td></tr>`;});
 if(!d.rows.length)h+='<tr><td colspan="18" style="color:#8b949e">'+(alt?'позиций и сеток по шиткоинам нет':'активных сеток SOXL/SOXS нет')+'</td></tr>';
 $(P+'tab').innerHTML=h;}
// ===================== Лаборатория ёрша (вкладка L) =====================
let AL={sym:'INITUSDT',mode:'core',core:'spot',lo:'',hi:'',n:20,usd:100,bars:720}, ALD=null;
try{Object.assign(AL,JSON.parse(localStorage.getItem('altlab')||'{}'))}catch(e){}
function alSave(){try{localStorage.setItem('altlab',JSON.stringify(AL))}catch(e){}}
function altSym(s){AL.sym=s;AL.lo='';AL.hi='';alSave();altLab();$('alctl').scrollIntoView({block:'start'});}
function setAL(k,v){if(k=='sym'){v=(v||'').toUpperCase().trim().replace(/[^A-Z0-9]/g,'');if(!v)return;if(!v.endsWith('USDT'))v+='USDT';AL.lo='';AL.hi='';}
 if(['n','usd','bars'].includes(k)){v=+v;if(!(v>0))return;} if(['lo','hi'].includes(k)){v=String(v||'').trim();if(v&&!(+v>0))return;}
 AL[k]=v;alSave();altLab();}
async function altLab(){ if(TAB!='L')return; const q=new URLSearchParams(AL).toString(); let d;
 $('alwarn').innerHTML='<span style="color:#8b949e">считаю '+AL.sym+'…</span>';
 try{d=await (await fetch('/altlab?'+q)).json()}catch(e){$('alwarn').textContent='offline';return}
 if(d.err){$('alwarn').innerHTML='<span class="neg">'+d.err+'</span>';return} ALD=d; renderAL(d);}
function renderAL(d){const m=v=>v==null?'—':(v>0?'+':'')+(+v).toFixed(2)+'$',R=d.res,F=d.fund_now,X=d.px,f6=v=>(+v).toPrecision(5);
 if(!['alsym','allo','alhi','aln','alusd'].includes(document.activeElement.id)){const b=(k,v,l)=>`<span class="gbtn ${String(AL[k])==String(v)?'on':''}" onclick="setAL('${k}','${v}')">${l}</span>`;
  $('alctl').innerHTML='перп: <input id="alsym" style="width:110px" value="'+d.sym+'" onkeydown="if(event.key==\'Enter\')setAL(\'sym\',this.value)"> <span class="gbtn" onclick="setAL(\'sym\',$(\'alsym\').value)">считать</span>'
   +' &nbsp; схема: '+b('mode','core','ядро + шорт по шкале')+b('mode','neutral','нейтральная сетка')+b('mode','trend','трендовая')
   +(d.mode=='core'?' &nbsp; ядро: '+b('core','spot','спот')+b('core','perp','перп'):'')
   +'<br>коридор: от <input id="allo" style="width:90px" placeholder="мин окна" value="'+(AL.lo||'')+'" onchange="setAL(\'lo\',this.value)"> до <input id="alhi" style="width:90px" placeholder="макс окна" value="'+(AL.hi||'')+'" onchange="setAL(\'hi\',this.value)">'
   +' &nbsp; уровней: <input id="aln" type="number" min="2" style="width:56px" value="'+d.n+'" onchange="setAL(\'n\',this.value)"> &nbsp; размер, $: <input id="alusd" type="number" min="5" style="width:70px" value="'+d.usd+'" onchange="setAL(\'usd\',this.value)">'
   +' &nbsp; окно: '+b('bars',360,'6 ч')+b('bars',720,'12 ч')+b('bars',1440,'24 ч')+b('bars',2880,'48 ч')+' &nbsp; <span style="color:#8b949e">'+d.t+'</span>';}
 const shortPays=F.rate<0, danger=Math.abs(F.day_pct)>=1;
 $('alwarn').innerHTML=(danger?'<span class="'+(shortPays?'neg':'warn')+'"><b>Фандинг '+d.sym.replace('USDT','')+' сейчас '+F.rate+'% каждые '+F.every_h+' ч = '+F.day_pct+'% в сутки</b> — '+(shortPays?'платит ШОРТ':'платит ЛОНГ')+'. ':'<span style="color:#8b949e">Фандинг сейчас '+F.rate+'% / '+F.every_h+' ч ('+F.day_pct+'% в сутки). ')
  +'За окно: '+F.sum+'% ('+F.n+' списаний), за 24 ч: '+F.sum24+'%.'+(d.mode!='trend'&&F.short_day_usd?' Шорт-нога этой схемы сейчас: '+m(F.short_day_usd)+' в сутки.':'')+'</span>'
  +'<br><span style="color:#8b949e">Шаг сетки '+d.step_pct+'% против комиссии за круг '+d.fee_rt_pct+'% (мейкер '+(d.maker*100).toFixed(3)+'%, тейкер '+(d.taker*100).toFixed(3)+'%)'+(d.step_pct<3*d.fee_rt_pct?' — <span class="warn">шаг меньше трёх комиссий</span>':'')+'.</span>';
 $('altop').innerHTML=tile('итог схемы за '+d.hours+' ч',m(R.total)+'<div style="font-size:11px;color:#8b949e;font-weight:400">'+(R.pct>0?'+':'')+R.pct+'% от $'+d.usd+' · '+m(R.per_h)+'/ч</div>',cl(R.total))
  +tile('эдж сетки (сетка − держать старт)',m(R.edge)+'<div style="font-size:11px;color:#8b949e;font-weight:400">сетка '+m(R.grid)+' · держать '+m(R.hold)+'</div>',cl(R.edge))
  +tile('фандинг',m(R.fund)+(d.mode=='core'?'<div style="font-size:11px;color:#8b949e;font-weight:400">шорт '+m(R.fund_short)+' · ядро-перп '+m(R.fund_core)+'</div>':''),cl(R.fund))
  +tile('сделок · оборот · комиссии',R.trades+' · $'+(+R.turn).toFixed(0)+' · '+m(-R.fee))+tile('макс. позиция · просадка','$'+(+R.max_usd).toFixed(0)+' · '+m(R.mdd),R.mdd<-0.2*d.usd?'neg':'')
  +tile('позиция в конце',(R.pos_end_usd<0?'шорт ':'лонг ')+'$'+Math.abs(R.pos_end_usd).toFixed(0))
  +tile('цена за окно',(X.chg>0?'+':'')+X.chg+'% · размах '+X.rng+'%<div style="font-size:11px;color:#8b949e;font-weight:400">'+f6(X.start)+' → '+f6(X.end)+' · коридор '+f6(d.lo)+'–'+f6(d.hi)+'</div>')
  +tile('«ёрш» (путь / смещение)',d.chop+'×',d.chop>=10?'pos':'warn');
 drawAL(d);}
function drawAL(d){const c=$('alc'),x=c.getContext('2d'),W=c.width,H=c.height,C=d.curve;x.clearRect(0,0,W,H);x.font='11px sans-serif';if(!C||C.length<2)return;
 const pl=60,pr=60,pt=12,pb=20,v=C.map(r=>r[1]),q=C.map(r=>r[2]);let lo=Math.min(...v,0),hi=Math.max(...v,0);const mg=(hi-lo)*.08||1;lo-=mg;hi+=mg;
 let qlo=Math.min(...q,0),qhi=Math.max(...q,0);const qm=(qhi-qlo)*.08||1;qlo-=qm;qhi+=qm;
 const t0=C[0][0],t1=C[C.length-1][0],X=t=>pl+(W-pl-pr)*(t-t0)/Math.max(t1-t0,1),Y=a=>pt+(H-pt-pb)*(1-(a-lo)/(hi-lo)),Q=a=>pt+(H-pt-pb)*(1-(a-qlo)/(qhi-qlo));
 for(let k=0;k<=4;k++){const a=hi-(hi-lo)*k/4,y=Y(a);x.strokeStyle='#21262d';x.beginPath();x.moveTo(pl,y);x.lineTo(W-pr,y);x.stroke();x.fillStyle='#8b949e';x.fillText((a>0?'+':'')+a.toFixed(1)+'$',4,y+3);x.fillText('$'+(qhi-(qhi-qlo)*k/4).toFixed(0),W-pr+6,y+3)}
 x.strokeStyle='#484f58';x.beginPath();x.moveTo(pl,Y(0));x.lineTo(W-pr,Y(0));x.stroke();
 x.strokeStyle='rgba(188,140,255,.55)';x.lineWidth=1;x.beginPath();C.forEach((r,i)=>{const y=Q(r[2]);i?x.lineTo(X(r[0]),y):x.moveTo(X(r[0]),y)});x.stroke();
 x.strokeStyle=v[v.length-1]>=0?'#3fb950':'#f85149';x.lineWidth=1.8;x.beginPath();C.forEach((r,i)=>{const y=Y(r[1]);i?x.lineTo(X(r[0]),y):x.moveTo(X(r[0]),y)});x.stroke();x.lineWidth=1;
 const hm=s=>{const D=new Date(s*1000);return ('0'+D.getDate()).slice(-2)+'.'+('0'+(D.getMonth()+1)).slice(-2)+' '+D.toTimeString().slice(0,5)};x.fillStyle='#8b949e';x.fillText(hm(t0),pl,H-5);x.fillText(hm(t1),W-pr-70,H-5);
 x.fillStyle='#3fb950';x.fillText('P&L схемы без фандинга, $ (левая ось)',pl+6,pt+10);x.fillStyle='#bc8cff';x.fillText('позиция, $ (правая ось; + лонг, − шорт)',pl+260,pt+10);}
// ===================== Пара SOXL / SOXS (вкладка R) =====================
let PRDATA=null;
async function prtick(){ if(TAB!='S')return; let d;
 try{d=await (await fetch('/pair')).json()}catch(e){return}
 if(d.err){$('prwarn').innerHTML='<span class="neg">ошибка: '+d.err+'</span>';return} PRDATA=d; renderPR(d); if(PCT.length)drawPC();}
let PRM='eq', PRB=10, LBS=0.05, LBL=12, LBW=360, LBD=null;
try{PRM=localStorage.getItem('prm')||PRM;PRB=+(localStorage.getItem('prb')??PRB);LBS=+(localStorage.getItem('lbs')||LBS);LBL=+(localStorage.getItem('lbl')||LBL);LBW=+(localStorage.getItem('lbw')||LBW);}catch(e){}
function setPRm(k,v){if(k=='m')PRM=v;if(k=='b'){v=+v;if(isNaN(v)||v<0)return;PRB=v}try{localStorage.setItem('prm',PRM);localStorage.setItem('prb',PRB)}catch(e){} if(PRDATA)renderPR(PRDATA);}
function renderPR(d){const m=v=>v==null?'—':(v>0?'+':'')+(+v).toFixed(2)+'$',u=v=>(v<0?'−':'')+'$'+Math.abs(+v).toFixed(0),a=d.a,b=d.b,H=d.hist||{},A=d.acct||{};
 const V=(d.var&&d.var[PRM])||d.var.eq,thr=PRB/100*d.gross,inBand=Math.abs(V.net)<=thr;
 if(document.activeElement.id!='prbin'){const bt=(v,l)=>`<span class="gbtn ${PRM==v?'on':''}" onclick="setPRm('m','${v}')">${l}</span>`;
  $('prctl').innerHTML='нейтраль считать: '+bt('eq','доллар к доллару (1 : 1)')+bt('beta','по бете (1 : '+d.var.beta.k+')')
   +' &nbsp; коридор нейтрали, % от суммы ног: <input id="prbin" type="number" min="0" step="1" value="'+PRB+'" style="width:56px" onchange="setPRm(\'b\',this.value)"> <span style="color:#8b949e">= ±$'+thr.toFixed(0)+' (хранится в этом браузере)</span>';}
 const dir=V.net>0?'ставка на РОСТ индекса':'ставка на ПАДЕНИЕ индекса',eat=V.per1?Math.abs(A.room/V.per1):null;
 const fa=V.fix_a,fb=V.fix_b,fixTxt=(fa>0?'купить ':'продать ')+Math.abs(fa).toFixed(2)+' SOXL или '+(fb>0?'купить ':'продать ')+Math.abs(fb).toFixed(2)+' SOXS';
 $('prwarn').innerHTML=(inBand?'<span class="pos"><b>В коридоре нейтрали</b>: ставка '+u(V.net)+' при пороге ±$'+thr.toFixed(0)+', '+m(V.per1)+' на +1% индекса.</span>'
   :'<span class="warn"><b>Вне коридора — '+dir+'</b>: '+m(V.per1)+' на каждый +1% индекса'+(eat!=null?' · весь запас над MM ($'+(+A.room).toFixed(0)+') съедает ход индекса на '+(V.net>0?'−':'+')+eat.toFixed(1)+'%':'')+'. До нейтрали: <b>'+fixTxt+'</b>.</span>')
  +' <span style="color:#8b949e">· обновлено '+d.t+'</span>';
 const side=x=>x.size>0?'лонг':x.size<0?'шорт':'нет';
 const th=d.theta||{}, td=d.true_delta||{}, c1=d.c1||{}, c2=d.c2||{};
 $('prtop').innerHTML=tile('чистая ставка пары',u(V.net)+'<div style="font-size:11px;color:#8b949e;font-weight:400">в индексе '+u(V.net_idx)+'</div>',inBand?'pos':'warn')
  +tile('на +1% индекса',m(V.per1),cl(V.per1))
  +tile('Θ $/день пары',sg(th.tot)+'$<div style="font-size:10px;color:#8b949e">опц '+sg(Math.round(((th.puts||0)+(th.calls||0))*100)/100)+' · фанд '+sg(Math.round(((th.soxl_perp||0)+(th.soxs_perp||0))*100)/100)+'</div>',th.tot>0?'pos':'neg')
  +tile('Δ SOXL со спотом',td.soxl_d+' шт<div style="font-size:10px;color:#8b949e">$'+td.soxl_dusd+'</div>')
  +tile('Δ SOXS со спотом',td.soxs_d+' шт<div style="font-size:10px;color:#8b949e">$'+td.soxs_dusd+'</div>')
  +tile('захеджировано / всего','$'+(+V.hedged).toFixed(0)+' / $'+(+d.gross).toFixed(0))
  +tile('до нейтрали',inBand&&Math.abs(V.net)<1?'не нужно':(fa>0?'купить ':'продать ')+Math.abs(fa).toFixed(2)+' SOXL<div style="font-size:11px;color:#8b949e;font-weight:400">или '+(fb>0?'купить ':'продать ')+Math.abs(fb).toFixed(2)+' SOXS</div>')
  +tile('итог пары сегодня',m(d.tot)+'<div style="font-size:11px;color:#8b949e;font-weight:400">реализ. '+m(d.real)+' · остаток '+m(d.upl)+'</div>',cl(d.tot))
  +tile('счёт: запас · свободно',A.buf+'× · $'+(+A.av).toFixed(0),A.buf<1.5?'neg':A.buf<2?'warn':'pos');
 const leg=(x,n)=>`<tr><td style="text-align:left"><b>${n}</b></td><td class="${x.size>0?'pos':x.size<0?'neg':''}">${side(x)} ${Math.abs(x.size)}</td><td>${u(x.usd)}</td><td>${x.avg??'—'} → ${x.px}</td><td class="${cl(x.upl)}">${m(x.upl)}</td><td class="${cl(x.real)}">${m(x.real)}</td><td>${x.n_ex}</td><td>${x.o_buy_q} ($${(+x.o_buy_usd).toFixed(0)})</td><td>${x.o_sell_q} ($${(+x.o_sell_usd).toFixed(0)})</td><td>${x.chg24>0?'+':''}${x.chg24}%</td><td>${x.fund}%</td><td>${x.maker==null?'—':(x.maker*100).toFixed(3)+' / '+(x.taker*100).toFixed(4)+'%'}</td></tr>`;
 $('prlegs').innerHTML='<tr><th>нога</th><th>позиция</th><th>$</th><th>ср. → марк</th><th>нереализ.</th><th>реализ. сегодня</th><th>исполн.</th><th>заявки купить</th><th>заявки продать</th><th>24ч</th><th>фандинг</th><th>мейкер / тейкер</th></tr>'
  +leg(a,'SOXL (3× бык)')+leg(b,'SOXS (3× медведь)')
  +`<tr><td style="text-align:left"><b>пара</b></td><td colspan="2">ставка ${u(V.net)}</td><td>SOXL/SOXS = ${d.ratio}</td><td class="${cl(d.upl)}">${m(d.upl)}</td><td class="${cl(d.real)}">${m(d.real)}</td><td colspan="6"></td></tr>`;
 $('prscen').innerHTML='<tr><th>индекс</th><th>SOXL</th><th>SOXS</th><th>итог</th></tr>'+V.scen.map(r=>`<tr><td>${r.mv>0?'+':''}${r.mv}%</td><td class="${cl(r.a)}">${m(r.a)}</td><td class="${cl(r.b)}">${m(r.b)}</td><td class="${cl(r.pnl)}"><b>${m(r.pnl)}</b>${Math.abs(r.pnl)>A.room&&r.pnl<0?' <span class="neg">&gt; запаса</span>':''}</td></tr>`).join('');
 const w=(x,l)=>x?tile('лонг обеих поровну, '+l,(x.both>0?'+':'')+x.both+'%<div style="font-size:11px;color:#8b949e;font-weight:400">SOXL '+(x.a>0?'+':'')+x.a+'% · SOXS '+(x.b>0?'+':'')+x.b+'%</div>',cl(x.both)):'';
 $('prmkt').innerHTML=tile('бета SOXS к SOXL (час, 8 дн.)',H.beta==null?'—':H.beta+'<div style="font-size:11px;color:#8b949e;font-weight:400">корреляция '+H.corr+' · '+H.n_h+' ч</div>')
  +tile('ход SOXL за час (σ)',H.vol_h==null?'—':'±'+H.vol_h+'%<div style="font-size:11px;color:#8b949e;font-weight:400">индекс ≈ ±'+(H.vol_h/d.lev).toFixed(2)+'%</div>')
  +w(H.w24,'24 ч')+w(H.w7,'7 дн')+w(H.w30,'30 дн')+tile('оборот 24ч','SOXL $'+(a.turn/1e6).toFixed(0)+'M · SOXS $'+(b.turn/1e6).toFixed(0)+'M');

 // Конструкция 1: SOXL spot + put long + SOXL perp + SOXS perp
 let h1='<h3>Конструкция 1: SOXL spot + SOXL put long + SOXL perp + SOXS perp (Синтетический защитный колл + дельта-хедж)</h3>';
 h1+='<div class="row">'
  +tile('Net Δ$',sg(c1.net_dusd)+'$',Math.abs(c1.net_dusd)<150?'pos':'warn')
  +tile('На +1% индекса',m(c1.per1),cl(c1.per1))
  +tile('Тета $/день',sg(c1.theta)+'$',c1.theta>0?'pos':'neg')
  +tile('Путов SOXL',c1.puts_q+' шт<div style="font-size:10px;color:#8b949e">$'+c1.puts_usd+' · Δ '+c1.puts_d+' · Θ '+m(c1.puts_th)+'</div>')
  +tile('SOXL perp',(a.size>0?'лонг ':'шорт ')+Math.abs(a.size)+'<div style="font-size:10px;color:#8b949e">$'+a.usd+' · фанд '+m(th.soxl_perp)+'/д</div>')
  +tile('SOXS perp',(b.size>0?'лонг ':'шорт ')+Math.abs(b.size)+'<div style="font-size:10px;color:#8b949e">$'+b.usd+' · фанд '+m(th.soxs_perp)+'/д</div>')
  +tile('Защитный пол (Floor)','<span class="pos">РИСК ОГРАНИЧЕН</span><div style="font-size:10px;color:#8b949e">путы + SOXS perp кроют обвал</div>')
  +'</div>';
 if(d.puts&&d.puts.length){
  h1+='<table style="margin-top:4px"><tr><th>пут</th><th>кол-во</th><th>страйк</th><th>экспирация</th><th>марк</th><th>стоимость</th><th>дельта</th><th>гамма</th><th>тета /д</th><th>IV</th><th>uPnL</th></tr>';
  d.puts.forEach(p=>{h1+=`<tr><td><b>${p.sym}</b></td><td>${p.sz}</td><td>${p.K}</td><td>${p.exp}</td><td>${p.mark}</td><td>$${p.usd}</td><td>${p.d}</td><td>${p.g}</td><td class="neg">${p.th}</td><td>${p.iv}%</td><td class="${cl(p.upl)}">${m(p.upl)}</td></tr>`;});
  h1+='</table>';
 }
 if(c1.scen&&c1.scen.length){
  h1+='<div style="margin-top:4px;font-size:11px;color:#8b949e">Сценарии хода индекса полупроводников (T+0):</div>';
  h1+='<table style="margin-top:2px"><tr><th>ход индекса</th><th>итог P&L</th><th>SOXL путы</th><th>SOXL перп</th><th>SOXS перп</th></tr>';
  c1.scen.forEach(s=>{h1+=`<tr><td>${s.mv>0?'+':''}${s.mv}%</td><td class="${cl(s.pnl)}"><b>${m(s.pnl)}</b></td><td class="${cl(s.puts)}">${m(s.puts)}</td><td class="${cl(s.perp_l)}">${m(s.perp_l)}</td><td class="${cl(s.perp_s)}">${m(s.perp_s)}</td></tr>`;});
  h1+='</table>';
 }
 $('prc1_box').innerHTML=h1;

 // Конструкция 2: SOXL call long против SOXS perp
 let h2='<h3>Конструкция 2: SOXL call long против SOXS perp (Синтетический стрэддл / Long Volatility)</h3>';
 h2+='<div class="row">'
  +tile('Net Δ к индексу',m(c2.per1)+' на 1%',cl(c2.per1))
  +tile('Тета $/день',sg(c2.theta)+'$',c2.theta>0?'pos':'neg')
  +tile('Коллов SOXL',c2.calls_q+' шт<div style="font-size:10px;color:#8b949e">$'+c2.calls_usd+' · Δ '+c2.calls_d+' · Θ '+m(c2.calls_th)+'</div>')
  +tile('SOXS perp',(b.size>0?'лонг ':'шорт ')+Math.abs(b.size)+'<div style="font-size:10px;color:#8b949e">$'+b.usd+' · фанд '+m(th.soxs_perp)+'/д</div>')
  +tile('Профиль','<span class="pos">ДЛИННАЯ ВОЛАТИЛЬНОСТЬ</span><div style="font-size:10px;color:#8b949e">выигрыш при любом резком выстреле</div>')
  +'</div>';
 if(d.calls&&d.calls.length){
  h2+='<table style="margin-top:4px"><tr><th>колл</th><th>кол-во</th><th>страйк</th><th>экспирация</th><th>марк</th><th>стоимость</th><th>дельта</th><th>гамма</th><th>тета /д</th><th>IV</th><th>uPnL</th></tr>';
  d.calls.forEach(c=>{h2+=`<tr><td><b>${c.sym}</b></td><td>${c.sz}</td><td>${c.K}</td><td>${c.exp}</td><td>${c.mark}</td><td>$${c.usd}</td><td>${c.d}</td><td>${c.g}</td><td class="neg">${c.th}</td><td>${c.iv}%</td><td class="${cl(c.upl)}">${m(c.upl)}</td></tr>`;});
  h2+='</table>';
 }
 if(c2.scen&&c2.scen.length){
  h2+='<div style="margin-top:4px;font-size:11px;color:#8b949e">Сценарии хода индекса полупроводников (T+0):</div>';
  h2+='<table style="margin-top:2px"><tr><th>ход индекса</th><th>итог P&L</th><th>SOXL коллы</th><th>SOXS перп</th></tr>';
  c2.scen.forEach(s=>{h2+=`<tr><td>${s.mv>0?'+':''}${s.mv}%</td><td class="${cl(s.pnl)}"><b>${m(s.pnl)}</b></td><td class="${cl(s.calls)}">${m(s.calls)}</td><td class="${cl(s.perp_s)}">${m(s.perp_s)}</td></tr>`;});
  h2+='</table>';
 }
 $('prc2_box').innerHTML=h2;
 if(TAB=='S'&&!PR_TVW)prTvLoad();
 if(TAB=='S')$('upd').textContent='спред · '+d.t;}
// ---- TradingView-виджет на вкладке «Спред SOXL/SOXS» ----
let PR_TV='NASDAQ:SOXL-NASDAQ:SOXM', PR_TVI='15', PR_TVW=null;
try{PR_TV=localStorage.getItem('pr_tv')||PR_TV;PR_TVI=localStorage.getItem('pr_tvi')||PR_TVI;}catch(e){}
function prTvLoad(){if(window.TradingView){prTvDraw();return}
 if(document.getElementById('tvjs')){let w=setInterval(()=>{if(window.TradingView){clearInterval(w);prTvDraw();}},200);return;}
 const sc=document.createElement('script');sc.id='tvjs';sc.src='https://s3.tradingview.com/tv.js';sc.onload=prTvDraw;document.head.appendChild(sc);}
function prTvSet(s){PR_TV=s;try{localStorage.setItem('pr_tv',PR_TV)}catch(e){} prTvDraw();}
function prTvInt(iv){PR_TVI=iv;try{localStorage.setItem('pr_tvi',PR_TVI)}catch(e){} prTvDraw();}
function prTvDraw(){if(!window.TradingView||TAB!='S')return;const box=$('prtv_box');if(!box)return;box.innerHTML='';
 const iv=[['1','1м'],['5','5м'],['15','15м'],['60','1ч'],['240','4ч'],['D','Д']];
 const bt=(s,l)=>`<span class="gbtn ${PR_TV==s?'on':''}" onclick="prTvSet('${s}')">${l}</span>`;
 $('prtv_ctl').innerHTML='<b>TradingView</b> график: '+bt('NASDAQ:SOXL-NASDAQ:SOXM','SOXL − SOXM (спред)')+bt('NASDAQ:SOXL/NASDAQ:SOXM','SOXL / SOXM (отношение)')
  +bt('NASDAQ:SOXL','SOXL (две линии: + SOXM)')+bt('BYBIT:SOXLUSDT.P/BYBIT:SOXSUSDT.P','SOXL / SOXS (Bybit)')+bt('NASDAQ:SOXX','SOXX (индекс)')
  +' &nbsp; '+iv.map(([v,l])=>`<span class="gbtn ${PR_TVI==v?'on':''}" onclick="prTvInt('${v}')">${l}</span>`).join('')
  +' &nbsp; <input id="prtvin" style="width:240px" value="'+PR_TV+'" onkeydown="if(event.key==\'Enter\')prTvSet(this.value)"> <span class="gbtn" onclick="prTvSet($(\'prtvin\').value)">показать</span>';
 const st=(PR_TV=='NASDAQ:SOXL')?[{id:"Overlay@tv-basicstudies",inputs:{symbol:"NASDAQ:SOXM"}}]:['BB@tv-basicstudies'];
 PR_TVW=new TradingView.widget({container_id:'prtv_box',symbol:PR_TV,interval:PR_TVI,autosize:true,theme:'dark',style:'1',locale:'ru',timezone:'Etc/UTC',hide_side_toolbar:false,allow_symbol_change:true,withdateranges:true,studies:st});}
// ===================== Лаборатория пары: волатильность и симулятор сетки (вкладка S) =====================
function setLB(k,v){v=+v;if(isNaN(v)||v<=0)return;if(k=='s')LBS=v;if(k=='l')LBL=v;if(k=='w')LBW=v;try{localStorage.setItem('lbs',LBS);localStorage.setItem('lbl',LBL);localStorage.setItem('lbw',LBW)}catch(e){} lbtick();}
async function lbtick(){ if(TAB!='S')return; let d;
 try{d=await (await fetch('/pairlab?step='+LBS+'&lot='+LBL+'&win='+LBW)).json()}catch(e){return}
 if(d.err){$('lbmsg').innerHTML='<span class="neg">ошибка: '+d.err+'</span>';return} LBD=d; renderLB(d);}
function renderLB(d){const m=v=>v==null?'—':(v>0?'+':'')+(+v).toFixed(2)+'$',p=v=>v==null?'—':(v>0?'+':'')+v+'%';
 if(!['lbsin','lblin'].includes(document.activeElement.id)){const b=(v,l)=>`<span class="gbtn ${LBW==v?'on':''}" onclick="setLB('w',${v})">${l}</span>`;
  $('lbctl').innerHTML='шаг сетки, %: <input id="lbsin" type="number" min="0.01" step="0.01" value="'+d.step+'" style="width:64px" onchange="setLB(\'s\',this.value)">'
   +' <span style="color:#8b949e">(≈ '+(d.px?(d.px.a*d.step/100).toFixed(3):'—')+' по цене SOXL)</span> &nbsp; лот, $: <input id="lblin" type="number" min="1" step="1" value="'+d.lot+'" style="width:64px" onchange="setLB(\'l\',this.value)">'
   +' &nbsp; окно: '+b(60,'1 ч')+b(360,'6 ч')+b(720,'12 ч')+' &nbsp; <span style="color:#8b949e">посчитано '+d.t+'</span>';}
 const W={'60':'1 ч','360':'6 ч','720':'12 ч'};let h='<tr><th>окно</th><th>что</th><th>σ 1 мин</th><th>итог за окно</th><th>размах</th><th>путь</th><th>путь / смещение</th><th>пересечений средней</th><th>лонг обеих поровну</th></tr>';
 Object.keys(W).forEach(k=>{const v=d.vol[k];if(!v)return;[['SOXL',v.a],['SOXS',v.b],['отношение SOXL/SOXS',v.ratio]].forEach(([n,x],i)=>{if(!x)return;
  h+=`<tr>${i==0?'<td rowspan="3"><b>'+W[k]+'</b></td>':''}<td style="text-align:left">${n}</td><td>${x.sd}%</td><td class="${cl(x.ret)}">${p(x.ret)}</td><td>${x.rng}%</td><td>${x.path}%</td><td class="${x.chop>=10?'pos':''}">${x.chop}×</td><td>${x.cross}</td>${i==0?'<td rowspan="3" class="'+cl(v.both)+'">'+p(v.both)+'</td>':''}</tr>`})});
 $('lbvol').innerHTML=h;
 const v6=d.vol['360'],v1=d.vol['60'];
 $('lbtop').innerHTML=tile('бета SOXS к SOXL (5 мин, 12 ч)',d.beta5==null?'—':d.beta5+'<div style="font-size:11px;color:#8b949e;font-weight:400">корреляция '+d.corr5+'</div>')
  +(v1&&v1.a?tile('SOXL: σ 1 мин · размах за час',v1.a.sd+'% · '+v1.a.rng+'%'):'')+(v6&&v6.a?tile('«ёрш» за 6 ч (путь / смещение)','SOXL '+v6.a.chop+'× · SOXS '+v6.b.chop+'×',v6.a.chop>=10?'pos':''):'')
  +(v6?tile('нейтральная пара за 6 ч','лонг обеих '+p(v6.both)+'<div style="font-size:11px;color:#8b949e;font-weight:400">шорт обеих '+p(-v6.both)+'</div>'):'');
 const S=d.sim;if(!S){$('lbsim').innerHTML='';return}
 const row=(n,x,per1,note)=>`<tr><td style="text-align:left"><b>${n}</b>${note?'<div style="font-size:11px;color:#8b949e;font-weight:400">'+note+'</div>':''}</td><td>${x.fills??'—'}</td><td>${x.cycles??'—'}</td><td class="${cl(x.real)}">${m(x.real)}</td><td>${x.per_h==null?'—':m(x.per_h)}</td><td class="${x.inv_usd>0?'pos':x.inv_usd<0?'neg':''}">${x.inv_usd==null?'—':(x.inv_usd<0?'−':'')+'$'+Math.abs(x.inv_usd).toFixed(0)}</td><td class="${cl(x.unreal)}">${m(x.unreal)}</td><td class="${cl(x.net)}"><b>${m(x.net)}</b></td><td>${x.max_usd==null?'—':'$'+(+x.max_usd).toFixed(0)}</td><td class="${per1>20?'warn':''}">±${(+per1).toFixed(1)}$</td></tr>`;
 const A=S.a,B=S.b,Bo=S.both,O=S.one;
 $('lbsim').innerHTML='<tr><th>вариант ('+S.hours+' ч, шаг '+d.step+'%, лот $'+d.lot+')</th><th>исполнений</th><th>закрытых циклов</th><th>реализовано</th><th>$/час</th><th>остаток в конце</th><th>нереализ.</th><th>итог</th><th>макс. остаток</th><th>макс. ставка на 1% индекса</th></tr>'
  +row('сетка только на SOXL',A,A.max_usd*3/100)+row('сетка только на SOXS',B,B.max_usd*3/100)
  +row('сетки на обеих ногах',{fills:A.fills+B.fills,cycles:A.cycles+B.cycles,real:Bo.real,per_h:+(A.per_h+B.per_h).toFixed(2),inv_usd:Bo.idx_now,unreal:Bo.unreal,net:Bo.net,max_usd:Bo.idx_max},Bo.per1_max,'остаток и макс. — в долларах индексной ставки: $SOXL − $SOXS')
  +row('сетка на SOXL + хедж остатка ногой SOXS',{fills:A.fills,cycles:A.cycles,real:A.real,per_h:A.per_h,inv_usd:null,unreal:+(A.unreal+O.hedge_pnl-O.fee).toFixed(2),net:O.net,max_usd:O.resid_max},O.per1_resid,'хедж-сделок '+O.trades+', результат хеджа '+m(O.hedge_pnl)+', комиссии '+m(-O.fee));
 const best=[['только SOXL',A.net],['только SOXS',B.net],['обе ноги',Bo.net],['SOXL + хедж',O.net]].sort((x,y)=>y[1]-x[1])[0];
 $('lbmsg').innerHTML='За это окно лучший итог: <b>'+best[0]+' '+m(best[1])+'</b>. '
  +(Bo.idx_max>Math.max(A.max_usd,B.max_usd)*1.3?'<span class="warn">Сетки на обеих ногах набирали остаток в одну сторону по индексу: худшая ставка $'+Bo.idx_max.toFixed(0)+' (±'+Bo.per1_max.toFixed(0)+'$ на 1% индекса) — ноги друг друга не хеджируют, а складываются.</span> ':'')
  +'<span style="color:#8b949e">Хедж остатка второй ногой срезает убыток на тренде, но забирает и доход сетки на возврате цены: доход сетки — это и есть ставка на возврат.</span>';}
// ===================== Графики пары SOXL / SOXS (вкладка R) =====================
let PCB='10s', PCW=360, PCM='px', PCD=null, PCT=[], PCH=null, PCY=null, DKSOX=null, DKPAIR=null;
try{PCB=localStorage.getItem('pcb')||PCB;PCW=+(localStorage.getItem('pcw')||PCW);PCM=localStorage.getItem('pcm')||PCM;}catch(e){}
try{if(!localStorage.getItem('pc_tv1')){PCM='tv';PCB='1m';PCW=1000;localStorage.setItem('pc_tv1','1');localStorage.setItem('pcb',PCB);localStorage.setItem('pcw',PCW);localStorage.setItem('pcm',PCM);}}catch(e){}
function setPC(k,v){if(k=='b')PCB=v;if(k=='w')PCW=+v;if(k=='m')PCM=v;try{localStorage.setItem('pcb',PCB);localStorage.setItem('pcw',PCW);localStorage.setItem('pcm',PCM)}catch(e){} if(k=='b'){PCT=[];pctick()}else if(PCD)renderPC(PCD);}
function pcctl(){const b=(k,v,l,cur)=>`<span class="gbtn ${cur==v?'on':''}" onclick="setPC('${k}','${v}')">${l}</span>`;
 return 'бар: '+b('b','10s','10с',PCB)+b('b','30s','30с',PCB)+b('b','1m','1м',PCB)+b('b','5m','5м',PCB)+b('b','1h','1ч',PCB)
  +' &nbsp; баров в окне: '+b('w',120,'120',PCW)+b('w',360,'360',PCW)+b('w',720,'720',PCW)+b('w',1000,'1000',PCW)
  +' &nbsp; вид: '+b('m','tv','как в TradingView',PCM)+b('m','px','цены + отношение + P&L',PCM)+b('m','pct','то же, % от начала окна',PCM);}
async function pctick(){ if(TAB!='S'&&TAB!='D')return; if(TAB=='D'&&!DKCHARTS)return; const bar=PCB; let d;
 try{d=await (await fetch('/pairchart?bar='+bar)).json()}catch(e){return}
 if(bar!=PCB)return; if(d.err&&!d.rows){$(TAB=='D'?'dkpcctl':'pcctl').innerHTML=pcctl()+' <span class="neg">ошибка: '+d.err+'</span>';return} PCD=d; renderPC(d);}
function renderPC(d){PCT=(d.rows||[]).slice(-PCW);
 $(TAB=='D'?'dkpcctl':'pcctl').innerHTML=pcctl()+' &nbsp; <span style="color:#8b949e">баров '+PCT.length+' из '+d.n+' · '+d.t+(d.err?' · '+d.err:'')+'</span>'; drawPC();}
// ---- график пары «как в TradingView»: SOXL и SOXS линиями на своих шкалах, панель SOXL − SOXS, мои сделки/позиции/заявки ----
function drawPCtv(c){const x=c.getContext('2d'),W=c.width,H=c.height,T=PCT,n=T.length;window.PCPR=150;
 x.fillStyle='#131722';x.fillRect(0,0,W,H);x.font='12px -apple-system,Segoe UI,sans-serif';x.textAlign='left';x.setLineDash([]);x.lineWidth=1;
 if(n<2){x.fillStyle='#787b86';x.fillText(PCB.endsWith('s')?'секундные сделки копятся с запуска сервера… минутные бары есть сразу — кнопка «1м»':'нет данных',14,24);return}
 const P=(TAB=='D'&&DKPAIR)?DKPAIR:(PRDATA||{}),sa=P.a?P.a.size:0,sb=P.b?P.b.size:0,ava=sa?P.a.avg:null,avb=sb?P.b.avg:null;
 const pl=6,pr=150,c1=W-pr,c2=W-75,pb=26,top=8,hh=H-pb-top,s1=top+Math.round(hh*.7),dt=s1+12,db=H-pb-4;
 const X=i=>pl+(c1-pl)*(i+.5)/n,bw=Math.max(1,(c1-pl)/n*.6);
 const OR=(TAB=='D'&&DKSOX)?(DKSOX.orders||[]):[],FL=(TAB=='D'&&DKSOX)?(DKSOX.fills||[]):[];
 const rng=(arr,extra)=>{let lo=Math.min(...arr),hi=Math.max(...arr);const sp=(hi-lo)||1;(extra||[]).forEach(v=>{if(v!=null&&v>lo-sp*.5&&v<hi+sp*.5){lo=Math.min(lo,v);hi=Math.max(hi,v)}});const m=(hi-lo)*.06||.02;return [lo-m,hi+m]};
 const A=T.map(r=>r[1]),B=T.map(r=>r[2]),Dd=T.map(r=>r[1]-r[2]);
 const [alo,ahi]=rng(A,[ava,...OR.filter(o=>o[0]=='L').map(o=>o[2])]),[blo,bhi]=rng(B,[avb,...OR.filter(o=>o[0]!='L').map(o=>o[2])]);
 const YA=v=>top+(s1-top)*(1-(v-alo)/(ahi-alo)),YB=v=>top+(s1-top)*(1-(v-blo)/(bhi-blo));
 let dlo=Math.min(...Dd,0),dhi=Math.max(...Dd,0);const dm=(dhi-dlo)*.1||.05;dlo-=dm;dhi+=dm;const YD=v=>dt+(db-dt)*(1-(v-dlo)/(dhi-dlo));
 const nice=(a,b,k)=>{const raw=(b-a)/k,p=Math.pow(10,Math.floor(Math.log10(raw))),m=[1,2,2.5,5,10].find(q=>q*p>=raw)*p;const o=[];for(let v=Math.ceil(a/m)*m;v<=b;v+=m)o.push(+v.toFixed(10));return o};
 nice(alo,ahi,8).forEach(v=>{const y=YA(v);x.strokeStyle='#1e222d';x.beginPath();x.moveTo(pl,y);x.lineTo(c1,y);x.stroke();x.fillStyle='#8fd3f4';x.fillText(v.toFixed(2),c2+6,y+4)});
 nice(blo,bhi,8).forEach(v=>{x.fillStyle='#f7a1a8';x.fillText(v.toFixed(2),c1+6,YB(v)+4)});
 nice(dlo,dhi,4).forEach(v=>{const y=YD(v);x.strokeStyle='#1e222d';x.beginPath();x.moveTo(pl,y);x.lineTo(c1,y);x.stroke();x.fillStyle='#b2b5be';x.fillText(v.toFixed(2),c2+6,y+4)});
 const sec=PCD?PCD.sec:60,tf=t=>{const Q=new Date(t*1000);return sec>=3600?('0'+Q.getDate()).slice(-2)+'.'+('0'+(Q.getMonth()+1)).slice(-2)+' '+Q.toTimeString().slice(0,5):Q.toTimeString().slice(0,sec<60?8:5)};
 const tstep=Math.max(1,Math.round(n/10));x.textAlign='center';
 for(let i=0;i<n;i+=tstep){const xx=X(i);x.strokeStyle='#1e222d';x.beginPath();x.moveTo(xx,top);x.lineTo(xx,db);x.stroke();x.fillStyle='#b2b5be';x.fillText(tf(T[i][0]),xx,H-8)}
 x.textAlign='left';x.strokeStyle='#2a2e39';x.beginPath();x.moveTo(pl,s1+6);x.lineTo(W,s1+6);x.stroke();[c1,c2].forEach(xx=>{x.beginPath();x.moveTo(xx,0);x.lineTo(xx,H-pb);x.stroke()});
 const vmax=Math.max(...T.map(r=>r[3]||0),1),vh=(s1-top)*.12;
 T.forEach((r,i)=>{x.fillStyle=(i&&r[1]<T[i-1][1])?'rgba(239,83,80,.35)':'rgba(38,166,154,.35)';const h=(r[3]||0)/vmax*vh;x.fillRect(X(i)-bw/2,s1-h,bw,h)});
 const line=(arr,Yf,col,w)=>{x.strokeStyle=col;x.lineWidth=w;x.beginPath();arr.forEach((v,i)=>{const y=Yf(v);i?x.lineTo(X(i),y):x.moveTo(X(i),y)});x.stroke();x.lineWidth=1};
 line(B,YB,'#f23645',1.5);line(A,YA,'#5ec8f2',1.5);
 x.strokeStyle='#787b86';x.setLineDash([2,3]);x.beginPath();x.moveTo(pl,YD(0));x.lineTo(c1,YD(0));x.stroke();x.setLineDash([]);
 line(Dd,YD,'#22c55e',1.5);x.fillStyle='#8b949e';x.fillText('SOXLUSDT.P − SOXSUSDT.P',pl+8,dt+14);
 const Ln=T[n-1];
 const posLine=(avg,sz,last,Yf,col,nm)=>{if(avg==null||!sz)return;const y=Yf(avg);x.strokeStyle=col;x.beginPath();x.moveTo(pl,y);x.lineTo(c1,y);x.stroke();
  const up=sz*(last-avg),t=nm+' '+sz+' @'+avg+'   '+(up>=0?'+':'')+up.toFixed(2)+' USD',w=x.measureText(t).width+12;x.fillStyle=col;x.fillRect(c1-w-90,y-9,w,18);x.fillStyle='#fff';x.fillText(t,c1-w-84,y+4);};
 posLine(ava,sa,Ln[1],YA,'#2962ff','SOXL');posLine(avb,sb,Ln[2],YB,'#c2185b','SOXS');
 x.font='10px sans-serif';
 OR.forEach(o=>{const Yf=o[0]=='L'?YA:YB,y=Yf(o[2]);if(y<top||y>s1)return;const col=o[1]=='B'?'rgba(38,166,154,.9)':'rgba(239,83,80,.9)';x.strokeStyle=col;x.setLineDash([2,3]);x.beginPath();x.moveTo(pl,y);x.lineTo(c1,y);x.stroke();x.setLineDash([]);x.fillStyle=col;x.fillText((o[0]=='L'?'SOXL ':'SOXS ')+(o[1]=='B'?'покупка ':'продажа ')+o[3]+' @'+o[2],c1-260,y-3);});
 const t0=T[0][0],tN=T[n-1][0]+sec;
 FL.forEach(f=>{if(f[0]<t0||f[0]>tN)return;let a1=0,b1=n-1;while(a1<b1){const md=(a1+b1+1)>>1;if(T[md][0]<=f[0])a1=md;else b1=md-1}
  const Yf=f[1]=='L'?YA:YB,xx=X(a1),y=Yf(f[3]),up=f[2]=='B',z=4;if(y<top||y>s1)return;
  x.fillStyle=up?'#2962ff':'#f23645';x.beginPath();if(up){x.moveTo(xx,y+2);x.lineTo(xx-z,y+2+z*1.7);x.lineTo(xx+z,y+2+z*1.7)}else{x.moveTo(xx,y-2);x.lineTo(xx-z,y-2-z*1.7);x.lineTo(xx+z,y-2-z*1.7)}x.closePath();x.fill();});
 x.font='12px -apple-system,Segoe UI,sans-serif';
 const tag=(xx,y,txt,bg)=>{x.fillStyle=bg;x.fillRect(xx,y-9,73,18);x.fillStyle='#fff';x.fillText(txt,xx+4,y+4)};
 tag(c2+1,YA(Ln[1]),Ln[1].toFixed(2),'#0288d1');tag(c1+1,YB(Ln[2]),Ln[2].toFixed(2),'#f23645');tag(c2+1,YD(Dd[n-1]),(Dd[n-1]>0?'+':'')+Dd[n-1].toFixed(2),'#16a34a');
 x.font='13px -apple-system,Segoe UI,sans-serif';let lx=pl+8;
 [['SOXLUSDT.P '+Ln[1].toFixed(2),'#5ec8f2'],['SOXSUSDT.P '+Ln[2].toFixed(2),'#f23645'],['SOXL − SOXS '+(Dd[n-1]>0?'+':'')+Dd[n-1].toFixed(2),'#22c55e'],['▲ покупка ▼ продажа · пунктир — заявки · сплошная — средняя позиции','#8b949e']].forEach(([t,col])=>{x.fillStyle=col;x.fillText(t,lx,top+16);lx+=x.measureText(t).width+18});
 if(PCH!=null&&PCH<n){const xx=X(PCH);x.strokeStyle='#6e7681';x.setLineDash([4,4]);x.beginPath();x.moveTo(xx,top);x.lineTo(xx,db);x.stroke();if(PCY!=null&&PCY<db){x.beginPath();x.moveTo(pl,PCY);x.lineTo(c1,PCY);x.stroke()}x.setLineDash([]);
  const r=T[PCH],tt=tf(r[0])+'   SOXL '+r[1].toFixed(2)+'   SOXS '+r[2].toFixed(2)+'   разница '+(r[1]-r[2]).toFixed(2),w=x.measureText(tt).width+12,bx=Math.min(xx+10,c1-w);
  x.fillStyle='rgba(22,27,34,.92)';x.fillRect(bx,top+24,w,20);x.fillStyle='#e6edf3';x.fillText(tt,bx+6,top+38);}
}
function drawPC(){const c=$(TAB=='D'?'dkpc':'prc');if(!c)return;if(PCM=='tv'){drawPCtv(c);return}window.PCPR=84;const x=c.getContext('2d'),W=c.width,H=c.height,T=PCT;
 x.fillStyle='#131722';x.fillRect(0,0,W,H);x.font='12px -apple-system,Segoe UI,sans-serif';x.textAlign='left';x.setLineDash([]);x.lineWidth=1;
 if(T.length<2){x.fillStyle='#787b86';x.fillText(PCB.endsWith('s')?'секундные сделки копятся с запуска сервера… минутные бары есть сразу — кнопка «1м»':'нет данных',14,24);return}
 const P=(TAB=='D'&&DKPAIR)?DKPAIR:(PRDATA||{}),sa=P.a?P.a.size:0,sb=P.b?P.b.size:0,ava=P.a&&P.a.size?P.a.avg:null,avb=P.b&&P.b.size?P.b.avg:null;
 const pr=84,pl=6,pb=26,top=8,hh=H-pb-top,s1=top+Math.round(hh*.55),s2=top+Math.round(hh*.78),p2t=s1+8,p2b=s2-2,p3t=s2+8,p3b=H-pb-4;
 const n=T.length,X=i=>pl+(W-pl-pr)*(i+.5)/n,bw=Math.max(1,(W-pl-pr)/n*.6),a0=T[0][1],b0=T[0][2],pct=PCM=='pct';
 const va=r=>pct?(r[1]/a0-1)*100:r[1],vb=r=>pct?(r[2]/b0-1)*100:r[2];
 const la=ava!=null?(pct?(ava/a0-1)*100:ava):null,lb=avb!=null?(pct?(avb/b0-1)*100:avb):null;
 const vals=T.flatMap(r=>[va(r),vb(r)]);let lo=Math.min(...vals),hi=Math.max(...vals);const sp=(hi-lo)||1;
 [la,lb].forEach(v=>{if(v!=null&&v>lo-sp*.6&&v<hi+sp*.6){lo=Math.min(lo,v);hi=Math.max(hi,v)}});
 const pm=(hi-lo)*.08||0.02;lo-=pm;hi+=pm;const Y=p=>top+(s1-top)*(1-(p-lo)/(hi-lo));
 const rat=T.map(r=>((r[1]/r[2])/(a0/b0)-1)*100),both=T.map(r=>((r[1]/a0-1)+(r[2]/b0-1))/2*100);
 let rlo=Math.min(...rat,...both,0),rhi=Math.max(...rat,...both,0);const rm=(rhi-rlo)*.1||0.02;rlo-=rm;rhi+=rm;const R=v=>p2t+(p2b-p2t)*(1-(v-rlo)/(rhi-rlo));
 const pnl=T.map(r=>sa*(r[1]-a0)+sb*(r[2]-b0)),hasPos=!!(sa||sb);
 let qlo=Math.min(...pnl,0),qhi=Math.max(...pnl,0);const qm=(qhi-qlo)*.14||1;qlo-=qm;qhi+=qm;const Q=v=>p3t+(p3b-p3t)*(1-(v-qlo)/(qhi-qlo));
 const nice=(a,b,k)=>{const raw=(b-a)/k,p=Math.pow(10,Math.floor(Math.log10(raw))),m=[1,2,2.5,5,10].find(q=>q*p>=raw)*p;const o=[];for(let v=Math.ceil(a/m)*m;v<=b;v+=m)o.push(+v.toFixed(10));return o};
 const sgn=v=>(v>0?'+':'')+v,grid=(arr,Yf,fmt)=>arr.forEach(v=>{const y=Yf(v);x.strokeStyle='#1e222d';x.beginPath();x.moveTo(pl,y);x.lineTo(W-pr,y);x.stroke();x.fillStyle='#b2b5be';x.fillText(fmt(v),W-pr+8,y+4)});
 grid(nice(lo,hi,8),Y,v=>pct?sgn(v.toFixed(2))+'%':v.toFixed(2));grid(nice(rlo,rhi,4),R,v=>sgn(v.toFixed(2))+'%');grid(nice(qlo,qhi,3),Q,v=>sgn(v.toFixed(Math.abs(qhi-qlo)<8?1:0))+'$');
 const sec=PCD?PCD.sec:10,tf=t=>{const D=new Date(t*1000);return sec>=3600?('0'+D.getDate()).slice(-2)+'.'+('0'+(D.getMonth()+1)).slice(-2)+' '+D.toTimeString().slice(0,5):D.toTimeString().slice(0,sec<60?8:5)};
 const tstep=Math.max(1,Math.round(n/9));x.textAlign='center';
 for(let i=0;i<n;i+=tstep){const xx=X(i);x.strokeStyle='#1e222d';x.beginPath();x.moveTo(xx,top);x.lineTo(xx,p3b);x.stroke();x.fillStyle='#b2b5be';x.fillText(tf(T[i][0]),xx,H-8)}
 x.textAlign='left';x.strokeStyle='#2a2e39';[s1+3,s2+3].forEach(y=>{x.beginPath();x.moveTo(pl,y);x.lineTo(W,y);x.stroke()});x.beginPath();x.moveTo(W-pr,0);x.lineTo(W-pr,H-pb);x.stroke();
 const vmax=Math.max(...T.map(r=>r[3]||0),1),vh=(s1-top)*.22;
 T.forEach((r,i)=>{const up=i?r[1]>=T[i-1][1]:true;x.fillStyle=up?'rgba(38,166,154,.45)':'rgba(239,83,80,.45)';const h=(r[3]||0)/vmax*vh;x.fillRect(X(i)-bw/2,s1-h,bw,h)});
 const line=(arr,Yf,col,w)=>{x.strokeStyle=col;x.lineWidth=w;x.beginPath();arr.forEach((v,i)=>{const y=Yf(v);i?x.lineTo(X(i),y):x.moveTo(X(i),y)});x.stroke();x.lineWidth=1};
 const tag=(y,txt,bg,fg)=>{x.fillStyle=bg;x.fillRect(W-pr+1,y-9,pr-2,18);x.fillStyle=fg||'#fff';x.fillText(txt,W-pr+5,y+4)};
 [[la,'#2962ff','ср. SOXL'],[lb,'#22c55e','ср. SOXS']].forEach(([v,col,nm])=>{if(v==null||v<lo||v>hi)return;const y=Y(v);x.strokeStyle=col;x.setLineDash([6,4]);x.beginPath();x.moveTo(pl,y);x.lineTo(W-pr,y);x.stroke();x.setLineDash([]);x.fillStyle=col;x.fillText(nm+' '+(pct?sgn(v.toFixed(2))+'%':v.toFixed(2)),pl+8,y-4)});
 line(T.map(vb),Y,'#22c55e',2);line(T.map(va),Y,'#2962ff',2);
 x.strokeStyle='#787b86';x.setLineDash([2,3]);[[R,p2t],[Q,p3t]].forEach(([F])=>{x.beginPath();x.moveTo(pl,F(0));x.lineTo(W-pr,F(0));x.stroke()});x.setLineDash([]);
 line(both,R,'#f0b90b',1.3);line(rat,R,'#f7525f',1.6);
 if(TAB=='D'&&DKSOX){const pv=(p,k)=>pct?(p/(k=='L'?a0:b0)-1)*100:p;x.font='10px sans-serif';
  (DKSOX.orders||[]).forEach(o=>{const v=pv(o[2],o[0]);if(v<lo||v>hi)return;const y=Y(v),col=o[1]=='B'?'rgba(38,166,154,.8)':'rgba(239,83,80,.8)';x.strokeStyle=col;x.setLineDash([2,3]);x.beginPath();x.moveTo(pl,y);x.lineTo(W-pr,y);x.stroke();x.setLineDash([]);x.fillStyle=col;x.fillText((o[0]=='L'?'SOXL ':'SOXS ')+(o[1]=='B'?'buy ':'sell ')+o[3]+' @'+o[2],W-pr-170,y-3);});
  const t0=T[0][0],sec0=PCD?PCD.sec:60,tN=T[n-1][0]+sec0;let nf=0;
  (DKSOX.fills||[]).forEach(f=>{if(f[0]<t0||f[0]>tN)return;let a1=0,b1=n-1;while(a1<b1){const md=(a1+b1+1)>>1;if(T[md][0]<=f[0])a1=md;else b1=md-1}
   const v=pv(f[3],f[1]);if(v<lo||v>hi)return;nf++;const xx=X(a1),y=Y(v),up=f[2]=='B',z=4;
   x.fillStyle=up?(f[1]=='L'?'#5b8cff':'#4ade80'):'#ff6b6b';x.beginPath();if(up){x.moveTo(xx,y+2);x.lineTo(xx-z,y+2+z*1.6);x.lineTo(xx+z,y+2+z*1.6)}else{x.moveTo(xx,y-2);x.lineTo(xx-z,y-2-z*1.6);x.lineTo(xx+z,y-2-z*1.6)}x.closePath();x.fill();});
  const ra=[],rb=[];for(let i=1;i<n;i++){if(T[i-1][1]>0&&T[i-1][2]>0&&T[i][1]>0&&T[i][2]>0){ra.push(Math.log(T[i][1]/T[i-1][1]));rb.push(Math.log(T[i][2]/T[i-1][2]));}}
  const mm=ra.length;if(mm>5){const ma=ra.reduce((q,v)=>q+v,0)/mm,mb=rb.reduce((q,v)=>q+v,0)/mm;let v1=0,v2=0,cv=0;for(let i=0;i<mm;i++){v1+=(ra[i]-ma)**2;v2+=(rb[i]-mb)**2;cv+=(ra[i]-ma)*(rb[i]-mb)}
   const be=cv/v1,co=cv/Math.sqrt(v1*v2),sd=Math.sqrt(v1/mm)*100,days=(T[n-1][0]-t0)/86400,bpd=days>0?both[n-1]/days:0;
   x.font='12px -apple-system,Segoe UI,sans-serif';x.fillStyle='#8b949e';x.fillText('окно: β SOXS/SOXL '+be.toFixed(2)+' · корр '+co.toFixed(2)+' · σ SOXL '+sd.toFixed(2)+'%/бар · моих сделок в окне '+nf+' · ▲ покупка ▼ продажа · пунктир — мои заявки',12,top+76);
   const nt=$('dkpcnote');if(nt)nt.innerHTML=`За окно (${days.toFixed(1)} дн): <b>лонг обеих поровну ${both[n-1]>0?'+':''}${both[n-1].toFixed(2)}%</b> — это и есть «тета» пары (распад + гамма), ≈ <span class="${bpd<0?'neg':'pos'}">${bpd>0?'+':''}${bpd.toFixed(2)}%/день</span>; на мои $${(DKSOX.L+DKSOX.S).toFixed(0)} это ≈ $${((DKSOX.L+DKSOX.S)*bpd/100).toFixed(2)}/день. Отношение SOXL/SOXS ${rat[n-1]>0?'+':''}${rat[n-1].toFixed(2)}% — не схождение, а ход индекса ×6. β окна ${be.toFixed(2)} против β 120 дн ${DKSOX.beta}: ${Math.abs(Math.abs(be)-DKSOX.beta)>0.1?'<span class="warn">расходятся — хедж по β может быть неточен</span>':'совпадают'}.`;}
  x.font='12px -apple-system,Segoe UI,sans-serif';}
 const L=T[n-1],lp=pnl[n-1];
 if(hasPos){x.beginPath();pnl.forEach((v,i)=>{const y=Q(v);i?x.lineTo(X(i),y):x.moveTo(X(i),y)});x.lineTo(X(n-1),Q(0));x.lineTo(X(0),Q(0));x.closePath();x.fillStyle=lp>=0?'rgba(38,166,154,.18)':'rgba(239,83,80,.18)';x.fill();line(pnl,Q,lp>=0?'#26a69a':'#ef5350',1.6)}
 else{x.fillStyle='#787b86';x.fillText('позиции по паре нет',pl+10,(p3t+p3b)/2+16)}
 let ya=Y(va(L)),yb=Y(vb(L));if(Math.abs(ya-yb)<19){if(ya<=yb)yb=ya+19;else ya=yb+19}
 const pf=v=>pct?sgn(v.toFixed(2))+'%':v.toFixed(2);
 tag(yb,pf(vb(L)),'#16a34a');tag(ya,pf(va(L)),'#2962ff');
 let yr=R(rat[n-1]),yo=R(both[n-1]);if(Math.abs(yr-yo)<19){if(yr<=yo)yo=yr+19;else yr=yo+19}
 tag(yo,sgn(both[n-1].toFixed(2))+'%','#f0b90b','#131722');tag(yr,sgn(rat[n-1].toFixed(2))+'%','#f7525f');
 if(hasPos)tag(Q(lp),sgn(lp.toFixed(2))+'$',lp>=0?'#26a69a':'#ef5350');
 const h=PCH!=null&&PCH>=0&&PCH<n?PCH:n-1,Hr=T[h],chg=(a,b)=>{const d=a-b;return sgn(d.toFixed(2))+' ('+sgn((d/b*100).toFixed(2))+'%)'};
 const fmV=v=>v>=1e6?(v/1e6).toFixed(2)+'M':v>=1e3?(v/1e3).toFixed(1)+'K':String(Math.round(v)),bl={'10s':'10с','30s':'30с','1m':'1м','5m':'5м','1h':'1ч'}[PCB]||PCB;
 x.font='13px -apple-system,Segoe UI,sans-serif';
 const leg=(y,parts)=>{let xx=12;parts.forEach(([t,col])=>{x.fillStyle=col;x.fillText(t,xx,y);xx+=x.measureText(t).width+6})};
 leg(top+16,[['Объём SOXL $','#b2b5be'],[fmV(Hr[3]||0),'#26a69a'],['· '+bl+' · Bybit','#787b86']]);
 leg(top+36,[['●','#2962ff'],['SOXLUSDT Perpetual · 3× бык','#d1d4dc'],[Hr[1].toFixed(2),'#2962ff'],[chg(Hr[1],a0),'#2962ff']]);
 leg(top+56,[['●','#22c55e'],['SOXSUSDT Perpetual · 3× медведь','#d1d4dc'],[Hr[2].toFixed(2),'#22c55e'],[chg(Hr[2],b0),'#22c55e']]);
 leg(p2t+16,[['SOXL/SOXS · % от начала окна','#d1d4dc'],[sgn(rat[h].toFixed(2))+'%','#f7525f'],[' лонг обеих поровну','#d1d4dc'],[sgn(both[h].toFixed(2))+'%','#f0b90b']]);
 leg(p3t+16,[['Моя текущая позиция за окно (SOXL '+sa+' · SOXS '+sb+')','#d1d4dc'],[hasPos?sgn(pnl[h].toFixed(2))+'$':'—',pnl[h]>=0?'#26a69a':'#ef5350']]);
 x.font='12px -apple-system,Segoe UI,sans-serif';
 if(PCH!=null&&PCH>=0&&PCH<n){const xx=X(PCH);x.strokeStyle='#9598a1';x.setLineDash([4,4]);x.beginPath();x.moveTo(xx,top);x.lineTo(xx,p3b);x.stroke();
  if(PCY!=null&&PCY<p3b&&PCY>top){x.beginPath();x.moveTo(pl,PCY);x.lineTo(W-pr,PCY);x.stroke();x.setLineDash([]);
   const v=PCY<s1?pf(lo+(hi-lo)*(1-(PCY-top)/(s1-top))):PCY<s2?sgn((rlo+(rhi-rlo)*(1-(PCY-p2t)/(p2b-p2t))).toFixed(2))+'%':sgn((qlo+(qhi-qlo)*(1-(PCY-p3t)/(p3b-p3t))).toFixed(2))+'$';tag(PCY,v,'#363a45')}
  x.setLineDash([]);const tt=tf(T[PCH][0]);x.fillStyle='#363a45';const tw=x.measureText(tt).width+12;x.fillRect(xx-tw/2,H-pb+2,tw,20);x.fillStyle='#fff';x.textAlign='center';x.fillText(tt,xx,H-8);x.textAlign='left';}}
(function(){const bind=()=>['prc','dkpc'].forEach(id=>{const c=$(id);if(!c||c._b)return;c._b=1;
 c.addEventListener('mousemove',e=>{const r=c.getBoundingClientRect(),sx=c.width/r.width,mx=(e.clientX-r.left)*sx,my=(e.clientY-r.top)*sx,n=PCT.length;if(!n)return;
  PCH=Math.max(0,Math.min(n-1,Math.floor((mx-6)/((c.width-6-(window.PCPR||84))/n))));PCY=my;drawPC();});
 c.addEventListener('mouseleave',()=>{PCH=null;PCY=null;drawPC();});});
 if(document.readyState!='loading')bind();else document.addEventListener('DOMContentLoaded',bind);})();
function drawPx(id,T,bar){const c=$(id),x=c.getContext('2d'),W=c.width,H=c.height;x.clearRect(0,0,W,H);x.font='10px sans-serif';
 if(T.length<2){x.fillStyle='#8b949e';x.fillText('сделки копятся…',10,20);return}
 const pl=56,pr=12,pt=14,pb=18,v=T.flatMap(r=>[r[1],r[2]]);let lo=Math.min(...v),hi=Math.max(...v);const m=(hi-lo)*.06||0.01;lo-=m;hi+=m;
 const t0=T[0][0],t1=T[T.length-1][0],X=t=>pl+(W-pl-pr)*(t-t0)/Math.max(t1-t0,1),Y=p=>pt+(H-pt-pb)*(1-(p-lo)/(hi-lo));
 for(let k=0;k<=4;k++){const p=hi-(hi-lo)*k/4,yy=Y(p);x.strokeStyle='#21262d';x.beginPath();x.moveTo(pl,yy);x.lineTo(W-pr,yy);x.stroke();x.fillStyle='#8b949e';x.fillText(p.toFixed(2),4,yy+3)}
 [[1,'#58a6ff','Bybit'],[2,'#3fb950','MEXC']].forEach(([j,col,n],q)=>{x.strokeStyle=col;x.lineWidth=1.5;x.beginPath();T.forEach((r,i)=>{const xx=X(r[0]),yy=Y(r[j]);i?x.lineTo(xx,yy):x.moveTo(xx,yy)});x.stroke();x.lineWidth=1;
  x.fillStyle=col;x.fillText(n+' '+T[T.length-1][j],pl+6+q*110,pt-3)});
 const hm=s=>new Date(s*1000).toTimeString().slice(0,8);x.fillStyle='#8b949e';x.fillText(hm(t0),pl,H-4);x.fillText(hm(t1),W-pr-50,H-4);}
function drawTick(id,B,fee,bar,T){T=T||[];const c=$(id),x=c.getContext('2d'),W=c.width,H=c.height;x.clearRect(0,0,W,H);x.font='10px sans-serif';
 if(B.length<2){x.fillStyle='#8b949e';x.fillText('история копится (снимок раз в 2 с)…',10,20);return}
 const pl=56,pr=12,pt=10,pb=18; let lo=Math.min(...B.map(r=>r[3]),-fee*1.1,...T.map(r=>r[3])),hi=Math.max(...B.map(r=>r[2]),fee*1.1,...T.map(r=>r[3]));
 B.forEach(r=>{if(r[5]>fee)lo=Math.min(lo,-r[5]);if(r[6]>fee)hi=Math.max(hi,r[6])});const m=(hi-lo)*.06;lo-=m;hi+=m;
 const t0=B[0][0],t1=B[B.length-1][0]+bar,X=t=>pl+(W-pl-pr)*(t-t0)/Math.max(t1-t0,1),Y=v=>pt+(H-pt-pb)*(1-(v-lo)/(hi-lo)),bw=Math.max(1,(W-pl-pr)/((t1-t0)/bar)*.7);
 for(let k=0;k<=4;k++){const v=hi-(hi-lo)*k/4,yy=Y(v);x.strokeStyle='#21262d';x.beginPath();x.moveTo(pl,yy);x.lineTo(W-pr,yy);x.stroke();x.fillStyle='#8b949e';x.fillText((v>0?'+':'')+v.toFixed(3)+'%',4,yy+3)}
 x.strokeStyle='#30363d';x.beginPath();x.moveTo(pl,Y(0));x.lineTo(W-pr,Y(0));x.stroke();
 x.setLineDash([4,3]);x.strokeStyle='#f85149';[fee,-fee].forEach(v=>{x.beginPath();x.moveTo(pl,Y(v));x.lineTo(W-pr,Y(v));x.stroke()});x.setLineDash([]);
 B.forEach(r=>{const xx=X(r[0])+bw/2,up=r[4]>=r[1];x.strokeStyle=x.fillStyle=up?'#26a69a':'#ef5350';x.beginPath();x.moveTo(xx,Y(r[2]));x.lineTo(xx,Y(r[3]));x.stroke();
  const y1=Y(Math.max(r[1],r[4])),y2=Y(Math.min(r[1],r[4]));x.fillRect(xx-bw/2,y1,bw,Math.max(1,y2-y1));
  if(r[6]>fee){x.fillStyle='#3fb950';x.beginPath();x.arc(xx,Y(r[6]),2.5,0,7);x.fill()} if(r[5]>fee){x.fillStyle='#58a6ff';x.beginPath();x.arc(xx,Y(-r[5]),2.5,0,7);x.fill()}});
 if(T.length>1){x.strokeStyle='#ff6b6b';x.lineWidth=1.3;x.beginPath();T.filter(r=>r[0]>=t0).forEach((r,i)=>{const xx=X(r[0])+bw/2,yy=Y(r[3]);i?x.lineTo(xx,yy):x.moveTo(xx,yy)});x.stroke();x.lineWidth=1;
  x.fillStyle='#ff6b6b';const lv=T[T.length-1][3];x.fillText('BYBIT/MEXC (сделки) '+(lv>0?'+':'')+lv.toFixed(3)+'%',pl+6,pt+8);}
 const hm=s=>new Date(s*1000).toTimeString().slice(0,8);x.fillStyle='#8b949e';x.fillText(hm(t0),pl,H-4);x.fillText(hm(t1),W-pr-50,H-4);
 x.fillStyle='#f85149';x.fillText('±тейкер '+fee.toFixed(3)+'%',W-pr-110,Y(fee)-3);}
function rtvSet(sym){sym=(sym||'').trim();if(!sym)return;RTV=sym;{const mm=sym.match(/BYBIT:([A-Z0-9]+)USDT/);if(mm){RTK=mm[1];try{localStorage.setItem('rtk',RTK)}catch(e){}rtkTick();}}try{localStorage.setItem('rtv',RTV)}catch(e){} rtvLoad();rtvDraw();$('rtv').scrollIntoView({block:'nearest'});}
function rtvInt(v){RTVI=v;try{localStorage.setItem('rtvi',RTVI)}catch(e){} rtvDraw();}
function drawDV(v){const cv=$('tdv'),x=cv.getContext('2d'),W=cv.width,H=cv.height;x.clearRect(0,0,W,H);const s=v.series||[];if(s.length<2)return;
 const lo=Math.min(...s,v.p10)-2,hi=Math.max(...s,v.p90)+2,X=i=>36+i/(s.length-1)*(W-110),Y=y=>8+(hi-y)/(hi-lo)*(H-16);x.font='9px sans-serif';
 [[v.p80,'#3fb950','p80 продавать'],[v.p50,'#8b949e','медиана'],[v.p10,'#d29922','p10']].forEach(([y,c,l])=>{x.strokeStyle=c;x.setLineDash([4,3]);x.beginPath();x.moveTo(36,Y(y));x.lineTo(W-74,Y(y));x.stroke();x.setLineDash([]);x.fillStyle=c;x.fillText(l+' '+y,W-72,Y(y)+3);});
 x.strokeStyle='#58a6ff';x.lineWidth=1.4;x.beginPath();s.forEach((y,i)=>i?x.lineTo(X(i),Y(y)):x.moveTo(X(i),Y(y)));x.stroke();x.lineWidth=1;
 x.fillStyle='#8b949e';x.fillText(hi.toFixed(0),2,12);x.fillText(lo.toFixed(0),2,H-4);x.fillText('120д',36,H-2);}
// ===================== ФАКТОРНЫЕ СПРЕДЫ =====================
let PSEL=0, PD=null;
async function ptick(){ if(!(TAB=='A'&&ASUB=='pairs'))return;
 let d; try{d=await (await fetch('/pairs')).json()}catch(e){$('pbar').textContent='offline';return}
 if(d.err&&!d.loading){$('pbar').textContent='ошибка: '+d.err;return}
 if(d.loading){$('pbar').textContent='считаю историю пар (первый запуск ~30–60с)...'+(d.err?' ошибка: '+d.err:'');return}
 PD=d; $('pbar').textContent='история обновлена '+d.age_min+' мин назад'+(d.busy?' · пересчёт...':'')+' · бюджет на пару $'+d.cfg.budget+' · '+d.t;
 let h='<tr><th>пара A / B</th><th>сессия</th><th>статус</th><th>β</th><th>σ ‰</th><th>полураспад</th><th>z live</th><th>сигнал</th><th>нога A</th><th>нога B</th><th>бэктест n / win / avg / worst / MAE</th><th>модель в позиции</th><th>мои ноги</th></tr>';
 d.rows.forEach((r,i)=>{ if(r.err){h+='<tr><td>'+r.a+' / '+r.b+'</td><td colspan="12" style="text-align:left" class="neg">'+r.err+'</td></tr>';return;}
  const zc=Math.abs(r.z)>=2?'neg':(Math.abs(r.z)>=1?'warn':''), sc={in:'pos',add:'warn',exit:'',wait:''}[r.sk];
  const bt=r.bt, op=bt.open, m=r.mine;
  h+='<tr style="cursor:pointer'+(i==PSEL?';background:#1f6feb22':'')+'" onclick="PSEL='+i+';ptick()"><td><b>'+r.a+'</b> / '+r.b+'</td><td>'+r.sess+(r.open?' <span class="pos">●</span>':' <span style="color:#8b949e">○</span>')+'</td>'
   +'<td class="'+(r.kind=='рабочая'?'pos':'warn')+'">'+r.kind+'</td><td>'+r.beta+'</td><td>'+r.sd_pm+'</td><td>'+(r.hl??'—')+' бар</td>'
   +'<td class="'+zc+'"><b>'+sg(r.z)+'</b></td><td class="'+sc+'">'+r.sig+'</td>'
   +'<td>$'+r.legA+(r.qa!=null?' = '+r.qa:'')+'</td><td>$'+r.legB+(r.qb!=null?' = '+r.qb:'')+'</td>'
   +'<td>'+(bt.n?bt.n+' / '+bt.win+'% / '+sg(bt.avg)+' / '+bt.worst+' / '+bt.mae:'нет сделок')+(bt.cost?' <span class="'+((bt.net??0)>0?'pos':'neg')+'">нетто '+sg(bt.net)+'</span> <span style="color:#8b949e">(ком. '+bt.cost+')</span>':'')+'</td>'
   +'<td>'+(op?op.dir+', '+op.days+'д, '+sg(op.pnl_bp)+'бп'+(op.units>1?' (добор)':''):'—')+'</td>'
   +'<td>'+(m?((m.hedged?'<span class="pos">':'<span class="neg">')+'A '+m.a+' / B '+m.b+' · B/A '+m.ratio+' (β '+r.beta+')</span>'):'—')+'</td></tr>';});
 $('ptab').innerHTML=h;
 const r=d.rows[PSEL]; if(r&&!r.err)drawZ(r);
 let f='<tr><th>фактор</th><th>нетто $</th><th>лимит</th><th>β-QQQ $</th><th>состав</th></tr>';
 d.fac.forEach(x=>{f+='<tr><td>'+x.f+'</td><td class="'+(x.over?'neg':'')+'">'+sg(x.net)+'</td><td>'+(x.lim?'±'+x.lim:'—')+'</td><td>'+sg(x.bqq)+'</td><td style="text-align:left">'+(x.parts.join(' · ')||'—')+'</td></tr>';});
 f+='<tr><td><b>Итого β-QQQ</b></td><td></td><td></td><td class="'+(Math.abs(d.tot_bqq)>100?'warn':'pos')+'"><b>'+sg(d.tot_bqq)+'</b></td><td style="text-align:left;color:#8b949e">цель ≈ 0: корзина не должна усиливать риск дальних путов ETH</td></tr>';
 $('pfac').innerHTML=f;
 $('upd').textContent='факторные спреды  .  '+d.t;}
function drawZ(r){const cv=$('pcv'),x=cv.getContext('2d'),W=cv.width,H=cv.height;x.clearRect(0,0,W,H);const s=r.zs||[];if(s.length<2)return;
 const vs=s.map(a=>a[1]).concat([r.z]);const lo=Math.min(-3.5,...vs),hi=Math.max(3.5,...vs);const X=i=>36+i/(s.length)*(W-120),Y=y=>10+(hi-y)/(hi-lo)*(H-24);x.font='9px sans-serif';
 [[0,'#8b949e','выход 0'],[2,'#f85149','вход +2'],[-2,'#3fb950','вход −2'],[3,'#d29922','добор +3'],[-3,'#d29922','добор −3']].forEach(([y,c,l])=>{x.strokeStyle=c;x.setLineDash(y?[4,3]:[]);x.beginPath();x.moveTo(36,Y(y));x.lineTo(W-84,Y(y));x.stroke();x.setLineDash([]);x.fillStyle=c;x.fillText(l,W-80,Y(y)+3);});
 x.strokeStyle='#58a6ff';x.lineWidth=1.3;x.beginPath();s.forEach((a,i)=>i?x.lineTo(X(i),Y(a[1])):x.moveTo(X(i),Y(a[1])));x.stroke();x.lineWidth=1;
 x.fillStyle='#e6edf3';x.beginPath();x.arc(X(s.length),Y(r.z),3.5,0,7);x.fill();
 const d0=new Date(s[0][0]),d1=new Date(s[s.length-1][0]);x.fillStyle='#8b949e';x.fillText(d0.getDate()+'.'+(d0.getMonth()+1),36,H-2);x.fillText(d1.getDate()+'.'+(d1.getMonth()+1),W-150,H-2);
 x.fillStyle='#e6edf3';x.font='bold 11px sans-serif';x.fillText(r.a+' / '+r.b+'  z '+r.z+'  (последние 400 баров сессии)',40,12);
 $('pnote').textContent='Ноги: '+r.a+' $'+r.legA+(r.qa!=null?' ('+r.qa+' шт @'+r.pa+')':'')+' против '+r.b+' $'+r.legB+(r.qb!=null?' ('+r.qb+' шт @'+r.pb+')':'')+' в противоположную сторону. Сначала тонкая нога лимиткой post-only, потом вторая.';}
function setGW(w){GW=w;['2d','30d','166d'].forEach(k=>$('gw'+k).classList.toggle('on',k==w));try{localStorage.setItem('gw',w)}catch(e){} if(GLD)drawSpread(GLD);}
async function gtick(){ if(!(TAB=='G'||(TAB=='A'&&ASUB=='gold')))return;
 let g; try{g=await (await fetch('/gold')).json()}catch(e){$('gbar').textContent='золото: offline';return}
 if(g.err){$('gbar').textContent='золото: ошибка '+g.err;return}
 GLD=g; renderGold(g); drawSpread(g); renderWings(g);}
function renderWings(g){
 const w=g.wings||{}; if(w.err){$('wtiles').innerHTML='<span class="neg">однодневки: '+w.err+'</span>';return;}
 const ex=w.exps||[];
 $('wtiles').innerHTML=tile('RV 30д / 2д',w.rv30+'% / '+w.rv2+'%')+tile('честная σ',w.sig+'%')
  +ex.map(e=>tile('ATM IV '+e.exp+' ('+e.h+'ч)',(e.ivatm??'—')+'%',e.ivatm&&e.ivatm>w.sig?'pos':'neg')).join('')
  +tile('круг стопа XAUT, 1 oz','$'+w.cost_xaut,'neg')+tile('круг стопа XAU, 1 oz','$'+w.cost_xau,'warn');
 const s=w.stop||{};
 let sh=(s.shorts&&s.shorts.length)?('Шорты XAUT-опционов: '+s.shorts.map(x=>x.sym+' ×'+x.q+' ('+(x.dist>0?'+':'')+x.dist+'%)').join(' · ')+'<br>'):'<span style="color:#8b949e">Шортов XAUT-опционов нет. </span>';
 if(s.itm&&s.itm.length){sh+='<b class="'+(Math.abs(s.gap)>0.0005?'neg':'pos')+'">В деньгах: '+s.itm.map(x=>x[0]+' '+x[1]+' ×'+x[2]).join(', ')
   +' → перп должен быть '+(s.need>0?'+':'')+s.need+' oz, сейчас '+(s.perp>0?'+':'')+s.perp+(Math.abs(s.gap)>0.0005?(' — ДОБАВЬ '+(s.gap>0?'лонг ':'шорт ')+Math.abs(s.gap)+' oz'):' — ок')+'</b>';}
 else if(s.shorts&&s.shorts.length) sh+='<span class="pos">Все шорты вне денег — стопы ждут на страйках, перп не нужен.</span>';
 $('wstop').innerHTML=sh;
 let h='';
 ex.forEach(e=>{
  const st=e.strangle;
  h+='<div style="margin:8px 0 2px;font-size:12px"><b style="color:#e6edf3">'+e.exp+'</b> · до экспиры '+e.h+'ч (08:00 UTC) · XAUT '+e.und
   +(st?(' &nbsp;·&nbsp; <b>самый широкий стрэнгл '+st.Kp+'P / '+st.Kc+'C</b>: премия $'+st.prem+'/oz (лот 0.1 = $'+st.lot01+') · P(в деньги) '+st.pitm+'% · '
    +'эдж <span class="'+(st.edge>0?'pos':'neg')+'">'+(st.edge>0?'+':'')+st.edge+'$</span> · БЕ '+st.be_lo+' / '+st.be_hi):' · нет пары с живым bid')+'</div>';
  h+='<table><tr><th>опцион</th><th>страйк</th><th>от цены</th><th>σ-дист</th><th>bid × разм</th><th>ask</th><th>IV</th><th>P(ITM)</th><th>честная</th><th>эдж $/oz</th><th>стоп XAUT</th><th>стоп XAU</th><th>разворотов XAUT/XAU</th></tr>';
  [...e.calls,...e.puts].forEach(r=>{h+='<tr><td>'+(r.cp=='C'?'<span class="warn">колл</span>':'<span class="pos">пут</span>')+'</td><td>'+r.K+'</td><td>'+(r.dist>0?'+':'')+r.dist+'%</td><td>'+r.sd+'</td><td>'+r.bid+' × '+r.bsz+'</td><td>'+r.ask+'</td><td>'+r.iv+'%</td><td>'+r.pitm+'%</td><td>'+r.fair+'</td><td class="'+(r.edge>0?'pos':'neg')+'">'+(r.edge>0?'+':'')+r.edge+'</td><td>'+(r.cp=='C'?'buy ≥ ':'sell ≤ ')+r.stop_xaut+'</td><td>'+r.stop_xau+'</td><td class="'+(r.kill_xau>=1?'pos':'neg')+'">'+r.kill_xaut+' / '+r.kill_xau+'</td></tr>';});
  h+='</table>';});
 $('wexps').innerHTML=h||'<span style="color:#8b949e">нет однодневных экспир в окне 72ч</span>';
}
const sg=v=>(v==null?'—':(v>0?'+':'')+v);
const pm=v=>(v==null?'—':(v>0?'+':'')+Number(v).toFixed(2)+'‰');
function renderGold(g){
 const a=g.acct, mc=a.mm_pct>75?'neg':(a.mm_pct>62?'warn':'pos');
 $('gacct').innerHTML=tile('XAU (index-перп)','$'+g.xau)+tile('XAUT (токен-перп)','$'+g.xaut)
  +tile('Эквити','$'+a.eq)+tile('Свободно','$'+a.av,a.av<50?'neg':'')+tile('MM',a.mm_pct+'%',mc);
 const col={conv:['#3fb950','#3fb95022'],div:['#d29922','#d2992222'],stop:['#f85149','#f8514922'],wait:['#8b949e','#161b22']}[g.kind];
 const bar=$('gbar'); bar.textContent=g.sig; bar.style.color=col[0]; bar.style.borderColor=col[0]=='#8b949e'?'#30363d':col[0]; bar.style.background=col[1];
 $('gwarn').innerHTML=(g.warns||[]).map(w=>'<div class="warn">⚠ '+w+'</div>').join('');
 const z=g.z, zc=v=>v==null?'':(Math.abs(v)>=2?'neg':(Math.abs(v)>=1?'warn':''));
 $('gtiles').innerHTML=tile('спред (mid)',pm(g.mid),zc(z['30d']))+tile('спред $',sg(g.usd)+'$')
  +tile('Z 2д',sg(z['2d']),zc(z['2d']))+tile('Z 30д',sg(z['30d']),zc(z['30d']))+tile('Z 166д',sg(z['166d']),zc(z['166d']))
  +tile('перцентиль 30д / 166д',g.rank['30d']+'% / '+g.rank['166d']+'%')
  +tile('полураспад 30д / 2д',(g.hl_h??'—')+'ч / '+(g.hl2_h??'—')+'ч')
  +tile('фандинг XAU / XAUT',(g.tk.XAUUSDT.fund*100).toFixed(4)+'% / '+(g.tk.XAUTUSDT.fund*100).toFixed(4)+'%')
  +tile('carry схожд. сейчас',sg(g.carry_now)+' бп/д',g.carry_now>=0?'pos':'neg')
  +tile('carry схожд. 30д',sg(g.carry_avg)+' бп/д',g.carry_avg>=0?'pos':'neg')
  +tile('круг комиссий',g.fees.rt_mm+' бп (XAU тейк: '+g.fees.rt_mt+')')
  +tile('1‰ на 1 унцию','$'+g.usd_pm_oz);
 const L=g.lv;
 const dist=(x,lvl)=>(x-lvl).toFixed(2);
 $('gconv').innerHTML=tile('исполнимо сейчас',pm(g.conv_x),g.conv_x<=L.conv_entry?'pos':'')
  +tile('ВХОД ≤ (p5)',pm(L.conv_entry),'pos')+tile('добор ≤',pm(L.conv_add))
  +tile('тейк 1 (½)',pm(L.conv_exit1))+tile('тейк 2 (среднее)',pm(L.conv_exit2))
  +tile('стоп',pm(L.conv_stop),'neg')+tile('до входа',dist(g.conv_x,L.conv_entry)+'‰',g.conv_x<=L.conv_entry?'pos':'')
  +tile('эдж до среднего',sg(g.edge_conv)+' бп',g.edge_conv>0?'pos':'neg')
  +tile('эдж на 1 oz','$'+(g.edge_conv/10*g.usd_pm_oz).toFixed(2),g.edge_conv>0?'pos':'neg');
 $('gdiv').innerHTML=tile('исполнимо сейчас',pm(g.div_x),g.div_x>=L.div_entry?'warn':'')
  +tile('ВХОД ≥ (p95)',pm(L.div_entry),'warn')+tile('добор ≥',pm(L.div_add))
  +tile('тейк 1 (½)',pm(L.div_exit1))+tile('тейк 2 (среднее)',pm(L.div_exit2))
  +tile('стоп',pm(L.div_stop),'neg')+tile('до входа',dist(L.div_entry,g.div_x)+'‰',g.div_x>=L.div_entry?'warn':'')
  +tile('эдж до среднего',sg(g.edge_div)+' бп',g.edge_div>0?'pos':'neg')
  +tile('эдж на 1 oz','$'+(g.edge_div/10*g.usd_pm_oz).toFixed(2),g.edge_div>0?'pos':'neg');
 const G=g.greeks, b=g.beta||{};
 $('ggreeks').innerHTML=tile('Δ золото, унций',sg(G.d_oz),Math.abs(G.d_oz)<0.01?'pos':'warn')+tile('Δ$ золото',sg(G.d_usd)+'$',Math.abs(G.d_usd)<50?'pos':'warn')
  +tile('Γ (опц XAUT)',G.g)+tile('Θ/д (опц XAUT)',sg(G.t),G.t<0?'neg':(G.t>0?'pos':''))+tile('Vega (опц XAUT)',sg(G.v))
  +tile('β XAUT←XAU (1ч, 30д)',b.b_ab??'—')+tile('корр XAUT/XAU',b.c_ab??'—')
  +tile('β золото←ETH',b.b_ge??'—')+tile('корр золото/ETH',b.c_ge??'—')
  +tile('спот / Earn XAUT',G.spot+' / '+G.earn);
 const s=g.spread;
 $('gspread').innerHTML=s?('<b>Открытый спред: '+(s.dir=='conv'?'<span class="pos">СХОЖДЕНИЕ</span> (лонг XAUT/шорт XAU)':'<span class="warn">РАСХОЖДЕНИЕ</span> (шорт XAUT/лонг XAU)')
   +'</b> · нога '+s.leg+' oz · вход '+pm(s.entry)+' · P&L '+pm(s.pnl_pm)+' = <span class="'+(s.pnl_usd>=0?'pos':'neg')+'">'+sg(s.pnl_usd)+'$</span>'
   +(Math.abs(s.resid)>0.0005?' · <span class="warn">перекос ног '+sg(s.resid)+' oz — не нейтрально</span>':' · ноги ровные'))
   :'<span style="color:#8b949e">Открытого спреда XAUT/XAU нет.</span>';
 let h='<tr><th>контракт</th><th>ст</th><th>разм</th><th>ср.цена</th><th>Δ</th><th>Γ</th><th>Θ/д</th><th>Vega</th><th>uPnL</th></tr>';
 (g.pos||[]).forEach(p=>{h+='<tr><td>'+p.sym+'</td><td class="'+(p.side=='B'?'pos':'neg')+'">'+p.side+'</td><td>'+p.sz+'</td><td>'+p.avg+'</td><td>'+p.d+'</td><td>'+p.g+'</td><td class="'+(p.t<0?'neg':'pos')+'">'+p.t+'</td><td>'+p.v+'</td><td class="'+(p.up<0?'neg':'pos')+'">'+p.up+'</td></tr>';});
 if(!(g.pos||[]).length)h+='<tr><td colspan="9" style="text-align:left;color:#8b949e">позиций по золоту нет</td></tr>';
 $('gpos').innerHTML=h;
 const st=g.stats[GW];
 $('gstat').textContent='окно '+GW+': среднее '+st.mean+'‰ · σ '+st.sd+' · p5 '+st.p5+' · медиана '+st.p50+' · p95 '+st.p95+' · мин '+st.min+' · макс '+st.max+' · точек '+st.n;
 $('upd').textContent='XAU '+g.xau+' · XAUT '+g.xaut+' · спред '+pm(g.mid)+'  .  '+g.t;
}
function drawSpread(g){const cv=$('gcv'),x=cv.getContext('2d'),W=cv.width,H=cv.height,pl=44,pr=128,pt=12,pb=20;x.clearRect(0,0,W,H);
 const S=(g.series||{})[GW]||[]; if(S.length<2)return; const L=g.lv, st=g.stats[GW];
 const levels=[[L.conv_entry,'#3fb950','вход схожд.',[5,4]],[L.conv_add,'#3fb95088','добор схожд.',[2,4]],[L.conv_stop,'#f85149','стоп схожд.',[2,3]],
  [L.div_entry,'#d29922','вход расхожд.',[5,4]],[L.div_add,'#d2992288','добор расхожд.',[2,4]],[L.div_stop,'#f85149','стоп расхожд.',[2,3]],
  [L.conv_exit2,'#8b949e','среднее 30д',[]],[L.conv_exit1,'#3fb95055','тейк1 схожд.',[1,3]],[L.div_exit1,'#d2992255','тейк1 расхожд.',[1,3]]];
 const vs=S.map(a=>a[1]); let y0=Math.min(...vs,L.conv_entry,g.mid),y1=Math.max(...vs,L.div_entry,g.mid);
 if(g.spread&&g.spread.entry!=null){y0=Math.min(y0,g.spread.entry);y1=Math.max(y1,g.spread.entry);}
 const q=(y1-y0)*0.08||0.3; y0-=q; y1+=q;
 const t0=S[0][0],t1=S[S.length-1][0];
 const X=t=>pl+(t-t0)/(t1-t0)*(W-pl-pr), Y=v=>pt+(y1-v)/(y1-y0)*(H-pt-pb);
 x.font='9px sans-serif';
 for(let i=0;i<=4;i++){const v=y0+(y1-y0)*i/4;x.strokeStyle='#21262d';x.beginPath();x.moveTo(pl,Y(v));x.lineTo(W-pr,Y(v));x.stroke();x.fillStyle='#8b949e';x.fillText(v.toFixed(2),4,Y(v)+3);}
 for(let i=0;i<=5;i++){const t=t0+(t1-t0)*i/5,dt=new Date(t);const lab=GW=='2d'?(dt.getDate()+'.'+(dt.getMonth()+1)+' '+String(dt.getHours()).padStart(2,'0')+':00'):(dt.getDate()+'.'+(dt.getMonth()+1));x.fillStyle='#8b949e';x.fillText(lab,X(t)-14,H-5);}
 levels.forEach(([v,c,lab,dash])=>{if(v==null||v<y0||v>y1)return;x.strokeStyle=c;x.setLineDash(dash);x.beginPath();x.moveTo(pl,Y(v));x.lineTo(W-pr,Y(v));x.stroke();x.setLineDash([]);
   x.fillStyle=c;x.fillText(lab+' '+v.toFixed(2),W-pr+4,Y(v)+3);});
 if(GW!='30d'){x.strokeStyle='#58a6ff55';x.setLineDash([6,3]);x.beginPath();x.moveTo(pl,Y(st.mean));x.lineTo(W-pr,Y(st.mean));x.stroke();x.setLineDash([]);}
 if(g.spread&&g.spread.entry!=null){x.strokeStyle='#bc8cff';x.setLineDash([3,2]);x.beginPath();x.moveTo(pl,Y(g.spread.entry));x.lineTo(W-pr,Y(g.spread.entry));x.stroke();x.setLineDash([]);x.fillStyle='#bc8cff';x.fillText('мой вход '+g.spread.entry.toFixed(2),W-pr+4,Y(g.spread.entry)-6);}
 x.strokeStyle='#58a6ff';x.lineWidth=1.4;x.beginPath();S.forEach((a,i)=>{const xx=X(a[0]),yy=Y(a[1]);i?x.lineTo(xx,yy):x.moveTo(xx,yy)});x.stroke();x.lineWidth=1;
 const cx=W-pr,cy=Y(g.mid);x.fillStyle='#e6edf3';x.beginPath();x.arc(cx,cy,3.5,0,7);x.fill();
 x.fillStyle='#58a6ff';x.fillRect(W-pr+2,cy-8,62,15);x.fillStyle='#0d1117';x.font='bold 10px sans-serif';x.fillText(g.mid.toFixed(2)+'‰',W-pr+6,cy+3);}
// ===================== КНИГА (главная) =====================
let KH=null, KGK='';
function bookMore(){}
// график P&L от цены — как в конструкторе портфеля Bybit (сегодня / через 30 дн / на последней экспирации)
const PAY={};
function drawPayoff(id,c,name){const cv=$(id);if(!cv||!c)return;PAY[id]={c,name};const x=cv.getContext('2d'),W=cv.width,H=cv.height,pl=56,pr=14,pt=22,pb=24;
 x.clearRect(0,0,W,H);const xs=c.x,ser=[['today','#f85149',[],'сегодня'],['d30','#58a6ff',[6,4],'через 30 дн'],['exp','#d29922',[3,3],'экспирация '+c.exp_label]];
 let lo=0,hi=0;ser.forEach(q=>(c[q[0]]||[]).forEach(v=>{lo=Math.min(lo,v);hi=Math.max(hi,v)}));const pad=(hi-lo)*0.08||10;lo-=pad;hi+=pad;
 const X=v=>pl+(v-xs[0])/(xs[xs.length-1]-xs[0])*(W-pl-pr),Y=v=>pt+(hi-v)/(hi-lo)*(H-pt-pb);
 x.font='10px sans-serif';x.strokeStyle='#21262d';x.fillStyle='#8b949e';
 for(let i=0;i<=5;i++){const v=lo+(hi-lo)*i/5,y=Y(v);x.beginPath();x.moveTo(pl,y);x.lineTo(W-pr,y);x.stroke();x.fillText((v>0?'+':'')+v.toFixed(0),4,y+3);}
 for(let i=0;i<=8;i++){const v=xs[0]+(xs[xs.length-1]-xs[0])*i/8,X0=X(v);x.beginPath();x.moveTo(X0,pt);x.lineTo(X0,H-pb);x.stroke();x.fillText(v>=100?v.toFixed(0):v.toFixed(1),X0-12,H-8);}
 x.strokeStyle='#8b949e';x.setLineDash([2,2]);x.beginPath();x.moveTo(pl,Y(0));x.lineTo(W-pr,Y(0));x.stroke();
 x.strokeStyle='#e6edf3';x.beginPath();x.moveTo(X(c.S0),pt);x.lineTo(X(c.S0),H-pb);x.stroke();x.setLineDash([]);
 ser.forEach(q=>{const a=c[q[0]];if(!a)return;x.strokeStyle=q[1];x.lineWidth=q[0]=='today'?2:1.5;x.setLineDash(q[2]);x.beginPath();a.forEach((v,i)=>{i?x.lineTo(X(xs[i]),Y(v)):x.moveTo(X(xs[i]),Y(v))});x.stroke();});
 x.setLineDash([]);x.lineWidth=1;
 // безубытки на экспирации
 const be=[];const ex=c.exp||[];for(let i=1;i<ex.length;i++)if((ex[i-1]<0)!=(ex[i]<0)){const f=ex[i-1]/(ex[i-1]-ex[i]);be.push(xs[i-1]+(xs[i]-xs[i-1])*f);}
 x.fillStyle='#d29922';be.forEach(b=>{x.beginPath();x.arc(X(b),Y(0),3,0,7);x.fill();x.fillText(b>=100?b.toFixed(0):b.toFixed(2),X(b)-14,Y(0)-6);});
 let lx=pl+6;ser.forEach(q=>{x.fillStyle=q[1];x.fillRect(lx,6,14,3);x.fillStyle='#c9d1d9';x.fillText(q[3],lx+18,11);lx+=x.measureText(q[3]).width+40;});
 x.fillStyle='#8b949e';x.fillText(name+' сейчас '+(c.S0>=100?c.S0.toFixed(0):c.S0.toFixed(2))+(be.length?' · безубыток на экспирации: '+be.map(b=>b>=100?b.toFixed(0):b.toFixed(2)).join(', '):''),lx+10,11);
 if(!cv._hv){cv._hv=1;cv.addEventListener('mousemove',ev=>{const P=PAY[id];if(!P)return;const r=cv.getBoundingClientRect(),mx=(ev.clientX-r.left)*cv.width/r.width;drawPayoff(id,P.c,P.name);
  const c2=P.c,n=c2.x.length,i=Math.max(0,Math.min(n-1,Math.round((mx-pl)/(W-pl-pr)*(n-1)))),xx=X(c2.x[i]),g=cv.getContext('2d');
  g.strokeStyle='#6e7681';g.beginPath();g.moveTo(xx,pt);g.lineTo(xx,H-pb);g.stroke();
  const txt=[(c2.x[i]>=100?c2.x[i].toFixed(0):c2.x[i].toFixed(2))+' ('+((c2.x[i]/c2.S0-1)*100).toFixed(1)+'%)','сегодня '+c2.today[i],'30 дн '+c2.d30[i],'эксп. '+c2.exp[i]];
  g.fillStyle='rgba(22,27,34,.92)';const bx=Math.min(xx+8,W-170);g.fillRect(bx,pt+4,160,62);g.fillStyle='#e6edf3';txt.forEach((t,k)=>g.fillText(t,bx+6,pt+18+k*14));});
  cv.addEventListener('mouseleave',()=>{const P=PAY[id];if(P)drawPayoff(id,P.c,P.name);});}}
// ===================== ДЕСК: текущее положение ETH + SOX =====================
async function dtick(){ if(TAB!='D')return;
 let d; try{d=await (await fetch('/desk')).json()}catch(e){$('upd').textContent='деск offline';return}
 if(d.err){$('dktodo').innerHTML='<span class="neg">ошибка: '+d.err+'</span>';return}
 renderDesk(d);}
// строка «Избранное» в шапке деска: переносит плитки по подписи (порядок — как в списке); пустые строки прячет
const DKFAV=['Эквити','Запас','Доступно','IM','MM','Маржа SOX','Δ SOX до закрытия США','Θ книги SOX','Δ купленных опционов','P&L открытых позиций','P&L счёта · 30 дн','MEXC','Брутто перпов'];
const DKTAIL={'Счёт':['Δ книги'],'SOX':['хедж SOX','Δ после закрытия']};   // плитки, которые ставятся в конец своей строки
const DKBETA=['β SOXS/SOXL','SOXL − SOXS','Отклонение от зеркала','Хедж в штуках','Распад пары L·S','β ETH к SOXL','корр ETH·SOXL'];
// подсказки к плиткам шапки деска (по подписи; ключ с «*» — по началу подписи) и режим «панели»
const DKTIPS={
 'Эквити':'Эквити единого счёта Bybit: деньги + стоимость всех позиций по марку. Боты и Funding — отдельно',
 'Запас':'Запас = эквити − MM: столько $ может потерять счёт до ликвидации. MM в Portfolio Margin пересчитывается сценарием (SOXL ±28%, IV), поэтому это сумма при нынешней марже, а не уровень цены. ×к MM = эквити / MM, ликвидация при ×1',
 'Доступно':'Доступно = эквити − IM: свободно под новые позиции и на вывод. При 0 новые ноги с риском не откроются, но до ликвидации ещё IM − MM',
 'IM':'Начальная маржа в % эквити. Правило: выше 40% — осторожно, выше 50% — новых ног не открывать, резать плечо',
 'MM':'Поддерживающая маржа в % эквити. 100% = ликвидация. Bybit считает её худшим сценарием по каждой монете отдельно (SOXL и SOXS не зачитываются)',
 'Маржа SOX':'MM — что Bybit держит под SOXL и SOXS (по монетам отдельно). Худший — реальный убыток пары в худшем сценарии Bybit с учётом зеркала. Без хода цены — сценарий, где меняется только IV',
 'Δ SOX до закрытия США':'Дельта книги в $ на 1 SOXL-эквивалент внутри дня: SOXL + SOXS по зеркалу от закрытия США (β = −1) + опционы. Минус — книга теряет на росте SOXL. Хедж — от $40',
 'Δ после закрытия':'Дельта после ребалансировки фондов на закрытии NYSE (16:00 ET): база зеркала переклеивается, SOXS считается к SOXL по текущим ценам. Разница с «до закрытия» — сколько выровнять на закрытии',
 'Δ книги':'Дельта всего единого счёта по зеркалу; в подписи — в SOXL и вместе с ботами (они на своих счетах). SOXL +1% → столько $ изменится книга',
 'хедж SOX':'Готовый хедж дельты SOX ордером на SOXLUSDT; «нейтраль», пока |Δ| < $40',
 'Δ купленных опционов':'Дельта всех купленных опционов (коллы + путы = стрэнглы/стрэддлы). Около нуля — купленная часть нейтральна и работает на гамму. «Без купленных» — Δ книги без них',
 'Θ книги SOX':'Тета книги в $ в сутки: тета опционов + фандинг перпов. У пары перпов SOXL/SOXS своей теты нет (ОС-13, руна 4)',
 'Θ /сутки':'Тета всего счёта в $ в сутки (все опционы + фандинг); «до 10:00» — сколько набежит до 10:00 Киева',
 'Θ опц SOX':'Тета только опционов SOXL в $ в сутки (греки Bybit)',
 'Γ книги':'Гамма книги: на сколько $ меняется дельта при ходе SOXL на 1%. Плюс — купленные опционы (ход в любую сторону помогает), минус — проданные. Пара перпов внутри дня линейна',
 'Гамма ±5% / ±10%':'P&L от гаммы (без дельты) при ходе SOXL на 5% и 10%',
 'Безубыток / день':'Какой дневной ход SOXL окупает тету купленной гаммы; рядом — реальная дневная вола (RV). RV выше безубытка — купленные опционы окупаются',
 'RV / IV':'RV — как SOXL реально ходил за 30 дней, IV — сколько хода заложено в цены опционов. RV выше IV — выгодно владеть опционами, ниже — продавать. Жёлтая — гамма книги стоит против этого',
 'Ожидание / сутки':'Сколько книга заработает за сутки, если SOXL будет ходить как последние 30 дней: тета + гамма × RV². Плюс — тета приносит больше, чем отнимают ходы (или купленные опционы окупаются)',
 'Коридор*':'Где сработает хедж дельты: цены SOXL, на которых |Δ| дойдёт до шага коридора, и объём ордеров Sell/Buy',
 'США*':'Сессия NYSE для SOXL/SOXS (праздники не учитываются). Основная вола — в сессию США',
 'SOXL / SOXS':'Цены перпов Bybit SOXLUSDT / SOXSUSDT',
 'β SOXS/SOXL':'Лог-β SOXS к SOXL за 120 дней и по часам за 3 дня. Внутри дня хедж по мандату −1 в штуках L0/S0; −0.94 в логарифмах — индикатор волы, а не коэффициент хеджа (ОС-13, руна 5)',
 'SOXL − SOXS':'Разница цен и отношение SOXL/SOXS. Само по себе не спред — отношение трендит',
 'Отклонение от зеркала':'SOXL/L0 + SOXS/S0 − 2 от цен закрытия США. Ниже нуля — обе ноги дешевле зеркала, выше — дороже. Обычно ~0.07%, в выходные до 0.3%; полувозврат ~22 мин',
 'Хедж в штуках':'Сколько SOXS на 1 SOXL держат дельту внутри дня = 0: L0/S0 (равные $ на закрытие). Постоянно весь день, меняется на закрытии',
 'Распад пары L·S':'Произведение SOXL·SOXS к закрытию США: падает при ходе индекса в любую сторону (−9x²). Для суммы ног это ставка тренд/пила, а не тета (ОС-13)',
 'До IM 50%':'Сколько IM снять (закрыть позиций), чтобы IM стал ≤ 50% эквити; «OK» — уже ниже',
 'Брутто перпов':'Сумма модулей номиналов перпов и её доля к эквити (плечо по перпам)',
 'Чистая Δ / эквити':'Модуль чистой дельты SOX в $ к эквити — направленное плечо книги',
 'P&L открытых позиций':'Нереализованный P&L открытых позиций от цены входа: перпы, опционы (по марку), боты с запуска. В подписи — закрытые сделки перпов за сегодня',
 'P&L счёта · 30 дн':'P&L за 30 дней по журналу Bybit (реализованный P&L перпов, премии и поставка опционов, фандинг, комиссии) + открытые позиции + боты. Переводы не входят',
 'MEXC':'Счёт MEXC (read-only ключ): спот + фьючерсы и открытые позиции. В маржу Bybit не входит'};
function dkTip(n){if(DKTIPS[n])return DKTIPS[n];const k=Object.keys(DKTIPS).find(k=>k.endsWith('*')&&n.startsWith(k.slice(0,-1)));return k?DKTIPS[k]:''}
let DKPANELS=0;try{DKPANELS=+(localStorage.getItem('dkpanels')||0)}catch(e){}
function dkPanSet(v){DKPANELS=v;try{localStorage.setItem('dkpanels',v)}catch(e){} if(DKD)renderTiles(DKD)}
const DKPAN=[['Bybit · счёт',['Эквити','Запас','Доступно','IM','MM','Маржа SOX','До IM 50%','Брутто перпов','Чистая Δ / эквити']],
 ['Bybit · направление',['Δ SOX до закрытия США','Δ после закрытия','Δ книги','хедж SOX','Δ купленных опционов']],
 ['P&L',['P&L открытых позиций','P&L счёта · 30 дн']],
 ['Тета и гамма',['Θ книги SOX','Θ /сутки','Θ опц SOX','Γ книги','Гамма ±5% / ±10%','Безубыток / день','Ожидание / сутки','RV / IV','Коридор*','США*']],
 ['Цены, бета и зеркало',['SOXL / SOXS','β SOXS/SOXL','SOXL − SOXS','Отклонение от зеркала','Хедж в штуках','Распад пары L·S']],
 ['MEXC',['MEXC']]];
function dkPanCss(){if(document.getElementById('dkpancss'))return;document.head.insertAdjacentHTML('beforeend','<style id="dkpancss">'
 +'#dktiles .dkpg{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:8px;align-items:start}'
 +'#dktiles .pnl{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:6px 10px}'
 +'#dktiles .pnl h4{margin:0 0 2px;font-size:12px;color:#e3b341;font-weight:500}'
 +'#dktiles .pnl .tile.sm{display:flex;justify-content:space-between;align-items:baseline;gap:10px;background:none;border:0;border-top:1px solid #21262d;border-radius:0;padding:3px 0;margin:0;min-width:0}'
 +'#dktiles .pnl .tile.sm .lbl{text-transform:none;letter-spacing:0;font-size:12px;white-space:nowrap}'
 +'#dktiles .pnl .tile.sm .val{font-size:15px;margin:0;text-align:right}'
 +'#dktiles .pnl .tile[title]:hover{background:#1f6feb14}'
 +'#dktiles .tile[title]{cursor:help}</style>')}
function dkPanCtl(box){const c=document.createElement('div');c.style.cssText='margin:0 0 4px;font-size:12px';
 c.innerHTML='<span class="gbtn'+(DKPANELS?'':' on')+'" onclick="dkPanSet(0)">плитки</span><span class="gbtn'+(DKPANELS?' on':'')+'" onclick="dkPanSet(1)">панели</span><span style="color:#8b949e"> · описание — наведи курсор на плитку</span>';
 box.insertBefore(c,box.firstChild)}
function dkPanBuild(box,tl){const used=new Set(),g=document.createElement('div');g.className='dkpg';
 const pick=n=>n.endsWith('*')?Object.keys(tl).filter(k=>k.startsWith(n.slice(0,-1))):(tl[n]?[n]:[]);
 const mk=(name,list)=>{const p=document.createElement('div');p.className='pnl';p.innerHTML='<h4>'+name+'</h4>';
  list.forEach(n=>pick(n).forEach(k=>{if(used.has(k))return;used.add(k);p.appendChild(tl[k])}));if(p.children.length>1)g.appendChild(p)};
 DKPAN.forEach(([n,l])=>mk(n,l));
 const rest=Object.keys(tl).filter(k=>!used.has(k));if(rest.length)mk('Прочее',rest);
 g.querySelectorAll('.val div').forEach(d=>{if(/^эквити −/.test(d.textContent))d.style.display='none'});   // чистые описания — уже в подсказке
 box.innerHTML='';box.appendChild(g)}
function dkFav(){const box=$('dktiles');if(!box)return;const tl={};
 box.querySelectorAll('.tile').forEach(t=>{const l=t.querySelector('.lbl');if(l)tl[l.textContent.trim()]=t});
 const row=document.createElement('div');row.className='row';row.style.cssText='margin:0 0 3px;gap:4px';
 row.innerHTML='<span style="color:#e3b341;font-size:12px;width:62px;align-self:center">Избранное</span>';
 DKFAV.forEach(n=>{if(tl[n])row.appendChild(tl[n])});if(row.children.length<2)return;
 box.insertBefore(row,box.firstChild);
 const brow=document.createElement('div');brow.className='row';brow.style.cssText='margin:0 0 3px;gap:4px';
 brow.innerHTML='<span style="color:#8b949e;font-size:12px;width:62px;align-self:center">Бета</span>';
 DKBETA.forEach(n=>{if(tl[n])brow.appendChild(tl[n])});
 if(brow.children.length>1){const g=[...box.querySelectorAll(':scope > .row')].find(r=>{const sp=r.querySelector(':scope > span');return sp&&sp.textContent.trim()=='Гамма'});if(g)g.after(brow);else box.appendChild(brow)}
 box.querySelectorAll(':scope > .row').forEach(r=>{const sp=r.querySelector(':scope > span'),k=sp&&sp.textContent.trim();if(k&&DKTAIL[k])DKTAIL[k].forEach(n=>{if(tl[n])r.appendChild(tl[n])});
  if(sp&&sp.style.width=='36px')sp.style.width='62px';
  if(r!==row&&!r.querySelector('.tile'))r.style.display='none'});
 dkPanCss();Object.keys(tl).forEach(n=>{const t=dkTip(n);if(t&&!tl[n].title)tl[n].title=t});   // описание при наведении — в обоих режимах
 if(DKPANELS)dkPanBuild(box,tl);dkPanCtl(box)}
function renderTiles(d){
 try{dkGam(d)}catch(e){}
 $('upd').textContent='деск · '+d.t+(d.tlive?' · плитки '+d.tlive:'');
 const a=d.acct,L=d.lev,th0=d.theta&&d.theta[0]?d.theta[0].tot:0,f0=v=>v==null?'—':(+v).toFixed(0),f2=v=>v==null?'—':(+v).toFixed(2);
 const ic=a.im_pct>=50?'neg':(a.im_pct>=40?'warn':'pos'),rc=a.room<a.eq*0.25?'neg':(a.room<a.eq*0.5?'warn':'pos');
 const T=(l,v,c)=>`<div class="tile sm"><div class="lbl">${l}</div><div class="val ${c||''}">${v}</div></div>`,e=d.eth,s=d.sox,xb=d.xbeta||{},xd=xb.day||{},xh=xb.hour||{};
 const sn=Math.abs(s.eq_usd)<40,en=Math.abs(e.delta)<0.1;
 const ds=d.dsum||{},dp=s.dparts||{};
 $('dktiles').innerHTML='<div class="row" style="margin:0 0 3px;gap:4px"><span style="color:#8b949e;font-size:12px;width:36px;align-self:center">Счёт</span>'+T('Эквити','$'+f0(a.eq))+T('IM',a.im_pct+'%',ic)+T('MM',a.mm_pct+'%',ic)+T('Запас','<span title="Запас = эквити − MM. Столько $ может потерять счёт до ликвидации. MM в Portfolio Margin пересчитывается сценарием (SOXL ±28%, IV), поэтому запас — это сумма при нынешней марже, а не уровень цены. ×к MM = эквити / MM, ликвидация при ×1">$'+f0(a.room)+'<span style="font-size:10px;color:#8b949e"> ×'+(a.mm?(a.eq/a.mm).toFixed(2):'—')+' к MM</span>'+'<div style="font-size:10px;color:#8b949e;font-weight:400;margin-top:2px">эквити − MM · убыток до ликвидации</div></span>',rc)+T('Доступно','<span title="Доступно = эквити − IM. Свободно под новые позиции и на вывод. При 0 Bybit не даст открыть ноги, увеличивающие риск, и вывести деньги, но ликвидации ещё нет: до неё остаётся IM − MM">$'+f0(a.avail)+'<div style="font-size:10px;color:#8b949e;font-weight:400;margin-top:2px">эквити − IM · под новые позиции и вывод</div></span>')+(()=>{if(DKETH||ds.total==null)return '';if(DKVIEW!='B')bldBotTick();const v=ds.total,bq=(BLDBOT&&BLDBOT.bots||[]).reduce((x,b)=>x+(!b.err&&b.base=='SOXL'?b.q:0),0),tip='Δ книги — дельта всего единого счёта в $ на 1 SOXL-эквивалент: SOXL + SOXS по зеркалу от закрытия США (внутри дня SOXS ≈ −SOXL) + опционы. SOXL +1% → книга ≈ '+(v>=0?'+':'−')+'$'+Math.abs(v/100).toFixed(2)+'. Боты — на своих изолированных счетах, в книгу не входят: показаны отдельно';return T('Δ книги','<span title="'+tip+'">$'+f0(v)+'<span style="font-size:10px;color:#8b949e"> '+(v/s.pxL>=0?'+':'')+(v/s.pxL).toFixed(2)+' SOXL'+(bq?' · с ботом $'+f0(v+bq*s.pxL):'')+'</span>'+'<div style="font-size:10px;color:#8b949e;font-weight:400;margin-top:2px">SOXL+1% → '+(v>=0?'+':'−')+'$'+Math.abs(v/100).toFixed(2)+' · хедж от $40</div></span>',Math.abs(v)<40?'pos':'warn')})()+(()=>{let pf=0,po=0;((s&&s.perps)||[]).forEach(x=>pf+=x.upl||0);((s&&s.legs)||[]).forEach(l=>po+=l.upl||0);const bt=(BLDBOT&&BLDBOT.bots||[]).filter(x=>!x.err),pb=bt.reduce((t,x)=>t+(x.pnl||0),0),ct=s&&s.gamma&&s.gamma.pnl?s.gamma.pnl.closed_today:null,tot=pf+po+pb,c=v=>v>0?'pos':(v<0?'neg':''),g=v=>(v>0?'+':'')+v.toFixed(2),N=(n,v)=>'<span style="font-size:10px;color:#8b949e;font-weight:400">'+n+' </span><span class="'+c(v)+'">'+g(v)+'</span>';return T('P&L открытых позиций','<span title="P&L открытых позиций от цены входа: перпы — нереализованный P&L перпов SOXL/SOXS (Bybit), опционы — нереализованный P&L открытых опционов по марку, бот — P&L ботов с запуска (свой счёт), всего — сумма трёх. В подписи — закрытые сделки перпов за сегодня (с 00:00 UTC), в «всего» не входят. Переводы не влияют">'+N('перпы',pf)+' &nbsp;'+N('опционы',po)+' &nbsp;'+N('бот',pb)+' &nbsp;'+N('всего',tot)+' $'+'<div style="font-size:10px;color:#8b949e;font-weight:400;margin-top:2px">от цены входа · бот с запуска'+(ct!=null?' · перпы закрыто сегодня '+g(ct):'')+'</div></span>')})()+(()=>{const W=window;if(!W.DKMXT||Date.now()-W.DKMXT>30000){W.DKMXT=Date.now();fetch('/mexc').then(r=>r.json()).then(j=>{W.DKMX=j}).catch(()=>{})}const X=W.DKMX,sp=t=>'<span style="font-size:10px;color:#8b949e"> '+t+'</span>';if(!X)return T('MEXC',sp('загрузка…'));if(!X.key)return T('MEXC',sp('нет ключа: C:\\Users\\DoOs\\.bybit\\mexc_credentials.json'));if(X.err)return T('MEXC','—'+sp('ошибка: '+X.err),'warn');const P=X.fut_pos||[];return T('MEXC','<span title="MEXC — отдельный счёт (read-only ключ, mexc_credentials.json): спот + фьючерсы. В маржу Bybit не входит. Деньги на споте фьючерсным ботам недоступны — перевод Спот → Фьючерсы в MEXC. Обновление раз в 30 с">$'+(X.eq||0).toFixed(0)+sp('спот $'+(X.spot_usd||0).toFixed(1)+' · фьючерсы $'+(X.fut_usd||0).toFixed(1)+(X.fut_usd?' (своб. $'+(X.fut_avail||0).toFixed(1)+', uPnL '+((X.fut_upl||0)>=0?'+':'')+(X.fut_upl||0).toFixed(2)+')':''))+'<div style="font-size:10px;color:#8b949e;font-weight:400;margin-top:2px">'+(P.length?P.map(p=>p.sym+' '+(p.side=='long'?'лонг':'шорт')+' '+p.vol+' @'+p.avg+(p.liq?' · ликв '+p.liq:'')+' · x'+p.lev).join(' | '):'позиций на фьючерсах нет')+'</div></span>',P.length?'':'')})()+(()=>{const W=window;if(!W.DKINCT||Date.now()-W.DKINCT>60000){W.DKINCT=Date.now();fetch('/income?days=30').then(r=>r.json()).then(j=>{if(j&&j.total!=null)W.DKINC=j}).catch(()=>{})}const I=W.DKINC,c=v=>v>0?'pos':(v<0?'neg':''),g=v=>(v>0?'+':'')+v.toFixed(2),N=(n,v)=>'<span style="font-size:10px;color:#8b949e;font-weight:400">'+n+' </span><span class="'+c(v)+'">'+g(v)+'</span>';if(!I)return T('P&L счёта · 30 дн','<span style="font-size:12px;color:#8b949e">журнал Bybit считается (~1 мин)…</span>');let pf=0;((s&&s.perps)||[]).forEach(x=>pf+=x.upl||0);let jp=0,jo=0;(I.cats||[]).forEach(x=>{if(/^Опционы/.test(x.name))jo+=x.tot;else jp+=x.tot});const bt=(BLDBOT&&BLDBOT.bots||[]).filter(x=>!x.err),pb=bt.reduce((t,x)=>t+(x.pnl||0),0),pp=jp+pf,tot=pp+jo+pb+(I.interest||0);return T('P&L счёта · 30 дн','<span title="P&L счёта за 30 дней. Источник — журнал операций Bybit (/v5/account/transaction-log, как вкладка «Доходы»): реализованный P&L перпов, премии и поставка опционов, фандинг; комиссии уже внутри. Перпы = журнал + нереализованный P&L открытых перпов. Опционы = журнал + текущая стоимость открытых опционов (марк × размер). Бот — P&L ботов с запуска (свой счёт). Переводы и пополнения не входят. Опционы, открытые раньше 30 дней, видны частично. Пересчёт раз в 10 мин">'+N('перпы+спот',pp)+' &nbsp;'+N('опционы',jo)+' &nbsp;'+N('бот',pb)+' &nbsp;'+N('всего',tot)+' $'+'<div style="font-size:10px;color:#8b949e;font-weight:400;margin-top:2px">комиссии '+g(-I.fees)+' · фандинг '+g(-I.funding)+(I.liq?' · ликвидации '+g(I.liq):'')+' (внутри) · переводы '+g(I.deposits)+' не входят · обновлено '+Math.round((I.age||0)/60)+' мин назад</div></span>',c(tot))})()+(!DKETH?'':T('Δ ETH + SOX','$'+f0(ds.total)+'<span style="font-size:10px;color:#8b949e"> ETH '+f0(ds.eth_usd)+' · SOXL '+f0(dp.L_usd)+' · SOXS '+f0(dp.S_usd)+'</span>',Math.abs(ds.total)<100?'':'warn'))
  +T('Θ /сутки','$'+f2(d.theta_day)+'<span style="font-size:10px;color:#8b949e"> до 10:00 '+f2(th0)+' · фанд '+f2(d.fund_day)+'</span>',d.theta_day<=-10?'neg':(d.theta_day<=-7?'warn':''))
  +(DKETH?'</div><div class="row" style="margin:0 0 3px;gap:4px"><span style="color:#8b949e;font-size:12px;width:36px;align-self:center">ETH</span>'
  +T('ETH','$'+f0(e.px))+T('Δ ETH',sg(e.delta),en?'pos':'warn')+T('хедж ETH',en?'нейтраль':('ETHUSDT '+(e.delta<0?'Buy ':'Sell ')+Math.abs(e.delta).toFixed(2)),en?'pos':'warn')
  +(e.cover?T('Шорт ETHUSD под путы','<span title="Инверс-шорт N контрактов ($1) даёт в $ N·(1−S/S0), на нуле ровно $N. N = Σ q·K голых проданных путов '+'(купленные, истекающие раньше, не считаются) — закрывает хвост на экспирации при любой цене. Апсайд: каждые +10% ETH = −10% от N.">'+(e.cover.inv_long>0?'ETHUSD в ЛОНГЕ $'+f0(e.cover.inv_long)+' — не шорт! ':'')+(e.cover.have>0?'добрать $'+f0(e.cover.add):'$'+f0(e.cover.need))+'<span style="font-size:10px;color:#8b949e"> с '+e.cover.from+' · сейчас $'+f0(e.cover.need_now)+' · ≈'+e.cover.eth+' ETH · +10% → $'+f0(e.cover.up10)+' · есть $'+f0(e.cover.have)+'</span></span>',e.cover.inv_long>0?'neg':(e.cover.have>=e.cover.need?'pos':'warn')):'')
  +T('вега ETH','$'+f2(e.vega),cl(e.vega))+T('Θ ETH','$'+f2(e.theta),cl(e.theta))+T('MM ETH','$'+f0(e.mm)):'')
  +'</div><div class="row" style="margin:0 0 3px;gap:4px"><span style="color:#8b949e;font-size:12px;width:36px;align-self:center">SOX</span>'
  +T('SOXL / SOXS','$'+f2(s.pxL)+' / '+f2(s.pxS))+T('SOXL − SOXS','$'+(s.pxL-s.pxS>0?'+':'')+(s.pxL-s.pxS).toFixed(2)+'<span style="font-size:10px;color:#8b949e"> ×'+(s.pxS?(s.pxL/s.pxS).toFixed(3):'—')+'</span>')+T('Δ SOX до закрытия США','$'+f0(s.eq_usd)+'<span style="font-size:10px;color:#8b949e"> '+(s.dparts&&s.dparts.shares!=null?(s.dparts.shares>0?'+':'')+s.dparts.shares+' SOXL · зеркало':'')+'</span>',sn?'pos':'warn')+T('Δ после закрытия',(s.eq_after==null?'—':'$'+f0(s.eq_after))+'<span style="font-size:10px;color:#8b949e"> база переклеится</span>'+(()=>{const u=s.gamma&&s.gamma.us,t=u&&u.next_close_ts;if(!t)return '';const m=Math.max(0,Math.round((t*1000-Date.now())/60000)),k=new Date(t*1000).toLocaleString('ru-RU',{timeZone:'Europe/Kyiv',weekday:'short',hour:'2-digit',minute:'2-digit'});return '<div style="font-size:10px;color:#e3b341;font-weight:400;margin-top:2px" title="Ребалансировка SOXL/SOXS — на закрытии NYSE 16:00 ET (праздники не учитываются). В этот момент база зеркала переклеивается: Δ до закрытия превращается в Δ после закрытия">ребалансировка фондов: '+k+' Киев · через '+(m>=60?Math.floor(m/60)+' ч ':'')+(m%60)+' мин</div>'})(),s.eq_after!=null&&Math.abs(s.eq_after)>=40?'warn':'')+(()=>{const lg=(s.legs||[]).filter(l=>l.q>0),c=lg.filter(l=>l.cp=='C').reduce((x,l)=>x+(l.d||0),0),p=lg.filter(l=>l.cp=='P').reduce((x,l)=>x+(l.d||0),0),r2=v=>(v>0?'+':'')+v.toFixed(2);if(!lg.length)return '';return T('Δ купленных опционов',r2(c+p)+' SOXL'+'<span style="font-size:10px;color:#8b949e"> $'+f0((c+p)*s.pxL)+' · коллы '+r2(c)+' · путы '+r2(p)+' · без купленных $'+f0(s.eq_usd-(c+p)*s.pxL)+'</span>')})()+T('хедж SOX',sn?'нейтраль':('SOXLUSDT '+(s.hedge>0?'Buy ':'Sell ')+Math.abs(s.hedge).toFixed(2)),sn?'pos':'warn')
  +T('β SOXS/SOXL',s.beta+'<span style="font-size:10px;color:#8b949e"> дни 120 дн (внутри дня зеркало −1) · час 3 дн '+(s.gamma&&s.gamma.rv&&s.gamma.rv.beta_h3!=null?Math.abs(s.gamma.rv.beta_h3).toFixed(2):'—')+'</span>')
  +(!DKETH?'':T('β ETH к SOXL',(xd.eth_on_soxl??'—')+'<span style="font-size:10px;color:#8b949e"> ч '+(xh.eth_on_soxl??'—')+'</span>')+T('корр ETH·SOXL',(xd.corr??'—')+'<span style="font-size:10px;color:#8b949e"> ч '+(xh.corr??'—')+'</span>'))
  +T('Θ опц SOX','$'+f2(s.opt.th),cl(s.opt.th))+(s.L0&&s.S0&&s.pxL&&s.pxS?(()=>{const dv=(s.pxL/s.L0+s.pxS/s.S0-2)*100,Lm=s.L0*(2-s.pxS/s.S0),pr=(s.pxL*s.pxS/(s.L0*s.S0)-1)*100,sp=t=>'<span style="font-size:10px;color:#8b949e"> '+t+'</span>';return T('Отклонение от зеркала','<span title="Отклонение от зеркала = SOXL/L0 + SOXS/S0 − 2, где L0, S0 — цены закрытия США. По мандату фондов внутри дня = 0. Ниже нуля — обе ноги дешевле зеркала (пара лонг+лонг в равных $ на закрытие зарабатывает на возврате), выше — дороже. Обычный размах ~0.07%, в выходные и дни тренда 0.24–0.28%, полувозврат ~22 мин. Урок ОС-13">'+(dv>0?'+':'')+dv.toFixed(2)+'%'+sp('зеркальный SOXL '+Lm.toFixed(2)+' · база '+s.L0+' / '+s.S0)+'</span>',Math.abs(dv)>=0.5?'warn':(Math.abs(dv)>=0.15?'':'pos'))+T('Хедж в штуках','<span title="Хедж внутри дня в штуках: на 1 SOXL — L0/S0 SOXS (равные $ на закрытие США). Весь день постоянный, меняется на закрытии. Урок ОС-13, руны 1–3">'+(s.L0/s.S0).toFixed(3)+sp('SOXS на 1 SOXL · 1 SOXS = '+(s.S0/s.L0).toFixed(3)+' SOXL')+'</span>')+T('Распад пары L·S','<span title="Произведение SOXL·SOXS к закрытию США. Внутри дня падает при ходе индекса в любую сторону: L·S = L0·S0·(1 − 9x²). Это распад пары от волатильности (тета лонг-лонга). Урок ОС-13, руна 4">'+(pr>0?'+':'')+pr.toFixed(2)+'%'+sp('к закрытию США · −9x²')+'</span>',pr<-0.5?'warn':'')})():'')+'</div>'
  +(()=>{const g=s.gamma||{};if(g.gR==null)return '';const sm=t=>'<span style="font-size:10px;color:#8b949e"> '+t+'</span>',gR=g.gR,D0=s.eq_usd,S=s.pxL,st=DKSTEP,rv=g.rv||{},us=g.us||{};
   let cor='—';if(gR>0){const up=S*(1+(st-D0)/gR),dn=S*(1+(-st-D0)/gR);cor='↑ '+up.toFixed(2)+' / ↓ '+dn.toFixed(2)+sm('Sell '+(st/up).toFixed(2)+' / Buy '+(st/dn).toFixed(2));}
   return '<div class="row" style="margin:0 0 3px;gap:4px"><span style="color:#8b949e;font-size:12px;width:36px;align-self:center">Гамма</span>'
    +T('Γ книги','$'+f2(gR/100)+sm('Δ$ на 1% SOXL, только опционы · пара внутри дня линейна, рывок на закрытии ≈ $'+f2(g.g_pair/100)+' на 1% хода дня'),gR>0?'pos':'neg')
    +T('Гамма ±5% / ±10%','$'+f2(g.g5)+' / '+f2(g.g10))
    +T('Θ книги SOX','$'+f2(g.th_book)+sm('опц '+f2(g.th_opt)+' · фанд '+f2(g.th_fund)),cl(g.th_book))
    +T('Безубыток / день',(g.be_pct??'—')+'%'+sm('ход SOXL · RV '+g.rv_day_pct+'%/день'),g.be_pct==null?'':(g.rv_day_pct>=g.be_pct?'pos':'warn'))
    +(()=>{const ok=rv.h30!=null&&g.iv_ref!=null,hi=ok&&rv.h30>g.iv_ref,bad=ok&&((hi&&gR<0)||(!hi&&gR>0));const tip='RV — как SOXL реально ходил за 30 дней (годовых). IV — сколько хода заложено в цены опционов SOXL у денег. RV выше IV: рынок ходит сильнее, чем стоят опционы — выгодно владеть опционами (гамма), продавать невыгодно. RV ниже IV: опционы переоценены — выгодно продавать (тета). Жёлтая — гамма книги стоит против этого';return T('RV / IV','<span title="'+tip+'">'+(rv.h30??'—')+'% / '+(g.iv_ref??'—')+'%'+sm('RV 7д '+rv.h7+' · день 30д '+rv.d30+' · 120д '+rv.d120)+(ok?'<div style="font-size:10px;color:#8b949e;font-weight:400;margin-top:2px">'+(hi?'RV выше: купленные опционы окупаются, проданные теряют':'RV ниже: проданные зарабатывают, купленные тают')+(bad?' · <span class="warn">книга против: Γ '+(gR<0?'отрицательная (продано больше)':'положительная (куплено больше)')+'</span>':' · книга по ветру')+'</div>':'')+'</span>',ok?(bad?'warn':'pos'):'')})()
    +T('Ожидание / сутки','$'+f2(g.exp_day)+sm('при RV 30д: гамма +'+f2(g.exp_gamma_day)+' и тета'),cl(g.exp_day))
    +T('Коридор ±$'+st,cor)
    +T('США: '+(us.state||'—'),us.state=='открыта'?'закрытие через '+us.to_close_h+' ч':'открытие через '+us.to_open_h+' ч'+sm(us.et||''),us.state=='открыта'?'pos':'')
    +'</div>';})()
  +(()=>{const Lv=d.lev||{},M={},st={};(d.margin||[]).forEach(m=>M[m.coin]=m);(e.stress||[]).forEach(x=>st[x.mv]=x.v);
   const sm=t=>'<span style="font-size:10px;color:#8b949e"> '+t+'</span>',flat=m=>{const r=m&&m.row?m.row.find(x=>Math.abs(x[0])<1e-9):null;return r?r[1]:null};
   const rx=a.mm?a.eq/a.mm:null,cut=a.im-0.5*a.eq,NL=[{b:'SOX',usd:s.eq_usd},...(Lv.net||[]).filter(x=>x.b!='SOXL'&&x.b!='SOXS')].sort((p,q)=>Math.abs(q.usd)-Math.abs(p.usd)),nx=a.eq?NL.reduce((t,x)=>t+Math.abs(x.usd),0)/a.eq:0,mE=M['ETH'],mL=M['SOXL'],mX=M['SOXL+SOXS']||mL;
   const s30=st[-0.3]?st[-0.3][2]:null,s50=st[-0.5]?st[-0.5][2]:null;
   return '<div class="row" style="margin:0 0 3px;gap:4px"><span style="color:#8b949e;font-size:12px;width:36px;align-self:center">Плечи</span>'
    +T('До IM 50%',cut>0?'снять IM $'+f0(cut):'ок'+sm('запас IM $'+f0(-cut)),cut>0?'neg':'pos')
    +T('Брутто перпов','$'+f0(Lv.gross)+sm('×'+Lv.gross_x+' эквити'),Lv.gross_x>3?'neg':(Lv.gross_x>2?'warn':''))
    +T('Чистая Δ / эквити','×'+nx.toFixed(2)+sm(NL.slice(0,3).map(x=>x.b+' '+(x.usd>0?'+':'')+f0(x.usd)).join(' · ')),nx>1.5?'neg':(nx>1?'warn':''))
    +(!DKETH?'':T('Проданные путы ETH','$'+f0(Lv.short_puts)+sm('×'+Lv.short_puts_x+' эквити · голых '+Lv.naked_now+' ETH'),Lv.short_puts_x>10?'warn':''))
    +(!DKETH?'':T('ETH −30% / −50%','$'+f0(s30)+' / '+f0(s50)+sm('IV +15, на сегодня'),s50!=null&&-s50>a.eq?'neg':(s30!=null&&-s30>a.room?'warn':'')))
    +(mE&&DKETH?T('Маржа ETH','$'+f0(mE.mm)+sm('худший '+f0(mE.worst)+' при '+(mE.move*100).toFixed(0)+'%/IV '+(mE.iv*100).toFixed(0)+'% · без хода цены '+f0(flat(mE)))):'')
    +(mX?T('Маржа SOX','<span title="MM — поддерживающая маржа Bybit под SOX: SOXL и SOXS считаются по отдельности и не зачитываются. Худший — убыток пары в худшем сценарии Bybit с учётом зеркала (лонг SOXS гасит часть убытка SOXL). Без хода цены — сценарий, где меняется только IV">'+'<span style="font-size:10px;color:#8b949e;font-weight:400">'+'MM </span>$'+f0(mX.mm)+' &nbsp;'+'<span style="font-size:10px;color:#8b949e;font-weight:400">'+'худший </span><span class="'+(mX.worst<0?'neg':'pos')+'">'+f0(mX.worst)+'</span>'+sm('при SOXL '+(mX.move*100).toFixed(0)+'%'+(mL&&mL.iv!=null?'/IV '+(mL.iv*100).toFixed(0)+'%':'')+' · без хода цены '+f0(flat(mL)))+'</span>'):'')
    +(!(Lv.usdt<0||Lv.usdc<0)?'':T('Займ',((Lv.usdt<0||Lv.usdc<0)?'$'+f0(Math.min(0,Lv.usdt)+Math.min(0,Lv.usdc)):'нет')+sm('USDT '+f0(Lv.usdt)+' · USDC '+f0(Lv.usdc)),(Lv.usdt<0||Lv.usdc<0)?'warn':'pos'))
    +'</div>';})();
 try{dkFav()}catch(e){}
 $('dkalerts').innerHTML=(d.alerts||[]).map(x=>`<div style="padding:4px 10px;margin:0 0 4px;border-radius:6px;font-weight:600;background:${x.lvl=='neg'?'rgba(248,81,73,.18)':'rgba(210,153,34,.18)'};border:1px solid ${x.lvl=='neg'?'#f85149':'#d29922'}" class="${x.lvl}">⚠ ${x.txt}</div>`).join('');
}
// плитки раз в 2 с: /desklive (живые позиции, wallet, цены) вливается в последний полный снимок /desk

// ---- блок «Гамма-скальп»: сетка хеджа от Γ книги, учёт по перпам, β/шум, опционы у денег ----
let DKSTEP=40; try{DKSTEP=+(localStorage.getItem('dkstep')||40)}catch(e){}
function dkStep(v){DKSTEP=v;try{localStorage.setItem('dkstep',v)}catch(e){} if(DKD)renderTiles(DKD);}
function dkGam(d){const box=$('dkgam');if(!box)return;const s=d.sox||{},g=s.gamma;if(!g){box.innerHTML='';return}
 if(g.err){box.innerHTML='<span class="neg">гамма: '+g.err+'</span>';return}
 const f2=v=>v==null?'—':(+v).toFixed(2),f0=v=>v==null?'—':(+v).toFixed(0),S=s.pxL,D0=s.eq_usd,gR=g.gR,st=DKSTEP,p=g.pnl||{},rv=g.rv||{};
 let grid='';
 if(gR>0){const rows=[];for(let k=3;k>=-3;k--){if(k==0){rows.push(`<tr style="background:#161b22"><td>сейчас</td><td><b>${f2(S)}</b></td><td>0%</td><td>Δ$ ${D0>0?'+':''}${f0(D0)}</td><td></td></tr>`);continue}
   const R=(k*st-D0)/gR,Sk=S*(1+R),q=st/Sk;rows.push(`<tr><td>${k>0?'↑ '+k:'↓ '+(-k)}</td><td>${f2(Sk)}</td><td>${R>0?'+':''}${(R*100).toFixed(2)}%</td><td>Δ$ ${k>0?'+':'−'}${Math.abs(k)*st}</td><td class="${k>0?'neg':'pos'}">SOXLUSDT ${k>0?'Sell':'Buy'} ${q.toFixed(2)}</td></tr>`)}
  grid='<table style="display:inline-table;margin-right:24px;vertical-align:top"><tr><th>уровень</th><th>SOXL</th><th>ход</th><th>дельта книги</th><th>ордер (вернуть Δ к 0)</th></tr>'+rows.join('')+'</table>';}
 else grid='<span class="warn">Γ книги ≤ 0 — короткая гамма, сетки покупки дёшево / продажи дорого нет.</span>';
 const steps=[20,40,80,150].map(v=>`<span class="gbtn ${st==v?'on':''}" onclick="dkStep(${v})">$${v}</span>`).join('');
 const acc='<table style="display:inline-table;vertical-align:top"><tr><th>Учёт SOX</th><th>сегодня (UTC)</th><th>7 дней</th></tr>'
  +`<tr><td>закрыто перпами (closed PnL Bybit, после комиссий)</td><td class="${cl(p.closed_today)}">$${f2(p.closed_today)}</td><td class="${cl(p.closed_7d)}">$${f2(p.closed_7d)} · ${p.n_close_7d??'—'} закр.</td></tr>`
  +`<tr><td>сделок перпами</td><td>${p.n_trade_today??'—'}</td><td>${p.n_trade_7d??'—'}</td></tr>`
  +`<tr><td>комиссии (уже внутри closed PnL)</td><td>$${f2(p.fee_today)}</td><td>$${f2(p.fee_7d)}</td></tr>`
  +`<tr><td>фандинг</td><td></td><td class="${cl(p.fund_7d)}">$${f2(p.fund_7d)}</td></tr>`
  +`<tr><td>тета опционов SOX (Bybit; ориентир)</td><td class="neg">$${f2(g.th_opt)}</td><td class="neg">≈ $${f2(g.th_opt*7)}</td></tr>`
  +`<tr><td colspan="3" style="color:#8b949e">пара перпов внутри дня линейна (зеркало), отдельной теты у неё нет: в пиле теряет, в тренде через закрытия зарабатывает</td></tr></table>`;
 const ch=g.chain||[],best=ch.reduce((m,c)=>c.g_per_th>(m?m.g_per_th:-1)?c:m,null);
 const chain='<table style="margin-top:6px"><tr><th>экспирация</th><th>дней</th><th>страйк у денег</th><th>IV</th><th>стрэддл, $</th><th>спред бид/аск</th><th>Δ$ стрэддла на 1% SOXL</th><th>Θ стрэддла /сут</th><th>Γ на $1 теты</th></tr>'
  +ch.map(c=>`<tr${best&&c.exp==best.exp?' style="background:#1f6feb1a"':''}><td>${c.exp}</td><td>${c.days}</td><td>${c.K}</td><td>${c.iv}%</td><td>${f2(c.straddle)}</td><td class="${c.spr_pct>12?'warn':''}">${c.spr_pct}%</td><td>${(c.g1*100).toFixed(3)}</td><td class="neg">${c.th}</td><td><b>${c.g_per_th}</b></td></tr>`).join('')+'</table>';
 const us=g.us||{};
 box.innerHTML=`<div style="margin-bottom:4px">Шаг коридора: ${steps} <span style="color:#8b949e">— уровни, где дельта книги дойдёт до ±шага при Γ книги $${f2(gR/100)} на 1% SOXL; ордер на уровне возвращает Δ к нулю (это и есть скальп гаммы). Гамма считается постоянной — на крупных ходах уровни сдвигаются.</span></div>`
  +grid+acc
  +`<div style="margin:6px 0 2px;color:#c9d1d9">β SOXS/SOXL: 120 дн ${s.beta} · по часам 3 дня ${rv.beta_h3!=null?Math.abs(rv.beta_h3).toFixed(2):'—'} · <b>шум хеджа через SOXS ±$${f2(g.resid_day)}/сутки</b> (1σ: SOXS против β·SOXL на твои ${g.qS} SOXS) · сессия США: <b>${us.state||'—'}</b> ${us.et||''}${us.state=='открыта'?' · закрытие через '+us.to_close_h+' ч':' · открытие через '+us.to_open_h+' ч'} — на открытии перпы прыгают гэпом</div>`
  +`<div style="color:#8b949e">Опционы SOXL у денег — где гамма дешевле. «Γ на $1 теты» ∝ 1/IV²: чем больше, тем больше гаммы на каждый доллар распада${best?'; сейчас лучше всего <b style="color:#58a6ff">'+best.exp+'</b>':''}. Спред — цена входа и выхода (бид/аск, % от mark).</div>`+chain;}
let DKD=null,DLB=0,TDX=[];
// ===================== КОНСТРУКТОР ПОЗИЦИИ (вкладка деска «Конструктор») =====================
// Данные: /builder (ноги Bybit с бид/аском и IV, цепочка SOXL, перпы Bybit и MEXC, база зеркала, β, RV, комиссии), /bkline (свечи индекса).
// Расчёты (P&L, греки, вероятность, калькуляторы) — в браузере. Только чтение: ордера не ставятся.
let DKVIEW='D'; try{DKVIEW=localStorage.getItem('dkview')||'D'}catch(e){}
let BLD=null,BLDQ={},BLDOFF={},BLDADD=[],BLDDAY=0,BLDLO=0.4,BLDHI=1.8,BLDKIV='D',BLDK=null,BLDBUSY=0,BLDH=null;
function dkView(v){DKVIEW=v;try{localStorage.setItem('dkview',v)}catch(e){}
 const m=$('dkmain'),b=$('dkbuild');if(m)m.style.display=v=='D'?'':'none';if(b)b.style.display=v=='B'?'':'none';
 ['D','B'].forEach(x=>{const e=$('dksw'+x);if(e)e.className='gbtn'+(x==v?' on':'')});
 bldCompactApply();if(v=='B'){bldTick();bldKline();}}
async function bldTick(){if(TAB!='D'||DKVIEW!='B'||BLDBUSY)return;BLDBUSY=1;
 try{const d=await (await fetch('/builder')).json();if(d.err){$('bldtop').innerHTML='<span class="neg">ошибка: '+d.err+'</span>';return}BLD=d;bldRender();bldBotTick();}
 catch(e){} finally{BLDBUSY=0}}
async function bldKline(){if(DKVIEW!='B')return;try{BLDK=await (await fetch('/bkline?iv='+BLDKIV)).json();bldCandles();}catch(e){}}
const bf0=v=>v==null||isNaN(v)?'—':(+v).toFixed(0),bf2=v=>v==null||isNaN(v)?'—':(+v).toFixed(2),bf4=v=>v==null||isNaN(v)?'—':(+v).toFixed(4),
 bsg=v=>v==null||isNaN(v)?'—':((v>0?'+':'')+(+v).toFixed(2)),bcl=v=>v>0?'pos':(v<0?'neg':''),bpct=v=>v==null||isNaN(v)?'—':(v*100).toFixed(1)+'%';
function bN(x){const t=1/(1+0.2316419*Math.abs(x)),d=0.3989423*Math.exp(-x*x/2),p=d*t*(0.3193815+t*(-0.3565638+t*(1.781478+t*(-1.821256+t*1.330274))));return x>0?1-p:p}
function bBS(S,K,T,v,cp){if(T<=0||v<=0||S<=0)return Math.max(0,cp=='C'?S-K:K-S);const sd=v*Math.sqrt(T),d1=(Math.log(S/K)+0.5*v*v*T)/sd,d2=d1-sd;return cp=='C'?S*bN(d1)-K*bN(d2):K*bN(-d2)-S*bN(-d1)}
const YR=31557600;
function bLnow(){return BLD?(BLD.perp.SOXL.mark||BLD.perp.SOXL.index):0}
function bSnow(){return BLD?(BLD.perp.SOXS.mark||BLD.perp.SOXS.index):0}
// цена SOXS как функция цены SOXL: в момент «сейчас» — зеркало от закрытия США; через ≥1 день — степень β (многодневная связь)
function bSoxs(S,t){const L=bLnow(),Sn=bSnow();if(!BLD||!L)return Sn;
 if(t-BLD.now<6*3600&&BLD.L0&&BLD.S0)return Math.max(0,BLD.S0*(2-S/BLD.L0));
 return Sn*Math.pow(S/L,BLD.beta||-0.94)}
function bldLegs(){if(!BLD)return[];const out=[];
 BLD.legs.forEach(l=>{const id=l.sym;out.push(Object.assign({},l,{id,sim:false,on:!BLDOFF[id],qq:(BLDQ[id]!=null?BLDQ[id]:l.q)}))});
 BLDADD.forEach((a,i)=>{const id='add'+i;let base;
  if(a.kind=='opt'){const c=BLD.chain.find(c=>c.sym==a.sym)||{};base=Object.assign({},c,{kind:'opt',base:'SOXL',sym:a.sym,avg:(a.q>0?(c.ask||c.mark):(c.bid||c.mark)),upl:0})}
  else{const p=BLD.perp[a.sym.slice(0,4)]||{};base=Object.assign({},p,{kind:'perp',sym:a.sym,base:a.sym.slice(0,4),avg:(a.q>0?p.ask:p.bid)||p.mark,upl:0})}
  out.push(Object.assign(base,{id,sim:true,on:!BLDOFF[id],q:a.q,qq:(BLDQ[id]!=null?BLDQ[id]:a.q)}))});
 return out}
function bLots(l){if(l.sim)return [{q:l.qq,px:l.avg}];const lots=[{q:l.q,px:l.avg}],dq=l.qq-l.q;
 if(Math.abs(dq)>1e-9)lots.push({q:dq,px:(dq>0?(l.ask||l.mark):(l.bid||l.mark))});return lots}
function bVal(l,S,t){if(l.kind=='opt')return bBS(S,l.K,(l.ets-t)/YR,l.markIv||0.0001,l.cp);if(l.base=='SOXS')return bSoxs(S,t);return S}
function bPnl(legs,S,t){let v=0;legs.forEach(l=>{if(!l.on)return;const x=bVal(l,S,t);bLots(l).forEach(o=>v+=o.q*(x-o.px))});return v}
function bGreeks(legs){const L=bLnow(),S=bSnow(),r={dL:0,dMir:0,dAft:0,g:0,v:0,th:0,pnl:0,dLC:0,lc:[],dLP:0};
 legs.forEach(l=>{if(!l.on)return;const q=l.qq;
  if(l.kind=='opt'){r.dL+=q*l.d;r.dMir+=q*l.d;r.dAft+=q*l.d;r.g+=q*l.g;r.v+=q*l.v;r.th+=q*l.th;
   if(q>0){if(l.cp=='C')r.dLC+=q*l.d;else r.dLP+=q*l.d;r.lc.push({n:(l.exp||'').replace(/26$/,'')+' '+l.K+l.cp+'×'+q,d:q*l.d})}}   // купленные опционы (стрэнглы/стрэддлы) — отдельной плиткой
  else if(l.base=='SOXL'){r.dL+=q;r.dMir+=q;r.dAft+=q}
  else{const m=(BLD.L0&&BLD.S0)?BLD.S0/BLD.L0:S/L*Math.abs(BLD.beta||0.94);r.dMir-=q*m;r.dAft-=q*S/L}
  r.pnl+=l.sim?(l.mark-l.avg)*q:(l.upl+(l.qq-l.q)*((l.mark||0)-((l.qq-l.q)>0?(l.ask||l.mark):(l.bid||l.mark))))});
 r.gR=r.g*L*L;return r}
function bLCTile(T,sm,G){const L=bLnow(),t=G.dLC+G.dLP;if(!G.lc.length)return T('Δ купленных опционов','нет'+sm('купленных опционов нет'));
 return T('Δ купленных опционов',bsg(t)+' SOXL'+sm('$'+bf0(t*L)+' · коллы '+bsg(G.dLC)+' · путы '+bsg(G.dLP)+' · без купленных '+bsg(G.dMir-t)+' · '+G.lc.map(x=>x.n+' '+bsg(x.d)).join(' · ')),'',
  'Дельта всех купленных опционов (коллы + путы = стрэнглы и стрэддлы), включённые ноги конструктора. Около нуля — купленная часть нейтральна и работает на гамму. «Без купленных» — Δ книги по зеркалу без них')}
function bAtmIv(exp){if(!BLD)return null;const L=bLnow();const c=BLD.chain.filter(c=>(!exp||c.exp==exp)&&c.markIv>0);if(!c.length)return null;
 const K=c.reduce((m,x)=>Math.abs(x.K-L)<Math.abs(m-L)?x.K:m,c[0].K);const v=c.filter(x=>x.K==K).map(x=>x.markIv);return v.reduce((a,b)=>a+b,0)/v.length}
function bExps(){if(!BLD)return[];const m={};BLD.chain.forEach(c=>m[c.exp]=c.ets);return Object.keys(m).sort((a,b)=>m[a]-m[b]).map(e=>({exp:e,ets:m[e]}))}
// ---------- отрисовка ----------
function bldRender(){if(!BLD)return;const legs=bldLegs(),G=bGreeks(legs),L=bLnow(),Sx=bSnow(),a=BLD.acct;
 const T=(l,v,c)=>`<div class="tile sm"><div class="lbl">${l}</div><div class="val ${c||''}">${v}</div></div>`,sm=t=>'<span style="font-size:10px;color:#8b949e"> '+t+'</span>';
 if(BLDCOMPACT)bldTopCompact(G);else{const chg=legs.some(l=>l.sim?l.on:(!l.on||l.qq!=l.q)),hc=Math.abs(G.dMir*L)<40?'pos':'warn',nd=!DKD,sx=nd?'':' с изменениями';   // полный вид: шапка деска видна, здесь только то, чего в ней нет
  $('bldtop').innerHTML=(nd?T('Эквити','$'+bf2(a.eq)+sm('шапка деска грузится'))+T('IM / MM',a.im_pct+'% / '+a.mm_pct+'%',a.im_pct>=50?'neg':(a.im_pct>=40?'warn':'pos')):'')+T('MEXC SOXL / SOXS',bf2(BLD.mexc.SOXL.last)+' / '+bf2(BLD.mexc.SOXS.last)+sm('Bybit '+bf2(L)+' / '+bf2(Sx)))
  +T('Δ только SOXL (как Bybit)',bsg(G.dL)+sm('$'+bf0(G.dL*L)))+bLCTile(T,sm,G)
  +(chg||nd?T('Δ'+sx+' (зеркало)',bsg(G.dMir)+sm('$'+bf0(G.dMir*L)+' · после закрытия '+bsg(G.dAft)),hc)
   +T('Хедж'+sx,Math.abs(G.dMir*L)<40?'нейтраль'+sm('|Δ| < $40'):('SOXLUSDT '+(G.dMir>0?'Sell ':'Buy ')+Math.abs(G.dMir).toFixed(2)),hc):'')
  +T('Γ / Вега / Θ',bf4(G.g)+' / '+bf2(G.v)+' / '+bf2(G.th)+sm('Δ$ на 1%: '+bf2(G.gR/100)),cl(G.th))+(()=>{const pu=f=>legs.filter(l=>l.on&&f(l)).reduce((x,l)=>x+(l.sim?(l.mark-l.avg)*l.qq:l.upl),0),pp=pu(l=>l.kind!='opt'),po=pu(l=>l.kind=='opt'),N=(n,v)=>'<span style="font-size:10px;color:#8b949e;font-weight:400">'+n+' </span><span class="'+bcl(v)+'">'+bsg(v)+'</span>';return T('P&L ног','<span title="Нереализованный P&L всех включённых ног таблицы (перпы + опционы) от средней цены входа — то же, что Unrealized P&L у позиций Bybit. Не входят: закрытые сделки, фандинг, комиссии, бот. Это не P&L за сегодня, а с момента открытия каждой ноги. Для симуляции: изменённый объём считается по бид/аск, добавленная нога — от цены добавления">'+N('перпы',pp)+' &nbsp;'+N('опционы',po)+' &nbsp;'+N('всего',G.pnl)+' $'+'<div style="font-size:10px;color:#8b949e;font-weight:400;margin-top:2px">нереализованный, от цены входа</div></span>')})()}
 bldMM();bldTable(legs,G);bldAddForm();bldPnl(legs);bldParams(legs,G);bldArbInit();bldArbCalc(G);bldNoArbInit();bldNoArbCalc(G);bldThInit();bldThCalc(G);bldQuests();bldBots();
 $('bldupd').textContent='обновлено '+BLD.t+' · ноги и цены Bybit/MEXC раз в 5 с';}
function bldMM(){const m=BLD.mm;if(!m||m.cov==null){$('bldmm').innerHTML='';return}
 const kv=t=>new Date(t*1000).toLocaleTimeString('ru-RU',{timeZone:'Europe/Kyiv',hour:'2-digit',minute:'2-digit'}),
  et=t=>new Date(t*1000).toLocaleTimeString('en-US',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',hour12:false}),
  dd=t=>new Date(t*1000).toLocaleDateString('ru-RU',{timeZone:'Europe/Kyiv',day:'2-digit',month:'2-digit'}),
  mins=s=>Math.round((Date.now()/1000-s)/60),cov=Math.round(m.cov*100);
 const ep=(m.episodes||[]).map(e=>`<tr><td>${dd(e.start)}</td><td>${kv(e.start)} <span style="color:#8b949e">(${et(e.start)} NY)</span></td><td>${e.end?kv(e.end)+' <span style="color:#8b949e">('+et(e.end)+' NY)</span>':(e.lost?'<span style="color:#8b949e">не знаю — сервер перезапускался</span>':'<span class="neg">идёт</span>')}</td><td>${e.min==null?'—':e.min}</td></tr>`).join('');
 const log=`<details style="font-size:12px;margin-top:2px"><summary style="color:#8b949e;cursor:pointer">Журнал уходов маркетмейкера (${(m.episodes||[]).length}) · файл data/mm_quotes.log</summary>`
  +(ep?`<table style="font-size:12px"><tr><th>Дата</th><th>Ушёл (Киев)</th><th>Вернулся</th><th>Мин.</th></tr>${ep}</table>`:'<div style="color:#8b949e">пока ни одного ухода не записано</div>')
  +`<div style="color:#8b949e">Покрытие = доля опционов у денег (страйк 0.7…1.4 × SOXL, экспирация дальше суток) с двусторонней котировкой. Норма ~90%. «Ушёл» — ниже ${Math.round(m.off_thr*100)}%, «вернулся» — от ${Math.round(m.on_thr*100)}%. Пишется фоном раз в 15 с, пока работает сервер.</div></details>`;
 if(m.state=='off')$('bldmm').innerHTML=`<div style="background:#3d1d20;border:1px solid #f85149;border-radius:6px;padding:6px 10px"><b class="neg">⚠ Маркетмейкер опционов SOXL ушёл</b> — котировки у ${cov}% опционов у денег (${m.q}/${m.n}), уже ${mins(m.since)} мин (с ${kv(m.since)} Киев / ${et(m.since)} NY). Лимитки стоят без встречной стороны, «по рынку» — только против случайных заявок. Бид/аск и P&L ниже — по пустому стакану.${log}</div>`;
 else $('bldmm').innerHTML=`<div style="font-size:12px;color:#8b949e">Маркетмейкер опционов SOXL на месте: котировки у <span class="${cov<70?'warn':'pos'}">${cov}%</span> опционов у денег (${m.q}/${m.n}).</div>${log}`;}
// ---------- квесты арбитражника (ОС-12) ----------
const BLDQS=[
 {id:'Q1',t:'Дорогая синтетика',h:'H3',w:'калькулятор (а) выше; стакан опционов Bybit',s:'найди страйк с максимальной конверсией/реверсией на 4DEC и ближней неделе; проверь размер на бид/аске',ok:'edge > 0 после комиссий и размер ≥ 1 контракт; повторить 5 раз в разное время'},
 {id:'Q2',t:'Охота за зеркалом',h:'H1',w:'«Параметры спреда» → отклонение от зеркала; калькулятор (г)',s:'16:20–17:00 и 22:40–23:20 Киев, суббота днём — снимок каждые 5 мин',ok:'|отклонение| > 30 бп (z > 3) несколько раз за сессию'},
 {id:'Q3',t:'Bybit против MEXC на открытии',h:'H2',w:'калькулятор (б); стаканы SOXLUSDT и SOXL_USDT',s:'16:30 Киев, 15 минут; скриншоты стаканов в момент максимума',ok:'> 10 бп после комиссий дольше 5 секунд'},
 {id:'Q4',t:'Фандинг-разведка',h:'H4',w:'история фандинга Bybit и MEXC; калькулятор (в)',s:'30 дней ставок SOXL и SOXS на обеих биржах, разница по интервалам',ok:'разница регулярная, ≥ 10% годовых'},
 {id:'Q5',t:'Спот SOXL',h:'H10',w:'Bybit Spot / MEXC Spot / другие биржи (xStocks)',s:'найти спотовый SOXL или SOXS: тикер, биржа, оборот, обеспечение',ok:'оборот ≥ $1 млн в сутки и понятное обеспечение'},
 {id:'Q6',t:'Бокс',h:'H7',w:'цепочка 4DEC',s:'K1 < K2 у денег: бокс = (C K1 − C K2) + (P K2 − P K1) по аскам/бидам, сравнить с K2 − K1',ok:'бокс дешевле K2 − K1 после 4 комиссий'},
 {id:'Q7',t:'Тета выходных',h:'H5',w:'IV ATM 4DEC, стрэддл у денег',s:'пятница 22:30 Киев и понедельник 10:00 — IV, цена стрэддла, ход SOXL',ok:'4 выходных подряд стрэддл теряет больше, чем дал ход'},
 {id:'Q8',t:'События и кривая IV',h:'H6',w:'IV по экспирациям',s:'за 3 дня до отчёта NVDA/AVGO/MU или CPI, в день и через день — перекос ближняя − 4DEC',ok:'перекос растёт до события и схлопывается после'},
 {id:'Q9',t:'Корзина против индекса',h:'H8',w:'перпы SOXX, NVDA, AVGO, TSM, MU, INTC на Bybit',s:'корзина с весами индекса, остаток «SOXX − корзина» за 30 дней',ok:'остаток возвращается быстрее 3 дней, σ > двух кругов комиссий'},
 {id:'Q10',t:'Глубина и размер',h:'H2',w:'стаканы SOXL на Bybit и MEXC',s:'объём в $ в ±0.05% и ±0.1% от середины в 16:30 и 03:00 Киев',ok:'понятен максимальный размер межбиржевой сделки'},
 {id:'Q11',t:'Тренд через закрытие',h:'H9',w:'свечи индекса SOXL (Д) выше',s:'30 дней: «трендовые» дни (закрытие у экстремума) против прочих, ход следующего дня',ok:'после трендовых дней продолжение чаще 55%'},
 {id:'Q12',t:'Часы без маркетмейкера',h:'H11',w:'плашка маркетмейкера вверху вкладки и её журнал',s:'неделю держать сервер; эпизоды сопоставить с открытием/закрытием США, выходными, отчётами',ok:'уходы повторяются в одни и те же часы'}];
function bQny(){const p=new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',weekday:'short',hour:'numeric',minute:'numeric',hour12:false}).formatToParts(new Date()),g=t=>(p.find(x=>x.type==t)||{}).value;
 return {d:g('weekday'),m:(+g('hour')%24)*60+(+g('minute'))}}
function bQStore(k,v){try{if(v===undefined)return JSON.parse(localStorage.getItem(k)||'null');localStorage.setItem(k,JSON.stringify(v))}catch(e){return null}}
function bQLive(){const b=BLD,L=bLnow(),mL=b.mexc.SOXL||{},mS=b.mexc.SOXS||{},pL=b.perp.SOXL||{},pS=b.perp.SOXS||{},R={},N=x=>x!=null&&isFinite(x)&&x!==0;
 const f2=x=>N(x)?(+x).toFixed(2):'—',f1=x=>x==null||!isFinite(x)?'—':(+x).toFixed(1),mmOff=b.mm&&b.mm.state=='off';
 const ex=bExps().filter(e=>e.ets-b.now>2*86400),by={};BLD.chain.forEach(c=>{(by[c.exp]=by[c.exp]||{});(by[c.exp][c.K]=by[c.exp][c.K]||{})[c.cp]=c});
 const of=b.fees&&b.fees.option?b.fees.option.taker:0.0003,leg=p=>Math.min(of*L,0.07*p);
 // Q1: конверсия/реверсия до комиссий против перпа Bybit
 let q1=null;ex.forEach(e=>Object.keys(by[e.exp]).forEach(k=>{const o=by[e.exp][k],C=o.C,P=o.P,K=+k;if(!C||!P)return;
  if(C.bid>0&&P.ask>0&&pL.ask>0){const v=K+C.bid-P.ask-pL.ask-leg(C.bid)-leg(P.ask);if(!q1||v>q1.v)q1={v,t:'конверсия',e:e.exp,K}}
  if(C.ask>0&&P.bid>0&&pL.bid>0){const v=pL.bid-(K+C.ask-P.bid)-leg(C.ask)-leg(P.bid);if(!q1||v>q1.v)q1={v,t:'реверсия',e:e.exp,K}}}));
 R.Q1=mmOff?'маркетмейкер ушёл — котировок нет':(q1?`лучшая ${q1.t} ${q1.e} K${q1.K}: <b class="${q1.v>0?'pos':'neg'}">${q1.v>=0?'+':''}${f2(q1.v)} $</b>/акц. после комиссий опционов (без перпа и фандинга)`:'нет пар колл/пут с котировками');
 // Q2: зеркало
 const dev=(S,Lp)=>(b.L0&&b.S0&&S&&Lp)?((S/b.S0-1)+(Lp/b.L0-1))*1e4:null,dB=dev(bSnow(),L),dM=dev(mS.last,mL.last);
 R.Q2=`отклонение <b class="${dB!=null&&Math.abs(dB)>30?'warn':''}">${f1(dB)} бп</b> Bybit · ${f1(dM)} бп MEXC (z ≈ ${dB==null?'—':(dB/10).toFixed(1)})`;
 // Q3: Bybit против MEXC
 const a=N(mL.bid)&&N(pL.ask)?(mL.bid-pL.ask)/L*1e4:null,c=N(pL.bid)&&N(mL.ask)?(pL.bid-mL.ask)/L*1e4:null;
 R.Q3=`купить Bybit → продать MEXC ${f1(a)} бп · купить MEXC → продать Bybit ${f1(c)} бп (до комиссий)<br><span style="color:#8b949e">котировки бирж сняты с разницей до 5 с — на быстром рынке это даёт ложные 10–30 бп; всё, что больше 10 бп, проверяй в двух стаканах одновременно</span>`;
 // Q4: фандинг
 const f8=x=>x==null?null:x*(8/(pL.fundH||8));const dF=(N(pL.funding)||pL.funding===0)&&mL.funding!=null?f8(pL.funding)-mL.funding:null;
 R.Q4=`SOXL Bybit ${pL.funding==null?'—':(f8(pL.funding)*100).toFixed(4)+'%'}/8ч · MEXC ${mL.funding==null?'—':(mL.funding*100).toFixed(4)+'%'}/8ч · разница ${dF==null?'—':(dF*100).toFixed(4)+'% ('+(dF*3*365*100).toFixed(1)+'% год.)'}`;
 R.Q5='живых данных нет — ищется руками';
 // Q6: бокс на соседних страйках у денег
 let q6=null;ex.forEach(e=>{const ks=Object.keys(by[e.exp]).map(Number).filter(k=>k>=0.8*L&&k<=1.25*L).sort((x,y)=>x-y);
  for(let i=0;i+1<ks.length;i++){const o1=by[e.exp][ks[i]],o2=by[e.exp][ks[i+1]];if(!o1.C||!o1.P||!o2.C||!o2.P)continue;const W=ks[i+1]-ks[i];
   if(o1.C.ask>0&&o2.C.bid>0&&o2.P.ask>0&&o1.P.bid>0){const v=W-(o1.C.ask-o2.C.bid+o2.P.ask-o1.P.bid)-leg(o1.C.ask)-leg(o2.C.bid)-leg(o2.P.ask)-leg(o1.P.bid);if(!q6||v>q6.v)q6={v,t:'купить',e:e.exp,k:ks[i]+'/'+ks[i+1]}}
   if(o1.C.bid>0&&o2.C.ask>0&&o2.P.bid>0&&o1.P.ask>0){const v=(o1.C.bid-o2.C.ask+o2.P.bid-o1.P.ask)-W-leg(o1.C.bid)-leg(o2.C.ask)-leg(o2.P.bid)-leg(o1.P.ask);if(!q6||v>q6.v)q6={v,t:'продать',e:e.exp,k:ks[i]+'/'+ks[i+1]}}}});
 R.Q6=mmOff?'маркетмейкер ушёл — котировок нет':(q6?`лучший бокс: ${q6.t} ${q6.e} ${q6.k}: <b class="${q6.v>0?'pos':'neg'}">${q6.v>=0?'+':''}${f2(q6.v)} $</b>/акц. после 4 комиссий`:'нет четырёх котировок');
 // Q7, Q8: IV
 const xs=bExps(),dec=xs.find(e=>e.exp=='4DEC26')||xs[xs.length-1],near=ex[0];
 let st=null;if(dec){const ks=Object.keys(by[dec.exp]||{}).map(Number);const k=ks.sort((x,y)=>Math.abs(x-L)-Math.abs(y-L))[0],o=by[dec.exp][k]||{};if(o.C&&o.P)st={k,v:o.C.mark+o.P.mark}}
 const ivD=dec?bAtmIv(dec.exp):null,ivN=near?bAtmIv(near.exp):null,pc=x=>x==null?'—':(x*100).toFixed(1)+'%';
 R.Q7=`IV ATM ${dec?dec.exp:'—'} ${pc(ivD)} · стрэддл K${st?st.k:'—'} (марк) ${st?f2(st.v)+' $':'—'} · SOXL ${f2(L)}`;
 R.Q8=`ближняя ${near?near.exp:'—'} ${pc(ivN)} − ${dec?dec.exp:'—'} ${pc(ivD)} = <b>${ivN!=null&&ivD!=null?((ivN-ivD)*100).toFixed(1)+' п.п.':'—'}</b>`;
 R.Q9='живых данных нет — перпы корзины смотреть на Bybit';
 R.Q10=`Bybit SOXL лучший бид ${f2(pL.bidSize)} акц. ($${N(pL.bidSize)?Math.round(pL.bidSize*L):'—'}) · аск ${f2(pL.askSize)} акц. — глубину глубже смотреть в стакане`;
 R.Q11='свечи «Д» выше; живого счёта нет';
 const m=b.mm||{};R.Q12=m.cov==null?'—':`покрытие котировок ${Math.round(m.cov*100)}% (${m.q}/${m.n}) — ${m.state=='off'?'<b class="neg">ушёл</b>':'на месте'} · эпизодов в журнале: ${(m.episodes||[]).length}`;
 return R}
function bQNow(id){const t=bQny(),wd=!['Sat','Sun'].includes(t.d),m=t.m,off=BLD.mm&&BLD.mm.state=='off';
 if(id=='Q2')return (wd&&((m>=560&&m<=630)||(m>=940&&m<=990)))||t.d=='Sat';
 if(id=='Q3')return wd&&m>=565&&m<=600;
 if(id=='Q7')return (t.d=='Fri'&&m>=900)||(t.d=='Mon'&&m<570);
 if(id=='Q10')return wd&&((m>=570&&m<=600)||(m>=1200&&m<=1260));
 if(id=='Q12')return off;
 if(id=='Q1'||id=='Q6')return !off;
 return false}
function bldQuests(){if(!BLD)return;const R=bQLive(),done=bQStore('bldqdone')||{};
 if(!$('bldqlist')){$('bldq').innerHTML=`<div style="font-size:12px;color:#8b949e;margin:2px 0 6px">Квесты из <b>ОС-12</b>: смотреть, считать, записывать — без сделок. «Сейчас» — живые цифры раз в 5 с. <span class="pos">●</span> — квест актуален в это время. «Записать снимок» кладёт цифры в журнал ниже (журнал живёт в этом браузере; кнопкой «копировать» перенеси его в «Журнал квестов» ОС-12).</div>
  <table id="bldqlist" style="font-size:12px"><tr><th></th><th>Квест</th><th>Где и что делать</th><th>Успех</th><th>Сейчас</th><th></th></tr>${BLDQS.map(q=>`<tr><td><input type="checkbox" id="bldqd_${q.id}" onchange="bldQDone('${q.id}',this.checked)"></td><td><b>${q.id}</b> <span id="bldqn_${q.id}"></span><br>${q.t}<br><span style="color:#8b949e">${q.h}</span></td><td><span style="color:#8b949e">Где:</span> ${q.w}<br>${q.s}</td><td>${q.ok}</td><td id="bldql_${q.id}" style="max-width:340px"></td><td><span class="gbtn" onclick="bldQSnap('${q.id}')">записать снимок</span></td></tr>`).join('')}</table>
  <div style="margin:6px 0 2px;font-size:12px"><b>Журнал снимков</b> <span class="gbtn" onclick="bldQCopy()">копировать как Markdown</span> <span class="gbtn" onclick="bldQClear()">очистить</span> <span id="bldqmsg" style="color:#8b949e"></span></div><div id="bldqlog"></div>`;bldQLog()}
 BLDQS.forEach(q=>{$('bldql_'+q.id).innerHTML=R[q.id]||'';$('bldqd_'+q.id).checked=!!done[q.id];$('bldqn_'+q.id).innerHTML=bQNow(q.id)?'<span class="pos" title="актуально сейчас">●</span>':'';
  $('bldqd_'+q.id).closest('tr').style.opacity=done[q.id]?.55:1});}
function bldQDone(id,v){const d=bQStore('bldqdone')||{};d[id]=v;bQStore('bldqdone',d);bldQuests()}
function bldQSnap(id){const R=bQLive(),tx=(R[id]||'').replace(/<[^>]+>/g,''),lg=bQStore('bldqlog')||[];
 lg.push({ts:Date.now(),id,tx});while(lg.length>300)lg.shift();bQStore('bldqlog',lg);bldQLog();$('bldqmsg').textContent='записано: '+id}
function bQts(ts){const d=new Date(ts);return d.toLocaleDateString('ru-RU',{timeZone:'Europe/Kyiv',day:'2-digit',month:'2-digit'})+' '+d.toLocaleTimeString('ru-RU',{timeZone:'Europe/Kyiv',hour:'2-digit',minute:'2-digit'})+' Киев / '+d.toLocaleTimeString('en-US',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',hour12:false})+' NY'}
function bldQLog(){const lg=(bQStore('bldqlog')||[]).slice(-40).reverse();
 $('bldqlog').innerHTML=lg.length?'<table style="font-size:12px"><tr><th>Время</th><th>Квест</th><th>Цифры</th></tr>'+lg.map(r=>`<tr><td>${bQts(r.ts)}</td><td>${r.id}</td><td>${r.tx}</td></tr>`).join('')+'</table>':'<div style="font-size:12px;color:#8b949e">пока пусто</div>'}
function bldQCopy(){const lg=bQStore('bldqlog')||[],md=lg.map(r=>`| ${bQts(r.ts)} | ${r.id} | ${r.tx.replace(/\|/g,'/')} | |`).join('\n');
 const ok=()=>{$('bldqmsg').textContent='скопировано строк: '+lg.length+' — вставь в «Журнал квестов» ОС-12'};
 if(navigator.clipboard)navigator.clipboard.writeText(md).then(ok,()=>{$('bldqmsg').textContent='буфер недоступен'});else $('bldqmsg').textContent='буфер недоступен'}
function bldQClear(){if(!confirm('Очистить журнал снимков квестов в этом браузере?'))return;bQStore('bldqlog',[]);bldQLog();$('bldqmsg').textContent='очищено'}
// ---- (Е) калькулятор теты: на сколько изменится тета книги от новых ног и как она растёт к экспирации (17.11) ----
function bThBS(S,K,T,v){if(T<=0||v<=0||S<=0)return 0;const sd=v*Math.sqrt(T),d1=(Math.log(S/K)+0.5*v*v*T)/sd;return -S*v*Math.exp(-d1*d1/2)/2.5066283/(2*Math.sqrt(T))/365.25}
function bldThInit(){if($('bth1q'))return;const ex=bExps(),L=bLnow();
 const def=(ex.find(e=>e.exp=='4DEC26')||ex.find(e=>e.ets-BLD.now>14*86400)||ex[ex.length-1]||{}).exp;
 const eo=ex.map(e=>`<option ${e.exp==def?'selected':''}>${e.exp}</option>`).join('');
 const leg=(i,sd,cp,K,q)=>`<div style="margin:2px 0">нога ${i}: <select id="bth${i}s" onchange="bldThCalc()"><option value="1" ${sd>0?'selected':''}>купить</option><option value="-1" ${sd<0?'selected':''}>продать</option></select>
  <select id="bth${i}c" onchange="bldThCalc()"><option ${cp=='P'?'selected':''}>P</option><option ${cp=='C'?'selected':''}>C</option></select>
  экспирация <select id="bth${i}e" onchange="bldThCalc()">${eo}</select> страйк <input id="bth${i}k" value="${K}" style="width:50px" onchange="bldThCalc()">
  кол-во <input id="bth${i}q" value="${q}" style="width:40px" onchange="bldThCalc()"> цена <input id="bth${i}p" value="" placeholder="аск/бид" style="width:60px" onchange="bldThCalc()"></div>`;
 $('bldth').innerHTML='<div style="font-size:12px">'+leg(1,1,'P',Math.round(L),9)+leg(2,-1,'C',Math.round(L)+3,9)
  +'<span style="color:#8b949e">Кол-во 0 — нога выключена. Пустая цена — текущий аск (покупка) или бид (продажа); впиши цену своей лимитки. Страйк — ближайший в цепочке.</span></div><div id="bthout"></div>'}
function bldThCalc(G){if(!BLD||!$('bth1q'))return;G=G||bGreeks(bldLegs());const b=BLD,L=bLnow(),eq=b.acct.eq,now=b.now,DAY=86400;
 const nl=[];for(const i of [1,2]){const q=Math.abs(+$('bth'+i+'q').value),sd=+$('bth'+i+'s').value,cp=$('bth'+i+'c').value,e=$('bth'+i+'e').value,K=+$('bth'+i+'k').value;
  if(!q)continue;const ch=b.chain.filter(c=>c.exp==e&&c.cp==cp);if(!ch.length)continue;
  const c=ch.reduce((m,x)=>Math.abs(x.K-K)<Math.abs(m.K-K)?x:m,ch[0]),pin=$('bth'+i+'p').value;
  nl.push({c,n:sd*q,px:pin!==''?+pin:(sd>0?(c.ask||c.mark):(c.bid||c.mark))})}
 if(!nl.length){$('bthout').innerHTML='<span class="warn">нет ног: проверь кол-во и экспирацию</span>';return}
 const bl=bldLegs().filter(l=>l.on);let fund=0;
 bl.forEach(l=>{if(l.kind!='opt')fund-=l.qq*(l.mark||0)*(l.funding||0)*24/(l.fundH||8)});
 const bopt=bl.filter(l=>l.kind=='opt'&&l.ets>now);
 let sTh=0,sD=0,sG=0,sV=0,cash=0,edge=0;
 const rows=nl.map(o=>{const c=o.c,n=o.n,th=n*c.th,dU=n*c.d*L;sTh+=th;sD+=dU;sG+=n*c.g*L*L/100;sV+=n*c.v;cash-=n*o.px;edge+=n*(c.mark-o.px);
  return `<tr><td>${n>0?'купить':'продать'} ${Math.abs(n)} × ${c.exp} ${c.K} ${c.cp}</td><td>${bf2(o.px)} (марк ${bf2(c.mark)}, IV ${bpct(c.markIv)})</td><td>${bsg(-n*o.px)} $</td><td>${bsg(dU)}</td><td>${bsg(n*c.g*L*L/100)}</td><td class="${bcl(th)}">${bsg(th)}</td><td>${bsg(n*c.v)}</td></tr>`});
 const th0=G.th+fund,th1=th0+sTh,d0=G.dMir*L,lim=-10,av=b.acct.avail;
 let h=`<table><tr><th>Нога</th><th>Цена</th><th>Премия</th><th>Δ$</th><th>Δ$ на 1% (гамма)</th><th>Θ $/сут</th><th>Вега $ на 1 п.п.</th></tr>${rows.join('')}
  <tr><td><b>Итого новые ноги</b></td><td>к марку ${bsg(edge)} $</td><td class="${av!=null&&-cash>av?'neg':''}">${bsg(cash)} $${av!=null?' (свободно $'+bf0(av)+')':''}</td><td>${bsg(sD)}</td><td>${bsg(sG)}</td><td class="${bcl(sTh)}"><b>${bsg(sTh)}</b></td><td>${bsg(sV)}</td></tr></table>
  <table style="margin-top:4px"><tr><th></th><th>Книга сейчас</th><th>Книга после</th></tr>
  <tr><td>Θ $/сут (опционы + фандинг ${bsg(fund)})</td><td class="${th0<lim?'neg':''}">${bsg(th0)} (${(th0/eq*100).toFixed(1)}% эквити)</td><td class="${th1<lim?'neg':''}"><b>${bsg(th1)}</b> (${(th1/eq*100).toFixed(1)}% эквити)</td></tr>
  <tr><td>Δ$ по зеркалу</td><td>${bsg(d0)}</td><td>${bsg(d0+sD)}</td></tr>
  <tr><td>Δ$ на 1% (гамма)</td><td>${bsg(G.gR/100)}</td><td>${bsg(G.gR/100+sG)}</td></tr></table>`;
 // как тета меняется к экспирации: Блэк–Шоулз, IV каждой ноги как сейчас
 const eMin=Math.min(...nl.map(o=>o.c.ets)),dMax=(eMin-now)/DAY,ms=[0.8,0.9,1,1.1,1.2];
 let hs=[0,1,3,7,14,21,30].filter(x=>x<dMax-0.05);[dMax-3,dMax-1].forEach(x=>{if(x>0.05&&!hs.some(y=>Math.abs(y-x)<0.5))hs.push(x)});hs.sort((a,b2)=>a-b2);
 const thN=(S,t)=>nl.reduce((a,o)=>a+o.n*bThBS(S,o.c.K,(o.c.ets-t)/YR,o.c.markIv),0),
  thB=(S,t)=>bopt.reduce((a,l)=>a+l.qq*bThBS(S,l.K,(l.ets-t)/YR,l.markIv),0),
  val=(S,t)=>nl.reduce((a,o)=>a+o.n*bBS(S,o.c.K,Math.max((o.c.ets-t)/YR,0),o.c.markIv,o.c.cp),0),v0=val(L,now);
 const tr=hs.map(d=>{const t=now+d*DAY,bk=thB(L,t)+thN(L,t)+fund,acc=val(L,t)-v0,dt=new Date(t*1000).toLocaleDateString('ru-RU',{day:'2-digit',month:'2-digit'});
  return `<tr><td>${d<0.05?'сегодня':'+'+d.toFixed(d%1?1:0)+' дн'} · ${dt}${Math.abs(d-(dMax-1))<0.05?' (за 1 день до эксп.)':(Math.abs(d-(dMax-3))<0.05?' (за 3 дня)':'')}</td>`
   +ms.map(m=>{const x=thN(L*m,t);return `<td class="${bcl(x)}">${bsg(x)}</td>`}).join('')
   +`<td class="${bk<lim?'neg':''}">${bsg(bk)}</td><td class="${bcl(acc)}">${bsg(acc)}</td></tr>`}).join('');
 h+=`<h4>Как меняется тета до экспирации (${nl.map(o=>o.c.exp).filter((x,i,a)=>a.indexOf(x)==i).join(', ')}, ${dMax.toFixed(1)} дн)</h4>
  <table><tr><th>Дата</th>${ms.map(m=>`<th>Θ новых ног, SOXL ${m==1?'как сейчас':(m>1?'+':'')+((m-1)*100).toFixed(0)+'%'}<br><span style="font-weight:normal;color:#8b949e">${bf2(L*m)}</span></th>`).join('')}<th>Θ книги, SOXL как сейчас</th><th>Тета новых ног накопленная с сегодня</th></tr>${tr}</table>
  <div style="font-size:11px;color:#8b949e">Θ — $ за календарный день. Будущие значения — модель Блэка–Шоулза, IV каждой ноги замороженная на сегодняшнем марке, без улыбки и без хода IV после событий; сегодняшние греки — Bybit. Θ книги включает купленные/проданные опционы книги (истёкшие выпадают) и фандинг перпов по текущей ставке; красная — хуже лимита −$10. «Накопленная» — сколько новые ноги потеряют (−) или заработают (+) на времени при цене как сейчас. Продажа колла с той же экспирацией гасит тету купленного пута, пока цена у страйков; уходит цена — перевешивает ближняя к деньгам нога.</div>`;
 $('bthout').innerHTML=h}
// ---- Боты Bybit в шапке конструктора (18.11): /bots → desk.bots_snapshot; ID ботов — в C:/Users/DoOs/.bybit/bots.json ----
let BLDBOT=null;
async function bldBotTick(){try{BLDBOT=await (await fetch('/bots')).json();bldBots()}catch(e){}}
function bldBots(){const el=$('bldbots');if(!el)return;const d=BLDBOT;if(!d){el.innerHTML='';return}
 if(d.err){el.innerHTML='<span class="neg">боты: '+d.err+'</span>';return}
 if(!d.bots||!d.bots.length){el.innerHTML='<div style="font-size:12px;color:#8b949e;margin:3px 0">Ботов нет: впиши Bot ID в '+d.file+' — инструкция в 10-DASHBOARD/Боты в конструкторе.md</div>';return}
 const T=(l,v,c,t)=>`<div class="tile sm"${t?' title="'+t+'"':''}><div class="lbl">${l}</div><div class="val ${c||''}">${v}</div></div>`,
  sm=t=>'<span style="font-size:10px;color:#8b949e"> '+t+'</span>',f1=v=>v==null||isNaN(v)?'—':(+v).toFixed(1);
 let h='',dB=0;
 d.bots.forEach(b=>{const lv=(b.signals[0]||['pos'])[0],bc=lv=='neg'?'#f85149':(lv=='warn'?'#d29922':'#3fb950');
  let r=`<div class="tile sm" style="min-width:230px;max-width:420px;border-color:${bc}"><div class="lbl">бот · ${b.name} · ${b.type=='grid'?'сетка':'мартингейл'} ${b.mode||''}${b.lev?' x'+b.lev:''}</div>`
   +`<div style="font-size:12px;font-weight:600;margin-top:2px">${b.signals.map(s=>`<div class="${s[0]}">${s[1]}</div>`).join('')}</div></div>`;
  if(!b.err){
   if(b.base=='SOXL')dB+=b.q;else if(b.base=='SOXS'&&BLD){const L=bLnow(),S=bSnow(),m=(BLD.L0&&BLD.S0)?BLD.S0/BLD.L0:S/L*Math.abs(BLD.beta||0.94);dB-=b.q*m}
   if(BLDCOMPACT){
    r+=b.type=='grid'?T('Диапазон',bf2(b.P)+sm(bf2(b.lo)+'–'+bf2(b.hi)+' · '+(b.zone=='in'?'внутри':(b.zone=='above'?'ВЫШЕ':'НИЖЕ'))+' · засажен '+(b.load*100).toFixed(0)+'%'),b.zone!='in'?'neg':(b.load>=0.8?'warn':'pos'),
      'Засажен — доля сетки, уже набранная в позицию')
     :T('Доборы',b.adds_used+' / '+b.adds+sm('цена '+bf2(b.P)+' · средняя '+bf2(b.avg)),b.adds&&b.adds_used>=b.adds?'neg':(b.load>=0.6?'warn':''));
    r+=T('Позиция / P&L',bsg(b.q)+' '+(b.base||'')+sm('$'+bf0(b.q*b.P)+' · P&L '+bsg(b.pnl)+(b.type=='grid'?' · сетка '+bsg(b.grid_profit)+' · сделок '+b.trades:' · раундов '+b.rounds)),bcl(b.pnl));
    r+=b.liq_full!=null?T('Ликвидация на краю','≈ '+bf2(b.liq_full)+sm('доливка $'+bf2(d.reserve)+' → '+bf2(b.liq_full_after)+' · сейчас '+bf2(b.liq)),b.dist!=null&&b.dist<d.topup_pct?'neg':(b.dist_full<d.warn_pct||(b.dist!=null&&b.dist<d.warn_pct)?'warn':''),
      'Если цена дойдёт до края '+bf2(b.edge)+' и сетка наберётся целиком — худшая ликвидация без доливки (оценка). «Сейчас» — Bybit по текущей позиции, она растёт вместе с позицией')
     :T('Ликвидация',bf2(b.liq)+sm(b.dist!=null?'до неё '+f1(b.dist)+'%':''),b.dist!=null&&b.dist<d.topup_pct?'neg':(b.dist!=null&&b.dist<d.warn_pct?'warn':'pos'));
    r+=T('Маржа бота','$'+bf2(b.eq||b.inv)+sm('свободно '+bf2(b.avail)+' · резерв Funding $'+bf2(d.reserve)));
   }else{
   if(b.type=='grid'){
    r+=T('Цена / диапазон',bf2(b.P)+sm(bf2(b.lo)+'–'+bf2(b.hi)+' · '+(b.zone=='in'?'внутри':(b.zone=='above'?'ВЫШЕ':'НИЖЕ'))),b.zone=='in'?'pos':'neg');
    r+=T('Засажен',(b.load*100).toFixed(0)+'%'+sm('до края '+bf2(b.edge)+': '+f1(Math.abs(b.P-b.edge)/b.P*100)+'%'),b.load>=0.95?'neg':(b.load>=0.8?'warn':''),
     'Доля сетки, которая уже набрана в позицию: лонг-сетка — (верх − цена)/(верх − низ); шорт — зеркально; нейтральная — от цены входа до ближнего края');
   }else{
    r+=T('Цена / средняя',bf2(b.P)+sm('средняя '+bf2(b.avg)));
    r+=T('Доборы',b.adds_used+' / '+b.adds+sm('шаг '+bf2(b.step)+'% · раундов '+b.rounds),b.adds&&b.adds_used>=b.adds?'neg':(b.load>=0.6?'warn':''));
   }
   r+=T('Позиция',bsg(b.q)+' '+(b.base||'')+sm('$'+bf0(b.q*b.P)+(b.Qmax?' · на краю ≈ '+bf2(b.Qmax):'')));
   r+=T('Ликвидация сейчас',bf2(b.liq)+sm(b.dist!=null?'до неё '+f1(b.dist)+'%':''),b.dist!=null&&b.dist<d.topup_pct?'neg':(b.dist!=null&&b.dist<d.warn_pct?'warn':'pos'),
    'Bybit, по текущей позиции. Порог доливки '+d.topup_pct+'%, внимание '+d.warn_pct+'% (bots.json)');
   if(b.liq_full!=null)r+=T('Ликвидация на краю','≈ '+bf2(b.liq_full)+sm('от края '+bf2(b.edge)+': '+f1(b.dist_full)+'%'),b.dist_full<d.warn_pct?'warn':'',
    'Оценка: цена дошла до края диапазона, позиция набрана целиком (вложение×плечо/цена края). Дальше позиция не растёт — это худшая ликвидация бота без доливки');
   r+=T('Доливка $'+bf2(d.reserve),(b.liq_after!=null?'→ '+bf2(b.liq_after):'—')+sm(b.liq_full_after!=null?'на краю → '+bf2(b.liq_full_after):''),'',
    'Куда уйдёт ликвидация, если долить весь свободный USDT с Funding (Invest More / Add margin в карточке бота)');
   r+=T('P&L',bsg(b.pnl)+' $'+sm(b.type=='grid'?'сетка '+bsg(b.grid_profit)+' · сделок '+b.trades+' (24ч '+b.trades24+')':'нереализ. '+bsg(b.upl)+' · раундов '+b.rounds),bcl(b.pnl));
   r+=T('Маржа бота','$'+bf2(b.eq||b.inv)+sm('позиция '+bf2(b.pos_bal)+' · ордера '+bf2(b.ord_bal)+' · своб. '+bf2(b.avail)+(b.funding?' · фандинг '+bsg(b.funding):'')));
}
  }
  h+=`<div class="row" style="margin:3px 0">${r}</div>`});
 if(BLD){const G=bGreeks(bldLegs()),L=bLnow();
  h+=`<div style="font-size:11px;color:#8b949e;margin:0 0 4px">Δ книги с ботами по зеркалу: <b>${bsg(G.dMir+dB)}</b> SOXL ($${bf0((G.dMir+dB)*L)}) · боты ${bsg(dB)} SOXL · боты в таблицу ног и P&L-кривую не входят · обновлено ${d.t}</div>`}
 el.innerHTML=h}
// ---- Компактная шапка конструктора (18.11): три строки «Риск / Книга / Время» вместо шапки деска; кнопка «все плитки» возвращает прежнее ----
let BLDCOMPACT=1;try{BLDCOMPACT=localStorage.getItem('bldcompact')==='0'?0:1}catch(e){}
function bldCompactSet(v){BLDCOMPACT=v?1:0;try{localStorage.setItem('bldcompact',BLDCOMPACT?'1':'0')}catch(e){};bldCompactApply();if(BLD)bldRender()}
function bldCompactApply(){const t=$('dktiles');if(t)t.style.display=(DKVIEW=='B'&&BLDCOMPACT)?'none':'';
 ['1','0'].forEach(x=>{const e=$('bldcm'+x);if(e)e.className='gbtn'+((x=='1')==!!BLDCOMPACT?' on':'')})}
function bldTopCompact(G){const L=bLnow(),Sx=bSnow(),a=BLD.acct,d=DKD,s=d&&d.sox,g=(s&&s.gamma)||{},rv=g.rv||{},us=BLD.us||{};
 const T=(l,v,c,t)=>`<div class="tile sm"${t?' title="'+t+'"':''}><div class="lbl">${l}</div><div class="val ${c||''}">${v}</div></div>`,
  sm=t=>'<span style="font-size:10px;color:#8b949e"> '+t+'</span>',
  R=(n,h)=>'<div class="row" style="margin:0 0 3px;gap:4px;flex-basis:100%"><span style="color:#8b949e;font-size:12px;width:44px;align-self:center">'+n+'</span>'+h+'</div>';
 if(!DKD&&!(window.BLDDKW>Date.now())){window.BLDDKW=Date.now()+10000;dtick().finally(()=>{if(DKD&&BLD)bldRender()})}   // данные деска (маржа, RV, ожидание) — не чаще раза в 10 с
 const room=d&&d.acct&&d.acct.room!=null?d.acct.room:(a.eq-a.mm),rc=room<a.eq*0.25?'neg':(room<a.eq*0.5?'warn':'pos'),ic=a.im_pct>=50?'neg':(a.im_pct>=40?'warn':'pos');
 const M={};((d&&d.margin)||[]).forEach(m=>M[m.coin]=m);const mX=M['SOXL+SOXS']||M['SOXL'],mL=M['SOXL']||{};
 const dU=G.dMir*L,hn=Math.abs(dU)<40,thB=g.th_book!=null?g.th_book:G.th;
 const now=Date.now()/1000,ex={};BLD.legs.forEach(l=>{if(l.kind=='opt'&&l.ets>now)(ex[l.ets]=ex[l.ets]||[]).push(l)});
 const e0=Object.keys(ex).sort((x,y)=>x-y)[0];let exv='—',exc='';
 if(e0){const ls=ex[e0],h=(e0-now)/3600,itm=ls.filter(l=>l.cp=='C'?L>l.K:L<l.K).length;exc=h<24?'warn':'';
  exv=ls[0].exp+sm('через '+(h<48?h.toFixed(1)+' ч':(h/24).toFixed(1)+' дн')+' · ног '+ls.length+' · '+(itm?'в деньгах '+itm:'все вне денег'))}
 $('bldtop').innerHTML=
  R('Риск',T('Эквити','$'+bf2(a.eq),'','Единый торговый счёт; бот на своём счёте и сюда не входит')
   +T('IM','<span class="'+ic+'">'+a.im_pct+'%</span>'+sm('MM '+a.mm_pct+'% · лимит 40/50%'),'','Единый торговый счёт, без бота')
   +T('Запас','$'+bf0(room)+sm('×'+(a.mm?(a.eq/a.mm).toFixed(2):'—')+' к MM'),rc,'Запас = эквити − MM: на сколько $ может упасть эквити до ликвидации счёта. MM в Portfolio Margin пересчитывается сценарием (SOXL ±28%, IV), поэтому это сумма при нынешней марже, а не уровень цены')
   +T('Доступно','$'+bf0(a.avail)+sm('Funding $'+(BLDBOT&&BLDBOT.reserve!=null?bf2(BLDBOT.reserve):'—')),'','Доступно = эквити − IM: свободно под новые позиции, на вывод и перевод в бота. При 0 новые ноги с риском не откроются, но до ликвидации ещё IM − MM; Funding — резерв доливки ботов')
   +(mX?T('Маржа SOX худший','$'+bf0(mX.worst)+sm('при SOXL '+(mX.move*100).toFixed(0)+'%'+(mL.iv!=null?'/IV '+(mL.iv*100).toFixed(0)+'%':'')+' · MM $'+bf0(mX.mm)),'','Худший сценарий Bybit Portfolio Margin по SOX'):'')
   +(()=>{let pf=0,po=0;BLD.legs.forEach(l=>{if(l.kind=='opt')po+=l.upl||0;else pf+=l.upl||0});
     const bt=(BLDBOT&&BLDBOT.bots||[]).filter(x=>!x.err),pb=bt.reduce((t,x)=>t+(x.pnl||0),0),gp=bt.reduce((t,x)=>t+(x.grid_profit||0),0),
      sp=(d&&s&&s.gamma&&s.gamma.pnl)||{},ct=sp.closed_today,tot=pf+po+pb;
     return T('P&L фьючи',bsg(pf)+' $'+sm('открытые'+(ct!=null?' · закрыто сегодня '+bsg(ct):'')),bcl(pf),'Нереализованный P&L открытых перпов SOXL/SOXS от цены входа (Bybit). «Закрыто сегодня» — closed PnL перпов с 00:00 UTC')
      +T('P&L опционы',bsg(po)+' $'+sm('открытые ноги'),bcl(po),'Нереализованный P&L открытых опционов от цены входа по марку Bybit')
      +T('P&L бот',bt.length?bsg(pb)+' $'+sm('сетка '+bsg(gp)):'—',bcl(pb),'P&L ботов с запуска (Bybit): прибыль сетки + нереализованный. Деньги, переведённые в бота, сюда не входят — это не убыток')
      +T('P&L всего',bsg(tot)+' $'+sm('фьючи + опционы + бот'),bcl(tot),'Сумма трёх плиток слева: открытые позиции книги от цены входа плюс P&L ботов. Переводы между счетами не влияют')})())
  +R('Книга',T('Δ по зеркалу',bsg(G.dMir)+' SOXL'+sm('$'+bf0(dU)+' · как Bybit '+bsg(G.dL)),hn?'pos':'warn','SOXL плюс SOXS по зеркалу (внутри дня SOXS ≈ −SOXL от закрытия США). Боты — под строкой ботов')
   +T('Δ после закрытия',bsg(G.dAft)+' SOXL'+sm('$'+bf0(G.dAft*L)+' · база зеркала переклеится'),Math.abs(G.dAft*L)<40?'':'warn','После закрытия США SOXS считается к SOXL по текущим ценам (S/L), а не от прошлого закрытия: дельта пары изменится на эту разницу')
   +bLCTile(T,sm,G)
   +T('Хедж',hn?'нейтраль'+sm('|Δ| < $40'):'SOXLUSDT '+(G.dMir>0?'Sell ':'Buy ')+Math.abs(G.dMir).toFixed(2),hn?'pos':'warn','Порог $40 — как в шапке деска')
   +T('Γ','$'+bf2(G.gR/100)+sm('Δ$ на 1% SOXL'),G.gR>=0?'pos':'neg','На сколько $ меняется дельта при ходе SOXL на 1%. Отрицательная — проданные опционы: ход против тебя ускоряется')
   +T('Θ / сутки','$'+bf2(thB)+sm('опционы '+bf2(G.th)+(g.th_fund!=null?' · фандинг '+bf2(g.th_fund):'')),thB<=-10?'neg':cl(thB),'Лимит −$10 в сутки')
   +T('Вега','$'+bf2(G.v)+sm('на 1 п.п. IV'),cl(G.v),'Сколько $ даёт рост IV на 1 процентный пункт')
   +(g.exp_day!=null?T('Ожидание / сутки','$'+bf2(g.exp_day)+sm('если SOXL ходит как 30 дн: тета '+bsg(thB)+' · гамма '+bsg(g.exp_gamma_day)),cl(g.exp_day),'Сколько книга заработает за сутки, если SOXL будет ходить так же, как последние 30 дней. Плюс — тета приносит больше, чем отнимают ходы рынка (или купленные опционы окупаются ходами). Минус — ходы съедают тету. Смотри, когда добавляешь или закрываешь опционную ногу: знак должен остаться плюсом'):'')
   +(rv.h30!=null?(()=>{const hi=g.iv_ref!=null&&rv.h30>g.iv_ref,bad=(hi&&G.gR<0)||(!hi&&G.gR>0);
     return T('RV / IV',rv.h30+'% / '+(g.iv_ref!=null?g.iv_ref:'—')+'%'+sm(hi?'RV выше: купленные опционы окупаются, проданные теряют':'RV ниже: проданные зарабатывают, купленные тают'),bad?'warn':'pos',
      'RV — как SOXL реально ходил (30 дн), IV — сколько хода заложено в цены опционов у денег. RV выше IV: рынок ходит сильнее, чем стоят опционы — выгодно владеть опционами (гамма), невыгодно их продавать. RV ниже IV: опционы переоценены — выгодно продавать (тета). Жёлтая — книга стоит против этого: гамма книги '+(G.gR<0?'отрицательная (продано больше)':'положительная (куплено больше)'))})():''))
  +R('Цены',T('SOXL / SOXS',bf2(L)+' / '+bf2(Sx))
   +T('SOXL − SOXS','$'+bsg(L-Sx)+sm('×'+(Sx?(L/Sx).toFixed(3):'—')),'','Разница цен и отношение SOXL/SOXS')
   +(BLD.L0&&BLD.S0?(()=>{const dv=(L/BLD.L0+Sx/BLD.S0-2)*100,Lm=BLD.L0*(2-Sx/BLD.S0),cz=new Date((BLD.close_ts||0)*1000).toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
     return T('Отклонение от зеркала',(dv>0?'+':'')+dv.toFixed(2)+'%'+sm('SOXL '+bf2(L)+' − перевёрнутый SOXS '+bf2(Lm)+' = '+bsg(L-Lm)+' $'),Math.abs(dv)>=0.5?'warn':'',
      'Спред = SOXL/L0 + SOXS/S0 − 2 от закрытия США '+cz+' (L0 '+BLD.L0+', S0 '+BLD.S0+'). 0 — идеальное зеркало. Перевёрнутый SOXS в ценах SOXL = L0·(2 − SOXS/S0). Пара на равные $ зарабатывает ровно это отклонение. Через закрытие США база переклеивается')})():'')
  )+R('Время',''
   +T('США: '+(us.state||'—'),us.state=='открыта'?'закрытие через '+us.to_close_h+' ч':'открытие через '+us.to_open_h+' ч',us.state=='открыта'?'pos':'')
   +T('Ближайшая экспирация',exv,exc,'Опционы SOXL истекают в 23:00 по Киеву (16:00 ET)'));
}
function bldTable(legs,G){const L=bLnow(),inp=(id,v)=>`<input value="${v}" style="width:62px" onchange="BLDQ['${id}']=+this.value;bldRender()">`;
 const rows=legs.map(l=>{const isO=l.kind=='opt',side=l.qq>=0?'<span class="pos">Buy</span>':'<span class="neg">Sell</span>';
  let d,g,v,th;if(isO){d=l.qq*l.d;g=l.qq*l.g;v=l.qq*l.v;th=l.qq*l.th}else if(l.base=='SOXL'){d=l.qq;g=v=th=null}
  else{d=-l.qq*((BLD.L0&&BLD.S0)?BLD.S0/BLD.L0:bSnow()/L);g=v=th=null}
  const pnl=l.sim?(l.mark-l.avg)*l.qq:(l.upl+(l.qq-l.q)*0);
  return `<tr style="${l.on?'':'opacity:.4'}"><td><input type="checkbox" ${l.on?'checked':''} onchange="BLDOFF['${l.id}']=!this.checked;bldRender()"></td>`
  +`<td>${isO?'Опцион SOXL':(l.base=='SOXL'?'Перп SOXLUSDT':'Перп SOXSUSDT')}${l.sim?' <span class="warn">сим.</span>':''}</td><td>${isO?l.exp:'—'}</td><td>${isO?l.K:'—'}</td><td>${isO?(l.cp=='C'?'Колл':'Пут'):'—'}</td><td>${side}</td>`
  +`<td>${bf2(l.bidSize)}</td><td class="pos">${bf2(l.bid)}${isO&&l.bidIv?' / '+bpct(l.bidIv):''}</td><td>${inp(l.id,l.qq)}</td><td>${bf2(l.askSize)}</td><td class="neg">${bf2(l.ask)}${isO&&l.askIv?' / '+bpct(l.askIv):''}</td>`
  +`<td class="${bcl(pnl)}">${bsg(pnl)}</td><td>${bf4(d)}${l.base=='SOXS'?sm2('SOXL-экв., зеркало'):''}</td><td>${g==null?'—':bf4(g)}</td><td>${v==null?'—':bf4(v)}</td><td>${th==null?'—':bf4(th)}</td>`
  +`<td>${l.sim?`<span class="gbtn" onclick="BLDADD.splice(${+l.id.slice(3)},1);bldRender()">✕</span>`:''}</td></tr>`}).join('');
 $('bldtab').innerHTML='<tr><th></th><th>Инструмент</th><th>Эксп.</th><th>Страйк</th><th>Тип</th><th>Сторона</th><th>Бид разм.</th><th>Бид (IV)</th><th>Кол-во</th><th>Аск разм.</th><th>Аск (IV)</th><th>P&L, $</th><th>Δ</th><th>Γ</th><th>Вега</th><th>Θ /сут</th><th></th></tr>'+rows
  +`<tr style="background:#161b22"><td></td><td colspan="10"><b>Итого включённых ног</b> · Δ только SOXL ${bsg(G.dL)} · с SOXS по зеркалу <b>${bsg(G.dMir)}</b> ($${bf0(G.dMir*L)}) · после закрытия США ${bsg(G.dAft)}</td><td class="${bcl(G.pnl)}"><b>${bsg(G.pnl)}</b></td><td><b>${bf4(G.dMir)}</b></td><td><b>${bf4(G.g)}</b></td><td><b>${bf4(G.v)}</b></td><td><b>${bf4(G.th)}</b></td><td></td></tr>`;}
function sm2(t){return '<br><span style="font-size:10px;color:#8b949e">'+t+'</span>'}
function bldAddForm(){if($('bldaddform'))return bldAddOpts();const ex=bExps();
 $('bldctl').innerHTML=`<div id="bldaddform" style="font-size:12px;margin:4px 0">Добавить ногу (симуляция): <select id="bldtyp" onchange="bldAddOpts()"><option value="opt">Опцион SOXL</option><option value="SOXLUSDT">Перп SOXLUSDT</option><option value="SOXSUSDT">Перп SOXSUSDT</option></select>
  <select id="bldexp" onchange="bldAddOpts()">${ex.map(e=>`<option>${e.exp}</option>`).join('')}</select> <select id="bldk"></select> <select id="bldcp"><option value="P">Пут</option><option value="C">Колл</option></select>
  кол-во (+ купить / − продать) <input id="bldqty" value="1" style="width:60px"> <span class="gbtn" onclick="bldAdd()">добавить</span>
  <span class="gbtn" onclick="BLDQ={};BLDOFF={};BLDADD=[];bldRender()">сбросить к реальной позиции</span>
  &nbsp; горизонт: <input id="bldday" type="range" min="0" max="30" value="${BLDDAY}" oninput="BLDDAY=+this.value;$('blddayv').textContent=this.value;bldPnl(bldLegs())"> день <b id="blddayv">${BLDDAY}</b>
  &nbsp; цена от <input id="bldlo" value="${BLDLO}" style="width:40px" onchange="BLDLO=+this.value;bldPnl(bldLegs())"> до <input id="bldhi" value="${BLDHI}" style="width:40px" onchange="BLDHI=+this.value;bldPnl(bldLegs())"> × текущей</div>`;bldAddOpts()}
function bldAddOpts(){const t=$('bldtyp').value,isO=t=='opt';['bldexp','bldk','bldcp'].forEach(i=>$(i).style.display=isO?'':'none');if(!isO||!BLD)return;
 const e=$('bldexp').value,L=bLnow(),ks=[...new Set(BLD.chain.filter(c=>c.exp==e).map(c=>c.K))].sort((a,b)=>a-b),cur=$('bldk').value;
 const atm=ks.reduce((m,k)=>Math.abs(k-L)<Math.abs(m-L)?k:m,ks[0]);$('bldk').innerHTML=ks.map(k=>`<option ${String(k)==(cur||String(atm))?'selected':''}>${k}</option>`).join('')}
function bldAdd(){const t=$('bldtyp').value,q=+$('bldqty').value;if(!q)return;
 if(t=='opt'){const e=$('bldexp').value,K=+$('bldk').value,cp=$('bldcp').value,c=BLD.chain.find(c=>c.exp==e&&c.K==K&&c.cp==cp);if(!c)return;BLDADD.push({kind:'opt',sym:c.sym,q})}
 else BLDADD.push({kind:'perp',sym:t,q});bldRender()}
// ---------- график P&L с вероятностью ----------
function bldPnl(legs){const c=$('bldpnl');if(!c||!BLD)return;const x=c.getContext('2d'),W=c.width,H=c.height,L=bLnow(),now=BLD.now;
 const on=legs.filter(l=>l.on),opts=on.filter(l=>l.kind=='opt'),tH=now+BLDDAY*86400,tE=opts.length?Math.max(...opts.map(l=>l.ets)):null;
 const n=161,lo=L*BLDLO,hi=L*BLDHI,xs=[...Array(n)].map((_,i)=>lo+(hi-lo)*i/(n-1));
 const yH=xs.map(S=>bPnl(legs,S,tH)),yE=tE?xs.map(S=>bPnl(legs,S,tE)):null;
 // вероятность (логнормаль, IV ATM нужной экспирации, без дрейфа)
 const dens=(T,iv)=>{if(!(T>0)||!(iv>0))return null;const s=iv*Math.sqrt(T),mu=Math.log(L)-0.5*s*s;return xs.map(S=>Math.exp(-Math.pow(Math.log(S)-mu,2)/(2*s*s))/(S*s*Math.sqrt(2*Math.PI)))};
 const ex=bExps(),ivE=tE?bAtmIv((ex.find(e=>e.ets==tE)||{}).exp):null,ivH=bAtmIv((ex.find(e=>e.ets>=tH)||ex[ex.length-1]||{}).exp);
 const dE=tE?dens((tE-now)/YR,ivE):null,dH=dens((tH-now)/YR,ivH),dx=(hi-lo)/(n-1);
 const prob=(y,d)=>{if(!d)return null;let p=0,m=0,ex=0;d.forEach((v,i)=>{m+=v*dx;ex+=y[i]*v*dx;if(y[i]>0)p+=v*dx});return {p:p/m,ex:ex/m}};
 const pE=yE?prob(yE,dE):null,pH=prob(yH,dH);
 const all=[...yH,...(yE||[])];let ymin=Math.min(...all,0),ymax=Math.max(...all,0);const pad=(ymax-ymin)*.08||1;ymin-=pad;ymax+=pad;
 const pl=60,pr=14,pt=14,pb=28,X=S=>pl+(W-pl-pr)*(S-lo)/(hi-lo),Y=v=>pt+(H-pt-pb)*(1-(v-ymin)/(ymax-ymin));
 x.fillStyle='#0d1117';x.fillRect(0,0,W,H);x.font='11px sans-serif';x.strokeStyle='#21262d';x.fillStyle='#8b949e';
 for(let i=0;i<=6;i++){const v=ymin+(ymax-ymin)*i/6,y=Y(v);x.beginPath();x.moveTo(pl,y);x.lineTo(W-pr,y);x.stroke();x.fillText(bf0(v),4,y+4)}
 for(let i=0;i<=8;i++){const S=lo+(hi-lo)*i/8,xx=X(S);x.beginPath();x.moveTo(xx,pt);x.lineTo(xx,H-pb);x.stroke();x.fillText(S.toFixed(1),xx-12,H-10)}
 const dd=dE||dH;if(dd){const mx=Math.max(...dd);x.fillStyle='rgba(88,166,255,.10)';x.beginPath();x.moveTo(X(xs[0]),H-pb);dd.forEach((v,i)=>x.lineTo(X(xs[i]),H-pb-(H-pt-pb)*.35*v/mx));x.lineTo(X(xs[n-1]),H-pb);x.closePath();x.fill()}
 x.strokeStyle='#f85149';x.setLineDash([4,4]);x.beginPath();x.moveTo(pl,Y(0));x.lineTo(W-pr,Y(0));x.stroke();
 x.strokeStyle='#8b949e';x.beginPath();x.moveTo(X(L),pt);x.lineTo(X(L),H-pb);x.stroke();x.setLineDash([]);
 const line=(y,col,dash)=>{x.strokeStyle=col;x.lineWidth=2;x.setLineDash(dash||[]);x.beginPath();y.forEach((v,i)=>i?x.lineTo(X(xs[i]),Y(v)):x.moveTo(X(xs[i]),Y(v)));x.stroke();x.setLineDash([]);x.lineWidth=1};
 if(yE)line(yE,'#d29922',[6,4]);line(yH,'#3fb950');
 const be=y=>{const r=[];for(let i=1;i<n;i++)if((y[i-1]<0)!=(y[i]<0)){const t=y[i-1]/(y[i-1]-y[i]);r.push(xs[i-1]+(xs[i]-xs[i-1])*t)}return r};
 const bH=be(yH),bE=yE?be(yE):[];x.fillStyle='#e6edf3';[...bH,...bE].forEach(S=>{x.beginPath();x.arc(X(S),Y(0),3,0,7);x.fill();x.fillText(S.toFixed(2),X(S)-14,Y(0)-6)});
 x.fillStyle='#3fb950';x.fillText('— через '+BLDDAY+' дн'+(BLDDAY==0?' (сейчас; SOXS по зеркалу)':' (SOXS по β)'),pl+6,pt+12);if(yE){x.fillStyle='#d29922';x.fillText('- - на последней экспирации '+new Date(tE*1000).toISOString().slice(0,10),pl+6,pt+26)}
 x.fillStyle='rgba(88,166,255,.7)';x.fillText('синее — распределение цены по IV',pl+6,pt+40);
 BLDH={xs,yH,yE,X,Y,lo,hi,pl,pr,pt,pb};
 const mE=yE?Math.min(...yE):null;
 $('bldpnlnote').innerHTML=`Безубыток: через ${BLDDAY} дн — ${bH.length?bH.map(v=>v.toFixed(2)).join(', '):'нет'}${yE?' · на экспирации — '+(bE.length?bE.map(v=>v.toFixed(2)).join(', '):'нет'):''}`
  +` · <b>вероятность прибыли</b> (логнормаль по IV, без дрейфа): ${pH?bpct(pH.p)+' через '+BLDDAY+' дн':'—'}${pE?' · '+bpct(pE.p)+' на экспирации':''}`
  +` · ожидание P&L по IV: ${pE?bsg(pE.ex)+' $':'—'} · худший P&L в диапазоне на экспирации: ${mE!=null?bsg(mE)+' $':'—'}`;}
(function(){const bind=()=>{const c=$('bldpnl');if(!c||c._b)return;c._b=1;
 c.addEventListener('mousemove',e=>{if(!BLDH)return;const r=c.getBoundingClientRect(),sx=c.width/r.width,mx=(e.clientX-r.left)*sx;const H=BLDH;
  const S=H.lo+(H.hi-H.lo)*(mx-H.pl)/(c.width-H.pl-H.pr);if(S<H.lo||S>H.hi)return;const i=Math.round((S-H.lo)/(H.hi-H.lo)*(H.xs.length-1));
  bldPnl(bldLegs());const x=c.getContext('2d');x.strokeStyle='#6e7681';x.beginPath();x.moveTo(H.X(H.xs[i]),H.pt);x.lineTo(H.X(H.xs[i]),c.height-H.pb);x.stroke();
  const t=['SOXL '+H.xs[i].toFixed(2)+' ('+((H.xs[i]/bLnow()-1)*100).toFixed(1)+'%)','через '+BLDDAY+' дн: '+bsg(H.yH[i])+' $'].concat(H.yE?['на экспирации: '+bsg(H.yE[i])+' $']:[]);
  const bx=Math.min(H.X(H.xs[i])+8,c.width-200);x.fillStyle='rgba(22,27,34,.92)';x.fillRect(bx,H.pt+50,190,16*t.length+6);x.fillStyle='#e6edf3';t.forEach((s,k)=>x.fillText(s,bx+6,H.pt+64+k*16))});
 c.addEventListener('mouseleave',()=>bldPnl(bldLegs()))};if(document.readyState!='loading')bind();else document.addEventListener('DOMContentLoaded',bind)})();
// ---------- свечи индекса SOXL ----------
function bldCandles(){const c=$('bldk');if(!c)return;const x=c.getContext('2d'),W=c.width,H=c.height;x.fillStyle='#0d1117';x.fillRect(0,0,W,H);
 $('bldkctl').innerHTML='Индекс SOXL (Bybit index price): '+[['15','15м'],['60','1ч'],['240','4ч'],['D','Д'],['W','Н']].map(([v,l])=>`<span class="gbtn ${BLDKIV==v?'on':''}" onclick="BLDKIV='${v}';bldKline()">${l}</span>`).join('');
 const R=(BLDK&&BLDK.rows)||[];if(R.length<2){x.fillStyle='#8b949e';x.fillText('нет данных',10,20);return}
 const rows=R.slice(-120),n=rows.length,lo=Math.min(...rows.map(r=>r[3])),hi=Math.max(...rows.map(r=>r[2])),pl=6,pr=56,pt=18,pb=22;
 const X=i=>pl+(W-pl-pr)*(i+.5)/n,Y=v=>pt+(H-pt-pb)*(1-(v-lo)/(hi-lo||1)),bw=Math.max(1,(W-pl-pr)/n*.65);
 x.font='10px sans-serif';x.strokeStyle='#21262d';x.fillStyle='#8b949e';for(let i=0;i<=5;i++){const v=lo+(hi-lo)*i/5,y=Y(v);x.beginPath();x.moveTo(pl,y);x.lineTo(W-pr,y);x.stroke();x.fillText(v.toFixed(2),W-pr+4,y+3)}
 rows.forEach((r,i)=>{const up=r[4]>=r[1];x.strokeStyle=x.fillStyle=up?'#26a69a':'#ef5350';x.beginPath();x.moveTo(X(i),Y(r[2]));x.lineTo(X(i),Y(r[3]));x.stroke();
  const y1=Y(Math.max(r[1],r[4])),y2=Y(Math.min(r[1],r[4]));x.fillRect(X(i)-bw/2,y1,bw,Math.max(1,y2-y1))});
 const cl=R.map(r=>r[4]),ma=k=>cl.map((_,i)=>i<k-1?null:cl.slice(i-k+1,i+1).reduce((a,b)=>a+b,0)/k).slice(-n);
 [[7,'#d29922'],[14,'#bc8cff'],[28,'#58a6ff']].forEach(([k,col])=>{const m=ma(k);x.strokeStyle=col;x.beginPath();let s=0;m.forEach((v,i)=>{if(v==null)return;s?x.lineTo(X(i),Y(v)):x.moveTo(X(i),Y(v));s=1});x.stroke()});
 const last=rows[n-1];x.fillStyle='#f85149';x.fillRect(W-pr+1,Y(last[4])-8,pr-2,16);x.fillStyle='#fff';x.fillText(last[4].toFixed(2),W-pr+4,Y(last[4])+4);
 x.fillStyle='#8b949e';x.fillText('O '+last[1]+'  H '+last[2]+'  L '+last[3]+'  C '+last[4]+'   MA7 / MA14 / MA28',pl+4,12)}
// ---------- параметры ----------
function bldParams(legs,G){const L=bLnow(),Sx=bSnow(),b=BLD,mL=b.mexc.SOXL||{},mS=b.mexc.SOXS||{},f=b.factors||{},rv=b.rv||{},vr=b.vr||{};
 const dev=(S,Lp)=>(b.L0&&b.S0&&S&&Lp)?((S/b.S0-1)+(Lp/b.L0-1))*1e4:null,dB=dev(Sx,L),dM=dev(mS.last,mL.last);
 const ex=bExps().filter(e=>e.ets-b.now<40*86400),ivs=ex.map(e=>e.exp+' '+bpct(bAtmIv(e.exp))).join(' · ');
 const be=G.gR>0&&G.th<0?Math.sqrt(2*Math.abs(G.th)/G.gR)*100:null,rvd=rv.h30?rv.h30/Math.sqrt(365):null;
 const fr=(r,h)=>r==null?'—':(r*100).toFixed(4)+'% / '+(h||8)+'ч ('+(r*(24/(h||8))*365*100).toFixed(1)+'% год.)';
 const row=(k,v,n)=>`<tr><td>${k}</td><td><b>${v}</b></td><td style="color:#8b949e;font-size:11px">${n||''}</td></tr>`;
 $('bldpar').innerHTML='<table>'+row('Зеркало: база закрытия США',b.L0?'SOXL '+b.L0+' · SOXS '+b.S0:'—',b.close_ts?new Date(b.close_ts*1000).toISOString().slice(0,16).replace('T',' ')+' UTC':'')
  +row('Отклонение от зеркала (r_S + r_L)',(dB==null?'—':dB.toFixed(1)+' бп Bybit')+(dM==null?'':' · '+dM.toFixed(1)+' бп MEXC'),'норма σ ≈ 10 бп; > 20 бп — см. калькулятор (г)')
  +row('β SOXS/SOXL',b.beta+' (дни 120 дн) · '+(rv.beta_h3!=null?rv.beta_h3:'—')+' (часы 3 дн)','внутри дня зеркало −1, β — многодневная средняя')
  +row('Факторы SOXL (дни 180 дн)',Object.keys(f).map(k=>k+' β '+f[k].beta+' / корр '+f[k].corr).join(' · '),'SOXL ≈ 3× индекса полупроводников, через него Nasdaq')
  +row('RV SOXL',(rv.h7||'—')+'% / '+(rv.h30||'—')+'% / '+(rv.d30||'—')+'% / '+(rv.d120||'—')+'%','часы 7 дн / часы 30 дн / дни 30 дн / дни 120 дн')
  +row('IV ATM по экспирациям',ivs,'где IV ниже — гамма дешевле (Γ на $1 теты ∝ 1/IV²)')
  +row('Variance ratio',`VR(2) ${vr['2']??vr[2]??'—'} · VR(5) ${vr['5']??vr[5]??'—'} · VR(10) ${vr['10']??vr[10]??'—'}`,'> 1 тренд (пара через закрытия зарабатывает), < 1 пила (зарабатывает сетка)')
  +row('Безубыточный ход гаммы',be==null?'—':be.toFixed(2)+'% в день · RV '+(rvd==null?'—':rvd.toFixed(2)+'%/день'),'только опционы включённых ног')
  +row('Фандинг Bybit',`SOXL ${fr(b.perp.SOXL.funding,b.perp.SOXL.fundH)} · SOXS ${fr(b.perp.SOXS.funding,b.perp.SOXS.fundH)}`,b.perp.SOXL.nextFunding?'следующий '+new Date(b.perp.SOXL.nextFunding*1000).toISOString().slice(11,16)+' UTC':'')
  +row('Фандинг MEXC',`SOXL ${fr(mL.funding,8)} · SOXS ${fr(mS.funding,8)}`,'интервал MEXC принят 8 ч')
  +row('Цены Bybit / MEXC',`SOXL ${bf2(b.perp.SOXL.bid)}/${bf2(b.perp.SOXL.ask)} · ${bf2(mL.bid)}/${bf2(mL.ask)} — SOXS ${bf2(b.perp.SOXS.bid)}/${bf2(b.perp.SOXS.ask)} · ${bf2(mS.bid)}/${bf2(mS.ask)}`,'бид/аск')
  +row('Комиссии Bybit',b.fees&&b.fees.linear?`перп мейкер ${(b.fees.linear.maker*100).toFixed(3)}% / тейкер ${(b.fees.linear.taker*100).toFixed(4)}% · опцион ${b.fees.option?(b.fees.option.taker*100).toFixed(2)+'%':'—'} + поставка 0.02%`:'—','опционы: от цены базового актива, не больше 7% премии (правило Bybit)')
  +row('Сессия США',b.us?b.us.state+' · '+b.us.et+(b.us.state=='открыта'?' · закрытие через '+b.us.to_close_h+' ч':' · открытие через '+b.us.to_open_h+' ч'):'—','60% дисперсии — в сессию, 5% — в выходные')
  +'</table>';}
// ---------- арбитражные калькуляторы ----------
function bldArbInit(){if($('arbexp'))return;const ex=bExps(),f=BLD.fees||{};
 $('bldarb').innerHTML=`<div style="font-size:12px;color:#8b949e;margin:2px 0 6px">Числа живые (раз в 5 с). Комиссии — с твоего аккаунта Bybit; MEXC — введи свои. Это оценка до исполнения: глубина стакана, проскальзывание и расчётная цена экспирации не учтены.</div>
 <h4>(а) Синтетический фьючерс из опционов против перпа</h4>
 Экспирация <select id="arbexp" onchange="bldArbCalc()">${ex.map(e=>`<option ${e.exp=='4DEC26'?'selected':''}>${e.exp}</option>`).join('')}</select>
 страйков вокруг цены <input id="arbnk" value="4" style="width:36px" onchange="bldArbCalc()"> · комиссия опциона <input id="arbfo" value="${f.option?f.option.taker:0.0003}" style="width:60px" onchange="bldArbCalc()"> от цены базы
 · комиссия перпа <input id="arbfp" value="${f.linear?f.linear.taker:0.000275}" style="width:70px" onchange="bldArbCalc()"> (мейкер = 0)
 <div id="arbA"></div>
 <h4>(б) Межбиржевой спред Bybit ↔ MEXC</h4>
 комиссия Bybit <input id="arbfb" value="${f.linear?f.linear.taker:0.000275}" style="width:70px" onchange="bldArbCalc()"> · комиссия MEXC <input id="arbfm" value="0.0002" style="width:70px" onchange="bldArbCalc()"> · номинал $ <input id="arbnb" value="300" style="width:60px" onchange="bldArbCalc()">
 <div id="arbB"></div>
 <h4>(в) Фандинг-кэрри (спот/перп, перп/перп)</h4>
 номинал $ <input id="arbnc" value="300" style="width:60px" onchange="bldArbCalc()"> · дней <input id="arbdc" value="7" style="width:40px" onchange="bldArbCalc()"> · круг комиссий, % <input id="arbfc" value="0.06" style="width:50px" onchange="bldArbCalc()">
 <div id="arbC"></div>
 <h4>(г) Зеркало SOXL ↔ SOXS</h4>
 номинал $ на ногу <input id="arbnd" value="300" style="width:60px" onchange="bldArbCalc()"> · σ нормы, бп <input id="arbsd" value="10" style="width:40px" onchange="bldArbCalc()">
 <div id="arbD"></div>`}
function bldArbCalc(){if(!BLD||!$('arbexp'))return;const b=BLD,L=bLnow(),PL=b.perp.SOXL,mL=b.mexc.SOXL||{},mS=b.mexc.SOXS||{},now=b.now,v=id=>+$(id).value;
 // (а)
 const e=$('arbexp').value,nk=v('arbnk'),fo=v('arbfo'),fp=v('arbfp'),ch=b.chain.filter(c=>c.exp==e),ets=(ch[0]||{}).ets,days=ets?(ets-now)/86400:0;
 const ks=[...new Set(ch.map(c=>c.K))].sort((a,b)=>a-b),i0=ks.reduce((m,k,i)=>Math.abs(k-L)<Math.abs(ks[m]-L)?i:m,0),sel=ks.slice(Math.max(0,i0-nk),i0+nk+1);
 const fund=(PL.funding||0)*L*(days*24/(PL.fundH||8));
 const optFee=p=>Math.min(fo*L,0.07*Math.max(p,0));
 const ra=sel.map(K=>{const C=ch.find(c=>c.K==K&&c.cp=='C'),P=ch.find(c=>c.K==K&&c.cp=='P');if(!C||!P)return '';
  const okS=C.bid>0&&P.ask>0,okB=C.ask>0&&P.bid>0,sb=okS?K+C.bid-P.ask:null,sa=okB?K+C.ask-P.bid:null,del=0.0002*L;
  const conv=(px)=>sb==null||!px?null:sb-px-optFee(C.bid)-optFee(P.ask)-2*fp*px-del-fund;
  const rev=(px)=>sa==null||!px?null:px-sa-optFee(C.ask)-optFee(P.bid)-2*fp*px-del+fund;
  const cB=conv(PL.ask),rB=rev(PL.bid),cM=conv(mL.ask),rM=rev(mL.bid),best=Math.max(cB??-1e9,rB??-1e9,cM??-1e9,rM??-1e9);
  const cell=x=>x==null?'—':`<span class="${x>0?'pos':''}">${bsg(x)} (${(x/L*100).toFixed(2)}%${days>0?', '+(x/L*36500/days).toFixed(0)+'% год.':''})</span>`;
  return `<tr${best>0?' style="background:#1f6feb1a"':''}><td>${K}</td><td>${bf2(sb)} / ${bf2(sa)}</td><td>${bf2(PL.bid)} / ${bf2(PL.ask)}</td><td>${cell(cB)}</td><td>${cell(rB)}</td><td>${cell(cM)}</td><td>${cell(rM)}</td></tr>`}).join('');
 $('arbA').innerHTML=`<div style="font-size:11px;color:#8b949e">Синтетика = K + колл − пут. <b>Конверсия</b>: продать синтетику (колл по биду, пут по аску) + купить перп по аску. <b>Реверсия</b>: купить синтетику + продать перп по биду. До экспирации ${days.toFixed(1)} дн; фандинг по текущей ставке ${(PL.funding*100).toFixed(4)}% → ${bsg(fund)} $ на акцию для лонга перпа. Комиссия опциона = min(ставка × цена базы, 7% премии), поставка 0.02%. Риск: перп закрывается по рынку, а опционы рассчитываются по цене поставки Bybit — расхождение не учтено.</div>`
  +'<table><tr><th>Страйк</th><th>Синтетика продать / купить</th><th>Перп Bybit бид / аск</th><th>Конверсия (Bybit)</th><th>Реверсия (Bybit)</th><th>Конверсия (перп MEXC)</th><th>Реверсия (перп MEXC)</th></tr>'+ra+'</table>';
 // (б)
 const fb=v('arbfb'),fm=v('arbfm'),nb=v('arbnb');
 const rb=[['SOXL',b.perp.SOXL,mL],['SOXS',b.perp.SOXS,mS]].map(([n,B,M])=>{if(!M||!M.bid)return `<tr><td>${n}</td><td colspan="6">MEXC: нет данных</td></tr>`;
  const mid=(B.bid+B.ask)/2,a=(M.bid-B.ask)/mid*1e4,c=(B.bid-M.ask)/mid*1e4,fee=2*(fb+fm)*1e4,best=Math.max(a,c)-fee,fd=((B.funding||0)-(M.funding||0))*1e4;
  return `<tr${best>0?' style="background:#1f6feb1a"':''}><td>${n}</td><td>${bf2(B.bid)} / ${bf2(B.ask)}</td><td>${bf2(M.bid)} / ${bf2(M.ask)}</td><td>${a.toFixed(1)} бп</td><td>${c.toFixed(1)} бп</td><td class="${best>0?'pos':''}">${best.toFixed(1)} бп = ${bsg(best/1e4*nb)} $</td><td>${fd.toFixed(2)} бп / интервал</td></tr>`}).join('');
 $('arbB').innerHTML='<table><tr><th></th><th>Bybit бид / аск</th><th>MEXC бид / аск</th><th>купить Bybit → продать MEXC</th><th>купить MEXC → продать Bybit</th><th>лучшее после 2 кругов комиссий</th><th>фандинг Bybit − MEXC</th></tr>'+rb+'</table>'
  +'<div style="font-size:11px;color:#8b949e">Две отдельные маржи и голые ноги на каждой бирже; перевод обеспечения между биржами не мгновенный. Разница фандинга — кэрри: лонг там, где ставка ниже.</div>';
 // (в)
 const nc=v('arbnc'),dc=v('arbdc'),fc=v('arbfc')/100,src=[['Bybit SOXL',b.perp.SOXL.funding,b.perp.SOXL.fundH],['Bybit SOXS',b.perp.SOXS.funding,b.perp.SOXS.fundH],['MEXC SOXL',mL.funding,8],['MEXC SOXS',mS.funding,8]];
 const rc=src.map(([n,r,h])=>{if(r==null)return '';const carry=nc*Math.abs(r)*dc*24/(h||8),net=carry-fc*nc,ann=Math.abs(r)*(24/(h||8))*365*100,bed=Math.abs(r)>0?fc/(Math.abs(r)*24/(h||8)):null;
  return `<tr><td>${n}</td><td>${(r*100).toFixed(4)}% / ${h||8}ч</td><td>${ann.toFixed(1)}%</td><td>${r>0?'шорт перпа получает':(r<0?'лонг перпа получает':'—')}</td><td>${bsg(carry)} $</td><td class="${net>0?'pos':'neg'}">${bsg(net)} $</td><td>${bed==null?'—':bed.toFixed(1)+' дн'}</td></tr>`}).join('');
 const diff=((b.perp.SOXL.funding||0)-(mL.funding||0));
 $('arbC').innerHTML='<table><tr><th>Источник</th><th>Ставка</th><th>Годовых</th><th>Кто получает</th><th>Кэрри за срок</th><th>После комиссий</th><th>Окупаемость</th></tr>'+rc+'</table>'
  +`<div style="font-size:11px;color:#8b949e">Спота SOXL/SOXS на Bybit и MEXC в API нет — классическая связка «спот + перп» здесь невозможна (квест: найти токенизированный SOXL). Работает перп/перп между биржами: разница SOXL Bybit − MEXC сейчас ${(diff*100).toFixed(4)}% за интервал.</div>`;
 // (г)
 const nd=v('arbnd'),sd=v('arbsd'),md=(B,M)=>[(B.bid+B.ask)/2,(M.bid+M.ask)/2];
 const rd=[['Bybit',b.perp.SOXL,b.perp.SOXS],['MEXC',mL,mS]].map(([n,LL,SS])=>{if(!LL||!LL.bid||!SS||!SS.bid||!b.L0)return `<tr><td>${n}</td><td colspan="5">нет данных</td></tr>`;
  const Lm=(LL.bid+LL.ask)/2,Sm=(SS.bid+SS.ask)/2,fair=b.S0*(2-Lm/b.L0),dev=((Sm/b.S0-1)+(Lm/b.L0-1))*1e4,z=dev/sd,qS=nd/Sm,qL=qS*b.S0/b.L0;
  const act=Math.abs(z)<2?'в норме':(dev>0?`SOXS дорог: продать ${qS.toFixed(2)} SOXS и ${qL.toFixed(2)} SOXL (или убавить обе ноги лонг-лонга)`:`SOXS дёшев: купить ${qS.toFixed(2)} SOXS и ${qL.toFixed(2)} SOXL`);
  return `<tr${Math.abs(z)>=2?' style="background:#1f6feb1a"':''}><td>${n}</td><td>${bf2(Lm)} / ${bf2(Sm)}</td><td>${bf2(fair)}</td><td>${dev.toFixed(1)} бп · z ${z.toFixed(1)}</td><td>${bsg(Math.abs(dev)/1e4*nd)} $</td><td>${act}</td></tr>`}).join('');
 $('arbD').innerHTML='<table><tr><th></th><th>SOXL / SOXS (середина)</th><th>SOXS по зеркалу</th><th>Отклонение</th><th>Если сойдётся</th><th>Действие (Δ-нейтрально)</th></tr>'+rd+'</table>'
  +'<div style="font-size:11px;color:#8b949e">Зеркало от закрытия США (база Bybit); для MEXC база та же — приближение. Пара сделок Δ-нейтральна внутри дня: SOXS на N$ против SOXL на N·S0/L0 акций.</div>';}
// ---------- калькуляторы без прогноза: (Г) риск перпа на опцион 1:1, (Д) нейтрализация дельты опционами ≤ 10% эквити ----------
function bldNoArbInit(){if($('naq'))return;const ex=bExps(),q0=((BLD.legs.find(l=>l.sym=='SOXLUSDT')||{}).q)||1,L=bLnow();
 const eo=ex.map(e=>`<option ${e.exp=='4DEC26'?'selected':''}>${e.exp}</option>`).join('');
 $('bldnoarb').innerHTML=`<h4>(Г) Риск перпа на опцион 1:1 с усреднением</h4>
 лонг перпа <input id="naq" value="${q0}" style="width:50px" onchange="bldNoArbCalc()"> SOXL по <input id="nas" value="${L.toFixed(2)}" style="width:60px" onchange="bldNoArbCalc()"> · пут: экспирация <select id="naexp" onchange="bldNoArbCalc()">${eo}</select>
 страйк <input id="nak" value="${Math.floor(L)}" style="width:50px" onchange="bldNoArbCalc()"> · шаг усреднения, % <input id="nastep" value="10" style="width:40px" onchange="bldNoArbCalc()"> · уровней <input id="nan" value="3" style="width:36px" onchange="bldNoArbCalc()"> · добор на уровне, шт <input id="naadd" value="${q0}" style="width:50px" onchange="bldNoArbCalc()">
 <div id="naG"></div>
 <h4>(Д) Нейтрализация дельты опционами — не больше доли эквити</h4>
 дельта книги, акций SOXL <input id="ndd" value="" placeholder="авто" style="width:60px" onchange="bldNoArbCalc()"> · экспирация <select id="ndexp" onchange="bldNoArbCalc()">${eo}</select> · лимит премии, % эквити <input id="ndlim" value="10" style="width:40px" onchange="bldNoArbCalc()">
 <div id="naD"></div>`}
function bldNoArbCalc(G){if(!BLD||!$('naq'))return;G=G||bGreeks(bldLegs());const b=BLD,L=bLnow(),eq=b.acct.eq,v=id=>+$(id).value;
 // (Г)
 const q0=v('naq'),s0=v('nas'),K=v('nak'),st=v('nastep')/100,n=Math.max(0,Math.round(v('nan'))),add=v('naadd'),e=$('naexp').value,ch=b.chain.filter(c=>c.exp==e),ets=(ch[0]||{}).ets;
 const P=ch.find(c=>c.cp=='P'&&c.K==K)||ch.filter(c=>c.cp=='P').reduce((m,c)=>!m||Math.abs(c.K-K)<Math.abs(m.K-K)?c:m,null);
 let html='';if(!P||!ets)html='<span class="warn">нет такого пута</span>';else{const iv=P.askIv||P.markIv,Ku=P.K,rows=[];let Q=0,cost=0,prem=0;
  for(let i=0;i<=n;i++){const px=s0*(1-i*st),q=i==0?q0:add,T=(ets-b.now)/YR,p=i==0?(P.ask||P.mark):bBS(px,Ku,T,iv,'P');Q+=q;cost+=q*px;prem+=q*p;
   const worst=cost-Q*Ku+prem;rows.push(`<tr><td>${i==0?'вход':'−'+(i*st*100).toFixed(0)+'%'}</td><td>${bf2(px)}</td><td>+${q} перп + ${q} пут ${Ku}</td><td>${bf2(p)} × ${q} = ${bf2(p*q)} $</td><td>${bf2(Q)} шт, средняя ${bf2(cost/Q)}</td><td>${bf2(prem)} $</td><td class="${worst>eq*0.25?'neg':''}">${bf2(worst)} $ (${(worst/eq*100).toFixed(0)}% эквити)</td></tr>`)}
  html=`<div style="font-size:11px;color:#8b949e">На каждом уровне докупается перп и столько же путов (1:1). Худший итог — цена на экспирации ниже страйка: (средняя − K) × объём + все премии. Пут на нижних уровнях — Блэк–Шоулз по IV аска (${bpct(iv)}), на скачке волы будет дороже.</div>`
   +'<table><tr><th>Уровень</th><th>Цена</th><th>Добор</th><th>Премия уровня</th><th>Позиция</th><th>Премии всего</th><th>Худший итог на экспирации</th></tr>'+rows.join('')+'</table>'}
 $('naG').innerHTML=html;
 // (Д)
 const dd=$('ndd').value===''?G.dMir:+$('ndd').value,e2=$('ndexp').value,ch2=b.chain.filter(c=>c.exp==e2),cp=dd>0?'P':'C',lim=v('ndlim')/100*eq;
 const cand=ch2.filter(c=>c.cp==cp&&c.ask>0&&Math.abs(c.d)>0.25&&Math.abs(c.d)<0.6).sort((a,b2)=>Math.abs(Math.abs(a.d)-0.45)-Math.abs(Math.abs(b2.d)-0.45))[0];
 if(!cand||!dd){$('naD').innerHTML='<span class="warn">нет подходящего опциона или дельта ≈ 0</span>';return}
 const nn=Math.abs(dd)/Math.abs(cand.d),need=Math.ceil(nn),prem=need*cand.ask,okN=Math.floor(lim/cand.ask),res=dd+Math.min(need,okN)*cand.d*(cp=='P'?1:1);
 $('naD').innerHTML=`<table><tr><th>Дельта книги</th><th>Чем гасить</th><th>Нужно контрактов</th><th>Премия</th><th>Лимит (${v('ndlim')}% эквити)</th><th>Остаток дельты в лимите</th><th>Θ /сут</th><th>Δ$ на 1% (гамма)</th></tr>
  <tr><td>${bsg(dd)} SOXL ($${bf0(dd*L)})</td><td>купить ${cp=='P'?'пут':'колл'} ${cand.exp} ${cand.K} (Δ ${bf2(cand.d)}, аск ${bf2(cand.ask)}, IV ${bpct(cand.askIv||cand.markIv)})</td><td>${need}</td><td class="${prem>lim?'neg':''}">${bf2(prem)} $ (${(prem/eq*100).toFixed(1)}% эквити)</td><td>$${bf0(lim)} → до ${okN} шт</td><td>${bsg(dd+Math.min(need,okN)*cand.d)} SOXL</td><td class="neg">${bf2(Math.min(need,okN)*cand.th)}</td><td>${bf2(Math.min(need,okN)*cand.g*L*L/100)}</td></tr></table>
  <div style="font-size:11px;color:#8b949e">Покупка опциона вместо перпа: дельта гасится, гамма добавляется (выигрыш на большом ходе), платишь премией и тетой. Лимит по эквити не даёт «залезть в плечо» опционами.</div>`;}

if(document.readyState!='loading')setTimeout(()=>dkView(DKVIEW),0);else document.addEventListener('DOMContentLoaded',()=>dkView(DKVIEW));

const DKETH=0;
const DKCHARTS=0;   // 16.11: графики спреда (виджет TradingView + свой график пары) убраны — Роман смотрит в приложении TradingView; 1 = вернуть
// ---- TradingView-виджет на деске: спред SOXL/SOXS по перпам Bybit (BYBIT:…USDT.P); ETF в США — NYSE Arca → префикс AMEX: ----
let DK_TV='APP', DK_TVI='1', DK_TVW=null;
try{DK_TV=localStorage.getItem('dk_tv3')||DK_TV;DK_TVI=localStorage.getItem('dk_tvi3')||DK_TVI;}catch(e){}
function dkTvLoad(){if(window.TradingView){dkTvDraw();return}
 if(document.getElementById('tvjs')){let w=setInterval(()=>{if(window.TradingView){clearInterval(w);dkTvDraw();}},200);return;}
 const sc=document.createElement('script');sc.id='tvjs';sc.src='https://s3.tradingview.com/tv.js';sc.onload=dkTvDraw;document.head.appendChild(sc);}
function dkTvSet(x){DK_TV=x;try{localStorage.setItem('dk_tv3',DK_TV)}catch(e){} dkTvDraw();}
function dkTvInt(iv){DK_TVI=iv;try{localStorage.setItem('dk_tvi3',DK_TVI)}catch(e){} dkTvDraw();}
function dkTvDraw(){if(!window.TradingView||TAB!='D')return;const box=$('dktv_box');if(!box)return;box.innerHTML='';
 const iv=[['1','1м'],['5','5м'],['15','15м'],['60','1ч'],['240','4ч'],['D','Д']];
 const bt=(x,l)=>`<span class="gbtn ${DK_TV==x?'on':''}" onclick="dkTvSet('${x}')">${l}</span>`;
 $('dktv_ctl').innerHTML='<b>TradingView, перпы Bybit</b>: '+bt('APP','как в приложении: 2 линии')+bt('BYBIT:SOXLUSDT.P-BYBIT:SOXSUSDT.P','SOXL − SOXS')+bt('BYBIT:SOXLUSDT.P/BYBIT:SOXSUSDT.P','SOXL / SOXS')+bt('BYBIT:SOXLUSDT.P+BYBIT:SOXSUSDT.P','SOXL + SOXS (лонг обеих)')+bt('BYBIT:SOXLUSDT.P','SOXL и SOXS линиями')
  +' &nbsp; <span style="color:#8b949e">биржа США:</span> '+bt('AMEX:SOXL/AMEX:SOXS','SOXL / SOXS')+bt('NASDAQ:SOXX','SOXX (индекс)')
  +' &nbsp; '+iv.map(([v,l])=>`<span class="gbtn ${DK_TVI==v?'on':''}" onclick="dkTvInt('${v}')">${l}</span>`).join('')
  +' &nbsp; <input id="dktvin" style="width:250px" value="'+DK_TV+'" onkeydown="if(event.key==\'Enter\')dkTvSet(this.value)"> <span class="gbtn" onclick="dkTvSet($(\'dktvin\').value)">показать</span>';

 if(DK_TV=='APP'){   // как в приложении: SOXL + SOXS линиями на своих шкалах (compareSymbols NewPriceScale); спред-символ в compare не принимается, спреды в анонимном виджете только дневные
  const mk=(h,cfg)=>{const w=document.createElement('div');w.className='tradingview-widget-container';w.style.cssText='height:'+h+'px;width:100%;overflow:hidden';
   const inner=document.createElement('div');inner.className='tradingview-widget-container__widget';inner.style.cssText='height:100%;width:100%';w.appendChild(inner);
   const sc=document.createElement('script');sc.type='text/javascript';sc.async=true;sc.src='https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js';
   sc.innerHTML=JSON.stringify(Object.assign({autosize:false,width:'100%',interval:DK_TVI,timezone:'Etc/UTC',theme:'dark',style:'2',locale:'ru',support_host:'https://www.tradingview.com'},cfg));
   w.appendChild(sc);box.appendChild(w);};
  box.style.height='auto';
  mk(520,{height:520,symbol:'BYBIT:SOXLUSDT.P',allow_symbol_change:true,hide_side_toolbar:false,withdateranges:true,compareSymbols:[{symbol:'BYBIT:SOXSUSDT.P',position:'NewPriceScale'}]});
  // спред SOXL − SOXS отдельным виджетом не ставим: анонимный виджет TradingView даёт спреды только на дневках (проверено 16.11) — внутридневной спред в графике ниже (drawPCtv)
  DK_TVW=1;return;}
 box.style.height='560px';
 const st=(DK_TV=='BYBIT:SOXLUSDT.P')?[{id:"Overlay@tv-basicstudies",inputs:{symbol:"BYBIT:SOXSUSDT.P"}}]:['BB@tv-basicstudies'];
 DK_TVW=new TradingView.widget({container_id:'dktv_box',symbol:DK_TV,interval:DK_TVI,autosize:true,theme:'dark',style:'1',locale:'ru',timezone:'Etc/UTC',hide_side_toolbar:false,allow_symbol_change:true,withdateranges:true,studies:st});}   // 16.11: ETH закрыт — деск только по спреду SOXL/SOXS; 1 = вернуть ETH-блоки
async function dlive(){ if(TAB!='D'||!DKD||DLB)return; DLB=1;
 try{const L=await (await fetch('/desklive')).json(); if(L.build){if(window.DKBUILD&&window.DKBUILD!=L.build){location.reload();return} window.DKBUILD=L.build;} if(L.wait||L.err)return; const d=DKD;
  Object.assign(d.acct,L.acct); Object.assign(d.eth,L.eth); Object.assign(d.sox,{pxL:L.sox.pxL,pxS:L.sox.pxS,eq_usd:L.sox.eq_usd,hedge:L.sox.hedge,eq_after:L.sox.eq_after});
  if(d.sox.opt)d.sox.opt.th=L.sox.opt_th; d.sox.dparts=Object.assign(d.sox.dparts||{},{L_usd:L.sox.L_usd,S_usd:L.sox.S_usd,shares:L.sox.shares});
  d.dsum=Object.assign(d.dsum||{},L.dsum); d.theta_day=L.theta_day; d.alerts=L.alerts; d.tlive=L.t; renderTiles(d);
 }catch(e){} finally{DLB=0}}
function renderDesk(d){
 if(TAB=='D'&&DKCHARTS&&!DK_TVW)dkTvLoad();
 $('dkchartsec').style.display=DKCHARTS?'':'none';
 $('upd').textContent='деск · '+d.t;
 const a=d.acct,L=d.lev,th0=d.theta&&d.theta[0]?d.theta[0].tot:0,f0=v=>v==null?'—':(+v).toFixed(0),f2=v=>v==null?'—':(+v).toFixed(2);
 const ic=a.im_pct>=50?'neg':(a.im_pct>=40?'warn':'pos'),rc=a.room<a.eq*0.25?'neg':(a.room<a.eq*0.5?'warn':'pos');
 const T=(l,v,c)=>`<div class="tile sm"><div class="lbl">${l}</div><div class="val ${c||''}">${v}</div></div>`,e=d.eth,s=d.sox,xb=d.xbeta||{},xd=xb.day||{},xh=xb.hour||{};
 const sn=Math.abs(s.eq_usd)<40,en=Math.abs(e.delta)<0.1;
 DKD=d; renderTiles(d);
 {const ds=d.dsum||{},rows=[];(d.dgroups||[]).filter(g=>DKETH||!g.grp.startsWith('ETH')).forEach(g=>{rows.push(`<tr style="background:#161b22"><td><b>${g.grp}</b></td><td><b>${sg((+g.d).toFixed(3))}</b></td><td class="${cl(g.usd)}"><b>$${f0(g.usd)}</b></td><td class="${cl(g.eq)}"><b>${g.grp.startsWith('ETH')?'':'$'+f0(g.eq)}</b></td></tr>`);
   (d.dpos||[]).filter(p=>p.grp==g.grp).forEach(p=>rows.push(`<tr><td style="padding-left:22px;color:#c9d1d9">${p.name}</td><td>${sg((+p.d).toFixed(3))}</td><td class="${cl(p.usd)}">$${f0(p.usd)}</td><td class="${cl(p.eq)}">${g.grp.startsWith('ETH')?'':'$'+f0(p.eq)}</td></tr>`));});
  $('dkdpos').innerHTML='<tr><th>Конструкция / позиция</th><th>Δ, шт</th><th>Δ$</th><th>Δ$ в SOXL-экв. (SOXS по зеркалу от закрытия США)</th></tr>'+rows.join('')
   +`<tr><td><b>ETH итого</b></td><td></td><td class="${cl(ds.eth_usd)}"><b>$${f0(ds.eth_usd)}</b></td><td></td></tr><tr><td><b>SOX итого (до закрытия США)</b></td><td></td><td></td><td class="${cl(ds.sox_eq)}"><b>$${f0(ds.sox_eq)}</b></td></tr><tr><td><b>ETH + SOX</b> <span style="color:#8b949e;font-size:11px">(разные активы, корр ETH·SOXL ${(d.xbeta&&d.xbeta.day&&d.xbeta.day.corr)??'—'} — сумма условная)</span></td><td></td><td colspan="2" class="${cl(ds.total)}"><b>$${f0(ds.total)}</b></td></tr>`;
  if(!DKETH)[...$('dkdpos').rows].forEach(r=>{const t=r.cells[0]&&r.cells[0].textContent;if(t&&(t.startsWith('ETH итого')||t.startsWith('ETH + SOX')))r.style.display='none';});}
 $('dktodo').innerHTML=(TDX=d.todo.filter(x=>DKETH||/SOX/.test(x.txt))).length?TDX.map(x=>`<div class="${x.lvl}">• ${x.txt}</div>`).join(''):'<span class="pos">Всё в нейтрали, срочного нет.</span>';
 // маржа
 const sc=d.margin.length?d.margin[0].row.map(r=>r[0]):[];
 $('dkmargin').innerHTML='<tr><th>Монета</th><th>MM</th><th>IM</th><th>надбавки</th><th>худший</th><th>где (цена / IV)</th><th style="text-align:left">сценарии: худший убыток при сдвиге цены</th></tr>'+d.margin.filter(m=>DKETH||['SOXL','SOXS','SOXL+SOXS'].includes(m.coin)).map(m=>{
  const cells=m.row.map(r=>`<span title="${(r[0]*100).toFixed(1)}%" style="display:inline-block;min-width:34px;padding:0 3px;text-align:right;background:${r[1]<0?'rgba(248,81,73,'+Math.min(.6,Math.abs(r[1])/80):'rgba(63,185,80,'+Math.min(.5,r[1]/150)})">${r[1].toFixed(0)}</span>`).join('');
  const rng=m.row.length?(m.row[0][0]*100).toFixed(0)+'%…'+(m.row[m.row.length-1][0]*100).toFixed(0)+'%':'';
  if(m.combo)return `<tr style="background:#1f6feb1a;border-top:1px solid #30363d"><td><b>${m.coin}</b></td><td>$${m.mm.toFixed(0)}</td><td>$${m.im.toFixed(0)}</td><td>$${m.cont.toFixed(0)}</td><td class="neg">$${m.worst.toFixed(0)}</td><td style="font-size:11px">ход SOXL ${(m.move*100).toFixed(1)}% · Bybit берёт сумму $${m.sum_worst.toFixed(0)}</td><td style="text-align:left;font-size:11px">${cells} <span style="color:#8b949e">по ходу SOXL ${rng}</span></td></tr>`;
  return `<tr><td><b>${m.coin}</b></td><td>$${m.mm.toFixed(0)}</td><td>$${m.im.toFixed(0)}</td><td>$${m.cont.toFixed(0)}</td><td class="neg">$${m.worst.toFixed(0)}</td><td>${(m.move*100).toFixed(1)}% / IV ${(m.iv*100).toFixed(0)}%</td><td style="text-align:left;font-size:11px">${cells} <span style="color:#8b949e">${rng}</span></td></tr>`}).join('');
 // ETH
 drawPayoff('dkeplot',e.curve,'ETH');
 if(0)$('x').innerHTML=tile('ETH','$'+f0(e.px))+tile('Δ',sg(e.delta)+' ETH',Math.abs(e.delta)<0.1?'pos':'warn')+tile('Вега','$'+f2(e.vega)+'/п.',cl(e.vega))+tile('Θ /сут','$'+f2(e.theta),cl(e.theta))+tile('MM ETH','$'+f0(e.mm))
  +tile('Худший Bybit',((e.move||0)*100).toFixed(0)+'% / IV '+((e.iv||0)*100).toFixed(0)+'%')+tile('Проданные путы','$'+e.short_notional+'<div style="font-size:10px;color:#8b949e">по страйкам</div>','warn');
 $('dkelegs').innerHTML='<tr><th>Нога</th><th>кол-во</th><th>mark</th><th>бид / аск</th><th>Δ</th><th>вега</th><th>Θ/сут</th><th>стоит</th><th>uPnL</th><th>выход по стакану</th><th title="как изменится худший сценарий Bybit по ETH">закрыть → маржа</th></tr>'
  +e.legs.sort((x,y)=>x.ets-y.ets).map(l=>`<tr${l.tail?' style="background:rgba(210,153,34,.08)"':''}><td>${l.sym}${l.tail?' <span class="warn" title="проданный пут — хвост">⚠</span>':''}</td><td class="${cl(l.q)}">${l.q}</td><td>${f2(l.mark)}</td><td>${l.bid} / ${l.ask}</td><td>${f2(l.d)}</td><td class="${cl(l.v)}">${f2(l.v)}</td><td class="${cl(l.th)}">${f2(l.th)}</td><td>$${f0(l.val)}</td><td class="${cl(l.upl)}">${f2(l.upl)}</td><td>${l.exit==null?'—':'$'+f0(l.exit)}</td><td class="${cl(l.dmm)}">${l.dmm==null?'—':sg(l.dmm)}</td></tr>`).join('');
 $('dkeperps').innerHTML=e.perps.length||e.spot?'<tr><th>Перп / спот</th><th>кол-во</th><th>$</th><th>вход</th><th>uPnL</th><th>фандинг</th><th>$/сут</th></tr>'+e.perps.map(p=>`<tr><td>${p.sym}</td><td class="${cl(p.q)}">${p.q}</td><td>${f0(p.usd)}</td><td>${p.avg}</td><td class="${cl(p.upl)}">${f2(p.upl)}</td><td>${p.fund}%</td><td class="${cl(p.fund_day)}">${f2(p.fund_day)}</td></tr>`).join('')+(Math.abs(e.spot)>1e-4?`<tr><td>спот ETH</td><td>${(+e.spot).toFixed(4)}</td><td>${f0(e.spot*e.px)}</td><td colspan="4"></td></tr>`:''):'';
 $('dkestress').innerHTML='<tr><th>ETH</th><th>IV −15</th><th>IV 0</th><th>IV +15</th><th>IV +30</th></tr>'+e.stress.map(r=>`<tr><td>${r.mv>0?'+':''}${(r.mv*100).toFixed(0)}% · ${r.S}</td>`+r.v.map(v=>`<td class="${v<-a.room?'neg':(v<0?'warn':'pos')}"${v<-a.room?' style="font-weight:700"':''}>${v>0?'+':''}${v}</td>`).join('')+'</tr>').join('')
  +`<tr><td colspan="5" style="color:#8b949e;font-size:11px;text-align:left">красное жирное — убыток больше запаса $${f0(a.room)}</td></tr>`;
 $('dketail').innerHTML='<tr><th>после экспирации</th><th>купл.</th><th>прод.</th><th>перевес прод.</th><th>ETH −50%</th><th>ETH −70%</th></tr>'+e.tail.map(t=>`<tr><td>${t.after}</td><td>${t.long}</td><td>${t.short}</td><td class="${t.naked>0?'warn':''}">${t.naked}</td><td class="${t.pay50<0?'neg':'pos'}">${t.pay50>0?'+':''}${t.pay50}</td><td class="${t.pay70<-a.eq?'neg':(t.pay70<0?'warn':'pos')}">${t.pay70>0?'+':''}${t.pay70}</td></tr>`).join('')
  +'<tr><td colspan="6" style="color:#8b949e;font-size:11px;text-align:left">выплата путов на экспирации, $ (без премий). Красное — минус</td></tr>';
 // SOX
 $('dkethsec').style.display=DKETH?'':'none';
 const ps=s.pseudo,hd=s.hedge;drawPayoff('dksplot',s.curve,'SOXL');DKSOX=s;const pL=s.perps.find(p=>p.sym=='SOXLUSDT'),pS=s.perps.find(p=>p.sym=='SOXSUSDT');DKPAIR={a:{size:pL?pL.q:0,avg:pL?pL.avg:null},b:{size:pS?pS.q:0,avg:pS?pS.avg:null}};if(PCT.length&&TAB=='D')drawPC();
 if(0)$('x').innerHTML=tile('Δ SOX','$'+f0(s.eq_usd)+'<div style="font-size:10px;color:#8b949e">SOXL-экв.</div>',Math.abs(s.eq_usd)<40?'pos':'warn')
  +tile('Хедж',hd==null||Math.abs(s.eq_usd)<40?'—':('SOXLUSDT '+(hd>0?'Buy ':'Sell ')+Math.abs(hd).toFixed(2)),Math.abs(s.eq_usd)<40?'':'warn')
  +tile('β SOXS/SOXL',s.beta)+tile('SOXL / SOXS','$'+f2(s.pxL)+' / $'+f2(s.pxS))+tile('MM SOXL','$'+f0(s.mm.SOXL))+tile('MM SOXS','$'+f0(s.mm.SOXS))
  +tile('Фандинг /сут','$'+f2(s.fund_day),cl(s.fund_day))+tile('Опционы Θ/сут','$'+f2(s.opt.th),cl(s.opt.th))+tile('Опционы вега','$'+f2(s.opt.v)+'/п.')+tile('Опционы Γ',f2(s.opt.g)+' на $1');
 $('dksperps').innerHTML='<tr><th>Перп</th><th>кол-во</th><th>$</th><th>вход</th><th>mark</th><th>uPnL</th><th>реализовано всего</th><th>фандинг /8ч</th><th>$/сут</th></tr>'+s.perps.map(p=>`<tr><td>${p.sym}</td><td>${p.q}</td><td>${f0(p.usd)}</td><td>${p.avg}</td><td>${f2(p.mark)}</td><td class="${cl(p.upl)}">${f2(p.upl)}</td><td class="${cl(p.real)}">${f2(p.real)}</td><td>${p.fund}%</td><td class="${cl(p.fund_day)}">${f2(p.fund_day)}</td></tr>`).join('');
 $('dkslegs').innerHTML=s.legs.length?'<tr><th>Опцион</th><th>кол-во</th><th>mark</th><th>бид / аск</th><th>Δ</th><th>Γ</th><th>вега</th><th>Θ/сут</th><th>стоит</th><th>uPnL</th></tr>'+s.legs.sort((x,y)=>x.ets-y.ets).map(l=>`<tr><td>${l.sym}</td><td>${l.q}</td><td>${f2(l.mark)}</td><td>${l.bid} / ${l.ask}</td><td>${f2(l.d)}</td><td>${(+l.g).toFixed(3)}</td><td>${f2(l.v)}</td><td class="${cl(l.th)}">${f2(l.th)}</td><td>$${f0(l.val)}</td><td class="${cl(l.upl)}">${f2(l.upl)}</td></tr>`).join(''):'';
 const vr=ps.vr||{};
 const dp=s.dparts||{},sg2=v=>(v>0?'+':'')+(+v).toFixed(2);
 $('dkspseudo').innerHTML=(s.dparts?`<div style="margin-bottom:4px"><b>Δ SOX</b> = SOXL (перп ${sg2(dp.L_perp)} · опционы ${sg2(dp.L_opt)} · спот ${sg2(dp.L_spot)} = ${sg2(dp.L_tot)} шт) × $${f2(s.pxL)} = $${f0(dp.L_usd)} &nbsp;+&nbsp; SOXS (перп ${sg2(dp.S_perp)} · опционы ${sg2(dp.S_opt)} · спот ${sg2(dp.S_spot)} = ${sg2(dp.S_tot)} шт) × зеркало $${f2(s.S0)}·${f2(s.pxL)}/${f2(s.L0)} (база — закрытие США) = $${f0(dp.S_usd)} &nbsp;→&nbsp; <b class="${sn?'pos':'warn'}">$${f0(s.eq_usd)}</b> (${sg2(dp.shares)} SOXL-экв.). После закрытия база переклеится: Δ станет $${f0(s.eq_after)} (Δ_L·L − Δ_S·S). Дельта опционов — греки Bybit по mark IV.</div>`:'');
 // тета и плечи
 $('dktheta').innerHTML='<tr><th>день</th>'+(DKETH?'<th>ETH</th>':'')+'<th>SOX</th><th>прочее</th><th>всего</th></tr>'+d.theta.slice(0,7).map(r=>`<tr><td>${r.d}</td>${DKETH?`<td class="${cl(r.ETH)}">${r.ETH}</td>`:''}<td class="${cl(r.SOX)}">${r.SOX}</td><td class="${cl(r['прочее'])}">${r['прочее']}</td><td class="${cl(r.tot)}"><b>${r.tot}</b></td></tr>`).join('')
  +`<tr><td colspan="5" style="text-align:left;color:#8b949e">только опционы (Блэк–Шоулз при той же цене и IV); пара перпов и фандинг — в плитке «Θ книги SOX»</td></tr>`;
 $('dklev').innerHTML='<tr><th>Мера</th><th>значение</th><th>к эквити</th></tr>'
  +`<tr><td>IM / MM</td><td>${L.im_pct}% / ${L.mm_pct}%</td><td class="${ic}">${L.im_pct>50?'выше 50%':'ок'}</td></tr>`
  +`<tr><td>Запас до ликвидации</td><td>$${f0(L.room)}</td><td>${L.room_x}× MM</td></tr>`
  +`<tr><td>Кэш USDT / USDC</td><td class="${L.usdt<0?'neg':''}">$${f0(L.usdt)} / $${f0(L.usdc)}</td><td>${L.usdt<0?'займ':'займа нет'}</td></tr>`
  +L.net.map(n=>`<tr><td>Δ ${n.b}</td><td>${n.true} · $${f0(n.usd)}</td><td>${n.x}×</td></tr>`).join('')
  +`<tr><td>Δ SOX с β (SOXL-экв.)</td><td>$${f0(L.sox_eq)}</td><td></td></tr>`
  +`<tr><td>Брутто перпов</td><td>$${f0(L.gross)}</td><td>${L.gross_x}× (ноги гасят друг друга)</td></tr>`
  +`<tr><td>Проданные путы по страйкам</td><td>$${L.short_puts}</td><td class="warn">${L.short_puts_x}×</td></tr>`
  +`<tr><td>Перевес проданных путов сейчас</td><td>${L.naked_now}</td><td></td></tr>`;}
// ===================== Навигация: 5 главных вкладок, внутри — подвкладки (старые вкладки целиком) =====================
const NAV={DK:{n:'Деск: спред SOXL/SOXS',subs:[['D','Текущее положение: спред SOXL/SOXS']]},
 K:{n:'Книга',subs:[['K','Книга']]},
 SP:{n:'Оператор спреда SOXL/SOXS',subs:[['S','Пара, конструкции, графики, сетки']]},
 ETH:{n:'ETH: sell far put',subs:[['T','ETH: продажа дальних путов']]},
 SH:{n:'Шиткоины',subs:[['L','Мои позиции и лаборатория'],['R','Сканер ершей и спредов MEXC↔Bybit'],['F','Широкие колебания (TradFi-сканер)']]},
 AP:{n:'Арб-порт',subs:[['A','Арбитраж'],['E','Портфель: детали'],['G','Золото: опционы'],['C','Капитал Bybit/MEXC'],['I','Доходы'],['O','Сводка (Оператор)']]}};
let NAVLAST={};try{NAVLAST=JSON.parse(localStorage.getItem('navlast')||'{}')}catch(e){}
function mainOf(t){for(const m in NAV)if(NAV[m].subs.some(x=>x[0]==t))return m;return 'K';}
function showMain(m){const L=NAVLAST[m];showTab(L&&NAV[m].subs.some(x=>x[0]==L)?L:NAV[m].subs[0][0]);}
function navSync(t){const m=mainOf(t);NAVLAST[m]=t;try{localStorage.setItem('navlast',JSON.stringify(NAVLAST))}catch(e){}
 for(const k in NAV){const b=$('mb'+k);if(b)b.classList.toggle('on',k==m);}
 const S=NAV[m].subs;$('subbar').innerHTML=S.length>1?'<span style="color:#8b949e;margin-right:6px">'+NAV[m].n+':</span>'+S.map(x=>`<span class="gbtn ${x[0]==t?'on':''}" id="tb${x[0]}" onclick="showTab('${x[0]}')">${x[1]}</span>`).join(''):'';}
function renderBook(d){
 const a=d.acct,all=d.groups.ALL,t=all.tot,eq=a.eq||0,b=all.beta;
 if(TAB=='K')$('upd').textContent='книга · '+d.t;
 const bc=a.buf<1.5?'neg':(a.buf<1.8?'warn':'pos'),dl=eq*0.2,th=eq*0.01;
 const dc=v=>Math.abs(v||0)<=dl?'pos':'warn',gETH=d.groups.ETH,gBTC=d.groups.BTC,gMNT=d.groups.MNT,gSOX=d.groups.SOX;
 $('ktiles').innerHTML=tile('Эквити','$'+a.eq)+tile('Свободно','$'+a.av,a.av<100?'warn':'')+tile('IM',a.im_pct+'%',bc)+tile('MM',a.mm_pct+'%',bc)+tile('Запас',a.buf+'x',bc)
  +tile('Δ$',sg(t.dusd),dc(t.dusd))+(t.eq!=null?tile('Δ ETH-экв.',sg(t.eq)+'$',dc(t.eq)):'')
  +tile('Θ /д',sg(t.t)+'$<div style="font-size:10px;color:#8b949e">опц '+sg(t.ot)+' · фанд '+sg(t.pt)+'</div>',Math.abs(t.t)>th?'warn':cl(t.t))
  +tile('Δ ETH (со спотом)',gETH&&gETH.tot.true!=null?(sg(gETH.tot.true)+' ETH<div style="font-size:10px;color:#8b949e">$'+Math.round(gETH.tot.dusd)+'</div>'):'—',dc(gETH?gETH.tot.dusd:0))
  +tile('Δ BTC (со спотом)',gBTC&&gBTC.tot.true!=null?(sg(gBTC.tot.true)+' BTC<div style="font-size:10px;color:#8b949e">$'+Math.round(gBTC.tot.dusd)+'</div>'):'—',dc(gBTC?gBTC.tot.dusd:0))
  +tile('Δ MNT (со спотом)',gMNT&&gMNT.tot.true!=null?(sg(gMNT.tot.true)+' MNT<div style="font-size:10px;color:#8b949e">$'+Math.round(gMNT.tot.dusd)+'</div>'):'—',dc(gMNT?gMNT.tot.dusd:0))
  +tile('Δ SOXL/SOXS',gSOX&&gSOX.tot.dusd!=null?(sg(Math.round(gSOX.tot.dusd))+'$<div style="font-size:10px;color:#8b949e">опц+перп+спот</div>'):'—',dc(gSOX?gSOX.tot.dusd:0))
  +tile('Vega',sg(t.v),cl(t.v))+tile('Γ$ на 1%',sg(t.g1),cl(t.g1))+tile('uPnL',sg(t.up)+'$',cl(t.up));
 const w=[];
 if(a.buf<1.5)w.push('<b class="neg">запас до MM '+a.buf+'× &lt; 1.5 — красная зона</b>');
 if(Math.abs(t.dusd)>dl)w.push('<span class="warn">|Δ$| '+Math.round(Math.abs(t.dusd))+' &gt; 20% эквити ('+Math.round(dl)+')</span>');
 if(t.eq!=null&&Math.abs(t.eq)>dl)w.push('<span class="warn">|Δ ETH-экв.| '+Math.round(Math.abs(t.eq))+' &gt; 20% эквити</span>');
 if(Math.abs(t.t)>th)w.push('<span class="warn">|Θ| '+Math.abs(t.t).toFixed(1)+' $/д &gt; 1% эквити ('+th.toFixed(1)+')</span>');
 $('kwarn').innerHTML=w.length?'⚠ '+w.join(' · '):'<span class="pos">в пределах правил риска (запас ≥ 1.5×, |Δ$| ≤ 20%, |Θ| ≤ 1% эквити)</span>';
 // payoff всего портфеля
 bookPay($('kpALL'),all,false);
 const N=all.now||[],mi=N.length?N.reduce((m,p)=>p[1]<m[1]?p:m,N[0]):null;
 $('kpaynote').innerHTML='Ось — ход ВСЕХ активов на один %; корреляции не учтены (грубый шок). Зелёная — на экспирации, синяя — T+0 (BS, IV без изменений).'+(mi?' Худшая точка T+0: <b class="neg">'+sg(mi[1])+'$</b> при '+(+mi[0]).toFixed(1)+'%':'')+(all.be_now&&all.be_now.length?' · безубыток T+0: '+all.be_now.map(v=>(+v).toFixed(1)+'%').join(', '):'');
 // группы
 let s='<tr><th>группа</th><th>Δ$</th><th>Θ/д</th><th>Vega</th><th>Γ$ 1%</th><th>uPnL</th></tr>';
 (all.summ||[]).forEach(r=>{s+=`<tr style="cursor:pointer" onclick="setPG('${r.key}');showTab('E')"><td>${r.name}</td><td class="${dc(r.dusd)}">${sg(r.dusd)}</td><td class="${cl(r.t)}">${sg(r.t)}</td><td>${sg(r.v)}</td><td>${sg(r.g1)}</td><td class="${cl(r.up)}">${sg(r.up)}</td></tr>`;});
 s+=`<tr style="border-top:1px solid #30363d"><td><b>Итого</b></td><td class="${dc(t.dusd)}"><b>${sg(t.dusd)}</b></td><td class="${cl(t.t)}">${sg(t.t)}</td><td>${sg(t.v)}</td><td>${sg(t.g1)}</td><td class="${cl(t.up)}">${sg(t.up)}</td></tr>`;
 $('ksumm').innerHTML=s;
 renderGam(d);
 // сетка payoff по группам: DOM пересобирается только при смене состава групп
 const ks=d.order.filter(k=>k!='ALL'&&d.groups[k]),key=ks.join(',');
 if(key!=KGK){KGK=key;$('kpgrid').innerHTML=ks.map(k=>`<div style="cursor:pointer" onclick="setPG('${k}');showTab('E')"><div class="ksub" id="kpt_${k}"></div><canvas id="kp_${k}" width="380" height="180"></canvas></div>`).join('');}
 ks.forEach(k=>{const g=d.groups[k],gt=g.tot,n=g.now||[],m=n.length?Math.min(...n.map(p=>p[1])):null;
  $('kpt_'+k).innerHTML=`<b style="color:#e6edf3">${g.name}</b>${g.main?' '+g.px:''} · Δ$ <span class="${dc(gt.dusd)}">${sg(gt.dusd)}</span> · Θ ${sg(gt.t)} · V ${sg(gt.v)}`+(m!=null?` · худш. T+0 <span class="${m<0?'neg':''}">${sg(Math.round(m))}</span>`:'');
  bookPay($('kp_'+k),g,true);});
 // стресс
 if(b&&b.stress){const st=b.stress,pn=v=>v<0?'neg':(v>0?'pos':'');let h='<tr><th>сценарий</th>'+st.moves.map(m=>'<th>'+(m>0?'+':'')+m+'%</th>').join('')+'</tr>';
  st.rows.forEach(r=>{h+='<tr><td>'+r.name+'</td>'+r.pnl.map(v=>'<td class="'+pn(v)+'">'+sg(v)+'</td>').join('')+'</tr>';});
  $('kstress').innerHTML=h;
  const wo=st.rows.reduce((w2,r)=>r.pnl.reduce((w3,v,i)=>v<w3.v?{v,n:r.name,m:st.moves[i]}:w3,w2),{v:Infinity}),room=a.eq-a.mm;
  $('kstnote').innerHTML='Корзина = '+(b.basket.join(', ')||'—')+'. T+0, BS при текущей IV, без пересчёта маржи. Эквити выше MM: <b>$'+room.toFixed(0)+'</b>'+(wo.v<0?' · худшая клетка <b class="'+(-wo.v>room?'neg':'warn')+'">'+sg(wo.v)+'$</b> — «'+wo.n+'» при '+(wo.m>0?'+':'')+wo.m+'%':'');}
 else{$('kstress').innerHTML='';$('kstnote').textContent='';}
 $('kposs').textContent='Позиции с греками ('+(all.pos||[]).length+')';
 if($('kposd').open)renderKPos(d);
}
let GSTEP='be', GAMB='ETH';
try{const v=localStorage.getItem('kgs');if(v)GSTEP=v;const w=localStorage.getItem('kgb');if(w)GAMB=w;}catch(e){}
function setGS(v){GSTEP=v;try{localStorage.setItem('kgs',v)}catch(e){} if(PORT){renderGam(PORT);if(TAB=='T')renderEthPort(PORT);}}
function setGB(v){GAMB=v;try{localStorage.setItem('kgb',v)}catch(e){} if(PORT)renderGam(PORT);}
const gfp=(v,S)=>v==null?'—':(S>=10000?v.toFixed(0):S>=100?v.toFixed(1):S>=1?v.toFixed(3):v.toFixed(5));
const gfq=v=>{const a=Math.abs(v);return a>=100?a.toFixed(0):a>=1?a.toFixed(2):a.toFixed(4)};
function renderGam(d){const G=d.groups.ALL.gam||[],eq=d.acct.eq||0,band=eq*0.2;
 $('kgsteps').innerHTML=[['0.5','0.5%'],['1','1%'],['1.5','1.5%'],['2','2%'],['be','= безубыток']].map(([k,l])=>`<span class="gbtn ${GSTEP==k?'on':''}" onclick="setGS('${k}')">${l}</span>`).join('');
 if(!G.length){$('kgam').innerHTML='<tr><td style="color:#8b949e">опционов нет</td></tr>';return}
 if(!G.find(r=>r.base==GAMB))GAMB=G[0].base;
 let s='<tr><th>актив</th><th>цена</th><th>Γ шт/$1</th><th>Γ$ 1%</th><th>Θ/д</th><th>Δ шт / Δ$</th><th>безуб. ход/д</th><th>IV/д · RV/д</th><th>вывод</th><th>ребаланс сейчас</th><th>шаг</th><th>объём шага</th><th>продать ≥</th><th>купить ≤</th><th>$ за цикл</th><th>циклов/д на Θ</th></tr>';
 G.forEach(r=>{const S=r.px,lg=r.g1>0,h=(GSTEP=='be'?(r.be||1):+GSTEP)/100,gg=r.g1/(S*S*0.01),q=Math.abs(gg)*S*h,cyc=100*Math.abs(r.g1)*h*h,fee=2*0.00036*q*S,net=cyc-fee;
  const ivd=r.iv?r.iv/Math.sqrt(365):null,rvd=r.rv?r.rv/Math.sqrt(365):null;
  let vv='—',vc='';
  if(r.be!=null&&rvd!=null){if(lg){vv=rvd>=r.be?'RV окупает Θ':'Θ дороже RV';vc=rvd>=r.be?'pos':'warn'}else{vv=rvd<=r.be?'продажа Γ окупается':'RV бьёт шорт Γ';vc=rvd<=r.be?'pos':'neg'}}
  const gold=r.base=='XAUT'&&d.groups.GOLD&&d.groups.GOLD.tot.hedge_xau!=null,hq=gold?d.groups.GOLD.tot.hedge_xau:-r.true,hd=gold?d.groups.GOLD.tot.dusd:r.dusd,hp=gold?'XAUUSDT (вся группа «Золото»)':r.perp;
  const out=Math.abs(hd)>band,rb=Math.abs(hd)<1?'—':`<span class="${out?'warn':''}">${hq<0?'продать':'купить'} ${gfq(hq)} ${hp}</span>${out?'':' <span style="color:#8b949e">(в коридоре)</span>'}`;
  const up=S*(1+h),dn=S*(1-h),act=lg?['продать','купить']:['купить','продать'];
  s+=`<tr style="cursor:pointer${r.base==GAMB?';background:#1f6feb22':''}" onclick="setGB('${r.base}')"><td><b>${r.base}</b></td><td>${gfp(S,S)}</td><td class="${cl(gg)}">${(+gg).toPrecision(3)}</td><td class="${cl(r.g1)}">${sg(r.g1)}</td><td class="${cl(r.t)}">${sg(r.t)}</td>`
   +`<td>${sg(r.true)} / ${sg(r.dusd)}$</td><td>${r.be==null?'—':r.be.toFixed(2)+'%'}</td><td>${ivd==null?'—':ivd.toFixed(2)}% · ${rvd==null?'—':rvd.toFixed(2)}%</td><td class="${vc}">${vv}</td><td style="text-align:left">${rb}</td>`
   +`<td>${(h*100).toFixed(2)}% (${gfp(S*h,S)})</td><td>${gfq(q)}</td><td>${act[0]} @ ${gfp(up,S)}</td><td>${act[1]} @ ${gfp(dn,S)}</td><td class="${lg?(net>0?'pos':'neg'):'neg'}">${lg?'+':'−'}${Math.abs(net).toFixed(2)}</td><td>${lg&&r.t<0&&net>0?(Math.abs(r.t)/net).toFixed(1):'—'}</td></tr>`;});
 $('kgam').innerHTML=s;
 const r=G.find(x=>x.base==GAMB),h=(GSTEP=='be'?(r.be||1):+GSTEP)/100;
 $('kgph').innerHTML=`<b style="color:#e6edf3">${r.base}</b>: Δ$ (синяя, левая ось) и Γ$ на 1% (жёлтая, правая) при сдвиге цены сейчас (T+0, BS по markIv); зелёные пунктиры — уровни сетки ±${(h*100).toFixed(2)}%`;
 $('kgeh').innerHTML=`<b style="color:#e6edf3">${r.base}</b>: Γ$ на 1% по экспирациям (зелёный — купленные, красный — проданные), под столбцами Θ/д`;
 gamProf($('kgprof'),r,h); gamExp($('kgexp'),r.exps||[]);
 $('kgnote').innerHTML='Γ шт/$1 — на сколько единиц актива меняется Δ при ходе цены на $1 (из позиций Bybit). Γ$ 1% = Γ·S²·0.01. <b>Безубыточный ход</b> = √(2|Θ| / (Γ$1%·100)) — дневной ход, при котором выигрыш гаммы ½·Γ·(ΔS)² равен тете за день; сравнивай с RV/д = RV30/√365 (и IV/д). Шаг сетки: объём = |Γ|·S·шаг (на столько сдвинется Δ), $ за цикл = Γ$1%·100·шаг² минус 2 мейкер-комиссии 0.036%. Длинная Γ: продаёшь на росте, откупаешь на падении; короткая — наоборот (догоняешь цену, это убыток за цикл). Ребаланс — перпом BASEUSDT до истинной Δ = 0 (опционы+перпы+спот+Earn); коридор ±20% эквити. Объёмы округляй до шага контракта. Профиль — BS без улыбки и без изменения IV.';
}
function gamProf(cv,r,h){const x=cv.getContext('2d'),W=cv.width,H=cv.height,pl=56,pr=56,pt=10,pb=20;x.clearRect(0,0,W,H);x.font='10px sans-serif';
 const P=r.prof||[];if(P.length<2){x.fillStyle='#8b949e';x.fillText('нет данных',10,20);return}
 const xs=P.map(p=>p[1]),x0=Math.min(...xs),x1=Math.max(...xs);
 const rg=a=>{let lo=Math.min(0,...a),hi=Math.max(0,...a);if(hi-lo<1e-9){hi+=1;lo-=1}const m=(hi-lo)*.08;return[lo-m,hi+m]};
 const L=rg(P.map(p=>p[2])),R=rg(P.map(p=>p[3]));
 const X=v=>pl+(v-x0)/(x1-x0)*(W-pl-pr),Y=(v,A)=>pt+(H-pt-pb)*(1-(v-A[0])/(A[1]-A[0]));
 const fm=v=>Math.abs(v)>=100?v.toFixed(0):v.toFixed(1);
 [0,.25,.5,.75,1].forEach(k=>{const yy=pt+(H-pt-pb)*k;x.strokeStyle='#21262d';x.beginPath();x.moveTo(pl,yy);x.lineTo(W-pr,yy);x.stroke();
  x.fillStyle='#58a6ff';x.fillText(fm(L[1]-(L[1]-L[0])*k),4,yy+3);x.fillStyle='#d29922';x.fillText(fm(R[1]-(R[1]-R[0])*k),W-pr+4,yy+3)});
 x.strokeStyle='#30363d';x.setLineDash([3,3]);x.beginPath();x.moveTo(pl,Y(0,L));x.lineTo(W-pr,Y(0,L));x.stroke();
 x.strokeStyle='#8b949e';x.beginPath();x.moveTo(X(r.px),pt);x.lineTo(X(r.px),H-pb);x.stroke();
 x.strokeStyle='#3fb95099';[r.px*(1+h),r.px*(1-h)].forEach(v=>{if(v<x0||v>x1)return;x.beginPath();x.moveTo(X(v),pt);x.lineTo(X(v),H-pb);x.stroke()});x.setLineDash([]);
 const ln=(i,A,c)=>{x.strokeStyle=c;x.lineWidth=1.6;x.beginPath();P.forEach((p,k)=>{const xx=X(p[1]),yy=Y(p[i],A);k?x.lineTo(xx,yy):x.moveTo(xx,yy)});x.stroke();x.lineWidth=1};
 ln(2,L,'#58a6ff');ln(3,R,'#d29922');
 x.fillStyle='#8b949e';x.fillText(gfp(x0,r.px)+' (−15%)',pl,H-5);x.fillText(gfp(r.px,r.px),X(r.px)-18,H-5);x.fillText(gfp(x1,r.px)+' (+15%)',W-pr-70,H-5);}
function gamExp(cv,E){const x=cv.getContext('2d'),W=cv.width,H=cv.height,pl=46,pt=12,pb=34;x.clearRect(0,0,W,H);x.font='10px sans-serif';
 if(!E.length){x.fillStyle='#8b949e';x.fillText('нет опционов',10,20);return}
 let lo=Math.min(0,...E.map(e=>e.g1s)),hi=Math.max(0,...E.map(e=>e.g1l));if(hi-lo<1e-9)hi=1;
 const Y=v=>pt+(H-pt-pb)*(1-(v-lo)/(hi-lo)),step=(W-pl-8)/E.length,bw=Math.min(36,step*0.6);
 x.strokeStyle='#30363d';x.beginPath();x.moveTo(pl,Y(0));x.lineTo(W-4,Y(0));x.stroke();
 x.fillStyle='#8b949e';x.fillText(hi.toFixed(0),4,Y(hi)+8);x.fillText(lo.toFixed(0),4,Y(lo));
 E.forEach((e,i)=>{const cx=pl+step*i+step/2;
  x.fillStyle='#3fb950';x.fillRect(cx-bw/2,Y(e.g1l),bw,Y(0)-Y(e.g1l));
  x.fillStyle='#f85149';x.fillRect(cx-bw/2,Y(0),bw,Y(e.g1s)-Y(0));
  x.fillStyle='#e6edf3';x.beginPath();x.arc(cx,Y(e.g1),3,0,7);x.fill();
  x.fillStyle='#8b949e';x.fillText(e.exp.replace(/26$|27$/,''),cx-16,H-20);x.fillStyle=e.t<0?'#f85149':'#3fb950';x.fillText('Θ '+(e.t>0?'+':'')+e.t.toFixed(1),cx-18,H-6);});
 x.fillStyle='#8b949e';x.fillText('● нетто',W-50,pt+2);}
function renderKPos(d){const P=d.groups.ALL.pos||[];
 let s='<tr><th>группа</th><th>контракт</th><th>ст</th><th>разм</th><th>Δ</th><th>Δ$</th><th>Γ</th><th>Θ/д</th><th>Vega</th><th>IV</th><th>uPnL</th></tr>';
 P.forEach(p=>{s+=`<tr><td style="color:#8b949e">${p.grp}</td><td>${p.sym}</td><td class="${p.side=='B'?'pos':'neg'}">${p.side}</td><td>${p.sz}</td><td>${p.d}</td><td>${p.dusd}</td><td>${p.g}</td><td class="${cl(p.t)}">${p.t}</td><td>${p.v}</td><td>${p.iv==null?'':p.iv+'%'}</td><td class="${cl(p.up)}">${p.up}</td></tr>`;});
 // спот и Earn в кошельке (ETH, купленный через ETHUSDC/ETHUSDT, MNT и т.п.) — тоже дельта
 (d.groups.ALL.rows||[]).filter(r=>(r.spot||r.earn)&&r.px).forEach(r=>{const q=+(r.spot+r.earn).toFixed(6);
  s+=`<tr><td style="color:#8b949e">${r.grp}</td><td>${r.base} спот${r.earn?' + Earn':''}</td><td class="pos">B</td><td>${q}</td><td>${q}</td><td>${Math.round(q*r.px)}</td><td>0</td><td>0</td><td>0</td><td></td><td>—</td></tr>`;});
 $('kpos').innerHTML=s;}
function bookPay(cv,g,sm){const x=cv.getContext('2d'),W=cv.width,H=cv.height,p=sm?30:46;x.clearRect(0,0,W,H);x.font=(sm?9:11)+'px sans-serif';
 const P=g.pay,N=g.now||[];if(!P||P.length<2){x.fillStyle='#8b949e';x.fillText('нет данных',10,20);return}
 const xs=P.map(a=>a[0]),ys=P.map(a=>a[1]).concat(N.map(a=>a[1]));
 const x0=Math.min(...xs),x1=Math.max(...xs);let y0=Math.min(0,...ys),y1=Math.max(0,...ys);if(y1==y0){y1+=1;y0-=1}
 const X=v=>p+(v-x0)/(x1-x0)*(W-2*p),Y=v=>H-p+10-(v-y0)/(y1-y0)*(H-2*p);
 x.strokeStyle='#30363d';x.beginPath();x.moveTo(p,Y(0));x.lineTo(W-p,Y(0));x.stroke();
 const cur=g.pct?0:g.px;
 x.strokeStyle='#8b949e';x.setLineDash([4,4]);x.beginPath();x.moveTo(X(cur),p-20);x.lineTo(X(cur),H-p+10);x.stroke();x.setLineDash([]);
 const line=(A,c,w)=>{if(!A.length)return;x.strokeStyle=c;x.lineWidth=w;x.beginPath();A.forEach((a,i)=>i?x.lineTo(X(a[0]),Y(a[1])):x.moveTo(X(a[0]),Y(a[1])));x.stroke();x.lineWidth=1;};
 line(N,'#58a6ff',sm?1.3:1.6);line(P,'#3fb950',sm?1.5:2);
 (g.be||[]).forEach(b=>{x.fillStyle='#8b949e';x.beginPath();x.arc(X(b),Y(0),sm?2:3,0,7);x.fill();if(!sm)x.fillText((g.pct?(+b).toFixed(1):+(+b).toPrecision(5))+(g.pct?'%':''),X(b)-16,Y(0)+16)});
 const fx=v=>Math.abs(v)>=100?Math.round(v):+(+v).toPrecision(4);
 x.fillStyle='#8b949e';x.fillText('$'+Math.round(y1),3,Y(y1)+8);x.fillText('$'+Math.round(y0),3,Y(y0));
 x.fillText(fx(x0)+(g.pct?'%':''),p-10,H-4);x.fillText(fx(x1)+(g.pct?'%':''),W-p-20,H-4);
 if(!sm){x.fillText(g.pct?'сейчас 0%':(g.main+' '+g.px),X(cur)-28,p-24);x.fillStyle='#3fb950';x.fillText('━ на экспирации',p+60,H-4);x.fillStyle='#58a6ff';x.fillText('━ сейчас (T+0)',p+170,H-4);}}
function bhSeries(B,w){const p0={};B.forEach(p=>{for(const k in (p[8]||{})) if(p0[k]==null&&p[8][k]>0)p0[k]=p[8][k];});
 const x0=p0.ETH,S=i=>B.filter(p=>p[i]!=null).map(p=>[p[0],p[i]]);
 const bk=p=>{let s=0,ws=0;for(const k in w){const a=(p[8]||{})[k];if(a>0&&p0[k]>0){s+=w[k]*(a/p0[k]-1);ws+=w[k];}}return ws?s/ws*100:null;};
 return {S,eth:x0?B.filter(p=>(p[8]||{}).ETH>0).map(p=>[p[0],(p[8].ETH/x0-1)*100]):[],bk:B.map(p=>[p[0],bk(p)]).filter(a=>a[1]!=null)};}
async function khtick(){ if(TAB!='K')return;
 let d; try{d=await (await fetch('/hist')).json()}catch(e){return}
 if(d.err){$('ktnote').textContent='история: ошибка '+d.err;return}
 KH=d; const B=d.bhist||[],w=d.basket_w||{},q=bhSeries(B,w);
 drawLines('kt1',[{n:'эквити $',c:'#e6edf3',pts:q.S(1),u:'$'},{n:'ETH',c:'#58a6ff',r:1,u:'%',pts:q.eth},{n:'корзина',c:'#d29922',r:1,u:'%',pts:q.bk}],{u:'%'});
 drawLines('kt2',[{n:'Δ$',c:'#58a6ff',pts:q.S(5)}],{u:'$'});
 drawLines('kt3',[{n:'IM %',c:'#d29922',pts:q.S(2)}],{u:'%'});
 drawLines('kt4',[{n:'MM %',c:'#f85149',pts:q.S(3)}],{u:'%'});
 drawLines('kt5',[{n:'Θ $/день',c:'#3fb950',pts:q.S(6)}],{u:'$'});
 drawLines('kt6',[{n:'вега $',c:'#bc8cff',pts:q.S(7)}],{u:'$'});
 $('ktnote').textContent='Корзина = '+Object.entries(w).map(([k,v])=>k+' '+(v*100).toFixed(0)+'%').join(', ')+(d.basket_eq?' (лонгов нет — равные веса)':' (лонговые крипто-ноги кроме ETH, веса по Δ$)')+'. Сбрасывается при перезапуске сервера.'+(d.sample_err?' ⚠ сэмплер: '+d.sample_err:'');}
// ===================== АРБИТРАЖ =====================
function setASub(k){ASUB=k;['mine','scan','gold','pairs'].forEach(x=>{$('as'+x[0].toUpperCase()+x.slice(1)).style.display=x==k?'':'none';$('asb_'+x).classList.toggle('on',x==k);});
 try{localStorage.setItem('asub',k)}catch(e){} if(k=='gold')gtick(); else if(k=='pairs')ptick(); else atick();}
async function atick(){ if(!(TAB=='A'&&(ASUB=='mine'||ASUB=='scan')))return;
 let d; try{d=await (await fetch('/arb')).json()}catch(e){$('upd').textContent='арбитраж: offline';return}
 if(d.err){$('upd').textContent='арбитраж: ошибка '+d.err;return}
 renderArb(d); $('upd').textContent='арбитраж · '+d.t;}
function renderArb(d){
 const T=d.tiles, f1=v=>v==null?'—':(+v).toFixed(1), f2=v=>v==null?'—':(+v).toFixed(2);
 const rc=T.resid<150?'pos':(T.resid<1000?'warn':'neg');
 $('atiles').innerHTML=tile('Остаточный риск $',f1(T.resid),rc)+tile('худший / лучший $',f1(T.min)+' / '+f1(T.max),'')
  +tile('Δ$ портфеля',sg(T.dusd),Math.abs(T.dusd)<150?'pos':'warn')+tile('Δ ETH-экв. (β)',sg(T.eq),Math.abs(T.eq)<150?'pos':'warn')
  +tile('Γ$ на 1%',sg(T.g1),'')+tile('Vega',sg(T.v),'')+tile('Θ / день',sg(T.t),cl(T.t))
  +tile('Фандинг $/день',sg(T.fund_day),cl(T.fund_day))+tile('связок / открытых',T.links+' / '+T.open,'');
 const B=d.bhist||[], w=d.basket_w||{}, q=bhSeries(B,w), S=q.S;
 drawLines('ach1',[{n:'эквити $',c:'#e6edf3',pts:S(1),u:'$'},
  {n:'ETH',c:'#58a6ff',r:1,u:'%',pts:q.eth},
  {n:'корзина',c:'#d29922',r:1,u:'%',pts:q.bk}],{u:'%'});
 drawLines('ach2',[{n:'IM %',c:'#d29922',pts:S(2)}],{u:'%'});
 drawLines('ach2b',[{n:'MM %',c:'#f85149',pts:S(3)}],{u:'%'});
 drawLines('ach2c',[{n:'IV ETH %',c:'#bc8cff',pts:S(4)}],{u:'%'});
 drawLines('ach3',[{n:'Δ$',c:'#58a6ff',pts:S(5)}],{u:'$'});
 drawLines('ach4',[{n:'Θ $/день',c:'#3fb950',pts:S(6)}],{u:'$'});
 drawLines('ach5',[{n:'вега $',c:'#bc8cff',pts:S(7)}],{u:'$'});
 $('achnote').textContent='История — в памяти сервера: точка раз в 20 с (фоновый сэмплер, пишет и при закрытой вкладке), до ~12 ч, сбрасывается при перезапуске. Корзина = '
  +Object.entries(w).map(([k,v])=>k+' '+(v*100).toFixed(0)+'%').join(', ')+(d.basket_eq?' (лонгов нет — равные веса)':' (лонговые крипто-ноги книги кроме ETH, веса по Δ$)')
  +'; % корзины = Σ вес × изменение цены. IV — ATM ближайшей экспирации ETH из датчика волы.'+(d.sample_err?' ⚠ сэмплер: '+d.sample_err:'');
 let h='<tr><th>связка</th><th>ноги</th><th>кол-во</th><th>uPnL $</th><th>смысл</th></tr>';
 (d.links||[]).forEach(l=>{h+=`<tr><td>${l.kind}</td><td style="text-align:left">${l.legs}</td><td>${l.q}</td><td class="${cl(l.up)}">${f2(l.up)}</td><td style="text-align:left;color:#8b949e">${l.note}</td></tr>`});
 if(!(d.links||[]).length)h+='<tr><td colspan=5 style="color:#8b949e">связок не найдено</td></tr>';
 $('alinks').innerHTML=h;
 h='<tr><th>нога</th><th>сторона</th><th>остаток / всего</th><th>Δ$</th><th>Θ/день</th><th>uPnL $</th><th>статус</th></tr>';
 (d.open||[]).forEach(o=>{const st=o.close_ok?'<span class="pos">можно закрыть в плюс</span>':(o.up<0?'<span class="neg">в минусе — не закрывать, хеджировать</span>':'<span style="color:#8b949e">—</span>');
  h+=`<tr><td>${o.sym}</td><td>${o.side}</td><td>${o.rem}${o.part?' / '+o.q:''}</td><td class="${cl(o.dusd)}">${sg(o.dusd)}</td><td class="${cl(o.t)}">${f2(o.t)}</td><td class="${cl(o.up)}">${f2(o.up)}</td><td style="text-align:left">${st}</td></tr>`});
 $('aopen').innerHTML=h;
 h='<tr><th>перп</th><th>ставка %</th><th>% годовых</th><th>$/день</th></tr>';
 (d.fund||[]).forEach(r=>{h+=`<tr><td>${r.sym}</td><td>${r.rate}</td><td>${r.apy}</td><td class="${cl(r.usd_day)}">${sg(r.usd_day)}</td></tr>`});
 h+=`<tr style="border-top:1px solid #30363d"><td><b>Итого</b></td><td></td><td></td><td class="${cl(T.fund_day)}"><b>${sg(T.fund_day)}</b></td></tr>`;
 $('afund').innerHTML=h;
 const s=d.scan;
 $('asbar').textContent=!s?(d.scan_busy?'скан идёт (первый раз ~30 с)…':'скан не готов'+(d.scan_err?': '+d.scan_err:'')):
  ('скан '+d.scan_age+' с назад · опционных пар просмотрено '+s.nscan+(s.est?' · ⚠ часть комиссий — оценка':' · комиссии из аккаунта')+(d.scan_busy?' · обновляется…':'')+(d.scan_err?' · ошибка: '+d.scan_err:''));
 if(!s)return;
 const ap=v=>`<td class="${cl(v)}"><b>${v==null?'—':v}</b></td>`, none=n=>`<tr><td colspan=${n} style="color:#8b949e">нет кандидатов с эджем &gt; 0</td></tr>`;
 h='<tr><th>актив</th><th>эксп.</th><th>дней</th><th>K</th><th>тип</th><th>хедж</th><th>эдж по ценам $/ед.</th><th>% год.</th><th>фандинг до эксп. $</th><th>с фандингом $</th><th>% год. с фанд.</th><th>объём</th><th>ноги</th></tr>';
 s.parity.forEach(r=>{h+=`<tr><td>${r.b}</td><td>${r.exp}</td><td>${r.dte}</td><td>${r.K}</td><td>${r.kind}</td><td>${r.hedge}${r.fund_only?' <span class="warn">только фандинг — не арбитраж</span>':''}</td><td class="${cl(r.edge)}">${r.edge}</td>${ap(r.apy)}<td class="${cl(r.fund)}">${r.fund}</td><td class="${cl(r.edge_f)}">${r.edge_f}</td><td class="${cl(r.apy_f)}">${r.apy_f}</td><td>${r.sz}</td><td style="text-align:left;color:#8b949e">${r.legs}</td></tr>`});
 if(!s.parity.length)h+=none(13); $('aspar').innerHTML=h;
 h='<tr><th>актив</th><th>эксп.</th><th>дней</th><th>K1–K2</th><th>тип</th><th>эдж $</th><th>эдж %</th><th>% год.</th></tr>';
 s.box.forEach(r=>{h+=`<tr><td>${r.b}</td><td>${r.exp}</td><td>${r.dte}</td><td>${r.K1}–${r.K2}</td><td>${r.kind}</td><td>${r.edge}</td><td>${r.pct}</td>${ap(r.apy)}</tr>`});
 if(!s.box.length)h+=none(8); $('asbox').innerHTML=h;
 h='<tr><th>актив</th><th>фьючерс</th><th>дней</th><th>направление</th><th>эдж %</th><th>% год.</th></tr>';
 s.cc.forEach(r=>{h+=`<tr><td>${r.b}</td><td>${r.fut}</td><td>${r.dte}</td><td>${r.kind}${r.need_spot?' <span class="warn">(нужен спот)</span>':''}</td><td class="${cl(r.pct)}">${r.pct}</td>${ap(r.apy)}</tr>`});
 $('ascc').innerHTML=h;
 h='<tr><th>актив</th><th>фьючерс</th><th>дней</th><th>направление</th><th>фандинг до эксп. %</th><th>эдж % (с фандингом)</th><th>% год.</th></tr>';
 s.cal.forEach(r=>{h+=`<tr><td>${r.b}</td><td>${r.fut}</td><td>${r.dte}</td><td>${r.kind}</td><td>${r.fund_pct}</td><td class="${cl(r.pct)}">${r.pct}</td>${ap(r.apy)}</tr>`});
 $('ascal').innerHTML=h;
 h='<tr><th>актив</th><th>ставка % (период)</th><th>% год. сейчас</th><th>% год. 7д</th><th>базис входа %</th><th>круг комиссий %</th><th>безубыток, дней</th><th>направление</th></tr>';
 s.carry.forEach(r=>{h+=`<tr${r.basket?'':' style="color:#8b949e"'}><td>${r.b}</td><td>${r.rate} (${r.ih}ч)</td>${ap(r.apy)}<td class="${cl(r.apy7)}">${r.apy7==null?'—':r.apy7}</td><td>${r.basis}</td><td>${r.fees_rt}</td><td>${r.be_days==null?'—':r.be_days}</td><td style="text-align:left">${r.dir}</td></tr>`});
 $('ascarry').innerHTML=h;
 h='<tr><th>актив</th><th>инверс % год.</th><th>линейный % год.</th><th>разница</th><th>направление</th></tr>';
 s.inv.forEach(r=>{h+=`<tr><td>${r.b}</td><td>${r.inv}</td><td>${r.lin}</td>${ap(r.diff)}<td style="text-align:left">${r.dir}</td></tr>`});
 $('asinv').innerHTML=h;}
function drawLines(id,ser,o){const c=$(id),x=c.getContext('2d'),W=c.width,H=c.height;x.clearRect(0,0,W,H);x.font='10px sans-serif';
 const all=ser.flatMap(s=>s.pts);if(all.length<2){x.fillStyle='#8b949e';x.fillText('история копится (точка раз в 20 с)…',10,20);return}
 const hasR=ser.some(s=>s.r&&s.pts.length),pl=52,pr=hasR?52:12,pt=8,pb=18;
 const rng=f=>{const v=ser.filter(f).flatMap(s=>s.pts.map(a=>a[1]));if(!v.length)return[0,1];let lo=Math.min(...v),hi=Math.max(...v);if(hi-lo<1e-9){lo-=1;hi+=1}const m=(hi-lo)*.08;return[lo-m,hi+m]};
 const L=rng(s=>!s.r),R=rng(s=>s.r),t0=Math.min(...all.map(a=>a[0])),t1=Math.max(...all.map(a=>a[0]));
 const X=t=>pl+(W-pl-pr)*(t-t0)/Math.max(t1-t0,1),Y=(v,r)=>{const[a,b]=r?R:L;return pt+(H-pt-pb)*(1-(v-a)/(b-a))};
 const fm=v=>Math.abs(v)>=100?v.toFixed(0):Math.abs(v)>=10?v.toFixed(1):v.toFixed(2);
 [0,.25,.5,.75,1].forEach(k=>{const yy=pt+(H-pt-pb)*k;x.strokeStyle='#21262d';x.beginPath();x.moveTo(pl,yy);x.lineTo(W-pr,yy);x.stroke();x.fillStyle='#8b949e';
  x.fillText(fm(L[1]-(L[1]-L[0])*k),4,yy+3);if(hasR)x.fillText(fm(R[1]-(R[1]-R[0])*k),W-pr+4,yy+3)});
 if(L[0]<0&&L[1]>0){x.strokeStyle='#30363d';x.setLineDash([3,3]);x.beginPath();x.moveTo(pl,Y(0));x.lineTo(W-pr,Y(0));x.stroke();x.setLineDash([])}
 let lx=pl+6;ser.forEach(s=>{if(!s.pts.length)return;x.strokeStyle=s.c;x.lineWidth=1.5;x.beginPath();s.pts.forEach((a,i)=>{const xx=X(a[0]),yy=Y(a[1],s.r);i?x.lineTo(xx,yy):x.moveTo(xx,yy)});x.stroke();x.lineWidth=1;
  const lv=s.pts[s.pts.length-1][1],su=s.u||o.u,lab=s.n+' '+(lv>0&&su=='%'?'+':'')+fm(lv)+(su=='%'?'%':'');x.fillStyle=s.c;x.fillRect(lx,pt+2,10,3);x.fillText(lab,lx+14,pt+8);lx+=x.measureText(lab).width+30});
 const hm=s=>new Date(s*1000).toTimeString().slice(0,5);x.fillStyle='#8b949e';x.fillText(hm(t0),pl,H-4);x.fillText(hm(t1),W-pr-30,H-4);}
try{const t=localStorage.getItem('gw');if(t&&['2d','30d','166d'].includes(t))setGW(t);}catch(e){}
// ===================== ОПЕРАТОР (вкладка O) =====================
let OP_TVW=null, OP_TV='NASDAQ:SOXL-NASDAQ:SOXM', OP_TVI='15';
try{OP_TV=localStorage.getItem('op_tv')||OP_TV;OP_TVI=localStorage.getItem('op_tvi')||OP_TVI;}catch(e){}
function opTvLoad(){if(window.TradingView){opTvDraw();return}
 if(document.getElementById('tvjs')){let w=setInterval(()=>{if(window.TradingView){clearInterval(w);opTvDraw();}},200);return;}
 const sc=document.createElement('script');sc.id='tvjs';sc.src='https://s3.tradingview.com/tv.js';sc.onload=opTvDraw;document.head.appendChild(sc);}
function opTvSet(s){OP_TV=s;try{localStorage.setItem('op_tv',OP_TV)}catch(e){} opTvDraw();}
function opTvInt(iv){OP_TVI=iv;try{localStorage.setItem('op_tvi',OP_TVI)}catch(e){} opTvDraw();}
function opTvDraw(){if(!window.TradingView||TAB!='O')return;const box=$('op_tv_box');if(!box)return;box.innerHTML='';
 const iv=[['1','1м'],['5','5м'],['15','15м'],['60','1ч'],['240','4ч'],['D','Д']];
 const bt=(s,l)=>`<span class="gbtn ${OP_TV==s?'on':''}" onclick="opTvSet('${s}')">${l}</span>`;
 $('op_tv_ctl').innerHTML='<b>TradingView</b> график: '+bt('NASDAQ:SOXL-NASDAQ:SOXM','SOXL − SOXM (спред)')+bt('NASDAQ:SOXL/NASDAQ:SOXM','SOXL / SOXM (отношение)')
  +bt('NASDAQ:SOXL','SOXL (две линии: + SOXM)')+bt('BYBIT:SOXLUSDT.P/BYBIT:SOXSUSDT.P','SOXL / SOXS (Bybit)')+bt('NASDAQ:SOXX','SOXX (индекс)')
  +' &nbsp; '+iv.map(([v,l])=>`<span class="gbtn ${OP_TVI==v?'on':''}" onclick="opTvInt('${v}')">${l}</span>`).join('')
  +' &nbsp; <input id="optvin" style="width:240px" value="'+OP_TV+'" onkeydown="if(event.key==\'Enter\')opTvSet(this.value)"> <span class="gbtn" onclick="opTvSet($(\'optvin\').value)">показать</span>';
 const st=(OP_TV=='NASDAQ:SOXL')?[{id:"Overlay@tv-basicstudies",inputs:{symbol:"NASDAQ:SOXM"}}]:['BB@tv-basicstudies'];
 OP_TVW=new TradingView.widget({container_id:'op_tv_box',symbol:OP_TV,interval:OP_TVI,autosize:true,theme:'dark',style:'1',locale:'ru',timezone:'Etc/UTC',hide_side_toolbar:false,allow_symbol_change:true,withdateranges:true,studies:st});}
function renderOp(d){
 if(!d||!d.groups||!d.acct)return;
 const a=d.acct,all=d.groups.ALL,t=all?all.tot:{},b=all?all.beta:{},eq=a.eq||0,dl=eq*0.2,fl=v=>Math.abs(v||0)<dl?'pos':'warn';
 // Block 1: Account tiles
 const mc=a.mm_pct>80?'neg':(a.mm_pct>50?'warn':'pos');
 const imc=a.im_pct>80?'neg':(a.im_pct>50?'warn':'pos');
 $('op_acct').innerHTML=tile('Эквити','$'+a.eq)+tile('Свободно','$'+a.av)+tile('IM',a.im_pct+'%',imc)+tile('MM',a.mm_pct+'%',mc)+tile('Запас',a.buf+'×',mc);
 // Block 2: Portfolio delta tiles
 const at=t;
 let pd=tile('Δ$ портфель',sg(at.dusd),fl(at.dusd));
 if(b&&b.eq_total!=null) pd+=tile('Δ ETH-экв.',sg(b.eq_total)+'$',fl(b.eq_total));
 if(b&&b.eq_crypto!=null) pd+=tile('Δ ETH-экв. (крипта)',sg(b.eq_crypto)+'$',fl(b.eq_crypto));
 pd+=tile('Θ /день портфель',sg(at.t),cl(at.t));
 $('op_pdelta').innerHTML=pd;
 // Block 3: Per-asset delta/theta tiles
 const ASSETS=['ETH','MNT','XAUT','SOXL','SOXS','BTC'];
 let asHtml='';
 const gSOXL=d.groups.SOXL||d.groups.SOX,gSOXS=d.groups.SOXS;
 ASSETS.forEach(name=>{
  let g=d.groups[name]||null;
  if(name=='XAUT')g=d.groups.XAUT||d.groups.GOLD||null;
  if(!g)return;
  const gt=g.tot;
  const neut=Math.abs(gt.dusd||0)<1?'нейтраль':((gt.dusd||0)<0?'купить $'+Math.abs(Math.round(gt.dusd||0)):'продать $'+Math.abs(Math.round(gt.dusd||0)));
  const br=d.groups.ALL.beta&&d.groups.ALL.beta.rows?(d.groups.ALL.beta.rows.find(r=>r.base==name||r.base==(name=='XAUT'?'XAUT':name))||{}):{};
  asHtml+=`<div class="tile" style="min-width:160px"><div class="lbl">${name}</div>`
   +`<div style="font-size:12px;margin-top:4px"><span style="color:#8b949e">Δ опц+перп:</span> <b>${sg(gt.od_perp)}</b></div>`
   +`<div style="font-size:12px"><span style="color:#8b949e">Δ +спот+Earn:</span> ${sg(gt.true)}</div>`
   +`<div style="font-size:12px"><span style="color:#8b949e">Δ$:</span> <span class="${fl(gt.dusd)}">${sg(gt.dusd)}</span></div>`
   +`<div style="font-size:12px"><span style="color:#8b949e">Θ/день:</span> <span class="${cl(gt.t)}">${sg(gt.t)}</span></div>`
   +`<div style="font-size:11px;color:#8b949e;margin-top:3px">Нейтрализация: <b class="${Math.abs(gt.dusd||0)>1?(gt.dusd||0)<0?'pos':'neg':''}">${neut}</b></div>`
   +(br.beta!=null?`<div style="font-size:11px;color:#8b949e">β к ETH: ${br.beta} · ETH-экв: ${sg(br.eq)}$</div>`:'')
   +`</div>`;
 });
 // Combined SOXL+SOXS pair tile
 if(d.groups.SOX){const sx=d.groups.SOX.tot;
  asHtml=`<div class="tile" style="min-width:160px;border-color:#1f6feb"><div class="lbl">SOXL+SOXS (пара)</div>`
   +`<div style="font-size:12px;margin-top:4px"><span style="color:#8b949e">Δ$:</span> <span class="${fl(sx.dusd)}">${sg(sx.dusd)}</span></div>`
   +`<div style="font-size:12px"><span style="color:#8b949e">Θ/день:</span> <span class="${cl(sx.t)}">${sg(sx.t)}</span></div>`
   +`<div style="font-size:11px;color:#8b949e;margin-top:3px">Нейтрализация: <b>${Math.abs(sx.dusd||0)<1?'нейтраль':((sx.dusd||0)<0?'купить $'+Math.abs(Math.round(sx.dusd||0)):'продать $'+Math.abs(Math.round(sx.dusd||0)))}</b></div>`
   +`</div>`+asHtml;
 }
 $('op_assets').innerHTML=asHtml;
 // Block 4: ETH block (inline, writing to op_ containers)
 const ge=d.groups.ETH;
 if(ge){const gte=ge.tot,v=ge.vol||{};
  $('op_eth_ov').innerHTML=tile('Δ ETH опц+перп+инверс',sg(gte.od_perp))+tile('Δ ETH +спот+Earn',sg(gte.true))+tile('Δ$ ETH',sg(gte.dusd),Math.abs(gte.dusd)>dl?'warn':'pos')
   +tile('Γ ETH',gte.g,cl(gte.g))+tile('Γ$ на 1%',sg(gte.g1),cl(gte.g1))+tile('Θ /д',sg(gte.t),Math.abs(gte.t)>eq*0.01?'warn':cl(gte.t))+tile('Vega',sg(gte.v),cl(gte.v))+tile('uPnL ETH',sg(gte.up)+'$',cl(gte.up))
   +(v.iv?tile('ATM IV / RV30',v.iv+'% / '+v.rv+'%',vcls(v.ratio))+tile('IV/RV',v.ratio||'—',vcls(v.ratio)):'');
  $('op_eth_vol').innerHTML=v.iv?('Датчик волы: <b class="'+vcls(v.ratio)+'">'+v.band+'</b> · ATM '+v.exp+' K'+v.K+' ('+v.dte+'д) · ранг сессии '+(v.rank==null?'—':v.rank+'%')+' · диапазон '+v.lo+'–'+v.hi+'% · премия IV−RV '+sg(v.prem)+'пт'):'';
  bookPay($('op_eth_pay'),ge,false);
  const N=ge.now||[],mi=N.length?N.reduce((m,p)=>p[1]<m[1]?p:m,N[0]):null;
  $('op_eth_paynote').innerHTML='Payoff ETH-группы: зелёная — на экспирации, синяя — T+0 (BS, IV без изменений), ось — цена ETH.'+(mi?' Худшая точка T+0: <b class="neg">'+sg(mi[1])+'$</b> при '+Math.round(mi[0]):'')+(ge.be&&ge.be.length?' · безубыток на экспирации: '+ge.be.join(', '):'')+(ge.be_now&&ge.be_now.length?' · T+0: '+ge.be_now.join(', '):'');
  // ETH book table (reuse logic from ttick)
  const nz=v=>v==null?'—':v;
  let bt='<tr><th>нога</th><th>тип</th><th>кол-во</th><th>дней</th><th>до страйка</th><th>в σ</th><th>ср. / марк</th><th>IV</th><th>Δ на 1</th><th>Δ$</th><th>Γ$ 1%</th><th>Θ/д</th><th>Vega</th><th>собрано</th><th>P&L</th></tr>';
  (ge.pos||[]).forEach(r=>{bt+=`<tr><td>${r.sym}</td><td class="${r.side=='B'?'pos':'neg'}">${r.kind||r.side}</td><td>${r.sz||r.q||''}</td><td>${nz(r.dte)}</td><td>${r.dist!=null?(r.dist>0?'+':'')+r.dist+'%':'—'}</td><td>${r.dsig!=null?nz(r.dsig):'—'}</td><td>${nz(r.avg)} / ${r.mark||''}</td><td>${r.iv!=null?r.iv+'%':'—'}</td><td>${r.d1||r.d||''}</td><td>${r.dusd||''}</td><td>${r.g1?sg(r.g1):'—'}</td><td>${r.t?sg(r.t):'—'}</td><td>${r.v?sg(r.v):'—'}</td><td>${r.capt!=null?r.capt+'%':'—'}</td><td class="${(r.up||0)>=0?'pos':'neg'}">${nz(r.up)}</td></tr>`;});
  if(!(ge.pos||[]).length)bt+='<tr><td colspan="15" style="color:#8b949e">позиций ETH нет</td></tr>';
  $('op_eth_bookt').innerHTML=bt;
  // gamma+scalp
  $('op_eth_gsteps').innerHTML='шаг сетки: '+[['0.5','0.5%'],['1','1%'],['1.5','1.5%'],['2','2%'],['be','= безубыток']].map(([k,l])=>`<span class="gbtn ${GSTEP==k?'on':''}" onclick="setGS('${k}')">${l}</span>`).join('');
  const gr=(d.groups.ALL.gam||[]).find(x=>x.base=='ETH');
  if(!gr){$('op_eth_gam').innerHTML='<tr><td style="color:#8b949e">опционов ETH нет</td></tr>';}
  else{const S=gr.px,lg=gr.g1>0,h=(GSTEP=='be'?(gr.be||1):+GSTEP)/100,gg=gr.g1/(S*S*0.01),q=Math.abs(gg)*S*h,cyc=100*Math.abs(gr.g1)*h*h,fee=2*0.00036*q*S,net=cyc-fee;
   const ivd=gr.iv?gr.iv/Math.sqrt(365):null,rvd=gr.rv?gr.rv/Math.sqrt(365):null;
   $('op_eth_gam').innerHTML='<tr><th>цена</th><th>Γ$ 1%</th><th>Θ/д</th><th>Δ шт/Δ$</th><th>шаг</th><th>$ за цикл</th></tr>'
    +`<tr><td>${gfp(S,S)}</td><td class="${cl(gr.g1)}">${sg(gr.g1)}</td><td class="${cl(gr.t)}">${sg(gr.t)}</td><td>${sg(gr.true)} / ${sg(gr.dusd)}$</td><td>${(h*100).toFixed(2)}%</td><td class="${lg?(net>0?'pos':'neg'):'neg'}">${lg?'+':'−'}${Math.abs(net).toFixed(2)}</td></tr>`;
   gamProf($('op_eth_gprof'),gr,h); gamExp($('op_eth_gexp'),gr.exps||[]);}
  // bought/sold
  const Be=ge.B,Se=ge.S,ce=ge.cov;
  $('op_eth_bs').innerHTML=(!Be.n&&!Se.n)?'<tr><td style="color:#8b949e">опционов нет</td></tr>':
   '<tr><th></th><th>ног</th><th>Γ</th><th>Δ шт</th><th>Γ$ 1%</th><th>Θ/д</th><th>Vega</th><th>Δ$</th></tr>'
   +[['Куплено',Be,'pos','neg','pos'],['Продано',Se,'neg','pos','neg']].map(([n,x,cg,ct,cv])=>`<tr><td>${n}</td><td>${x.n}</td><td class="${cg}">${x.g}</td><td>${x.d}</td><td class="${cg}">${x.g1}</td><td class="${ct}">${x.t}</td><td class="${cv}">${x.v}</td><td>${x.dusd}</td></tr>`).join('')
   +`<tr><td>Покрытие % (short÷long)</td><td></td><td></td><td></td><td class="${ce.g>100?'neg':'warn'}">${ce.g}</td><td class="pos">${ce.t}</td><td class="warn">${ce.v}</td><td></td></tr>`;
  const Re=(ge.rows||[]).filter(x=>x.base=='ETH');
  $('op_eth_spot').innerHTML='<tr><th>актив</th><th>цена</th><th>спот</th><th>Earn</th><th>перп Δ</th><th>истинная Δ</th><th>Δ$</th><th>нужный перп-хедж</th></tr>'
   +Re.map(x=>`<tr><td>${x.base}</td><td>${x.px}</td><td>${x.spot}</td><td>${x.earn}</td><td>${x.pd}</td><td>${x.true}</td><td>${sg(x.dusd)}</td><td class="warn">${Math.abs(x.dusd)<1?'нейтраль':(x.hedge>0?'ЛОНГ +':'ШОРТ ')+Math.abs(x.hedge)+' ETH'}</td></tr>`).join('');
  // ETH против корзины стресс
  const beta_all=d.groups.ALL.beta;
  if(beta_all&&beta_all.stress){const st=beta_all.stress,pn=v=>v<0?'neg':(v>0?'pos':'');let hh='<tr><th>сценарий</th>'+st.moves.map(m=>'<th>'+(m>0?'+':'')+m+'%</th>').join('')+'</tr>';
   st.rows.forEach(rr=>{hh+='<tr><td>'+rr.name+'</td>'+rr.pnl.map(v=>'<td class="'+pn(v)+'">'+sg(v)+'</td>').join('')+'</tr>';});
   $('op_eth_beta').innerHTML=hh;
   $('op_eth_betanote').innerHTML='Корзина = '+(beta_all.basket.join(', ')||'—')+'. Весь портфель, T+0, BS при текущей IV. Δ ETH-экв. портфеля '+sg(beta_all.eq_total)+'$.';}
 }
 // Orders (ETH only) - from cached /tail data via TAILd
 if(typeof TAILd!='undefined'&&TAILd){
  const O=(TAILd.orders||[]).filter(o=>/ETH/.test(o.sym)),bk={};
  (TAILd.book||[]).forEach(r=>{if(r.mark!=null)bk[r.sym.split(' ')[0]]=r.mark;});
  $('op_eth_ordh').textContent='Открытые заявки ETH ('+O.length+')'+(O.length?'':' — нет');
  let h=O.length?'<tr><th>инструмент</th><th>тип</th><th>сторона</th><th>цена</th><th>кол-во</th><th>сейчас марк</th><th>до цены</th></tr>':'';
  O.forEach(o=>{const m=bk[o.sym]||(TAILd.S||null),dist=m&&o.price?((o.price/m-1)*100):null;
   h+=`<tr><td><b>${o.sym}</b> <span style="color:#8b949e">${o.cat}</span></td><td>${o.type}</td><td class="${o.side=='Buy'?'pos':'neg'}">${o.side}</td><td>${o.price}</td><td>${o.qty}</td><td>${m??'—'}</td><td>${dist==null?'—':(dist>0?'+':'')+dist.toFixed(1)+'%'}</td></tr>`;});
  $('op_eth_ord').innerHTML=h;
 } else {$('op_eth_ordh').textContent='Открытые заявки ETH (загрузка…)';$('op_eth_ord').innerHTML='';}
 // ARB ETH - reuse from ARBd cache
 if(typeof ARBd!='undefined'&&ARBd){
  const isE=x=>/(^|[^A-Z])ETH/.test(x||''),f2=v=>v==null?'—':(+v).toFixed(2);
  let h='<tr><th>связка ETH</th><th>ноги</th><th>кол-во</th><th>uPnL $</th><th>смысл</th></tr>';const L=(ARBd.links||[]).filter(l=>isE(l.legs));
  L.forEach(l=>{h+=`<tr><td>${l.kind}</td><td style="text-align:left">${l.legs}</td><td>${l.q}</td><td class="${cl(l.up)}">${f2(l.up)}</td><td style="text-align:left;color:#8b949e">${l.note}</td></tr>`;});
  if(!L.length)h+='<tr><td colspan=5 style="color:#8b949e">связок с ETH нет</td></tr>'; $('op_eth_alinks').innerHTML=h;
  h='<tr><th>открытая нога ETH</th><th>сторона</th><th>остаток</th><th>Δ$</th><th>Θ/день</th><th>uPnL $</th></tr>';const O=(ARBd.open||[]).filter(o=>isE(o.sym));
  O.forEach(o=>{h+=`<tr><td>${o.sym}</td><td>${o.side}</td><td>${o.rem}</td><td class="${cl(o.dusd)}">${sg(o.dusd)}</td><td class="${cl(o.t)}">${f2(o.t)}</td><td class="${cl(o.up)}">${f2(o.up)}</td></tr>`;});
  if(!O.length)h+='<tr><td colspan=6 style="color:#8b949e">открытых ног ETH нет</td></tr>'; $('op_eth_aopen').innerHTML=h;
  h='<tr><th>перп ETH</th><th>ставка %</th><th>% годовых</th><th>$/день</th></tr>';const Fd=(ARBd.fund||[]).filter(r=>isE(r.sym));
  Fd.forEach(r=>{h+=`<tr><td>${r.sym}</td><td>${r.rate}</td><td>${r.apy}</td><td class="${cl(r.usd_day)}">${sg(r.usd_day)}</td></tr>`;});
  if(!Fd.length)h+='<tr><td colspan=4 style="color:#8b949e">перпов ETH нет</td></tr>'; $('op_eth_afund').innerHTML=h;
  $('op_eth_abar').textContent='арбитраж: данные доступны';
 } else {$('op_eth_abar').textContent='арбитраж: загрузка…';}
 // SOXL/SOXS pair tiles (from PRDATA)
 if(PRDATA){const pd2=PRDATA,V=(pd2.var&&pd2.var[PRM])||pd2.var.eq,th=pd2.theta||{},a=pd2.a,b2=pd2.b,c1=pd2.c1||{},c2=pd2.c2||{};
  const m=v=>v==null?'—':(v>0?'+':'')+(+v).toFixed(2)+'$',u=v=>(v<0?'−':'')+'$'+Math.abs(+v).toFixed(0);
  $('op_pr_top').innerHTML=tile('чистая ставка пары',u(V.net)+' ('+((V.net>0?'ставка РОСТ':'ставка ПАДЕНИЕ'))+')',Math.abs(V.net)<150?'pos':'warn')
   +tile('на +1% индекса',m(V.per1),cl(V.per1))
   +tile('Θ $/день пары',sg(th.tot)+'$',th.tot>0?'pos':'neg')
   +tile('захеджировано / всего','$'+(+V.hedged).toFixed(0)+' / $'+(+pd2.gross).toFixed(0));
  // C1 construction
  let h1='<h3>Конструкция 1: SOXL spot + SOXL put long + SOXL perp + SOXS perp</h3>';
  h1+='<div class="row">'+tile('Net Δ$',sg(c1.net_dusd)+'$',Math.abs(c1.net_dusd)<150?'pos':'warn')+tile('На +1% индекса',m(c1.per1),cl(c1.per1))+tile('Тета $/день',sg(c1.theta)+'$',c1.theta>0?'pos':'neg')+'</div>';
  $('op_prc1').innerHTML=h1;
  // C2 construction
  let h2='<h3>Конструкция 2: SOXL call long против SOXS perp</h3>';
  h2+='<div class="row">'+tile('Net Δ к индексу',m(c2.per1)+' на 1%',cl(c2.per1))+tile('Тета $/день',sg(c2.theta)+'$',c2.theta>0?'pos':'neg')+'</div>';
  $('op_prc2').innerHTML=h2;
 }
 // Load TradingView
 if(TAB=='O'&&!OP_TVW)opTvLoad();
}
let TAILd=null, ARBd=null;
async function opTick(){if(TAB!='O')return;
 try{
  const [pd,td,ad]=await Promise.all([fetch('/port').then(r=>r.json()),fetch('/tail').then(r=>r.json()),fetch('/arb').then(r=>r.json())]);
  if(pd&&!pd.err){PORT=pd;}
  if(td&&!td.err){TAILd=td;}
  if(ad&&!ad.err){ARBd=ad;}
  if(PORT)renderOp(PORT);
  if(TAB=='O')$('upd').textContent='оператор · '+(PORT&&PORT.t?PORT.t:new Date().toLocaleTimeString('ru'));
 }catch(e){if(TAB=='O')$('upd').textContent='оператор: offline';}
 if(TAB=='O')setTimeout(opTick,5000);}
try{let t=localStorage.getItem('tab');if(t=='P'){ASUB='pairs';t='A';} if(window.DESK_ONLY)t='D'; if(['D','E','A','G','T','F','R','C','I','S','O','L'].includes(t))showTab(t);}catch(e){if(window.DESK_ONLY)showTab('D');}
setInterval(dlive,2000);
if(window.DESK_ONLY){setInterval(dtick,15000);setInterval(pctick,5000);setInterval(bldTick,5000);setInterval(bldKline,60000);}else{setInterval(gtick,4000);setInterval(ttick,6000);setInterval(etick,20000);setInterval(ftick,15000);setInterval(rtick,15000);setInterval(rtkTick,4000);setInterval(grtick,15000);setInterval(gatick,15000);setInterval(prtick,10000);setInterval(pctick,5000);setInterval(lbtick,20000);setInterval(ctick,15000);setInterval(itick,10000);setInterval(ptick,6000);setInterval(atick,6000);setInterval(khtick,20000);khtick();setInterval(dtick,15000);}
if(!window.DESK_ONLY){tick();setInterval(tick,4000);}
</script></body></html>'''

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _write(self, body, ctype):
        try:
            self.send_response(200); self.send_header("Content-Type",ctype); self.send_header("Cache-Control","no-store"); self.end_headers(); self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass  # вкладку закрыли в момент отправки — не роняем сервер
    def do_GET(self):
        if self.path.startswith("/tail") or self.path.startswith("/pairs"):
            fn = tail_pairs.tail_snapshot if self.path.startswith("/tail") else tail_pairs.pairs_snapshot
            try: body = json.dumps(fn(sget, pub)).encode()
            except Exception as e: body = json.dumps({"err":str(e)}).encode()
            self._write(body, "application/json")
        elif self.path.startswith("/mexc"):
            try: body = json.dumps(capital_split.mexc_snapshot()).encode()
            except Exception as e: body = json.dumps({"err": str(e)[:200]}).encode()
            self._write(body, "application/json")
        elif self.path.startswith("/capital"):
            try: body = json.dumps(capital_split.capital_snapshot(_port())).encode()
            except Exception as e: body = json.dumps({"err":str(e)}).encode()
            self._write(body, "application/json")
        elif self.path.startswith("/pairlab"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try: body = json.dumps(pair_stats.lab(pub, (q.get("step") or ["0.05"])[0], (q.get("lot") or ["12"])[0], (q.get("win") or ["360"])[0])).encode()
            except Exception as e: body = json.dumps({"err":str(e)}).encode()
            self._write(body, "application/json")
        elif self.path.startswith("/pairchart"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try: body = json.dumps(pair_stats.series(pub, (q.get("bar") or ["10s"])[0])).encode()
            except Exception as e: body = json.dumps({"err":str(e)}).encode()
            self._write(body, "application/json")
        elif self.path.startswith("/pair"):
            try: body = json.dumps(pair_stats.pair_snapshot(sget, pub, tail_pairs.open_orders)).encode()
            except Exception as e: body = json.dumps({"err":str(e)}).encode()
            self._write(body, "application/json")
        elif self.path.startswith("/grid"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try: body = json.dumps(grid_stats.grid_snapshot(sget, tail_pairs.open_orders, (q.get("win") or ["today"])[0], pub, (q.get("f") or [""])[0])).encode()
            except Exception as e: body = json.dumps({"err":str(e)}).encode()
            self._write(body, "application/json")
        elif self.path.startswith("/altlab"):
            q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
            try: body = json.dumps(alt_lab.lab(sget, pub, q.get("sym", "INITUSDT"), q.get("mode", "core"), q.get("core", "spot"), q.get("lo"), q.get("hi"), q.get("n", 20), q.get("usd", 100), q.get("bars", 720))).encode()
            except Exception as e: body = json.dumps({"err":str(e)}).encode()
            self._write(body, "application/json")
        elif self.path.startswith("/income"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try: body = json.dumps(income.income_snapshot(sget, pub, (q.get("days") or ["30"])[0])).encode()
            except Exception as e: body = json.dumps({"err":str(e)}).encode()
            self._write(body, "application/json")
        elif self.path.startswith("/rufftick"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try: body = json.dumps(ruff_tick.series(sget, (q.get("coin") or ["SOXL"])[0], (q.get("bar") or ["10"])[0])).encode()
            except Exception as e: body = json.dumps({"err":str(e)}).encode()
            self._write(body, "application/json")
        elif self.path.startswith("/ruff"):
            try: body = json.dumps(ruff_scan.ruff_snapshot(sget, pub)).encode()
            except Exception as e: body = json.dumps({"err":str(e)}).encode()
            self._write(body, "application/json")
        elif self.path.startswith("/tradfi"):
            try: body = json.dumps(tradfi_scan.tradfi_snapshot(sget, pub)).encode()
            except Exception as e: body = json.dumps({"err":str(e)}).encode()
            self._write(body, "application/json")
        elif self.path.startswith("/gold"):
            try: body = json.dumps(gold_snapshot()).encode()
            except Exception as e: body = json.dumps({"err":str(e)}).encode()
            self._write(body, "application/json")
        elif self.path.startswith("/arb"):
            try: body = json.dumps(arb_book.arb_snapshot(sget, pub, _port)).encode()
            except Exception as e: body = json.dumps({"err":str(e)}).encode()
            self._write(body, "application/json")
        elif self.path.startswith("/hist"):
            try: body = json.dumps(arb_book.hist(_port())).encode()
            except Exception as e: body = json.dumps({"err":str(e)}).encode()
            self._write(body, "application/json")
        elif self.path.startswith("/builder"):
            try: body = json.dumps(desk.builder_snapshot(sget, pub)).encode()
            except Exception as e: body = json.dumps({"err":str(e)}).encode()
            self._write(body, "application/json")
        elif self.path.startswith("/bots"):
            try: body = json.dumps(desk.bots_snapshot(spost, sget)).encode()
            except Exception as e: body = json.dumps({"err":str(e)}).encode()
            self._write(body, "application/json")
        elif self.path.startswith("/bkline"):
            try:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                body = json.dumps(desk.builder_kline(pub, (qs.get("iv") or ["D"])[0])).encode()
            except Exception as e: body = json.dumps({"err":str(e)}).encode()
            self._write(body, "application/json")
        elif self.path.startswith("/desklive"):
            try: body = json.dumps(dict(desk.desk_live(sget, pub), build=_BUILD)).encode()
            except Exception as e: body = json.dumps({"err":str(e)}).encode()
            self._write(body, "application/json")
        elif self.path.startswith("/desk"):
            try: body = json.dumps(desk.desk_snapshot(sget, pub, _port())).encode()
            except Exception as e: body = json.dumps({"err":str(e)}).encode()
            self._write(body, "application/json")
        elif self.path.startswith("/port"):
            try: body = json.dumps(_port()).encode()
            except Exception as e: body = json.dumps({"err":str(e)}).encode()
            self._write(body, "application/json")
        elif self.path.startswith("/data"):
            try: body = json.dumps(snapshot()).encode()
            except Exception as e: body = json.dumps({"err":str(e)}).encode()
            self._write(body, "application/json")
        elif self.path.startswith("/old"):
            self._write(HTML.encode(), "text/html; charset=utf-8")
        else:
            self._write(DESK_HTML.encode(), "text/html; charset=utf-8")

# Главная страница с 16.11.2026 — только «Деск: ETH + SOX»; полная старая версия со всеми вкладками — /old
_BUILD = str(int(time.time()))   # метка запуска сервера: открытая вкладка деска перезагрузится, если сервер перезапущен (новый HTML)
DESK_HTML = (HTML.replace("<title>Книга Bybit</title>", "<title>Деск SOXL/SOXS</title>", 1)
             .replace("<body>", "<body><script>window.DESK_ONLY=1</script><style>#mainbar,#subbar{display:none}</style>", 1)
             .replace("<b>Книга Bybit — реалтайм</b>", "<b>Деск: спред SOXL / SOXS</b>", 1))

def _sampler():   # фоновая запись истории для графиков «Арбитража» (раз в 20 с, только чтение API)
    while True:
        try:
            arb_book.sample(_port()); arb_book._SAMPLE_ERR["e"] = None
        except Exception as e:
            arb_book._SAMPLE_ERR["e"] = str(e)[:200]
        time.sleep(20)

if __name__ == "__main__":
    import threading
    import sys as _sys
    if "--full" in _sys.argv:   # старые вкладки: история «Арбитража» и секундный сэмплер ершей (нагрузка на API)
        threading.Thread(target=_sampler, daemon=True).start()
        ruff_tick.start(pub)
    if "--full" in _sys.argv:   # сэмплер пары нужен только графикам спреда (вкладка S, деск при DKCHARTS=1)
        pair_stats.start(pub)
    def _mm_loop():   # монитор маркетмейкера опционов SOXL: журнал data/mm_quotes.log пишется и без открытой вкладки
        while True:
            try: desk.mm_tick(pub)
            except Exception: pass
            time.sleep(15)
    threading.Thread(target=_mm_loop, daemon=True).start()
    print("Book dashboard -> http://localhost:8899  (Ctrl+C остановить)")
    ThreadingHTTPServer(("127.0.0.1",8899), Handler).serve_forever()
