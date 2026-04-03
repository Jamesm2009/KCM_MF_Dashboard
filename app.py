"""
Fund Performance Dashboard — Tiingo data source
3-phase sequential loader:
  Phase 1: 13-month history  → performance, RS rank, flags, z-score, sparkline
  Phase 2: 3-year history    → price range bar + TTM yield from dividends
  Phase 3: VIX comparison    → Risk On/Off signal
RS Score: (1D×0.10) + (1W×0.20) + (1M×0.30) + (3M×0.40)
Set env var TIINGO_TOKEN with your free token from tiingo.com
"""

from flask import Flask, render_template, jsonify
import requests
import pandas as pd
import threading
from datetime import datetime, date, timedelta
import json, os

app = Flask(__name__)

TIINGO_TOKEN = os.environ.get("TIINGO_TOKEN", "")
TIINGO_BASE  = "https://api.tiingo.com/tiingo/daily"
TIINGO_IEX   = "https://api.tiingo.com/iex"

cache = {
    "data": {}, "ranked": [], "last_updated": "Loading...",
    "vix_signal": "grey", "vix9d_value": "—", "vix_value": "—",
    "phase": 0, "progress": "Starting...", "error": None,
}
_lock = threading.Lock()


def load_funds():
    with open("funds.json", "r") as f:
        return json.load(f)


# ── Tiingo fetch ──────────────────────────────────────────────────────────────

def tiingo_history(symbol, years=1):
    """Return a DataFrame with adjClose and divCash indexed by date."""
    if not TIINGO_TOKEN:
        raise ValueError("TIINGO_TOKEN environment variable not set")
    start = (date.today() - timedelta(days=int(365 * years + 10))).strftime("%Y-%m-%d")
    url   = f"{TIINGO_BASE}/{symbol}/prices"
    params = {"startDate": start, "token": TIINGO_TOKEN, "resampleFreq": "daily"}
    r = requests.get(url, params=params, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    data = r.json()
    if not data:
        return None
    df = pd.DataFrame(data)
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    df = df.set_index("date").sort_index()
    return df


def tiingo_quote(symbol):
    """Return latest quote dict from Tiingo IEX endpoint."""
    url    = f"{TIINGO_IEX}/{symbol}"
    params = {"token": TIINGO_TOKEN}
    r = requests.get(url, params=params, timeout=15)
    if r.status_code != 200:
        return None
    data = r.json()
    return data[0] if data else None


def fetch_vix():
    """
    Tiingo doesn't carry VIX directly. We use ^VIX proxies:
    VIXY (ProShares VIX Short-Term) as Risk-Off proxy and
    fetch both with tiingo_history and compare short vs longer SMA.
    Or we simply compare VIXY 5d vs 21d SMA as a risk signal.
    """
    try:
        df = tiingo_history("VIXY", years=0.25)  # VIX short-term ETF
        if df is None or df.empty:
            return "grey", "—", "—"
        c = df["adjClose"].dropna()
        if len(c) < 10:
            return "grey", "—", "—"
        sma5  = round(c.tail(5).mean(),  2)
        sma21 = round(c.tail(21).mean(), 2)
        # sma5 > sma21 means short-term volatility rising → Risk Off
        sig = "grey" if abs(sma5 - sma21) < 0.1 else ("red" if sma5 > sma21 else "green")
        return sig, round(sma5, 2), round(sma21, 2)
    except Exception as e:
        print(f"  VIX proxy error: {e}")
        return "grey", "—", "—"


# ── Calc helpers ──────────────────────────────────────────────────────────────

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


def calc_ttm_yield(df):
    """Sum dividends over last 12 months divided by current price."""
    try:
        cutoff   = df.index[-1] - pd.Timedelta(days=365)
        last_yr  = df[df.index >= cutoff]
        ttm_div  = last_yr["divCash"].sum()
        cur_px   = df["adjClose"].dropna().iloc[-1]
        if ttm_div > 0 and cur_px > 0:
            return round(ttm_div / cur_px * 100, 2)
    except Exception:
        pass
    return None


def rebuild_ranked():
    rows     = list(cache["data"].values())
    scored   = sorted([r for r in rows if r.get("rs_score") is not None],
                      key=lambda x: x["rs_score"], reverse=True)
    unscored = [r for r in rows if r.get("rs_score") is None]
    for i, r in enumerate(scored):
        r["rank"] = i + 1
    for r in unscored:
        r["rank"] = None
    cache["ranked"] = scored + unscored


# ── Phase 1: 13-month performance ─────────────────────────────────────────────

def phase1(funds):
    print("=== PHASE 1: 13-month performance ===")
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
            df = tiingo_history(ticker, years=1.1)
            if df is None or df.empty:
                print(f"    skip — no data")
                continue

            closes = df["adjClose"].dropna()
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
                "low3": None, "high3": None, "last_price": None,
                "bar_pct": None, "ttm_yield": None, "rank": None,
            }
            with _lock:
                cache["data"][ticker] = row
                rebuild_ranked()
            print(f"    OK  rs={'%.2f'%rs if rs else 'n/a'}")

        except Exception as e:
            print(f"    ERR: {e}")

    with _lock:
        cache["phase"]        = 2
        cache["last_updated"] = datetime.now().strftime("%-m/%-d/%y %H:%M ET")
    print(f"Phase 1 done — {len(cache['data'])} funds loaded")


