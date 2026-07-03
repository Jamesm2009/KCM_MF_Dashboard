"""
Fund Performance Dashboard — Tiingo + Upstash Redis cache
Redis cache survives Render spindowns and is shared across all browsers/devices.
Daily refresh via /refresh. Serves from cache instantly on repeat visits.
RS Score: (1D×0.10) + (1W×0.20) + (1M×0.30) + (3M×0.40)
"""

from flask import Flask, render_template, jsonify
import requests
import pandas as pd
import threading
import time
import json, os
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

app = Flask(__name__)
CT  = ZoneInfo("America/Chicago")

TIINGO_TOKEN  = os.environ.get("TIINGO_TOKEN", "")
TIINGO_BASE   = "https://api.tiingo.com/tiingo/daily"
REDIS_URL     = os.environ.get("UPSTASH_REDIS_REST_URL", "")
REDIS_TOKEN   = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
REDIS_KEY_MF  = "mf_dashboard_cache"
REDIS_KEY_PRG = "mf_dashboard_progress"

# ── Z-Score chart benchmarks ──────────────────────────────────────────────────
# These get their Z-scores computed alongside fund data and displayed as
# reference markers on the Z-Score Chart tab.
BENCHMARKS = [
    {"symbol": "SPY", "name": "S&P 500 ETF",           "role": "us_equity"},
    {"symbol": "VGK", "name": "FTSE Europe ETF",       "role": "intl_equity"},
    {"symbol": "TLT", "name": "20+ Yr Treasury ETF",   "role": "bonds"},
    {"symbol": "DBC", "name": "Broad Commodity ETF",   "role": "commodity"},
]


def classify_fund(fund):
    """
    Return chart-filter category for a fund based on its category + type text.
    Used only by the Z-Score Chart; does not affect the main table.
    Options: us_equity | intl_equity | bond | target_date | balanced | commodity
    """
    cat  = (fund.get("category") or "").lower()
    typ  = (fund.get("type") or "").lower()
    name = (fund.get("name") or "").lower()
    hay  = typ + " " + name

    if cat == "target_date":
        return "target_date"
    if cat == "balanced":
        return "balanced"
    if cat == "bond":
        # Templeton Global Bond doubles as USD/Intl proxy
        return "bond"
    # Everything else has category == 'equity' in current funds.json
    if "commodit" in hay:
        return "commodity"
    if "intl" in hay or "international" in hay or "europacific" in hay or "emerging" in hay:
        return "intl_equity"
    if "us + intl" in hay:
        # SmallCaps World spans both; treat as intl for chart
        return "intl_equity"
    return "us_equity"


def target_date_short_label(name):
    """Turn '2010 T.R. Price Retirement' into '2010' for chart labels."""
    if not name:
        return ""
    for token in name.split():
        if token.isdigit() and len(token) == 4:
            return token
    return name

cache = {
    "data": {}, "ranked": [], "last_updated": "Loading...",
    "vix_signal": "grey", "vix9d_value": "—", "vix_value": "—",
    "phase": 0, "progress": "Starting...", "error": None, "ttm_last_updated": "—",
    "benchmarks": {},
}
_lock    = threading.Lock()
_started = False


def load_funds():
    with open("funds.json", "r") as f:
        return json.load(f)


# ── Upstash Redis helpers ─────────────────────────────────────────────────────

def redis_set(key, value, ex_seconds=90000):
    """Store JSON value in Redis."""
    if not REDIS_URL or not REDIS_TOKEN:
        return False
    try:
        payload = json.dumps(value)
        r = requests.post(
            REDIS_URL,
            headers={"Authorization": f"Bearer {REDIS_TOKEN}",
                     "Content-Type": "application/json"},
            json=["SET", key, payload, "EX", ex_seconds],
            timeout=10
        )
        return r.status_code == 200
    except Exception as e:
        print(f"  Redis SET error: {e}")
        return False


def redis_get(key):
    """Retrieve and parse JSON value from Redis."""
    if not REDIS_URL or not REDIS_TOKEN:
        return None
    try:
        r = requests.post(
            REDIS_URL,
            headers={"Authorization": f"Bearer {REDIS_TOKEN}",
                     "Content-Type": "application/json"},
            json=["GET", key],
            timeout=10
        )
        if r.status_code != 200:
            return None
        result = r.json().get("result")
        if result is None:
            return None
        return json.loads(result)
    except Exception as e:
        print(f"  Redis GET error: {e}")
        return None


