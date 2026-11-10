# -*- coding: utf-8 -*-
"""Real-time дашборд книги Bybit: MM/IM, греки, бета-линия во времени (TV-стиль),
payoff, профиль греков и структурный срез пут/колл (страйк×время).
Бэкенд читает .bybit/credentials.json (read-only). Запуск: python book_dashboard.py -> http://localhost:8899
"""
import json, hmac, hashlib, time, re, datetime, math, urllib.request, urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tail_pairs, portfolio_book, arb_book, tradfi_scan, ruff_scan

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
.ksub{font-size:11px;color:#8b949e;margin:6px 0 0}</style></head><body>
<div class="hd"><b>Книга Bybit — реалтайм</b><span id="upd">загрузка...</span></div>
<div style="margin:6px 0 4px"><span class="gbtn on" id="tbK" onclick="showTab('K')">Книга</span><span class="gbtn" id="tbT" onclick="showTab('T')">ETH: продажа края</span><span class="gbtn" id="tbF" onclick="showTab('F')">TradFi-сканер</span><span class="gbtn" id="tbR" onclick="showTab('R')">Ерши MEXC↔Bybit</span><span class="gbtn" id="tbMore" onclick="bookMore()">Ещё ▾</span><span id="moreBox" style="display:none"><span class="gbtn" id="tbE" onclick="showTab('E')">Портфель: детали</span><span class="gbtn" id="tbA" onclick="showTab('A')">Арбитраж</span><span class="gbtn" id="tbG" onclick="showTab('G')">Золото: опционы</span></span></div>
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

<div id="tabR" style="display:none">
<h3>Ерши MEXC ↔ Bybit: мелкие монеты-перпы на обеих биржах — расхождение цены и фандинга, болтанка, стакан, комиссии. ТОЛЬКО МОНИТОРИНГ</h3>
<div class="row" id="rtop"></div>
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
<div id="tordh" style="color:#8b949e;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;margin-top:4px"></div><table id="tord"></table>
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
function showTab(t){TAB=t;['K','E','A','G','T','F','R'].forEach(k=>{$('tab'+k).style.display=t==k?'':'none';$('tb'+k).classList.toggle('on',t==k);});
 const inMore=['E','A','G'].includes(t); $('tbMore').classList.toggle('on',inMore); if(inMore)$('moreBox').style.display='';
 try{localStorage.setItem('tab',t)}catch(e){} if(t=='K'){if(PORT)renderBook(PORT);khtick();} if(t=='E'&&PORT)renderPort(PORT); if(t=='G')gtick(); if(t=='T'){ttick();if(PORT)renderEthPort(PORT);etick();} if(t=='F')ftick(); if(t=='R')rtick(); if(t=='A')setASub(ASUB);}
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
 // открытые заявки (лимитки): ETH сверху, остальные ниже
 {const O=(d.orders||[]).slice().sort((a,b)=>(/ETH/.test(b.sym)?1:0)-(/ETH/.test(a.sym)?1:0)),bk={};(d.book||[]).forEach(r=>{if(r.mark!=null)bk[r.sym.split(' ')[0]]=r.mark;});
  $('tordh').textContent='Открытые заявки ('+O.length+')'+(O.length?'':' — нет');
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
 R.slice(0,120).forEach((r,i)=>{h+=`<tr><td>${i+1}</td><td><b>${r.coin}</b></td><td>${r.bb_px}</td><td>${r.mx_px}</td><td class="${Math.abs(r.diff)>0.5?'warn':''}">${sg(r.diff)}%</td><td>${r.bb_sp} / ${r.mx_sp}</td><td>${sg(r.edge)}%</td><td class="${r.net>0?'pos':''}">${sg(r.net)}%</td><td class="${cl(r.f8)}">${sg(r.f8)}%</td><td class="${r.net8>0?'pos':'neg'}"><b>${sg(r.net8)}%</b></td><td style="text-align:left;color:#8b949e">${r.net8>0?r.dir:''}</td>`
  +`<td>${r.fb} / ${r.fm}${r.fi_m==null?'*':''}</td><td class="${Math.abs(r.fdiff)>0.1?'warn':''}">${sg(r.fdiff)}</td><td>${fm(r.bturn)} / ${fm(r.mturn)}</td><td>${r.rng??'—'}%</td><td>${r.chop??'—'}</td><td>${r.cross??'—'}</td>`
  +`<td>${pc(r.bfee[0])}/${pc(r.bfee[1])} · ${pc(r.mfee[0])}/${pc(r.mfee[1])}${r.mzero?' <span class="pos">0-fee</span>':''}</td></tr>`;});
 if(!R.length)h+='<tr><td colspan="18" style="color:#8b949e">нет пар</td></tr>';
 $('rtab').innerHTML=h; if(TAB=='R')$('upd').textContent='ерши · '+d.t;}
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
function bookMore(){const b=$('moreBox');b.style.display=b.style.display=='none'?'':'none';}
function renderBook(d){
 const a=d.acct,all=d.groups.ALL,t=all.tot,eq=a.eq||0,b=all.beta;
 if(TAB=='K')$('upd').textContent='книга · '+d.t;
 const bc=a.buf<1.5?'neg':(a.buf<1.8?'warn':'pos'),dl=eq*0.2,th=eq*0.01;
 const dc=v=>Math.abs(v||0)<=dl?'pos':'warn';
 $('ktiles').innerHTML=tile('Эквити','$'+a.eq)+tile('Свободно','$'+a.av,a.av<100?'warn':'')+tile('IM',a.im_pct+'%',bc)+tile('MM',a.mm_pct+'%',bc)+tile('Запас',a.buf+'x',bc)
  +tile('Δ$',sg(t.dusd),dc(t.dusd))+(t.eq!=null?tile('Δ ETH-экв.',sg(t.eq)+'$',dc(t.eq)):'')
  +tile('Θ /д',sg(t.t),Math.abs(t.t)>th?'warn':cl(t.t))+tile('Vega',sg(t.v),cl(t.v))+tile('Γ$ на 1%',sg(t.g1),cl(t.g1))+tile('uPnL',sg(t.up)+'$',cl(t.up));
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
try{let t=localStorage.getItem('tab');if(t=='P'){ASUB='pairs';t='A';} if(['E','A','G','T','F','R'].includes(t))showTab(t);}catch(e){}
setInterval(gtick,4000);setInterval(ttick,6000);setInterval(etick,20000);setInterval(ftick,15000);setInterval(rtick,15000);setInterval(ptick,6000);setInterval(atick,6000);setInterval(khtick,20000);khtick();
tick();setInterval(tick,4000);
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
        elif self.path.startswith("/port"):
            try: body = json.dumps(_port()).encode()
            except Exception as e: body = json.dumps({"err":str(e)}).encode()
            self._write(body, "application/json")
        elif self.path.startswith("/data"):
            try: body = json.dumps(snapshot()).encode()
            except Exception as e: body = json.dumps({"err":str(e)}).encode()
            self._write(body, "application/json")
        else:
            self._write(HTML.encode(), "text/html; charset=utf-8")

def _sampler():   # фоновая запись истории для графиков «Арбитража» (раз в 20 с, только чтение API)
    while True:
        try:
            arb_book.sample(_port()); arb_book._SAMPLE_ERR["e"] = None
        except Exception as e:
            arb_book._SAMPLE_ERR["e"] = str(e)[:200]
        time.sleep(20)

if __name__ == "__main__":
    import threading
    threading.Thread(target=_sampler, daemon=True).start()
    print("Book dashboard -> http://localhost:8899  (Ctrl+C остановить)")
    ThreadingHTTPServer(("127.0.0.1",8899), Handler).serve_forever()
