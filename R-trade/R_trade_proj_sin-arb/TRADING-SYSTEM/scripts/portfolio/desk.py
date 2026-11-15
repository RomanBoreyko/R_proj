"""Вкладка «Деск»: текущее положение портфеля — ETH и спред SOXL/SOXS. ТОЛЬКО ЧТЕНИЕ.

Собирает на одном экране то, на что Роман смотрит при работе (по сессии 15–16.11.2026):
  счёт: эквити, IM/MM, запас до ликвидации (эквити − MM), займ USDT;
  маржа по монетам из /v5/asset/portfolio-margin (MM, худший сценарий цена×IV, строка сценариев);
  дельта по монетам и готовые хедж-ордера перпом (ETH; SOX с бетой SOXS к SOXL; прочие монеты);
  ETH: ноги (Δ, вега, тета, бид/аск), «закрыть → маржа Bybit ±$», стресс цена×IV (Блэк–Шоулз),
       хвост проданных путов по датам экспираций (когда купленные истекают раньше проданных);
  SOX: ноги, Δ в SOXL-эквиваленте с β, псевдо-стренгл пары перпов (Γ$, распад/сутки, безубыток, VR), фандинг;
  тета по дням (до 10:00 Киева = 08:00 UTC, при неизменных цене и IV);
  плечи: маржа, займ, чистая дельта, брутто перпов, обязательства по проданным путам.
Маршрут: /desk → desk_snapshot(sget, pub, port). Кэш 12 с.
"""
import time, math, datetime

_C = {}
MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}

def ff(x):
    try: return float(x)
    except Exception: return 0.0

def _cached(k, ttl, fn):
    v = _C.get(k)
    if v and time.time() - v[0] < ttl: return v[1]
    r = fn(); _C[k] = (time.time(), r); return r