def redis_del(key):
    if not REDIS_URL or not REDIS_TOKEN:
        return
    try:
        requests.post(
            REDIS_URL,
            headers={"Authorization": f"Bearer {REDIS_TOKEN}",
                     "Content-Type": "application/json"},
            json=["DEL", key],
            timeout=10
        )
    except Exception:
        pass


def save_to_redis():
    """Save full cache to Redis."""
    payload = {
        "data":         cache["data"],
        "last_updated": cache["last_updated"],
        "vix_signal":   cache["vix_signal"],
        "vix9d_value":  str(cache["vix9d_value"]),
        "vix_value":    str(cache["vix_value"]),
        "phase": cache["phase"],
        "ttm_last_updated": cache.get("ttm_last_updated", "—"),
        "benchmarks":   cache.get("benchmarks", {}),
    }
    ok = redis_set(REDIS_KEY_MF, payload)
    print(f"  Redis save: {'OK' if ok else 'FAILED'} ({len(cache['data'])} funds)")


def load_from_redis():
    """Restore cache from Redis. Returns True if full cache found."""
    print("  Checking Redis for cached data...")
    payload = redis_get(REDIS_KEY_MF)
    if not payload:
        print("  No Redis cache found.")
        return False
    cache["data"]         = payload.get("data", {})
    cache["last_updated"] = payload.get("last_updated", "—")
    cache["vix_signal"]   = payload.get("vix_signal", "grey")
    cache["vix9d_value"]  = payload.get("vix9d_value", "—")
    cache["vix_value"]    = payload.get("vix_value", "—")
    cache["phase"] = payload.get("phase", 0)
    cache["ttm_last_updated"] = payload.get("ttm_last_updated", "—")
    cache["benchmarks"] = payload.get("benchmarks", {})

    # Backfill chart fields on funds cached before Z-Score chart was added
    try:
        funds_lookup = {f["symbol"]: f for f in load_funds()}
    except Exception:
        funds_lookup = {}
    for sym, row in cache["data"].items():
        if "chart_category" not in row or "chart_label" not in row:
            fund_def = funds_lookup.get(sym, {
                "symbol": sym, "name": row.get("name",""),
                "type":   row.get("type",""), "category": row.get("category",""),
            })
            row["chart_category"] = classify_fund(fund_def)
            row["chart_label"] = (target_date_short_label(row.get("name",""))
                                  if row.get("category") == "target_date"
                                  else sym)

    rebuild_ranked()
    n = len(cache["data"])
    print(f"  Redis restored {n} funds (phase={cache['phase']}).")
    return n > 0


def save_progress(completed_symbols):
    """Save list of completed symbols so we can resume after spindown."""
    redis_set(REDIS_KEY_PRG, list(completed_symbols), ex_seconds=90000)


def load_progress():
    """Return set of already-completed symbols."""
    result = redis_get(REDIS_KEY_PRG)
    if isinstance(result, list):
        return set(result)
    return set()


# ── Tiingo ────────────────────────────────────────────────────────────────────

def tiingo_history(symbol, years=3):
    if not TIINGO_TOKEN:
        raise ValueError("TIINGO_TOKEN not set")
    start  = (date.today() - timedelta(days=int(365*years+10))).strftime("%Y-%m-%d")
    url    = f"{TIINGO_BASE}/{symbol}/prices"
    params = {"startDate": start, "token": TIINGO_TOKEN, "resampleFreq": "daily"}
    while True:
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 429:
                now       = datetime.now(CT)
                next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                wait_secs = int((next_hour - now).total_seconds()) + 120
                resume_at = (now + timedelta(seconds=wait_secs)).strftime("%H:%M")
                msg = f"Rate limit — resuming at {resume_at} CT ({wait_secs//60} min)"
                print(f"    429 {symbol} — {msg}")
                with _lock:
                    cache["progress"] = msg
                    save_to_redis()
                time.sleep(wait_secs)
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
            print(f"    timeout {symbol} — retrying in 10s")
            time.sleep(10)


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
    c    = closes.dropna()
    tail = c.tail(days).values
    if len(tail) < 2:
        return ""
    mn, mx = tail.min(), tail.max()
    if mn == mx:
        return ""
    n   = len(tail) - 1
    pts = [f"{round(i/n*w,1)},{round((1-(v-mn)/(mx-mn))*(h-2)+1,1)}"
           for i, v in enumerate(tail)]
    sma63 = c.tail(63).mean() if len(c) >= 63 else c.mean()
    col   = "#16a34a" if c.iloc[-1] > sma63 else "#dc2626"
    sma_pts = []
    for i, v in enumerate(tail):
        window = tail[max(0, i-62):i+1]
        sv = window.mean()
        sma_pts.append(f"{round(i/n*w,1)},{round((1-(sv-mn)/(mx-mn))*(h-2)+1,1)}")
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
            f'xmlns="http://www.w3.org/2000/svg">'
            f'<polyline points="{" ".join(sma_pts)}" fill="none" stroke="#9ca3af" '
            f'stroke-width="1" stroke-dasharray="2,2" stroke-linejoin="round" stroke-linecap="round"/>'
            f'<polyline points="{" ".join(pts)}" fill="none" stroke="{col}" '
            f'stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>'
            f'</svg>')

