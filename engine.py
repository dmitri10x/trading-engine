#!/usr/bin/env python3
"""
Dmitri's swing trading engine (runs on GitHub Actions).
Reads : settings.json, tickers.json (held symbols, optional), POLYGON_API_KEY env
Writes: result.json = market context + metrics for a liquid universe (plus held tickers) + screened ideas
Rules : price above MA150, market cap > $1B, ATR% >= 4, high volume, stop = 1.5 ATR, R >= 2.
Claude reads result.json, merges with Robinhood positions, updates Airtable and messages Dmitri.
"""
import json, os, sys, time, math, datetime as dt
import urllib.request, urllib.parse, urllib.error

BASE = "https://api.polygon.io"

def load(path, default):
    try:
        with open(path) as f: return json.load(f)
    except Exception: return default

S = load("settings.json", {})
KEY = os.environ.get("POLYGON_API_KEY", "")
MODE = sys.argv[1] if len(sys.argv) > 1 else "premarket"
def num(k, d):
    try: return float(S.get(k, d))
    except Exception: return d
MIN_MCAP_B   = num("MIN_MARKET_CAP_B", 1)
MIN_ATR_PCT  = num("MIN_ATR_PCT", 4)
MIN_AVG_VOL  = num("MIN_AVG_VOLUME", 1_000_000)
MIN_REL_VOL  = num("MIN_REL_VOLUME", 1.5)
MA_LEN       = int(num("MA_LENGTH", 150))
STOP_ATR     = num("STOP_ATR_MULT", 1.5)
MIN_R        = num("MIN_R_MULTIPLE", 2)
MAX_IDEAS    = int(num("MAX_IDEAS_PER_DAY", 5))
ALERT_MOVE   = num("ALERT_MOVE_PCT", 4)
UNIVERSE     = int(num("UNIVERSE_SIZE", 300 if MODE == "premarket" else 120))

CALLS = 0
def get(path, params=None, retries=8):
    """GET with backoff for the free tier (5 calls/min)."""
    global CALLS
    params = dict(params or {}); params["apiKey"] = KEY
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    for i in range(retries):
        try:
            CALLS += 1
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429: time.sleep(13); continue
            if e.code in (403, 404): return None
            time.sleep(3)
        except Exception:
            time.sleep(3)
    return None

def bars(ticker, days=300):
    end = dt.date.today(); start = end - dt.timedelta(days=int(days * 1.6))
    j = get(f"/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}", {"adjusted": "true", "sort": "asc", "limit": 5000})
    return (j or {}).get("results") or []

def details(ticker):
    j = get(f"/v3/reference/tickers/{ticker}")
    return (j or {}).get("results") or {}

def metrics(b):
    if len(b) < 30: return None
    c = [x["c"] for x in b]; h = [x["h"] for x in b]; l = [x["l"] for x in b]; v = [x["v"] for x in b]
    n = len(c)
    ma = sum(c[-MA_LEN:]) / min(MA_LEN, n)
    trs = [max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1])) for i in range(1, n)]
    atr = sum(trs[-14:]) / min(14, len(trs))
    avg_vol = sum(v[-21:-1]) / max(1, min(20, n - 1))
    rel_vol = v[-1] / avg_vol if avg_vol else 0
    hi20 = max(h[-20:]); hi52 = max(h[-252:]); lo20 = min(l[-20:])
    ma20 = sum(c[-20:]) / min(20, n); ma50 = sum(c[-50:]) / min(50, n)
    chg1 = (c[-1] / c[-2] - 1) * 100 if n > 1 else 0
    chg5 = (c[-1] / c[-6] - 1) * 100 if n > 5 else 0
    price = c[-1]; stop = round(price - STOP_ATR * atr, 2)
    target = round(max(hi20, price + 2 * (price - stop)), 2)
    dist = round((price / ma - 1) * 100, 1)
    if price <= ma: status, why = "Exit", f"Below MA150 ({round(ma,2)}). Rule broken: not a hold under the system."
    elif dist < 3: status, why = "Watch", f"Only {dist}% above MA150. One bad day flips it."
    else: status, why = "Hold", f"{dist}% above MA150, trend intact."
    alerts = []
    if abs(chg1) >= ALERT_MOVE: alerts.append(f"moved {chg1:+.1f}% on {round(rel_vol,2)}x volume")
    return dict(close=price, ma150=round(ma, 2), ma150_full=n >= MA_LEN, ma20=round(ma20, 2), ma50=round(ma50, 2),
                atr=round(atr, 2), atr_pct=round(atr / price * 100, 2), avg_vol=int(avg_vol), rel_vol=round(rel_vol, 2),
                hi20=hi20, hi52=hi52, lo20=lo20, chg1=round(chg1, 2), chg5=round(chg5, 2),
                above_ma150=price > ma, dist_ma150_pct=dist, stop=stop, target=target, status=status, note=why, alerts=alerts,
                last_date=dt.datetime.utcfromtimestamp(b[-1]["t"] / 1000).strftime("%Y-%m-%d"))

