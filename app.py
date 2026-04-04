"""
Fund Performance Dashboard — Tiingo, single-phase loader
One API call per fund (3y history), computes everything from it.
Total requests: 35 funds + 1 VIX proxy = 36/update (under 50/hr limit)
RS Score: (1D×0.10) + (1W×0.20) + (1M×0.30) + (3M×0.40)
"""

from flask import Flask, render_template, jsonify
import requests
import pandas as pd
import threading
import time
from datetime import datetime, date, timedelta
import json, os

app = Flask(__name__)

TIINGO_TOKEN = os.environ.get("TIINGO_TOKEN", "")
TIINGO_BASE  = "https://api.tiingo.com/tiingo/daily"

cache = {
    "data": {}, "ranked": [], "last_updated": "Loading...",
    "vix_signal": "grey", "vix9d_value": "—", "vix_value": "—",
    "phase": 0, "progress": "Starting...", "error": None,
}
_lock = threading.Lock()


def load_funds():
    with open("funds.json", "r") as f:
        return json.load(f)


# ── Tiingo fetch (single call, 3y) ────────────────────────────────────────────

def tiingo_history(symbol, years=3, retries=3):
    if not TIINGO_TOKEN:
        raise ValueError("TIINGO_TOKEN not set")
    start  = (date.today() - timedelta(days=int(365*years+10))).strftime("%Y-%m-%d")
    url    = f"{TIINGO_BASE}/{symbol}/prices"
    params = {"startDate": start, "token": TIINGO_TOKEN, "resampleFreq": "daily"}

    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 429:
                wait = 70 * (attempt + 1)
                print(f"    429 {symbol} — waiting {wait}s")
                with _lock:
                    cache["progress"] = f"Rate limit hit — waiting {wait}s then resuming..."
                time.sleep(wait)
                continue
            if r.status_code == 404:
                return None
            r.raise_for_status()
            data = r.json()
            if not data:
                return None
            df = pd.DataFrame(data)
            df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
            return df.set_index("date").sort_index()
        except requests.exceptions.Timeout:
            print(f"    timeout {symbol} attempt {attempt+1}")
            time.sleep(5)
    return None


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
    c = closes.dropna()
    tail = c.tail(days).values
    if len(tail) < 2:
        return ""
    mn, mx = tail.min(), tail.max()
    if mn == mx:
        return ""
    n   = len(tail) - 1
    pts = [f"{round(i/n*w,1)},{round((1-(v-mn)/(mx-mn))*(h-2)+1,1)}"
           for i, v in enumerate(tail)]
    # Green if last price > 63-day SMA, red if below
    sma63 = c.tail(63).mean() if len(c) >= 63 else c.mean()
    col   = "#16a34a" if c.iloc[-1] > sma63 else "#dc2626"
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
            f'xmlns="http://www.w3.org/2000/svg">'
            f'<polyline points="{" ".join(pts)}" fill="none" stroke="{col}" '
            f'stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>'
            f'</svg>')


def calc_ttm_yield(df, closes):
    try:
        cutoff  = closes.index[-1] - pd.Timedelta(days=365)
        div_col = "divCash" if "divCash" in df.columns else None
        if not div_col:
            return None
        ttm_div = df[div_col][df.index >= cutoff].sum()
        cur_px  = closes.iloc[-1]
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


# ── VIX proxy (VIXY ETF short vs longer SMA) ─────────────────────────────────

def fetch_vix():
    try:
        df = tiingo_history("VIXY", years=0.5)
        if df is None or df.empty:
            return "grey", "—", "—"
        c    = df["adjClose"].dropna()
        sma5 = round(c.tail(5).mean(),  2)
        sma21= round(c.tail(21).mean(), 2)
        sig  = "grey" if abs(sma5-sma21) < 0.05 else ("red" if sma5 > sma21 else "green")
        return sig, sma5, sma21
    except Exception as e:
        print(f"  VIX error: {e}")
        return "grey", "—", "—"


# ── Main update (single phase) ────────────────────────────────────────────────