def price_bar_data(closes):
    c = closes.dropna()
    if len(c) < 2:
        return None, None, None, None
    lo, hi = round(c.min(), 2), round(c.max(), 2)
    last   = round(c.iloc[-1], 2)
    pct    = round((last - lo) / (hi - lo) * 100, 1) if hi > lo else 50.0
    return lo, hi, last, pct


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


# ── VIX ───────────────────────────────────────────────────────────────────────

def fetch_vix():
    try:
        df_vixy = tiingo_history("VIXY", years=0.1)
        time.sleep(3)
        df_vxx  = tiingo_history("VXX",  years=0.1)
        if df_vixy is None or df_vxx is None or df_vixy.empty or df_vxx.empty:
            return "grey", "—", "—"
        v9  = round(df_vixy["adjClose"].dropna().iloc[-1], 2)
        vix = round(df_vxx["adjClose"].dropna().iloc[-1],  2)
        sig = "grey" if abs(v9-vix) < 0.10 else ("red" if v9 > vix else "green")
        return sig, v9, vix
    except Exception as e:
        print(f"  VIX error: {e}")
        return "grey", "—", "—"


# ── Main update ───────────────────────────────────────────────────────────────

def run_update():
    with _lock:
        cache["phase"]  = 1
        cache["error"]  = None

    time.sleep(10)

    try:
        if not TIINGO_TOKEN:
            with _lock:
                cache["error"] = "TIINGO_TOKEN not set."
                cache["phase"] = 4
            return

        funds     = load_funds()
        total     = len(funds)
        completed = load_progress()
        remaining = [f for f in funds if f["symbol"] not in completed]

        if completed:
            print(f"  Resuming: {len(completed)} done, {len(remaining)} remaining")
            with _lock:
                cache["progress"] = f"Resuming — {len(completed)} done, {len(remaining)} to go..."

        for fund in remaining:
            ticker   = fund["symbol"]
            name     = fund.get("name", ticker)
            category = fund.get("category", "equity")
            ftype    = fund.get("type", "")
            ms_url   = fund.get("morningstar_url",
                       f"https://www.morningstar.com/search#q={ticker}")
            ttm      = fund.get("ttm_yield", None)

            done_count = len(completed)
            with _lock:
                cache["progress"] = f"Loading {done_count+1}/{total}: {ticker}"
            print(f"  [{done_count+1}/{total}] {ticker}")

            try:
                df = tiingo_history(ticker, years=3)
                if df is None or df.empty:
                    print(f"    skip")
                    time.sleep(3)
                    continue

                closes = df["adjClose"].dropna()
                if len(closes) < 30:
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
                rs  = None
                if all(v is not None for v in [d1, w1, m1, m3]):
                    rs = (d1*0.10)+(w1*0.20)+(m1*0.30)+(m3*0.40)

                zsc   = zscore_1yr(closes)
                ob_os = ("Overbought" if zsc and zsc > 2.10
                         else "Oversold" if zsc and zsc < -2.05 else "")
                lo, hi, last_px, bar_pct = price_bar_data(closes)

                row = {
                    "symbol": ticker, "name": name,
                    "type": ftype, "category": category,
                    "chart_category": classify_fund(fund),
                    "chart_label":    (target_date_short_label(name)
                                       if category == "target_date" else ticker),
                    "morningstar_url": ms_url,
                    "sparkline":   make_sparkline(closes),
                    "1D": fmt(d1), "1W": fmt(w1), "1M": fmt(m1),
                    "3M": fmt(m3), "6M": fmt(m6), "YTD": fmt(ytd), "1Y": fmt(y1),
                    "rs_score":   round(rs, 3) if rs is not None else None,
                    "zscore": zsc, "ob_os": ob_os,
                    "trade_flag": sma_flag(closes, 21),
                    "trend_flag": sma_flag(closes, 63),
                    "low3": lo, "high3": hi, "last_price": last_px, "bar_pct": bar_pct,
                    "ttm_yield": ttm, "rank": None,
                }

                with _lock:
                    cache["data"][ticker] = row
                    rebuild_ranked()
                    cache["last_updated"] = datetime.now(CT).strftime("%-m/%-d/%y %H:%M CT")

                completed.add(ticker)
                save_progress(completed)
                save_to_redis()
                print(f"    OK")

            except Exception as e:
                print(f"    ERR {ticker}: {e}")

            time.sleep(3)

        # ── Benchmarks for Z-Score Chart tab ─────────────────────────────────
        with _lock:
            cache["progress"] = "Fetching benchmark Z-scores..."
        print("  Fetching benchmark tickers for Z-Score Chart...")
        bench_out = {}
        for bench in BENCHMARKS:
            bsym = bench["symbol"]
            try:
                bdf = tiingo_history(bsym, years=1.2)
                if bdf is None or bdf.empty:
                    print(f"    {bsym}: no data")
                    continue
                bcloses = bdf["adjClose"].dropna()
                if len(bcloses) < 60:
                    print(f"    {bsym}: insufficient history")
                    continue
                bzsc = zscore_1yr(bcloses)
                bench_out[bsym] = {
                    "symbol": bsym,
                    "name":   bench["name"],
                    "role":   bench["role"],
                    "zscore": bzsc,
                }
                print(f"    {bsym}: z={bzsc}")
            except Exception as e:
                print(f"    {bsym} ERR: {e}")
            time.sleep(3)

        with _lock:
            cache["benchmarks"] = bench_out

        # VIX
        with _lock:
            cache["progress"] = "Fetching VIX signal..."
        sig, v9, vi = fetch_vix()

        with _lock:
            cache["vix_signal"]   = sig
            cache["vix9d_value"]  = v9
            cache["vix_value"]    = vi
            cache["phase"]        = 4
            cache["progress"]     = "Complete"
            cache["last_updated"] = datetime.now(CT).strftime("%-m/%-d/%y %H:%M CT")
            save_to_redis()

        # Clear progress key — full load done
        redis_del(REDIS_KEY_PRG)
        print(f"Done — {len(cache['data'])} funds loaded.")

    except Exception as e:
        import traceback; traceback.print_exc()
        with _lock:
            cache["error"] = str(e)
            cache["phase"] = 4


