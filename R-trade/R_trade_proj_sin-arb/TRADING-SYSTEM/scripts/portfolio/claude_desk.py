# -*- coding: utf-8 -*-
"""Деск Claude (спредер) — отдельная страница /cl, данные /cljson.

Собран по тому, о чём Роман спрашивает чаще всего (22.11.2026):
  «статус» → маржа (IM/MM как у Bybit, запас, доступно) и дельта книги;
  «за нейтралить» → дельта по частям (перп / опционы / прочие монеты) и что её сдвинет;
  «риск до вторника» → P&L по сценариям до ближайшей экспирации против запаса;
  скрины ордеров → что сделает каждый открытый ордер (откуп / новая продажа / не reduce-only);
  «что можно откупить по нормальной цене» → проданные: бид/аск/марк и в плюс ли от входа;
  «хуясе фандинг» → фандинг текущий и по факту за 24 ч по монетам;
  «насколько цена ходит в день» → ATR и ход закрытие-к-закрытию.
Только чтение API. Использует готовые снимки desk.py (кэш) — лишних запросов почти нет.
"""
import time, math
import threading, collections
import desk, capital_split, income

ff = desk.ff
_C = {}

def _cached(k, ttl, fn):
    v = _C.get(k)
    if v and time.time() - v[0] < ttl: return v[1]
    r = fn(); _C[k] = (time.time(), r); return r

def _opt_tickers(pub):
    def f():
        out = {}
        for b in ("SOXL", "SOXS"):
            try:
                for x in pub("/v5/market/tickers", {"category": "option", "baseCoin": b})["result"]["list"]:
                    out[x["symbol"].replace("-USDT", "")] = x
            except Exception:
                pass
        return out
    return _cached("otk", 5, f)

def _orders(sget):
    def f():
        out = []
        for cat, ex in (("option", {}), ("linear", {"settleCoin": "USDT"}), ("linear", {"settleCoin": "USDC"}), ("spot", {})):
            try:
                for o in sget("/v5/order/realtime", dict(category=cat, limit="50", **ex))["result"]["list"]:
                    out.append(dict(o, _cat=cat))
            except Exception:
                pass
        return out
    return _cached("ord", 4, f)

def _atr(pub, sym="SOXLUSDT"):
    def f():
        try:
            k = pub("/v5/market/kline", {"category": "linear", "symbol": sym, "interval": "D", "limit": "32"})["result"]["list"][::-1][:-1]
            k = [(ff(a[2]), ff(a[3]), ff(a[4])) for a in k]
            tr, cc = [], []
            for i in range(1, len(k)):
                h, l, c = k[i]; pc = k[i - 1][2]
                tr.append(max(h - l, abs(h - pc), abs(l - pc)) / pc * 100); cc.append(abs(c / pc - 1) * 100)
            m = lambda a, n: round(sum(a[-n:]) / len(a[-n:]), 1) if a else None
            return {"atr14": m(tr, 14), "atr30": m(tr, 30), "cc14": m(cc, 14), "cc30": m(cc, 30), "last7": [round(x, 1) for x in tr[-7:]]}
        except Exception as e:
            return {"err": str(e)}
    return _cached("atr" + sym, 600, f)

def _execs(sget):
    def f():
        out = []
        for cat in ("option", "linear"):
            try:
                for x in sget("/v5/execution/list", {"category": cat, "limit": "20"})["result"]["list"]:
                    if x.get("execType") == "Funding": continue      # фандинг — не сделка
                    out.append({"ts": ff(x["execTime"]) / 1000, "sym": x["symbol"].replace("-USDT", ""), "side": x["side"],
                                "q": ff(x["execQty"]), "px": ff(x["execPrice"]), "fee": ff(x.get("execFee")), "type": x.get("execType")})
            except Exception:
                pass
        return sorted(out, key=lambda e: -e["ts"])[:8]
    return _cached("exe", 10, f)

_COST = {"d1": None, "d7": None, "t1": 0, "t7": 0, "busy": False, "loan": None, "tl": 0}

