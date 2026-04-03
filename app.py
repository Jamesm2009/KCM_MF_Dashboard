"""
Fund Performance Dashboard — 3-phase sequential loader
Phase 1: 1yr history  → performance, RS rank, flags, z-score, sparkline (fast ~2 min)
Phase 2: 3yr history  → price range bar (medium)
Phase 3: .info calls  → TTM yield (slow, best-effort)
RS Score: (1D×0.10) + (1W×0.20) + (1M×0.30) + (3M×0.40)
"""

from flask import Flask, render_template, jsonify
import yfinance as yf
import pandas as pd
import threading
from datetime import datetime, date
import json, os

app = Flask(__name__)

# phase: 0=not started, 1=loading perf, 2=loading bars, 3=loading ttm, 4=done
cache = {
    "data": {}, "ranked": [], "last_updated": "Loading...",
    "vix_signal": "grey", "vix9d_value": "—", "vix_value": "—",
    "phase": 0, "progress": "Starting...", "error": None,
}
_lock = threading.Lock()


def load_funds():
    with open("funds.json", "r") as f:
        return json.load(f)


# ── Calculation helpers ───────────────────────────────────────────────────────

def period_return(closes, days):
    if len(closes) < 2:
        return None
    latest = closes.index[-1]
    past   = closes[closes.index <= latest - pd.Timedelta(days=days)]
    if past.empty:
        return None
    return (closes.iloc[-1] - past.iloc[-1]) / past.iloc[-1] * 100


def ytd_return(closes):
    yr = closes[closes.index.year == date.today().year]
    if yr.empty:
        return None
    return (yr.iloc[-1] - yr.iloc[0]) / yr.iloc[0] * 100


def zscore_1yr(closes):
    cutoff = closes.index[-1] - pd.Timedelta(days=365)
    c = closes[closes.index >= cutoff].dropna()
    if len(c) < 20:
        return None
    std = c.std()
    if std == 0:
        return None
    return round((c.iloc[-1] - c.mean()) / std, 2)


def sma_flag(closes, window):
    c = closes.dropna()
    if len(c) < window:
        return "grey"
    sma  = c.tail(window).mean()
    last = c.iloc[-1]
    return "green" if last > sma else ("red" if last < sma else "grey")


def make_sparkline(closes, days=170, w=90, h=28):
    c = closes.dropna().tail(days).values
    if len(c) < 2:
        return ""
    mn, mx = c.min(), c.max()
    if mn == mx:
        return ""
    n   = len(c) - 1
    pts = [f"{round(i/n*w,1)},{round((1-(v-mn)/(mx-mn))*(h-2)+1,1)}"
           for i, v in enumerate(c)]
    col = "#16a34a" if c[-1] >= c[0] else "#dc2626"
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
            f'xmlns="http://www.w3.org/2000/svg">'
            f'<polyline points="{" ".join(pts)}" fill="none" stroke="{col}" '
            f'stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>'
            f'</svg>')


def fetch_one(symbol, period, timeout=45):
    """Fetch one ticker history with a hard timeout."""
    result, err = [None], [None]
    def _go():
        try:
            result[0] = yf.Ticker(symbol).history(period=period)
        except Exception as e:
            err[0] = e
    t = threading.Thread(target=_go, daemon=True)
    t.start(); t.join(timeout=timeout)
    if t.is_alive():
        return None   # timed out
    if err[0]:
        raise err[0]
    return result[0]


def rebuild_ranked():
    """Sort cache['data'] dict by rs_score and store as list in cache['ranked']."""
    rows = list(cache["data"].values())
    scored   = sorted([r for r in rows if r.get("rs_score") is not None],
                      key=lambda x: x["rs_score"], reverse=True)
    unscored = [r for r in rows if r.get("rs_score") is None]
    for i, r in enumerate(scored):
        r["rank"] = i + 1
    for r in unscored:
        r["rank"] = None
    cache["ranked"] = scored + unscored


# ── Phase 1 — 1-year performance data ────────────────────────────────────────

