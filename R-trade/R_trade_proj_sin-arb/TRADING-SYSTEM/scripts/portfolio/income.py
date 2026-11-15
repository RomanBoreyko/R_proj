"""Источники дохода (вкладка «Доходы»). ТОЛЬКО ЧТЕНИЕ.

Журнал операций UTA /v5/account/transaction-log за N дней (окна по 7 дней, курсор) → деньги по категориям:
  ETH-перпы и фьючерсы (ETHUSDT, ETHPERP, инверс ETHUSD, датированные ETHUSDT-…), опционы по монетам
  (проданные / купленные — по первой сделке символа в окне), прочие перпы, спот;
  отдельно: комиссии, фандинг, ликвидации, проценты по займу, пополнения (в доход НЕ входят).
Деньги = поле change журнала (реализованный P&L перпов, премии опционов, поставка, фандинг, комиссии уже внутри),
монеты переводятся в $ по текущей цене. Для ещё открытых опционов к деньгам прибавляется текущая стоимость
(mark × размер) — получается полный итог символа. Ограничение: позиции, открытые ДО начала окна, видны
только частью (например, откуп проданного раньше опциона без исходной премии) — поэтому есть окна 7/30/90 дней.
Маршрут: /income?days=30 → income_snapshot(sget, pub, days). Кэш 10 мин на окно.
"""
import time, collections

_C = {}
SKIP = {"TRANSFER_IN", "TRANSFER_OUT", "ONCHAINEARN_SUBSCRIPTION", "ONCHAINEARN_REDEMPTION", "CURRENCY_BUY", "CURRENCY_SELL",
        "EXEMPTED_INTEREST"}

def ff(x):
    try: return float(x)
    except Exception: return 0.0

def _log(sget, days):
    now = int(time.time() * 1000); D = 86400000; t = now - days * D; rows = []
    while t < now:
        e = min(t + 7 * D - 1, now); cur = ""
        for _ in range(200):
            p = {"accountType": "UNIFIED", "startTime": str(t), "endTime": str(e), "limit": "50"}
            if cur: p["cursor"] = cur
            r = sget("/v5/account/transaction-log", p)["result"]; rows += r["list"]; cur = r.get("nextPageCursor") or ""
            if not cur: break
        t = e + 1
    return rows

def _px(pub, coins):
    px = {"USDT": 1.0, "USDC": 1.0, "USD": 1.0}
    for c in coins:
        if c in px: continue
        try: px[c] = ff(pub("/v5/market/tickers", {"category": "spot", "symbol": c + "USDT"})["result"]["list"][0]["lastPrice"])
        except Exception: px[c] = 0.0
    return px

def _optval(sget):
    v = {}
    for bc in ("ETH", "BTC", "SOL", "XRP", "MNT", "XAUT", "QQQ", "DOGE"):
        try:
            for p in sget("/v5/position/list", {"category": "option", "baseCoin": bc, "limit": "200"})["result"]["list"]:
                if ff(p["size"]) > 0: v[p["symbol"]] = ff(p["markPrice"]) * ff(p["size"]) * (1 if p["side"] == "Buy" else -1)
        except Exception:
            pass
    return v

def _cat(sym, first, cat=""):
    if cat == "option" or sym.count("-") >= 3:
        return "Опционы " + sym.split("-")[0] + (" · проданные" if first == "Sell" else " · купленные")
    if cat == "spot":
        return "Спот (переоценка по текущей цене)"
    if sym.startswith("ETH"):
        return "ETH перпы и фьючерсы"
    return "Прочие перпы (TradFi, альты)"