def _sum_costs(rows, px):
    """Издержки из журнала операций: комиссии (fee>0 — расход), фандинг (funding<0 — заплатил), проценты (INTEREST)."""
    fee = collections.defaultdict(float); fund = collections.defaultdict(float); intr = 0.0
    for r in rows:
        k = px.get(r.get("currency"), 1.0); ty = r.get("type")
        if ty == "INTEREST": intr += ff(r.get("change")) * k; continue
        if ty == "TRADE": fee[r.get("category") or "?"] -= ff(r.get("fee")) * k
        if ty == "SETTLEMENT" or ff(r.get("funding")): fund[(r.get("symbol") or "?").replace("USDT", "")] += ff(r.get("funding")) * k
    fees = sum(fee.values()); fd = sum(fund.values())
    return {"fee": round(fees, 2), "fee_by": {c: round(v, 2) for c, v in fee.items() if abs(v) >= 0.01},
            "fund": round(fd, 2), "fund_by": {c: round(v, 2) for c, v in sorted(fund.items(), key=lambda x: x[1]) if abs(v) >= 0.01},
            "int": round(intr, 3), "tot": round(fees + fd + intr, 2)}

def _costs_bg(sget, pub):
    try:
        now = time.time()
        if now - _COST["t1"] > 180:
            r1 = income._log(sget, 1); px = income._px(pub, {r["currency"] for r in r1})
            _COST["d1"] = _sum_costs(r1, px); _COST["t1"] = now
        if now - _COST["t7"] > 1800:
            r7 = income._log(sget, 7); px = income._px(pub, {r["currency"] for r in r7})
            _COST["d7"] = _sum_costs(r7, px); _COST["t7"] = now
        if now - _COST["tl"] > 600:
            ln = []
            for c in ("USDT", "USDC", "INIT"):
                try:
                    x = sget("/v5/account/collateral-info", {"currency": c})["result"]["list"][0]
                    a = ff(x.get("borrowAmount"))
                    if a > 0:
                        fr = ff(x.get("freeBorrowAmount")); hr = ff(x.get("hourlyBorrowRate"))
                        ln.append({"c": c, "amt": round(a, 2), "free": round(fr, 2), "hr": hr, "year": round(hr * 24 * 365 * 100, 2),
                                   "day": round(max(0.0, a - fr) * hr * 24, 4), "ratio": ff(x.get("collateralRatio"))})
                except Exception:
                    pass
            _COST["loan"] = ln; _COST["tl"] = now
    except Exception as e:
        _COST["err"] = str(e)
    finally:
        _COST["busy"] = False

def _costs(sget, pub):
    """Тяжёлый журнал за 7 дней грузится в фоне; страница не ждёт (до готовности — None)."""
    if not _COST["busy"] and (time.time() - min(_COST["t1"], _COST["t7"], _COST["tl"]) > 180):
        _COST["busy"] = True; threading.Thread(target=_costs_bg, args=(sget, pub), daemon=True).start()
    return {k: _COST.get(k) for k in ("d1", "d7", "loan", "err")}

def _scen(legs, qperp, S0, now):
    """P&L книги SOX по сценариям: к ближайшей экспирации и через сутки (BS, IV ног неизменна)."""
    exps = sorted({l["ets"] for l in legs if l["ets"] > now})
    if not exps: return None
    t1 = exps[0] + 60; t2 = (exps[1] + 60) if len(exps) > 1 else now + 86400
    def val(S, t):
        v = qperp * S
        for l in legs:
            T = max(0.0, (l["ets"] - t) / 365 / 86400)
            v += l["q"] * desk._bs(S, l["K"], T, max(0.05, l["iv"] or 1.0), l["cp"])
        return v
    base = val(S0, now)
    moves = (-30, -20, -10, -5, 0, 5, 10, 20, 30)
    rows = []
    for m in moves:
        S = S0 * (1 + m / 100)
        rows.append({"m": m, "S": round(S, 2), "exp": round(val(S, t1) - base, 0), "d1": round(val(S, t2) - base, 0)})
    h = 0.05
    d_after = (val(S0 + h, t1) - val(S0 - h, t1)) / (2 * h)
    return {"exp_ts": exps[0], "exp2_ts": (exps[1] if len(exps) > 1 else None), "rows": rows, "d_after": round(d_after, 1)}