def phase1(funds):
    print("=== PHASE 1: 1yr performance ===")
    for i, fund in enumerate(funds):
        ticker   = fund["symbol"]
        name     = fund.get("name", ticker)
        category = fund.get("category", "equity")
        ftype    = fund.get("type", "")
        ms_url   = fund.get("morningstar_url",
                   f"https://www.morningstar.com/search#q={ticker}")

        with _lock:
            cache["progress"] = f"Phase 1 — {i+1}/{len(funds)}: {ticker}"

        print(f"  P1 [{i+1}/{len(funds)}] {ticker}")
        try:
            hist = fetch_one(ticker, period="13mo")
            if hist is None or hist.empty:
                print(f"    skip — no data")
                continue

            closes = hist["Close"].dropna()
            if len(closes) < 10:
                continue

            d1  = period_return(closes, 1)
            w1  = period_return(closes, 7)
            m1  = period_return(closes, 30)
            m3  = period_return(closes, 91)
            m6  = period_return(closes, 182)
            ytd = ytd_return(closes)
            y1  = period_return(closes, 365)

            rs = None
            if all(v is not None for v in [d1, w1, m1, m3]):
                rs = (d1*0.10) + (w1*0.20) + (m1*0.30) + (m3*0.40)

            zsc   = zscore_1yr(closes)
            ob_os = ("Overbought" if zsc and zsc > 2.10
                     else "Oversold" if zsc and zsc < -2.05 else "")

            def fmt(v): return round(v, 2) if v is not None else None

            row = {
                "symbol": ticker, "name": name, "type": ftype,
                "category": category, "morningstar_url": ms_url,
                "sparkline":   make_sparkline(closes),
                "1D": fmt(d1), "1W": fmt(w1), "1M": fmt(m1),
                "3M": fmt(m3), "6M": fmt(m6), "YTD": fmt(ytd), "1Y": fmt(y1),
                "rs_score":   round(rs, 3) if rs is not None else None,
                "zscore": zsc, "ob_os": ob_os,
                "trade_flag": sma_flag(closes, 21),
                "trend_flag": sma_flag(closes, 63),
                # placeholders for later phases
                "low3": None, "high3": None, "last_price": None, "bar_pct": None,
                "ttm_yield": None, "rank": None,
            }
            with _lock:
                cache["data"][ticker] = row
                rebuild_ranked()

            print(f"    OK  rs={'%.2f'%rs if rs else 'n/a'}")

        except Exception as e:
            print(f"    ERR: {e}")

    # VIX after phase 1
    try:
        with _lock: cache["progress"] = "Phase 1 — fetching VIX..."
        v9h = fetch_one("^VIX9D", period="5d", timeout=20)
        vih  = fetch_one("^VIX",   period="5d", timeout=20)
        if v9h is not None and not v9h.empty and vih is not None and not vih.empty:
            v9 = round(v9h["Close"].dropna().iloc[-1], 2)
            vi = round(vih["Close"].dropna().iloc[-1], 2)
            sig = "grey" if abs(v9-vi) <= 0.1 else ("red" if v9>vi else "green")
            with _lock:
                cache["vix_signal"]  = sig
                cache["vix9d_value"] = v9
                cache["vix_value"]   = vi
    except Exception as ve:
        print(f"  VIX error: {ve}")

    with _lock:
        cache["phase"]        = 2
        cache["last_updated"] = datetime.now().strftime("%-m/%-d/%y %H:%M ET")
    print(f"Phase 1 done — {len(cache['data'])} funds")


# ── Phase 2 — 3-year price range bar ─────────────────────────────────────────