# ── Phase 2: 3-year bar + TTM yield ──────────────────────────────────────────

def phase2(funds):
    print("=== PHASE 2: 3yr bar + TTM yield ===")
    for i, fund in enumerate(funds):
        ticker = fund["symbol"]
        if ticker not in cache["data"]:
            continue

        with _lock:
            cache["progress"] = f"Phase 2 — {i+1}/{len(funds)}: {ticker}"
        print(f"  P2 [{i+1}/{len(funds)}] {ticker}")

        try:
            df = tiingo_history(ticker, years=3)
            if df is None or df.empty:
                continue
            c  = df["adjClose"].dropna()
            if len(c) < 2:
                continue
            lo   = round(c.min(), 2)
            hi   = round(c.max(), 2)
            last = round(c.iloc[-1], 2)
            pct  = round((last-lo)/(hi-lo)*100, 1) if hi > lo else 50.0
            ttm  = calc_ttm_yield(df)

            with _lock:
                cache["data"][ticker].update({
                    "low3": lo, "high3": hi, "last_price": last,
                    "bar_pct": pct, "ttm_yield": ttm,
                })
                rebuild_ranked()
            print(f"    OK  lo={lo} hi={hi} ttm={ttm}")

        except Exception as e:
            print(f"    ERR: {e}")

    with _lock:
        cache["phase"]        = 3
        cache["last_updated"] = datetime.now().strftime("%-m/%-d/%y %H:%M ET")
    print("Phase 2 done")


# ── Phase 3: VIX risk signal ──────────────────────────────────────────────────

def phase3():
    print("=== PHASE 3: VIX risk signal ===")
    with _lock:
        cache["progress"] = "Phase 3 — fetching VIX risk signal..."
    sig, v9, vi = fetch_vix()
    with _lock:
        cache["vix_signal"]   = sig
        cache["vix9d_value"]  = v9
        cache["vix_value"]    = vi
        cache["phase"]        = 4
        cache["last_updated"] = datetime.now().strftime("%-m/%-d/%y %H:%M ET")
        cache["progress"]     = "All phases complete"
    print(f"Phase 3 done — VIX signal={sig}")


# ── Orchestrator ──────────────────────────────────────────────────────────────

def run_update():
    with _lock:
        cache["phase"]    = 1
        cache["progress"] = "Starting Phase 1..."
        cache["error"]    = None
    try:
        if not TIINGO_TOKEN:
            with _lock:
                cache["error"]   = "TIINGO_TOKEN not set. Add it in Render → Environment."
                cache["phase"]   = 4
                cache["loading"] = False
            return
        funds = load_funds()
        phase1(funds)
        phase2(funds)
        phase3()
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
        snap = dict(cache)
        funds = list(snap["ranked"])
    is_loading = snap["phase"] < 2 or len(funds) == 0
    return render_template("index.html",
        funds=funds, last_updated=snap["last_updated"],
        vix_signal=snap["vix_signal"], vix9d=snap["vix9d_value"],
        vix=snap["vix_value"], is_loading=is_loading,
        phase=snap["phase"], progress=snap["progress"],
        error=snap["error"])


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
    """Visit /test to verify Tiingo connectivity."""
    if not TIINGO_TOKEN:
        return jsonify({"status": "error", "detail": "TIINGO_TOKEN not set in environment"})
    try:
        df = tiingo_history("VFIAX", years=0.05)
        if df is None:
            return jsonify({"status": "ticker not found on Tiingo"})
        if df.empty:
            return jsonify({"status": "empty response"})
        last = round(df["adjClose"].dropna().iloc[-1], 2)
        return jsonify({"status": "ok", "VFIAX_last_close": last, "rows": len(df)})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)})


@app.route("/api/data")
def api_data():
    with _lock:
        return jsonify(cache["ranked"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