def trigger_update():
    threading.Thread(target=run_update, daemon=True).start()


def _ensure_started():
    global _started
    if not _started:
        _started = True
        restored = load_from_redis()
        if restored and cache["phase"] == 4:
            print("  Full cache from Redis — no download needed.")
            with _lock:
                cache["progress"] = "Loaded from cache"
        elif restored and cache["phase"] < 4:
            # Partial load saved — resume
            print("  Partial cache found — resuming download.")
            trigger_update()
        else:
            trigger_update()


# ── Routes ────────────────────────────────────────────────────────────────────

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
    """Force a full fresh download — use once daily after market close."""
    redis_del(REDIS_KEY_MF)
    redis_del(REDIS_KEY_PRG)
    with _lock:
        cache["data"]   = {}
        cache["ranked"] = []
        cache["phase"]  = 0
    trigger_update()
    return jsonify({"status": "full refresh started — check /status for progress"})


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


@app.route("/api/data")
def api_data():
    with _lock:
        return jsonify(cache["ranked"])


@app.route("/api/zscores")
def api_zscores():
    """Data for the Z-Score Chart tab: funds + benchmarks."""
    with _lock:
        funds_out = []
        for row in cache["ranked"]:
            if row.get("zscore") is None:
                continue
            funds_out.append({
                "symbol":         row.get("symbol"),
                "name":           row.get("name"),
                "label":          row.get("chart_label") or row.get("symbol"),
                "chart_category": row.get("chart_category") or "us_equity",
                "category":       row.get("category"),
                "zscore":         row.get("zscore"),
            })
        benchmarks = list(cache.get("benchmarks", {}).values())
        return jsonify({
            "funds":        funds_out,
            "benchmarks":   benchmarks,
            "last_updated": cache["last_updated"],
        })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