def _N(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def _bs(S, K, T, s, cp):
    if T <= 0 or s <= 0: return max(0.0, (S - K) if cp == "C" else (K - S))
    d1 = (math.log(S / K) + 0.5 * s * s * T) / (s * math.sqrt(T)); d2 = d1 - s * math.sqrt(T)
    return S * _N(d1) - K * _N(d2) if cp == "C" else K * _N(-d2) - S * _N(-d1)

def _exp_ts(code):
    d = int(code[:-5]); m = MON[code[-5:-2]]; y = 2000 + int(code[-2:])
    return datetime.datetime(y, m, d, 8, 0, tzinfo=datetime.timezone.utc).timestamp()

def _beta(pub):
    """β SOXS к SOXL, σ индекса в день, variance ratio — по дневным свечам перпов Bybit (кэш 1 ч)."""
    def f():
        def kl(s):
            return {int(x[0]): ff(x[4]) for x in pub("/v5/market/kline", {"category": "linear", "symbol": s, "interval": "D", "limit": "120"})["result"]["list"]}
        a, b = kl("SOXLUSDT"), kl("SOXSUSDT"); t = sorted(set(a) & set(b))
        ra = [math.log(a[t[i]] / a[t[i - 1]]) for i in range(1, len(t)) if a[t[i - 1]] > 0 and a[t[i]] > 0]
        rb = [math.log(b[t[i]] / b[t[i - 1]]) for i in range(1, len(t)) if b[t[i - 1]] > 0 and b[t[i]] > 0]
        n = min(len(ra), len(rb)); ra, rb = ra[-n:], rb[-n:]
        if n < 20: return {"beta": -0.94, "sig": 0.031, "vr": {}, "n": n}
        va = sum(x * x for x in ra) / n; cov = sum(x * y for x, y in zip(ra, rb)) / n
        ix = [x / 3 for x in ra]; v1 = sum(x * x for x in ix) / n; vr = {}
        for k in (2, 5, 10):
            bl = [sum(ix[i:i + k]) for i in range(0, n - k + 1, k)]
            if bl and v1 > 0: vr[k] = round(sum(x * x for x in bl) / len(bl) / k / v1, 2)
        return {"beta": round(cov / va, 3) if va else -0.94, "sig": round(math.sqrt(v1), 4), "vr": vr, "n": n}
    try: return _cached("beta", 3600, f)
    except Exception: return {"beta": -0.94, "sig": 0.031, "vr": {}, "n": 0}

def _xbeta(pub):
    """β ETH к SOXL (и обратно), корреляция — дневные (120) и часовые (720) свечи перпов Bybit. Кэш 1 ч."""
    def f():
        out = {}
        for lab, iv, n in (("day", "D", "120"), ("hour", "60", "720")):
            def kl(s):
                return {int(x[0]): ff(x[4]) for x in pub("/v5/market/kline", {"category": "linear", "symbol": s, "interval": iv, "limit": n})["result"]["list"]}
            a, b = kl("ETHUSDT"), kl("SOXLUSDT"); t = sorted(set(a) & set(b))
            re, rl = [], []
            for i in range(1, len(t)):
                if a[t[i - 1]] > 0 and a[t[i]] > 0 and b[t[i - 1]] > 0 and b[t[i]] > 0:
                    re.append(math.log(a[t[i]] / a[t[i - 1]])); rl.append(math.log(b[t[i]] / b[t[i - 1]]))
            m = len(re)
            if m < 20: continue
            me, ml = sum(re) / m, sum(rl) / m
            ve = sum((x - me) ** 2 for x in re) / m; vl = sum((x - ml) ** 2 for x in rl) / m
            cv = sum((x - me) * (y - ml) for x, y in zip(re, rl)) / m
            out[lab] = {"eth_on_soxl": round(cv / vl, 3) if vl else None, "soxl_on_eth": round(cv / ve, 3) if ve else None,
                        "corr": round(cv / math.sqrt(ve * vl), 3) if ve and vl else None, "n": m,
                        "sd_eth": round(math.sqrt(ve) * 100, 2), "sd_soxl": round(math.sqrt(vl) * 100, 2)}
        return out
    try: return _cached("xbeta", 3600, f)
    except Exception: return {}

def _curve(legs, lin, S0, lo, hi, now, n=121):
    """Кривые P&L книги от цены базового (как в конструкторе Bybit): сегодня, через 30 дней, на последней экспирации.
    Опционы — Блэк–Шоулз по mark IV (после своей экспирации — внутренняя стоимость), lin(S) — перпы/спот. P&L относительно текущей стоимости."""
    if not S0: return None
    legs = [l for l in legs if l["ets"] - now > 24 * 3600]          # ноги, истекающие в ближайшие сутки, не рисуем (как будто уже истекли)
    last = max((l["ets"] for l in legs), default=now)
    def value(S, at):
        v = 0.0
        for l in legs:
            v += l["q"] * _bs(S, l["K"], max(0.0, (l["ets"] - at) / 31557600), max(0.05, l["iv"]), l["cp"])
        return v + lin(S)
    xs = [S0 * (lo + (hi - lo) * i / (n - 1)) for i in range(n)]
    b0 = value(S0, now)
    hz = [("today", now), ("d30", min(now + 30 * 86400, last) if last > now else now), ("exp", last)]
    out = {"x": [round(x, 4) for x in xs], "S0": S0, "exp_label": time.strftime("%d.%m.%y", time.gmtime(last)) if last > now else ""}
    for k, at in hz: out[k] = [round(value(x, at) - b0, 1) for x in xs]
    return out

def _sox_fills(sget, days=7):
    """Мои исполнения SOXL/SOXS за N дней — маркеры на графике пары: [t_сек, 'L'|'S', 'B'|'S', цена, кол-во, мейкер]."""
    out = []; st = str(int((time.time() - days * 86400) * 1000))
    for sym, k in (("SOXLUSDT", "L"), ("SOXSUSDT", "S")):
        cur = ""
        for _ in range(6):
            q = {"category": "linear", "symbol": sym, "startTime": st, "limit": "100"}
            if cur: q["cursor"] = cur
            try: r = sget("/v5/execution/list", q)["result"]
            except Exception: break
            for x in r.get("list", []):
                if x.get("execType", "Trade") != "Trade": continue
                out.append([int(x["execTime"]) // 1000, k, "B" if x["side"] == "Buy" else "S", ff(x["execPrice"]), ff(x["execQty"]), 1 if x.get("isMaker") else 0])
            cur = r.get("nextPageCursor") or ""
            if not cur: break
    return sorted(out)[-600:]

def _sox_orders(sget):
    """Открытые заявки SOXL/SOXS — уровни сетки на графике: [sym 'L'|'S', 'B'|'S', цена, кол-во]."""
    out = []
    for sym, k in (("SOXLUSDT", "L"), ("SOXSUSDT", "S")):
        try:
            for x in sget("/v5/order/realtime", {"category": "linear", "symbol": sym, "limit": "50"})["result"]["list"]:
                if ff(x.get("price")): out.append([k, "B" if x["side"] == "Buy" else "S", ff(x["price"]), ff(x["qty"]) - ff(x.get("cumExecQty"))])
        except Exception:
            pass
    return out

def _pm(sget):
    r = sget("/v5/asset/portfolio-margin", {})["result"]
    out = {"wallet": {k: ff(v) for k, v in (r.get("wallet") or {}).items()}, "coins": {}}
    for a in r.get("assetPnlRange") or []:
        P = []
        for p in a.get("perpPositionPnlRanges") or []: P.append((p["symbolName"], p["pnlRanges"]))
        for ex in a.get("optionExpiryDatePnlRanges") or []:
            for p in ex.get("optionPositionPnlRanges") or []: P.append((p["symbolName"], p["pnlRanges"]))
        tot = a.get("totalPnlRanges", {}).get("ALL", {}).get("pnlRanges") or []
        out["coins"][a["baseCoin"]] = {
            "mm": round(ff(a.get("asset", {}).get("assetMM")), 2), "im": round(ff(a.get("asset", {}).get("assetIM")), 2),
            "cont": round(ff(a.get("contingency", {}).get("contingencyComponents")), 2),
            "move": a.get("maxLossPriceMove"), "iv": a.get("maxLossIvShock"),
            "row": [[ff(p["priceScale"]), round(min(ff(x) for x in p["pnls"]), 1)] for p in tot if p.get("pnls")],
            "P": P}
    return out

def _worst(P, excl=None):
    if not P: return 0.0
    sc = len(P[0][1]); best = 0.0
    for i in range(sc):
        m = max(len(pr[i]["pnls"]) for _, pr in P)
        for j in range(m):
            v = 0.0
            for n, pr in P:
                if n == excl: continue
                pn = pr[i]["pnls"]; v += ff(pn[j] if len(pn) > 1 else pn[0])
            best = min(best, v)
    return best

def _build(sget, pub, port):
    now = time.time()
    acct = port.get("acct", {}); rows = {r["base"]: r for r in port.get("groups", {}).get("ALL", {}).get("rows", [])}
    pm = _pm(sget); W = pm["wallet"]
    eq = W.get("equity") or ff(acct.get("eq")); mm = W.get("accountMM") or ff(acct.get("mm")); im = W.get("accountIM") or ff(acct.get("im"))
    # --- позиции
    opts = [p for p in sget("/v5/position/list", {"category": "option", "limit": "200"})["result"]["list"] if ff(p["size"])]
    lin = []
    for sc in ("USDT", "USDC"):
        try: lin += [p for p in sget("/v5/position/list", {"category": "linear", "settleCoin": sc, "limit": "200"})["result"]["list"] if ff(p["size"])]
        except Exception: pass
    try: inv = [p for p in sget("/v5/position/list", {"category": "inverse", "limit": "200"})["result"]["list"] if ff(p["size"])]
    except Exception: inv = []
    T = {}
    for b in sorted({p["symbol"].split("-")[0] for p in opts}):
        try:
            for x in _cached("otk:" + b, 10, lambda b=b: pub("/v5/market/tickers", {"category": "option", "baseCoin": b})["result"]["list"]): T[x["symbol"]] = x
        except Exception: pass
    LT = {}
    for s in {p["symbol"] for p in lin} | {"ETHUSDT", "SOXLUSDT", "SOXSUSDT"}:
        try: LT[s] = _cached("ltk:" + s, 10, lambda s=s: pub("/v5/market/tickers", {"category": "linear", "symbol": s})["result"]["list"][0])
        except Exception: pass
    legs = []
    for p in opts:
        s = p["symbol"]; parts = s.split("-"); b, e, k, cp = parts[0], parts[1], parts[2], parts[3]; t = T.get(s, {})
        q = ff(p["size"]) * (1 if p["side"] == "Buy" else -1)
        legs.append({"base": b, "sym": s.replace("-USDT", ""), "q": q, "K": ff(k), "cp": cp, "exp": e, "ets": _exp_ts(e),
                     "S": ff(t.get("underlyingPrice")) or ff(rows.get(b, {}).get("px")), "iv": ff(t.get("markIv")),
                     "mark": round(ff(p["markPrice"]), 4), "avg": round(ff(p["avgPrice"]), 4), "bid": ff(t.get("bid1Price")), "ask": ff(t.get("ask1Price")),
                     "d": round(ff(p.get("delta")), 4), "g": round(ff(p.get("gamma")), 5), "v": round(ff(p.get("vega")), 3), "th": round(ff(p.get("theta")), 3),
                     "upl": round(ff(p.get("unrealisedPnl")), 2), "val": round(q * ff(p["markPrice"]), 2)})
    perps = []
    for p in lin + inv:
        q = ff(p["size"]) * (1 if p["side"] == "Buy" else -1); s = p["symbol"]; lt = LT.get(s, {})
        fr = ff(lt.get("fundingRate")); fi = ff(lt.get("fundingIntervalHour")) or 8; mk = ff(p["markPrice"])
        is_inv = s.endswith("USD")
        notional = q if is_inv else q * mk
        perps.append({"sym": s, "q": q, "avg": round(ff(p["avgPrice"]), 2), "mark": mk, "usd": round(notional, 2),
                      "upl": round(ff(p.get("unrealisedPnl")) * (mk if is_inv else 1), 2), "real": round(ff(p.get("cumRealisedPnl")) * (mk if is_inv else 1), 2),
                      "fund": round(fr * 100, 4), "fund_day": round(-notional * fr * 24 / fi, 2), "inv": is_inv})
    # --- маржа по монетам
    margin = []
    for c, v in sorted(pm["coins"].items(), key=lambda kv: -kv[1]["mm"]):
        if v["mm"] < 0.5: continue
        margin.append({"coin": c, "mm": v["mm"], "im": v["im"], "cont": v["cont"], "move": v["move"], "iv": v["iv"], "row": v["row"],
                       "worst": round(_worst(v["P"]), 1)})
    worst_sum = round(sum(m["worst"] for m in margin), 1)
    # --- дельты и хеджи
    B = _beta(pub); beta = abs(B["beta"]) or 0.94
    eth = rows.get("ETH", {}); pxE = ff(eth.get("px")) or ff(LT.get("ETHUSDT", {}).get("markPrice"))
    dE = ff(eth.get("true")); pxL = ff(LT.get("SOXLUSDT", {}).get("markPrice")) or ff(rows.get("SOXL", {}).get("px"))
    pxS = ff(LT.get("SOXSUSDT", {}).get("markPrice")) or ff(rows.get("SOXS", {}).get("px"))
    dL = ff(rows.get("SOXL", {}).get("true")); qS = ff(rows.get("SOXS", {}).get("true"))
    soxEq = dL * pxL - qS * pxS * beta
    todo = []
    if abs(dE) >= 0.1 and pxE:
        todo.append({"lvl": "warn" if abs(dE) < 0.3 else "neg", "txt": "ETHUSDT %s %.2f — дельта ETH %+.2f ($%+.0f)" % ("Buy" if dE < 0 else "Sell", abs(dE), dE, dE * pxE)})
    if abs(soxEq) >= 40 and pxL:
        todo.append({"lvl": "warn" if abs(soxEq) < 100 else "neg", "txt": "SOXLUSDT %s %.2f — дельта SOX $%+.0f в SOXL-экв. (с SOXS × β %.2f)" % ("Buy" if soxEq < 0 else "Sell", abs(soxEq) / pxL, soxEq, beta)})
    for b, r in rows.items():
        if b in ("ETH", "SOXL", "SOXS"): continue
        if abs(ff(r.get("dusd"))) >= 50 and ff(r.get("px")):
            todo.append({"lvl": "", "txt": "%sUSDT %s %.4g — дельта %s $%+.0f" % (b, "Buy" if ff(r["dusd"]) < 0 else "Sell", abs(ff(r["true"])), b, ff(r["dusd"]))})
    # экспирации ближайших 36 ч
    soon = {}
    for l in legs:
        if 0 < l["ets"] - now < 36 * 3600: soon.setdefault((l["base"], l["exp"]), []).append(l)
    for (b, e), L in soon.items():
        dd = sum(l["d"] for l in L); hrs = (L[0]["ets"] - now) / 3600
        sym = {"ETH": "ETHUSDT"}.get(b, b + "USDT")
        todo.append({"lvl": "warn", "txt": "%s %s экспирирует через %.0f ч (10:00 Киев): %s; их дельта %+.2f уйдёт → после экспирации %s %s ~%.2f" % (
            b, e, hrs, ", ".join("%s ×%g" % (l["sym"].split("-", 2)[-1], l["q"]) for l in L), dd, sym, "Sell" if dd < 0 else "Buy", abs(dd))})
    room = eq - mm; im_pct = im / eq * 100 if eq else 0
    if im_pct > 50: todo.append({"lvl": "neg" if im_pct > 75 else "warn", "txt": "IM %.0f%% > 50%% — не добавлять ноги, резать плечо" % im_pct})
    # --- ETH: ноги, «закрыть → маржа», стресс, хвост
    pe = pm["coins"].get("ETH", {}); baseW = _worst(pe.get("P") or [])
    eth_legs = [l for l in legs if l["base"] == "ETH"]
    for l in eth_legs:
        nm = l["sym"] + "-USDT"
        l["dmm"] = round(_worst(pe.get("P") or [], nm) - baseW, 1) if pe else None   # + освободит, − добавит
        l["exit"] = round(l["q"] * (l["bid"] if l["q"] > 0 else l["ask"]), 2) if (l["bid"] if l["q"] > 0 else l["ask"]) else None
        l["tail"] = l["q"] < 0 and l["cp"] == "P"
    eth_perps = [p for p in perps if p["sym"].startswith("ETH")]
    spotE = ff(eth.get("spot"))
    def eth_book(S, dv):
        v = 0.0
        for l in eth_legs:
            Tt = (l["ets"] - now) / 31557600
            if Tt <= 0: continue
            v += l["q"] * _bs(S, l["K"], Tt, max(0.05, l["iv"] + dv), l["cp"])
        for p in eth_perps: v += p["q"] * (S / pxE - 1) if p["inv"] else p["q"] * (S - pxE)
        return v + spotE * (S - pxE)
    stress = []
    if pxE and eth_legs:
        b0 = eth_book(pxE, 0.0)
        for mv in (-0.7, -0.5, -0.4, -0.3, -0.2, -0.1, -0.05, 0.0, 0.05, 0.1, 0.2):
            stress.append({"mv": mv, "S": round(pxE * (1 + mv)), "v": [round(eth_book(pxE * (1 + mv), dv) - b0) for dv in (-0.15, 0.0, 0.15, 0.30)]})
    tail = []
    puts = [l for l in eth_legs if l["cp"] == "P"]
    for e in sorted({l["ets"] for l in puts}):
        alive = [l for l in puts if l["ets"] > e]
        lo = sum(l["q"] for l in alive if l["q"] > 0); sh = -sum(l["q"] for l in alive if l["q"] < 0)
        minK = min((l["K"] for l in alive if l["q"] < 0), default=None)
        pay = lambda S: round(sum(l["q"] * max(0.0, l["K"] - S) for l in alive))
        tail.append({"after": time.strftime("%d.%m.%y", time.gmtime(e)), "long": round(lo, 2), "short": round(sh, 2),
                     "naked": round(max(0.0, sh - lo), 2), "minK": minK, "pay50": pay(pxE * 0.5), "pay70": pay(pxE * 0.3)})
    short_notional = round(sum(-l["q"] * l["K"] for l in puts if l["q"] < 0))
    # --- SOX
    sox_legs = [l for l in legs if l["base"] in ("SOXL", "SOXS")]
    sox_perps = [p for p in perps if p["sym"] in ("SOXLUSDT", "SOXSUSDT")]
    Ld = sum(p["usd"] for p in sox_perps if p["sym"] == "SOXLUSDT"); Sd = sum(p["usd"] for p in sox_perps if p["sym"] == "SOXSUSDT")
    Npair = max(0.0, Ld) + max(0.0, Sd); sig = B["sig"]
    pseudo = {"N": round(Npair, 2), "gam": {str(x): round(4.5 * Npair * (x / 100) ** 2, 2) for x in (5, 10, 20)},
              "drag": round(-4.5 * Npair * sig * sig, 2), "be": {str(h): round(sig * math.sqrt(h) * 100, 1) for h in (1, 5, 10)},
              "vr": B["vr"], "sig": round(sig * 100, 2), "beta": B["beta"]}
    # β пары (лонг SOXL + лонг SOXS) к SOXL: доходность пары на 1 доходности SOXL; $ на 1% хода SOXL
    pair_beta = round((Ld + Sd * B["beta"]) / (Ld + Sd), 3) if (Ld + Sd) else None
    pair_usd1 = round((Ld + Sd * B["beta"]) / 100, 2)
    qL = sum(p["q"] for p in sox_perps if p["sym"] == "SOXLUSDT"); qSp = sum(p["q"] for p in sox_perps if p["sym"] == "SOXSUSDT")
    sox_lin = lambda S: (qL * (S - pxL) + (qSp * pxS * ((S / pxL) ** B["beta"] - 1) if pxL and pxS else 0.0))
    sox_curve = _curve([l for l in sox_legs if l["base"] == "SOXL"], sox_lin, pxL, 0.4, 1.6, now) if pxL else None
    sox = {"legs": sox_legs, "perps": sox_perps, "eq_usd": round(soxEq, 1), "pxL": pxL, "pxS": pxS, "beta": beta,
           "opt": {k: round(sum(l[k] for l in sox_legs), 3) for k in ("d", "g", "v", "th", "upl", "val")},
           "fund_day": round(sum(p["fund_day"] for p in sox_perps), 2), "pseudo": pseudo,
           "mm": {c: pm["coins"].get(c, {}).get("mm") for c in ("SOXL", "SOXS")},
           "hedge": round(-soxEq / pxL, 2) if pxL else None,
           "pair_beta": pair_beta, "pair_usd1": pair_usd1, "L": round(Ld, 2), "S": round(Sd, 2), "curve": sox_curve,
           "fills": _cached("soxfills", 30, lambda: _sox_fills(sget)), "orders": _sox_orders(sget),
           # из чего сложена дельта SOX: SOXL (перп + опционы + спот) − SOXS (перп + опционы + спот) × |β|, в SOXL-экв.
           "dparts": {"L_perp": round(qL, 4), "L_opt": round(sum(l["d"] for l in sox_legs if l["base"] == "SOXL"), 4),
                      "L_spot": round(ff(rows.get("SOXL", {}).get("spot")), 4), "L_tot": round(dL, 4), "L_usd": round(dL * pxL, 1),
                      "S_perp": round(qSp, 4), "S_opt": round(sum(l["d"] for l in sox_legs if l["base"] == "SOXS"), 4),
                      "S_spot": round(ff(rows.get("SOXS", {}).get("spot")), 4), "S_tot": round(qS, 4), "S_usd": round(-qS * pxS * beta, 1),
                      "shares": round(soxEq / pxL, 3) if pxL else None}}
    eth_lin = lambda S: sum((p["q"] * (S / pxE - 1) if p["inv"] else p["q"] * (S - pxE)) for p in eth_perps) + spotE * (S - pxE)
    eth_curve = _curve(eth_legs, eth_lin, pxE, 0.3, 1.6, now) if pxE else None
    xb = _xbeta(pub)
    # --- тета по дням (до 08:00 UTC)
    st = datetime.datetime.now(datetime.timezone.utc); b0d = st.replace(hour=8, minute=0, second=0, microsecond=0)
    if b0d.timestamp() <= now: b0d += datetime.timedelta(days=1)
    pts = [now] + [(b0d + datetime.timedelta(days=i)).timestamp() for i in range(14)]
    grp = lambda b: "ETH" if b == "ETH" else ("SOX" if b in ("SOXL", "SOXS") else "прочее")
    def val(at, g):
        return sum(l["q"] * _bs(l["S"], l["K"], max(0.0, (l["ets"] - at) / 31557600), l["iv"], l["cp"]) for l in legs if grp(l["base"]) == g and l["S"] and l["iv"])
    theta = []
    for i in range(1, len(pts)):
        rr = {g: round(val(pts[i], g) - val(pts[i - 1], g), 2) for g in ("ETH", "SOX", "прочее")}
        rr["d"] = time.strftime("%d.%m %a", time.localtime(pts[i])); rr["tot"] = round(sum(rr[g] for g in ("ETH", "SOX", "прочее")), 2)
        theta.append(rr)
    theta_tot = {str(dd): round(sum(val(now + dd * 86400, g) - val(now, g) for g in ("ETH", "SOX", "прочее")), 1) for dd in (30, 60, 90)}
    fund_all = round(sum(p["fund_day"] for p in perps), 2)
    # --- плечи
    stable = port.get("stable") or {}
    net = []
    for b, r in rows.items():
        if abs(ff(r.get("dusd"))) >= 3: net.append({"b": b, "true": round(ff(r.get("true")), 4), "usd": round(ff(r.get("dusd")), 1), "x": round(abs(ff(r.get("dusd"))) / eq, 2) if eq else None})
    lev = {"im_pct": round(im_pct, 1), "mm_pct": round(mm / eq * 100, 1) if eq else None, "room": round(room, 1), "room_x": round(eq / mm, 2) if mm else None,
           "usdt": round(ff(stable.get("USDT")), 2), "usdc": round(ff(stable.get("USDC")), 2),
           "gross": round(sum(abs(p["usd"]) for p in perps), 1), "gross_x": round(sum(abs(p["usd"]) for p in perps) / eq, 2) if eq else None,
           "short_puts": short_notional, "short_puts_x": round(short_notional / eq, 1) if eq else None,
           "naked_now": round(max(0.0, -sum(l["q"] for l in puts if l["q"] < 0) - sum(l["q"] for l in puts if l["q"] > 0)), 2),
           "sox_eq": round(soxEq, 1), "net": sorted(net, key=lambda x: -abs(x["usd"]))}
    gap = next((t for t in tail if t["pay50"] < 0), None)
    if gap: todo.append({"lvl": "warn", "txt": "Хвост ETH: после экспирации %s купленные путы уже истекли, проданных больше на %.1f (страйк %s). Выплата на экспирации: ETH −50%% → $%+d, −70%% → $%+d при эквити $%d. Закрыть/перекатить проданные до этой даты" % (gap["after"], gap["naked"], int(gap["minK"]) if gap["minK"] else "—", gap["pay50"], gap["pay70"], eq)})
    return {"t": time.strftime("%H:%M:%S"), "acct": {"eq": round(eq, 2), "im": round(im, 2), "mm": round(mm, 2), "im_pct": round(im_pct, 1), "mm_pct": lev["mm_pct"],
                                                     "room": round(room, 1), "avail": round(W.get("availableBalance", ff(acct.get("av"))), 2)},
            "todo": todo, "margin": margin, "worst_sum": worst_sum,
            "eth": {"px": pxE, "delta": round(dE, 4), "dusd": round(dE * pxE, 1), "spot": spotE, "legs": eth_legs, "perps": eth_perps,
                    "theta": round(sum(l["th"] for l in eth_legs), 2), "vega": round(sum(l["v"] for l in eth_legs), 2),
                    "mm": pe.get("mm"), "move": pe.get("move"), "iv": pe.get("iv"), "stress": stress, "tail": tail, "short_notional": short_notional,
                    "curve": eth_curve},
            "sox": sox, "xbeta": xb, "theta": theta, "theta_tot": theta_tot, "fund_day": fund_all, "lev": lev}

def desk_snapshot(sget, pub, port):
    v = _C.get("desk")
    if v and time.time() - v[0] < 12: return v[1]
    r = _build(sget, pub, port); _C["desk"] = (time.time(), r)
    return r
