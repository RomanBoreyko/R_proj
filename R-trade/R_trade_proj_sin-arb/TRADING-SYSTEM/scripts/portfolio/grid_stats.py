"""Блок «Сетки» во вкладке «Ерши»: как реально зарабатывают сеточные/скальп-заявки на линейных перпах. ТОЛЬКО ЧТЕНИЕ.

Символ попадает в блок, если за окно у него ≥ 20 исполнений или сейчас ≥ 10 открытых заявок (SOXL, SOXS и т.п.).
По символу:
  исполнения /v5/execution/list (число, доля мейкера, оборот, комиссии, по часам),
  закрытия /v5/position/closed-pnl (реализованный P&L, сколько в плюс/минус, лучшая/худшая),
  позиция /v5/position/list (остаток, средняя, нереализованный P&L),
  заявки tail_pairs.open_orders (покупки/продажи, $; «сверх позиции» — объём, который ОТКРОЕТ позицию в другую
  сторону или нарастит её: в Portfolio Margin такие заявки держат маржу как исполненные).
Настоящий итог сетки = реализованный + нереализованный по остатку: сетка закрывает только прибыльные куски,
а то, что ушло против, копится в остатке.
Маршрут: /grid?win=today|24h|7d → grid_snapshot(sget, open_orders, win). Кэш 20 с.
"""
import time, datetime, collections

_C = {}

def ff(x):
    try: return float(x)
    except Exception: return 0.0

def _pages(sget, path, p, maxp=60):
    out, cur = [], ""
    for _ in range(maxp):
        q = dict(p)
        if cur: q["cursor"] = cur
        r = sget(path, q)["result"]; out += r.get("list", []); cur = r.get("nextPageCursor") or ""
        if not cur: break
    return out

def _start(win):
    now = time.time()
    if win == "24h": return now - 86400
    if win == "7d": return now - 7 * 86400 + 60
    d = datetime.datetime.now(); return datetime.datetime(d.year, d.month, d.day).timestamp()

def _build(sget, open_orders, win):
    st = int(_start(win) * 1000)
    ex = _pages(sget, "/v5/execution/list", {"category": "linear", "startTime": str(st), "limit": "100"})
    cp = _pages(sget, "/v5/position/closed-pnl", {"category": "linear", "startTime": str(st), "limit": "100"})
    pos = {}
    for sc in ("USDT", "USDC"):
        try:
            for p in sget("/v5/position/list", {"category": "linear", "settleCoin": sc, "limit": "200"})["result"]["list"]:
                if ff(p["size"]) > 0: pos[p["symbol"]] = p
        except Exception:
            pass
    try: oo = [o for o in open_orders(sget) if o["cat"] == "linear"]
    except Exception: oo = []
    E = collections.defaultdict(list); P = collections.defaultdict(list); O = collections.defaultdict(list)
    for x in ex:
        if x.get("execType", "Trade") == "Trade": E[x["symbol"]].append(x)
    for x in cp: P[x["symbol"]].append(x)
    for o in oo: O[o["sym"]].append(o)
    syms = {s for s in E if len(E[s]) >= 20} | {s for s in O if len(O[s]) >= 10} | set(pos)      # + любые открытые позиции
    rows = []
    for s in sorted(syms):
        e, c, o, p = E.get(s, []), P.get(s, []), O.get(s, []), pos.get(s)
        pn = [ff(x["closedPnl"]) for x in c]
        size = ff(p["size"]) * (1 if p and p["side"] == "Buy" else -1) if p else 0.0
        mark = ff(p["markPrice"]) if p else (ff(e[0]["execPrice"]) if e else (o[0]["price"] if o else 0))
        buy = [x for x in o if x["side"] == "Buy"]; sell = [x for x in o if x["side"] == "Sell"]
        bq, sq = sum(x["qty"] for x in buy), sum(x["qty"] for x in sell)
        # сверх позиции: если всё исполнится в одну сторону — какой станет позиция
        after_buy, after_sell = size + bq, size - sq
        over = max(abs(after_buy), abs(after_sell)) - abs(size)
        hrs = collections.Counter(time.strftime("%H", time.localtime(int(x["execTime"]) / 1000)) for x in e)
        real = round(sum(pn), 2); upl = round(ff(p.get("unrealisedPnl")), 2) if p else 0.0
        rows.append({"sym": s, "n_ex": len(e), "maker": round(sum(1 for x in e if x.get("isMaker")) / len(e) * 100) if e else None,
                     "turn": round(sum(ff(x["execValue"]) for x in e), 2), "fee": round(sum(ff(x["execFee"]) for x in e), 4),
                     "n_cl": len(pn), "win": sum(1 for v in pn if v > 0), "loss": sum(1 for v in pn if v < 0),
                     "real": real, "best": round(max(pn), 2) if pn else None, "worst": round(min(pn), 2) if pn else None,
                     "size": size, "avg": ff(p["avgPrice"]) if p else None, "mark": mark, "upl": upl, "tot": round(real + upl, 2),
                     "inv_usd": round(abs(size) * mark, 2),
                     "o_buy": len(buy), "o_buy_q": round(bq, 4), "o_buy_usd": round(sum(x["qty"] * x["price"] for x in buy), 2),
                     "o_buy_px": [min((x["price"] for x in buy), default=None), max((x["price"] for x in buy), default=None)],
                     "o_sell": len(sell), "o_sell_q": round(sq, 4), "o_sell_usd": round(sum(x["qty"] * x["price"] for x in sell), 2),
                     "o_sell_px": [min((x["price"] for x in sell), default=None), max((x["price"] for x in sell), default=None)],
                     "after_buy": round(after_buy, 4), "after_sell": round(after_sell, 4), "over_usd": round(over * mark, 2),
                     "ro": sum(1 for x in o if x.get("ro")), "hours": sorted(hrs.items())})
    tot = {k: round(sum(r[k] for r in rows), 2) for k in ("real", "upl", "tot", "turn", "fee", "inv_usd", "over_usd")}
    tot["n_ex"] = sum(r["n_ex"] for r in rows)
    return {"t": time.strftime("%H:%M:%S"), "win": win, "rows": rows, "tot": tot}