def build(sget, pub, port):
    now = time.time()
    v = desk._C.get("desk")                 # полный снимок тяжёлый (~10 с): берём готовый, если ему < 60 с
    D = v[1] if (v and now - v[0] < 60) else desk.desk_snapshot(sget, pub, port() if callable(port) else port)
    try: L = desk.desk_live(sget, pub)
    except Exception: L = {}
    acct = (L.get("acct") if L and L.get("acct") else D.get("acct")) or {}
    s = D.get("sox", {}); legs = s.get("legs", []); perps = s.get("perps", [])
    pxL, pxS = s.get("pxL"), s.get("pxS")
    dp = s.get("dparts", {}); g = s.get("gamma", {}) or {}
    qperp = sum(p["q"] for p in perps if p["sym"].startswith("SOXL"))
    # --- дельта по частям и прочие монеты
    net = [n for n in (D.get("lev", {}) or {}).get("net", []) if n.get("b") not in ("SOXL", "SOXS")]
    # --- позиции для разметки ордеров
    pos = {l["sym"]: l for l in legs}
    allp = {}
    try:
        for x in sget("/v5/position/list", {"category": "linear", "settleCoin": "USDT"})["result"]["list"]:
            if ff(x["size"]): allp[x["symbol"]] = ff(x["size"]) * (1 if x["side"] == "Buy" else -1)
    except Exception:
        pass
    tk = _opt_tickers(pub)
    orders = []
    for o in _orders(sget):
        sym = o["symbol"].replace("-USDT", ""); side = 1 if o["side"] == "Buy" else -1
        q = ff(o["qty"]) - ff(o.get("cumExecQty")); px = ff(o.get("price")); ro = str(o.get("reduceOnly")).lower() == "true"
        if o["_cat"] == "option":
            hold = pos.get(sym, {}).get("q", 0.0)
            du = ff(tk.get(sym, {}).get("delta"))
            dd = side * q * du
            bid, ask = ff(tk.get(sym, {}).get("bid1Price")), ff(tk.get(sym, {}).get("ask1Price"))
        else:
            hold = allp.get(o["symbol"], 0.0); dd = side * q if o["symbol"].startswith("SOXL") else None
            bid = ask = None
        if hold == 0: kind = "открытие"
        elif hold * side < 0: kind = "откуп" if hold < 0 else "закрытие"
        else: kind = "новая продажа" if side < 0 else "докупка"
        warn = []
        if o["_cat"] != "option" and o["_cat"] != "spot" and hold * side < 0 and not ro:
            warn.append("не reduce-only: после закрытия позиции откроет встречную")
        if o["_cat"] != "option" and hold * side < 0 and q > abs(hold) + 1e-9:
            warn.append("больше позиции на %g" % round(q - abs(hold), 4))
        if kind in ("новая продажа",) and ff(acct.get("im_pct")) >= 50:
            warn.append("новая короткая нога при IM %s%%" % acct.get("im_pct"))
        orders.append({"sym": sym, "cat": o["_cat"], "side": o["side"], "q": q, "px": px, "ro": ro, "kind": kind, "hold": hold,
                       "dd": round(dd, 2) if dd is not None else None, "bid": bid, "ask": ask, "warn": warn})
    # --- проданные: откуп
    own_sell = {(x["sym"], x["px"]) for x in orders if x["cat"] == "option" and x["side"] == "Sell"}
    shorts = []
    for l in sorted([l for l in legs if l["q"] < 0], key=lambda l: (l["ets"], l["K"])):
        ask, bid, mk, avg = l.get("ask") or 0, l.get("bid") or 0, l.get("mark") or 0, l.get("avg") or 0
        own = (l["sym"], ask) in own_sell
        shorts.append({"sym": l["sym"], "q": l["q"], "avg": avg, "bid": bid, "ask": ask, "mark": mk, "own_ask": own,
                       "ratio": round(ask / mk, 1) if (ask and mk) else None,
                       "plus": (ask > 0 and ask < avg), "pnl_ask": round((avg - ask) * -l["q"], 2) if ask else None,
                       "d": l["d"], "g": l["g"], "th": l["th"], "exp_h": round((l["ets"] - now) / 3600, 1)})
    # --- экспирации
    ex = {}
    for l in legs:
        e = ex.setdefault(l["exp"], {"exp": l["exp"], "ts": l["ets"], "n": 0, "d": 0.0, "g": 0.0, "th": 0.0, "legs": []})
        e["n"] += 1; e["d"] += l["d"]; e["g"] += l["g"]; e["th"] += l["th"]
        e["legs"].append("%s%g%s×%g" % ("+" if l["q"] > 0 else "−", l["K"], l["cp"], abs(l["q"])))
    exps = sorted(ex.values(), key=lambda e: e["ts"])
    for e in exps:
        e["d"], e["g"], e["th"] = round(e["d"], 2), round(e["g"], 3), round(e["th"], 2); e["h"] = round((e["ts"] - now) / 3600, 1)
    # --- фандинг
    fund = [{"sym": p["sym"], "usd": p["usd"], "rate": p.get("fund"), "day": p.get("fund_day"), "d24": p.get("fund24_day"), "r24": p.get("fund24")}
            for p in (D.get("perps_all") or perps)]
    try: mx = capital_split.mexc_snapshot()
    except Exception as e: mx = {"err": str(e)}
    return {
        "t": time.strftime("%H:%M:%S"), "acct": acct, "pxL": pxL, "pxS": pxS,
        "delta": {"tot": dp.get("L_tot"), "usd": dp.get("L_usd"), "perp": dp.get("L_perp"), "opt": dp.get("L_opt"),
                  "S_tot": dp.get("S_tot"), "gamma": (s.get("opt") or {}).get("g"), "net": net},
        "theta": {"opt": (s.get("opt") or {}).get("th"), "fund_now": D.get("fund_day"), "fund24": D.get("fund24_day"),
                  "fund24_by": D.get("fund24_by"), "day": D.get("theta_day")},
        "scen": _scen(legs, qperp, pxL, now) if pxL else None,
        "margin": D.get("margin"), "orders": orders, "shorts": shorts, "exps": exps, "fund": fund,
        "atr": _atr(pub), "rv": g.get("rv"), "iv": g.get("iv_ref"), "us": g.get("us"), "mexc": mx,
        "alerts": D.get("alerts"), "execs": _execs(sget), "costs": _costs(sget, pub),
    }