def grouped():
    d = dt.date.today()
    for _ in range(7):
        d -= dt.timedelta(days=1)
        j = get(f"/v2/aggs/grouped/locale/us/market/stocks/{d}", {"adjusted": "true"})
        rows = (j or {}).get("results") or []
        if rows: return d.isoformat(), rows
    return None, []

def idea_from(t, m, info):
    mcap_b = (info.get("market_cap") or 0) / 1e9
    if not m["ma150_full"] or not m["above_ma150"]: return None
    if m["atr_pct"] < MIN_ATR_PCT or m["avg_vol"] < MIN_AVG_VOL or m["rel_vol"] < MIN_REL_VOL or mcap_b < MIN_MCAP_B: return None
    price = m["close"]; stop = m["stop"]; risk = price - stop
    if risk <= 0: return None
    if price >= m["hi20"] * 0.99: setup, target = "Breakout", round(price + 3 * risk, 2)
    elif abs(price - m["ma20"]) / price < 0.02 and m["ma20"] > m["ma50"]: setup, target = "Pullback to MA", round(m["hi20"], 2)
    else: setup, target = "Volume surge", round(max(m["hi20"], price + 2.5 * risk), 2)
    rr = (target - price) / risk
    if rr < MIN_R: return None
    score = 3 + (m["rel_vol"] >= 2) + (m["dist_ma150_pct"] > 10 and m["chg5"] > 0)
    return dict(symbol=t, name=info.get("name", ""), setup=setup, price=price, entry=price, stop=stop, target=target,
                r_multiple=round(rr, 1), mcap_b=round(mcap_b, 1), atr_pct=m["atr_pct"], rel_vol=m["rel_vol"],
                chg1=m["chg1"], dist_ma150_pct=m["dist_ma150_pct"], score=min(5, int(score)),
                note=f"{setup}; {m['chg1']:+.1f}% on {m['rel_vol']}x vol; {m['dist_ma150_pct']}% above MA150; ATR {m['atr_pct']}%")

def main():
    if not KEY:
        json.dump({"error": "POLYGON_API_KEY missing (GitHub Actions secret)"}, open("result.json", "w")); return
    held = [p["symbol"] if isinstance(p, dict) else p for p in load("tickers.json", [])]
    res = dict(mode=MODE, as_of=dt.datetime.utcnow().isoformat() + "Z", market={}, metrics={}, ideas=[], alerts=[])
    for t in ("SPY", "QQQ", "IWM"):
        m = metrics(bars(t, 200))
        if m: res["market"][t] = dict(close=m["close"], chg1=m["chg1"], chg5=m["chg5"], above_ma150=m["above_ma150"], ma50=m["ma50"])
    gdate, rows = grouped(); res["grouped_date"] = gdate
    liquid = []
    for r in rows:
        t = r.get("T", ""); c = r.get("c", 0); v = r.get("v", 0)
        if not t.isalpha() or len(t) > 5 or c < 5 or v < MIN_AVG_VOL: continue
        liquid.append((c * v, t, r))
    liquid.sort(reverse=True)
    universe = list(dict.fromkeys(held + [t for _, t, _ in liquid[:UNIVERSE]]))
    grouped_by = {t: r for _, t, r in liquid}
    for t in universe:
        m = metrics(bars(t))
        if not m: continue
        res["metrics"][t] = m
        for a in m["alerts"]: res["alerts"].append(f"{t} {a}")
    if MODE == "premarket":
        # candidates: strong day on big dollar volume, not already held
        cands = []
        for t, m in res["metrics"].items():
            if t in held: continue
            g = grouped_by.get(t)
            if not g: continue
            if m["chg1"] < 1.5 or m["rel_vol"] < MIN_REL_VOL or not m["above_ma150"] or m["atr_pct"] < MIN_ATR_PCT: continue
            cands.append((m["chg1"] * math.log10(g["c"] * g["v"]), t))
        cands.sort(reverse=True)
        for _, t in cands[:20]:
            idea = idea_from(t, res["metrics"][t], details(t))
            if idea: res["ideas"].append(idea)
            if len(res["ideas"]) >= MAX_IDEAS: break
        res["ideas"].sort(key=lambda x: (-x["score"], -x["r_multiple"]))
        res["scan_note"] = f"universe {len(universe)}, candidates {len(cands)}, ideas {len(res['ideas'])}"
    res["held_missing"] = [t for t in held if t not in res["metrics"]]
    res["api_calls"] = CALLS
    json.dump(res, open("result.json", "w"), indent=1)
    print("done", MODE, "tickers", len(res["metrics"]), "calls", CALLS)

if __name__ == "__main__":
    main()