CORE_BASES = {"ETH", "BTC", "SOL", "MNT", "XAUT", "PAXG"}         # не шиткоины: эти монеты живут в «ETH» и «Арб-порте»
SOX = {"SOXLUSDT", "SOXSUSDT"}

def _tickers(pub):
    v = _C.get("tk")
    if v and time.time() - v[0] < 10: return v[1]
    r = {x["symbol"]: x for x in pub("/v5/market/tickers", {"category": "linear"})["result"]["list"]}; _C["tk"] = (time.time(), r)
    return r

def _filter(d, f, pub):
    """f='sox' — только SOXL/SOXS; f='alt' — только крипто-шиткоины (крипто-перпы USDT без ETH/BTC/SOL/MNT и без TradFi)."""
    if not f: return d
    if f == "sox": keep = lambda s: s in SOX
    else:
        try:
            import ruff_scan
            crypto = set(ruff_scan._bb_inst(pub)) if pub else set()
        except Exception:
            crypto = set()
        keep = lambda s: s in crypto and s.endswith("USDT") and s[:-4] not in CORE_BASES
    rows = [dict(r) for r in d["rows"] if keep(r["sym"])]
    if pub:
        try:
            tk = _tickers(pub)
            for r in rows:
                t = tk.get(r["sym"], {}); fr = ff(t.get("fundingRate")); fi = ff(t.get("fundingIntervalHour")) or 8
                r["fund"] = round(fr * 100, 4); r["fund_h"] = fi
                r["fund_day"] = round(-r["size"] * r["mark"] * fr * 24 / fi, 2)       # $/сутки по текущей ставке: + получаю, − плачу
                r["chg24"] = round(ff(t.get("price24hPcnt")) * 100, 1)
        except Exception:
            pass
    tot = {k: round(sum(r[k] for r in rows), 2) for k in ("real", "upl", "tot", "turn", "fee", "inv_usd", "over_usd")}
    tot["n_ex"] = sum(r["n_ex"] for r in rows)
    tot["fund_day"] = round(sum(r.get("fund_day", 0.0) for r in rows), 2)
    return dict(d, rows=rows, tot=tot, f=f)

def grid_snapshot(sget, open_orders, win="today", pub=None, f=None):
    win = win if win in ("today", "24h", "7d") else "today"
    v = _C.get(win)
    if v and time.time() - v[0] < 20: r = v[1]
    else: r = _build(sget, open_orders, win); _C[win] = (time.time(), r)
    return _filter(r, f if f in ("sox", "alt") else None, pub)