PAGE = r'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Деск Claude</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--line:#30363d;--tx:#e6edf3;--mut:#8b949e;--pos:#3fb950;--neg:#f85149;--warn:#d29922;--acc:#58a6ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.4 system-ui,Segoe UI,Arial,sans-serif;padding:12px 16px}
h1{font-size:15px;margin:0 0 4px}.sub{color:var(--mut);font-size:13px;margin-bottom:10px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(185px,1fr));gap:6px}
.sec{margin:14px 0 5px;color:var(--mut);font-size:13px;text-transform:uppercase;letter-spacing:.04em}
.t{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:7px 9px}
.t .k{color:var(--mut);font-size:13px}.t .v{font-size:24px;font-weight:650;margin-top:2px;white-space:nowrap}
.t .s{color:var(--mut);font-size:12.5px;margin-top:2px}
.pos{color:var(--pos)}.neg{color:var(--neg)}.warn{color:var(--warn)}.acc{color:var(--acc)}
table{border-collapse:collapse;width:100%;background:var(--card);border:1px solid var(--line);border-radius:6px;font-size:14px}
th,td{padding:4px 7px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}th{color:var(--mut);font-weight:500}
td:first-child,th:first-child{text-align:left}.wrap{overflow-x:auto}
.two{display:grid;grid-template-columns:1fr 1fr;gap:8px}@media(max-width:900px){.two{grid-template-columns:1fr}}
.hero{display:grid;grid-template-columns:1.1fr 1fr 1fr;gap:8px;margin-bottom:6px}@media(max-width:900px){.hero{grid-template-columns:1fr}}
.big{font-size:38px!important}.bar{height:14px;border-radius:7px;background:linear-gradient(90deg,var(--neg) 0 30%,var(--warn) 30% 50%,var(--pos) 50%);position:relative;margin:10px 0 4px}
.bar i{position:absolute;top:-5px;width:4px;height:24px;background:var(--tx);border-radius:2px}.bl{display:flex;justify-content:space-between;font-size:12px;color:var(--mut)}
.al{background:#3d1d1d;border:1px solid var(--neg);border-radius:6px;padding:6px 9px;margin-bottom:8px}
</style></head><body>
<h1>Деск Claude · спред SOXL / SOXS</h1>
<div class="sub" id="upd">загрузка…</div><div id="al"></div>
<div class="hero"><div class="t"><div class="k">Запас до ликвидации (маржинальный баланс − MM) · идея макета R_tail</div><div class="v big" id="gz">—</div>
<div class="bar"><i id="gm"></i></div><div class="bl"><span>ликвидация</span><span>MM 70%</span><span>MM 50%</span><span>спокойно</span></div></div>
<div class="t" title="Покрывает ли тета опционов все издержки: комиссии + фандинг + проценты по займу (по факту из журнала Bybit)"><div class="k">Тета против издержек, $ в сутки</div><div class="v big" id="tc">…</div><div class="s" id="tcs"></div></div>
<div class="t"><div class="k">Последние исполнения — сверка стороны и инструмента</div><div id="exe" style="font-size:14px;margin-top:4px"></div></div></div>
<div class="sec">Риск счёта Bybit</div><div class="grid" id="risk"></div>
<div class="sec">Дельта: из чего состоит</div><div class="grid" id="delta"></div>
<div class="two"><div><div class="sec" id="sch">P&L по сценариям</div><div class="wrap"><table id="scen"></table></div></div>
<div><div class="sec">Открытые ордера — что сделают</div><div class="wrap"><table id="ord"></table></div></div></div>
<div class="sec">Проданные: откуп (зелёное — дешевле входа, без фиксации убытка)</div><div class="wrap"><table id="sh"></table></div>
<div class="sec">Экспирации</div><div class="wrap"><table id="ex"></table></div>
<div class="sec">Издержки: комиссии, фандинг, проценты по займу — по факту из журнала операций Bybit</div><div class="grid" id="cs"></div><div class="grid" id="th" style="margin-top:6px"></div><div class="two" style="margin-top:6px"><div class="wrap"><table id="fd"></table></div><div class="wrap"><table id="ln"></table></div></div>
<div class="sec">Ход цены и MEXC</div><div class="grid" id="mv"></div>
<script>
const $=id=>document.getElementById(id),f0=v=>v==null?'—':(+v).toFixed(0),f1=v=>v==null?'—':(+v).toFixed(1),f2=v=>v==null?'—':(+v).toFixed(2),
sg=v=>v==null?'—':((v>0?'+':'')+(+v).toFixed(v%1?2:0)),cl=v=>v==null?'':(v>0?'pos':(v<0?'neg':''));
function T(k,v,c,s,tip){return '<div class="t"'+(tip?' title="'+tip+'"':'')+'><div class="k">'+k+'</div><div class="v '+(c||'')+'">'+v+'</div>'+(s?'<div class="s">'+s+'</div>':'')+'</div>'}
function hrs(h){return h==null?'—':(h<48?f1(h)+' ч':f1(h/24)+' дн')}
function render(d){
 const a=d.acct||{},room=a.room,ic=a.im_pct>=80?'neg':(a.im_pct>=50?'warn':'pos'),mc=a.mm_pct>=70?'neg':(a.mm_pct>=50?'warn':'pos');
 $('upd').textContent='обновлено '+d.t+' · SOXL '+f2(d.pxL)+' / SOXS '+f2(d.pxS)+' · данные Bybit (только чтение) · основной деск — /';
 $('al').innerHTML=(d.alerts||[]).map(x=>'<div class="al">⚠ '+x.txt+'</div>').join('');
 const sox=(d.margin||[]).find(m=>m.coin=='SOXL')||{};
 $('gz').innerHTML='$'+f0(room)+'<span style="font-size:14px;color:var(--mut)"> · MM '+f1(a.mm_pct)+'% · '+(a.mm_pct?(100/a.mm_pct).toFixed(2):'—')+'× к MM</span>';$('gz').className='v big '+mc;
 $('gm').style.left=Math.max(0,Math.min(98,100-(a.mm_pct||0)))+'%';
 $('exe').innerHTML=(d.execs||[]).map(e=>'<div>'+new Date(e.ts*1000).toLocaleString('ru',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})+' · <b class="'+(e.side=='Buy'?'pos':'neg')+'">'+(e.side=='Buy'?'купил':'продал')+'</b> '+e.sym+' ×'+e.q+' по '+e.px+(e.type&&e.type!='Trade'?' <span class="warn">'+e.type+'</span>':'')+'</div>').join('')||'—';
 $('risk').innerHTML=T('IM',f1(a.im_pct)+'%',ic,'лимит 50%','Как у Bybit: IM / маржинальный баланс')+T('MM',f1(a.mm_pct)+'%',mc,'ликвидация при 100%','Как у Bybit: MM / маржинальный баланс')
  +T('Запас до ликвидации','$'+f0(room),room<150?'neg':(room<300?'warn':'pos'),'маржинальный баланс − MM','Столько $ может потерять счёт до ликвидации')
  +T('Доступно','$'+f0(a.avail),a.avail<50?'neg':'','на премии, откупы, новые ноги','При 0 Bybit не даст купить путы/хедж')
  +T('Маржа. баланс','$'+f0(a.eq),'','без монет, которые PM не берёт в залог')
  +T('Худший сценарий SOXL','$'+f0(sox.worst),'neg','MM SOXL $'+f0(sox.mm)+' · ход '+(sox.move||'')+' · IV '+(sox.iv||''));
 const D=d.delta||{};
 $('delta').innerHTML='<div class="t" style="grid-column:span 2" title="Дельта книги SOX: в долларах и в штуках SOXL (перп + опционы, греки Bybit)"><div class="k">Δ книги SOX</div><div class="v '+(Math.abs(D.usd)<100?'pos':'warn')+'">$'+f0(D.usd)+' <span style="color:var(--mut);font-weight:400">/</span> '+sg(D.tot)+' SOXL</div><div class="s">в $ / в штуках SOXL · ноль = нейтраль</div></div>'
  +T('Δ перпа',sg(D.perp)+' SOXL','','перп SOXLUSDT')+T('Δ опционов',sg(D.opt)+' SOXL','','все ноги, греки Bybit')
  +T('Гамма опц.',f2(D.gamma),D.gamma<0?'neg':'pos','Δ на каждый $1 SOXL','Отрицательная: при росте Δ падает, при падении растёт — «изгиб вниз»')
  +(D.net||[]).filter(n=>Math.abs(n.usd)>=5).map(n=>T('Δ '+n.b,'$'+f0(n.usd),Math.abs(n.usd)<50?'pos':'warn',f0(n.true)+' '+n.b+' (спот+перп)')).join('');
 const S=d.scen;
 if(S){const r=S.rows;$('sch').textContent='P&L SOX к экспирации '+new Date(S.exp_ts*1000).toLocaleString('ru',{weekday:'short',hour:'2-digit',minute:'2-digit'})+' и следующей'+(S.exp2_ts?' ('+new Date(S.exp2_ts*1000).toLocaleString('ru',{weekday:'short'})+')':'')+' · Δ после экспирации '+sg(S.d_after);
  $('scen').innerHTML='<tr><th>ход</th><th>SOXL</th><th>к ближайшей</th><th>к следующей</th></tr>'+r.map(x=>'<tr><td>'+(x.m>0?'+':'')+x.m+'%</td><td>'+f2(x.S)+'</td><td class="'+(x.exp<-room?'neg':cl(x.exp))+'"'+(x.exp<-room?' style="font-weight:700"':'')+'>'+sg(x.exp)+'</td><td class="'+cl(x.d1)+'">'+sg(x.d1)+'</td></tr>').join('')
  +'<tr><td colspan="4" style="color:var(--mut);font-size:12.5px;text-align:left">BS при нынешней IV, только книга SOX; жирное красное — убыток больше запаса $'+f0(room)+' (MM при падении тоже меняется — оценка грубая)</td></tr>'}
 const O=d.orders||[];
 $('ord').innerHTML=O.length?'<tr><th>ордер</th><th>цена</th><th>что это</th><th>Δ SOXL</th></tr>'+O.map(o=>'<tr><td>'+(o.side=='Buy'?'<span class="pos">Buy</span>':'<span class="neg">Sell</span>')+' '+o.sym+' ×'+o.q+(o.ro?' <span class="s">RO</span>':'')+'</td><td>'+o.px+'</td><td class="'+(o.kind=='новая продажа'?'warn':'')+'">'+o.kind+(o.warn.length?'<div class="neg" style="font-size:12.5px;white-space:normal">⚠ '+o.warn.join('; ')+'</div>':'')+'</td><td class="'+cl(o.dd)+'">'+sg(o.dd)+'</td></tr>').join(''):'<tr><td>открытых ордеров нет</td></tr>';
 const H=d.shorts||[];
 $('sh').innerHTML='<tr><th>нога</th><th>шт</th><th>вход</th><th>бид</th><th>аск</th><th>марк</th><th>аск/марк</th><th>откуп по аску</th><th>Δ</th><th>Θ/сут</th><th>до эксп.</th></tr>'+H.map(x=>'<tr><td>'+x.sym+'</td><td>'+x.q+'</td><td>'+f2(x.avg)+'</td><td>'+(x.bid||'—')+'</td><td>'+(x.ask||'—')+(x.own_ask?' <span class="warn" title="лучший аск — твой собственный Sell-ордер">твой</span>':'')+'</td><td>'+f2(x.mark)+'</td><td class="'+(x.ratio==null?'':(x.ratio>2?'neg':(x.ratio>1.2?'warn':'pos')))+'">'+(x.ratio?'×'+x.ratio:'нет аска')+'</td><td class="'+(x.plus?'pos':'neg')+'">'+(x.pnl_ask==null?'—':sg(x.pnl_ask))+'</td><td>'+sg(x.d)+'</td><td>'+f2(x.th)+'</td><td>'+hrs(x.exp_h)+'</td></tr>').join('');
 const E=d.exps||[];
 $('ex').innerHTML='<tr><th>экспирация</th><th>через</th><th>Δ уйдёт</th><th>Γ</th><th>Θ</th><th>ноги</th></tr>'+E.map(e=>'<tr><td>'+e.exp+'</td><td>'+hrs(e.h)+'</td><td class="'+cl(-e.d)+'">'+sg(-e.d)+'</td><td>'+f2(e.g)+'</td><td>'+f2(e.th)+'</td><td style="text-align:left;font-size:13px">'+e.legs.join(' ')+'</td></tr>').join('')
  +'<tr><td colspan="6" style="color:var(--mut);font-size:12.5px;text-align:left">«Δ уйдёт» — на сколько сдвинется дельта книги, когда ноги сгорят (при нынешней цене). Экспирация TradFi 16:00 ET = 23:00 Киев</td></tr>';
 const t=d.theta||{},fb=t.fund24_by||{};
 $('th').innerHTML=T('Фандинг · текущая ставка','$'+f2(t.fund_now),cl(t.fund_now),'прогноз × сутки по открытым перпам, прыгает')+T('Θ счёта','$'+f2(t.day),cl(t.day),'опционы + фандинг (текущий)');
  +T('Фандинг · текущая ставка','$'+f2(t.fund_now),cl(t.fund_now),'прогноз × сутки, прыгает')+T('Θ счёта','$'+f2(t.day),cl(t.day),'опционы + фандинг (текущий)');
 $('fd').innerHTML='<tr><th>открытый перп</th><th>$</th><th>ставка сейчас</th><th>за 24 ч</th><th>$ за 24 ч</th></tr>'+(d.fund||[]).map(p=>'<tr><td>'+p.sym+'</td><td>'+f0(p.usd)+'</td><td class="'+cl(-p.rate*Math.sign(p.usd))+'">'+f2(p.rate)+'%</td><td>'+(p.r24==null?'—':f2(p.r24)+'%')+'</td><td class="'+cl(p.d24)+'">'+(p.d24==null?'—':sg(p.d24))+'</td></tr>').join('');
 const C=d.costs||{},c1=C.d1,c7=C.d7,by=o=>Object.entries(o||{}).map(([k,v])=>k+' '+f2(v)).join(', ');
 {const th=(d.theta||{}).opt; if(c1||c7){const c7d=c7?c7.tot/7:null,base=c7d!=null?c7d:c1.tot,net=th+base,cov=base?th/-base:null;
  $('tc').innerHTML='<span class="'+cl(net)+'">'+sg(Math.round(net*100)/100)+'</span><span style="font-size:14px;color:var(--mut)"> = Θ '+f2(th)+' − издержки '+f2(-base)+'</span>';
  $('tcs').innerHTML=(cov!=null?'<b class="'+(cov>=1?'pos':'neg')+'">тета покрывает '+(cov>=10?'×'+cov.toFixed(0):Math.round(cov*100)+'%')+' издержек</b> · ':'')
   +'издержки: среднее за 7 дней '+(c7d==null?'…':f2(c7d))+'/сут'+(c7?' (комиссии '+f2(c7.fee/7)+', фандинг '+f2(c7.fund/7)+', проценты '+f2(c7.int/7)+')':'')+' · за последние 24 ч '+(c1?f2(c1.tot):'…');}
  else $('tc').textContent='…журнал грузится'}
 $('cs').innerHTML=(!c1?T('Издержки','…','','журнал грузится в фоне'):
   T('Итого за 24 ч','$'+f2(c1.tot),cl(c1.tot),'комиссии + фандинг + проценты')+T('Комиссии · 24 ч','$'+f2(c1.fee),cl(c1.fee),by(c1.fee_by))
   +T('Фандинг · 24 ч','$'+f2(c1.fund),cl(c1.fund),by(c1.fund_by)||'—','Списано и начислено по факту, включая уже закрытые позиции')+T('Проценты по займу · 24 ч','$'+f2(c1.int),cl(c1.int),'маржинальный спот'))
  +(!c7?'':T('Итого за 7 дней','$'+f2(c7.tot),cl(c7.tot),'комиссии '+f2(c7.fee)+' · фандинг '+f2(c7.fund)+' · проценты '+f2(c7.int))+T('Фандинг · 7 дней','$'+f2(c7.fund),cl(c7.fund),by(c7.fund_by)))
  +T('Θ опционов за сутки','$'+f2((d.theta||{}).opt),cl((d.theta||{}).opt),'для сравнения: окупает ли тета издержки');
 const LN=C.loan||[];
 $('ln').innerHTML=LN.length?'<tr><th>займ</th><th>сумма</th><th>без процентов</th><th>ставка</th><th>в сутки</th><th>коэф. залога</th></tr>'+LN.map(l=>'<tr><td>'+l.c+'</td><td>'+f2(l.amt)+'</td><td>'+f2(l.free)+'</td><td>'+f2(l.year)+'% год.</td><td class="neg">−$'+(l.day).toFixed(3)+'</td><td>'+l.ratio+'</td></tr>').join(''):'<tr><td>займов нет</td></tr>';
 const A=d.atr||{},rv=d.rv||{},m=d.mexc||{},us=d.us||{};
 $('mv').innerHTML=T('ATR SOXL (14 дн)',f1(A.atr14)+'%','','макс−мин за день · 30 дн '+f1(A.atr30)+'%','Средний дневной диапазон с гэпами')
  +T('Закрытие→закрытие',f1(A.cc14)+'%','','в среднем за день · 30 дн '+f1(A.cc30)+'%')
  +T('Последние 7 дней',(A.last7||[]).map(x=>f0(x)).join(' · '),'','ATR каждого дня, %')
  +T('RV / IV',f0(rv.h30)+'% / '+f0(d.iv)+'%',(rv.h30<d.iv)?'pos':'warn','RV 7д '+f0(rv.h7)+' · 120д '+f0(rv.d120))
  +T('США',us.state||'—','',us.to_open_h!=null?'открытие через '+f1(us.to_open_h)+' ч':(us.to_close_h!=null?'закрытие через '+f1(us.to_close_h)+' ч':''))
  +T('MEXC','$'+f0(m.eq),'',(m.fut_pos||[]).map(p=>p.sym.replace('_USDT','')+' '+p.side+' x'+p.lev).join(' · ')||'позиций нет',(m.fut_pos||[]).some(p=>p.lev>10)?'Плечо выше 10x':'');
}
async function tick(){try{const r=await fetch('/cljson');const d=await r.json();if(d.err){$('upd').textContent='ошибка: '+d.err}else render(d)}catch(e){$('upd').textContent='деск offline'}setTimeout(tick,5000)}
tick();
</script></body></html>'''