def run_update():
    with _lock:
        cache["phase"]    = 1
        cache["progress"] = "Waiting 15s before starting..."
        cache["error"]    = None

    time.sleep(15)   # let any previous rate-limit window breathe

    try:
        if not TIINGO_TOKEN:
            with _lock:
                cache["error"] = "TIINGO_TOKEN not set in Render Environment."
                cache["phase"] = 4
            return

        funds = load_funds()
        total = len(funds)

        for i, fund in enumerate(funds):
            ticker   = fund["symbol"]
            name     = fund.get("name", ticker)
            category = fund.get("category", "equity")
            ftype    = fund.get("type", "")
            ms_url   = fund.get("morningstar_url",
                       f"https://www.morningstar.com/search#q={ticker}")

            with _lock:
                cache["progress"] = f"Loading {i+1}/{total}: {ticker}"
            print(f"  [{i+1}/{total}] {ticker}")

            try:
                df = tiingo_history(ticker, years=3)
                if df is None or df.empty:
                    print(f"    skip — no data")
                    time.sleep(3)
                    continue

                closes = df["adjClose"].dropna()
                if len(closes) < 30:
                    print(f"    skip — too few rows")
                    time.sleep(3)
                    continue

                def fmt(v): return round(v, 2) if v is not None else None

                d1  = period_return(closes, 1)
                w1  = period_return(closes, 7)
                m1  = period_return(closes, 30)
                m3  = period_return(closes, 91)
                m6  = period_return(closes, 182)
                ytd = ytd_return(closes)
                y1  = period_return(closes, 365)

                rs = None
                if all(v is not None for v in [d1, w1, m1, m3]):
                    rs = (d1*0.10)+(w1*0.20)+(m1*0.30)+(m3*0.40)

                zsc   = zscore_1yr(closes)
                ob_os = ("Overbought" if zsc and zsc > 2.10
                         else "Oversold" if zsc and zsc < -2.05 else "")

                lo   = round(closes.min(), 2)
                hi   = round(closes.max(), 2)
                last = round(closes.iloc[-1], 2)
                pct  = round((last-lo)/(hi-lo)*100, 1) if hi > lo else 50.0

                with _lock:
                    cache["data"][ticker] = {
                        "symbol": ticker, "name": name,
                        "type": ftype, "category": category,
                        "morningstar_url": ms_url,
                        "sparkline":   make_sparkline(closes),
                        "1D": fmt(d1), "1W": fmt(w1), "1M": fmt(m1),
                        "3M": fmt(m3), "6M": fmt(m6), "YTD": fmt(ytd), "1Y": fmt(y1),
                        "rs_score":   round(rs, 3) if rs is not None else None,
                        "zscore": zsc, "ob_os": ob_os,
                        "trade_flag": sma_flag(closes, 21),
                        "trend_flag": sma_flag(closes, 63),
                        "low3": lo, "high3": hi, "last_price": last, "bar_pct": pct,
                        "ttm_yield":  calc_ttm_yield(df, closes),
                        "rank": None,
                    }
                    rebuild_ranked()
                    cache["last_updated"] = datetime.now().strftime("%-m/%-d/%y %H:%M ET")

                print(f"    OK  rs={'%.2f'%rs if rs else 'n/a'}")

            except Exception as e:
                print(f"    ERR {ticker}: {e}")

            time.sleep(3)   # ~20 requests/min — well under 50/hr

        # VIX
        with _lock:
            cache["progress"] = f"Loading VIX signal..."
        sig, v9, vi = fetch_vix()

        with _lock:
            cache["vix_signal"]  = sig
            cache["vix9d_value"] = v9
            cache["vix_value"]   = vi
            cache["phase"]       = 4
            cache["progress"]    = "Complete"
            cache["last_updated"]= datetime.now().strftime("%-m/%-d/%y %H:%M ET")

        print(f"Done — {len(cache['data'])} funds, VIX={sig}")

    except Exception as e:
        import traceback; traceback.print_exc()
        with _lock:
            cache["error"] = str(e)
            cache["phase"] = 4


def trigger_update():
    threading.Thread(target=run_update, daemon=True).start()

# Do NOT call trigger_update() here — gunicorn forks workers AFTER module
# import, killing any threads started here. Instead we start on first request.
_started = False


# ── Routes ────────────────────────────────────────────────────────────────────

def _ensure_started():
    """Start the background loader on the very first request in this worker."""
    global _started
    if not _started:
        _started = True
        trigger_update()


@app.route("/")
def index():
    _ensure_started()
    with _lock:
        snap  = dict(cache)
        funds = list(snap["ranked"])
    is_loading = snap["phase"] < 4 or len(funds) == 0
    return render_template("index.html",
        funds=funds, last_updated=snap["last_updated"],
        vix_signal=snap["vix_signal"], vix9d=snap["vix9d_value"],
        vix=snap["vix_value"], is_loading=is_loading,
        phase=snap["phase"], progress=snap["progress"],
        error=snap["error"])


@app.route("/refresh")
def refresh():
    trigger_update()
    return jsonify({"status": "refresh started — check /status for progress"})


@app.route("/status")
def status():
    _ensure_started()
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
    if not TIINGO_TOKEN:
        return jsonify({"status": "error", "detail": "TIINGO_TOKEN not set"})
    try:
        df = tiingo_history("VFIAX", years=0.02)
        if df is None:  return jsonify({"status": "not found"})
        if df.empty:    return jsonify({"status": "empty"})
        return jsonify({"status": "ok",
                        "VFIAX_last_close": round(df["adjClose"].dropna().iloc[-1], 2),
                        "rows": len(df)})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)})


@app.route("/api/data")
def api_data():
    with _lock:
        return jsonify(cache["ranked"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