def _build(sget, pub, days):
    rows = sorted(_log(sget, days), key=lambda r: int(r["transactionTime"]))
    px = _px(pub, {r["currency"] for r in rows}); usd = lambda r, k="change": ff(r.get(k)) * px.get(r["currency"], 0.0)
    ov = _optval(sget)
    S = {}; by_type = collections.defaultdict(float); daily = collections.defaultdict(lambda: collections.defaultdict(float))
    fees = fund = liq = interest = dep = 0.0; liq_ev = collections.Counter(); dep_l = []
    for r in rows:
        ty = r["type"]; s = r.get("symbol") or ""; ch = usd(r); day = time.strftime("%Y-%m-%d", time.localtime(int(r["transactionTime"]) / 1000))
        if ty in ("TRANSFER_IN", "TRANSFER_OUT"):
            dep += ch; dep_l.append([day, r["currency"], round(ff(r.get("change")), 4), round(ch, 2)]); continue
        if ty in SKIP: continue
        if ty == "INTEREST": interest += ch; daily[day]["Проценты по займу"] += ch; continue
        by_type[ty] += ch; fees += usd(r, "fee"); fund += usd(r, "funding")
        if ty == "LIQUIDATION": liq += ch; liq_ev[time.strftime("%d.%m %H:00", time.localtime(int(r["transactionTime"]) / 1000))] += 1
        if not s: continue
        ck = (s, r.get("category") or ""); d = S.get(ck)
        if d is None: d = S[ck] = {"sym": s, "c": ck[1], "cash": 0.0, "fee": 0.0, "fund": 0.0, "liq": 0.0, "n": 0, "first": None, "last": day}
        d["cash"] += ch; d["fee"] += usd(r, "fee"); d["fund"] += usd(r, "funding"); d["n"] += 1; d["last"] = day
        if ty == "LIQUIDATION": d["liq"] += ch
        if d["first"] is None and ty == "TRADE": d["first"] = r.get("side")
        daily[day][_cat(s, d["first"], d["c"])] += ch
    syms = []
    for (s, c0), d in S.items():
        v = ov.get(s, 0.0) if c0 == "option" else 0.0; cat = _cat(s, d["first"], c0)
        syms.append({"sym": s + (" (спот)" if c0 == "spot" else ""), "cat": cat, "cash": round(d["cash"], 2), "open": round(v, 2), "tot": round(d["cash"] + v, 2),
                     "fee": round(d["fee"], 2), "fund": round(d["fund"], 3), "liq": round(d["liq"], 2), "n": d["n"],
                     "is_open": c0 == "option" and s in ov, "last": d["last"]})
    cats = collections.defaultdict(lambda: {"tot": 0.0, "cash": 0.0, "open": 0.0, "fee": 0.0, "liq": 0.0, "n": 0, "win": 0.0, "loss": 0.0})
    for x in syms:
        c = cats[x["cat"]]; c["tot"] += x["tot"]; c["cash"] += x["cash"]; c["open"] += x["open"]; c["fee"] += x["fee"]; c["liq"] += x["liq"]; c["n"] += 1
        if x["tot"] >= 0: c["win"] += x["tot"]
        else: c["loss"] += x["tot"]
    cats = sorted([dict(name=k, **{a: round(b, 2) for a, b in v.items()}) for k, v in cats.items()], key=lambda c: -c["tot"])
    total = round(sum(x["tot"] for x in syms) + interest, 2)
    days_l = sorted(daily)
    return {"t": time.strftime("%H:%M:%S"), "days": days, "rows": len(rows), "total": total,
            "fees": round(fees, 2), "funding": round(fund, 2), "liq": round(liq, 2), "interest": round(interest, 2),
            "deposits": round(dep, 2), "dep_list": dep_l, "liq_events": sorted(liq_ev.items()),
            "by_type": {k: round(v, 2) for k, v in by_type.items()}, "cats": cats,
            "syms": sorted(syms, key=lambda x: x["tot"]),
            "daily": [[d, {k: round(v, 2) for k, v in daily[d].items()}] for d in days_l],
            "open_val": round(sum(ov.values()), 2)}

_BUSY, _ERR = set(), {}

def _run(sget, pub, days):
    try:
        _C[days] = (time.time(), _build(sget, pub, days)); _ERR.pop(days, None)
    except Exception as e:
        _ERR[days] = str(e)[:200]
    finally:
        _BUSY.discard(days)

def income_snapshot(sget, pub, days=30):
    """Журнал за 30 дней — ~8 тыс. строк, ~1 мин. Считается фоном; пока считается — отдаём прошлый результат или busy."""
    import threading
    days = max(1, min(180, int(days or 30)))
    v = _C.get(days)
    if (not v or time.time() - v[0] > 600) and days not in _BUSY:
        _BUSY.add(days); threading.Thread(target=_run, args=(sget, pub, days), daemon=True).start()
    if v: return dict(v[1], busy=days in _BUSY, age=round(time.time() - v[0]))
    return {"busy": True, "days": days, "err": _ERR.get(days)}