def phase2(funds):
    print("=== PHASE 2: 3yr price bar ===")
    for i, fund in enumerate(funds):
        ticker = fund["symbol"]
        if ticker not in cache["data"]:
            continue

        with _lock: cache["progress"] = f"Phase 2 — {i+1}/{len(funds)}: {ticker}"
        print(f"  P2 [{i+1}/{len(funds)}] {ticker}")

        try:
            hist = fetch_one(ticker, period="3y")
            if hist is None or hist.empty:
                continue
            c = hist["Close"].dropna()
            if len(c) < 2:
                continue
            lo   = round(c.min(), 2)
            hi   = round(c.max(), 2)
            last = round(c.iloc[-1], 2)
            pct  = round((last - lo) / (hi - lo) * 100, 1) if hi > lo else 50.0
            with _lock:
                cache["data"][ticker].update({
                    "low3": lo, "high3": hi, "last_price": last, "bar_pct": pct
                })
                rebuild_ranked()
            print(f"    OK  lo={lo} hi={hi}")
        except Exception as e:
            print(f"    ERR: {e}")

    with _lock:
        cache["phase"]        = 3
        cache["last_updated"] = datetime.now().strftime("%-m/%-d/%y %H:%M ET")
    print("Phase 2 done")


# ── Phase 3 — TTM yield ───────────────────────────────────────────────────────

def phase3(funds):
    print("=== PHASE 3: TTM yield ===")
    for i, fund in enumerate(funds):
        ticker = fund["symbol"]
        if ticker not in cache["data"]:
            continue

        with _lock: cache["progress"] = f"Phase 3 — {i+1}/{len(funds)}: {ticker}"
        print(f"  P3 [{i+1}/{len(funds)}] {ticker}")

        ttm = [None]
        def _fetch_info():
            try:
                info = yf.Ticker(ticker).info
                val  = info.get("trailingAnnualDividendYield") or info.get("dividendYield") or 0
                ttm[0] = round(val * 100, 2) if val and val > 0 else None
            except Exception:
                pass

        t = threading.Thread(target=_fetch_info, daemon=True)
        t.start(); t.join(timeout=20)

        with _lock:
            cache["data"][ticker]["ttm_yield"] = ttm[0]
            rebuild_ranked()
        print(f"    ttm={ttm[0]}")

    with _lock:
        cache["phase"]        = 4
        cache["last_updated"] = datetime.now().strftime("%-m/%-d/%y %H:%M ET")
        cache["progress"]     = "All phases complete"
    print("Phase 3 done — all phases complete.")


# ── Orchestrator ──────────────────────────────────────────────────────────────

def run_update():
    with _lock:
        cache["phase"]    = 1
        cache["progress"] = "Starting Phase 1..."
        cache["error"]    = None
    try:
        funds = load_funds()
        phase1(funds)
        phase2(funds)
        phase3(funds)
    except Exception as e:
        import traceback; traceback.print_exc()
        with _lock:
            cache["error"] = str(e)
            cache["phase"] = 4


def trigger_update():
    threading.Thread(target=run_update, daemon=True).start()

trigger_update()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    with _lock:
        funds      = list(cache["ranked"])
        updated    = cache["last_updated"]
        vix_signal = cache["vix_signal"]
        vix9d      = cache["vix9d_value"]
        vix        = cache["vix_value"]
        phase      = cache["phase"]
        progress   = cache["progress"]
        error      = cache["error"]
    is_loading = (phase < 2) or (len(funds) == 0)
    return render_template("index.html",
        funds=funds, last_updated=updated,
        vix_signal=vix_signal, vix9d=vix9d, vix=vix,
        is_loading=is_loading, phase=phase,
        progress=progress, error=error)


@app.route("/refresh")
def refresh():
    trigger_update()
    return jsonify({"status": "refresh started"})


@app.route("/status")
def status():
    with _lock:
        return jsonify({
            "phase":        cache["phase"],
            "funds":        len(cache["data"]),
            "progress":     cache["progress"],
            "last_updated": cache["last_updated"],
            "error":        cache["error"],
        })


@app.route("/test")
def test():
    """Quick connectivity check — visit /test first after deploy."""
    try:
        h = fetch_one("VFIAX", period="5d", timeout=30)
        if h is None:  return jsonify({"status": "timeout — Yahoo Finance unreachable"})
        if h.empty:    return jsonify({"status": "empty response"})
        return jsonify({"status": "ok",
                        "VFIAX_last_close": round(h["Close"].dropna().iloc[-1], 2),
                        "rows": len(h)})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)})


@app.route("/api/data")
def api_data():
    with _lock:
        return jsonify(cache["ranked"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
